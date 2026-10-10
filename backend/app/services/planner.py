"""Itinerary planner — turns budget + vibe + context into an ordered plan of real
nearby places. Optimises for: stops CLOSE together (no crossing town) and best value
(great rating for the price). Uses Yelp (ratings/prices)
when keyed, falls back to OpenStreetMap. An optional LLM writes the narration.
"""
import logging
import math
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.services import foursquare
from app.services import geocoding
from app.services import google_places
from app.services import yelp
from app.services.osm_places import fetch_places
from app.services import llm
from app.services import weather as weather_svc
from app.services import events as events_svc
from app.services import live_events
from app.services import costs
from app.services import experience
from app.services import special_days
from app.services import venue_index
from app.metrics import time_itinerary_generation

log = logging.getLogger(__name__)

# Indoor-only activity set for wet/cold days.
INDOOR_ACTIVITIES = "arcades,escapegames,bowling,poolhalls,lasertag,karaoke,trampoline,museums,aquariums"

PRICE_LEVEL = {"$": 1, "$$": 2, "$$$": 3, "$$$$": 4}

# How much the fun/hype signal counts (activity & leisure slots only). Tuned so a
# ~1.5-star gap can flip on excitement (paintball 3.5★ vs gallery 5★) but a
# genuinely bad place can't ride "fun" past a much better one. rating contributes
# rating*2.0 (≈2 pts/star); fun contributes up to fun_factor*1.3 (≈6.5 max).
FUN_WEIGHT = 1.3

# How much popularity/buzz counts. Higher = favour the busy, well-known, trendy
# spots a younger crowd wants (Cactus Club-type places with thousands of reviews)
# over tiny little-known spots. log10(reviews) so 2000 reviews ≈ 3.3, 80 ≈ 1.9.
BUZZ_WEIGHT = 0.7

# Foursquare popularity is 0..1 from real foot traffic — the closest thing to a
# live "trendy right now" signal. Weighted so a genuinely buzzing spot can beat
# a slightly better-rated sleepy one, without drowning out rating/distance.
POPULARITY_WEIGHT = 1.5

# ---- time budget ----
# The page gives /plan/* requests 60s, and LLM extraction/narration can use some
# of that, so venue gathering for one plan gets a fixed wall-clock budget. When it
# runs out the plan ships with what it has (and says what's missing); slow searches
# keep running in the background and warm the caches for a retry.
PLAN_TIME_BUDGET = 25.0
TRIP_TIME_BUDGET = 36.0          # shared across all days of a multi-day trip
# IO-bound HTTP fan-out, shared across requests.
_POOL = ThreadPoolExecutor(max_workers=12, thread_name_prefix="plan-gather")


def _result(fut, default, timeout=0.0):
    """A future's value if it finished in time without error, else `default`."""
    if fut is None:
        return default
    try:
        return fut.result(timeout=max(0.0, timeout))
    except Exception:   # timeout, or the search itself failed
        return default


# ---- taste-profile knobs (all neutral by default: no profile → no change) ----
# crowd_pref in [-1, 1] scales buzz + popularity: "quiet & cozy" (-1) keeps 40% of
# their pull, "buzzing & busy" (+1) gets 160%.
CROWD_SCALE = 0.6
# Energy match for activity/leisure picks: points lost per unit of distance between
# a venue's arousal (experience.arousal_for, ~1–5) and the user's preferred energy.
ENERGY_WEIGHT = 0.8
# Soft nudge for things the profile says it loves (cuisines, activities). Smaller
# than an explicitly *requested* interest (_interest_bonus = 2.5) on purpose.
LIKE_WEIGHT = 1.2
# Venues that exist to serve alcohol — filtered out for a "no alcohol" profile.
# Compared after normalising category titles ("Cocktail Bars" → "cocktailbars",
# Foursquare "Night Club" → "nightclub", OSM "pub"). Gastropubs stay: they're
# restaurants first.
_ALCOHOL_CATS = {"bar", "bars", "pub", "pubs", "cocktailbar", "cocktailbars", "winebar", "winebars",
                 "beerbar", "beerbars", "brewery", "breweries", "brewpub", "brewpubs", "danceclub",
                 "danceclubs", "nightclub", "nightclubs", "lounge", "lounges", "nightlife",
                 "sportsbar", "sportsbars", "divebar", "divebars", "beergarden", "beergardens",
                 "whiskeybar", "whiskeybars", "tikibar", "tikibars", "speakeasy", "speakeasies"}
_ALCOHOL_TERMS = {"rooftop", "speakeasy", "wine bar", "cocktail", "craft beer", "brewery", "sake",
                  "hookah", "nightclub"}
# The evening "vibes" stop without the bar: shows, games, music.
NO_ALCOHOL_LEISURE = "comedyclubs,karaoke,musicvenues,bowling,arcades,escapegames,theater"

# "activity" = real things to do (aquarium, arcade, escape room, museum, bowling…).
# "leisure"  = evening vibes (lounge, hookah, cocktail/live-music, karaoke…).
# Every plan is built as activity + food + leisure so it's never all restaurants.
SLOT_SPECS = {
    "cafe":    {"label": "Coffee & warm-up", "icon": "☕", "yelp": {"categories": "coffee,cafes"}, "osm": "food", "base": 8},
    "scenic":  {"label": "Scenic & free",    "icon": "🌇", "yelp": {"categories": "parks,beaches,gardens,landmarks", "term": "scenic view"}, "osm": "nature", "base": 0},
    "activity":{"label": "Activity",         "icon": "🎡", "yelp": {"categories": "aquariums,zoos,museums,galleries,arcades,escapegames,amusementparks,bowling,active,arts", "term": "things to do"}, "osm": "activities", "base": 28},
    "lunch":   {"label": "Lunch",            "icon": "🥗", "yelp": {"categories": "restaurants", "term": "lunch"}, "osm": "food", "base": 18},
    "dinner":  {"label": "Dinner",           "icon": "🍽️", "yelp": {"categories": "restaurants", "term": "dinner"}, "osm": "food", "base": 30},
    "leisure": {"label": "Vibes & drinks",   "icon": "🎶", "yelp": {"categories": "danceclubs,nightlife,lounges,cocktailbars,hookah_bars,karaoke,comedyclubs,musicvenues,bars"}, "osm": "party", "base": 18},
    "drinks":  {"label": "Drinks",           "icon": "🍸", "yelp": {"categories": "bars,cocktailbars"}, "osm": "party", "base": 15},
    "dessert": {"label": "Sweet finish",     "icon": "🍰", "yelp": {"categories": "desserts,icecream"}, "osm": "food", "base": 10},
    "event":   {"label": "Live event",       "icon": "🎟️", "yelp": {}, "osm": "activities", "base": 40},
}

# Stops that are the same thing to a person ("their bar" may be a leisure or a drinks stop).
_KIND_OF_STOP = {"leisure": "night", "drinks": "night", "dinner": "meal", "lunch": "meal"}

# Day order used to sequence a user-assembled (picker) itinerary.
SLOT_ORDER = {"cafe": 0, "scenic": 1, "activity": 2, "event": 3, "lunch": 4,
              "dinner": 6, "leisure": 7, "drinks": 7, "dessert": 8}

