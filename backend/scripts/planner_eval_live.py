"""Replay planner conversations against a running API (real venue search and,
if a key is set, the real AI) and print each one for reading and scoring.

    .venv/bin/python -m scripts.planner_eval_live                      # local API
    .venv/bin/python -m scripts.planner_eval_live https://trekrank.onrender.com/api/v1

It signs up a throwaway @example.com account (left out of admin stats) and
deletes it at the end. Each turn talks to /plan/chat exactly the way the
planner page does (whole history, the plan on screen, questions already asked).
The automatic checks match tests/planner_eval; read the plans for the rest.
"""
import sys
import uuid

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8001/api/v1"
TORONTO = (43.6487, -79.3854)

SCENARIOS = {
    "date night, then edits": ["$80 date night", "swap the dinner for sushi", "add dessert"],
    "friends, budget each": ["night out with 4 friends, $60", "$60 each", "make it cheaper"],
    "family afternoon": ["family afternoon with two kids, around $120", "remove the first stop"],
    "no bars": ["$90 evening out, no bars", "swap the activity for something more active"],
    "fancier": ["chill evening for two, $70", "make it fancier"],
    "what's on": ["what's happening tonight?"],
}


class Convo:
    def __init__(self, http, token):
        self.http, self.h = http, {"Authorization": f"Bearer {token}"}
        self.messages, self.turn_start, self.asked, self.plan = [], 0, [], None
        self.bar = {"budget": 100, "vibe": "chill", "group_type": "date", "party_size": 2,
                    "time_of_day": "evening", "transport": "transit", "radius_km": 7}
        self.session, self.problems = uuid.uuid4().hex, []

    def say(self, text):
        self.messages.append({"role": "user", "content": text})
        r = self.http.post(f"{BASE}/plan/chat", headers=self.h, timeout=90, json={
            "messages": self.messages, "turn_start": self.turn_start, "asked": self.asked,
            "current_plan": self.plan, "defaults": self.bar, "session_id": self.session,
            "lat": TORONTO[0], "lng": TORONTO[1], "tz": "America/Toronto"})
        res = r.json() if r.status_code == 200 else {"type": "error", "message": f"HTTP {r.status_code}: {r.text[:200]}"}
        self.messages.append({"role": "assistant", "content": res.get("message", "")})
        if res.get("type") in ("itinerary", "events"):
            self.turn_start = len(self.messages)
        before = [s["name"] for s in (self.plan or {}).get("stops", [])]
        if res.get("plan"):
            self.plan = res["plan"]
        ask = res.get("ask")
        if ask in self.asked:
            self.problems.append(f"asked '{ask}' twice")
        elif ask:
            self.asked.append(ask)
        for k, v in (res.get("understood") or {}).items():
            self.bar[k] = v
        low = res.get("message", "").lower()
        if "what's the budget" in low or "mood you're after" in low:
            self.problems.append("asked for budget/mood")
        if res.get("type") == "error":
            self.problems.append(res["message"])
        return res, before


def show(res, before):
    print(f"  ← [{res.get('type')}/{res.get('intent')}] {res.get('message', '')}")
    plan = res.get("plan")
    if plan:
        pp = max(1, plan.get("party_size") or 1)
        print(f"    budget ${plan['budget']:g} for {pp} · estimated ${plan['estimated_cost']} · "
              f"{plan.get('walk_km', 0)} km")
        for i, s in enumerate(plan["stops"], 1):
            mark = "  " if s["name"] in before else "+ " if before else "  "
            cats = ", ".join(s.get("categories", [])[:2])
            print(f"    {mark}{i}. {s.get('time') or '--:--':>8}  {s['label']}: {s['name']}"
                  f"  (~${s['est_cost']}/pp{', ' + cats if cats else ''})")
    for e in (res.get("events") or [])[:5]:
        print(f"    • {e.get('name')} @ {e.get('venue')} {e.get('time') or ''}")
    if res.get("quick_replies"):
        print(f"    quick replies: {res['quick_replies']}")


def main():
    with httpx.Client() as http:
        uid = uuid.uuid4().hex[:8]
        r = http.post(f"{BASE}/auth/register", json={
            "email": f"evallive_{uid}@example.com", "username": f"evallive_{uid}",
            "display_name": "Eval", "password": "password123", "accept_terms": True})
        r.raise_for_status()
        token = r.json()["access_token"]
        failures = 0
        try:
            for title, turns in SCENARIOS.items():
                print(f"\n=== {title} ===")
                c = Convo(http, token)
                for text in turns:
                    print(f"  → {text}")
                    show(*c.say(text))
                if c.problems:
                    failures += 1
                    print(f"  ✗ {'; '.join(c.problems)}")
                else:
                    print("  ✓ no repeated or budget/mood questions")
        finally:
            http.delete(f"{BASE}/auth/account", headers={"Authorization": f"Bearer {token}"})
        print(f"\n{len(SCENARIOS) - failures}/{len(SCENARIOS)} conversations passed the automatic checks.")


if __name__ == "__main__":
    main()
