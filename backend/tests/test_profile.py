"""Taste profile: survey save/resume/complete, the answers → planner mapping, the
request-beats-profile merge rules, and a returning user's one-tap plan."""
import uuid

import app.api.plan as plan_api
from app.database import SessionLocal
from app.models import TasteProfile
from app.services import planner, taste_profile
from tests.conftest import make_user, auth_headers

FULL = {
    "crew": "partner", "time": "evening", "transport": "walk", "distance": "near",
    "energy": "recharge", "crowds": "quiet", "novelty": "new", "pace": "relaxed",
    "diet": ["vegan"], "cuisines": ["japanese", "thai"], "spend": "2",
    "loves": ["live_music", "museums"], "nopes": ["nightlife"], "drinks": "none",
}


def test_survey_definition(client):
    r = client.get("/api/v1/users/me/profile/questions")
    assert r.status_code == 200
    qs = r.json()["questions"]
    assert len(qs) == 14
    assert {q["id"] for q in qs} == set(FULL)
    assert all(q["options"] for q in qs)


def test_autosave_resume_and_complete(client):
    _, token = make_user(client, "taste")
    h = auth_headers(token)

    r = client.get("/api/v1/users/me/profile", headers=h).json()
    assert r["completed"] is False and set(r["missing"]) == set(taste_profile.REQUIRED)

    # one answer at a time (autosave) — merged, invalid values dropped
    client.put("/api/v1/users/me/profile", headers=h, json={"answers": {"crew": "partner"}})
    r = client.put("/api/v1/users/me/profile", headers=h,
                   json={"answers": {"energy": "recharge", "crowds": "not-an-option", "bogus": 1}}).json()
    assert r["answers"] == {"crew": "partner", "energy": "recharge"}
    assert r["completed"] is False and r["archetype"] is None

    # the rest → completed, archetype + summary + planner defaults
    r = client.put("/api/v1/users/me/profile", headers=h, json={"answers": FULL}).json()
    assert r["completed"] is True and r["missing"] == []
    assert r["archetype"]["title"].startswith("Cozy")
    assert "🚫 No alcohol" in r["summary"]
    assert r["defaults"]["group_type"] == "date" and r["defaults"]["budget"] == 140.0

    # resume: a fresh GET returns everything saved
    assert client.get("/api/v1/users/me/profile", headers=h).json()["answers"] == FULL


def test_optional_questions_can_be_skipped(client):
    _, token = make_user(client, "taste")
    required_only = {k: v for k, v in FULL.items() if k not in ("diet", "nopes")}
    r = client.put("/api/v1/users/me/profile", headers=auth_headers(token),
                   json={"answers": required_only}).json()
    assert r["completed"] is True


def test_defaults_mapping():
    d = taste_profile.defaults_for(FULL)
    assert (d["group_type"], d["party_size"]) == ("date", 2)
    assert d["time_of_day"] == "evening" and d["transport"] == "walk"
    assert d["radius_scale"] == 0.6 and d["crowd_pref"] == -1.0 and d["target_stops"] == 3
    assert d["vibe"] == "chill"                      # recharge energy
    assert d["dietary"] == "vegan" and d["no_alcohol"] is True and d["vary"] is True
    assert "sushi" in d["likes"] and "museum" in d["likes"]
    assert "nightclub" in d["avoid"]
    assert taste_profile.defaults_for({}) == {}      # no profile → no opinions

    # a love that's also a hard nope counts as a nope
    clash = taste_profile.defaults_for({**FULL, "loves": ["museums", "nightlife"]})
    assert "lounge" not in clash["likes"] and "nightclub" in clash["avoid"] and "museum" in clash["likes"]


