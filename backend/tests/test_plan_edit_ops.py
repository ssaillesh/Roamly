"""Editing the plan on screen directly (POST /plan/edit): every change comes back with
its times, cost and distance worked out again, so the page never guesses them."""
import pytest

from app.services import plan_edit
from app.services.plan_edit import EditContext, apply_edits
from app.services.planner import route_km
from tests.conftest import auth_headers, make_user

A = (43.65, -79.38)


def _stop(slot, name, cost, dlat=0.0, **kw):
    return {"slot": slot, "label": slot.title(), "icon": "x", "name": name, "lat": A[0] + dlat, "lng": A[1],
            "est_cost": cost, **kw}


def _plan():
    return {"vibe": "chill", "budget": 200, "party_size": 2, "estimated_cost": 0,
            "center": {"lat": A[0], "lng": A[1]}, "start": "2026-11-06T18:00",
            "intro": "An easy evening.",
            "stops": [_stop("activity", "Bowl-a-rama", 20), _stop("dinner", "Bar Raval", 40, 0.01),
                      _stop("leisure", "The Rex", 15, 0.02), _stop("dessert", "Sweet Olenka's", 8, 0.03)]}


@pytest.fixture(autouse=True)
def _fake_search(monkeypatch):
    def gather(anchor, slot_key, *a, **k):
        return [{"id": f"ovt:new-{slot_key}", "name": f"New {slot_key} place", "lat": anchor[0], "lng": anchor[1],
                 "rating": 4.4, "review_count": 50, "categories": [slot_key]}]
    monkeypatch.setattr(plan_edit, "_gather", gather)


def _edit(ops, plan=None, ctx=None):
    return apply_edits(plan or _plan(), ops, ctx or EditContext(party_size=2))


def test_removing_a_stop_reworks_the_times_cost_and_distance():
    before, _, _ = _edit([{"op": "set_start", "time": "6 PM"}])
    after, changed, _ = _edit([{"op": "remove", "stop": 2}])
    assert changed == ["Removed Bar Raval."]
    assert [s["name"] for s in after["stops"]] == ["Bowl-a-rama", "The Rex", "Sweet Olenka's"]
    assert after["stops"][1]["time"] == before["stops"][1]["time"]     # The Rex moves up into dinner's place
    assert after["estimated_cost"] == (20 + 15 + 8) * 2
    assert after["walk_km"] == route_km(after["stops"])                   # distance re-measured, not kept


def test_a_new_start_time_moves_every_stop():
    plan, changed, _ = _edit([{"op": "set_start", "time": "8 PM"}])
    assert changed == ["It now starts at 8:00 PM."]
    assert "T20:00" in plan["start"] and plan["stops"][0]["time"] == "8:00 PM"
    assert plan["intro"] == "An easy evening."                          # nothing gone, so the intro stays


def test_a_bad_start_time_changes_nothing():
    plan, changed, problems = _edit([{"op": "set_start", "time": "after work"}])
    assert not changed and problems and plan["start"] == "2026-11-06T18:00"


def test_more_people_means_a_bigger_total():
    plan, changed, _ = _edit([{"op": "set_party", "party_size": 6}])
    assert changed == ["It's now for 6."] and plan["party_size"] == 6
    assert plan["estimated_cost"] == (20 + 40 + 15 + 8) * 6


def test_shorter_drops_the_least_needed_stops_and_keeps_dinner():
    plan, _, _ = _edit([{"op": "set_length", "stops": 2}])
    assert [s["slot"] for s in plan["stops"]] == ["activity", "dinner"]      # dessert, then drinks, went first


def test_a_requested_place_is_never_dropped():
    p = _plan()
    p["stops"][3]["requested"] = True
    plan, _, _ = _edit([{"op": "set_length", "stops": 3}], plan=p)
    assert "Sweet Olenka's" in [s["name"] for s in plan["stops"]]


def test_longer_adds_the_next_kind_of_stop():
    short = _plan()
    short["stops"] = short["stops"][:2]                                     # activity, dinner
    plan, changed, _ = _edit([{"op": "set_length", "stops": 3}], plan=short)
    assert len(plan["stops"]) == 3 and plan["stops"][-1]["slot"] == "dessert"
    assert changed == ["Added New dessert place."]


def test_an_event_goes_in_at_its_own_time_with_its_real_price():
    ev = {"name": "Jazz Jam", "lat": A[0], "lng": A[1], "time": "8:45 PM", "price_min": 25,
          "venue": "Poetry Jazz Café", "url": "javascript:alert(1)", "image": "http://x/insecure.jpg"}
    plan, changed, _ = _edit([{"op": "set_start", "time": "6 PM"}, {"op": "add_event", "event": ev}])
    names = [s["name"] for s in plan["stops"]]
    jam = plan["stops"][names.index("Jazz Jam")]
    assert changed[-1] == "Added Jazz Jam."
    assert jam["est_cost"] == 25 and jam["slot"] == "event"
    assert jam["url"] is None and jam["image"] is None                    # only safe links come through
    assert names.index("Jazz Jam") < len(names) - 1                         # not just tacked on the end
    assert jam["time"] == "8:45 PM"                                          # its own time, kept after re-timing
    after = plan["stops"][names.index("Jazz Jam") + 1]
    assert after["time"] == "11:15 PM"                                       # the next stop follows on from it


def test_an_event_without_a_place_is_refused():
    plan, changed, problems = _edit([{"op": "add_event", "event": {"name": "Somewhere"}}])
    assert not changed and problems


def test_moving_a_stop():
    plan, changed, _ = _edit([{"op": "move", "stop": 4, "to": 1}])
    assert plan["stops"][0]["name"] == "Sweet Olenka's" and changed == ["Moved Sweet Olenka's to stop 1."]


# ---- the endpoint ----

def test_the_edit_endpoint(client):
    _, token = make_user(client, "editor")
    r = client.post("/api/v1/plan/edit", headers=auth_headers(token),
                    json={"plan": _plan(), "ops": [{"op": "remove", "stop": 2}, {"op": "set_start", "time": "7 PM"}]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["type"] == "itinerary"
    assert [s["name"] for s in body["plan"]["stops"]] == ["Bowl-a-rama", "The Rex", "Sweet Olenka's"]
    assert body["plan"]["stops"][0]["time"] == "7:00 PM"


def test_the_edit_endpoint_refuses_bad_input(client):
    _, token = make_user(client, "editor")
    h = auth_headers(token)
    assert client.post("/api/v1/plan/edit", json={"plan": _plan(), "ops": [{"op": "remove", "stop": 1}]}).status_code in (401, 403)
    assert client.post("/api/v1/plan/edit", headers=h, json={"plan": _plan(), "ops": [{"op": "explode"}]}).status_code == 422
    assert client.post("/api/v1/plan/edit", headers=h, json={"plan": _plan(), "ops": []}).status_code == 422
    big = _plan()
    big["stops"] = [_stop("activity", f"Stop {i}", 5) for i in range(11)]
    assert client.post("/api/v1/plan/edit", headers=h, json={"plan": big, "ops": [{"op": "remove", "stop": 1}]}).status_code == 422
    # a change that does nothing comes back as a message, not a plan
    r = client.post("/api/v1/plan/edit", headers=h, json={"plan": _plan(), "ops": [{"op": "set_party", "party_size": 2}]})
    assert r.status_code == 200
