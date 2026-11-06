"""Chat-driven itinerary concierge. Requires a signed-in account — the LLM
planner is a members-only feature (debug is admin-only).

With an LLM key it runs as a sharp concierge that reads the whole conversation,
extracts what it needs, asks one focused follow-up when required, then builds a
real plan optimised for close-together, best-value stops. Without a
key it degrades to a keyword heuristic so the app still works.
"""
import random
import re
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.middleware.auth import get_current_user, require_admin
from app.models import PlanReview, User, TasteProfile
from app.schemas.plan import (
    ChatRequest, ChatResponse, Plan, Option, Section, OptionsResponse, BuildRequest, Event, EditRequest,
    FeedbackRequest, ReviewOut, ReviewRequest,
)
from app.services import foursquare, google_places, llm, osm_places, yelp, taste_profile, usage
from app.services import feedback, plan_intent, venue_index
from app.services.analytics import save_plan_log
from app.services.plan_edit import EditContext, apply_edits
from app.services import events as events_svc
from app.services import live_events
from app.services.geocoding import geocode, geocode_place
from app.services.planner import (
    build_plan, build_trip, VIBE_PLANS, gather_options, option_sections, build_from_selection,
    _WATER_WORDS, _POOL, _result, PLAN_TIME_BUDGET,
)

router = APIRouter(prefix="/plan", tags=["planner"])


@router.get("/debug")
def debug(_admin: User = Depends(require_admin)):
    """Config health for the planner brain. Never returns the API keys."""
    ok, detail = llm.ping()
    return {
        "llm_configured": llm.available(),
        "llm_base_url": settings.llm_base_url,
        "llm_model": settings.llm_model,
        "llm_test_ok": ok,
        "llm_test_detail": detail,
        "yelp_configured": yelp.available(),
        "foursquare_configured": foursquare.available(),
        "google_places_configured": google_places.available(),
        "ticketmaster_configured": events_svc.available(),
    }

VIBE_LABELS = {
    "chill": "Chill", "romantic": "Romantic", "adventurous": "Adventurous",
    "extravagant": "Extravagant", "night_out": "Night out",
}
_VIBE_KEYWORDS = {
    "chill": ["chill", "relax", "casual", "low key", "lowkey", "cozy"],
    "romantic": ["romantic", "date", "romance", "anniversary", "cute"],
    "adventurous": ["adventure", "adventurous", "active", "outdoors", "explore"],
    "extravagant": ["extravagant", "fancy", "luxury", "luxurious", "upscale", "splurge", "baller"],
    "night_out": ["night out", "party", "bars", "clubbing", "nightlife"],
}

def _last_msg(req) -> str:
    return req.messages[-1].content.lower() if req.messages else ""


def _turn(req):
    """The current request: the messages since the last plan/events answer."""
    return req.messages[req.turn_start:] if req.turn_start < len(req.messages) else req.messages[-1:]


def _turn_text(req):
    return " ".join(m.content for m in _turn(req) if m.role == "user").lower()


def _orig_user_text(req):
    return " ".join(m.content for m in _turn(req) if m.role == "user")


def _extract_location(orig_text):
    """Catch 'in/around/near <Capitalized Place>' when the LLM isn't available."""
    m = re.search(r"\b(?:in|around|near|at)\s+([A-Z][\w.'-]+(?:\s+[A-Z][\w.'-]+){0,2})", orig_text)
    return m.group(1).strip() if m else None


# Words that are never a city — vibes, budget talk, planning chatter — so the
# place-name guesser doesn't waste geocode calls (or hit a village named "Fancy").
_LOC_NOISE = set("""
anywhere somewhere nearby here there chill chilling chilled relax relaxed romantic
adventurous adventure extravagant fancy luxury upscale vibe vibes mood budget total
around about range looking look feel feeling something anything plan plans planning
night evening morning afternoon tonight today tomorrow weekend week date dinner lunch
brunch breakfast drinks dessert coffee friends family solo group crew boys girls guys
buddies squad please thanks thank want wants need needs cheap cheaper free from with
without this that these those just like love hate maybe kind sort city town area local
place places nothing whatever surprise random walk walking transit drive driving car
dollars bucks cash money spend spending doing going make makes give gives
""".split())


def _guess_location_coords(req):
    """Last resort when nothing else located the user: pull plausible place names
    straight out of the messages (newest first) and geocode them, restricted to
    real settlements. Handles a bare "Miami" or a lowercase "waterloo"."""
    attempts = 0
    for m in reversed(_turn(req)):
        if m.role != "user" or attempts >= 4:
            continue
        text = m.content

        # "waterloo to toronto" / "between X and Y": geocode both ends; if they're
        # the same region use the midpoint, else trust the better-known second one
        # (a bare "waterloo" alone can resolve to the wrong continent).
        rng = re.search(r"\b([A-Za-z'’.-]{3,})\s+(?:to|and|-|through)\s+([A-Za-z'’.-]{3,})\b", text)
        if rng and not any(w.lower() in _LOC_NOISE for w in rng.groups()):
            a, b = geocode_place(rng.group(1)), geocode_place(rng.group(2))
            attempts += 2
            if a and b:
                dlat, dlng = abs(a[0] - b[0]), abs(a[1] - b[1])
                if dlat < 2 and dlng < 2:   # same region → plan around the middle
                    return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
                return b
            if a or b:
                return a or b

        cands = re.findall(r"\b[A-Z][a-z'’.-]+(?:\s+[A-Z][a-z'’.-]+){0,2}\b", text)  # capitalized first
        cands += re.findall(r"\b[a-z][a-z'’.-]{3,}\b", text)
        seen = set()
        for cand in cands:
            lc = cand.lower()
            if lc in seen or lc in _LOC_NOISE or any(w in _LOC_NOISE for w in lc.split()):
                continue
            seen.add(lc)
            attempts += 1
            coords = geocode_place(cand)
            if coords:
                return coords
            if attempts >= 4:
                break
    return None


def _target_stops(text, time_of_day, default=None):
    """How many stops to fill — a whole day is more; an evening is fewer. The
    taste profile's pace is the default when the request doesn't say."""
    if any(k in text for k in ["full day", "all day", "whole day", "entire day", "day trip",
                                "fill the day", "more the merrier", "packed", "jam pack",
                                "jam-pack", "as much as", "maximize", "lots to do", "the whole thing"]):
        return 6
    if default:
        return int(default)
    return {"morning": 5, "afternoon": 5, "night": 4}.get(time_of_day, 4)


