"""The stored venue list: which places can be which stop, finding candidates
(well known first, closed ones out, a search term narrows), placing names, and
the planner using it instead of a live search."""
from datetime import datetime

import pytest

from app.database import SessionLocal
from app.models import Area, Venue
from app.services import planner, venue_index
from app.services.plan_edit import EditContext, apply_edits
from app.services.venue_index import slots_for, wikidata_slots

# conftest switches these off for every test; the tests here use the real ones.
REAL_CANDIDATES, REAL_FIND_AREA = venue_index.candidates, venue_index.find_area
REAL_FIND_PLACE = venue_index.find_place
LAT, LNG = 10.0, 10.0          # far from any loaded city, so seeded rows never mix with real ones
CITY = "testville"
FRIDAY_6PM = datetime(2026, 10, 9, 18, 0, tzinfo=planner.LOCAL_TZ)


def _v(key, name, slots, dlat, **kw):
    row = {"id": f"test:{key}", "city": CITY, "name": name, "lat": LAT + dlat, "lng": LNG,
           "slots": slots, "category": slots[0], "categories": [slots[0]], "chain": False,
           "fast_food": False, "website": None, "address": None, "hours": None,
           "notability": 0.0, "wikidata": None, "confidence": 1.0}
    row.update(kw)
    return row


@pytest.fixture
def seeded():
    rows = [
        _v("museum", "Big Famous Museum", ["activity"], 0.01, notability=40.0, categories=["museum"]),
        _v("arcade", "Corner Arcade", ["activity"], 0.001, categories=["arcade"]),
        _v("park", "Quiet Park", ["scenic"], 0.001, categories=["park"]),
        _v("sushi", "Kaito Sushi", ["dinner", "lunch"], 0.002, hours="Mo-Su 17:00-23:00",
           categories=["sushi restaurant", "japanese restaurant", "restaurant"]),
        _v("noon", "Noon Diner", ["dinner", "lunch"], 0.001, hours="Mo-Su 08:00-15:00", categories=["diner"]),
        _v("burger", "Burger Chain", ["dinner", "lunch"], 0.0005, chain=True, fast_food=True,
           categories=["fast food restaurant"]),
        _v("bistro", "Le Bistro", ["dinner", "lunch"], 0.003, website="https://bistro.example",
           categories=["bistro"]),
        _v("hall", "Grand Hall", ["leisure", "drinks", "activity"], 0.002,
           categories=["music venue", "performing arts venue"]),
        _v("pub", "Corner Pub", ["leisure", "drinks"], 0.002, categories=["pub", "bar"]),
    ]
    with SessionLocal() as db:
        db.query(Venue).filter(Venue.city == CITY).delete()
        db.query(Area).filter(Area.city == CITY).delete()
        db.bulk_insert_mappings(Venue, rows)
        db.add(Area(id="test:hood", city=CITY, name="Zqx Market Village", kind="neighborhood",
                    lat=LAT + 0.02, lng=LNG))
        db.commit()
    venue_index._loaded_cache.clear()
    yield
    with SessionLocal() as db:
        db.query(Venue).filter(Venue.city == CITY).delete()
        db.query(Area).filter(Area.city == CITY).delete()
        db.commit()
    venue_index._loaded_cache.clear()


def test_overture_categories_to_stops():
    assert slots_for("sushi_restaurant", ["food_and_drink", "restaurant", "sushi_restaurant"]) == ["dinner", "lunch"]
    assert "dessert" in slots_for("ice_cream_shop", ["food_and_drink", "ice_cream_shop"])
    assert slots_for("museum", ["arts_and_entertainment", "museum"]) == ["activity"]
    # "historic site" in Overture is mostly plaques, office towers and whole districts
    assert slots_for("historic_site", ["attractions_and_activities", "historic_site"], notable=True) == []
    assert slots_for("tourist_attraction", [], notable=False) == []
    assert slots_for("tourist_attraction", [], notable=True) == ["activity"]


def test_wikidata_types_to_stops():
    assert wikidata_slots(["television tower", "architectural landmark", "observation tower"]) == ["activity"]
    assert wikidata_slots(["art museum"]) == ["activity"]
    assert wikidata_slots(["amusement park"]) == ["activity"]        # not "scenic" for ending in park
    assert wikidata_slots(["urban park"]) == ["scenic"]
    assert wikidata_slots(["zoo", "nonprofit organization", "municipal company"]) == ["activity"]
    for not_a_stop in (["skyscraper"], ["metro station", "underground station"], ["car park"],
                       ["federal electoral district of Canada"], ["adult movie theater", "movie theater"]):
        assert wikidata_slots(not_a_stop) == [], not_a_stop


def test_well_known_ranks_higher_and_closed_places_are_left_out(seeded):
    friday_2pm, friday_7pm = datetime(2026, 10, 9, 14, 0), datetime(2026, 10, 9, 19, 0)
    acts = {c["name"]: c["boost"] for c in REAL_CANDIDATES("activity", LAT, LNG, 5000, at=friday_2pm)}
    assert acts["Big Famous Museum"] > acts["Corner Arcade"]
    dinners = {c["name"]: c["boost"] for c in REAL_CANDIDATES("dinner", LAT, LNG, 5000, at=friday_7pm)}
    assert "Noon Diner" not in dinners                                # closes at 3pm
    assert {"Kaito Sushi", "Le Bistro", "Burger Chain"} <= set(dinners)
    assert dinners["Burger Chain"] < dinners["Le Bistro"] - 2         # fast-food chain isn't dinner
    assert dinners["Kaito Sushi"] > dinners["Le Bistro"] - 0.5        # known-open isn't penalised


