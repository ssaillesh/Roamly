"""Rating a plan (POST /plan/review) and reading the ratings on the admin Reviews page."""
from tests.conftest import auth_headers, make_user
from tests.test_admin import make_admin, make_real_user

A = (43.65, -79.38)


def _plan(name="Bar Raval"):
    stop = lambda slot, n: {"slot": slot, "label": slot.title(), "icon": "x", "name": n, "vid": f"ovt:{n}",
                            "lat": A[0], "lng": A[1], "est_cost": 20, "time": "7:00 PM"}
    return {"vibe": "chill", "budget": 150, "party_size": 2, "estimated_cost": 80,
            "center": {"lat": A[0], "lng": A[1]}, "intro": "An easy night.",
            "stops": [stop("dinner", name), stop("leisure", "The Rex")]}


def _rate(client, token, **body):
    return client.post("/api/v1/plan/review", headers=auth_headers(token), json={"plan": _plan(), **body})


def test_rating_a_plan_then_adding_a_comment_keeps_one_review(client):
    _, token = make_user(client, "rater")
    first = _rate(client, token, stars=4)
    assert first.status_code == 200, first.text
    assert first.json()["comment"] is None
    second = _rate(client, token, stars=2, comment="  Dinner was   closed when we got there. ")
    assert second.json()["id"] == first.json()["id"]                     # the same plan: updated, not doubled
    assert second.json() == {"id": first.json()["id"], "stars": 2, "comment": "Dinner was closed when we got there."}
    third = _rate(client, token, stars=3)                                 # stars again, no comment: comment kept
    assert third.json()["comment"] == "Dinner was closed when we got there."


def test_a_different_plan_is_a_different_review(client):
    _, token = make_user(client, "rater")
    a = _rate(client, token, stars=5).json()["id"]
    b = client.post("/api/v1/plan/review", headers=auth_headers(token),
                    json={"plan": _plan("Bar Isabel"), "stars": 5}).json()["id"]
    assert a != b


def test_a_trip_can_be_rated(client):
    _, token = make_user(client, "rater")
    r = client.post("/api/v1/plan/review", headers=auth_headers(token),
                    json={"days": [_plan(), _plan("Bar Isabel")], "stars": 4})
    assert r.status_code == 200, r.text


def test_bad_ratings_are_refused(client):
    _, token = make_user(client, "rater")
    assert client.post("/api/v1/plan/review", json={"plan": _plan(), "stars": 4}).status_code in (401, 403)
    assert _rate(client, token, stars=0).status_code == 422
    assert _rate(client, token, stars=6).status_code == 422
    assert _rate(client, token, stars=3, comment="x" * 1001).status_code == 422
    assert client.post("/api/v1/plan/review", headers=auth_headers(token), json={"stars": 3}).status_code == 422


def test_admins_see_ratings_and_mark_them_read(client):
    _, admin = make_admin(client)
    real, token = make_real_user(client)
    rid = _rate(client, token, stars=1, comment="Nothing was open.").json()["id"]
    h = auth_headers(admin)

    d = client.get("/api/v1/admin/reviews?stars=low&show=new", headers=h).json()
    row = next(r for r in d["reviews"] if r["id"] == rid)
    assert row["stars"] == 1 and row["comment"] == "Nothing was open." and row["username"] == real["username"]
    assert row["stops"] == 2 and row["names"] == ["Bar Raval", "The Rex"]
    assert d["summary"]["unread"] >= 1 and len(d["summary"]["by_stars"]) == 5
    assert all(r["id"] != rid for r in client.get("/api/v1/admin/reviews?stars=high", headers=h).json()["reviews"])

    full = client.get(f"/api/v1/admin/reviews/{rid}", headers=h).json()
    assert full["plans"][0]["stops"][0]["name"] == "Bar Raval"           # the plan exactly as it was rated

    assert client.post(f"/api/v1/admin/reviews/{rid}/reviewed", headers=h, json={"value": True}).status_code == 200
    unread = client.get("/api/v1/admin/reviews?show=new", headers=h).json()["reviews"]
    assert all(r["id"] != rid for r in unread)
    # a changed rating is new again
    _rate(client, token, stars=2)
    assert any(r["id"] == rid for r in client.get("/api/v1/admin/reviews?show=new", headers=h).json()["reviews"])


def test_test_accounts_are_left_out_unless_asked(client):
    _, admin = make_admin(client)
    _, token = make_user(client, "tester")                               # an example.com test account
    rid = _rate(client, token, stars=5).json()["id"]
    h = auth_headers(admin)
    assert all(r["id"] != rid for r in client.get("/api/v1/admin/reviews", headers=h).json()["reviews"])
    assert any(r["id"] == rid for r in
               client.get("/api/v1/admin/reviews?include_test=true", headers=h).json()["reviews"])


def test_only_admins_can_read_reviews(client):
    _, token = make_user(client, "nosy")
    assert client.get("/api/v1/admin/reviews", headers=auth_headers(token)).status_code == 403


def test_reviews_export(client):
    _, admin = make_admin(client)
    _, token = make_real_user(client)
    _rate(client, token, stars=3, comment="Fine, a bit far apart.")
    r = client.get("/api/v1/admin/export/reviews.csv", headers=auth_headers(admin))
    assert r.status_code == 200 and "Fine, a bit far apart." in r.text and "Bar Raval · The Rex" in r.text