def _extract_days(text):
    if "weekend" in text:
        return 2
    m = re.search(r"(\d+)\s*[- ]?day", text)
    if m:
        return max(1, min(int(m.group(1)), 5))
    for w, n in {"two": 2, "three": 3, "four": 4, "five": 5}.items():
        if re.search(rf"\b{w}[- ]day", text):
            return n
    if re.search(r"\bweek\b|\b7[- ]day\b|\bweek[- ]?long\b", text):
        return 5   # cap trips at 5 days of planning
    return 1


# "no parks", "i don't want to do museums", "skip the clubbing", "without bars"…
_AVOID_RE = re.compile(
    r"(?:\bno\b|\bnot\b|\bskip\b|\bavoid\b|\bwithout\b|\bhate\b|\bdislike\b"
    r"|(?:do not|don'?t|rather not)(?:\s+want)?(?:\s+to)?(?:\s+(?:do|go to|visit|see))?)"
    r"\s+(?:the\s+|any\s+|doing\s+|more\s+)?([a-z][a-z '&-]{2,38}?)"
    r"(?=[,.!?;]|$|\s+(?:please|though|tho|but|can|instead|today|tonight)\b)")

# "no limit / no cap / not sure"-style phrases are not avoids
_AVOID_NOISE = ("limit", "cap", "sure", "idea", "preference", "budget", "alcohol", "drink",
                "later", "earlier", "longer", "more", "less", "problem", "worries", "rush", "matter")


def _extract_avoid(text):
    hits = []
    for m in _AVOID_RE.finditer(text):
        for t in re.split(r"\s+(?:and|or)\s+|,", m.group(1)):
            t = re.sub(r"^(?:into|going to|doing|to do|going)\s+", "", t.strip())
            if t and t.split()[0] not in _AVOID_NOISE:
                hits.append(t)
    return ", ".join(dict.fromkeys(hits))


_NUM_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
              "nine": 9, "ten": 10, "a couple": 2, "couple of": 2}
_PARTY_RE = re.compile(
    r"\b(\d{1,2}|two|three|four|five|six|seven|eight|nine|ten)\s+(?:of us|people|persons|ppl|adults"
    r"|friends|guys|girls|buddies|kids|of my friends)\b"
    r"|\bme and (\d{1,2}|two|three|four|five|six|seven|eight|nine) (?:friends|others|buddies|people)\b"
    r"|\b(?:for|party of|group of) (\d{1,2}|two|three|four|five|six|seven|eight|nine|ten)\b(?!\s*(?:\$|dollars|bucks|km|mi|hours?|pm|am))")


def _party_size(text):
    """'4 friends' → 4, 'me and 3 friends' → 4, 'for six' → 6; None if not said."""
    m = _PARTY_RE.search(text)
    if not m:
        return None
    raw = next(g for g in m.groups() if g)
    n = int(raw) if raw.isdigit() else _NUM_WORDS.get(raw)
    if n and m.group(2):          # "me and 3 friends" counts you too
        n += 1
    elif n and re.search(r"\b(?:with|and) (?:the |our |my )?(?:\w+ )?kids\b", text) and "kids" in m.group(0):
        n += 2                    # "family with two kids" = the kids plus two adults
    return n if n and 1 <= n <= 20 else None


# A number that isn't money: a time ("10 pm", "7:30"), a distance, a duration, a head count.
_NOT_MONEY = r"(?!\s*(?:a\.?m\b|p\.?m\b|:\d|%|km\b|kms\b|mi\b|miles?\b|mins?\b|minutes?\b|hours?\b|hrs?\b|"\
             r"people\b|ppl\b|persons?\b|of us\b|friends\b|guys\b|kids\b|adults\b|th\b|st\b|nd\b|rd\b|days?\b|stops?\b))"
_MONEY_AFTER = r"\s*(?:\$|dollars?\b|bucks\b|cad\b|each\b|pp\b|per person\b|a head\b|a person\b|total\b|budget\b)"
_MONEY_BEFORE = r"\b(?:budget(?: of| is)?|under|around|about|max(?:imum)?|up to|spend(?:ing)?|less than|no more than)\s*\$?\s*"
_BUDGET_RANGE = re.compile(rf"\$?\s*\b(\d{{2,4}})\s*(?:-|to)\s*\$?\s*(\d{{2,4}})\b{_NOT_MONEY}")
_BUDGET_ONE = re.compile(rf"\$\s*(\d{{2,4}})\b|\b(\d{{2,4}})(?={_MONEY_AFTER})|{_MONEY_BEFORE}(\d{{2,4}})\b{_NOT_MONEY}")


def _budget(text: str) -> float | None:
    """'$60', '60 each', '60 bucks', 'under 60', 'budget 60', '50-100' → the amount.
    A bare number is never money: '10 pm', '20 minutes', '15 km', '2025' are not budgets."""
    m = _BUDGET_RANGE.search(text)
    if m:
        return float(max(int(m.group(1)), int(m.group(2))))
    m = _BUDGET_ONE.search(text)
    return float(next(g for g in m.groups() if g)) if m else None


def _word(text: str, *words: str) -> bool:
    """Any of these as whole words ("car" in "by car", not in "card"; plural allowed)."""
    return any(re.search(rf"\b{re.escape(w)}s?\b", text) for w in words)


def _stem(text: str, *stems: str) -> bool:
    """Any word starting with one of these ("relax" → "relaxing"; "date" not in "update")."""
    return any(re.search(rf"\b{re.escape(w)}", text) for w in stems)


def _heuristic_extract(text):
    out = {"days": _extract_days(text)}
    n = _party_size(text)
    if n:
        out["party_size"] = n
    b = _budget(text)
    if b is not None:
        out["budget"] = b
    for vibe, kws in _VIBE_KEYWORDS.items():
        if _stem(text, *kws):
            out["vibe"] = vibe
            break
    if _word(text, "morning", "breakfast", "brunch"):
        out["time_of_day"] = "morning"
    elif _word(text, "afternoon", "midday", "lunch"):
        out["time_of_day"] = "afternoon"
    elif _word(text, "late night", "late-night", "night out", "clubbing"):
        out["time_of_day"] = "night"
    if _word(text, "walk", "walking", "on foot"):
        out["transport"] = "walk"
    elif _word(text, "transit", "subway", "metro", "bus", "buses", "train", "ttc", "streetcar"):
        out["transport"] = "transit"
    elif _word(text, "car", "driving", "drive"):
        out["transport"] = "car"
    if _word(text, "solo", "myself", "just me", "alone"):
        out["group_type"], out["party_size"] = "solo", 1
    elif _word(text, "family", "kid", "kids"):
        out["group_type"] = "family"
    elif _word(text, "friend", "friends"):
        out["group_type"] = "friends"
        out.setdefault("party_size", 4)
    elif _word(text, "date", "couple", "for two", "partner"):
        out["group_type"], out["party_size"] = "date", out.get("party_size", 2)
    for diet in ["vegan", "vegetarian", "halal", "kosher", "gluten-free", "gluten free", "pescatarian"]:
        if diet in text:
            out["dietary"] = diet
            break
    if any(w in text for w in ["no alcohol", "sober", "non-alcoholic", "no drinks"]):
        out["group_type"] = out.get("group_type") or "family"  # suppresses bars
    return out


