"""Itinerary planner — turns budget + vibe + context into an ordered plan of real
nearby places. Optimises for: stops CLOSE together (no crossing town) and best value
(great rating for the price). Uses Yelp (ratings/prices)
when keyed, falls back to OpenStreetMap. An optional LLM writes the narration.
"""
import math
import random
import re

from app.services import foursquare
from app.services import geocoding
from app.services import google_places
from app.services import yelp
from app.services.osm_places import fetch_places
from app.services import llm
from app.services import weather as weather_svc
from app.services import events as events_svc
from app.services import experience
from app.metrics import time_itinerary_generation

# Indoor-only activity set for wet/cold days.
INDOOR_ACTIVITIES = "arcades,escapegames,bowling,poolhalls,lasertag,karaoke,trampoline,museums,aquariums"

PRICE_COST = {"$": 15, "$$": 30, "$$$": 65, "$$$$": 110}
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


def _haversine_km(a_lat, a_lng, b_lat, b_lng):
    R = 6371
    dlat = math.radians(b_lat - a_lat)
    dlng = math.radians(b_lng - a_lng)
    x = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(a_lat)) * math.cos(math.radians(b_lat)) * math.sin(dlng / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(x))


def _est_cost(cand, slot):
    return PRICE_COST.get(cand.get("price"), slot["base"])


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


def avoid_match(c, avoid):
    """True when a candidate is something the user said NOT to do — matched
    against its name and categories, singular/plural tolerant ('parks'→'park')."""
    toks = _tokens(avoid)
    if not toks:
        return False
    hay = (c.get("name") or "").lower() + " " + " ".join(
        str(x).lower() for x in (c.get("categories") or []))
    return any(t.rstrip("s") in hay for t in toks)


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


def _gather(anchor, slot_key, price_pref, radius, dietary, term_override=None, cat_override=None):
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


def _score(c, anchor, penalty, fancy=False, apply_fun=False, interests=""):
    """Higher is better. Balances rating, closeness, price preference (cheaper for
    value vibes / pricier when they want fancy), and — for activity/leisure
    slots — a fun/hype signal that's independent of star rating."""
    rating = c.get("rating") or 3.6                      # neutral for unrated OSM
    dist = _haversine_km(anchor[0], anchor[1], c["lat"], c["lng"])
    level = PRICE_LEVEL.get(c.get("price"), 2)
    score = rating * 2.0 - dist * penalty
    score += _interest_bonus(c, interests)   # what they ASKED for outranks generic hits
    score += (c.get("popularity") or 0) * POPULARITY_WEIGHT   # Foursquare foot-traffic trendiness
    score += (level - 1) * 0.6 if fancy else -(level - 1) * 0.5   # tier preference
    score += math.log10((c.get("review_count") or 0) + 1) * BUZZ_WEIGHT   # buzz / popularity
    if apply_fun:
        # Excitement matters here, not just satisfaction — so a high-energy
        # experience can out-rank a better-reviewed but sleepy one.
        score += experience.fun_factor_for(c.get("categories")) * FUN_WEIGHT
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
          interests="", rng=random, explore=False):
    ranked = sorted(
        (c for c in cands if c.get("name") and c["name"].lower() not in used),
        key=lambda c: _score(c, anchor, penalty, fancy, apply_fun, interests), reverse=True)
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
               requested_venues=None, seed=None, surprise=False):
    with time_itinerary_generation("single_day"):
        return _build_plan(
            lat=lat, lng=lng, budget=budget, vibe=vibe, party_size=party_size,
            transport=transport, time_of_day=time_of_day, group_type=group_type,
            dietary=dietary, interests=interests, avoid=avoid, radius=radius,
            exclude=exclude, vary=vary, target_stops=target_stops,
            requested_venues=requested_venues, seed=seed, surprise=surprise,
        )


