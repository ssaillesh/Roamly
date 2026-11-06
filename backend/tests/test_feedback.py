"""Learning from what people keep (services/feedback.py): the maths stays small and safe,
nothing changes without data, each person counts once, and the endpoints record the
right things."""
import uuid

import pytest
from sqlalchemy import delete, select

from app.database import SessionLocal
from app.models import StopFeedback, User, VenueSignal
from app.services import feedback, plan_edit, planner
from app.services.plan_edit import EditContext, apply_edits
from tests.conftest import auth_headers, make_user

A = (43.65, -79.38)


# ---- the maths ----

def test_no_data_means_no_lean():
    assert feedback.score(0, 0) == 0.5
    assert feedback.bonus(500, 0.5) == 0.0


def test_a_few_votes_barely_move_the_score():
    assert 0.5 < feedback.score(3, 0) < 0.62          # three saves aren't proof
    assert feedback.score(30, 0) > 0.85               # thirty are
    assert feedback.score(0, 30) < 0.15


def test_nothing_counts_until_enough_people_saw_it():
    assert feedback.bonus(feedback.MIN_USERS - 1, 0.95) == 0.0
    assert feedback.bonus(feedback.MIN_USERS, 0.95) > 0


def test_the_nudge_is_capped_and_can_be_switched_off(monkeypatch):
    assert feedback.bonus(1000, 1.0) == feedback.SIGNAL_CAP
    assert feedback.bonus(1000, 0.0) == -feedback.SIGNAL_CAP
    monkeypatch.setattr(feedback, "SIGNAL_WEIGHT", 0)
    assert feedback.bonus(1000, 1.0) == 0.0


def _venue(name, rating, **kw):
    return {"name": name, "lat": A[0], "lng": A[1], "rating": rating, "review_count": 100,
            "categories": ["restaurant"], **kw}


def test_a_venue_without_a_signal_scores_exactly_as_before():
    plain = _venue("Plain", 4.2)
    assert planner._score(plain, A, 1.0) == planner._score({**plain, "signal": 0}, A, 1.0)


def test_a_loved_place_cant_beat_one_half_a_star_better():
    loved = _venue("Loved", 4.0, signal=feedback.SIGNAL_CAP)
    better = _venue("Better", 4.5)
    assert planner._score(better, A, 1.0) > planner._score(loved, A, 1.0)
    # …but it does settle a tie
    assert planner._score(loved, A, 1.0) > planner._score(_venue("Same", 4.0), A, 1.0)


def test_only_stored_venues_are_recorded():
    stops = [{"vid": "ovt:a", "slot": "dinner"}, {"name": "A typed-in place"}, {"vid": "", "slot": "event"}]
    assert feedback.rows_for("shown", stops) == [("shown", "ovt:a", "dinner")]


def test_admin_and_test_accounts_dont_count():
    real = User(email="sam@gmail.com", username="sam", is_admin=False, is_test=False)
    assert feedback.counts_for(real)
    assert not feedback.counts_for(User(email="x@gmail.com", username="x", is_admin=True, is_test=False))
    assert not feedback.counts_for(User(email="x@example.com", username="x", is_admin=False, is_test=False))


# ---- what an edit tells us ----

@pytest.fixture
def _fake_search(monkeypatch):
    monkeypatch.setattr(plan_edit, "_gather", lambda anchor, slot_key, *a, **k: [
        {"id": f"ovt:new-{slot_key}", "name": f"New {slot_key}", "lat": anchor[0], "lng": anchor[1],
         "rating": 4.4, "review_count": 50, "categories": [slot_key]}])


def _plan():
    stop = lambda slot, vid: {"slot": slot, "label": slot, "icon": "x", "name": f"{slot} place", "vid": vid,
                              "lat": A[0], "lng": A[1], "est_cost": 20}
    return {"vibe": "chill", "budget": 200, "party_size": 2, "estimated_cost": 0,
            "center": {"lat": A[0], "lng": A[1]}, "start": "2026-11-06T18:00",
            "stops": [stop("activity", "ovt:act"), stop("dinner", "ovt:din"), stop("leisure", "ovt:bar")]}


def test_removing_or_swapping_for_something_else_counts_against_the_place(_fake_search):
    ctx = EditContext()
    apply_edits(_plan(), [{"op": "remove", "stop": 1}, {"op": "swap", "slot": "dinner", "wish": "something else"}], ctx)
    assert ctx.dropped == [("removed", "ovt:act", "activity"), ("swapped", "ovt:din", "dinner")]


def test_a_swap_for_a_particular_wish_or_a_shorter_plan_doesnt(_fake_search):
    ctx = EditContext()
    apply_edits(_plan(), [{"op": "swap", "slot": "dinner", "wish": "sushi"}], ctx)
    apply_edits(_plan(), [{"op": "swap", "slot": "dinner", "wish": "cheaper"}], ctx)
    apply_edits(_plan(), [{"op": "set_length", "stops": 2}], ctx)
    assert ctx.dropped == []


# ---- storing and summing up (real database) ----

@pytest.fixture
def people():
    """Real-looking accounts (test accounts are ignored by design); removed afterwards."""
    tag = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        users = [User(email=f"fb{tag}{i}@gmail.com", username=f"fb{tag}{i}", display_name="F",
                      is_admin=False, is_test=False) for i in range(feedback.MIN_USERS + 2)]
        db.add_all(users)
        db.commit()
        ids = [u.id for u in users]
    yield tag, ids
    with SessionLocal() as db:
        db.execute(delete(User).where(User.id.in_(ids)))          # their feedback goes with them
        db.execute(delete(VenueSignal).where(VenueSignal.vid.like(f"ovt:{tag}%")))
        db.commit()