def _clean(prefs):
    """Drop null/empty so build_plan uses its own defaults."""
    keep = {}
    for k in ("budget", "vibe", "party_size", "days", "time_of_day",
              "transport", "group_type", "dietary", "interests", "avoid",
              "requested_venues", "likes", "crowd_pref", "energy", "no_alcohol",
              "radius_scale", "radius"):
        v = prefs.get(k)
        if v not in (None, "", "null") and v != []:
            keep[k] = v
    return keep


def _nothing_found(generic: str, ran_out_of_time: bool = False) -> str:
    """Don't blame the budget/vibe when the venue search itself is down or slow."""
    if ran_out_of_time or osm_places.degraded():
        return ("The venue search I use is overloaded right now, so I couldn't pull places. "
                "Give it a minute and try again.")
    return generic


_NOTHING = ("I couldn't find enough places near there for that. Want me to search a wider area, "
            "or loosen something (like what to avoid)?")
_PER_PERSON_RE = re.compile(r"\b(each|per person|per head|a head|pp|/ ?person|apiece)\b")
_TOTAL_RE = re.compile(r"\b(total|in total|altogether|all together|all in|for (?:all|everyone|the group|us all))\b")
_AMOUNT_RE = re.compile(r"\$\s*\d|\b\d{2,5}\s*(?:dollars|bucks|each|pp)\b")


def _money(v) -> float | None:
    """A budget from the router: 80, "80", "$80", "50-100" (→ 100). None if absent."""
    if isinstance(v, (int, float)):
        return float(v) if v > 0 else None
    nums = re.findall(r"\d+(?:\.\d+)?", str(v or ""))
    return max(float(n) for n in nums) if nums else None


def _from_router(raw: dict) -> dict:
    """Router prefs → the shape _resolve works with (unknown/bad values dropped)."""
    out = _structured({k: v for k, v in raw.items() if k not in ("budget",)})
    if raw.get("budget") is not None and _money(raw["budget"]):
        out["budget"] = min(_money(raw["budget"]), 10000.0)
    for k in ("budget_scope",):
        if raw.get(k) in ("total", "per_person"):
            out[k] = raw[k]
    if isinstance(raw.get("requested_venues"), list):
        out["requested_venues"] = [str(v)[:80] for v in raw["requested_venues"] if v][:3]
    try:
        if raw.get("days"):
            out["days"] = max(1, min(int(raw["days"]), 5))
    except (TypeError, ValueError):
        pass
    return out


def _taste_defaults(db: Session, user: User) -> dict:
    """Planner defaults from the user's taste profile ({} if they have none)."""
    row = db.get(TasteProfile, user.id)
    return taste_profile.defaults_for(row.answers) if row and row.answers else {}


_PREF_CHOICES = {
    "vibe": set(VIBE_PLANS), "group_type": {"date", "friends", "family", "solo"},
    "time_of_day": {"morning", "afternoon", "evening", "night"},
    "transport": {"walk", "transit", "car", "any"},
}


RADIUS_KM_MIN, RADIUS_KM_MAX = 1.0, 20.0


# Search radius (m) when they name a neighbourhood or a place rather than a city.
_AREA_RADIUS = {"microhood": 1500, "neighborhood": 2000, "venue": 1500, "macrohood": 4000}


def _radius_m(km: float) -> int:
    """Search radius in metres, clamped to the planner's 1-20 km range."""
    return int(max(RADIUS_KM_MIN, min(km, RADIUS_KM_MAX)) * 1000)


_RADIUS_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*(km|kms|kilomet(?:er|re)s?|mi|miles?)\b")


def _extract_radius(text):
    """'within 5 km', 'under 3 miles' → metres (None if not stated)."""
    m = _RADIUS_RE.search(text or "")
    if not m:
        return None
    km = float(m.group(1)) * (1.609 if m.group(2).startswith("mi") else 1.0)
    return _radius_m(km)


def _structured(raw: dict) -> dict:
    """Sanitise chip-sent prefs (untrusted client input) into the same shape the
    extractor produces. Unknown keys and bad values are dropped."""
    out = {}
    for k, allowed in _PREF_CHOICES.items():
        if raw.get(k) in allowed:
            out[k] = raw[k]
    for k, lo, hi in (("party_size", 1, 20), ("days", 1, 5)):
        try:
            out[k] = max(lo, min(hi, int(raw[k])))
        except (KeyError, TypeError, ValueError):
            pass
    try:
        if raw.get("budget") is not None:
            out["budget"] = max(0.0, min(float(raw["budget"]), 10000.0))
    except (TypeError, ValueError):
        pass
    try:
        if raw.get("radius_km") is not None:
            out["radius"] = _radius_m(float(raw["radius_km"]))
    except (TypeError, ValueError):
        pass
    for k, n in (("location", 100), ("interests", 200), ("avoid", 200), ("dietary", 100)):
        if isinstance(raw.get(k), str) and raw[k].strip():
            out[k] = raw[k].strip()[:n]
    if isinstance(raw.get("requested_venues"), list):     # "Plan a night here" from the club guide
        names = [v.strip()[:80] for v in raw["requested_venues"] if isinstance(v, str) and v.strip()]
        if names:
            out["requested_venues"] = names[:3]
    return out


def _words(s):
    return {w.rstrip("s") for w in re.findall(r"[a-z]{4,}", (s or "").lower())}


