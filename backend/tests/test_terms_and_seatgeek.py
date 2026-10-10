"""Terms acceptance (sign-up requires it; existing accounts are asked once) and the
SeatGeek source, used the way its API terms allow: concerts only, the search point
rounded, a short cache, and only in the Nightlife tab (never the AI planner)."""
import uuid

import pytest

from app.config import settings
from app.database import SessionLocal
from app.legal import TERMS_VERSION
from app.models import User
from app.redis_client import redis_client
from app.services import live_events, nightlife, seatgeek, usage
from tests.conftest import auth_headers, make_user


def _body(**over):
    uid = uuid.uuid4().hex[:8]
    body = {"email": f"terms_{uid}@example.com", "username": f"terms_{uid}", "display_name": "T",
            "password": "password123"}
    body.update(over)
    return body


def test_sign_up_needs_the_terms_accepted(client):
    r = client.post("/api/v1/auth/register", json=_body())
    assert r.status_code == 422 and "Terms" in r.json()["detail"]
    r = client.post("/api/v1/auth/register", json=_body(accept_terms=True))
    assert r.status_code == 201
    me = client.get("/api/v1/users/me", headers=auth_headers(r.json()["access_token"])).json()
    assert me["needs_terms"] is False


def test_existing_accounts_are_asked_once(client):
    user, token = make_user(client, "older")
    with SessionLocal() as db:                       # an account from before the terms existed
        u = db.get(User, uuid.UUID(user["id"]))
        u.terms_version = u.terms_accepted_at = None
        db.commit()
    assert client.get("/api/v1/users/me", headers=auth_headers(token)).json()["needs_terms"] is True
    r = client.post("/api/v1/users/me/accept-terms", headers=auth_headers(token))
    assert r.status_code == 200 and r.json()["needs_terms"] is False
    with SessionLocal() as db:
        assert db.get(User, uuid.UUID(user["id"])).terms_version == TERMS_VERSION


class _Resp:
    status_code = 200

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body

    def raise_for_status(self):
        pass


SG_EVENT = {"title": "Shub & Handsome Tiger (19+)", "type": "concert", "datetime_local": "2026-10-10T20:00:00",
            "datetime_utc": "2026-10-11T00:00:00", "enddatetime_utc": None, "time_tbd": False,
            "url": "https://seatgeek.com/x", "stats": {"lowest_price": 25},
            "taxonomies": [{"name": "concert"}], "performers": [{"name": "Shub", "genres": [{"name": "Electronic"}]}],
            "venue": {"name": "The Dance Cave", "location": {"lat": 43.6649, "lon": -79.4105}}}


@pytest.fixture
def sg(monkeypatch):
    monkeypatch.setattr(settings, "seatgeek_client_id", "test-id")
    sent = []

    def get(url, params=None, **kw):
        sent.append(params)
        return _Resp({"events": [SG_EVENT]})
    monkeypatch.setattr(seatgeek.httpx, "get", get)
    for k in redis_client.scan_iter("sg:*"):
        redis_client.delete(k)
    yield sent
    for k in redis_client.scan_iter("sg:*"):
        redis_client.delete(k)


def test_seatgeek_events_and_the_minimum_data(sg):
    got = seatgeek.search_events(43.665123, -79.410987, radius_km=10)
    ev = got[0]
    assert (ev["name"], ev["venue"], ev["source"]) == ("Shub & Handsome Tiger (19+)", "The Dance Cave", "seatgeek")
    assert ev["start"] == "2026-10-11T00:00:00Z" and ev["time"] == "20:00:00"
    assert ev["genre"] == "Electronic" and ev["adults_only"] is True and ev["price_min"] == 25
    params = sg[0]
    assert (params["lat"], params["lon"]) == (43.67, -79.41)          # rounded to ~1 km
    assert params["taxonomies.name"] == "concert"
    seatgeek.search_events(43.665123, -79.410987, radius_km=10)
    assert len(sg) == 1                                                # cached, not re-fetched


def test_seatgeek_is_off_without_an_id_or_over_the_cap(monkeypatch, sg):
    monkeypatch.setattr(settings, "seatgeek_client_id", "")
    assert seatgeek.search_events(43.65, -79.38) == [] and sg == []
    monkeypatch.setattr(settings, "seatgeek_client_id", "test-id")
    monkeypatch.setattr(usage, "allow", lambda p: p != "seatgeek")
    assert seatgeek.search_events(43.65, -79.38) == [] and sg == []


def test_seatgeek_shows_in_nightlife_only(monkeypatch, sg):
    monkeypatch.setattr(nightlife.ticketmaster, "search_events", lambda *a, **kw: [])
    feed = nightlife.feed(43.65, -79.40, window="tonight", radius_km=10)
    assert any(e["source"] == "seatgeek" for e in feed["events"])

    def never(*a, **kw):
        raise AssertionError("SeatGeek data must not reach the planner or its AI")
    monkeypatch.setattr(seatgeek, "search_events", never)
    live_events.happening(43.65, -79.40, window="tonight")             # What's on + planner events
