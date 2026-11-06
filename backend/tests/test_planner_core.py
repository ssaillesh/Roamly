"""The planner's core rules: the shape of the night, weather, repeatable picks, what each
stop carries, and the plan it makes when the AI isn't there."""
import random
from datetime import datetime

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


# ---- plans built from the user's picks ----

def test_a_plan_from_picks_has_times_and_its_event_keeps_its_own(monkeypatch):
    monkeypatch.setattr(planner.llm, "available", lambda: False)
    monkeypatch.setattr(planner, "plan_start", lambda tod=None, now=None: datetime(2026, 11, 6, 18, 0, tzinfo=planner.LOCAL_TZ))
    picks = [{"slot": "drinks", "name": "Civil Liberties", "lat": A[0], "lng": A[1], "id": "ovt:civ"},
             {"slot": "event", "name": "Jazz at The Rex", "lat": A[0], "lng": A[1], "time": "9:30 PM", "est_cost": 15},
             {"slot": "dinner", "name": "Bar Raval", "lat": A[0], "lng": A[1]}]
    plan = planner.build_from_selection(picks, lat=A[0], lng=A[1], party_size=2, budget=150, time_of_day="evening")
    # dinner, then drinks (they fit before the show), then the show at its own time
    assert [(s["name"], s["time"]) for s in plan["stops"]] == [
        ("Bar Raval", "6:00 PM"), ("Civil Liberties", "7:35 PM"), ("Jazz at The Rex", "9:30 PM")]
    assert plan["start"].startswith("2026-11-06T18:00") and plan["stops"][1]["vid"] == "ovt:civ"
    edited, changed, _ = apply_edits(plan, [{"op": "set_start", "time": "5 PM"}], EditContext())
    assert edited["stops"][0]["time"] == "5:00 PM" and edited["stops"][2]["time"] == "9:30 PM"   # the show doesn't move


# ---- real times, real hours ----

def test_a_place_closed_at_the_real_time_is_swapped_for_one_open_then(monkeypatch):
    six = datetime(2026, 11, 6, 18, 0, tzinfo=planner.LOCAL_TZ)
    monkeypatch.setattr(planner, "plan_start", lambda tod=None, now=None: six)

    def gather(anchor, slot_key, *a, **k):
        if slot_key == "dinner":   # the better one shuts at 7:30, before an after-activity dinner
            return [{"id": "ovt:early", "name": "Early Bird", "lat": A[0], "lng": A[1], "rating": 4.9,
                     "review_count": 900, "categories": ["restaurant"], "hours": "Mo-Su 17:00-19:30"},
                    {"id": "ovt:late", "name": "Night Kitchen", "lat": A[0], "lng": A[1], "rating": 4.0,
                     "review_count": 50, "categories": ["restaurant"], "hours": "Mo-Su 17:00-23:00"}]
        return _spots(slot_key)
    _offline(monkeypatch, gather)
    plan = planner.build_plan(lat=A[0], lng=A[1], budget=300, vibe="chill", transport="transit",
                              time_of_day="night", target_stops=2)   # night arc: dinner first…
    assert plan["stops"][0]["name"] == "Early Bird"                    # …at 6, while it's open
    plan = planner.build_plan(lat=A[0], lng=A[1], budget=300, vibe="chill", transport="transit",
                              time_of_day="evening", target_stops=3)  # scenic, activity, then dinner
    dinner = next(s for s in plan["stops"] if s["slot"] == "dinner")
    assert dinner["name"] == "Night Kitchen"                           # Early Bird would be closed by then


def test_each_leg_has_a_time_and_a_way_of_getting_there(monkeypatch):
    _offline(monkeypatch, _gather)
    plan = planner.build_plan(lat=A[0], lng=A[1], budget=200, vibe="chill", transport="transit",
                              time_of_day="evening", target_stops=3)
    assert len(plan["hops"]) == len(plan["stops"]) - 1
    assert all(h["minutes"] >= 1 and h["mode"] in ("walk", "transit", "car") for h in plan["hops"])
    assert Plan(**plan).hops[0].minutes == plan["hops"][0]["minutes"]