def _apply_session_early(prefs, session):
    """The page's "Planning around" settings fill what a typed request doesn't say.
    They sit between the message (always wins) and the taste profile (fills only
    what's left) — and already started from the profile, so they come first."""
    if not session:
        return
    if not prefs.get("group_type") and session.get("group_type"):
        prefs["group_type"] = session["group_type"]
        if session.get("party_size"):
            prefs["party_size"] = session["party_size"]
    for k in ("time_of_day", "transport", "interests", "dietary"):
        if not prefs.get(k) and session.get(k):
            prefs[k] = session[k]
    # Things ruled out earlier ("no bars") keep applying on later messages.
    if session.get("avoid"):
        prefs["avoid"] = ", ".join(dict.fromkeys(filter(None, [prefs.get("avoid"), session["avoid"]])))


def _apply_taste_early(prefs, taste):
    """Profile traits the request didn't state. Runs right after extraction.
    The LLM always fills party_size (default 2), so the crew only comes from the
    profile when the request names no group at all."""
    if not taste:
        return
    if not prefs.get("group_type") and taste.get("group_type"):
        prefs["group_type"] = taste["group_type"]
        prefs["party_size"] = taste.get("party_size", prefs.get("party_size"))
    for k in ("time_of_day", "transport", "dietary"):
        if not prefs.get(k) and taste.get(k):
            prefs[k] = taste[k]


def _apply_taste_late(prefs, taste):
    """Profile fallbacks that must come AFTER the request-driven logic in
    _resolve: nopes after the interest scrub (so a stated interest beats a
    profile nope), budget after the per-person conversion (so it isn't scaled
    twice), vibe after surprise (so "surprise me" still surprises)."""
    if not taste:
        return
    stated = _words(prefs.get("interests"))
    if taste.get("avoid"):
        keep = [t for t in taste["avoid"].split(", ")
                if not any(w.startswith(x) or x.startswith(w) for w in stated for x in _words(t))]
        prefs["avoid"] = ", ".join(dict.fromkeys(filter(None, [prefs.get("avoid"), *keep])))
    if taste.get("likes"):
        banned = _words(prefs.get("avoid"))
        prefs["likes"] = ", ".join(t for t in taste["likes"].split(", ") if not (_words(t) & banned))
    if not prefs.get("budget") and taste.get("budget_per_person"):
        prefs["budget"] = taste["budget_per_person"] * int(prefs.get("party_size") or taste.get("party_size") or 2)
    if not prefs.get("vibe") and taste.get("vibe"):
        prefs["vibe"] = taste["vibe"]
    for k in ("crowd_pref", "energy", "no_alcohol", "radius_scale"):
        if k in taste:
            prefs[k] = taste[k]
    if taste.get("vary"):
        prefs["vary"] = True
    if taste.get("target_stops"):
        prefs.setdefault("target_stops", taste["target_stops"])


def _resolve(req: ChatRequest, taste: dict | None = None, extracted: dict | None = None):
    """Shared: extract prefs + resolve the location. Returns (lat, lng, prefs).
    Anything the request states wins; the taste profile fills the gaps.
    `extracted` is the router's reading of the conversation (None → keywords)."""
    structured = req.prefs is not None
    # Chip-sent prefs skip the LLM entirely and never run text heuristics.
    text = "" if structured else _turn_text(req)
    # Typed requests carry the page's current settings; they fill the gaps only.
    session = _structured(req.defaults) if (req.defaults and not structured) else {}
    if structured:
        prefs = _structured(req.prefs)
    elif extracted is not None:
        prefs = _from_router(extracted)
        # A head count spelled out ("two kids", "4 friends") beats the model's guess,
        # which tends to miss the adults ("family with two kids" → 2).
        n = _party_size(text)
        if n:
            prefs["party_size"] = n
    else:
        prefs = _heuristic_extract(text)
    # Was a budget actually said in this request (vs. coming from the bar/profile)?
    prefs["_budget_stated"] = bool(prefs.get("budget")) and not structured
    _apply_session_early(prefs, session)
    _apply_taste_early(prefs, taste)

    # A city the user names wins over the device pin (and over the page's place).
    lat, lng = req.lat, req.lng
    loc_name = prefs.get("location") or (None if structured else _extract_location(_orig_user_text(req)))
    if not loc_name and session.get("location"):
        loc_name = prefs["location"] = session["location"]
    place_kind = None
    if loc_name:
        # neighbourhoods and landmarks from the stored list first (no live geocoder)
        found = venue_index.find_place(loc_name, (req.lat, req.lng))
        coords = found[:2] if found else geocode(loc_name, None)
        place_kind = found[2] if found else None
        if coords:
            lat, lng = coords
    if (lat is None or lng is None) and not structured:
        coords = _guess_location_coords(req)
        if coords:
            lat, lng = coords

    # "Don't want X" is a hard rule: merge the LLM's `avoid` with the regex catch,
    # and scrub avoided things back out of interests so they can't sneak in.
    avoid_bits = [b for b in [prefs.get("avoid"), _extract_avoid(text)] if b]
    avoid = ", ".join(dict.fromkeys(", ".join(avoid_bits).split(", "))) if avoid_bits else ""
    if avoid:
        prefs["avoid"] = avoid
        av_toks = [t.strip().rstrip("s") for t in avoid.split(",") if t.strip()]
        if prefs.get("interests"):
            prefs["interests"] = ", ".join(
                w for w in re.split(r"[,;]", prefs["interests"])
                if w.strip() and not any(t in w.lower() for t in av_toks))

    # Water activities (kayaking, waterbiking, beach…) count as an interest even
    # when the extractor misses them, so the planner routes to water venues.
    water_hits = [w for w in _WATER_WORDS if w in text and not any(
        t.strip().rstrip("s") in w for t in (avoid or "").split(",") if t.strip())]
    if water_hits and not any(w in (prefs.get("interests") or "").lower() for w in _WATER_WORDS):
        prefs["interests"] = ", ".join(filter(None, [prefs.get("interests"), *water_hits[:3]]))

    if not prefs.get("budget") and any(w in text for w in ["no limit", "no object", "unlimited"]):
        prefs["budget"] = 500
        prefs.setdefault("_assumed", []).append("no limit → a $500 budget")
    group_kw = any(w in text for w in ["boys", "guys", "bros", "the crew", "squad", "group of",
                                        "the group", "lads", "bachelor", "stag", "bachelorette",
                                        "buddies", "girls night", "girls trip"])
    if group_kw:
        prefs["group_type"] = "friends"        # a boys'/girls' night is NOT a date
        if not prefs.get("party_size"):
            prefs["party_size"] = 4
    # A stated budget is a TOTAL unless they said per person. When a group's wording
    # doesn't say, _budget_scope_question asks once (no hidden multiplying).
    # The router may *guess* total/per person; only count it when the words were
    # actually said, or this turn answers our "total or each?" question.
    scope = prefs.pop("budget_scope", None)
    said = ("per_person" if _PER_PERSON_RE.search(text) else
            "total" if _TOTAL_RE.search(text) else None)
    if scope and not said and "budget_scope" not in req.asked:
        scope = None
    scope = scope or (None if structured else said)
    prefs["_scope"] = scope
    if prefs.get("_budget_stated") and scope == "per_person":
        prefs["budget"] = float(prefs["budget"]) * int(prefs.get("party_size") or 2)
    if not prefs.get("budget") and session.get("budget"):
        prefs["budget"] = session["budget"]          # the bar's budget is a total — never re-scaled
    if req.surprise or any(w in text for w in ["surprise", "random", "you pick", "you choose", "whatever"]):
        prefs["surprise"] = True
        prefs.setdefault("budget", 100)
        rng = random.Random(req.seed) if req.seed is not None else random
        prefs["vibe"] = prefs.get("vibe") or rng.choice(list(VIBE_PLANS))
    if not structured and not prefs.get("radius"):
        r = _extract_radius(text)
        if r:
            prefs["radius"] = r
        elif session.get("radius"):
            prefs["radius"] = session["radius"]
    # "In Kensington Market" means around there, not anywhere within 7 km of it.
    tight = _AREA_RADIUS.get(place_kind)
    if tight and not (not structured and _extract_radius(text)):
        prefs["radius"] = min(prefs.get("radius") or tight, tight)
    if not structured and re.search(r"\b(wider|widen|further|farther|bigger area|more area)\b", _last_msg(req)):
        prefs["radius"] = _radius_m(2 * (prefs.get("radius") or 7000) / 1000)
        prefs.setdefault("_assumed", []).append(f"searching within {prefs['radius'] / 1000:g} km")
    if not prefs.get("days"):
        prefs["days"] = _extract_days(text)
    if not prefs.get("time_of_day") and any(k in text for k in
            ["full day", "all day", "whole day", "entire day", "day trip", "the whole thing"]):
        prefs["time_of_day"] = "morning"

    # "Make it fancier/cheaper" moves the budget by a stated step (the same steps as
    # the page's buttons), from the budget in play. An amount in the message wins.
    last = "" if structured else (req.messages[-1].content.lower() if req.messages else "")
    base = float(prefs.get("budget") or (req.current_plan or {}).get("budget") or 100)
    if any(w in last for w in ["fancier", "fancy", "nicer", "upscale", "classier", "classy",
                               "bougie", "boujee", "high end", "high-end", "luxury", "luxurious",
                               "more expensive", "splurge", "baller", "treat ourselves", "treat myself"]):
        prefs["vibe"] = "extravagant"
        if not _AMOUNT_RE.search(last) and re.search(r"\b(make|go|bit|more|little|something)\b", last):
            prefs["budget"] = round(base * 1.5 / 5) * 5
            prefs.setdefault("_assumed", []).append(f"fancier → ${prefs['budget']:g} budget")
    elif any(w in last for w in ["cheaper", "budget friendly", "more affordable", "save money",
                                 "less expensive", "cost less"]):
        if not _AMOUNT_RE.search(last):
            prefs["budget"] = max(20, round(base * 0.65 / 5) * 5)
            prefs.setdefault("_assumed", []).append(f"cheaper → ${prefs['budget']:g} budget")
    if not prefs.get("vibe") and session.get("vibe"):       # after surprise, so "surprise me" still surprises
        prefs["vibe"] = session["vibe"]
    _apply_taste_late(prefs, taste)
    return lat, lng, prefs