# Each vibe = activity + a meal + a leisure/finish. Price band scales with vibe.
VIBE_PLANS = {
    "chill":       {"price": "1,2",   "slots": ["activity", "dinner", "dessert"]},
    "romantic":    {"price": "2,3",   "slots": ["activity", "dinner", "leisure"]},
    "adventurous": {"price": "1,2,3", "slots": ["activity", "dinner", "leisure"]},
    "extravagant": {"price": "3,4",   "slots": ["activity", "dinner", "leisure"]},
    "night_out":   {"price": "2,3",   "slots": ["activity", "dinner", "leisure"]},
}
DEFAULT_VIBE = "romantic"

# Distance penalty per km = this / search radius in km (7 km → ~0.36, 2 km → 1.25).
DISTANCE_WEIGHT_KM = 2.5

# A "day arc" ordered morning→night. We take the first N depending on how much of
# the day the guest wants filled — so a full day is many stops, an evening is few.
# Repeated slot types (e.g. two activities) resolve to different venues.
ARCS = {
    "morning":   ["cafe", "scenic", "activity", "lunch", "activity", "dinner", "leisure", "dessert"],
    "afternoon": ["scenic", "activity", "lunch", "activity", "dinner", "leisure", "dessert"],
    "evening":   ["scenic", "activity", "dinner", "leisure", "dessert"],
    "night":     ["dinner", "activity", "leisure", "dessert"],
}

# Transport shapes the search. Only "walk" chains stops tightly together (a
# walkable route); everything else searches the whole central-city radius from the
# centre, so you get the best spots across town — but bounded so it stays inside
# the city (~7km of downtown = central Toronto, not Scarborough/Mississauga).
TRANSPORT = {
    "walk":    {"radius": 1600, "penalty": 1.5, "cluster": 1000, "chain": True},
    "transit": {"radius": 7000, "penalty": 0.35, "chain": False},
    "car":     {"radius": 9000, "penalty": 0.2,  "chain": False},
    "any":     {"radius": 7000, "penalty": 0.35, "chain": False},
}


# ---- when each stop happens ----
# Plans are timed in the stored list's city (Toronto for now). The time decides
# which places are open, and is shown on each stop even without the AI narrator.
LOCAL_TZ = ZoneInfo("America/Toronto")
START_HOUR = {"morning": 10, "afternoon": 13, "evening": 18, "night": 20}
# Minutes per kind of stop, including getting to the next one.
STOP_MINUTES = {"cafe": 45, "scenic": 60, "activity": 105, "lunch": 75, "dinner": 105,
                "leisure": 90, "drinks": 75, "dessert": 40, "event": 150}


def plan_start(time_of_day=None, now=None) -> datetime:
    """When a plan starts: today at the part of day's usual hour; if that has
    passed, the next half hour; if the part of day is mostly over (3h+), tomorrow."""
    now = now or datetime.now(LOCAL_TZ)
    base = now.replace(hour=START_HOUR.get(time_of_day or "evening", 18), minute=0, second=0, microsecond=0)
    if base >= now:
        return base
    soon = (now + timedelta(minutes=30 - now.minute % 30)).replace(second=0, microsecond=0)
    return soon if soon - base <= timedelta(hours=3) else base + timedelta(days=1)


def schedule(slots, start: datetime) -> list[datetime]:
    """Planned start of each stop, one after another from `start`."""
    out, t = [], start
    for s in slots:
        out.append(t)
        t += timedelta(minutes=STOP_MINUTES.get(s, 60))
    return out


def clock(t: datetime) -> str:
    return t.strftime("%I:%M %p").lstrip("0")


def parse_start(value) -> datetime | None:
    try:
        t = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=LOCAL_TZ)


def retime(plan: dict) -> None:
    """Give every stop its clock time from the plan's start (after edits too)."""
    start = parse_start(plan.get("start"))
    if start:
        for s, t in zip(plan["stops"], schedule([s["slot"] for s in plan["stops"]], start)):
            s["time"] = clock(t)


def _haversine_km(a_lat, a_lng, b_lat, b_lng):
    R = 6371
    dlat = math.radians(b_lat - a_lat)
    dlng = math.radians(b_lng - a_lng)
    x = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(a_lat)) * math.cos(math.radians(b_lat)) * math.sin(dlng / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(x))


def _est_cost(cand, slot_key):
    """Rough cost per person (services/costs.py)."""
    return costs.estimate(cand, slot_key)


# Fun, real activity categories keyed to who's going — so a boys' day gets
# arcades/karting/paintball/escape rooms, not an art gallery.
WATER_ACTIVITIES = ("rafting,paddleboarding,surfing,sailing,boatcharters,boattours,jetskis,"
                    "waterparks,snorkeling,divingcenters,beaches,lakes")
_WATER_WORDS = ["kayak", "canoe", "paddle", "waterbike", "water bike", "water biking",
                "jet ski", "jetski", "boat", "sail", "surf", "snorkel", "scuba", "raft",
                "swim", "beach", "on the water", "water activities", "water sports",
                "watersport", "lake"]


def wants_water(interests, text=""):
    s = f"{interests or ''} {text or ''}".lower()
    return any(w in s for w in _WATER_WORDS)


def activity_categories(group_type, vibe, interests):
    it = (interests or "").lower()
    competitive = "arcades,escapegames,gokarts,lasertag,paintball,axethrowing,minigolf,bowling,trampoline,karaoke"
    # Water first: kayaking, waterbiking, paddleboarding etc. beat every other
    # routing rule — if they asked to be on the water, put them on the water.
    if wants_water(it):
        return WATER_ACTIVITIES
    if any(w in it for w in ["active", "sporty", "energetic", "physical"]):   # moving, not karaoke
        return "gokarts,climbing,trampoline,bowling,lasertag,paintball,axethrowing,minigolf"
    if any(w in it for w in ["paintball", "arcade", "escape", "kart", "laser", "axe", "bowling",
                              "competitive", "mini golf", "minigolf", "trampoline", "climb", "karaoke"]):
        return competitive
    if group_type == "friends" or any(w in it for w in ["boys", "guys", "bros", "bachelor", "squad", "buddies"]):
        return competitive
    # "fun & exciting" energy → active/competitive, NOT a quiet gallery.
    if vibe in ("adventurous", "night_out"):
        return competitive
    if group_type == "date" or vibe == "romantic":
        return "aquariums,galleries,observatories,wineries,museums,minigolf,arcades"
    if group_type == "family":
        return "aquariums,zoos,amusementparks,minigolf,bowling,arcades,trampoline,museums"
    # sensible default: fun, hands-on — deliberately no galleries/museums so they
    # don't win on rating for someone who just wants a good time.
    return "arcades,escapegames,gokarts,bowling,minigolf,aquariums,trampoline"


_CUISINES = ["korean bbq", "kbbq", "bbq", "sushi", "ramen", "korean", "japanese", "italian",
             "steakhouse", "steak", "burgers", "burger", "pizza", "thai", "indian", "chinese",
             "dim sum", "mexican", "tacos", "seafood", "vietnamese", "pho", "mediterranean",
             "greek", "hotpot", "hot pot", "noodles"]


def cuisine_term(interests, dietary):
    s = f"{interests or ''} {dietary or ''}".lower()
    for w in _CUISINES:
        if w in s:
            return w
    return None


