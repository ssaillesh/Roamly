"""Share the night: public plan links and the story card."""
import io

import pytest
from PIL import Image

from app.services import share_card
from tests.conftest import auth_headers, make_user

PLAN = {
    "title": "Tapas, jazz and a last drink",
    "area": "Queen West",
    "date": "2026-11-03",
    "party_size": 2,
    "estimated_cost": 142,
    "budget": 160,
    "walk_km": 1.6,
    "transport": "walk",
    "stops": [
        {"name": "Bar Raval", "time": "7:00 PM", "slot": "dinner", "est_cost": 38,
         "lat": 43.6556, "lng": -79.4100, "image": "https://example.com/raval.jpg",
         "why": "Small plates at the long marble bar."},
        {"name": "The Rex", "time": "9:00 PM", "slot": "event", "est_cost": 15, "lat": 43.6505, "lng": -79.3889},
        {"name": "Civil Liberties", "time": "10:45 PM", "slot": "drinks", "est_cost": 18},
    ],
}


@pytest.fixture(autouse=True)
def _no_photos(monkeypatch):
    # Never fetch real photos in tests.
    monkeypatch.setattr(share_card, "fetch_image", lambda url, timeout=4.0: None)
    share_card._cache.clear()


def test_share_needs_an_account(client):
    assert client.post("/api/v1/share/plan", json=PLAN).status_code in (401, 403)


def test_share_link_round_trip(client):
    _, token = make_user(client, "sharer")
    r = client.post("/api/v1/share/plan", json=PLAN, headers=auth_headers(token))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["url"].endswith("/n/" + out["code"])
    assert out["image_path"] == f"/share/plan/{out['code']}/card.jpg"
    assert len(out["code"]) == 8 and out["code"].isalnum()

    # anyone can read it, without signing in
    got = client.get(f"/api/v1/share/plan/{out['code']}")
    assert got.status_code == 200
    body = got.json()
    assert body["title"] == PLAN["title"]
    assert [s["name"] for s in body["plan"]["stops"]] == ["Bar Raval", "The Rex", "Civil Liberties"]

    # the same plan shared again keeps its link
    again = client.post("/api/v1/share/plan", json=PLAN, headers=auth_headers(token)).json()
    assert again["code"] == out["code"]


def test_deleting_the_account_takes_its_links_down(client):
    # The privacy policy promises this.
    _, token = make_user(client, "sharer")
    code = client.post("/api/v1/share/plan", json=PLAN, headers=auth_headers(token)).json()["code"]
    assert client.delete("/api/v1/auth/account", headers=auth_headers(token)).status_code == 204
    assert client.get(f"/api/v1/share/plan/{code}").status_code == 404


def test_shared_plan_is_cleaned(client):
    _, token = make_user(client, "sharer")
    dirty = {**PLAN, "secret_field": "x", "title": "  A   night  ",
             "stops": [{**PLAN["stops"][0], "image": "http://insecure.example/x.jpg",
                        "url": "javascript:alert(1)", "why": "w" * 2000, "extra": 1}]}
    code = client.post("/api/v1/share/plan", json=dirty, headers=auth_headers(token)).json()["code"]
    plan = client.get(f"/api/v1/share/plan/{code}").json()["plan"]
    assert "secret_field" not in plan
    assert plan["title"] == "A night"
    stop = plan["stops"][0]
    assert "extra" not in stop
    assert "image" not in stop          # https only (drawn server-side)
    assert "url" not in stop            # no javascript: links on the public page
    assert len(stop["why"]) == 400


def test_bad_plans_are_refused(client):
    _, token = make_user(client, "sharer")
    h = auth_headers(token)
    assert client.post("/api/v1/share/plan", json={**PLAN, "stops": []}, headers=h).status_code == 422
    assert client.post("/api/v1/share/plan", json={**PLAN, "stops": PLAN["stops"] * 5}, headers=h).status_code == 422
    assert client.post("/api/v1/share/plan", json={**PLAN, "date": "tomorrow"}, headers=h).status_code == 422


def test_unknown_and_malformed_codes(client):
    assert client.get("/api/v1/share/plan/zzzzzzzz").status_code == 404
    assert client.get("/api/v1/share/plan/../etc").status_code in (404, 422)
    assert client.get("/api/v1/share/plan/ABC!").status_code == 422


def test_story_card_image(client):
    _, token = make_user(client, "sharer")
    code = client.post("/api/v1/share/plan", json=PLAN, headers=auth_headers(token)).json()["code"]
    r = client.get(f"/api/v1/share/plan/{code}/card.jpg")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert "max-age" in r.headers["cache-control"]
    img = Image.open(io.BytesIO(r.content))
    assert img.size == (1080, 1920)


def test_split_title():
    assert share_card.split_title("Tapas, jazz and a last drink") == ("Tapas, jazz", "and a last drink")
    assert share_card.split_title("Chill night with friends") == ("Chill night", "with friends")
    assert share_card.split_title("Romantic date") == ("Romantic", "date")
    assert share_card.split_title("Tonight") == ("Tonight", "")


@pytest.mark.parametrize("url", [
    "http://example.com/a.jpg",            # not https
    "https://127.0.0.1/a.jpg",             # loopback
    "https://10.0.0.5/a.jpg",              # private
    "https://169.254.169.254/latest/meta-data",   # cloud metadata
    "https://localhost/a.jpg",
    "file:///etc/passwd",
    None,
])
def test_photo_fetch_refuses_unsafe_urls(url, monkeypatch):
    monkeypatch.undo()   # the real fetcher, not the autouse stub
    called = []
    monkeypatch.setattr(share_card.httpx, "stream", lambda *a, **k: called.append(a) or (_ for _ in ()).throw(AssertionError))
    assert share_card.fetch_image(url) is None
    assert not called


def test_photo_fetch_rechecks_redirects(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(share_card, "_public_host", lambda host: host == "cdn.example.com")

    class Resp:
        is_redirect, status_code = True, 302
        headers = {"location": "https://127.0.0.1/private.jpg"}
        def __enter__(self): return self
        def __exit__(self, *a): return False

    seen = []
    monkeypatch.setattr(share_card.httpx, "stream", lambda m, url, **k: seen.append(url) or Resp())
    assert share_card.fetch_image("https://cdn.example.com/a.jpg") is None
    assert seen == ["https://cdn.example.com/a.jpg"]    # the redirect target was never requested