# "What's on?" questions get a list of events, not a plan. Deliberately narrow:
# "plan a date tonight" is still a plan; "what's happening tonight" is events.
_EVENTS_RE = re.compile(
    r"\b(?:what'?s|whats|what is) (?:on|happening|going on)\b"
    r"|\bhappening (?:now|right now|tonight|today|this weekend|near me|nearby|around)\b"
    r"|\b(?:live )?events? (?:tonight|today|now|right now|this weekend|near me|nearby|around)\b"
    r"|\bany(?:thing)? (?:events?|festivals?|shows?|concerts?|gigs?)\b"
    r"|\b(?:festivals?|concerts?|gigs?) (?:tonight|today|now|this weekend|near me|nearby|on)\b")
_WINDOW_LABEL = {"now": "right now", "tonight": "tonight", "weekend": "this weekend"}


def _events_window(req: ChatRequest, last: str) -> str | None:
    """now | tonight | weekend when this is a what's-on request, else None."""
    mode = (req.prefs or {}).get("mode")
    if mode in _WINDOW_LABEL:
        return mode
    if not last or not _EVENTS_RE.search(last):
        return None
    if "weekend" in last:
        return "weekend"
    return "tonight" if "tonight" in last else "now"


def _events_reply(lat, lng, window, radius_km, tz, prefs) -> ChatResponse:
    when = _WINDOW_LABEL[window]
    if not live_events.available_for(lat, lng):
        return ChatResponse(type="message", window=window,
            message="Live event search isn't switched on here yet — want me to plan something instead?")
    evs = live_events.happening(lat, lng, window=window, radius_km=radius_km, tz=tz,
        classification=events_svc.classification_for(prefs.get("vibe"), prefs.get("interests")))
    km = f"{radius_km:g} km"
    if not evs:
        return ChatResponse(type="events", window=window, events=[], message=(
            f"Nothing listed {when} within {km}. I check ticketed events and the city's festival "
            "calendar, so small free pop-ups can be missing — try a wider radius, or I can plan "
            "something instead."))
    on_now = sum(1 for e in evs if live_events.is_on_now(e))
    lead = f"{on_now} on now · " if window == "now" and on_now else ""
    return ChatResponse(type="events", window=window, events=[Event(**e) for e in evs],
        message=f"Here's what's on {when} within {km} ({lead}{len(evs)} found):")


_UNDERSTOOD_CHOICES = {"vibe": set(VIBE_PLANS), "group_type": {"date", "friends", "family", "solo"},
                       "time_of_day": {"morning", "afternoon", "evening", "night"},
                       "transport": {"walk", "transit", "car"}}