# Interest keywords routed to the slot they belong to, so "rooftop patio and
# gelato" shapes the drinks stop AND the dessert stop — not just the activity.
_SLOT_INTEREST_TERMS = {
    "leisure": ["nightclub", "clubbing", "club", "dance", "dj", "rave", "hookah", "shisha",
                "rooftop", "live music", "jazz", "speakeasy", "wine bar", "board game",
                "karaoke", "billiards", "pool hall", "comedy"],
    "drinks":  ["rooftop", "speakeasy", "wine bar", "cocktail", "craft beer", "brewery", "sake"],
    "dessert": ["gelato", "ice cream", "boba", "bubble tea", "crepe", "waffle", "churro",
                "cheesecake", "donut", "bakery", "matcha"],
    "cafe":    ["matcha", "boba", "bubble tea", "specialty coffee", "brunch"],
    "scenic":  ["beach", "waterfront", "harbourfront", "boardwalk", "sunset", "lookout",
                "botanical", "garden", "island", "trail", "picnic"],
}


def interest_term_for(slot_key, interests):
    it = (interests or "").lower()
    for w in _SLOT_INTEREST_TERMS.get(slot_key, ()):
        if w in it:
            return w
    return None


def _tokens(s):
    return [t.strip() for t in re.split(r"[,;/]| and | or ", (s or "").lower()) if len(t.strip()) >= 3]


# "No bars" means no drinking spots, whatever they're called: The Office Pub is
# listed as a karaoke venue, and still a pub.
_DRINKING = ["bar", "pub", "tavern", "lounge", "brewery", "brewpub", "taproom", "saloon", "nightclub",
             "night club", "beer garden", "beer hall", "wine bar", "cocktail", "speakeasy"]
_AVOID_KINDS = {"bar": _DRINKING, "pub": _DRINKING, "drink": _DRINKING, "drinking": _DRINKING,
                "alcohol": _DRINKING, "booze": _DRINKING, "club": ["nightclub", "night club", "dance club"]}
# A restaurant called "… Sushi Bar" is still dinner, not a bar.
_FOOD_RE = re.compile(r"\b(restaurant|cafe|coffee|bakery|dessert|ice cream|eatery|diner|bistro|"
                      r"delicatessen|food)\b")


def _words_re(words):
    return re.compile(r"\b(?:" + "|".join(re.escape(w) for w in words) + r")(?:e?s)?\b")


def avoid_match(c, avoid):
    """True when a candidate is something the user said NOT to do — matched
    against its name and categories, singular/plural tolerant ('parks'→'park')."""
    toks = _tokens(avoid)
    if not toks:
        return False
    name = (c.get("name") or "").lower()
    cats = " ".join(str(x).lower().replace("_", " ") for x in (c.get("categories") or []))
    for t in toks:
        base = t[:-1] if t.endswith("s") and len(t) > 3 else t
        kinds = _AVOID_KINDS.get(base)
        if kinds:
            rx = _words_re(kinds)
            if rx.search(cats) or (rx.search(name) and not _FOOD_RE.search(cats)):
                return True
        elif _words_re([base]).search(name + " " + cats):
            return True
    return False


def _interest_bonus(c, interests):
    """Ranking boost when a candidate actually matches what the user asked for —
    by name or category — so stated interests beat generic popularity."""
    it = (interests or "").lower()
    if not it:
        return 0.0
    hay = (c.get("name") or "").lower() + " " + " ".join(
        str(x).lower() for x in (c.get("categories") or []))
    tokens = [t for t in re.split(r"[,;/]| and ", it) if len(t.strip()) >= 3]
    return 2.5 if any(t.strip() in hay for t in tokens) else 0.0


def _like_bonus(c, likes):
    """Soft nudge toward what the taste profile loves (cuisines, activities).
    Unlike stated `interests`, likes never re-route the search — they only
    re-rank what was found, so a sushi lover doesn't get sushi every time."""
    toks = _tokens(likes)
    if not toks:
        return 0.0
    hay = (c.get("name") or "").lower() + " " + " ".join(
        str(x).lower() for x in (c.get("categories") or []))
    return LIKE_WEIGHT if any(t in hay for t in toks) else 0.0


def _is_alcohol_venue(c):
    cats = c.get("categories") or []
    if any(re.sub(r"[^a-z0-9]", "", str(x).lower()) in _ALCOHOL_CATS for x in cats):
        return True
    # named like one ("The Office Pub", listed as karaoke) and not a restaurant
    return bool(_words_re(_DRINKING).search((c.get("name") or "").lower())) and not _FOOD_RE.search(
        " ".join(str(x).lower() for x in cats))


def _norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _name_matches(requested: str, found: str) -> bool:
    """Loose match between a name the user typed and a venue name from a
    verified source — normalised containment either way, so "Joe's Pizza"
    matches "Joes Pizza Kitsilano"."""
    a, b = _norm_name(requested), _norm_name(found)
    return bool(a) and bool(b) and (a in b or b in a)


# Which slot a verified user-requested venue occupies, inferred from its
# categories. First match wins; anything unrecognised counts as an activity.
_REQUEST_SLOT_RULES = [
    ("cafe", ("coffee", "cafe", "tea house")),
    ("dessert", ("dessert", "ice cream", "gelato", "bakery", "donut")),
    ("drinks", ("bar", "pub", "brewer", "cocktail", "lounge", "nightlife",
                "club", "winery", "hookah", "karaoke")),
    ("scenic", ("park", "beach", "garden", "landmark", "lookout", "trail")),
    ("dinner", ("restaurant", "food", "pizza", "sushi", "steak", "bbq",
                "diner", "bistro", "noodle", "taco")),
]


def _slot_for_categories(categories) -> str:
    hay = " ".join(str(c).lower() for c in (categories or []))
    for slot_key, words in _REQUEST_SLOT_RULES:
        if any(w in hay for w in words):
            return slot_key
    return "activity"


def verify_requested_venue(name: str, lat: float, lng: float, radius: int = 8000) -> dict | None:
    """Research a place the user asked for by name against verified sources
    (Yelp, then Foursquare). Returns the real venue or None — the LLM never
    gets to decide whether a place exists."""
    if not (name or "").strip():
        return None
    # The stored venue list first: instant, and it's where the Nightlife tab's places come from.
    try:
        stored = venue_index.find_venue(name, lat, lng, radius)
    except Exception:
        stored = None
    if stored and _name_matches(name, stored["name"]):
        return stored
    for cands in (yelp.search(lat, lng, term=name, radius=radius, limit=8),
                  foursquare.search(lat, lng, query=name, radius=radius, limit=8)):
        for c in cands:
            if _name_matches(name, c.get("name") or ""):
                return c
    # Keyless fallback: OSM/Nominatim, hard-bounded to the area.
    c = geocoding.find_place(name, lat, lng, radius_km=radius / 1000)
    if c and _name_matches(name, c.get("name") or ""):
        return c
    return None


