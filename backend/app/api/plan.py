"""Chat-driven itinerary concierge. Requires a signed-in account — the LLM
planner is a members-only feature (debug stays open for ops).

With an LLM key it runs as a sharp concierge that reads the whole conversation,
extracts what it needs, asks one focused follow-up when required, then builds a
real plan optimised for close-together, best-value stops. Without a
key it degrades to a keyword heuristic so the app still works.
"""
import random
import re

from fastapi import APIRouter, Depends

from app.config import settings
from app.middleware.auth import get_current_user
from app.models import User
from app.schemas.plan import (
    ChatRequest, ChatResponse, Plan, Option, Section, OptionsResponse, BuildRequest, Event,
)
from app.services import foursquare, google_places, llm, yelp
from app.services import events as events_svc
from app.services.geocoding import geocode, geocode_place
from app.services.planner import (
    build_plan, build_trip, VIBE_PLANS, gather_options, option_sections, build_from_selection,
    _WATER_WORDS,
)

router = APIRouter(prefix="/plan", tags=["planner"])


@router.get("/debug")
def debug():
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

# The concierge's brain. Tuned to this app: sharp, efficient, gathers the four
# signals that matter, and only asks when it truly needs to.
SYSTEM = """You are the planning brain for a local outing concierge app.
Persona: a sharp, polished concierge — efficient and tasteful, minimal fluff, no rambling.
Your job: read the whole conversation and output ONLY JSON (no prose) with these keys:
  budget: the dollar number the user stated (a range like "50-100" → use the higher end), or null
  budget_per_person: true if that number is per-person ("$100 each", or a modest budget for a group of friends); else false
  vibe: one of chill|romantic|adventurous|extravagant|night_out, or null
  party_size: integer (default 2)
  days: integer (1 unless they mention a weekend/multi-day trip)
  time_of_day: morning|afternoon|evening|night or null
  transport: walk|transit|car or null
  group_type: date|friends|family|solo or null
  dietary: short string of food prefs/restrictions, or ""
  location: a city/neighbourhood the user names to plan in (e.g. "Toronto", "Kensington Market"), or null
  interests: short string of specific things they want (e.g. "aquarium, hookah, rec room"), or ""
  avoid: short string of things they explicitly do NOT want (e.g. "parks, museums, clubbing"), or "".
    Treat "no X", "not X", "don't want X", "skip X", "anything but X" as avoid — and NEVER
    leave a rejected thing in interests.
  requested_venues: array of specific venues/places the user asks to include by NAME —
    proper nouns only (e.g. "end the night at Celeste's" → ["Celeste's"], "can we hit
    The Rec Room" → ["The Rec Room"]). Generic types ("a rooftop bar", "korean bbq")
    belong in interests, NOT here. [] if none.
  ready: true ONLY if budget AND vibe are known
  question: if not ready, ONE concise concierge-style question to get the missing essentials
  quick_replies: array of 2-5 short tappable answers for that question
IMPORTANT: if the latest message asks for a NEW or SEPARATE plan (a "plan B", a plan for
another group / different people, "start over", a different day), extract preferences from
THAT request alone — do not carry over budget, vibe, group or interests from earlier plans
unless the user explicitly says "same as before". Ask again for anything now missing.
Planning is optimised for stops close together and best value — so a
walkable, budget-fitting plan. Prefer to gather time_of_day, transport, group_type and
dietary when natural, but NEVER block a plan on them: if budget and vibe are known,
set ready=true and leave the rest null. Ask at most what's essential, in one question."""


# "Plan B" / new-plan detection: when the guest pivots to a separate plan (another
# group, a second itinerary), drop the old conversation so plan A's budget/vibe/group
# don't bleed into plan B. The last message becomes the whole context.
_FRESH_RE = re.compile(
    r"\b(plan b|new plan|fresh plan|start over|from scratch|second (plan|itinerary)"
    r"|another (plan|itinerary|one for)|(plan|itinerary) for (another|a different|my other)"
    r"|different (group|crew|people|friends))\b")


def _fresh_cut(req: ChatRequest) -> ChatRequest:
    if len(req.messages) > 1 and _FRESH_RE.search(req.messages[-1].content.lower()):
        return req.model_copy(update={"messages": [req.messages[-1]]})
    return req


def _joined_user_text(req):
    return " ".join(m.content for m in req.messages if m.role == "user").lower()


def _orig_user_text(req):
    return " ".join(m.content for m in req.messages if m.role == "user")


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
    for m in reversed(req.messages):
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


def _target_stops(text, time_of_day):
    """How many stops to fill — a whole day is more; an evening is fewer."""
    if any(k in text for k in ["full day", "all day", "whole day", "entire day", "day trip",
                                "fill the day", "more the merrier", "packed", "jam pack",
                                "jam-pack", "as much as", "maximize", "lots to do", "the whole thing"]):
        return 6
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
_AVOID_NOISE = ("limit", "cap", "sure", "idea", "preference", "budget", "alcohol", "drink")