def _understood(prefs: dict) -> dict:
    """The resolved settings worth showing back to the user (valid values only)."""
    out = {k: prefs[k] for k, ok in _UNDERSTOOD_CHOICES.items() if prefs.get(k) in ok}
    for k, cast in (("party_size", int), ("budget", float)):
        try:
            if prefs.get(k):
                out[k] = cast(prefs[k])
        except (TypeError, ValueError):
            pass
    if prefs.get("radius"):
        out["radius_km"] = round(prefs["radius"] / 1000, 1)
    if isinstance(prefs.get("location"), str) and prefs["location"].strip():
        out["location"] = prefs["location"].strip()[:100]
    for k, n in (("interests", 120), ("avoid", 120), ("dietary", 60)):
        if isinstance(prefs.get(k), str) and prefs[k].strip():
            out[k] = prefs[k].strip()[:n]    # "sushi" / "no bars" ride along on the next message
    return out


@router.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, background: BackgroundTasks, user: User = Depends(get_current_user),
         db: Session = Depends(get_db)):
    seen: dict = {}
    start = time.perf_counter()
    with usage.request_tracker() as tracker:
        resp = _chat(req, user, db, seen)
    if seen.get("prefs") is not None:
        resp.understood = _understood(seen["prefs"])
    background.add_task(
        save_plan_log, user_id=user.id, endpoint="chat",
        user_text=_orig_user_text(req) or None, prefs=seen.get("prefs") or req.prefs,
        lat=seen.get("lat", req.lat), lng=seen.get("lng", req.lng), resp=resp, tracker=tracker,
        duration_ms=int((time.perf_counter() - start) * 1000),
        session_id=req.session_id, intent=seen.get("intent") or resp.intent)
    _learn(background, user, resp, seen)
    return resp


def _learn(background: BackgroundTasks, user: User, resp, seen: dict) -> None:
    """Queue what this answer showed, and what an edit in it dropped, for
    services/feedback.py. Admin and test accounts don't count."""
    if not feedback.counts_for(user):
        return
    if seen.get("dropped") and seen.get("before"):
        background.add_task(feedback.record, user.id, feedback.plan_key(seen["before"]), list(seen["dropped"]))
    for p in ([resp.plan] if getattr(resp, "plan", None) else list(getattr(resp, "days", None) or [])):
        stops = [s.model_dump() for s in p.stops]
        if rows := feedback.rows_for("shown", stops):
            background.add_task(feedback.record, user.id, feedback.plan_key(stops), rows)


def _chat(req: ChatRequest, user: User, db: Session, seen: dict) -> ChatResponse:
    structured = req.prefs is not None
    text = "" if structured else _turn_text(req)
    last = "" if structured else (req.messages[-1].content.lower() if req.messages else "")
    taste = _taste_defaults(db, user)

    routed = None
    current = _valid_plan(req.current_plan)      # the plan on screen, if the page sent a usable one
    if not structured:
        routed = (plan_intent.route([m.model_dump() for m in req.messages], current, req.defaults)
                  or plan_intent.route_rules(text, current, events=bool(_events_window(req, last))))
        seen["intent"] = routed["intent"]
        if routed["intent"] in ("ask_about_plan", "chitchat") and routed.get("answer"):
            return ChatResponse(type="message", message=routed["answer"][:600], intent=routed["intent"])

    extracted = routed["prefs"] if routed and routed["source"] == "llm" else None
    lat, lng, prefs = _resolve(req, taste, extracted)
    seen.update(prefs=prefs, lat=lat, lng=lng)

    if routed and routed["intent"] == "edit_plan" and current:
        edited = _edit(current, routed["ops"], prefs, seen, req.seed)
        if edited:
            return edited

    if lat is None or lng is None:
        if "location" in req.asked:
            msg = ("Tell me where to plan — I still don't have a place. Turn on live location, "
                   "or type a neighbourhood or city (e.g. \"Kensington Market\").")
        else:
            msg = "Tell me where — turn on live location, or just name a place (e.g. \"in Toronto\")."
        return ChatResponse(type="message", message=msg, ask="location")

    window = _events_window(req, last) or (
        ("weekend" if "weekend" in last else "tonight" if "tonight" in last else "now")
        if routed and routed["intent"] == "events" else None)
    if window:
        radius_km = (prefs["radius"] / 1000) if prefs.get("radius") else taste.get("radius_km", 10)
        resp = _events_reply(lat, lng, window, radius_km, req.tz, prefs)
        resp.intent = "events"
        return resp

    q = _budget_scope_question(prefs, req.asked)
    if q:
        return q
    if not prefs.get("budget"):                    # never block on it: assume, and say so
        prefs["budget"] = 100.0
        prefs.setdefault("_assumed", []).append("a $100 budget")
    if not prefs.get("vibe"):
        prefs["vibe"] = "chill"

    opts = _clean(prefs)
    days = int(opts.pop("days", 1) or 1)
    opts.pop("budget", None)
    budget = float(prefs["budget"])

    # A "try another / something different" gives fresh picks instead of repeating.
    # A client-sent exclude list (venues already shown) also counts as a re-roll.
    opts["vary"] = bool(req.exclude) or any(w in last for w in ["another", "different", "something else",
                                            "try again", "switch", "change it", "new plan",
                                            "not this", "plan b"]) or bool(prefs.get("vary"))
    opts["surprise"] = bool(prefs.get("surprise"))
    if req.seed is not None:
        opts["seed"] = req.seed
    if req.exclude:
        opts["exclude"] = {n.lower() for n in req.exclude if n}
    # How full to make the day (whole day → more stops; evening → fewer).
    opts["target_stops"] = _target_stops(text, opts.get("time_of_day"), prefs.get("target_stops"))

    if days > 1:
        trip = [Plan(**d) for d in build_trip(days=days, lat=lat, lng=lng, budget=budget, **opts) if d["stops"]]
        if not trip:
            return ChatResponse(type="message", intent="new_plan", message=_nothing_found(_NOTHING),
                                quick_replies=["Search a wider area", "Something different"])
        title = f"Your {len(trip)}-day {opts.get('vibe','').replace('_',' ')} trip".strip()
        return ChatResponse(type="itinerary", message=f"{title} — arranged day by day.", days=trip, title=title,
                            intent="new_plan")

    plan_dict = build_plan(lat=lat, lng=lng, budget=budget, **opts)
    if not plan_dict["stops"]:
        return ChatResponse(type="message", intent="new_plan", message=_nothing_found(
            _NOTHING, ran_out_of_time=bool(plan_dict.get("skipped"))),
            quick_replies=["Search a wider area", "Something different"])
    plan = Plan(**plan_dict)
    msg = plan.intro or "Your plan:"
    if plan.quick:
        msg = "Quick version: the AI planner is busy right now, so these are Rova's top-ranked picks. " + msg
    if plan_dict.get("special_note"):
        msg = f"{plan_dict['special_note']} {msg}"
    if prefs.get("_assumed"):
        msg += f" (Assumed {', '.join(prefs['_assumed'])} — change it anytime.)"
    # Be honest about requested places we couldn't confirm exist nearby —
    # better than silently dropping them (or worse, inventing them).
    skipped = plan_dict.get("skipped") or []
    if skipped:
        msg += (f" (Venue search was slow just now, so I left out: {', '.join(s.lower() for s in skipped)}. "
                "Tap 🔄 Try another — it'll be quicker the second time.)")
    missing = plan_dict.get("unverified_requests") or []
    if missing:
        msg += (f" (I looked for {', '.join(missing)} but couldn't verify "
                f"{'it' if len(missing) == 1 else 'them'} nearby, so it's not included.)")
    return ChatResponse(type="itinerary", message=msg, plan=plan, intent="new_plan")