def route_for(slot_key, *, interests="", dietary="", group_type=None, vibe=DEFAULT_VIBE,
              bad_weather=False, no_alcohol=False):
    """(term_override, cat_override) for a slot's search: group-aware fun for
    activities, cuisine for meals, stated interests routed to the slot they fit."""
    term_override = cat_override = None
    cz = cuisine_term(interests, dietary)
    if slot_key == "activity":
        cat_override = INDOOR_ACTIVITIES if bad_weather else activity_categories(group_type, vibe, interests)
        if interests and not cz:
            term_override = interests          # e.g. "escape room"
    elif slot_key in ("dinner", "lunch") and cz:
        term_override = cz                     # e.g. "korean bbq"
    elif slot_key not in ("dinner", "lunch"):
        # hookah/rooftop shape the leisure stop, gelato the dessert stop, beach the scenic stop
        t = interest_term_for(slot_key, interests)
        if t:
            term_override = "nightclub" if t in ("clubbing", "club", "dance", "dj", "rave") else t
    if no_alcohol and slot_key == "leisure":
        cat_override = NO_ALCOHOL_LEISURE      # shows, games, music — not a bar
        if term_override in _ALCOHOL_TERMS or term_override == "nightclub":
            term_override = None
    return term_override, cat_override


# What they'd say to want fast food on a date anyway.
_CASUAL_ASK = re.compile(r"\b(fast food|mcdonald'?s?|quick|cheap eats|comfort food|drive.?thru|late.?night snack)\b", re.I)
_FOOD_AND_DRINK_SLOTS = {"dinner", "lunch", "dessert", "cafe", "drinks", "leisure"}


def too_casual(c, slot_key, vibe=None, group_type=None, wish="") -> bool:
    """Fast food has no place in a date or a fancy night (McDonald's is for a quick,
    cheap or comfort plan), unless they asked for it."""
    dressy = vibe in ("romantic", "extravagant") or group_type == "date"
    return (dressy and slot_key in _FOOD_AND_DRINK_SLOTS and not _CASUAL_ASK.search(wish or "")
            and venue_index.is_fast_food(c))


def allowed(c, avoid="", no_alcohol=False) -> bool:
    """False for a candidate the user ruled out ("no bars") or, for a no-alcohol
    profile, a venue that exists to serve alcohol."""
    return not avoid_match(c, avoid) and not (no_alcohol and _is_alcohol_venue(c))


def stop_from(pick, slot_key, **extra) -> dict:
    """A plan stop from a chosen candidate."""
    slot = SLOT_SPECS[slot_key]
    return {
        "slot": slot_key, "label": slot["label"], "icon": slot["icon"],
        "name": pick["name"], "lat": pick["lat"], "lng": pick["lng"],
        "rating": pick.get("rating"), "price": pick.get("price"),
        "address": pick.get("address"), "image": pick.get("image"),
        "url": pick.get("url"), "source": pick.get("source"),
        "est_cost": _est_cost(pick, slot_key), "categories": pick.get("categories") or [],
        # arousal (energy) drives the narration's emotional arc downstream.
        "arousal": experience.arousal_for(pick.get("categories")),
        **extra,
    }


def route_km(stops) -> float:
    """Total distance hopping stop to stop, in km."""
    return round(sum(_haversine_km(a["lat"], a["lng"], b["lat"], b["lng"])
                     for a, b in zip(stops, stops[1:])), 1)


def _stored(anchor, slot_key, radius, dietary, term_override, cat_override, at):
    """Candidates from the stored venue list, or None (area not covered / DB trouble)."""
    want = term_override or (dietary if dietary and slot_key in ("lunch", "dinner") else None)
    try:
        return venue_index.candidates(slot_key, anchor[0], anchor[1], radius,
                                      term=want, cats=cat_override, at=at)
    except Exception:
        log.exception("stored venue lookup failed; using the live search")
        return None


def _gather(anchor, slot_key, price_pref, radius, dietary, term_override=None, cat_override=None, at=None):
    """Candidate places for one stop. The stored venue list first (instant, knows
    opening hours and how well known a place is); the live searches only where
    it has nothing. `at` = when the stop happens (places closed then are left out)."""
    stored = _stored(anchor, slot_key, radius, dietary, term_override, cat_override, at)
    if stored:
        return stored
    slot = SLOT_SPECS[slot_key]
    term = slot["yelp"].get("term")
    if dietary and slot_key in ("lunch", "dinner"):
        term = f"{dietary} {term or ''}".strip()
    if term_override:                       # e.g. "escape room" / "korean bbq"
        term = term_override
    categories = cat_override or slot["yelp"].get("categories")
    # No hard price filter — a full candidate pool means "fancier" plans never
    # starve; the price tier is applied as a soft preference in scoring instead.
    # best_match (Yelp's relevance/popularity blend) surfaces the trendy, busy
    # spots better than pure rating; our own scoring then re-ranks with buzz + fun.
    cands = yelp.search(anchor[0], anchor[1], term=term,
                        categories=categories,
                        radius=radius, limit=20, sort_by="best_match")
    # Foursquare: annotate Yelp candidates with real foot-traffic popularity
    # (matched by name) and add trendy Foursquare-only venues to the pool.
    if foursquare.available():
        q = term or (categories or "").split(",")[0] or None
        fsq = foursquare.search(anchor[0], anchor[1], query=q, radius=radius, limit=15)
        by_name = {_norm_name(f["name"]): f for f in fsq if f.get("name")}
        matched = set()
        for c in cands:
            f = by_name.get(_norm_name(c.get("name") or ""))
            if f:
                c["popularity"] = f.get("popularity")
                matched.add(_norm_name(c["name"]))
        cands.extend(f for n, f in by_name.items() if n not in matched)
    # Google Maps: the widest real-world coverage of restaurants/activities.
    # Query by the slot's term (or first category), merge non-duplicates into
    # the pool and backfill ratings/photos on venues other sources found bare.
    if google_places.available():
        q = term or (categories or "").split(",")[0] or slot["label"]
        goog = google_places.search(anchor[0], anchor[1], query=q, radius=radius, limit=15)
        have = {_norm_name(c.get("name") or "") for c in cands}
        for g in goog:
            n = _norm_name(g["name"])
            if n in have:
                for c in cands:
                    if _norm_name(c.get("name") or "") == n:
                        c.setdefault("image", None)
                        c["image"] = c["image"] or g.get("image")
                        c["rating"] = c.get("rating") or g.get("rating")
                        c["review_count"] = c.get("review_count") or g.get("review_count")
                        break
            else:
                cands.append(g)
    if len(cands) < 5:
        for h in fetch_places(slot["osm"], lat=anchor[0], lng=anchor[1], radius=radius):
            cands.append({"source": "osm", "name": h["name"], "lat": h["lat"], "lng": h["lng"],
                          "rating": None, "review_count": None, "price": None,
                          "categories": [h.get("subtype") or slot["osm"]],
                          "address": h.get("address"), "image": None, "url": h.get("website")})
    return cands


