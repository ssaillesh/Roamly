"""The planner's fallbacks and guard rails: multi-day trips, the AI curator's answers
checked before they're used, walking plans that stay walkable, and the quick plan
whenever the AI fails."""
from app.services import curator, planner
from app.services.travel import haversine_km

A = (43.65, -79.38)


def _offline(monkeypatch, gather, ai=False):
    monkeypatch.setattr(planner.weather_svc, "get_weather", lambda lat, lng: None)
    monkeypatch.setattr(planner.live_events, "available_for", lambda lat, lng: False)
    monkeypatch.setattr(planner.llm, "available", lambda: ai)
    monkeypatch.setattr(planner, "_gather", gather)


def _spots(slot_key, n=8, step=0.001):
    return [{"id": f"ovt:{slot_key}{i}", "name": f"{slot_key} spot {i}", "lat": A[0] + i * step, "lng": A[1],
             "rating": 4.6 - i * 0.05, "review_count": 100, "categories": [slot_key],
             "hours": "Mo-Su 08:00-02:00"} for i in range(n)]


def _gather(anchor, slot_key, *a, **k):
    return _spots(slot_key)


def _plan(**kw):
    return planner.build_plan(lat=A[0], lng=A[1], budget=200, vibe="chill", time_of_day="evening",
                              target_stops=3, **kw)


# ---- trips ----

def test_a_trip_has_one_plan_per_day_and_never_repeats_a_place(monkeypatch):
    _offline(monkeypatch, _gather)
    trip = planner.build_trip(days=3, lat=A[0], lng=A[1], budget=600, vibe="chill",
                              time_of_day="evening", target_stops=3, seed=7)
    assert [p["day"] for p in trip] == [1, 2, 3]
    names = [s["name"] for p in trip for s in p["stops"]]
    assert len(names) == len(set(names))
    assert all(p["budget"] == 200 for p in trip)                     # the budget is split across days


def test_a_trip_is_at_most_five_days(monkeypatch):
    _offline(monkeypatch, lambda anchor, slot_key, *a, **k: _spots(slot_key, 30))
    assert len(planner.build_trip(days=9, lat=A[0], lng=A[1], budget=900, target_stops=2)) == 5


# ---- the AI curator's answer is checked, not trusted ----

def _choose(monkeypatch, answer, lists=None):
    monkeypatch.setattr(curator.llm, "chat_json", lambda *a, **k: answer)
    stops = [{"slot": "dinner", "name": "dinner spot 0"}, {"slot": "drinks", "name": "drinks spot 0"}]
    lists = lists or [_spots("dinner", 3), _spots("drinks", 3)]
    return curator.choose(stops, lists, context="", times=["6:00 PM", "8:00 PM"],
                          distance=lambda c: 0.5, labels={})


def test_no_answer_means_no_curation(monkeypatch):
    assert _choose(monkeypatch, None) is None
    assert _choose(monkeypatch, {"stops": "dinner"}) is None
    assert _choose(monkeypatch, {"stops": [{"id": "nope"}, {"id": "9z"}]}) is None   # nothing valid


def test_an_unknown_choice_keeps_the_rule_pick_and_its_words_are_dropped(monkeypatch):
    out = _choose(monkeypatch, {"stops": [{"id": "1b", "why": "Lovely."}, {"id": "7q", "why": "Made up."}]})
    (first, why1), (second, why2) = out["picks"]
    assert first["name"] == "dinner spot 1" and why1 == "Lovely."
    assert second is None and why2 is None


def test_words_naming_a_place_not_in_the_plan_are_dropped(monkeypatch):
    out = _choose(monkeypatch, {"stops": [{"id": "1a", "why": "Better than dinner spot 2, honestly."},
                                          {"id": "2a", "why": "A good pour."}],
                                "intro": "Skip drinks spot 1 tonight.", "tip": "Book ahead."})
    assert out["picks"][0][1] is None and out["picks"][1][1] == "A good pour."
    assert out["intro"] is None and out["tip"] == "Book ahead."


