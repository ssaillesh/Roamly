"""Planner evaluation harness: a small fake downtown Toronto, and a client that
talks to /plan/chat exactly the way webui/roamly.html does — whole history,
`turn_start`, the plan on screen, the questions already asked, and a settings
bar that mirrors `understood` — so conversations can be replayed and checked.

Offline and deterministic: venue search, weather, live events and (unless a
test scripts it) the LLM are faked.
"""
import uuid

from app.api import plan as plan_api
from app.services import llm, plan_edit, planner

C = (43.6487, -79.3854)   # King & Bay


def _v(name, cats, price, rating, dlat, dlng):
    return {"source": "fixture", "name": name, "lat": C[0] + dlat, "lng": C[1] + dlng,
            "rating": rating, "review_count": 800, "price": price, "categories": cats,
            "address": None, "image": None, "url": None}


CITY = {
    "dinner": [
        _v("Pai Northern Thai Kitchen", ["Thai"], "$$", 4.6, 0.001, 0.002),
        _v("Gusto 101", ["Italian"], "$$", 4.4, -0.004, -0.02),
        _v("Sushi Kaji", ["Sushi Bars", "Japanese"], "$$$", 4.8, 0.003, -0.01),
        _v("Burger's Priest", ["Burgers"], "$", 4.2, 0.002, 0.004),
        _v("Canoe", ["Canadian (New)"], "$$$$", 4.6, 0.0, 0.003),
    ],
    "activity": [
        _v("Ripley's Aquarium of Canada", ["Aquariums"], "$$", 4.6, -0.006, -0.0),
        _v("The Rec Room", ["Arcades"], "$$", 4.3, -0.005, 0.001),
        _v("Bowlerama Toronto", ["Bowling"], "$", 4.1, 0.01, -0.02),
        _v("Escape Casa Loma", ["Escape Games"], "$$", 4.7, 0.03, -0.02),
        _v("Art Gallery of Ontario", ["Museums", "Art Galleries"], "$$", 4.7, 0.005, -0.008),
    ],
    "leisure": [
        _v("Bar Raval", ["Cocktail Bars"], "$$", 4.5, 0.007, -0.025),
        _v("The Rex Hotel Jazz", ["Music Venues"], "$", 4.4, 0.002, -0.005),
        _v("Comedy Bar", ["Comedy Clubs"], "$", 4.5, 0.01, -0.04),
        _v("Library Bar", ["Lounges"], "$$$", 4.6, 0.0, 0.001),
    ],
    "dessert": [
        _v("Sweet Jesus", ["Ice Cream & Frozen Yogurt"], "$", 4.2, 0.001, -0.003),
        _v("Bang Bang Ice Cream", ["Ice Cream & Frozen Yogurt"], "$", 4.6, 0.01, -0.04),
        _v("Nadège Patisserie", ["Bakeries", "Desserts"], "$$", 4.5, 0.005, -0.02),
    ],
    "cafe": [
        _v("Pilot Coffee Roasters", ["Coffee & Tea"], "$", 4.5, 0.001, 0.001),
        _v("Sam James Coffee Bar", ["Coffee & Tea"], "$", 4.6, 0.003, -0.002),
    ],
    "scenic": [
        _v("Trinity Bellwoods Park", ["Parks"], None, 4.7, 0.0, -0.03),
        _v("Toronto Islands", ["Parks", "Beaches"], None, 4.7, -0.015, 0.0),
    ],
}
CITY["lunch"] = CITY["dinner"]
CITY["drinks"] = CITY["leisure"]
PLACES = {"toronto": C, "kensington market": (43.6547, -79.4005)}


def fake_gather(anchor, slot_key, price_pref, radius, dietary, term_override=None, cat_override=None, **kw):
    """Like the real search: a term narrows the results when anything matches it."""
    cands = [dict(c) for c in CITY.get(slot_key, [])]
    if term_override:
        words = [w for w in term_override.lower().replace(",", " ").split() if len(w) >= 3]
        hits = [c for c in cands
                if any(w in (c["name"] + " " + " ".join(c["categories"])).lower() for w in words)]
        if hits:
            return hits
    return cands


def offline(monkeypatch, llm_replies=None):
    """Fake the outside world. `llm_replies`: router answers to hand out in order
    (narration calls get None → template text); None → no LLM at all."""
    monkeypatch.setattr(planner, "_gather", fake_gather)
    monkeypatch.setattr(plan_edit, "_gather", fake_gather)
    monkeypatch.setattr(planner.weather_svc, "get_weather", lambda lat, lng: None)
    monkeypatch.setattr(planner.live_events, "available_for", lambda lat, lng: False)
    monkeypatch.setattr(plan_api, "geocode", lambda name, _=None: PLACES.get((name or "").strip().lower()))
    monkeypatch.setattr(plan_api, "geocode_place", lambda name: PLACES.get((name or "").strip().lower()))
    if llm_replies is None:
        monkeypatch.setattr(llm, "available", lambda: False)
        return None
    queue = list(llm_replies)
    calls = []

    def chat_json(messages, **kw):
        system = messages[0]["content"]
        calls.append(system[:40])
        if system.startswith("You route messages"):
            return queue.pop(0) if queue else None
        return None   # narration → template
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "chat_json", chat_json)
    return calls


class Convo:
    """A planner conversation driven the way the page drives it."""

    BAR = {"budget": 100, "vibe": "chill", "group_type": "date", "party_size": 2,
           "time_of_day": "evening", "transport": "transit", "radius_km": 7}

    def __init__(self, client, token, *, located=True, bar=None):
        self.client, self.token = client, token
        self.messages, self.turn_start, self.asked = [], 0, []
        self.plan = None
        self.bar = dict(self.BAR if bar is None else bar)
        self.loc = C if located else None
        self.session = uuid.uuid4().hex
        self.replies = []        # every server response, in order

    def say(self, text: str) -> dict:
        self.messages.append({"role": "user", "content": text})
        body = {"messages": self.messages, "turn_start": self.turn_start, "asked": self.asked,
                "current_plan": self.plan, "defaults": self.bar or None, "session_id": self.session,
                "seed": 7, "tz": "America/Toronto"}
        if self.loc:
            body.update(lat=self.loc[0], lng=self.loc[1])
        r = self.client.post("/api/v1/plan/chat", json=body,
                             headers={"Authorization": f"Bearer {self.token}"})
        assert r.status_code == 200, r.text
        res = r.json()
        self.replies.append(res)
        self.messages.append({"role": "assistant", "content": res["message"]})
        if res["type"] in ("itinerary", "events"):
            self.turn_start = len(self.messages)          # a result closes the turn
        if res.get("plan"):
            self.plan = res["plan"]
        if res.get("ask") and res["ask"] not in self.asked:
            self.asked.append(res["ask"])
        for k, v in (res.get("understood") or {}).items():   # the bar mirrors what was understood
            self.bar[k] = v
        return res

    # ---- checks shared by every scenario ----
    def names(self, plan=None):
        return [s["name"] for s in (plan or self.plan)["stops"]]

    def questions(self):
        return [r["ask"] for r in self.replies if r.get("type") == "question" and r.get("ask")]

    def assert_healthy(self):
        """No question asked twice; never asked for budget or mood."""
        qs = self.questions()
        assert len(qs) == len(set(qs)), f"repeated question: {qs}"
        for r in self.replies:
            low = r["message"].lower()
            assert "what's the budget" not in low and "mood you're after" not in low, r["message"]