def _extract_avoid(text):
    hits = []
    for m in _AVOID_RE.finditer(text):
        for t in re.split(r"\s+(?:and|or)\s+|,", m.group(1)):
            t = re.sub(r"^(?:into|going to|doing|to do|going)\s+", "", t.strip())
            if t and t.split()[0] not in _AVOID_NOISE:
                hits.append(t)
    return ", ".join(dict.fromkeys(hits))


def _heuristic_extract(text):
    out = {"days": _extract_days(text)}
    rng = re.search(r"\$?\s*(\d{2,4})\s*(?:-|to)\s*\$?\s*(\d{2,4})", text)   # "50-100"
    if rng:
        out["budget"] = float(max(int(rng.group(1)), int(rng.group(2))))
    else:
        m = re.search(r"\$?\s*(\d{2,4})\b", text)
        if m:
            out["budget"] = float(m.group(1))
    for vibe, kws in _VIBE_KEYWORDS.items():
        if any(k in text for k in kws):
            out["vibe"] = vibe
            break
    if any(w in text for w in ["morning", "breakfast", "brunch"]):
        out["time_of_day"] = "morning"
    elif any(w in text for w in ["afternoon", "midday", "lunch"]):
        out["time_of_day"] = "afternoon"
    elif any(w in text for w in ["late night", "night out", "clubbing"]):
        out["time_of_day"] = "night"
    if any(w in text for w in ["walk", "walking", "on foot"]):
        out["transport"] = "walk"
    elif any(w in text for w in ["transit", "subway", "metro", "bus", "train"]):
        out["transport"] = "transit"
    elif any(w in text for w in ["car", "driving", "drive"]):
        out["transport"] = "car"
    if any(w in text for w in ["solo", "myself", "just me", "alone"]):
        out["group_type"], out["party_size"] = "solo", 1
    elif "family" in text or "kids" in text:
        out["group_type"] = "family"
    elif "friends" in text:
        out["group_type"] = "friends"
    elif any(w in text for w in ["date", "couple", "for two", "partner"]):
        out["group_type"], out["party_size"] = "date", out.get("party_size", 2)
    for diet in ["vegan", "vegetarian", "halal", "kosher", "gluten-free", "gluten free", "pescatarian"]:
        if diet in text:
            out["dietary"] = diet
            break
    if any(w in text for w in ["no alcohol", "sober", "non-alcoholic", "no drinks"]):
        out["group_type"] = out.get("group_type") or "family"  # suppresses bars
    return out


def _llm_extract(req):
    convo = "\n".join(f"{m.role}: {m.content}" for m in req.messages)
    return llm.chat_json([{"role": "system", "content": SYSTEM},
                          {"role": "user", "content": convo}])


def _clean(prefs):
    """Drop null/empty so build_plan uses its own defaults."""
    keep = {}
    for k in ("budget", "vibe", "party_size", "days", "time_of_day",
              "transport", "group_type", "dietary", "interests", "avoid",
              "requested_venues"):
        v = prefs.get(k)
        if v not in (None, "", "null") and v != []:
            keep[k] = v
    return keep


def _needs(prefs):
    if not prefs.get("budget"):
        return ChatResponse(type="question",
            message="Certainly. What's the budget for the outing?",
            quick_replies=["$50", "$100", "$200", "No limit"])
    if not prefs.get("vibe"):
        return ChatResponse(type="question",
            message="And the mood you're after?",
            quick_replies=[VIBE_LABELS[v] for v in ["chill", "romantic", "adventurous", "extravagant", "night_out"]])
    return None