def _build_plan(*, lat, lng, budget, vibe=DEFAULT_VIBE, party_size=2, transport="any",
                time_of_day=None, group_type=None, dietary="", interests="", avoid="",
                radius=None, exclude=None, vary=False, target_stops=4,
                requested_venues=None, seed=None, surprise=False):
    # A caller-supplied seed makes the draw reproducible; each new seed is a
    # genuinely different roll. Surprise mode implies variety.
    rng = random.Random(seed) if seed is not None else random
    vary = vary or surprise
    vibe = vibe if vibe in VIBE_PLANS else DEFAULT_VIBE
    price_pref = VIBE_PLANS[vibe]["price"]
    tconf = TRANSPORT.get(transport, TRANSPORT["any"])
    radius = radius or tconf["radius"]
    used = set(exclude or ())
    slots = _slots_for(vibe, time_of_day, group_type, target_stops)

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

    center = (lat, lng)
    chain = tconf.get("chain", False)   # only walking chains stops tightly together
    fancy = vibe == "extravagant"       # bias picks toward pricier/upscale venues
    anchor = center
    stops = []
    for i, slot_key in enumerate(slots):
        slot = SLOT_SPECS[slot_key]
        # route the search: group-aware fun for activities, cuisine for meals.
        term_override = cat_override = None
        cz = cuisine_term(interests, dietary)
        if slot_key == "activity":
            cat_override = INDOOR_ACTIVITIES if bad_weather else activity_categories(group_type, vibe, interests)
            if interests and not cz:
                term_override = interests          # e.g. "escape room"
        elif slot_key in ("dinner", "lunch") and cz:
            term_override = cz                     # e.g. "korean bbq"
        elif slot_key not in ("dinner", "lunch"):
            # route each stated interest to the slot it belongs to — hookah/rooftop
            # shape the leisure stop, gelato the dessert stop, beach the scenic stop.
            t = interest_term_for(slot_key, interests)
            if t:
                term_override = "nightclub" if t in ("clubbing", "club", "dance", "dj", "rave") else t
        # Walk mode: chain each stop near the last for a tight walkable route.
        # City mode: search the whole central-city radius from the centre, so you
        # get the best spots across town (still bounded to the city by `radius`).
        if chain:
            search_from = anchor
            r = tconf["cluster"] if i > 0 else radius
        else:
            search_from = center
            r = radius
        # Fun/hype only matters for what you DO — not for a restaurant or cafe.
        apply_fun = slot_key in ("activity", "leisure")
        cands = [c for c in _gather(search_from, slot_key, price_pref, r, dietary, term_override, cat_override)
                 if not avoid_match(c, avoid)]
        pick = _pick(cands, used, search_from, tconf["penalty"], vary, fancy, apply_fun,
                     interests, rng=rng, explore=surprise)
        if not pick and r < radius:
            cands = [c for c in _gather(search_from, slot_key, price_pref, radius, dietary, term_override, cat_override)
                     if not avoid_match(c, avoid)]
            pick = _pick(cands, used, search_from, tconf["penalty"], vary, fancy, apply_fun,
                         interests, rng=rng, explore=surprise)
        if not pick:
            continue
        used.add(pick["name"].lower())
        if chain:
            anchor = (pick["lat"], pick["lng"])
        stops.append({
            "slot": slot_key, "label": slot["label"], "icon": slot["icon"],
            "name": pick["name"], "lat": pick["lat"], "lng": pick["lng"],
            "rating": pick.get("rating"), "price": pick.get("price"),
            "address": pick.get("address"), "image": pick.get("image"),
            "url": pick.get("url"), "source": pick.get("source"),
            "est_cost": _est_cost(pick, slot), "categories": pick.get("categories") or [],
            # arousal (energy) drives the narration's emotional arc downstream.
            "arousal": experience.arousal_for(pick.get("categories")),
        })

    # Venues the user asked for BY NAME: research each against the same verified
    # sources the planner trusts (Yelp/Foursquare). Confirmed-real ones join the
    # plan as first-class stops — so the narrator may name them — and the rest
    # are reported back as unverified instead of being invented or ignored.
    unverified = []
    for req_name in (requested_venues or [])[:3]:
        if any(_name_matches(req_name, s["name"]) for s in stops):
            continue                            # planner already picked it
        pick = verify_requested_venue(req_name, lat, lng, max(radius, 8000))
        if not pick:
            unverified.append(req_name)
            continue
        if pick["name"].lower() in used:        # e.g. already on an earlier trip day
            continue
        used.add(pick["name"].lower())
        slot_key = _slot_for_categories(pick.get("categories"))
        slot = SLOT_SPECS[slot_key]
        stop = {
            "slot": slot_key, "label": slot["label"], "icon": slot["icon"],
            "name": pick["name"], "lat": pick["lat"], "lng": pick["lng"],
            "rating": pick.get("rating"), "price": pick.get("price"),
            "address": pick.get("address"), "image": pick.get("image"),
            "url": pick.get("url"), "source": pick.get("source"),
            "est_cost": _est_cost(pick, slot), "categories": pick.get("categories") or [],
            "arousal": experience.arousal_for(pick.get("categories")),
            "requested": True,                  # user asked for it — never trim it
        }
        pos = next((i for i, s in enumerate(stops)
                    if SLOT_ORDER.get(s["slot"], 9) > SLOT_ORDER.get(slot_key, 9)),
                   len(stops))
        stops.insert(pos, stop)

    def total():
        return sum(s["est_cost"] for s in stops) * max(1, party_size)

    # Only trim on a real overage (estimates are rough), and drop the least
    # essential first — a dessert/drink before ever cutting the activity, so the
    # plan keeps its activity + food + leisure shape.
    DROP_ORDER = {"dessert": 0, "cafe": 1, "drinks": 1, "leisure": 1, "scenic": 2, "activity": 3}
    while stops and total() > budget * 1.15 and len(stops) > 2:
        droppable = [s for s in stops
                     if s["slot"] not in ("dinner", "lunch") and not s.get("requested")]
        if not droppable:
            break
        stops.remove(min(droppable, key=lambda s: DROP_ORDER.get(s["slot"], 5)))

    # how spread out the plan ended up (for the UI / narration)
    spread = 0.0
    for a, b in zip(stops, stops[1:]):
        spread += _haversine_km(a["lat"], a["lng"], b["lat"], b["lng"])

    # Live events happening near here (concerts, games, comedy) — surfaced as
    # options the guest can build around, not forced into the fixed stops.
    events = []
    if events_svc.available():
        cls = events_svc.classification_for(vibe, interests)
        events = events_svc.search_events(lat, lng, radius_km=25, size=8, classification=cls)[:3]

    plan = {
        "vibe": vibe, "budget": budget, "party_size": party_size, "currency": "USD",
        "estimated_cost": total(), "center": {"lat": lat, "lng": lng},
        "walk_km": round(spread, 1), "stops": stops,
        "weather": (wx["summary"] if wx else None),
        "events": events,
        # names the user asked for that no verified source could confirm nearby
        "unverified_requests": unverified,
    }
    _narrate(plan, interests, group_type, time_of_day, dietary, wx, events, avoid)
    return plan


