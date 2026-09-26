import pytest

pytest.skip("Badges API is switched off (no web UI yet) — see app/main.py",
            allow_module_level=True)

from tests.conftest import make_user, auth_headers


def test_first_trip_and_flight_badges(client):
    user, token = make_user(client, "badge")
    h = auth_headers(token)
    client.post("/api/v1/trips", json={
        "origin_city": "Toronto", "origin_country": "CA",
        "dest_city": "Tokyo", "dest_country": "JP",
        "transport_mode": "flight", "start_date": "2026-03-15",
    }, headers=h)

    earned = {b["id"] for b in client.get("/api/v1/badges/me", headers=h).json() if b["earned"]}
    assert "first_trip" in earned
    assert "first_flight" in earned


def test_five_countries_badge(client):
    user, token = make_user(client, "five")
    h = auth_headers(token)
    trips = [
        {"dest_city": "Tokyo", "dest_country": "JP", "start_date": "2026-01-01"},
        {"dest_city": "London", "dest_country": "GB", "start_date": "2026-02-01"},
        {"dest_city": "Paris", "dest_country": "FR", "start_date": "2026-03-01"},
        {"dest_city": "New York", "dest_country": "US", "start_date": "2026-04-01"},
        {"dest_city": "Montreal", "dest_country": "CA", "start_date": "2026-05-01"},
    ]
    client.post("/api/v1/trips/backfill", json={"trips": trips}, headers=h)
    earned = {b["id"] for b in client.get("/api/v1/badges/me", headers=h).json() if b["earned"]}
    assert "five_countries" in earned
    # 5 consecutive months -> streak_3 badge
    assert "streak_3" in earned


def test_badge_appears_in_feed(client):
    user, token = make_user(client, "feedbadge")
    h = auth_headers(token)
    client.post("/api/v1/trips", json={
        "dest_city": "Paris", "dest_country": "FR", "start_date": "2026-03-15",
    }, headers=h)
    events = {i["event_type"] for i in client.get("/api/v1/feed/me", headers=h).json()["items"]}
    assert "new_trip" in events
    assert "badge_earned" in events