def _score(c, anchor, penalty, fancy=False, apply_fun=False, interests="", taste=None,
           slot_key=None, allowance=None):
    """Higher is better. Balances rating, closeness, price preference (cheaper for
    value vibes / pricier when they want fancy), and — for activity/leisure
    slots — a fun/hype signal that's independent of star rating.

    `taste` (from the user's taste profile, all optional): likes → soft bonus,
    crowd_pref → how much buzz counts, energy → match venue energy on
    activity/leisure stops. None/empty leaves the score exactly as before."""
    taste = taste or {}
    crowd = 1 + CROWD_SCALE * (taste.get("crowd_pref") or 0)
    rating = c.get("rating") or 3.6                      # neutral for unrated OSM
    dist = _haversine_km(anchor[0], anchor[1], c["lat"], c["lng"])
    level = PRICE_LEVEL.get(c.get("price"), 2)
    score = rating * 2.0 - dist * penalty
    score += _interest_bonus(c, interests)   # what they ASKED for outranks generic hits
    score += _like_bonus(c, taste.get("likes"))   # what their profile loves, gently
    score += (c.get("popularity") or 0) * POPULARITY_WEIGHT * crowd   # Foursquare foot-traffic trendiness
    score += (level - 1) * 0.6 if fancy else -(level - 1) * 0.5   # tier preference
    score += math.log10((c.get("review_count") or 0) + 1) * BUZZ_WEIGHT * crowd   # buzz / popularity
    score += c.get("boost", 0)   # stored list: well known, open then, not a chain (venue_index)
    if slot_key and allowance is not None:   # its share of the budget (services/costs.py)
        score += costs.fit(_est_cost(c, slot_key), allowance, slot_key, fancy)
    if apply_fun:
        # Excitement matters here, not just satisfaction — so a high-energy
        # experience can out-rank a better-reviewed but sleepy one.
        score += experience.fun_factor_for(c.get("categories")) * FUN_WEIGHT
        if taste.get("energy") is not None:
            score -= abs(experience.arousal_for(c.get("categories")) - taste["energy"]) * ENERGY_WEIGHT
    return score


def tag_for(c) -> tuple[str | None, str | None]:
    """A single vibe tag for a candidate so the picker UI can show fun vs mediocre."""
    rating = c.get("rating") or 0
    rc = c.get("review_count") or 0
    fun = experience.fun_factor_for(c.get("categories"))
    if (c.get("popularity") or 0) >= 0.95:
        return ("🔥", "Trending now")
    if fun >= 4.0 and rc >= 150:
        return ("🔥", "Buzzing")
    if rating >= 4.7 and rc >= 30:
        return ("⭐", "Top-rated")
    if fun <= 2.3:
        return ("😴", "Low-key")
    return (None, None)


def _pick(cands, used, anchor, penalty, vary=False, fancy=False, apply_fun=False,
          interests="", rng=random, explore=False, taste=None, slot_key=None, allowance=None):
    ranked = sorted(
        (c for c in cands if c.get("name") and c["name"].lower() not in used),
        key=lambda c: _score(c, anchor, penalty, fancy, apply_fun, interests, taste, slot_key, allowance),
        reverse=True)
    if not ranked:
        return None
    # Surprise mode: a wide, even draw from the strong top of the ranking —
    # adventurous, but never the genuinely weak tail.
    if explore and len(ranked) > 1:
        pool = ranked[:min(10, len(ranked))]
        return rng.choice(pool)
    # On a "try another", draw rank-weighted from the strong top slice — better
    # picks stay likelier, but the pool is wide enough that re-rolls really differ.
    if vary and len(ranked) > 1:
        pool = ranked[:min(6, len(ranked))]
        weights = [1.0 / (i + 1) ** 0.7 for i in range(len(pool))]
        return rng.choices(pool, weights=weights, k=1)[0]
    return ranked[0]


def _slots_for(vibe, time_of_day, group_type, length):
    eff = time_of_day or ("night" if vibe == "night_out" else "evening")
    arc = ARCS.get(eff, ARCS["evening"])
    slots = arc[:max(2, min(length, len(arc)))]
    # No bars for family / kids — swap drinks/leisure for dessert.
    if group_type in ("family", "kids"):
        slots = ["dessert" if s in ("drinks", "leisure") else s for s in slots]
    return slots


def build_plan(*, lat, lng, budget, vibe=DEFAULT_VIBE, party_size=2, transport="any",
               time_of_day=None, group_type=None, dietary="", interests="", avoid="",
               radius=None, exclude=None, vary=False, target_stops=4,
               requested_venues=None, seed=None, surprise=False,
               likes="", crowd_pref=0.0, energy=None, no_alcohol=False, radius_scale=1.0,
               time_budget=None):
    with time_itinerary_generation("single_day"):
        return _build_plan(
            lat=lat, lng=lng, budget=budget, vibe=vibe, party_size=party_size,
            transport=transport, time_of_day=time_of_day, group_type=group_type,
            dietary=dietary, interests=interests, avoid=avoid, radius=radius,
            exclude=exclude, vary=vary, target_stops=target_stops,
            requested_venues=requested_venues, seed=seed, surprise=surprise,
            likes=likes, crowd_pref=crowd_pref, energy=energy, no_alcohol=no_alcohol,
            radius_scale=radius_scale, time_budget=time_budget,
        )


