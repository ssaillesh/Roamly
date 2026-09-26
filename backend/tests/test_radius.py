"""Search radius: chips (radius_km), typed requests ("within 5 km"), the 1-20 km
clamp, the profile's starting value, and that it reaches the planner."""
import app.api.plan as plan_api
from app.services import planner, taste_profile
from tests.conftest import make_user, auth_headers


def test_typed_radius():
    assert plan_api._extract_radius("somewhere within 5 km") == 5000
    assert plan_api._extract_radius("under 3 miles please") == 4827
    assert plan_api._extract_radius("0.2km") == 1000          # clamped to the 1 km floor
    assert plan_api._extract_radius("50 km") == 20000         # clamped to the 20 km ceiling
    assert plan_api._extract_radius("a chill date") is None


def test_chip_radius_is_sanitised():
    assert plan_api._structured({"radius_km": 7.5}) == {"radius": 7500}
    assert plan_api._structured({"radius_km": "far"}) == {}


def test_profile_gives_a_starting_radius():
    assert taste_profile.defaults_for({"transport": "walk", "distance": "near"})["radius_km"] == 1.2
    assert taste_profile.defaults_for({"transport": "car", "distance": "far"})["radius_km"] == 20.0
    assert "radius_km" not in taste_profile.defaults_for({})


def test_radius_reaches_the_planner(client, monkeypatch):
    _, token = make_user(client, "radius")
    seen = {}

    def fake_build_plan(**kw):
        seen.update(kw)
        return {"vibe": kw["vibe"], "budget": kw["budget"], "party_size": kw.get("party_size", 2),
                "estimated_cost": 30, "center": {"lat": kw["lat"], "lng": kw["lng"]}, "stops": [
                    {"slot": "dinner", "label": "Dinner", "icon": "🍽️", "name": "X",
                     "lat": kw["lat"], "lng": kw["lng"], "est_cost": 30}]}
    monkeypatch.setattr(plan_api, "build_plan", fake_build_plan)

    client.post("/api/v1/plan/chat", headers=auth_headers(token), json={
        "messages": [], "lat": 43.65, "lng": -79.38,
        "prefs": {"budget": 80, "vibe": "chill", "radius_km": 12}})
    assert seen["radius"] == 12000


def test_explicit_radius_beats_transport_default(monkeypatch):
    radii = []
    monkeypatch.setattr(planner, "_gather", lambda anchor, slot, price, radius, *a, **k: radii.append(radius) or [])
    planner.gather_options("dinner", lat=43.65, lng=-79.38, transport="walk", radius=9000)
    planner.gather_options("dinner", lat=43.65, lng=-79.38, transport="walk")
    assert radii == [9000, planner.TRANSPORT["walk"]["radius"]]
