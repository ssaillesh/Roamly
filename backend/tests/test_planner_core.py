"""The planner's core rules: the shape of the night, weather, repeatable picks, what each
stop carries, and the plan it makes when the AI isn't there."""
import random

from app.schemas.plan import Plan
from app.services import planner
from app.services.plan_edit import EditContext, apply_edits

A = (43.65, -79.38)


def _offline(monkeypatch, gather, weather=None):
    monkeypatch.setattr(planner.weather_svc, "get_weather", lambda lat, lng: weather)
    monkeypatch.setattr(planner.live_events, "available_for", lambda lat, lng: False)
    monkeypatch.setattr(planner.llm, "available", lambda: False)
    monkeypatch.setattr(planner, "_gather", gather)


def _spots(slot_key, n=3):
    return [{"id": f"ovt:{slot_key}{i}", "name": f"{slot_key} spot {i}", "lat": A[0] + i * 0.001, "lng": A[1],
             "rating": 4.5 - i * 0.1, "review_count": 100, "categories": [slot_key],
             "hours": "Mo-Su 08:00-02:00"} for i in range(n)]


def _gather(anchor, slot_key, *a, **k):
    return _spots(slot_key)


# ---- the shape of the night ----

def test_arcs_by_time_of_day():
    assert planner._slots_for("chill", "evening", None, 4) == ["scenic", "activity", "dinner", "leisure"]
    assert planner._slots_for("chill", "night", None, 3) == ["dinner", "activity", "leisure"]
    assert planner._slots_for("night_out", None, None, 4)[0] == "dinner"      # a night out starts at night
    assert len(planner._slots_for("chill", "morning", None, 1)) == 2            # never fewer than two stops


def test_family_plans_have_no_bars():
    slots = planner._slots_for("chill", "night", "family", 4)
    assert "leisure" not in slots and "drinks" not in slots and "dessert" in slots


def test_rain_moves_the_outdoor_stop_inside(monkeypatch):
    _offline(monkeypatch, _gather, weather={"summary": "Rain, 8°C", "rainy": True, "cold": False})
    plan = planner.build_plan(lat=A[0], lng=A[1], budget=200, vibe="chill", time_of_day="evening", target_stops=4)
    slots = [s["slot"] for s in plan["stops"]]
    assert "scenic" not in slots
    assert slots.count("cafe") <= 1


# ---- picking ----

def test_the_same_inputs_give_the_same_pick():
    cands = _spots("dinner", 6)
    first = planner._pick(cands, set(), A, 1.0)["name"]
    assert all(planner._pick(cands, set(), A, 1.0)["name"] == first for _ in range(5))


def test_try_another_draws_from_the_strong_top_reproducibly():
    cands = _spots("dinner", 10)
    a = planner._pick(cands, set(), A, 1.0, vary=True, rng=random.Random(42))["name"]
    b = planner._pick(cands, set(), A, 1.0, vary=True, rng=random.Random(42))["name"]
    assert a == b                                                       # same seed, same roll
    ranked = sorted(cands, key=lambda c: planner._score(c, A, 1.0), reverse=True)
    assert a in {c["name"] for c in ranked[:6]}                          # never the weak tail


def test_names_already_used_are_never_picked():
    cands = _spots("dinner", 2)
    assert planner._pick(cands, {"dinner spot 0"}, A, 1.0)["name"] == "dinner spot 1"
    assert planner._pick(cands, {"dinner spot 0", "dinner spot 1"}, A, 1.0) is None


# ---- what a plan carries ----

def test_stops_carry_their_venue_id_and_hours_through_the_schema(monkeypatch):
    _offline(monkeypatch, _gather)
    plan = planner.build_plan(lat=A[0], lng=A[1], budget=200, vibe="chill", transport="transit",
                              time_of_day="evening", target_stops=3, interests="jazz")
    assert all(s["vid"] and s["vid"].startswith("ovt:") for s in plan["stops"])
    assert all(s["hours"] for s in plan["stops"])
    shown = Plan(**plan).model_dump()                                   # what the page gets back
    assert shown["stops"][0]["vid"] == plan["stops"][0]["vid"]
    assert shown["stops"][0]["arousal"] is not None
    assert shown["transport"] == "transit" and shown["interests"] == "jazz"


def test_without_the_ai_the_plan_is_a_quick_version(monkeypatch):
    _offline(monkeypatch, _gather)
    plan = planner.build_plan(lat=A[0], lng=A[1], budget=200, vibe="romantic", time_of_day="evening", target_stops=3)
    assert plan["quick"] is True
    assert plan["intro"].startswith("A romantic plan") and all(not s.get("why") for s in plan["stops"])


def test_old_saved_plans_without_the_new_fields_still_load():
    old = {"vibe": "chill", "budget": 100, "party_size": 2, "estimated_cost": 60,
           "center": {"lat": A[0], "lng": A[1]},
           "stops": [{"slot": "dinner", "label": "Dinner", "icon": "x", "name": "Old Spot",
                      "lat": A[0], "lng": A[1], "est_cost": 30}]}
    p = Plan(**old)
    assert p.stops[0].vid is None and p.transport is None


# ---- edits keep what the user read ----

def test_an_edit_keeps_the_intro_unless_it_names_a_place_thats_gone():
    plan = {"vibe": "chill", "budget": 200, "party_size": 2, "estimated_cost": 0,
            "center": {"lat": A[0], "lng": A[1]}, "start": "2026-11-06T18:00",
            "intro": "Start slow, then dinner and a late drink.",
            "stops": [{"slot": s, "label": s, "icon": "x", "name": f"{s} place", "lat": A[0], "lng": A[1],
                       "est_cost": 20} for s in ("scenic", "dinner", "leisure")]}
    kept, changed, _ = apply_edits(plan, [{"op": "remove", "slot": "scenic"}], EditContext())
    assert changed and kept["intro"] == "Start slow, then dinner and a late drink."
    plan["intro"] = "Begin at dinner place, then on to leisure place."
    redone, _, _ = apply_edits(plan, [{"op": "remove", "slot": "dinner"}], EditContext())
    assert "dinner place" not in redone["intro"]                         # it named the removed stop