def _rows(vid):
    with SessionLocal() as db:
        return db.scalars(select(StopFeedback).where(StopFeedback.vid == vid)).all()


def test_the_same_action_on_the_same_plan_counts_once(people):
    tag, ids = people
    vid = f"ovt:{tag}x"
    for _ in range(3):
        feedback.record(ids[0], "plan1", [("removed", vid, "dinner")])
    feedback.record(ids[0], "plan2", [("removed", vid, "dinner")])
    assert len(_rows(vid)) == 2


def test_one_account_can_only_do_so_much_in_an_hour(people, monkeypatch):
    tag, ids = people
    monkeypatch.setattr(feedback, "HOURLY_LIMIT", 3)
    feedback.record(ids[0], "p", [("removed", f"ovt:{tag}{i}", "dinner") for i in range(5)])
    feedback.record(ids[0], "p", [("shown", f"ovt:{tag}s", "dinner")])          # shown isn't limited
    with SessionLocal() as db:
        events = db.scalars(select(StopFeedback.event).where(StopFeedback.user_id == ids[0])).all()
    assert sorted(events) == ["removed"] * 3 + ["shown"]


def test_the_nightly_summary_counts_people_not_clicks(people):
    tag, ids = people
    loved, dropped = f"ovt:{tag}loved", f"ovt:{tag}dropped"
    for i, uid in enumerate(ids):
        feedback.record(uid, f"plan{i}", [("shown", loved, "dinner"), ("shown", dropped, "drinks")])
        feedback.record(uid, f"plan{i}", [("saved", loved, "dinner"), ("removed", dropped, "drinks")])
    feedback.record(ids[0], "again", [("saved", loved, "dinner")])          # a second save by the same person
    with SessionLocal() as db:
        feedback.aggregate(db)
        a = db.get(VenueSignal, loved)
        b = db.get(VenueSignal, dropped)
    n = len(ids)
    assert (a.users, a.positive, a.negative) == (n, n, 0)
    assert (b.users, b.positive, b.negative) == (n, 0, n)
    assert feedback.signals()[loved] > 0 > feedback.signals()[dropped]


# ---- the endpoints ----

@pytest.fixture
def recorded(monkeypatch):
    calls = []
    monkeypatch.setattr(feedback, "counts_for", lambda user: True)
    monkeypatch.setattr(feedback, "record", lambda uid, key, rows: calls.append(rows))
    return calls


def test_saving_a_plan_is_recorded(client, recorded):
    _, token = make_user(client, "saver")
    body = {"event": "saved", "stops": [{"vid": "ovt:a", "slot": "dinner"}, {"name": "no id"}]}
    assert client.post("/api/v1/plan/feedback", json=body).status_code in (401, 403)
    assert client.post("/api/v1/plan/feedback", headers=auth_headers(token),
                       json={**body, "event": "loved"}).status_code == 422
    r = client.post("/api/v1/plan/feedback", headers=auth_headers(token), json=body)
    assert r.status_code == 204
    assert recorded == [[("saved", "ovt:a", "dinner")]]


def test_an_edit_records_what_was_dropped_and_what_is_shown(client, recorded, _fake_search):
    _, token = make_user(client, "editor")
    r = client.post("/api/v1/plan/edit", headers=auth_headers(token),
                    json={"plan": _plan(), "ops": [{"op": "remove", "stop": 2}]})
    assert r.status_code == 200, r.text
    assert [("removed", "ovt:din", "dinner")] in recorded
    assert [("shown", "ovt:act", "activity"), ("shown", "ovt:bar", "leisure")] in recorded


def test_sharing_records_the_places_in_it(client, recorded):
    _, token = make_user(client, "sharer")
    body = {"title": "A night", "party_size": 2,
            "stops": [{"name": "Bar Raval", "vid": "ovt:raval", "slot": "drinks"}, {"name": "Jazz"}]}
    r = client.post("/api/v1/share/plan", headers=auth_headers(token), json=body)
    assert r.status_code == 200, r.text
    assert recorded == [[("shared", "ovt:raval", "drinks")]]


def test_test_accounts_record_nothing(client, monkeypatch):
    calls = []
    monkeypatch.setattr(feedback, "record", lambda *a: calls.append(a))
    _, token = make_user(client, "tester")          # an example.com address: a test account
    client.post("/api/v1/plan/feedback", headers=auth_headers(token),
                json={"event": "saved", "stops": [{"vid": "ovt:a"}]})
    assert calls == []


def test_the_admin_page_shows_edits_and_what_people_keep(client, _fake_search):
    from tests.test_admin import make_admin, make_real_user
    _, admin = make_admin(client)
    _, token = make_real_user(client)
    r = client.post("/api/v1/plan/edit", headers=auth_headers(token),
                    json={"plan": _plan(), "ops": [{"op": "remove", "stop": 2}]})
    assert r.status_code == 200
    p = client.get("/api/v1/admin/planner", headers=auth_headers(admin)).json()
    assert any(r["edit"] for r in p["series"])
    assert any(o["key"] == "remove" for o in p["breakdown"]["edit_ops"])
    assert "quick_rate" in p["totals"]
    assert p["learned"]["min_users"] == feedback.MIN_USERS and "top" in p["learned"]
