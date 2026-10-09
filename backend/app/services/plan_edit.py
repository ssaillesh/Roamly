"""Editing an existing plan: "swap the bar", "drop dessert", "add bowling".

A follow-up that changes part of a plan must not rebuild the whole thing. The
ops here change only the stops they name; everything else (the venues, their
times and descriptions) stays exactly as the user saw it. A replacement stop is
searched between its neighbours, so the route stays tight.

Ops (from services/plan_intent.py):
  {"op": "swap",   "stop": 2 | None, "slot": "dinner" | None, "wish": "sushi"}
  {"op": "remove", "stop": 3 | None, "slot": "dessert" | None}
  {"op": "add",    "wish": "bowling", "slot": None, "position": 2 | None}
  {"op": "move",   "stop": 3, "to": 1}
`stop` is 1-based, as the user sees the stops numbered; it may also be "last".
"""
import copy
import random
import re
from dataclasses import dataclass, field

from app.services.planner import (
    SLOT_ORDER, SLOT_SPECS, TRANSPORT, VIBE_PLANS, DEFAULT_VIBE, PLAN_TIME_BUDGET, _POOL,
    _est_cost, _gather, _pick, _result, allowed, cuisine_term, route_for, route_km, stop_from,
)

# Words that name a kind of stop → that stop's slot. Checked in order.
_SLOT_WORDS = [
    ("dessert", ["dessert", "ice cream", "gelato", "sweet", "bakery", "cake", "donut", "boba",
                 "bubble tea", "crepe", "waffle", "churro"]),
    ("cafe", ["coffee", "cafe", "café", "latte", "matcha"]),
    ("lunch", ["lunch", "brunch"]),
    ("dinner", ["dinner", "restaurant", "food", "eat", "meal", "supper"]),
    ("leisure", ["bar", "drinks", "drink", "pub", "club", "lounge", "cocktail", "nightlife",
                 "hookah", "karaoke", "comedy", "live music", "rooftop", "speakeasy", "wine"]),
    ("scenic", ["park", "view", "scenic", "beach", "waterfront", "garden", "lookout", "trail", "sunset"]),
    ("activity", ["activity", "museum", "gallery", "bowling", "arcade", "escape", "aquarium", "zoo",
                  "mini golf", "minigolf", "karting", "go kart", "laser tag", "climbing", "axe",
                  "trampoline", "paintball", "cinema", "movie", "show", "theatre", "theater"]),
]
# Slots the user treats as the same thing ("the bar" may be a leisure or a drinks stop).
_SAME = {"dinner": {"dinner", "lunch"}, "lunch": {"lunch", "dinner"},
         "leisure": {"leisure", "drinks"}, "drinks": {"drinks", "leisure"}}
_ORDINALS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3,
             "fourth": 4, "4th": 4, "fifth": 5, "5th": 5, "sixth": 6, "6th": 6}
_CHEAPER = re.compile(r"\b(cheap(er)?|less expensive|budget|affordable|inexpensive)\b")
_FANCIER = re.compile(r"\b(fanc(y|ier)|nicer|upscale|classier|high[- ]end|luxur\w*|splurge)\b")


@dataclass
class EditContext:
    """What a re-pick needs to know about the request (the same knobs as build_plan)."""
    vibe: str = DEFAULT_VIBE
    party_size: int = 2
    transport: str = "any"
    group_type: str | None = None
    dietary: str = ""
    interests: str = ""
    avoid: str = ""
    no_alcohol: bool = False
    radius: int | None = None
    taste: dict = field(default_factory=dict)
    seed: int | None = None


def slot_for_text(text: str, default: str | None = None) -> str | None:
    """The kind of stop a phrase is about ("sushi" → dinner, "bowling" → activity)."""
    t = (text or "").lower()
    if not t:
        return default
    if cuisine_term(t, ""):
        return default if default in ("dinner", "lunch") else "dinner"
    for slot, words in _SLOT_WORDS:
        if any(re.search(rf"\b{re.escape(w)}", t) for w in words):
            return slot
    return default


def find_stop(stops: list[dict], op: dict) -> int | None:
    """0-based index of the stop an op means: its number, its name, or its kind."""
    n = op.get("stop")
    if isinstance(n, str):
        key = n.strip().lower()
        n = (int(key) if key.isdigit() else len(stops) if key in ("last", "final", "end")
             else _ORDINALS.get(key))
    if isinstance(n, int) and 1 <= n <= len(stops):
        return n - 1
    name = (op.get("name") or "").lower().strip()
    if name:
        for i, s in enumerate(stops):
            if name in s["name"].lower() or s["name"].lower() in name:
                return i
    slot = op.get("slot")
    if slot:
        family = _SAME.get(slot, {slot})
        for i, s in enumerate(stops):
            if s.get("slot") in family:
                return i
    return None


def _between(stops: list[dict], i: int, plan: dict) -> tuple[float, float]:
    """Where to search for a stop placed at index i (the stop there is excluded):
    the middle of its neighbours, else the plan's centre."""
    near = [s for s in (stops[i - 1] if i > 0 else None, stops[i] if i < len(stops) else None) if s]
    if near:
        return (sum(s["lat"] for s in near) / len(near), sum(s["lng"] for s in near) / len(near))
    c = plan.get("center") or {}
    return (c.get("lat"), c.get("lng"))