def test_evening_rules(seeded):
    friday_9pm = datetime(2026, 10, 9, 21, 0)
    acts = {c["name"] for c in REAL_CANDIDATES("activity", LAT, LNG, 5000, at=friday_9pm)}
    assert "Big Famous Museum" not in acts           # no listed hours: museums keep daytime hours
    assert {"Corner Arcade", "Grand Hall"} <= acts
    night = {c["name"] for c in REAL_CANDIDATES("leisure", LAT, LNG, 5000, at=friday_9pm)}
    assert night == {"Corner Pub"}                   # a concert hall is for a show, not drinks


def test_a_search_term_narrows_and_the_radius_holds(seeded):
    assert [c["name"] for c in REAL_CANDIDATES("dinner", LAT, LNG, 5000, term="sushi")] == ["Kaito Sushi"]
    near = {c["name"] for c in REAL_CANDIDATES("activity", LAT, LNG, 500)}
    assert near == {"Corner Arcade", "Grand Hall"}                    # the museum is ~1.1 km away


def test_an_area_without_stored_venues_falls_back(seeded):
    assert REAL_CANDIDATES("dinner", -10.0, -10.0, 3000) is None


def test_placing_names(seeded, monkeypatch):
    monkeypatch.setattr(venue_index, "find_place", REAL_FIND_PLACE)
    assert REAL_FIND_PLACE("Zqx Market Village")[2] == "neighborhood"
    assert REAL_FIND_PLACE("big famous museum")[2] == "venue"
    assert REAL_FIND_AREA("Zqx Market Village") == pytest.approx((LAT + 0.02, LNG))
    assert REAL_FIND_AREA("big famous museum") == pytest.approx((LAT + 0.01, LNG))   # well known: anywhere
    # partial names and little-known places only count for someone already in the city
    assert REAL_FIND_AREA("Zqx Market", near=(45.5, -73.6)) is None
    assert REAL_FIND_AREA("Corner Arcade", near=(45.5, -73.6)) is None
    monkeypatch.setitem(venue_index.CITIES, CITY, (9.9, 9.9, 10.1, 10.1))
    assert REAL_FIND_AREA("Zqx Market", near=(LAT, LNG)) == pytest.approx((LAT + 0.02, LNG))
    assert REAL_FIND_AREA("Corner Arcade", near=(LAT, LNG)) == pytest.approx((LAT + 0.001, LNG))


def test_planner_uses_the_stored_list_with_times(seeded, monkeypatch):
    monkeypatch.setattr(venue_index, "candidates", REAL_CANDIDATES)
    monkeypatch.setattr(planner, "plan_start", lambda time_of_day=None, now=None: FRIDAY_6PM)
    monkeypatch.setattr(planner.weather_svc, "get_weather", lambda lat, lng: None)
    monkeypatch.setattr(planner.live_events, "available_for", lambda lat, lng: False)
    monkeypatch.setattr(planner.llm, "available", lambda: False)

    def no_live_search(*a, **kw):
        raise AssertionError("the live search was used for a covered area")
    monkeypatch.setattr(planner.yelp, "search", no_live_search)
    monkeypatch.setattr(planner, "fetch_places", no_live_search)

    plan = planner.build_plan(lat=LAT, lng=LNG, budget=300, vibe="chill", time_of_day="evening",
                              target_stops=3)
    stops = plan["stops"]
    assert [s["slot"] for s in stops] == ["scenic", "activity", "dinner"]
    assert stops[2]["name"] in ("Kaito Sushi", "Le Bistro")          # open at 8:45 PM, not a chain
    assert [s["time"] for s in stops] == ["6:00 PM", "7:00 PM", "8:45 PM"]
    assert plan["start"].startswith("2026-10-09T18:00")

    # removing a stop moves the later ones up
    edited, changed, _ = apply_edits(plan, [{"op": "remove", "stop": 1}], EditContext())
    assert changed and [s["time"] for s in edited["stops"]] == ["6:00 PM", "7:45 PM"]


def test_plan_start_rules():
    tz = planner.LOCAL_TZ
    day = lambda hh, mm=0, d=9: datetime(2026, 10, d, hh, mm, tzinfo=tz)
    assert planner.plan_start("evening", day(15, 10)) == day(18)
    assert planner.plan_start("evening", day(19, 40)) == day(20)      # already evening: next half hour
    assert planner.plan_start("evening", day(22, 10)) == day(18, d=10)  # evening's mostly over: tomorrow
    assert planner.plan_start("morning", day(8)) == day(10)
    assert planner.clock(day(9, 5)) == "9:05 AM"


def test_tonight_means_the_places_night():
    """A phone set to Malaysia time looking at Toronto gets Toronto's evening."""
    assert venue_index.tz_for(43.65, -79.38, "Asia/Kuala_Lumpur") == "America/Toronto"
    assert venue_index.tz_for(48.85, 2.35, "Europe/Paris") == "Europe/Paris"
    assert venue_index.tz_for(None, None, None) is None
