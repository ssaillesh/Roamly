"""Typed requests carry the page's "Planning around" settings (`defaults`): what the
message says wins, the bar fills the gaps, and the reply's `understood` echoes what
was actually used — so the bar, the chat and the planner never disagree."""
import app.api.plan as plan_api
from tests.conftest import make_user, auth_headers

BAR = {"budget": 80, "vibe": "romantic", "group_type": "friends", "party_size": 4, "radius_km": 3,
       "time_of_day": "night", "transport": "walk", "interests": "karaoke"}


def _fake_plan(seen):
    def fake(**kw):
        seen.clear()
        seen.update(kw)
        return {"vibe": kw["vibe"], "budget": kw["budget"], "party_size": kw.get("party_size", 2),
                "estimated_cost": 30, "center": {"lat": kw["lat"], "lng": kw["lng"]}, "stops": [
                    {"slot": "dinner", "label": "Dinner", "icon": "🍽️", "name": "X",
                     "lat": kw["lat"], "lng": kw["lng"], "est_cost": 30}]}
    return fake


def _chat(client, token, text, **extra):
    return client.post("/api/v1/plan/chat", headers=auth_headers(token), json={
        "messages": [{"role": "user", "content": text}], **extra}).json()


def test_bar_fills_what_the_message_leaves_out(client, monkeypatch):
    _, token = make_user(client, "session")
    monkeypatch.setattr(plan_api.llm, "available", lambda: False)          # keyword reader
    seen = {}
    monkeypatch.setattr(plan_api, "build_plan", _fake_plan(seen))
    r = _chat(client, token, "plan something fun", defaults=BAR, lat=43.65, lng=-79.38)
    assert r["type"] == "itinerary"                 # no "what's the budget?" — the bar already says
    assert seen["budget"] == 80 and seen["vibe"] == "romantic" and seen["party_size"] == 4
    assert seen["radius"] == 3000 and seen["time_of_day"] == "night" and seen["interests"] == "karaoke"
    u = r["understood"]
    assert u["budget"] == 80 and u["interests"] == "karaoke" and u["radius_km"] == 3.0


def test_message_beats_the_bar(client, monkeypatch):
    _, token = make_user(client, "session2")
    monkeypatch.setattr(plan_api.llm, "available", lambda: False)
    seen = {}
    monkeypatch.setattr(plan_api, "build_plan", _fake_plan(seen))
    bar = {**BAR, "budget": 200, "radius_km": 10}
    r = _chat(client, token, "$60 chill date, within 2 km", defaults=bar, lat=43.65, lng=-79.38)
    assert seen["budget"] == 60 and seen["vibe"] == "chill" and seen["radius"] == 2000
    assert seen["party_size"] == 2 and r["understood"]["group_type"] == "date"


def test_bar_place_is_used_unless_the_message_names_one(client, monkeypatch):
    _, token = make_user(client, "session3")
    monkeypatch.setattr(plan_api.llm, "available", lambda: False)
    places = {"Kensington Market": (43.6547, -79.4005), "Queen West": (43.6447, -79.4115)}
    monkeypatch.setattr(plan_api, "geocode", lambda name, _=None: places.get(name))
    seen = {}
    monkeypatch.setattr(plan_api, "build_plan", _fake_plan(seen))
    bar = {"budget": 80, "vibe": "chill", "location": "Kensington Market"}
    r = _chat(client, token, "plan a date", defaults=bar)
    assert (seen["lat"], seen["lng"]) == places["Kensington Market"]
    assert r["understood"]["location"] == "Kensington Market"
    _chat(client, token, "plan a date in Queen West", defaults=bar)
    assert (seen["lat"], seen["lng"]) == places["Queen West"]


def test_bad_defaults_are_ignored(client, monkeypatch):
    """`defaults` is untrusted client input — sanitised exactly like chip prefs."""
    _, token = make_user(client, "session4")
    monkeypatch.setattr(plan_api.llm, "available", lambda: False)
    seen = {}
    monkeypatch.setattr(plan_api, "build_plan", _fake_plan(seen))
    junk = {"budget": "lots", "vibe": "evil", "radius_km": 999, "party_size": -3, "group_type": "army"}
    r = _chat(client, token, "$50 chill night", defaults=junk, lat=43.65, lng=-79.38)
    assert r["type"] == "itinerary" and seen["budget"] == 50 and seen["vibe"] == "chill"
    assert seen["radius"] == 20000                  # clamped to the 20 km max
