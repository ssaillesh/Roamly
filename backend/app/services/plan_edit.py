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
  {"op": "set_start", "time": "7:30 PM"}        every stop re-timed from the new start
  {"op": "set_party", "party_size": 6}          the cost re-counted for that many
  {"op": "set_length", "stops": 3}              shorter: the least needed stops go; longer: more are added
  {"op": "add_event", "event": {...}}           an event from the list beside the plan, at its own time
`stop` is 1-based, as the user sees the stops numbered; it may also be "last".
"""
import copy
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.services import costs
from app.services.planner import (
    LOCAL_TZ, SLOT_ORDER, SLOT_SPECS, TRANSPORT, VIBE_PLANS, DEFAULT_VIBE, PLAN_TIME_BUDGET, _POOL,
    _est_cost, _gather, _pick, _result, _template_intro, allowed, cuisine_term, parse_start, retime, route_for,
    route_km, schedule, stop_from, too_casual,
)

# Words that name a kind of stop → that stop's slot. Checked in order.
_SLOT_WORDS = [
    ("dessert", ["dessert", "ice cream", "gelato", "sweet", "bakery", "cake", "donut", "boba",
                 "bubble tea", "crepe", "waffle", "churro"]),
    ("cafe", ["coffee", "cafe", "café", "latte", "matcha"]),
    ("lunch", ["lunch", "brunch"]),
    ("dinner", ["dinner", "restaurant", "food", "eat", "meal", "supper"]),
    # "a late-night bite", "a snack": something small to eat at the end, not a full meal
    # (after the dinner words, so "a bite to eat" / "a bite for dinner" stays dinner).
    ("dessert", ["bite", "snack", "nibble"]),
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
# "Swap it for something similar / something else": no real wish, just "another one".
_NO_WISH = re.compile(r"^(?:(?:for|with|to)\s+)?(?:some(?:thing|where)|any(?:thing|where)|another(?: one)?|a different one|one)"
                      r"(?:\s+(?:similar|else|different|new|like it|like that))?$", re.I)


def real_wish(text: str) -> str:
    """The wish in a swap or add, or "" when it's only "something similar" / "another one"."""
    t = " ".join((text or "").split()).strip(" .!")
    return "" if _NO_WISH.match(t) else t


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


def _when(plan: dict, slots: list[str], i: int):
    """When the stop at index i (of `slots`, the plan's stops after the change) happens."""
    start = parse_start(plan.get("start"))
    return schedule(slots, start)[i] if start else None


def _share(plan: dict, others: list[dict], ctx: EditContext) -> float | None:
    """What's left of the budget per person for one stop, after the other stops."""
    budget = plan.get("budget") or 0
    if budget <= 0:
        return None
    return max(0.0, budget / max(1, ctx.party_size) - sum(s.get("est_cost") or 0 for s in others))


def _repick(slot_key, anchor, wish, ctx: EditContext, exclude: set[str], cost_cap=None, fancy=None, at=None,
            allowance=None):
    """Best real venue for one stop, or None. `wish` steers the search ("sushi");
    `at` = when it happens (places closed then are left out); `allowance` = its
    share of the budget per person."""
    tconf = TRANSPORT.get(ctx.transport, TRANSPORT["any"])
    radius = ctx.radius or tconf["radius"]
    price_pref = VIBE_PLANS.get(ctx.vibe, VIBE_PLANS[DEFAULT_VIBE])["price"]
    term, cats = route_for(slot_key, interests=wish or ctx.interests, dietary=ctx.dietary,
                           group_type=ctx.group_type, vibe=ctx.vibe, no_alcohol=ctx.no_alcohol)
    fut = _POOL.submit(_gather, anchor, slot_key, price_pref, radius, ctx.dietary, term, cats, at=at)
    cands = [c for c in _result(fut, [], timeout=PLAN_TIME_BUDGET) if allowed(c, ctx.avoid, ctx.no_alcohol)
             and not too_casual(c, slot_key, ctx.vibe, ctx.group_type, wish or ctx.interests)]
    if cost_cap is not None:
        cands = [c for c in cands if _est_cost(c, slot_key) < cost_cap] or cands
    rng = random.Random(ctx.seed) if ctx.seed is not None else random
    return _pick(cands, exclude, anchor, tconf["penalty"],
                 fancy=ctx.vibe == "extravagant" if fancy is None else fancy,
                 apply_fun=slot_key in ("activity", "leisure"),
                 interests=", ".join(filter(None, [wish, ctx.interests])), rng=rng, taste=ctx.taste,
                 slot_key=slot_key, allowance=allowance)


