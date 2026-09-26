import pytest

pytest.skip("Trips API is switched off (no web UI yet) — see app/main.py",
            allow_module_level=True)

from tests.conftest import make_user, auth_headers


def test_create_trip_processes_distance_and_stats(client):
    user, token = make_user(client, "trip")
    h = auth_headers(token)

    resp = client.post("/api/v1/trips", json={
        "title": "Spring break in Tokyo",
        "origin_city": "Toronto", "origin_country": "CA",
        "dest_city": "Tokyo", "dest_country": "JP",
        "transport_mode": "flight",
        "start_date": "2026-03-15", "end_date": "2026-03-25",
    }, headers=h)
    assert resp.status_code == 201, resp.text
    trip = resp.json()
    assert trip["dest_country"] == "JP"

    # Eager worker has run: refetch the trip and expect a computed distance.
    detail = client.get(f"/api/v1/trips/{trip['id']}", headers=h).json()
    assert detail["status"] == "done"
    assert detail["distance_km"] and detail["distance_km"] > 9000  # Toronto->Tokyo ~10,000km

    me = client.get("/api/v1/users/me", headers=h).json()
    assert me["total_countries"] == 1
    assert me["total_cities"] == 1
    assert me["total_trips"] == 1
    assert me["total_km"] > 9000


def test_backfill_and_map(client):
    user, token = make_user(client, "back")
    h = auth_headers(token)
    resp = client.post("/api/v1/trips/backfill", json={"trips": [
        {"dest_city": "Tokyo", "dest_country": "JP", "start_date": "2024-03-15", "transport_mode": "flight"},
        {"dest_city": "London", "dest_country": "GB", "start_date": "2023-07-01", "transport_mode": "flight"},
        {"dest_city": "Paris", "dest_country": "FR", "start_date": "2023-08-01", "transport_mode": "train"},
    ]}, headers=h)
    assert resp.status_code == 201
    assert resp.json()["created"] == 3

    stats = client.get(f"/api/v1/users/{user['username']}/stats", headers=h).json()
    assert stats["total_countries"] == 3
    assert "AS" in stats["continents_visited"]
    assert "EU" in stats["continents_visited"]

    m = client.get(f"/api/v1/users/{user['username']}/map", headers=h).json()
    codes = {c["code"] for c in m["countries"]}
    assert codes == {"JP", "GB", "FR"}
    tokyo = next(c for c in m["cities"] if c["name"] == "Tokyo")
    assert abs(tokyo["lat"] - 35.6762) < 0.01


def test_delete_trip_updates_stats(client):
    user, token = make_user(client, "del")
    h = auth_headers(token)
    r = client.post("/api/v1/trips", json={
        "dest_city": "Paris", "dest_country": "FR", "start_date": "2026-01-01",
    }, headers=h)
    tid = r.json()["id"]
    assert client.get("/api/v1/users/me", headers=h).json()["total_trips"] == 1
    assert client.delete(f"/api/v1/trips/{tid}", headers=h).status_code == 204
    assert client.get("/api/v1/users/me", headers=h).json()["total_trips"] == 0
