"""'What's happening' — time windows, both event sources (Ticketmaster + the City
of Toronto festivals feed), merging, and the /plan/chat events reply. Every
external call is faked; "now" is pinned so windows are deterministic."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import app.api.plan as plan_api
from app.config import settings
from app.redis_client import redis_client
from app.services import city_events, events as ticketmaster, live_events
from tests.conftest import make_user, auth_headers

TO = ZoneInfo("America/Toronto")
WED_7PM = datetime(2026, 9, 23, 19, 0, tzinfo=TO)          # a Wednesday evening
SAT_2PM = datetime(2026, 9, 26, 14, 0, tzinfo=TO)
SANKOFA = (43.6561, -79.3802)


def _local(dt):
    return dt.astimezone(TO).replace(tzinfo=None)


# ---------------------------------------------------------------- windows

def test_windows():
    s, e = live_events.window_range("now", "America/Toronto", WED_7PM)
    assert (_local(s), _local(e)) == (datetime(2026, 9, 23, 17), datetime(2026, 9, 23, 23))
    s, e = live_events.window_range("tonight", "America/Toronto", WED_7PM)
    assert _local(e) == datetime(2026, 9, 24, 3)               # until 3am
    s, e = live_events.window_range("weekend", "America/Toronto", WED_7PM)
    assert (_local(s), _local(e)) == (datetime(2026, 9, 25, 17), datetime(2026, 9, 27, 23, 59))
    s, e = live_events.window_range("weekend", "America/Toronto", SAT_2PM)
    assert _local(s) == datetime(2026, 9, 26, 12)              # already the weekend: from now
    s, _ = live_events.window_range("now", "Not/AZone", WED_7PM)
    assert _local(s) == datetime(2026, 9, 23, 17)              # bad zone → Toronto default


# ---------------------------------------------------------------- Ticketmaster

class _Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("err", request=None, response=None)

    def json(self):
        return self._data


def test_ticketmaster_window_and_radius(monkeypatch):
    monkeypatch.setattr(settings, "ticketmaster_api_key", "test-key")
    seen = {}

    def fake_get(url, params=None, timeout=None):
        seen.update(params)
        return _Resp({"_embedded": {"events": [{
            "name": "Jazz at the Rex", "url": "https://tm/x",
            "dates": {"start": {"localDate": "2026-09-23", "localTime": "20:00:00",
                                "dateTime": "2026-09-24T00:00:00Z"}},
            "_embedded": {"venues": [{"name": "The Rex", "location": {"latitude": "43.65", "longitude": "-79.39"}}]},
        }]}})
    monkeypatch.setattr(ticketmaster.httpx, "get", fake_get)
    start, end = live_events.window_range("now", "America/Toronto", WED_7PM)
    for k in redis_client.scan_iter("tm:*"):               # results are cached per area/window
        redis_client.delete(k)
    out = ticketmaster.search_events(43.6, -79.39, radius_km=7.4, start_utc=start, end_utc=end)
    assert seen["radius"] == 7 and seen["startDateTime"] == "2026-09-23T21:00:00Z"
    assert seen["endDateTime"] == "2026-09-24T03:00:00Z"
    assert out[0]["source"] == "ticketmaster" and out[0]["start"] == "2026-09-24T00:00:00Z"


# ---------------------------------------------------------------- City of Toronto feed

FEED = {"value": [
    {"calEvent": {"eventName": "Sankofa Square Harvest Festival", "freeEvent": "Yes",
                  "eventWebsite": "https://example.org/harvest", "categoryString": "Festival,Food",
                  "dates": [{"allDay": False, "startDateTime": "2026-09-23T17:00:00",
                             "endDateTime": "2026-09-23T22:00:00"}],
                  "locations": [{"locationName": "Sankofa Square", "coords": {"lat": 43.6561, "lng": -79.3802}}]}},
    {"calEvent": {"eventName": "Far Away Fair", "freeEvent": "Yes",
                  "dates": [{"startDateTime": "2026-09-23T18:00:00", "endDateTime": "2026-09-23T21:00:00"}],
                  "locations": [{"locationName": "Scarborough", "coords": {"lat": 43.78, "lng": -79.19}}]}},
    {"calEvent": {"eventName": "Last Month's Show", "freeEvent": "No", "cost": {"adult": 25},
                  "dates": [{"startDateTime": "2026-08-01T19:00:00", "endDateTime": "2026-08-01T22:00:00"}],
                  "locations": [{"locationName": "Massey Hall", "coords": {"lat": 43.654, "lng": -79.379}}]}},
]}


@pytest.fixture
def clean_feed_cache():
    for k in (city_events._CACHE_KEY, city_events._DOWN_KEY):
        redis_client.delete(k)
    yield
    for k in (city_events._CACHE_KEY, city_events._DOWN_KEY):
        redis_client.delete(k)


def test_city_feed_free_events_in_window(monkeypatch, clean_feed_cache):
    monkeypatch.setattr(city_events.httpx, "get", lambda *a, **k: _Resp(FEED))
    start, end = live_events.window_range("now", "America/Toronto", WED_7PM)
    out = city_events.search(*SANKOFA, start_utc=start, end_utc=end, radius_km=5)
    assert [e["name"] for e in out] == ["Sankofa Square Harvest Festival"]   # not far, not past
    assert out[0]["free"] is True and out[0]["source"] == "city_toronto" and out[0]["venue"] == "Sankofa Square"


def test_city_feed_down_is_quiet(monkeypatch, clean_feed_cache):
    monkeypatch.setattr(city_events.httpx, "get", lambda *a, **k: _Resp({}, status=403))
    start, end = live_events.window_range("now", "America/Toronto", WED_7PM)
    assert city_events.search(*SANKOFA, start_utc=start, end_utc=end, radius_km=5) == []
    assert redis_client.get(city_events._DOWN_KEY)                 # backs off for a while
    assert city_events.available_for(49.28, -123.12) is False      # only around Toronto


# ---------------------------------------------------------------- merging + chat

def test_merge_dedupes_and_puts_on_now_first(monkeypatch):
    on_now = {"name": "Harvest Festival", "lat": 43.656, "lng": -79.380, "source": "city_toronto",
              "start": "2026-09-23T21:00:00+00:00", "end": "2026-09-24T02:00:00+00:00", "free": True}
    later = {"name": "Jazz at the Rex", "lat": 43.650, "lng": -79.390, "source": "ticketmaster",
             "start": "2026-09-24T01:30:00Z"}
    monkeypatch.setattr(ticketmaster, "search_events", lambda *a, **k: [later, {**on_now, "source": "ticketmaster"}])
    monkeypatch.setattr(city_events, "search", lambda *a, **k: [on_now])
    out = live_events.happening(*SANKOFA, window="now", radius_km=5, now=WED_7PM)
    assert [e["name"] for e in out] == ["Harvest Festival", "Jazz at the Rex"]      # deduped, on-now first
    assert out[0]["distance_km"] is not None


def test_whats_on_routes_to_events(client, monkeypatch):
    _, token = make_user(client, "events")
    monkeypatch.setattr(plan_api.live_events, "available_for", lambda lat, lng: True)
    monkeypatch.setattr(plan_api.live_events, "happening", lambda *a, **k: [
        {"name": "Harvest Festival", "venue": "Sankofa Square", "lat": 43.656, "lng": -79.38,
         "start": "2020-01-01T00:00:00Z", "free": True, "source": "city_toronto", "distance_km": 0.1}])
    r = client.post("/api/v1/plan/chat", headers=auth_headers(token), json={
        "messages": [{"role": "user", "content": "what's happening right now?"}],
        "lat": 43.656, "lng": -79.38, "tz": "America/Toronto"}).json()
    assert r["type"] == "events" and r["window"] == "now" and r["events"][0]["free"] is True

    # and the Happening-now button (structured mode) without any text
    r = client.post("/api/v1/plan/chat", headers=auth_headers(token), json={
        "messages": [], "prefs": {"mode": "tonight", "radius_km": 3}, "lat": 43.656, "lng": -79.38}).json()
    assert r["type"] == "events" and r["window"] == "tonight" and "within 3 km" in r["message"]


def test_nothing_on_is_honest(client, monkeypatch):
    _, token = make_user(client, "events")
    monkeypatch.setattr(plan_api.live_events, "available_for", lambda lat, lng: True)
    monkeypatch.setattr(plan_api.live_events, "happening", lambda *a, **k: [])
    r = client.post("/api/v1/plan/chat", headers=auth_headers(token), json={
        "messages": [], "prefs": {"mode": "now"}, "lat": 43.656, "lng": -79.38}).json()
    assert r["type"] == "events" and r["events"] == [] and "Nothing listed right now" in r["message"]


def test_plan_requests_still_plan():
    req = plan_api.ChatRequest(messages=[])
    assert plan_api._events_window(req, "plan a date tonight") is None
    assert plan_api._events_window(req, "any concerts this weekend") == "weekend"


def test_chat_reports_what_it_understood(client, monkeypatch):
    """The page's 'Planning around' bar mirrors what a typed request was read as."""
    _, token = make_user(client, "understood")
    monkeypatch.setattr(plan_api.llm, "available", lambda: False)          # keyword reader
    monkeypatch.setattr(plan_api, "build_plan", lambda **kw: {
        "vibe": kw["vibe"], "budget": kw["budget"], "party_size": kw.get("party_size", 2),
        "estimated_cost": 30, "center": {"lat": kw["lat"], "lng": kw["lng"]}, "stops": [
            {"slot": "dinner", "label": "Dinner", "icon": "🍽️", "name": "X", "lat": kw["lat"], "lng": kw["lng"], "est_cost": 30}]})
    r = client.post("/api/v1/plan/chat", headers=auth_headers(token), json={
        "messages": [{"role": "user", "content": "$60 chill date, walking, within 3 km"}],
        "lat": 43.65, "lng": -79.38}).json()
    u = r["understood"]
    assert u["budget"] == 60 and u["vibe"] == "chill" and u["group_type"] == "date"
    assert u["transport"] == "walk" and u["radius_km"] == 3.0