def _what(slot_key):
    return SLOT_SPECS[slot_key]["label"].lower()


def _remove(stops, op, plan, ctx, exclude, changed, problems):
    i = find_stop(stops, op)
    if i is None:
        problems.append("I couldn't tell which stop to remove.")
        return
    changed.append(f"Removed {stops.pop(i)['name']}.")


def _swap(stops, op, plan, ctx, exclude, changed, problems):
    wish = real_wish(op.get("wish") or "")
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
    slots = [s["slot"] for s in stops]
    slots[i] = slot
    pick = _repick(slot, _between(rest, i, plan), taste_wish, ctx, exclude,
                   cost_cap=old.get("est_cost") if cheaper else None, fancy=True if fancier else None,
                   at=_when(plan, slots, i), allowance=None if fancier else _share(plan, rest, ctx))
    if not pick:
        problems.append(f"I couldn't find another {taste_wish or _what(slot)} nearby, so {old['name']} stays.")
        return
    new = stop_from(pick, slot, **({"time": old["time"]} if old.get("time") else {}))
    stops[i] = new
    exclude.add(new["name"].lower())
    changed.append(f"Swapped {old['name']} for {new['name']}.")


def _add(stops, op, plan, ctx, exclude, changed, problems):
    wish = real_wish(op.get("wish") or "")
    slot = op.get("slot") if op.get("slot") in SLOT_SPECS else slot_for_text(wish, "activity")
    pos = op.get("position")
    if isinstance(pos, int) and 1 <= pos <= len(stops) + 1:
        i = pos - 1
    else:   # the day's natural order: activity before dinner, drinks after, dessert last
        i = next((k for k, s in enumerate(stops)
                  if SLOT_ORDER.get(s.get("slot"), 5) > SLOT_ORDER.get(slot, 5)), len(stops))
    slots = [s["slot"] for s in stops]
    slots.insert(i, slot)
    pick = _repick(slot, _between(stops, i, plan), wish, ctx, exclude, at=_when(plan, slots, i),
                   allowance=_share(plan, stops, ctx))
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


_CLOCK = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*([ap])?\.?\s*m?\.?\s*$", re.I)


def parse_clock(text, base: datetime | None = None) -> datetime | None:
    """'7:30 PM' / '19:30' / '7pm' → that time on the plan's day (today if it has none).
    A bare '7' or '11:30' takes its half of the day from the plan: in an evening plan
    (from 3 PM on, or no plan) it's PM; in a daytime plan, AM."""
    m = _CLOCK.match(str(text or ""))
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if h > 23 or mi > 59 or (ap and not 1 <= h <= 12):
        return None
    if ap == "p" and h != 12:
        h += 12
    elif ap == "a" and h == 12:
        h = 0
    elif not ap and 1 <= h <= 11 and (base is None or base.hour >= 15):
        h += 12
    day = base or datetime.now(LOCAL_TZ)
    return day.replace(hour=h, minute=mi, second=0, microsecond=0)


def _set_start(stops, op, plan, ctx, exclude, changed, problems):
    base = parse_start(plan.get("start"))
    t = parse_clock(op.get("time"), base)
    if not t:
        problems.append("I couldn't read that start time — try something like 7:30 PM.")
        return
    plan["start"] = t.isoformat(timespec="minutes")
    changed.append(f"It now starts at {t.strftime('%I:%M %p').lstrip('0')}.")


def _set_party(stops, op, plan, ctx, exclude, changed, problems):
    try:
        n = int(op.get("party_size"))
    except (TypeError, ValueError):
        n = 0
    if not 1 <= n <= 20:
        problems.append("Tell me how many are going (1 to 20).")
        return
    plan["party_size"] = ctx.party_size = n
    changed.append("It's now for just you." if n == 1 else f"It's now for {n}.")