def _build_plan(*, lat, lng, budget, vibe=DEFAULT_VIBE, party_size=2, transport="any",
                time_of_day=None, group_type=None, dietary="", interests="", avoid="",
                radius=None, exclude=None, vary=False, target_stops=4,
                requested_venues=None, seed=None, surprise=False,
                likes="", crowd_pref=0.0, energy=None, no_alcohol=False, radius_scale=1.0,
                time_budget=None):
    # A caller-supplied seed makes the draw reproducible; each new seed is a
    # genuinely different roll. Surprise mode implies variety.
    rng = random.Random(seed) if seed is not None else random
    vary = vary or surprise
    deadline = time.monotonic() + (time_budget or PLAN_TIME_BUDGET)
    left = lambda: deadline - time.monotonic()
    vibe = vibe if vibe in VIBE_PLANS else DEFAULT_VIBE
    price_pref = VIBE_PLANS[vibe]["price"]
    tconf = TRANSPORT.get(transport, TRANSPORT["any"])
    radius = radius or int(tconf["radius"] * (radius_scale or 1.0))
    # Closeness counts more in a small area: "in Kensington Market" (2 km) should
    # mean Kensington, not a famous square 1.6 km away.
    penalty = max(tconf["penalty"], DISTANCE_WEIGHT_KM / max(radius / 1000, 0.5))
    used = set(exclude or ())
    slots = _slots_for(vibe, time_of_day, group_type, target_stops)
    taste = {"likes": likes, "crowd_pref": crowd_pref, "energy": energy}
    if no_alcohol:
        slots = ["dessert" if s == "drinks" else s for s in slots]

    # Weather-aware: on a wet/cold day, keep it indoors — swap the outdoor scenic
    # stop for a cosy cafe and force indoor activities.
    wx = weather_svc.get_weather(lat, lng)
    bad_weather = bool(wx and (wx["rainy"] or wx["cold"]))
    if bad_weather:
        slots = ["cafe" if s == "scenic" else s for s in slots]
        # the swap can create a second cafe (morning arc) — make it an activity
        seen_cafe = False
        for i, s in enumerate(slots):
            if s == "cafe":
                if seen_cafe:
                    slots[i] = "activity"
                seen_cafe = True

    # A tight budget gets fewer, better stops (not a hot-dog dinner); what they
    # asked for ("…and dessert") stays.
    slots = costs.fewer_stops(slots, budget, party_size,
                              keep=lambda s: bool(interest_term_for(s, interests)))
    theme = _special_theme(lat, lng, slots, interests, avoid, no_alcohol)
    center = (lat, lng)
    start = plan_start(time_of_day)
    at = {}
    for s, t in zip(slots, schedule(slots, start)):
        at.setdefault(s, t)   # a repeated kind of stop is checked at its first time
    chain = tconf.get("chain", False)   # only walking chains stops tightly together
    fancy = vibe == "extravagant"       # bias picks toward pricier/upscale venues

    def route(slot_key):
        term, cats = route_for(slot_key, interests=interests, dietary=dietary, group_type=group_type,
                               vibe=vibe, bad_weather=bad_weather, no_alcohol=no_alcohol)
        if theme and slot_key == theme[1] and term is None:
            term = theme[2]                    # St. Patrick's Day → an Irish pub
        return term, cats

    def keep(c):
        return allowed(c, avoid, no_alcohol)

    # Gather every slot's candidates (and live events) concurrently, from the
    # centre at the full radius — one parallel wave instead of a slow network
    # round-trip per stop. Whatever isn't back by the deadline is left out.
    jobs = {k: _POOL.submit(_gather, center, k, price_pref, radius, dietary, *route(k), at=at[k])
            for k in dict.fromkeys(slots)}
    # Events in the next 24h within the plan's reach — ticketed and free city ones.
    ev_job = (_POOL.submit(live_events.happening, lat, lng, window="day", size=8,
                           radius_km=max(5.0, radius / 1000),
                           classification=events_svc.classification_for(vibe, interests))
              if live_events.available_for(lat, lng) else None)
    day_job = None
    if theme and theme[0].get("keyword") and events_svc.available():
        s_utc, e_utc = live_events.window_range("day")
        day_job = _POOL.submit(events_svc.search_events, lat, lng, radius_km=max(5.0, radius / 1000), size=4,
                               keyword=theme[0]["keyword"], start_utc=s_utc, end_utc=e_utc)
    wait(list(jobs.values()), timeout=max(0.0, left()))
    late = {k for k, f in jobs.items() if not f.done()}
    found = {k: [c for c in _result(f, []) if keep(c) and not too_casual(c, k, vibe, group_type, interests)]
             for k, f in jobs.items()}

    # Each stop's share of the budget per person: a $40 night and a $400 night
    # pick different places, not just a different number of them.
    allow = costs.allowances(slots, budget, party_size)
    if fancy:   # "fancier": lean to the pricier places (cheaper swaps still keep it near budget)
        allow = {k: v * 1.3 for k, v in allow.items()}
    pools = {}   # picked name → (its candidates, where they were searched from, itself), for cheaper swaps
    anchor = center
    stops, skipped = [], []
    for i, slot_key in enumerate(slots):
        slot = SLOT_SPECS[slot_key]
        cands, search_from = found.get(slot_key, []), center
        if chain and i > 0:
            # Walk mode: chain each stop near the last for a tight walkable route.
            search_from = anchor
            near = [c for c in cands if c.get("lat") is not None and c.get("lng") is not None
                    and _haversine_km(anchor[0], anchor[1], c["lat"], c["lng"]) * 1000 <= tconf["cluster"]]
            if not any((c.get("name") or "").lower() not in used for c in near) and left() > 3:
                near = [c for c in _gather(anchor, slot_key, price_pref, tconf["cluster"], dietary,
                                           *route(slot_key), at=at[slot_key])
                        if keep(c) and not too_casual(c, slot_key, vibe, group_type, interests)]
            cands = near or cands
        # Fun/hype only matters for what you DO — not for a restaurant or cafe.
        apply_fun = slot_key in ("activity", "leisure")
        pick = _pick(cands, used, search_from, penalty, vary, fancy, apply_fun,
                     interests, rng=rng, explore=surprise, taste=taste,
                     slot_key=slot_key, allowance=allow.get(slot_key))
        if not pick:
            if slot_key in late:
                skipped.append(slot["label"])      # ran out of time, not out of places
            continue
        used.add(pick["name"].lower())
        pools[pick["name"].lower()] = (cands, search_from, pick)
        if chain:
            anchor = (pick["lat"], pick["lng"])
        stops.append(stop_from(pick, slot_key))

    # Venues the user asked for BY NAME: research each against the same verified
    # sources the planner trusts (Yelp/Foursquare). Confirmed-real ones join the
    # plan as first-class stops — so the narrator may name them — and the rest
    # are reported back as unverified instead of being invented or ignored.
    unverified = []
    for req_name in (requested_venues or [])[:3]:
        if any(_name_matches(req_name, s["name"]) for s in stops):
            continue                            # planner already picked it
        if left() <= 0:
            unverified.append(req_name)            # out of time to research it
            continue
        pick = verify_requested_venue(req_name, lat, lng, max(radius, 8000))
        if not pick:
            unverified.append(req_name)
            continue
        if pick["name"].lower() in used:        # e.g. already on an earlier trip day
            continue
        used.add(pick["name"].lower())
        slot_key = _slot_for_categories(pick.get("categories"))
        stop = stop_from(pick, slot_key, requested=True)   # user asked for it — never trim it
        # It takes the place of the planned stop of the same kind (their bar, not a
        # second bar at midnight); otherwise it slots in where that kind goes.
        same = next((i for i, s in enumerate(stops) if not s.get("requested")
                     and _KIND_OF_STOP.get(s["slot"], s["slot"]) == _KIND_OF_STOP.get(slot_key, slot_key)), None)
        if same is not None:
            stop["slot"], stop["label"], stop["icon"] = (stops[same]["slot"], stops[same]["label"],
                                                         stops[same]["icon"])
            stops[same] = stop
            continue
        pos = next((i for i, s in enumerate(stops)
                    if SLOT_ORDER.get(s["slot"], 9) > SLOT_ORDER.get(slot_key, 9)),
                   len(stops))
        stops.insert(pos, stop)

    def total():
        return sum(s["est_cost"] for s in stops) * max(1, party_size)

    # Over budget (beyond 10%: estimates are rough)? First swap in cheaper places —
    # the swap that saves the most per bit of quality lost (a cheaper bar before a
    # hot-dog dinner); only then drop the least essential stop — a dessert/drink
    # before the activity, and never a free one (it saves nothing).
    limit = budget * 1.10 if budget else float("inf")

    def score_as(c, slot_key, frm):
        return _score(c, frm, penalty, fancy, slot_key in ("activity", "leisure"), interests,
                      taste, slot_key, allow.get(slot_key))

    for _ in range(3 * len(stops)):
        if total() <= limit:
            break
        best = None
        for i, s in enumerate(stops):
            pool = pools.get(s["name"].lower())
            if not pool or s.get("requested"):
                continue
            cands, frm, was = pool
            cheaper = [c for c in cands if _est_cost(c, s["slot"]) < s["est_cost"]]
            alt = _pick(cheaper, used, frm, penalty, fancy=fancy,
                        apply_fun=s["slot"] in ("activity", "leisure"), interests=interests,
                        taste=taste, slot_key=s["slot"], allowance=allow.get(s["slot"]))
            if not alt:
                continue
            saving = s["est_cost"] - _est_cost(alt, s["slot"])
            loss = max(0.05, score_as(was, s["slot"], frm) - score_as(alt, s["slot"], frm))
            if best is None or saving / loss > best[0]:
                best = (saving / loss, i, alt, (cands, frm, alt))
        if best is None:
            break
        _, i, alt, pool = best
        used.add(alt["name"].lower())
        pools[alt["name"].lower()] = pool
        stops[i] = stop_from(alt, stops[i]["slot"])

    DROP_ORDER = {"dessert": 0, "cafe": 1, "drinks": 1, "leisure": 1, "scenic": 2, "activity": 3}
    while stops and total() > limit and len(stops) > 2:
        droppable = [s for s in stops if s["slot"] not in ("dinner", "lunch")
                     and not s.get("requested") and s["est_cost"] > 0]
        if not droppable:
            break
        stops.remove(min(droppable, key=lambda s: (DROP_ORDER.get(s["slot"], 5), -s["est_cost"])))

    spread = route_km(stops)   # how spread out the plan ended up (for the UI / narration)

    # Live events happening near here (concerts, games, comedy) — surfaced as
    # options the guest can build around, not forced into the fixed stops.
    events, seen_events = [], set()      # the special day's own events first
    for e in _result(day_job, [], timeout=left()) + (_result(ev_job, [], timeout=left()) or []):
        k = re.sub(r"[^a-z0-9]", "", (e.get("name") or "").lower())
        if k and k not in seen_events:
            seen_events.add(k)
            events.append(e)
    events = events[:3]

    plan = {
        "vibe": vibe, "budget": budget, "party_size": party_size, "currency": "USD",
        "estimated_cost": total(), "center": {"lat": lat, "lng": lng},
        "walk_km": spread, "stops": stops, "start": start.isoformat(timespec="minutes"),
        "weather": (wx["summary"] if wx else None),
        "events": events,
        # names the user asked for that no verified source could confirm nearby
        "unverified_requests": unverified,
        # slots dropped because the time budget ran out (not for lack of places)
        "skipped": skipped,
    }
    if theme:
        plan["special_note"] = _theme_note(theme, stops)
    retime(plan)
    _narrate(plan, interests, group_type, time_of_day, dietary, wx, events, avoid)
    return plan


