"""A plan must always come back inside its time budget — the page gives /plan/*
requests 60s, and the free venue fallback (public Overpass) can hang for minutes."""
import time

from app.services import planner


def _offline(monkeypatch, gather):
    monkeypatch.setattr(planner.weather_svc, "get_weather", lambda lat, lng: None)
    monkeypatch.setattr(planner.events_svc, "available", lambda: False)
    monkeypatch.setattr(planner.llm, "available", lambda: False)
    monkeypatch.setattr(planner, "_gather", gather)


def _spots(slot_key, n=3):
    return [{"name": f"{slot_key} spot {i}", "lat": 43.65 + i * 0.001, "lng": -79.38,
             "rating": 4.5, "review_count": 100, "categories": [slot_key]} for i in range(n)]


def test_hung_searches_are_skipped_not_waited_on(monkeypatch):
    def gather(anchor, slot_key, *a, **k):
        if slot_key != "dinner":
            time.sleep(3)                     # a hung upstream search
        return _spots(slot_key)
    _offline(monkeypatch, gather)

    t0 = time.monotonic()
    plan = planner.build_plan(lat=43.65, lng=-79.38, budget=500, vibe="romantic",
                              time_of_day="evening", target_stops=4, time_budget=1.0)
    assert time.monotonic() - t0 < 2.0       # budget respected, not 3s × stops
    assert [s["slot"] for s in plan["stops"]] == ["dinner"]
    assert plan["skipped"] and "Dinner" not in plan["skipped"]


def test_stops_are_searched_in_parallel(monkeypatch):
    def gather(anchor, slot_key, *a, **k):
        time.sleep(0.6)
        return _spots(slot_key)
    _offline(monkeypatch, gather)

    t0 = time.monotonic()
    plan = planner.build_plan(lat=43.65, lng=-79.38, budget=500, vibe="romantic",
                              time_of_day="evening", target_stops=4, time_budget=10.0)
    assert len(plan["stops"]) >= 3 and plan["skipped"] == []
    assert time.monotonic() - t0 < 1.5       # ~one search's latency, not one per stop