# Which stops go first when a plan is made shorter (the evening's heart, dinner, stays).
_DROP_ORDER = ["dessert", "cafe", "scenic", "drinks", "leisure", "activity", "lunch", "dinner", "event"]
# What a longer plan gets next, in this order, if it hasn't got one.
_GROW_ORDER = ["dessert", "leisure", "activity", "scenic", "cafe"]


def _set_length(stops, op, plan, ctx, exclude, changed, problems):
    try:
        want = int(op.get("stops"))
    except (TypeError, ValueError):
        want = 0
    if not 2 <= want <= 8:
        problems.append("A plan can have 2 to 8 stops.")
        return
    while len(stops) > want:
        drop = next((i for slot in _DROP_ORDER for i in range(len(stops) - 1, -1, -1)
                     if stops[i].get("slot") == slot and not stops[i].get("requested")), None)
        if drop is None:
            break
        changed.append(f"Removed {stops.pop(drop)['name']}.")
    have = {s.get("slot") for s in stops}
    for slot in [x for x in _GROW_ORDER if x not in have]:
        if len(stops) >= want:
            break
        _add(stops, {"op": "add", "slot": slot}, plan, ctx, exclude, changed, problems)
    if len(stops) < want and not problems:
        problems.append(f"I couldn't find more stops nearby, so it has {len(stops)}.")


def _add_event(stops, op, plan, ctx, exclude, changed, problems):
    ev = op.get("event") if isinstance(op.get("event"), dict) else {}
    name, lat, lng = str(ev.get("name") or "").strip()[:120], ev.get("lat"), ev.get("lng")
    if not name or not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
        problems.append("That event has no place on the map, so I can't add it.")
        return
    if name.lower() in exclude:
        problems.append(f"{name} is already in the plan.")
        return
    price = 0 if ev.get("free") else ev.get("price_min")
    cost = int(round(price)) if isinstance(price, (int, float)) and price >= 0 else \
        costs.estimate({"categories": [ev.get("category") or "event"]}, "event")
    url = ev.get("url") if isinstance(ev.get("url"), str) and ev["url"].startswith(("https://", "http://")) else None
    img = ev.get("image") if isinstance(ev.get("image"), str) and ev["image"].startswith("https://") else None
    retime(plan)                         # the times as they stand, to place it among them
    when = parse_clock(ev.get("time"), parse_start(plan.get("start")))
    new = {**stop_from({"name": name, "lat": lat, "lng": lng, "address": ev.get("venue"), "image": img,
                        "url": url, "source": ev.get("source") or "ticketmaster",
                        "categories": [c for c in [ev.get("category")] if c]}, "event"),
           "est_cost": cost, "label": "Live event"}
    if when:   # at its own time: in the plan where that time falls
        times = [parse_clock(s.get("time"), when) for s in stops]
        times = [t + timedelta(days=1) if t and when and t < when - timedelta(hours=6) else t for t in times]
        i = next((k for k, t in enumerate(times) if t and t > when), len(stops))
        new["time"] = when.strftime("%I:%M %p").lstrip("0")
    else:
        i = len(stops)
    stops.insert(i, new)
    exclude.add(name.lower())
    changed.append(f"Added {name}.")


_OPS = {"remove": _remove, "swap": _swap, "add": _add, "move": _move, "set_start": _set_start,
        "set_party": _set_party, "set_length": _set_length, "add_event": _add_event}


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
    if changed:
        retime(plan)   # later stops shift when one is added, removed or moved
    plan["estimated_cost"] = sum(s.get("est_cost") or 0 for s in stops) * max(1, ctx.party_size)
    plan["walk_km"] = route_km(stops)
    if changed:
        # The intro and tip stay as the user read them, unless they name a place that's
        # gone; the reply message says what changed.
        gone = [m.group(1) for c in changed if (m := re.match(r"(?:Removed|Swapped) (.+?)(?: for .+)?\.$", c))]
        if not plan.get("intro") or any(g in plan["intro"] for g in gone):
            plan["intro"] = _template_intro(plan)
        if plan.get("tip") and any(g in plan["tip"] for g in gone):
            plan["tip"] = None
    return plan, changed, problems
