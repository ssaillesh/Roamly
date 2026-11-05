"""The Nightlife tab: club nights from Ticketmaster and Roamly's own approved
listings, the club index from the stored venue list, special days, the public
"list your event" form and the admin review queue. Plus plans on a special day."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models import AdminAuditLog, Area, NightlifeEvent, Venue
from app.services import nightlife, planner, special_days, venue_index
from tests.conftest import auth_headers, make_user
from tests.test_admin import make_admin

LAT, LNG = 10.0, 10.0          # far from any loaded city (as in test_venue_index.py)
CITY = "nightville"
ST_PATRICKS_EVE = datetime(2027, 3, 16, 0, 0, tzinfo=timezone.utc)      # Mar 15, 8 PM in Toronto
REAL_NIGHTLIFE, REAL_FIND_VENUE = venue_index.nightlife, venue_index.find_venue


def _v(key, name, cats, dlat=0.001, slots=("leisure", "drinks")):
    return {"id": f"nl:{key}", "city": CITY, "name": name, "lat": LAT + dlat, "lng": LNG, "slots": list(slots),
            "category": cats[0], "categories": cats, "chain": False, "fast_food": False, "website": None,
            "address": None, "hours": None, "notability": 0.0, "wikidata": None, "confidence": 1.0}


@pytest.fixture
def town():
    rows = [
        {**_v("club", "Neon Club", ["dance club", "bar"]), "image": "https://neonclub.example/og.jpg"},
        _v("pub", "Kelly's Irish Pub", ["irish pub", "pub", "bar"], 0.002),
        _v("gay", "Rainbow Room", ["gay bar", "bar"], 0.0025),
        _v("hall", "Grand Concert Hall", ["music venue", "performing arts venue"], 0.003),
        _v("jazz", "Blue Note Lounge", ["jazz and blues venue", "music venue", "performing arts venue"], 0.0015),
        _v("diner", "Night Owl Diner", ["diner"], 0.001, slots=("dinner",)),
    ]
    with SessionLocal() as db:
        db.query(Venue).filter(Venue.city == CITY).delete()
        db.query(Area).filter(Area.city == CITY).delete()
        db.bulk_insert_mappings(Venue, rows)
        db.add(Area(id="nl:area", city=CITY, name="Nightville Village", kind="neighborhood", lat=LAT, lng=LNG))
        db.commit()
    venue_index._loaded_cache.clear()
    venue_index._areas_cache = None
    yield
    with SessionLocal() as db:
        db.query(Venue).filter(Venue.city == CITY).delete()
        db.query(Area).filter(Area.city == CITY).delete()
        db.query(NightlifeEvent).filter(NightlifeEvent.venue_name.like("Test %")).delete()
        db.commit()
    venue_index._loaded_cache.clear()
    venue_index._areas_cache = None


def _tm(event_name, venue, genre=None, dlat=0.002):
    return {"name": event_name, "venue": venue, "lat": LAT + dlat, "lng": LNG, "date": "2027-03-15",
            "time": "22:00:00", "start": "2027-03-16T02:00:00Z", "genre": genre, "category": "Music",
            "source": "ticketmaster"}


@pytest.fixture
def ticketmaster(monkeypatch):
    def search(lat, lng, *, keyword=None, classification=None, **kw):
        if keyword:
            return [_tm("St. Patrick's Bash", "Somewhere Else", None, 0.004)]
        return [_tm("DJ Nova All Night", "Warehouse 9", "Dance/Electronic", 0.005),
                _tm("The Fiddlers", "Kelly's Irish Pub", "Folk"),              # at a known night spot
                _tm("Symphony No. 5", "Grand Concert Hall", "Classical", 0.03)]   # not nightlife
    monkeypatch.setattr(nightlife.ticketmaster, "search_events", search)
    monkeypatch.setattr(nightlife.ticketmaster, "available", lambda: True)
    monkeypatch.setattr(venue_index, "nightlife", REAL_NIGHTLIFE)


def _own(title, status, when, **kw):
    with SessionLocal() as db:
        db.add(NightlifeEvent(title=title, venue_name="Test Venue", starts_at=when, status=status,
                              source="admin", lat=LAT, lng=LNG, **kw))
        db.commit()


def test_feed_merges_club_nights_and_the_club_index(town, ticketmaster):
    _own("Approved Roamly Night", "approved", ST_PATRICKS_EVE + timedelta(hours=1), kind="special_guest")
    _own("Not Reviewed Yet", "pending", ST_PATRICKS_EVE + timedelta(hours=1))
    f = nightlife.feed(LAT, LNG, window="tonight", radius_km=10, now=ST_PATRICKS_EVE)

    names = [e["name"] for e in f["events"]]
    assert {"DJ Nova All Night", "The Fiddlers", "St. Patrick's Bash", "Approved Roamly Night"} <= set(names)
    assert "Symphony No. 5" not in names and "Not Reviewed Yet" not in names
    bash = next(e for e in f["events"] if e["name"] == "St. Patrick's Bash")
    assert bash["special_day"] == "st_patricks"

    assert f["special_day"]["key"] == "st_patricks" and f["special_day"]["days_until"] == 2
    spots = {s["name"]: s for s in f["spots"]}
    assert "Grand Concert Hall" not in spots and "Night Owl Diner" not in spots
    assert spots["Neon Club"]["kind"] == "clubs" and spots["Rainbow Room"]["kind"] == "lgbtq"
    assert spots["Blue Note Lounge"]["kind"] == "music"            # a jazz bar is nightlife, not a concert hall
    assert spots["Kelly's Irish Pub"]["events"][0]["name"] == "The Fiddlers"
    assert f["spots"][0]["name"] == "Kelly's Irish Pub"            # a place with an event tonight first
    assert spots["Neon Club"]["area"] == "Nightville Village"
    assert [s["name"] for s in f["themed_spots"]] == ["Kelly's Irish Pub"]
    assert {k["key"] for k in f["kinds"]} >= {"clubs", "bars", "lgbtq", "music"}


def test_nightlife_endpoint(client, town, ticketmaster):
    _, token = make_user(client, "night")
    assert client.get("/api/v1/nightlife?lat=10&lng=10").status_code == 401
    assert client.get("/api/v1/nightlife", headers=auth_headers(token)).status_code == 400   # no location
    r = client.get("/api/v1/nightlife?lat=10&lng=10&window=weekend", headers=auth_headers(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["covered"] and body["window"] == "weekend" and body["spots"]
    photos = {sp["name"]: sp["image"] for sp in body["spots"]}
    assert photos["Neon Club"] == "https://neonclub.example/og.jpg"     # the loader's photo reaches the page
    assert photos["Kelly's Irish Pub"] is None                          # none stored → the page's icon
    assert client.get("/api/v1/nightlife?lat=10&lng=10&window=forever",
                      headers=auth_headers(token)).status_code == 422


def _submission(**over):
    body = {"title": "Test Afro House Night", "venue_name": "Test Club", "kind": "club_night",
            "starts_at": (datetime.now(timezone.utc) + timedelta(days=3)).isoformat(),
            "submitter_email": "promoter@example.com", "price_min": 15}
    body.update(over)
    return body


def _ip():
    return {"X-Forwarded-For": f"203.0.113.{uuid.uuid4().int % 250}-{uuid.uuid4().hex[:6]}"}


@pytest.fixture
def behind_proxy(monkeypatch):
    """Like production behind one proxy: X-Forwarded-For's last entry is the address
    the proxy saw, so each test gets its own address (and its own daily count)."""
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1)


def test_venue_submissions_wait_for_review(client, behind_proxy, monkeypatch):
    linked = {"id": "nl:club", "name": "Test Club", "lat": 43.65, "lng": -79.4, "address": "1 Club St"}
    monkeypatch.setattr(venue_index, "find_venue", lambda *a, **kw: linked)
    r = client.post("/api/v1/nightlife/submissions", json=_submission(), headers=_ip())
    assert r.status_code == 202, r.text
    with SessionLocal() as db:
        row = db.scalars(select(NightlifeEvent).where(NightlifeEvent.title == "Test Afro House Night")).one()
        assert (row.status, row.source, row.venue_id, row.lat) == ("pending", "submission", "nl:club", 43.65)
        db.delete(row)
        db.commit()


def test_submission_checks(client, behind_proxy):
    h = _ip()
    past = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    assert client.post("/api/v1/nightlife/submissions", json=_submission(starts_at=past), headers=h).status_code == 422
    assert client.post("/api/v1/nightlife/submissions",
                       json=_submission(submitter_email="not-an-email"), headers=h).status_code == 422
    assert client.post("/api/v1/nightlife/submissions",
                       json=_submission(url="javascript:alert(1)"), headers=h).status_code == 422
    # the honeypot: accepted, never stored
    r = client.post("/api/v1/nightlife/submissions", json=_submission(title="Test Bot Spam", website="x"), headers=h)
    assert r.status_code == 202
    with SessionLocal() as db:
        assert not db.scalars(select(NightlifeEvent).where(NightlifeEvent.title == "Test Bot Spam")).first()


def test_submissions_are_rate_limited(client, behind_proxy):
    h = _ip()
    codes = [client.post("/api/v1/nightlife/submissions", json=_submission(title=f"Test Limit {i}"),
                         headers=h).status_code for i in range(7)]
    assert codes[:5] == [202] * 5 and codes[-1] == 429
    with SessionLocal() as db:
        db.query(NightlifeEvent).filter(NightlifeEvent.title.like("Test Limit %")).delete(synchronize_session=False)
        db.commit()


def test_admin_reviews_and_manages_events(client):
    _, plain = make_user(client, "notadmin")
    assert client.get("/api/v1/admin/events", headers=auth_headers(plain)).status_code == 403
    _, token = make_admin(client)
    h = auth_headers(token)
    start = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0)
    r = client.post("/api/v1/admin/events", headers=h, json={
        "title": "Test Special Guest Night", "venue_name": "Test Venue Hall", "starts_at": start.isoformat(),
        "kind": "special_guest", "lat": 43.65, "lng": -79.38})
    assert r.status_code == 201, r.text
    made = r.json()
    assert made["status"] == "approved" and made["source"] == "admin"
    upcoming = client.get("/api/v1/admin/events?status=upcoming", headers=h).json()["events"]
    assert any(e["id"] == made["id"] for e in upcoming)

    r = client.patch(f"/api/v1/admin/events/{made['id']}", headers=h, json={"title": "Test Renamed Night"})
    assert r.json()["title"] == "Test Renamed Night"
    assert client.post(f"/api/v1/admin/events/{made['id']}/status", headers=h,
                       json={"status": "rejected"}).json()["status"] == "rejected"
    assert client.delete(f"/api/v1/admin/events/{made['id']}", headers=h).status_code == 204
    with SessionLocal() as db:
        actions = set(db.scalars(select(AdminAuditLog.action).where(AdminAuditLog.target == str(made["id"]))))
    assert {"event_create", "event_edit", "event_rejected", "event_delete"} <= actions


def test_admin_venue_search(client, town):
    _, token = make_admin(client)
    found = client.get("/api/v1/admin/venues/search?q=Kelly", headers=auth_headers(token)).json()
    assert any(v["id"] == "nl:pub" for v in found)


# ---- plans on a special day ------------------------------------------------------

LEISURE = [{"name": "Generic Bar", "categories": ["bar"]}, {"name": "Kelly's Irish Pub", "categories": ["irish pub", "pub"]}]


@pytest.fixture
def st_patricks(monkeypatch):
    day = special_days.active(datetime(2027, 3, 17).date())
    monkeypatch.setattr(special_days, "active", lambda today=None: day)
    monkeypatch.setattr(venue_index, "in_city", lambda lat, lng: True)
    monkeypatch.setattr(planner.weather_svc, "get_weather", lambda lat, lng: None)
    monkeypatch.setattr(planner.live_events, "available_for", lambda lat, lng: False)
    monkeypatch.setattr(planner.events_svc, "available", lambda: False)
    monkeypatch.setattr(planner.llm, "available", lambda: False)

    def gather(anchor, slot_key, price, radius, dietary, term=None, cats=None, **kw):
        cands = LEISURE if slot_key == "leisure" else [{"name": f"{slot_key} place", "categories": [slot_key]}]
        cands = [dict(c, lat=43.65, lng=-79.38, rating=4.5, review_count=100) for c in cands]
        hits = [c for c in cands if term and term.split()[0] in (c["name"] + " ".join(c["categories"])).lower()]
        return hits or cands
    monkeypatch.setattr(planner, "_gather", gather)


def _night(**kw):
    return planner.build_plan(lat=43.65, lng=-79.38, budget=400, party_size=2, vibe="night_out",
                              time_of_day="night", target_stops=4, **kw)


def test_a_special_day_plan_leans_into_it(st_patricks):
    plan = _night()
    bar = next(s for s in plan["stops"] if s["slot"] == "leisure")
    assert bar["name"] == "Kelly's Irish Pub"
    assert "St. Patrick's Day" in plan["special_note"]


def test_the_theme_steps_aside(st_patricks):
    assert "special_note" not in _night(avoid="bars")
    assert "special_note" not in _night(no_alcohol=True)
    assert "special_note" not in _night(interests="rooftop")        # they asked for something else


def test_plan_a_night_here_keeps_the_place(client, monkeypatch):
    """The club guide's button sends the place as a requested venue in the settings."""
    from tests.planner_eval.harness import offline
    offline(monkeypatch)
    place = {"name": "Kelly's Irish Pub", "lat": 43.65, "lng": -79.38, "categories": ["irish pub", "pub"],
             "rating": None, "review_count": None, "price": None, "source": "overture"}
    monkeypatch.setattr(venue_index, "find_venue", lambda *a, **kw: dict(place))
    _, token = make_user(client, "nighthere")
    r = client.post("/api/v1/plan/chat", headers=auth_headers(token), json={
        "messages": [], "lat": 43.65, "lng": -79.38,
        "prefs": {"vibe": "night_out", "budget": 200, "party_size": 2, "requested_venues": ["Kelly's Irish Pub"]}})
    assert r.status_code == 200, r.text
    assert "Kelly's Irish Pub" in [s["name"] for s in r.json()["plan"]["stops"]]