def build_trip(*, days, lat, lng, budget, exclude=None, **opts):
    with time_itinerary_generation("multi_day"):
        days = max(1, min(days, 5))
        per_day = budget / days
        used, trip = set(exclude or ()), []
        seed = opts.pop("seed", None)
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
        "est_cost": _est_cost(c, SLOT_SPECS[slot_key]),
        "arousal": experience.arousal_for(c.get("categories")),
        "tag": emoji, "tag_label": label,
    }


def gather_options(slot_key, *, lat, lng, vibe=DEFAULT_VIBE, group_type=None,
                   interests="", dietary="", transport="any", avoid="", limit=6):
    """Ranked, tagged candidate venues for one category — the picker's menu."""
    tconf = TRANSPORT.get(transport, TRANSPORT["any"])
    price_pref = VIBE_PLANS.get(vibe, VIBE_PLANS[DEFAULT_VIBE])["price"]
    term_override = cat_override = None
    cz = cuisine_term(interests, dietary)
    if slot_key == "activity":
        cat_override = activity_categories(group_type, vibe, interests)
        if interests and not cz:
            term_override = interests
    elif slot_key in ("dinner", "lunch") and cz:
        term_override = cz
    elif slot_key not in ("dinner", "lunch"):
        t = interest_term_for(slot_key, interests)
        if t:
            term_override = "nightclub" if t in ("clubbing", "club", "dance", "dj", "rave") else t
    cands = [c for c in _gather((lat, lng), slot_key, price_pref, tconf["radius"], dietary, term_override, cat_override)
             if not avoid_match(c, avoid)]
    apply_fun = slot_key in ("activity", "leisure")
    fancy = vibe == "extravagant"
    ranked = sorted((c for c in cands if c.get("name")),
                    key=lambda c: _score(c, (lat, lng), tconf["penalty"], fancy, apply_fun, interests),
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
            "est_cost": sel.get("est_cost") if sel.get("est_cost") is not None else _est_cost(sel, spec),
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
            f"{i+1}. {s['label']} — {s['name']}"
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
             "Return JSON: {\"intro\": str, \"stops\": [{\"time\": str, \"desc\": str}, ...], \"tip\": str}.\n"
             "intro = 3-4 sentences about the day/group/vibe — set the scene and the arc of the "
             "night; mention NO venue names that aren't in the list (it's fine to name the listed "
             "ones or none).\n"
             "stops = one per venue IN ORDER: a realistic clock time (from the start time, allowing "
             "travel + dwell) and 2-3 vivid sentences about THAT venue only — what it is, why it "
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
        data = llm.chat_json(msg)
        if data and isinstance(data.get("stops"), list) and data["stops"]:
            plan["intro"] = data.get("intro") or _template_intro(plan)
            plan["tip"] = data.get("tip")
            for s, extra in zip(stops, data["stops"]):
                if isinstance(extra, dict):
                    s["time"] = extra.get("time")
                    s["why"] = extra.get("desc") or s.get("why")
            return
    # deterministic fallback
    plan["intro"] = _template_intro(plan)
    for s in stops:
        bits = []
        if s.get("rating"):
            bits.append(f"{s['rating']}★" + (f" · {s['price']}" if s.get("price") else ""))
        if s.get("categories"):
            bits.append(s["categories"][0])
        s["why"] = " · ".join(bits) or f"A solid {s['label'].lower()} nearby."


def _template_intro(plan):
    n = len(plan["stops"])
    return (f"A {plan['vibe']} plan — {n} stop{'s' if n != 1 else ''}, "
            f"about ${plan['estimated_cost']} for {plan['party_size']}.")