def _budget_scope_question(prefs: dict, asked: list[str]) -> ChatResponse | None:
    """For a group of 3+, "$100" could mean the total or each. Ask once; if the
    answer still doesn't say, treat it as the total and say so."""
    n = int(prefs.get("party_size") or 2)
    if not prefs.get("_budget_stated") or prefs.get("_scope") or n < 3:
        return None
    b = float(prefs["budget"])
    if "budget_scope" in asked:
        prefs.setdefault("_assumed", []).append(f"${b:g} is the total for all {n}")
        return None
    return ChatResponse(type="question", ask="budget_scope", intent="new_plan",
                        message=f"Is ${b:g} the total for all {n} of you, or per person?",
                        quick_replies=[f"${b:g} total", f"${b:g} each"])


def _valid_plan(raw) -> dict | None:
    """The plan the page sent back, validated (it's client input); None if unusable."""
    if not isinstance(raw, dict):
        return None
    try:
        plan = Plan(**raw).model_dump()
    except Exception:
        return None
    return plan if plan["stops"] else None


def _edit(current: dict, ops: list[dict], prefs: dict, seen: dict, seed=None) -> ChatResponse | None:
    """Change only the stops a follow-up names; the rest stay exactly as shown."""
    seen.update(lat=current["center"]["lat"], lng=current["center"]["lng"])
    # The plan carries the brief it was made to (transport, what they asked for); this
    # turn's words win, the plan fills the rest, so a swap keeps to the same kind of night.
    ctx = EditContext(
        vibe=prefs.get("vibe") or current["vibe"], party_size=int(current["party_size"] or 2),
        transport=prefs.get("transport") or current.get("transport") or "any", group_type=prefs.get("group_type"),
        dietary=prefs.get("dietary") or "", interests=current.get("interests") or "",
        avoid=prefs.get("avoid") or "",
        no_alcohol=bool(prefs.get("no_alcohol")), radius=prefs.get("radius"),
        taste={"likes": prefs.get("likes", ""), "crowd_pref": prefs.get("crowd_pref") or 0.0,
               "energy": prefs.get("energy")},
        seed=seed)
    plan, changed, problems = apply_edits(current, ops, ctx)
    seen.update(dropped=ctx.dropped, before=current["stops"])
    message = " ".join(changed + problems) or "I left the plan as it was."
    if not changed:
        return ChatResponse(type="message", message=message, intent="edit_plan")
    if not plan["stops"]:
        return ChatResponse(type="message", intent="edit_plan",
                            message="That would leave nothing in the plan — tell me what to put in instead.")
    if not problems:
        message += " Everything else stays the same."
    return ChatResponse(type="itinerary", message=message, plan=Plan(**plan), intent="edit_plan")


# The taste settings that shape a re-pick. Not the profile's transport, group or vibe:
# the plan on screen already says those, and an edit keeps to the plan.
_EDIT_TASTE = ("likes", "crowd_pref", "energy", "avoid", "no_alcohol", "dietary")
MAX_EDIT_STOPS = 10


@router.post("/edit", response_model=ChatResponse)
def edit(req: EditRequest, background: BackgroundTasks, user: User = Depends(get_current_user),
         db: Session = Depends(get_db)):
    """Change the plan on screen directly: remove, swap, add or move a stop, change the
    start time, the party size or the length, or add an event from beside the plan. The
    times, cost and distance come back re-worked, so the page never has to guess them."""
    plan = req.plan.model_dump()
    if len(plan["stops"]) > MAX_EDIT_STOPS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"A plan has at most {MAX_EDIT_STOPS} stops.")
    ops = [o.model_dump(exclude_none=True) for o in req.ops]
    start = time.perf_counter()
    with usage.request_tracker() as tracker:
        taste = _taste_defaults(db, user)
        seen: dict = {}
        resp = _edit(plan, ops, {k: taste[k] for k in _EDIT_TASTE if k in taste}, seen, seed=req.seed)
    background.add_task(
        save_plan_log, user_id=user.id, endpoint="edit", user_text=None, prefs={"ops": ops},
        lat=plan["center"]["lat"], lng=plan["center"]["lng"], resp=resp, tracker=tracker,
        duration_ms=int((time.perf_counter() - start) * 1000), session_id=req.session_id, intent="edit_plan")
    _learn(background, user, resp, seen)
    return resp


MAX_REVIEWS_PER_HOUR = 30


