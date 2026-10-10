"""What does this message want? Reads the whole conversation plus the plan on
screen and decides between a new plan, an edit to the current plan, a what's-on
lookup, or a question about the plan, and pulls out the request's preferences.

One LLM call (`route`) when the model is available; `route_rules` is the
keyword fallback when it isn't, covering the common phrasings.
"""
import re

from app.services import llm
from app.services.plan_edit import find_stop, slot_for_text

INTENTS = ("new_plan", "edit_plan", "events", "ask_about_plan", "chitchat")
PREF_KEYS = ("budget", "budget_scope", "vibe", "party_size", "days", "time_of_day", "transport",
             "group_type", "dietary", "location", "interests", "avoid", "requested_venues", "radius_km")

SYSTEM = """You route messages for an outing-planner app. Read the conversation, the
plan currently on screen (if any) and the current settings, then output ONLY JSON:
{
 "intent": "new_plan" | "edit_plan" | "events" | "ask_about_plan" | "chitchat",
 "ops": [ ... ],            // only for edit_plan
 "prefs": { ... },          // what the plan should respect
 "answer": string | null    // only for ask_about_plan / chitchat
}
intent:
- edit_plan: a plan is on screen and the LATEST message changes specific stops of it
  (swap/replace one, remove one, add one, reorder). Keep everything else.
- new_plan: no plan yet, or the latest message asks for a different/whole new plan, or
  changes something about the WHOLE plan (budget, mood, day/time, area, group size).
- events: asks what's on / happening / events / shows nearby (not a plan).
- ask_about_plan: a question about the plan on screen; answer it from the plan only.
- chitchat: anything else; answer in one friendly sentence and invite a request.
ops (edit_plan), stops are numbered as shown, 1-based:
  {"op":"swap","stop":N,"wish":"what they want instead, e.g. sushi / cheaper / a rooftop bar"}
  {"op":"remove","stop":N}
  {"op":"add","wish":"what to add, e.g. bowling","position":N or null}
  {"op":"move","stop":N,"to":M}
prefs (null/"" when not stated — never invent; later messages win over earlier ones):
  budget: the dollar amount stated (a range → the higher end), or null
  budget_scope: "per_person" if they said each/pp/per person/a head; "total" if they said
    total/altogether/for everyone/in total; else null. An answer to "total or each?" counts.
  vibe: chill|romantic|adventurous|extravagant|night_out or null
  party_size: integer or null, counting everyone going ("family with two kids" = 4,
    "me and 3 friends" = 4); group_type: date|friends|family|solo or null
  days: integer or null; time_of_day: morning|afternoon|evening|night or null
  transport: walk|transit|car or null; radius_km: number or null
  location: a place they name to plan in, or null
  interests: things they want ("sushi, rooftop bar"), or ""
  avoid: things they do NOT want ("no bars" → "bars"); never leave a rejected thing in interests
  dietary: food rules ("vegan"), or ""
  requested_venues: specific venue names they ask to include, [] if none
For new_plan, prefs = everything said in the conversation that still applies; if the latest
message starts a separate plan (another group, "plan B", "start over"), only what applies to it.
For edit_plan, prefs need only avoid/dietary/interests if the message changes them."""


def _plan_lines(plan: dict | None) -> str:
    if not plan or not plan.get("stops"):
        return "(no plan on screen)"
    return "\n".join(
        f"{i + 1}. {s.get('label', s.get('slot', 'stop'))}: {s.get('name')}"
        + (f" at {s['time']}" if s.get("time") else "")
        + (f" (~${s['est_cost']}/person)" if s.get("est_cost") is not None else "")
        for i, s in enumerate(plan["stops"]))


def _settings_line(settings: dict | None) -> str:
    s = settings or {}
    bits = [f"{k}={s[k]}" for k in ("budget", "vibe", "group_type", "party_size", "time_of_day",
                                     "transport", "radius_km", "location", "interests", "avoid",
                                     "dietary") if s.get(k) not in (None, "")]
    return ", ".join(bits) or "none"


def route(messages: list[dict], current_plan: dict | None, settings: dict | None) -> dict | None:
    """LLM routing; None when the model is unavailable or answers badly (the
    caller then uses route_rules)."""
    if not llm.available():
        return None
    convo = "\n".join(f"{m['role']}: {m['content']}" for m in messages[-16:])
    data = llm.chat_json([
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content":
            f"CURRENT SETTINGS: {_settings_line(settings)}\n"
            f"PLAN ON SCREEN:\n{_plan_lines(current_plan)}\n"
            f"CONVERSATION:\n{convo}"},
    ], timeout=15.0)
    return clean(data, current_plan)


