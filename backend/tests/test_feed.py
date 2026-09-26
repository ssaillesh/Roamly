import pytest

pytest.skip("Feed API is switched off (no web UI yet) — see app/main.py",
            allow_module_level=True)

from tests.conftest import make_user, auth_headers


def test_friends_feed_shows_friend_activity(client):
    a_user, a_token = make_user(client, "fa")
    b_user, b_token = make_user(client, "fb")

    # befriend
    client.post("/api/v1/friends/request", json={"username": b_user["username"]},
                headers=auth_headers(a_token))
    fid = client.get("/api/v1/friends/requests", headers=auth_headers(b_token)).json()[0]["friendship_id"]
    client.post(f"/api/v1/friends/accept/{fid}", headers=auth_headers(b_token))

    # B logs a trip
    client.post("/api/v1/trips", json={
        "title": "Weekend in Montreal", "dest_city": "Montreal", "dest_country": "CA",
        "start_date": "2026-06-20",
    }, headers=auth_headers(b_token))

    # A's friends-feed includes B's trip
    feed = client.get("/api/v1/feed", headers=auth_headers(a_token)).json()
    usernames = {i["user"]["username"] for i in feed["items"]}
    assert b_user["username"] in usernames
    trip_items = [i for i in feed["items"] if i["event_type"] == "new_trip"]
    assert any(i["trip"]["dest_city"] == "Montreal" for i in trip_items)


def test_share_card_generation(client):
    user, token = make_user(client, "share")
    client.post("/api/v1/trips", json={
        "dest_city": "Tokyo", "dest_country": "JP", "start_date": "2026-03-15",
    }, headers=auth_headers(token))
    resp = client.post("/api/v1/share/card", json={"card_type": "year_recap", "year": 2026},
                       headers=auth_headers(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["image_url"].endswith(".png")