@router.post("/review", response_model=ReviewOut)
def review_plan(req: ReviewRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Rate the plan on screen, 1–5 stars, with an optional comment, for the admin
    Reviews page. One rating per person per plan: sending it again changes it."""
    plans = [req.plan] if req.plan else list(req.days)
    if not plans or not any(p.stops for p in plans):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "There's no plan to rate.")
    if any(len(p.stops) > MAX_EDIT_STOPS for p in plans):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"A plan has at most {MAX_EDIT_STOPS} stops.")
    dumped = [p.model_dump(mode="json") for p in plans]
    key = feedback.plan_key([s for p in dumped for s in p["stops"]])
    comment = " ".join(req.comment.split())[:1000] or None if req.comment is not None else None
    for attempt in range(2):
        try:
            row = _save_review(db, user, req, key, dumped, comment)
            break
        except IntegrityError:        # the same plan rated twice at once: update the one that won
            db.rollback()
            if attempt:
                raise
    return ReviewOut(id=str(row.id), stars=row.stars, comment=row.comment)


def _save_review(db: Session, user: User, req: ReviewRequest, key: str, dumped: list[dict],
                 comment: str | None) -> PlanReview:
    row = db.scalar(select(PlanReview).where(PlanReview.user_id == user.id, PlanReview.plan_key == key))
    if row is None:
        hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        recent = db.scalar(select(func.count()).select_from(PlanReview).where(
            PlanReview.user_id == user.id, PlanReview.created_at >= hour_ago)) or 0
        if recent >= MAX_REVIEWS_PER_HOUR:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "That's a lot of ratings — try again later.")
        row = PlanReview(user_id=user.id, plan_key=key, stars=req.stars, comment=comment,
                         plan={"plan": dumped[0]} if req.plan else {"days": dumped},
                         session_id=req.session_id)
        db.add(row)
    else:
        if req.comment is not None and comment != row.comment:
            row.comment = comment
            row.reviewed_at = None        # something new to read
        if row.stars != req.stars:
            row.stars = req.stars
            row.reviewed_at = None
    db.commit()
    return row


@router.post("/feedback", status_code=status.HTTP_204_NO_CONTENT)
def plan_feedback(req: FeedbackRequest, background: BackgroundTasks, user: User = Depends(get_current_user)):
    """The page says a plan was saved (sharing is recorded by /share/plan itself)."""
    stops = [s.model_dump() for s in req.stops]
    if feedback.counts_for(user) and (rows := feedback.rows_for(req.event, stops)):
        background.add_task(feedback.record, user.id, feedback.plan_key(stops), rows)


@router.post("/options", response_model=OptionsResponse)
def options(req: ChatRequest, background: BackgroundTasks, user: User = Depends(get_current_user),
            db: Session = Depends(get_db)):
    """Build-your-own: ranked, tagged candidate venues per category to pick from."""
    start = time.perf_counter()
    with usage.request_tracker() as tracker:
        resp = _options(req, user, db)
    loc = resp.location or {}
    background.add_task(
        save_plan_log, user_id=user.id, endpoint="options", session_id=req.session_id,
        user_text=_orig_user_text(req) or None, prefs=resp.context or req.prefs,
        lat=loc.get("lat", req.lat), lng=loc.get("lng", req.lng), resp=resp, tracker=tracker,
        duration_ms=int((time.perf_counter() - start) * 1000))
    return resp


def _options(req: ChatRequest, user: User, db: Session) -> OptionsResponse:
    lat, lng, prefs = _resolve(req, _taste_defaults(db, user))
    if lat is None or lng is None:
        return OptionsResponse(ok=False,
            message="Enable location or name a city (e.g. \"in Toronto\") so I can pull nearby options.")

    vibe = prefs.get("vibe") or "adventurous"
    group_type = prefs.get("group_type")
    time_of_day = prefs.get("time_of_day")
    interests = prefs.get("interests", "") or ""
    dietary = prefs.get("dietary", "") or ""
    transport = prefs.get("transport", "any") or "any"
    party_size = int(prefs.get("party_size") or 2)
    budget = prefs.get("budget")

    # Every section is searched in parallel and bounded by the plan time budget.
    specs = option_sections(time_of_day, vibe, group_type)
    jobs = [_POOL.submit(gather_options, slot_key, lat=lat, lng=lng, vibe=vibe, group_type=group_type,
                         interests=interests, dietary=dietary, transport=transport,
                         avoid=prefs.get("avoid", "") or "", limit=6,
                         likes=prefs.get("likes", "") or "",
                         crowd_pref=prefs.get("crowd_pref") or 0.0,
                         energy=prefs.get("energy"),
                         no_alcohol=bool(prefs.get("no_alcohol")),
                         radius_scale=prefs.get("radius_scale") or 1.0,
                         radius=prefs.get("radius"))
            for slot_key, *_ in specs]
    deadline = time.monotonic() + PLAN_TIME_BUDGET
    sections = []
    for (slot_key, label, icon, hint), job in zip(specs, jobs):
        opts = _result(job, [], timeout=deadline - time.monotonic())
        if opts:
            sections.append(Section(key=slot_key, label=label, icon=icon, hint=hint,
                                    options=[Option(**o) for o in opts]))

    events = []
    if events_svc.available():
        cls = events_svc.classification_for(vibe, interests)
        events = [Event(**e) for e in events_svc.search_events(lat, lng, radius_km=25, size=6, classification=cls)]

    return OptionsResponse(
        ok=True, location={"lat": lat, "lng": lng},
        context={"vibe": vibe, "budget": budget, "party_size": party_size,
                 "group_type": group_type, "time_of_day": time_of_day,
                 "interests": interests, "dietary": dietary, "transport": transport},
        sections=sections, events=events)


@router.post("/build", response_model=ChatResponse)
def build(req: BuildRequest, background: BackgroundTasks, user: User = Depends(get_current_user)):
    """Assemble + sequence + narrate an itinerary from the user's picked venues."""
    start = time.perf_counter()
    with usage.request_tracker() as tracker:
        resp = _build(req)
    background.add_task(
        save_plan_log, user_id=user.id, endpoint="build", user_text=None, session_id=req.session_id,
        prefs={"vibe": req.vibe, "budget": req.budget, "party_size": req.party_size,
               "group_type": req.group_type, "time_of_day": req.time_of_day,
               "interests": req.interests, "dietary": req.dietary,
               "picked": len(req.selections)},
        lat=req.lat, lng=req.lng, resp=resp, tracker=tracker,
        duration_ms=int((time.perf_counter() - start) * 1000))
    _learn(background, user, resp, {})
    return resp


def _build(req: BuildRequest) -> ChatResponse:
    if not req.selections:
        return ChatResponse(type="message", message="Pick at least one spot to build your itinerary.")
    plan_dict = build_from_selection(
        req.selections, lat=req.lat, lng=req.lng, party_size=req.party_size,
        budget=req.budget, vibe=req.vibe, interests=req.interests,
        group_type=req.group_type, time_of_day=req.time_of_day, dietary=req.dietary)
    if not plan_dict["stops"]:
        return ChatResponse(type="message", message="Couldn't build that — try picking a few spots.")
    plan = Plan(**plan_dict)
    return ChatResponse(type="itinerary", message=plan.intro or "Your itinerary:", plan=plan)
