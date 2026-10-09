"""Free-tier blocker: metered APIs are never called past their free allowance."""
import uuid

import httpx
import pytest

from app.config import settings
from app.database import SessionLocal
from app.models import ApiUsage
from app.redis_client import redis_client
from app.services import foursquare, ga4, google_places, usage, yelp


@pytest.fixture
def provider(monkeypatch):
    """A made-up provider with a cap of 2 calls a day, cleaned up afterwards."""
    name = f"test_{uuid.uuid4().hex[:8]}"
    monkeypatch.setitem(usage.FREE_TIER, name, ("day", 3, 2, "test"))
    monkeypatch.setattr(settings, "free_tier_only", True)
    yield name
    redis_client.delete(f"quota:{name}:{usage._period_key('day')[0]}")
    with SessionLocal() as db:
        db.query(ApiUsage).filter(ApiUsage.provider == name).delete()
        db.commit()


def test_stops_at_the_cap(provider):
    assert [usage.allow(provider) for _ in range(4)] == [True, True, False, False]
    q = usage.quota_status()[provider]
    assert (q["used"], q["blocked"]) == (2, 2)


def test_cap_of_zero_never_calls(monkeypatch, provider):
    monkeypatch.setitem(usage.FREE_TIER, provider, ("month", 0, 0, "paid only"))
    assert usage.allow(provider) is False


def test_switching_the_blocker_off_allows_everything(monkeypatch, provider):
    monkeypatch.setattr(settings, "free_tier_only", False)
    assert all(usage.allow(provider) for _ in range(5))


def test_no_redis_means_no_call(monkeypatch, provider):
    def down(*a, **k):
        raise ConnectionError("redis down")
    monkeypatch.setattr(redis_client, "incr", down)
    assert usage.allow(provider) is False


def test_a_redis_restart_cant_reset_the_cap(provider):
    # Two calls already saved to Postgres today, Redis counter empty (restart).
    with SessionLocal() as db:
        db.add(ApiUsage(day=usage._period_key("day")[2], provider=provider, calls=2, errors=0))
        db.commit()
    assert usage.allow(provider) is False


def test_paid_products_are_never_called(monkeypatch):
    """Foursquare, Yelp and Google Places stay off even with keys set and the
    blocker switched off: re-enabling them takes a code change."""
    for key in ("yelp_api_key", "foursquare_api_key", "google_places_api_key"):
        monkeypatch.setattr(settings, key, "test-key")
    monkeypatch.setattr(settings, "free_tier_only", False)

    def no_http(*a, **k):
        raise AssertionError("a paid product was called")
    monkeypatch.setattr(httpx, "get", no_http)
    monkeypatch.setattr(httpx, "post", no_http)

    q = uuid.uuid4().hex
    assert not (yelp.available() or foursquare.available() or google_places.available())
    assert not any(usage.allow(p) for p in ("yelp", "foursquare", "google_places"))
    assert yelp.search(43.65, -79.38, term=q) == []
    assert foursquare.search(43.65, -79.38, query=q) == []
    assert google_places.search(43.65, -79.38, query=q) == []


@pytest.mark.parametrize("pid,creds,expect", [
    ("", "", "GA4_PROPERTY_ID isn't set"),
    ("G-P8639TWYYC", "{}", "should be the property number"),
    ("558232293", "", "GA4_CREDENTIALS_JSON isn't set"),
    ("558232293", '{"type": "service_acc', "isn't valid JSON"),
    ("558232293", '{"type": "service_account"}', "isn't a service-account key"),
    ("558232293", '{"client_email": "a@b", "private_key": "k"}', None),
])
def test_ga4_says_what_is_wrong(monkeypatch, pid, creds, expect):
    monkeypatch.setattr(settings, "ga4_property_id", pid)
    monkeypatch.setattr(settings, "ga4_credentials_json", creds)
    problem = ga4.problem()
    assert (problem is None) if expect is None else (expect in problem)