def test_request_beats_profile():
    taste = taste_profile.defaults_for(FULL)

    # a stated crew wins, and the profile budget re-scales per person for it
    prefs = {"group_type": "friends", "party_size": 4}
    plan_api._apply_taste_early(prefs, taste)
    plan_api._apply_taste_late(prefs, taste)
    assert prefs["group_type"] == "friends" and prefs["budget"] == 70.0 * 4

    # no stated crew → the profile's crew replaces the LLM's default party size
    prefs = {"party_size": 2}
    plan_api._apply_taste_early(prefs, {**taste, "group_type": "friends", "party_size": 4})
    assert prefs["group_type"] == "friends" and prefs["party_size"] == 4

    # a stated interest overrides a matching profile nope
    prefs = {"interests": "dance clubs"}
    plan_api._apply_taste_late(prefs, taste)
    assert "dance club" not in prefs["avoid"]

    # a request's avoid removes a matching profile love
    prefs = {"avoid": "museums"}
    plan_api._apply_taste_late(prefs, taste)
    assert "museum" not in prefs["likes"] and "sushi" in prefs["likes"]


def test_returning_user_plans_in_one_tap(client, monkeypatch):
    _, token = make_user(client, "taste")
    h = auth_headers(token)
    client.put("/api/v1/users/me/profile", headers=h, json={"answers": FULL})

    seen = {}

    def fake_build_plan(**kw):
        seen.update(kw)
        return {"vibe": kw["vibe"], "budget": kw["budget"], "party_size": kw["party_size"],
                "estimated_cost": 60, "center": {"lat": kw["lat"], "lng": kw["lng"]}, "stops": [
                    {"slot": "dinner", "label": "Dinner", "icon": "🍽️", "name": "Test Kitchen",
                     "lat": kw["lat"], "lng": kw["lng"], "est_cost": 30}]}

    monkeypatch.setattr(plan_api, "build_plan", fake_build_plan)
    monkeypatch.setattr(plan_api.llm, "available", lambda: (_ for _ in ()).throw(AssertionError("LLM called")))

    # structured chip request with NO budget/vibe → ready immediately from the profile
    r = client.post("/api/v1/plan/chat", headers=h,
                    json={"messages": [], "lat": 43.65, "lng": -79.38, "prefs": {}})
    body = r.json()
    assert r.status_code == 200 and body["type"] == "itinerary", body
    assert seen["budget"] == 140.0 and seen["vibe"] == "chill"
    assert seen["no_alcohol"] is True and seen["crowd_pref"] == -1.0 and seen["target_stops"] == 3

    # chips override the profile (and a chip budget is a total, never re-scaled)
    client.post("/api/v1/plan/chat", headers=h, json={
        "messages": [], "lat": 43.65, "lng": -79.38,
        "prefs": {"group_type": "friends", "party_size": 4, "budget": 200, "vibe": "adventurous"}})
    assert seen["group_type"] == "friends" and seen["budget"] == 200.0 and seen["vibe"] == "adventurous"


def test_scoring_knobs():
    base = {"name": "X", "lat": 43.65, "lng": -79.38, "rating": 4.5, "categories": ["Arcades"]}
    busy = {**base, "review_count": 3000, "popularity": 0.9}
    quiet = {**base, "review_count": 20, "popularity": 0.1}
    a = (43.65, -79.38)
    gap_quiet = planner._score(busy, a, 1, taste={"crowd_pref": -1}) - planner._score(quiet, a, 1, taste={"crowd_pref": -1})
    gap_busy = planner._score(busy, a, 1, taste={"crowd_pref": 1}) - planner._score(quiet, a, 1, taste={"crowd_pref": 1})
    assert gap_quiet < gap_busy                       # quiet profiles care less about buzz
    assert planner._score(busy, a, 1) == planner._score(busy, a, 1, taste={})   # neutral = unchanged
    assert planner._is_alcohol_venue({"categories": ["Cocktail Bars"]})
    assert planner._is_alcohol_venue({"categories": ["Night Club"]})
    assert not planner._is_alcohol_venue({"categories": ["Gastropubs", "Barbeque"]})


def test_account_delete_removes_profile(client):
    user, token = make_user(client, "taste")
    h = auth_headers(token)
    client.put("/api/v1/users/me/profile", headers=h, json={"answers": FULL})
    assert client.delete("/api/v1/auth/account", headers=h).status_code == 204
    with SessionLocal() as db:
        assert db.get(TasteProfile, uuid.UUID(user["id"])) is None
