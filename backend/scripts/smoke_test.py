"""Live end-to-end smoke test against a running Roamly API (default :8001).

Exercises what the web app actually uses: register -> taste survey (autosave +
complete) -> one-tap structured plan -> free-text plan -> password-reset request
(must never leak a token) -> account deletion. Hits real venue/geocoding services,
so a plan may honestly report that the free venue search is overloaded.
"""
import sys
import random

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8001/api/v1"

PROFILE = {
    "crew": "partner", "time": "evening", "transport": "walk", "distance": "medium",
    "energy": "balanced", "crowds": "depends", "novelty": "mix", "pace": "steady",
    "diet": [], "cuisines": ["japanese", "italian"], "spend": "2",
    "loves": ["live_music", "games"], "nopes": [], "drinks": "sometimes",
}


def h(t):
    return {"Authorization": f"Bearer {t}"}


def plan_ok(r):
    """An itinerary, or an honest 'venue search is overloaded' message."""
    d = r.json()
    if d["type"] == "itinerary":
        return True, f"{len((d.get('plan') or {}).get('stops') or [])} stops"
    return "overloaded" in d.get("message", ""), d.get("message", "")[:90]


def main():
    c = httpx.Client(timeout=90)
    u = f"smoke_{random.randint(1000, 999999)}"
    email = f"{u}@example.com"
    r = c.post(f"{BASE}/auth/register", json={
        "email": email, "username": u, "display_name": "Smoke", "password": "password123"})
    r.raise_for_status()
    token = r.json()["access_token"]
    print(f"[1] registered @{u}")

    qs = c.get(f"{BASE}/users/me/profile/questions").json()["questions"]
    c.put(f"{BASE}/users/me/profile", headers=h(token), json={"answers": {"crew": "partner"}})
    p = c.put(f"{BASE}/users/me/profile", headers=h(token), json={"answers": PROFILE}).json()
    assert p["completed"], p
    print(f"[2] taste survey: {len(qs)} questions, completed → {p['archetype']['emoji']} {p['archetype']['title']}")

    r = c.post(f"{BASE}/plan/chat", headers=h(token), json={
        "messages": [], "prefs": {"location": "Toronto"}})
    ok, detail = plan_ok(r)
    assert ok, detail
    print(f"[3] one-tap plan (profile defaults, no AI extraction): {detail}")

    r = c.post(f"{BASE}/plan/chat", headers=h(token), json={
        "messages": [{"role": "user", "content": "$120 chill date night in Toronto"}]})
    ok, detail = plan_ok(r)
    assert ok, detail
    print(f"[4] free-text plan: {detail}")

    r = c.post(f"{BASE}/auth/forgot-password", json={"email": email}).json()
    assert "reset_token" not in r, "reset token leaked in the response!"
    print("[5] forgot-password: generic response, no token leaked")

    assert c.delete(f"{BASE}/auth/account", headers=h(token)).status_code == 204
    assert c.get(f"{BASE}/users/me", headers=h(token)).status_code == 401
    print("[6] account deleted (profile cascades with it)")

    print("\nALL SMOKE CHECKS PASSED ✅")


if __name__ == "__main__":
    main()