def _resolve(req: ChatRequest):
    """Shared: extract prefs + resolve the location. Returns (lat, lng, prefs)."""
    text = _joined_user_text(req)
    prefs = (_llm_extract(req) if llm.available() else None) or _heuristic_extract(text)

    # A city the user names wins over the device pin.
    lat, lng = req.lat, req.lng
    loc_name = prefs.get("location") or _extract_location(_orig_user_text(req))
    if loc_name:
        coords = geocode(loc_name, None)
        if coords:
            lat, lng = coords
    if lat is None or lng is None:
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
    group_kw = any(w in text for w in ["boys", "guys", "bros", "the crew", "squad", "group of",
                                        "the group", "lads", "bachelor", "stag", "bachelorette",
                                        "buddies", "girls night", "girls trip"])
    if group_kw:
        prefs["group_type"] = "friends"        # a boys'/girls' night is NOT a date
        if not prefs.get("party_size"):
            prefs["party_size"] = 4
    per_person = (bool(prefs.get("budget_per_person"))
                  or bool(re.search(r"\b(each|per person|per head|a head|pp|/ ?person)\b", text))
                  or group_kw or prefs.get("group_type") == "friends")
    if prefs.get("budget") and per_person:
        prefs["budget"] = float(prefs["budget"]) * int(prefs.get("party_size") or 2)
    if req.surprise or any(w in text for w in ["surprise", "random", "you pick", "you choose", "whatever"]):
        prefs["surprise"] = True
        prefs.setdefault("budget", 100)
        rng = random.Random(req.seed) if req.seed is not None else random
        prefs["vibe"] = prefs.get("vibe") or rng.choice(list(VIBE_PLANS))
    if not prefs.get("days"):
        prefs["days"] = _extract_days(text)
    if not prefs.get("time_of_day") and any(k in text for k in
            ["full day", "all day", "whole day", "entire day", "day trip", "the whole thing"]):
        prefs["time_of_day"] = "morning"

    # Deterministic "make it fancier/cheaper" tier shifts.
    last = req.messages[-1].content.lower() if req.messages else ""
    if any(w in last for w in ["fancier", "fancy", "nicer", "upscale", "classier", "classy",
                               "bougie", "boujee", "high end", "high-end", "luxury", "luxurious",
                               "more expensive", "splurge", "baller", "treat ourselves", "treat myself"]):
        prefs["vibe"] = "extravagant"
        b = float(prefs.get("budget") or 0)
        prefs["budget"] = max(b * 1.6, b, 250)
        if any(w in last for w in ["no object", "no limit", "unlimited", "sky is the limit"]):
            prefs["budget"] = max(prefs["budget"], 600)
    elif any(w in last for w in ["cheaper", "cheap", "budget friendly", "more affordable",
                                 "affordable", "save money", "less expensive", "cost less", "on a budget"]):
        prefs["vibe"] = "chill"
    return lat, lng, prefs


@router.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, user: User = Depends(get_current_user)):
    req = _fresh_cut(req)
    text = _joined_user_text(req)
    last = req.messages[-1].content.lower() if req.messages else ""
    lat, lng, prefs = _resolve(req)
    if lat is None or lng is None:
        return ChatResponse(type="message",
            message="Tell me where — enable location, or just name a city (e.g. \"in Toronto\") — plus your budget and vibe.")

    missing = _needs(prefs)
    if missing:
        if prefs.get("question"):
            return ChatResponse(type="question", message=prefs["question"],
                                quick_replies=prefs.get("quick_replies") or missing.quick_replies)
        return missing

    opts = _clean(prefs)
    days = int(opts.pop("days", 1) or 1)
    opts.pop("budget", None)
    budget = float(prefs["budget"])

    # A "try another / something different" gives fresh picks instead of repeating.
    # A client-sent exclude list (venues already shown) also counts as a re-roll.
    opts["vary"] = bool(req.exclude) or any(w in last for w in ["another", "different", "something else",
                                            "try again", "switch", "change it", "new plan",
                                            "not this", "plan b"])
    opts["surprise"] = bool(prefs.get("surprise"))
    if req.seed is not None:
        opts["seed"] = req.seed
    if req.exclude:
        opts["exclude"] = {n.lower() for n in req.exclude if n}
    # How full to make the day (whole day → more stops; evening → fewer).
    opts["target_stops"] = _target_stops(text, opts.get("time_of_day"))

    if days > 1:
        trip = [Plan(**d) for d in build_trip(days=days, lat=lat, lng=lng, budget=budget, **opts) if d["stops"]]
        if not trip:
            return ChatResponse(type="message", message="I couldn't source enough for that trip nearby. A larger budget or different vibe?")
        title = f"Your {len(trip)}-day {opts.get('vibe','').replace('_',' ')} trip".strip()
        return ChatResponse(type="itinerary", message=f"{title} — arranged day by day.", days=trip, title=title)

    plan_dict = build_plan(lat=lat, lng=lng, budget=budget, **opts)
    if not plan_dict["stops"]:
        return ChatResponse(type="message", message="I couldn't source enough open spots nearby for that. A larger budget or different vibe?")
    plan = Plan(**plan_dict)
    msg = plan.intro or "Your plan:"
    # Be honest about requested places we couldn't confirm exist nearby —
    # better than silently dropping them (or worse, inventing them).
    missing = plan_dict.get("unverified_requests") or []
    if missing:
        msg += (f" (I looked for {', '.join(missing)} but couldn't verify "
                f"{'it' if len(missing) == 1 else 'them'} nearby, so it's not included.)")
    return ChatResponse(type="itinerary", message=msg, plan=plan)


@router.post("/options", response_model=OptionsResponse)
def options(req: ChatRequest, user: User = Depends(get_current_user)):
    """Build-your-own: ranked, tagged candidate venues per category to pick from."""
    req = _fresh_cut(req)
    lat, lng, prefs = _resolve(req)
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

    sections = []
    for slot_key, label, icon, hint in option_sections(time_of_day, vibe, group_type):
        opts = gather_options(slot_key, lat=lat, lng=lng, vibe=vibe, group_type=group_type,
                              interests=interests, dietary=dietary, transport=transport,
                              avoid=prefs.get("avoid", "") or "", limit=6)
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
def build(req: BuildRequest, user: User = Depends(get_current_user)):
    """Assemble + sequence + narrate an itinerary from the user's picked venues."""
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