def test_the_same_place_twice_keeps_the_rule_pick(monkeypatch):
    same = [{"name": "Both Ways", "lat": A[0], "lng": A[1], "categories": ["bar"]}]
    out = _choose(monkeypatch, {"stops": [{"id": "1a"}, {"id": "2a"}]},
                  lists=[same + _spots("dinner", 1), [dict(same[0])] + _spots("drinks", 1)])
    assert out["picks"][0][0]["name"] == "Both Ways" and out["picks"][1][0] is None


def test_the_curators_picks_and_words_make_the_plan(monkeypatch):
    _offline(monkeypatch, _gather, ai=True)

    def choose(stops, lists, **kw):
        return {"picks": [(lists[i][1] if lists[i][1:] else None, f"Why {i}") for i in range(len(stops))],
                "intro": "A good night.", "tip": None}
    monkeypatch.setattr(planner.curator, "choose", choose)
    plan = _plan(transport="transit")
    assert not plan.get("quick") and plan["intro"] == "A good night."
    assert all(s.get("why", "").startswith("Why") for s in plan["stops"])


def test_when_the_curator_fails_the_plan_says_its_quick(monkeypatch):
    _offline(monkeypatch, _gather, ai=True)
    monkeypatch.setattr(planner.curator, "choose", lambda *a, **k: None)
    plan = _plan(transport="transit")
    assert plan["quick"] is True and plan["intro"].startswith("A chill plan")


# ---- walking plans ----

def test_a_walking_plan_chains_stops_close_together(monkeypatch):
    # The only view is 5 km east. Each later stop has a better-rated place back in the
    # centre and a plainer one near the view: on foot, the plan stays by the view.
    east = A[1] + 0.06

    def gather(anchor, slot_key, *a, **k):
        if slot_key == "scenic":
            return [{**_spots("scenic", 1)[0], "lng": east}]
        best, near_view = _spots(slot_key, 2)
        return [{**best, "rating": 4.8}, {**near_view, "lng": east + 0.002, "rating": 4.0}]
    _offline(monkeypatch, gather)
    plan = _plan(transport="walk")
    pts = [(s["lat"], s["lng"]) for s in plan["stops"]]
    assert plan["stops"][0]["slot"] == "scenic" and len(pts) == 3
    assert all(haversine_km(*a, *b) <= planner.TRANSPORT["walk"]["cluster"] / 1000 for a, b in zip(pts, pts[1:]))


def test_a_walking_plan_with_nothing_close_still_fills_the_stop(monkeypatch):
    def gather(anchor, slot_key, price, radius, *a, **k):
        if radius == planner.TRANSPORT["walk"]["cluster"]:
            return []                                       # the close-by search finds nothing
        far = 0.05 if slot_key != "scenic" else 0.0         # everything but the first stop is 5 km off
        return [{**c, "lat": c["lat"] + far} for c in _spots(slot_key, 2)]
    _offline(monkeypatch, gather)
    plan = _plan(transport="walk")
    assert len(plan["stops"]) == 3                          # a far stop beats a missing one


def test_a_walking_plan_is_described_by_the_ai_or_is_a_quick_plan(monkeypatch):
    _offline(monkeypatch, _gather, ai=True)
    monkeypatch.setattr(planner.llm, "chat_json", lambda *a, **k: None)       # the narrator times out
    plan = _plan(transport="walk")
    assert plan["quick"] is True and all(not s.get("why") for s in plan["stops"])

    monkeypatch.setattr(planner.llm, "chat_json", lambda *a, **k: {
        "intro": "Walk it.", "tip": "Wear good shoes.", "stops": [{"desc": f"Stop {i}"} for i in range(3)]})
    plan = _plan(transport="walk")
    assert not plan.get("quick") and plan["intro"] == "Walk it." and plan["stops"][0]["why"] == "Stop 0"
