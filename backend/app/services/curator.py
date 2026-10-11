"""The AI picks the stops.

The rules gather real candidates for each stop and rank them (budget share, open at
the planned time, distance, how well known, what was asked for). Here, one LLM call
chooses each stop from a short list of those candidates and writes the plan's intro,
a line per stop and a tip, in the same reply. Narration used to be a separate call;
it's folded in here, so a plan costs the router call plus this one.

The model can't invent a place: it answers with shortlist ids, checked here. An
unknown id, a place used twice, or a line that names a place it didn't pick, and that
part falls back to the rule pick (or no line). If the call fails entirely the planner
keeps the rule picks and labels the plan a "quick version".

SeatGeek events never reach this prompt (their terms forbid passing their data to
an AI); the events here come from Ticketmaster and the city's calendar only.
"""
import re

from app.services import costs, llm

SHORTLIST = 8          # candidates per stop shown to the model
MAX_WHY = 280          # characters per stop line

SYSTEM = (
    "You plan an outing by choosing ONE place for each stop from that stop's shortlist. Every "
    "place listed is real, open at that time and allowed, and you may only answer with their ids.\n"
    "Choose the best outing for this group: it fits the vibe and the budget (the picks' ~$ per person "
    "times the number of people should stay within the total budget), stops are close to each other, "
    "distinctive local or well-known places beat generic chains, it has variety (not the same kind of "
    "place twice), and it includes what they asked for.\n"
    "Then write, using only the facts listed (never invent dishes, prices, hours or features):\n"
    "- intro: 2-3 warm sentences setting the scene for this group; name no place you didn't pick.\n"
    "- why: for each stop, 1-2 sentences on what it is and why it fits. Free stops (parks, squares) can "
    "say they're free.\n"
    "- tip: one practical closing line.\n"
    'Reply with JSON only: {"stops": [{"id": "1a", "why": "..."}, ...], "intro": "...", "tip": "..."} '
    "with exactly one entry per stop, in order."
)


def _ids(i: int, n: int) -> list[str]:
    return [f"{i + 1}{chr(97 + j)}" for j in range(n)]


def shortlists(stops, pools, exclude, rank) -> list[list[dict]]:
    """For each stop, its best candidates (the rule pick always among them), without
    places that other stops already use or that the caller excludes."""
    taken = {s["name"].lower() for s in stops}
    out = []
    for s in stops:
        pool = pools.get(s["name"].lower())
        if not pool or s.get("requested"):
            out.append([])
            continue
        cands, frm, was = pool
        own = s["name"].lower()
        ranked, seen = [], set()
        for c in sorted(cands, key=lambda c: rank(c, s["slot"], frm), reverse=True):
            name = (c.get("name") or "").lower()
            if not name or name in seen or name in exclude or (name in taken and name != own):
                continue
            seen.add(name)
            ranked.append(c)
            if len(ranked) >= SHORTLIST:
                break
        if own not in seen:
            ranked = [was] + ranked[:SHORTLIST - 1]
        out.append(ranked)
    return out


def _line(c: dict, slot_key: str, km: float) -> str:
    cats = ", ".join(str(x) for x in (c.get("categories") or [])[:2])
    bits = [c["name"], cats, f"~${costs.estimate(c, slot_key)}/person", f"{km:.1f} km from the start"]
    if (c.get("notability") or 0) >= 3:
        bits.append("well known")
    if c.get("rating"):
        bits.append(f"{c['rating']}★")
    if c.get("chain"):
        bits.append("chain")
    if c.get("term_hit"):
        bits.append("matches what they asked for")
    return " — ".join(b for b in bits if b)


def _clean(text, banned: set[str], limit: int) -> str | None:
    """The model's text, or None if it's empty or names a place it didn't pick."""
    if not isinstance(text, str) or not text.strip():
        return None
    low = text.lower()
    if any(name in low for name in banned):
        return None
    return text.strip()[:limit]


def _norm_id(v) -> str:
    return re.sub(r"[^a-z0-9]", "", str(v or "").lower()).lstrip("s")


def choose(stops, lists, *, context: str, times: list[str], distance, labels: dict,
           events: list[dict] | None = None, timeout: float = 15.0) -> dict | None:
    """One call: {"picks": [(candidate or None, why or None) per stop], "intro", "tip"},
    or None when the model is unavailable, fails, or picks nothing valid."""
    if not stops or not any(lists):
        return None
    blocks = []
    for i, (s, lst) in enumerate(zip(stops, lists)):
        head = f"Stop {i + 1} · {times[i]} · {labels.get(s['slot'], s['slot'])}"
        if not lst:            # asked for by name: fixed, not up for choosing
            blocks.append(f"{head} (fixed: {s['name']}; answer with id \"{i + 1}a\")")
            continue
        rows = [f"  {cid}: {_line(c, s['slot'], distance(c))}" for cid, c in zip(_ids(i, len(lst)), lst)]
        blocks.append(head + "\n" + "\n".join(rows))
    ev = ""
    if events:
        ev = "\nEvents nearby (only for the tip, by exact name): " + "; ".join(
            f"{e['name']} at {e.get('venue')}" for e in events[:3] if e.get("name"))
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"{context}\n\n" + "\n".join(blocks) + ev}]
    data = llm.chat_json(msgs, temperature=0.6, timeout=timeout)
    if not isinstance(data, dict) or not isinstance(data.get("stops"), list):
        return None

    all_names = {(c.get("name") or "").lower() for lst in lists for c in lst}
    picks, chosen = [], set()
    for i, lst in enumerate(lists):
        entry = data["stops"][i] if i < len(data["stops"]) and isinstance(data["stops"][i], dict) else {}
        c = dict(zip(_ids(i, len(lst)), lst)).get(_norm_id(entry.get("id"))) if lst else None
        if c is not None and c["name"].lower() in chosen:
            c = None                                         # the same place twice: keep the rule pick
        name = (c or stops[i])["name"].lower()
        chosen.add(name)
        why = _clean(entry.get("why"), {n for n in all_names - {name} if len(n) >= 4}, MAX_WHY)
        if lst and c is None:
            why = None                                       # it described a place we're not using
        picks.append((c, why))
    if not any(c for c, _ in picks):
        return None
    banned = {n for n in all_names - chosen if len(n) >= 4}
    return {"picks": picks, "intro": _clean(data.get("intro"), banned, 600),
            "tip": _clean(data.get("tip"), banned, 240)}