def test_stated_budget_beats_the_fancier_bump(client, monkeypatch):
    """'$60 fancy date' means $60 — only a follow-up 'make it fancier' raises it."""
    _, token = make_user(client, "fancy")
    monkeypatch.setattr(plan_api.llm, "available", lambda: False)
    seen = {}
    def fake(**kw):
        seen.update(kw)
        return {"vibe": kw["vibe"], "budget": kw["budget"], "party_size": kw.get("party_size", 2),
                "estimated_cost": 30, "center": {"lat": kw["lat"], "lng": kw["lng"]}, "stops": [
                    {"slot": "dinner", "label": "Dinner", "icon": "🍽️", "name": "X", "lat": kw["lat"], "lng": kw["lng"], "est_cost": 30}]}
    monkeypatch.setattr(plan_api, "build_plan", fake)
    h = auth_headers(token)
    client.post("/api/v1/plan/chat", headers=h, json={"lat": 43.65, "lng": -79.38,
        "messages": [{"role": "user", "content": "$60 fancy date night"}]})
    assert seen["budget"] == 60 and seen["vibe"] == "extravagant"
    client.post("/api/v1/plan/chat", headers=h, json={"lat": 43.65, "lng": -79.38, "messages": [
        {"role": "user", "content": "$60 chill date night"}, {"role": "assistant", "content": "Here you go"},
        {"role": "user", "content": "make it fancier"}]})
    assert seen["budget"] >= 250 and seen["vibe"] == "extravagant"
