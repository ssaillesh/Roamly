"""The LLM router: its decisions are applied faithfully, and a bad answer falls
back to the keyword router instead of failing the request."""
from tests.conftest import make_user
from tests.planner_eval.harness import Convo, offline


def _plan(prefs):
    return {"intent": "new_plan", "ops": [], "prefs": prefs, "answer": None}


def test_llm_edit_swaps_the_numbered_stop(client, monkeypatch):
    offline(monkeypatch, llm_replies=[
        _plan({"budget": 80, "vibe": "romantic", "group_type": "date"}),
        {"intent": "edit_plan", "ops": [{"op": "swap", "stop": 2, "wish": "sushi"}], "prefs": {}},
    ])
    _, token = make_user(client, "evalllm")
    c = Convo(client, token)
    c.say("romantic date for $80")
    before = c.names()
    r = c.say("can the second one be sushi instead")
    assert r["intent"] == "edit_plan"
    after = c.names()
    assert "sushi" in after[1].lower() and after[0] == before[0] and after[2:] == before[2:]


def test_llm_reads_per_person_budget(client, monkeypatch):
    offline(monkeypatch, llm_replies=[
        _plan({"budget": 50, "budget_scope": "per_person", "party_size": 4, "group_type": "friends"}),
    ])
    _, token = make_user(client, "evalllm2")
    r = Convo(client, token).say("4 of us, about fifty a head")
    assert r["type"] == "itinerary" and r["plan"]["budget"] == 200


def test_llm_answers_questions_about_the_plan(client, monkeypatch):
    offline(monkeypatch, llm_replies=[
        _plan({"budget": 80}),
        {"intent": "ask_about_plan", "ops": [], "prefs": {}, "answer": "It's about a 10 minute walk."},
    ])
    _, token = make_user(client, "evalllm3")
    c = Convo(client, token)
    c.say("$80 night")
    before = c.names()
    r = c.say("how far apart are the stops?")
    assert r["type"] == "message" and "walk" in r["message"]
    assert c.names() == before                       # the plan on screen is untouched


def test_bad_llm_answer_falls_back_to_keywords(client, monkeypatch):
    offline(monkeypatch, llm_replies=[
        _plan({"budget": 80}),
        {"intent": "make me a sandwich"},             # nonsense → keyword router
    ])
    _, token = make_user(client, "evalllm4")
    c = Convo(client, token)
    c.say("$80 night")
    r = c.say("remove the dessert")
    assert r["type"] in ("itinerary", "message")
    assert r.get("intent") in ("edit_plan", "new_plan")