def clean(data, current_plan) -> dict | None:
    """Validate a router answer; None if unusable."""
    if not isinstance(data, dict) or data.get("intent") not in INTENTS:
        return None
    intent = data["intent"]
    ops = [o for o in (data.get("ops") or []) if isinstance(o, dict) and o.get("op")]
    if intent == "edit_plan" and not (current_plan and current_plan.get("stops") and ops):
        intent = "new_plan"                 # nothing to edit: plan instead of failing
    prefs = data.get("prefs") if isinstance(data.get("prefs"), dict) else {}
    prefs = {k: prefs[k] for k in PREF_KEYS if k in prefs}
    if prefs.get("budget_scope") not in ("total", "per_person"):
        prefs.pop("budget_scope", None)
    answer = data.get("answer") if isinstance(data.get("answer"), str) else None
    return {"intent": intent, "ops": ops if intent == "edit_plan" else [], "prefs": prefs,
            "answer": answer, "source": "llm"}


# ---- keyword fallback ----------------------------------------------------------

_NEW_RE = re.compile(r"\b(new plan|fresh plan|start over|from scratch|plan b|another plan|different plan"
                     r"|something (?:else|different) entirely|completely different)\b")
_REMOVE_RE = re.compile(r"\b(remove|drop|skip|cut|lose|get rid of|ditch|take out|no more)\b")
_SWAP_RE = re.compile(r"\b(swap|replace|change|switch|instead of|different|another|other than)\b")
_ADD_RE = re.compile(r"\b(add|include|throw in|squeeze in|also (?:do|go|hit|visit))\b")
_WISH_RE = re.compile(r"\b(?:for|with|to|into|instead)\s+(?:a |an |some |the )?(.+)$")
_ADD_WISH_RE = re.compile(r"\b(?:add|include|throw in|squeeze in|also (?:do|go to|hit|visit))\s+"
                          r"(?:a |an |some |the |in )?(.+?)(?:\s+(?:stop|too|as well|please))?[.!?]*$")
_ORD_RE = re.compile(r"\b(first|second|third|fourth|fifth|sixth|last|final|1st|2nd|3rd|4th|5th|6th)\b"
                     r"|\bstop\s*(?:#|number\s*)?(\d)\b")


def _stop_ref(text: str, plan: dict) -> dict | None:
    """An op target from the message: an ordinal, a venue name, or a kind of stop."""
    stops = plan.get("stops") or []
    m = _ORD_RE.search(text)
    if m:
        return {"stop": m.group(2) or m.group(1)}
    for s in stops:
        words = [w for w in re.findall(r"[a-z0-9']{4,}", s["name"].lower())
                 if w not in ("the", "and", "restaurant", "cafe", "park", "bar")]
        if words and any(w in text for w in words):
            return {"name": s["name"]}
    head = re.split(r"\b(?:for|with|to|into|instead)\b", text)[0]   # "swap the bar for X" → "the bar"
    slot = slot_for_text(head)
    if slot and find_stop(stops, {"slot": slot}) is not None:
        return {"slot": slot}
    return None


def route_rules(text: str, current_plan: dict | None, events: bool = False) -> dict:
    """Keyword routing over the latest user turn (lower-cased)."""
    text = (text or "").lower().strip()
    base = {"ops": [], "prefs": {}, "answer": None, "source": "rules"}
    if events:
        return {**base, "intent": "events"}
    if not (current_plan and current_plan.get("stops")) or _NEW_RE.search(text):
        return {**base, "intent": "new_plan"}
    ref = _stop_ref(text, current_plan)
    if _ADD_RE.search(text):
        m = _ADD_WISH_RE.search(text)
        wish = (m.group(1) if m else "").strip()
        if wish:
            return {**base, "intent": "edit_plan", "ops": [{"op": "add", "wish": wish}]}
    if ref and _REMOVE_RE.search(text):
        return {**base, "intent": "edit_plan", "ops": [{"op": "remove", **ref}]}
    if ref and (_SWAP_RE.search(text) or re.search(r"\b(cheaper|fancier|nicer|closer)\b", text)):
        m = _WISH_RE.search(text.split("instead of")[0] if "instead of" in text else text)
        wish = (m.group(1) if m else "").strip(" .!?")
        if re.search(r"\b(cheaper|fancier|nicer)\b", text) and not wish:
            wish = re.search(r"\b(cheaper|fancier|nicer)\b", text).group(1)
        return {**base, "intent": "edit_plan", "ops": [{"op": "swap", **ref, "wish": wish}]}
    return {**base, "intent": "new_plan"}