def _special_theme(lat, lng, slots, interests, avoid, no_alcohol):
    """(day, slot, what to lean towards) when today is a special day in the stored
    city and the plan has that kind of stop — unless they asked for something else
    there, ruled it out ("no bars"), or don't drink."""
    if not venue_index.in_city(lat, lng):
        return None
    day = special_days.active()
    theme = special_days.theme_for(day["key"]) if day and day["today"] else None
    if not theme or theme[0] not in slots or interest_term_for(theme[0], interests):
        return None
    if avoid_match({"name": theme[1], "categories": [theme[1]]}, avoid):
        return None
    if no_alcohol and theme[0] in ("leisure", "drinks"):
        return None
    return day, theme[0], theme[1]


def _theme_note(theme, stops) -> str | None:
    """"☘️ It's St. Patrick's Day, so drinks are at McVeigh's." — only if a stop fits."""
    day, slot_key, want = theme
    words = [want, *venue_index._TERM_ALIASES.get(want, ())]
    for s in stops:
        hay = (s["name"] + " " + " ".join(s.get("categories") or [])).lower()
        if s["slot"] == slot_key and any(w in hay for w in words):
            return f"{day['emoji']} It's {day['name']}, so your {SLOT_SPECS[slot_key]['label'].lower()} stop is {s['name']}."
    return None


def build_trip(*, days, lat, lng, budget, exclude=None, **opts):
    with time_itinerary_generation("multi_day"):
        days = max(1, min(days, 5))
        per_day = budget / days
        used, trip = set(exclude or ()), []
        seed = opts.pop("seed", None)
        opts["time_budget"] = max(8.0, TRIP_TIME_BUDGET / days)
        for d in range(days):
            plan = build_plan(lat=lat, lng=lng, budget=per_day, exclude=used,
                              seed=(seed + d if seed is not None else None), **opts)
            plan["day"] = d + 1
            for s in plan["stops"]:
                used.add(s["name"].lower())
            trip.append(plan)
        return trip


# ===================== PICKER: options + build-from-selection =================

def _to_option(c, slot_key):
    emoji, label = tag_for(c)
    return {
        "slot": slot_key, "name": c.get("name"),
        "lat": c.get("lat"), "lng": c.get("lng"),
        "rating": c.get("rating"), "review_count": c.get("review_count"),
        "price": c.get("price"), "categories": c.get("categories") or [],
        "address": c.get("address"), "image": c.get("image"),
        "url": c.get("url"), "source": c.get("source"),
        "est_cost": _est_cost(c, slot_key),
        "arousal": experience.arousal_for(c.get("categories")),
        "tag": emoji, "tag_label": label,
    }


def gather_options(slot_key, *, lat, lng, vibe=DEFAULT_VIBE, group_type=None,
                   interests="", dietary="", transport="any", avoid="", limit=6,
                   likes="", crowd_pref=0.0, energy=None, no_alcohol=False, radius_scale=1.0,
                   radius=None):
    """Ranked, tagged candidate venues for one category — the picker's menu."""
    tconf = TRANSPORT.get(transport, TRANSPORT["any"])
    price_pref = VIBE_PLANS.get(vibe, VIBE_PLANS[DEFAULT_VIBE])["price"]
    term_override, cat_override = route_for(slot_key, interests=interests, dietary=dietary,
                                            group_type=group_type, vibe=vibe, no_alcohol=no_alcohol)
    radius = radius or int(tconf["radius"] * (radius_scale or 1.0))   # an explicit radius wins
    cands = [c for c in _gather((lat, lng), slot_key, price_pref, radius, dietary, term_override, cat_override)
             if allowed(c, avoid, no_alcohol)]
    apply_fun = slot_key in ("activity", "leisure")
    fancy = vibe == "extravagant"
    ranked = sorted((c for c in cands if c.get("name")),
                    key=lambda c: _score(c, (lat, lng), tconf["penalty"], fancy, apply_fun, interests,
                                         {"likes": likes, "crowd_pref": crowd_pref, "energy": energy}),
                    reverse=True)
    seen, out = set(), []
    for c in ranked:
        n = c["name"].lower()
        if n in seen:
            continue
        seen.add(n)
        out.append(_to_option(c, slot_key))
        if len(out) >= limit:
            break
    return out


def option_sections(time_of_day, vibe, group_type):
    """Which category sections the picker shows, given the intent."""
    secs = []
    day = time_of_day in ("morning", "afternoon")
    if day:
        secs.append(("cafe", "Coffee & warm-up", "☕", "optional"))
    secs.append(("scenic", "Scenic & free", "🌇", "optional — beaches, parks, lookouts"))
    secs.append(("activity", "Activity", "🎡", "pick 1–2"))
    if day:
        secs.append(("lunch", "Lunch", "🥗", "pick 1"))
    secs.append(("dinner", "Dinner", "🍽️", "pick 1"))
    if group_type not in ("family", "kids"):
        secs.append(("leisure", "Drinks & vibes", "🎶", "optional"))
    secs.append(("dessert", "Sweet finish", "🍰", "optional"))
    return secs


