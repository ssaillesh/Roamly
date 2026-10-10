"""Whole conversations against the planner, the way the page has them.

Phase 1 checks: follow-ups edit the plan on screen instead of starting over,
nothing is asked twice, budget/mood are never asked, the budget means what was
said, and things ruled out stay ruled out.
"""
import pytest

from app.services import planner
from tests.conftest import make_user
from tests.planner_eval.harness import Convo, offline


@pytest.fixture
def convo(client, monkeypatch):
    offline(monkeypatch)
    _, token = make_user(client, "evalconv")
    return lambda **kw: Convo(client, token, **kw)


def test_swap_changes_only_that_stop(convo):
    c = convo()
    first = c.say("$80 date night")
    assert first["type"] == "itinerary"
    before = c.names()
    dinner = next(i for i, s in enumerate(c.plan["stops"]) if s["slot"] == "dinner")

    r = c.say("swap the dinner for sushi")
    assert r["type"] == "itinerary" and r["intent"] == "edit_plan", r
    after = c.names()
    assert "sushi" in after[dinner].lower()
    assert [n for i, n in enumerate(after) if i != dinner] == [n for i, n in enumerate(before) if i != dinner]
    c.assert_healthy()


def test_remove_and_add(convo):
    c = convo()
    c.say("$250 chill night out")      # roomy budget: this test is about edits, not trimming
    before = c.names()
    bar = next(s["name"] for s in c.plan["stops"] if s["slot"] in ("leisure", "drinks"))

    c.say("remove the bar")
    assert bar not in c.names() and len(c.names()) == len(before) - 1

    kept = c.names()
    r = c.say("add bowling")
    assert r["intent"] == "edit_plan"
    assert any("bowl" in n.lower() for n in c.names())
    assert all(n in c.names() for n in kept)      # nothing else moved out
    c.assert_healthy()


def test_cheaper_replans_on_a_lower_budget_without_asking(convo):
    c = convo()
    c.say("$120 date night")
    r = c.say("make it cheaper")
    assert r["type"] == "itinerary"
    assert r["plan"]["budget"] == 80          # 120 × 0.65 → nearest $5, stated in the reply
    assert "cheaper" in r["message"].lower()
    c.assert_healthy()


def test_group_budget_is_asked_once_then_respected(convo):
    c = convo()
    q = c.say("night out with 4 friends, $60")
    assert q["type"] == "question" and q["ask"] == "budget_scope"
    r = c.say("$60 each")
    assert r["type"] == "itinerary" and r["plan"]["budget"] == 240
    c.assert_healthy()


def test_unclear_group_budget_answer_is_not_asked_again(convo):
    c = convo()
    c.say("night out with 4 friends, $60")
    r = c.say("whatever works")
    assert r["type"] == "itinerary" and r["plan"]["budget"] == 60
    assert "total for all 4" in r["message"]
    c.assert_healthy()


def test_never_asks_for_budget_or_mood(convo):
    c = convo(bar={})
    r = c.say("plan something fun for tonight")
    assert r["type"] == "itinerary", r
    assert "$100" in r["message"]             # the assumption is said, not asked
    c.assert_healthy()


def test_no_bars_still_applies_to_later_edits(convo):
    c = convo()
    c.say("$90 evening out, no bars")
    assert not any("bar" in n.lower() for n in c.names())
    c.say("swap the dinner for thai")
    assert not any("bar" in n.lower() for n in c.names())
    assert "bars" in c.bar.get("avoid", "")
    c.assert_healthy()


def test_starting_over_makes_a_new_plan(convo):
    c = convo()
    c.say("$80 date night")
    r = c.say("start over — plan a family afternoon instead")
    assert r["intent"] == "new_plan" and r["type"] == "itinerary"
    assert r["understood"]["group_type"] == "family"


def test_location_is_asked_then_used(convo):
    c = convo(located=False)
    q = c.say("date night")
    assert q["ask"] == "location" and q["message"].startswith("Tell me where")
    r = c.say("in Toronto")
    assert r["type"] == "itinerary"
    c.assert_healthy()


def test_bad_plan_on_screen_falls_back_to_planning(convo):
    c = convo()
    c.say("$80 date night")
    c.plan = {"stops": "garbage"}             # a broken page state must not break the reply
    r = c.say("swap the dinner for sushi")
    assert r["type"] == "itinerary"


def test_a_named_neighbourhood_keeps_the_plan_there(client, monkeypatch):
    """"In Kensington Market" searches around it, not 7 km out (the bar's radius)."""
    from app.services import venue_index
    offline(monkeypatch)
    radii = []
    orig = planner._gather

    def spy(anchor, slot_key, price, radius, *a, **kw):
        radii.append(radius)
        return orig(anchor, slot_key, price, radius, *a, **kw)
    monkeypatch.setattr(planner, "_gather", spy)
    monkeypatch.setattr(venue_index, "find_place", lambda name, near=None:
                        (43.6547, -79.4005, "neighborhood") if "kensington" in name.lower() else None)
    _, token = make_user(client, "evalhood")
    c = Convo(client, token)                                   # the bar's radius: 7 km
    r = c.say("date night in Kensington Market")
    assert r["type"] == "itinerary"
    assert radii and max(radii) <= 2000
