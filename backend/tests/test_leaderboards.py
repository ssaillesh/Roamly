import pytest

pytest.skip("Leaderboards API is switched off (no web UI yet) — see app/main.py",
            allow_module_level=True)

from tests.conftest import make_user, auth_headers


def _befriend(client, a_user, a_token, b_user, b_token):
    client.post("/api/v1/friends/request", json={"username": b_user["username"]},
                headers=auth_headers(a_token))
    reqs = client.get("/api/v1/friends/requests", headers=auth_headers(b_token)).json()
    fid = reqs[0]["friendship_id"]
    client.post(f"/api/v1/friends/accept/{fid}", headers=auth_headers(b_token))


def test_friend_leaderboard_ranks_by_countries(client):
    a_user, a_token = make_user(client, "lba")
    b_user, b_token = make_user(client, "lbb")
    _befriend(client, a_user, a_token, b_user, b_token)

    # A visits 3 countries, B visits 1
    client.post("/api/v1/trips/backfill", json={"trips": [
        {"dest_city": "Tokyo", "dest_country": "JP", "start_date": "2026-01-01"},
        {"dest_city": "London", "dest_country": "GB", "start_date": "2026-02-01"},
        {"dest_city": "Paris", "dest_country": "FR", "start_date": "2026-03-01"},
    ]}, headers=auth_headers(a_token))
    client.post("/api/v1/trips", json={
        "dest_city": "Montreal", "dest_country": "CA", "start_date": "2026-01-01",
    }, headers=auth_headers(b_token))

    board = client.get("/api/v1/leaderboards/friends?metric=countries&period=all_time",
                       headers=auth_headers(a_token)).json()
    assert board["rankings"][0]["user"]["username"] == a_user["username"]
    assert board["rankings"][0]["value"] == 3.0
    assert board["my_rank"] == 1


def test_global_leaderboard(client):
    user, token = make_user(client, "glob")
    client.post("/api/v1/trips", json={
        "dest_city": "Tokyo", "dest_country": "JP", "start_date": "2026-03-15",
    }, headers=auth_headers(token))
    board = client.get("/api/v1/leaderboards/global?metric=countries&limit=100").json()
    assert board["metric"] == "countries"
    assert any(r["user"]["username"] == user["username"] for r in board["rankings"])