def build_from_selection(selections, *, lat, lng, party_size=2, budget=0,
                         vibe=DEFAULT_VIBE, interests="", group_type=None,
                         time_of_day=None, dietary=""):
    """Assemble + sequence + narrate an itinerary from the user's picked venues."""
    stops = []
    for sel in selections:
        slot_key = sel.get("slot", "activity")
        spec = SLOT_SPECS.get(slot_key, SLOT_SPECS["activity"])
        stops.append({
            "slot": slot_key, "label": spec["label"], "icon": spec["icon"],
            "name": sel.get("name"), "lat": sel.get("lat"), "lng": sel.get("lng"),
            "rating": sel.get("rating"), "price": sel.get("price"),
            "address": sel.get("address"), "image": sel.get("image"),
            "url": sel.get("url"), "source": sel.get("source"),
            "est_cost": sel.get("est_cost") if sel.get("est_cost") is not None else _est_cost(sel, slot_key),
            "categories": sel.get("categories") or [],
            "arousal": sel.get("arousal") if sel.get("arousal") is not None else experience.arousal_for(sel.get("categories")),
        })
    stops.sort(key=lambda s: SLOT_ORDER.get(s["slot"], 5))
    total = sum(s["est_cost"] for s in stops) * max(1, party_size)
    spread = 0.0
    for a, b in zip(stops, stops[1:]):
        spread += _haversine_km(a["lat"], a["lng"], b["lat"], b["lng"])
    plan = {
        "vibe": vibe, "budget": budget or total, "party_size": party_size, "currency": "USD",
        "estimated_cost": total, "center": {"lat": lat, "lng": lng},
        "walk_km": round(spread, 1), "stops": stops, "weather": None, "events": [],
    }
    _narrate(plan, interests, group_type, time_of_day, dietary, None, None)
    return plan


_START_HINT = {"morning": "10:00 AM", "afternoon": "1:00 PM", "night": "8:00 PM"}


def _narrate(plan, interests, group_type, time_of_day, dietary, wx=None, events=None, avoid=""):
    stops = plan["stops"]
    if llm.available() and stops:
        # The highest-arousal stop is the emotional peak — narrate the day as a
        # build → peak → wind-down arc around it.
        peak_idx = max(range(len(stops)), key=lambda i: stops[i].get("arousal", 2.5))
        listing = "\n".join(
            f"{i+1}. " + (f"{s['time']} " if s.get("time") else "") + f"{s['label']} — {s['name']}"
            + (f" ({s['price']}, {s['rating']}★)" if s.get('rating') else "")
            + (f" [{', '.join(s['categories'][:2])}]" if s.get('categories') else "")
            + f"  ~${s['est_cost']}/pp"
            + (" [USER-REQUESTED — they asked for this place by name and it's "
               "verified; acknowledge naturally that it made the plan]" if s.get("requested") else "")
            + (" [PEAK — emotional high point of the day; write it with the most "
               "energy and anticipation]" if i == peak_idx else "")
            for i, s in enumerate(stops))
        ctx = ", ".join(filter(None, [
            f"group: {group_type}" if group_type else "",
            f"time: {time_of_day}" if time_of_day else "",
            f"dietary: {dietary}" if dietary else "",
            f"they specifically want: {interests}" if interests else "",
            f"they do NOT want: {avoid} — never present a stop as that thing" if avoid else "",
            f"weather today: {wx['summary']} (kept it indoors)" if wx and (wx.get("rainy") or wx.get("cold")) else "",
        ]))
        ev_line = ""
        if events:
            ev_line = "Live events near here tonight: " + "; ".join(
                f"{e['name']} at {e['venue']}" + (f" {e['time'][:5]}" if e.get('time') else "")
                for e in events if e.get("name"))
        start = _START_HINT.get(time_of_day, "6:00 PM")
        msg = [
            {"role": "system", "content":
             "You are Layer 3 of a strict pipeline: a formatting + narration engine, NOT a "
             "recommender or search engine. You are given a FIXED, ordered list of REAL venues "
             "(already retrieved from verified sources) and possibly a list of REAL events.\n"
             "ABSOLUTE RULES (zero tolerance):\n"
             "- Narrate ONLY the exact venues in the VENUES list. Never invent, add, swap, rename, "
             "or substitute any place, restaurant, bar, activity, or neighbourhood-as-destination.\n"
             "- Do NOT name any venue, business, or event that is not in the provided lists. Not in "
             "the intro, not in a description, not in the tip. If you're unsure a name was given to "
             "you, do not use it.\n"
             "- The Context line may quote the user mentioning or requesting places by name. Those "
             "are NOT venues given to you — the VENUES list is the ONLY source of venue names. If "
             "the user asks for a place that is not in VENUES, silently ignore that request: do not "
             "name it, do not add a stop for it, do not mention that it was skipped.\n"
             "- Do NOT modify names, ratings, categories, or prices.\n"
             "- Only reference an event if it appears in the EVENTS line, using its exact name/time.\n"
             "- Use your own knowledge ONLY for tone, timing, transitions and readability — never for "
             "facts about specific places.\n"
             "Return JSON: {\"intro\": str, \"stops\": [{\"desc\": str}, ...], \"tip\": str}.\n"
             "intro = 3-4 sentences about the day/group/vibe — set the scene and the arc of the "
             "night; mention NO venue names that aren't in the list (it's fine to name the listed "
             "ones or none).\n"
             "stops = one per venue IN ORDER (the times shown are fixed — keep to them): "
             "2-3 vivid sentences about THAT venue only — what it is, why it "
             "fits this group and budget, and one concrete thing to do or order there (drawn only "
             "from its listed categories/price, never invented specifics like dish names you can't "
             "know). If a stop is free (a park, beach, lookout), say so — free is a feature.\n"
             "tip = one closing line; may reference the listed venues/events by their exact names only.\n"
             "VARY YOUR ENERGY per stop to follow the day's emotional arc: calm, unhurried language "
             "for the early/low-key stops, building excitement and anticipation toward the stop marked "
             "[PEAK], then an easy, satisfied wind-down after it. (This shapes TONE only — it does not "
             "let you add, invent, or reorder any venue.)\n"
             "Confident, warm, concrete. No markdown."},
            {"role": "user", "content":
             f"Vibe: {plan['vibe']}. Budget: ${plan['budget']} for {plan['party_size']}. "
             f"Start around {start}. Context — {ctx or 'none given'}. "
             f"Stops span ~{plan['walk_km']}km total, all within the city.\nVENUES:\n{listing}"
             + (f"\n{ev_line}" if ev_line else "")},
        ]
        data = llm.chat_json(msg, timeout=12.0)   # template intro if the model is slow
        if data and isinstance(data.get("stops"), list) and data["stops"]:
            plan["intro"] = data.get("intro") or _template_intro(plan)
            plan["tip"] = data.get("tip")
            for s, extra in zip(stops, data["stops"]):
                if isinstance(extra, dict):
                    s["time"] = s.get("time") or extra.get("time")
                    s["why"] = extra.get("desc") or s.get("why")
            return
    # deterministic fallback
    plan["intro"] = _template_intro(plan)
    # No per-stop "why" without the narrator: the card already shows rating,
    # price and category, and repeating them read as clutter.
    for s in stops:
        s.pop("why", None)


def _template_intro(plan):
    n = len(plan["stops"])
    vibe = str(plan["vibe"]).replace("_", " ")
    return (f"{'An' if vibe[:1] in 'aeiou' else 'A'} {vibe} plan — {n} stop{'s' if n != 1 else ''}, "
            f"about ${plan['estimated_cost']} for {plan['party_size']}.")