def _repick(slot_key, anchor, wish, ctx: EditContext, exclude: set[str], cost_cap=None, fancy=None):
    """Best real venue for one stop, or None. `wish` steers the search ("sushi")."""
    tconf = TRANSPORT.get(ctx.transport, TRANSPORT["any"])
    radius = ctx.radius or tconf["radius"]
    price_pref = VIBE_PLANS.get(ctx.vibe, VIBE_PLANS[DEFAULT_VIBE])["price"]
    term, cats = route_for(slot_key, interests=wish or ctx.interests, dietary=ctx.dietary,
                           group_type=ctx.group_type, vibe=ctx.vibe, no_alcohol=ctx.no_alcohol)
    fut = _POOL.submit(_gather, anchor, slot_key, price_pref, radius, ctx.dietary, term, cats)
    cands = [c for c in _result(fut, [], timeout=PLAN_TIME_BUDGET) if allowed(c, ctx.avoid, ctx.no_alcohol)]
    if cost_cap is not None:
        cands = [c for c in cands if _est_cost(c, SLOT_SPECS[slot_key]) < cost_cap] or cands
    rng = random.Random(ctx.seed) if ctx.seed is not None else random
    return _pick(cands, exclude, anchor, tconf["penalty"],
                 fancy=ctx.vibe == "extravagant" if fancy is None else fancy,
                 apply_fun=slot_key in ("activity", "leisure"),
                 interests=", ".join(filter(None, [wish, ctx.interests])), rng=rng, taste=ctx.taste)


def _what(slot_key):
    return SLOT_SPECS[slot_key]["label"].lower()


def _remove(stops, op, plan, ctx, exclude, changed, problems):
    i = find_stop(stops, op)
    if i is None:
        problems.append("I couldn't tell which stop to remove.")
        return
    changed.append(f"Removed {stops.pop(i)['name']}.")


def _swap(stops, op, plan, ctx, exclude, changed, problems):
    wish = (op.get("wish") or "").strip()
    i = find_stop(stops, op)
    if i is None:
        if slot_for_text(wish) or op.get("slot"):     # "swap dinner for sushi" with no dinner yet
            _add(stops, {**op, "slot": op.get("slot") or slot_for_text(wish)}, plan, ctx,
                 exclude, changed, problems)
        else:
            problems.append("I couldn't tell which stop to change.")
        return
    old = stops[i]
    cheaper, fancier = bool(_CHEAPER.search(wish.lower())), bool(_FANCIER.search(wish.lower()))
    taste_wish = "" if (cheaper or fancier) else wish
    slot = slot_for_text(taste_wish, old["slot"]) if taste_wish else old["slot"]
    exclude.add(old["name"].lower())
    rest = stops[:i] + stops[i + 1:]
    pick = _repick(slot, _between(rest, i, plan), taste_wish, ctx, exclude,
                   cost_cap=old.get("est_cost") if cheaper else None, fancy=True if fancier else None)
    if not pick:
        problems.append(f"I couldn't find another {taste_wish or _what(slot)} nearby, so {old['name']} stays.")
        return
    new = stop_from(pick, slot, **({"time": old["time"]} if old.get("time") else {}))
    stops[i] = new
    exclude.add(new["name"].lower())
    changed.append(f"Swapped {old['name']} for {new['name']}.")


def _add(stops, op, plan, ctx, exclude, changed, problems):
    wish = (op.get("wish") or "").strip()
    slot = op.get("slot") if op.get("slot") in SLOT_SPECS else slot_for_text(wish, "activity")
    pos = op.get("position")
    if isinstance(pos, int) and 1 <= pos <= len(stops) + 1:
        i = pos - 1
    else:   # the day's natural order: activity before dinner, drinks after, dessert last
        i = next((k for k, s in enumerate(stops)
                  if SLOT_ORDER.get(s.get("slot"), 5) > SLOT_ORDER.get(slot, 5)), len(stops))
    pick = _repick(slot, _between(stops, i, plan), wish, ctx, exclude)
    if not pick:
        problems.append(f"I couldn't find {wish or _what(slot)} nearby to add.")
        return
    new = stop_from(pick, slot)
    stops.insert(i, new)
    exclude.add(new["name"].lower())
    changed.append(f"Added {new['name']}.")


def _move(stops, op, plan, ctx, exclude, changed, problems):
    i, to = find_stop(stops, op), op.get("to")
    if isinstance(to, str) and to.strip().isdigit():
        to = int(to)
    if i is None or not isinstance(to, int) or not 1 <= to <= len(stops):
        problems.append("I couldn't tell how to reorder the stops.")
        return
    s = stops.pop(i)
    stops.insert(to - 1, s)
    changed.append(f"Moved {s['name']} to stop {to}.")


_OPS = {"remove": _remove, "swap": _swap, "add": _add, "move": _move}


def apply_edits(plan: dict, ops: list[dict], ctx: EditContext) -> tuple[dict, list[str], list[str]]:
    """Apply ops to a copy of `plan`. Returns (plan, what changed, what couldn't
    be done), both as short sentences for the reply."""
    plan = copy.deepcopy(plan)
    stops: list[dict] = plan.get("stops") or []
    changed, problems = [], []
    # Never re-suggest a venue already in the plan, nor one the user just swapped out.
    exclude = {s["name"].lower() for s in stops}
    for op in ops or []:
        fn = _OPS.get((op.get("op") or "").lower())
        if fn:
            fn(stops, op, plan, ctx, exclude, changed, problems)
        else:
            problems.append("I didn't understand part of that change.")

    plan["stops"] = stops
    plan["estimated_cost"] = sum(s.get("est_cost") or 0 for s in stops) * max(1, ctx.party_size)
    plan["walk_km"] = route_km(stops)
    if changed:
        # The old intro/tip may name venues that are gone; say what changed instead.
        plan["intro"] = " ".join(changed)
        gone = [m.group(1) for c in changed if (m := re.match(r"(?:Removed|Swapped) (.+?)(?: for .+)?\.$", c))]
        if plan.get("tip") and any(g in plan["tip"] for g in gone):
            plan["tip"] = None
    return plan, changed, problems
