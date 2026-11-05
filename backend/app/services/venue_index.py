"""The stored venue list: which planner stop a place can be, and finding
candidates near a point.

Rows come from scripts/load_venues.py (Overture Maps places, Wikidata for how
well known a place is, OpenStreetMap for opening hours). Plans read from here so
they never wait on a live map search; an area with no stored venues returns None
and the planner falls back to the live search.
"""
import math
import re
import time
from datetime import datetime

from sqlalchemy import String, func, or_, select

from app.database import SessionLocal
from app.models import Area, Venue
from app.services.opening_hours import is_open

# Cities the loader covers: (south, west, north, east), and their time zones.
CITIES = {"toronto": (43.58, -79.64, 43.86, -79.11)}
CITY_TZ = {"toronto": "America/Toronto"}

# ---- Overture category → planner stops ------------------------------------------
_RESTAURANT_EXTRA = {"diner", "gastropub", "bistro", "delicatessen", "tapas_bar", "fast_food_restaurant"}
_LUNCH_ONLY = {"sandwich_shop", "bagel_shop"}
_DESSERT = {"dessert_shop", "ice_cream_shop", "gelato_shop", "frozen_yogurt_shop", "donut_shop",
            "cupcake_shop", "chocolatier", "bakery", "macaron_shop", "pie_shop", "shaved_ice_shop",
            "bubble_tea_shop", "creperie", "patisserie", "cake_shop"}
_CAFE = {"coffee_shop", "cafe", "tea_room", "bubble_tea_shop", "coffee_roastery", "hong_kong_style_cafe",
         "bakery", "bagel_shop", "internet_cafe"}
_LEISURE = {"music_venue", "jazz_and_blues_venue", "comedy_club", "karaoke_venue", "salsa_club", "cabaret",
            "dance_club", "pool_billiards", "hookah_bar", "gastropub", "tapas_bar"}
_LEISURE_ROOTS = {"alcoholic_beverage_venue", "nightlife_venue"}
_NOT_LEISURE = {"airport_lounge", "winery", "distillery", "adult_entertainment_venue", "strip_club",
                "erotic_massage_venue", "casino", "betting_center", "lottery_vendor", "bingo_hall"}
_ACTIVITY = {"art_gallery", "aquarium", "zoo", "petting_zoo", "wildlife_sanctuary", "amusement_park",
             "arcade", "escape_room", "bowling_alley", "paintball", "virtual_reality_center", "planetarium",
             "observatory", "movie_theater", "theatre_venue", "performing_arts_venue", "opera_and_ballet",
             "circus_venue", "haunted_house", "ice_skating_rink", "skating_rink", "archery_range",
             "driving_range", "farmers_market", "public_market", "night_market", "castle", "pool_billiards",
             "karaoke_venue", "comedy_club", "rock_climbing_spot", "trampoline_park", "laser_tag_arena",
             "go_kart_track", "mini_golf_course", "axe_throwing", "water_park", "science_museum"}
_ACTIVITY_WORDS = ("museum", "kart", "mini_golf", "minigolf", "climbing", "trampoline", "laser_tag", "axe")
_SCENIC = {"park", "beach", "botanical_garden", "nature_reserve", "lookout", "pier", "quay", "marina",
           "island", "waterfall", "recreational_trail_or_path", "lighthouse", "garden", "scenic_point"}
# Only worth a stop when they're well known. (Not "historic_site": in Overture that's
# mostly plaques, office towers and whole districts. Famous ones arrive from
# Wikidata with a real type instead — see WIKIDATA_TYPES.)
_NOTABLE_ONLY = {"tourist_attraction", "attraction"}

# Wikidata "instance of" labels → planner stops, for well-known places Overture
# doesn't list as stops (CN Tower, Distillery District, Kensington Market…). A type
# counts when it is the word or ends with it ("art museum" → museum); activity is
# checked first, so "amusement park" isn't scenic.
WIKIDATA_TYPES = {
    "activity": ["observation tower", "museum", "zoo", "aquarium", "amusement park", "theme park",
                 "water park", "castle", "fort", "market", "concert hall", "theatre", "theater",
                 "planetarium", "science centre", "science center", "art gallery", "performing arts center",
                 "theatre building", "performance hall"],
    "scenic": ["park", "island", "island group", "square", "beach", "botanical garden", "garden",
               "neighborhood", "warehouse district", "historic district", "waterfront", "boardwalk",
               "promenade", "lookout"],
}
_WD_NOT_STOP = ("station", "skyscraper", "office", "electoral", "school", "university", "college",
                "hospital", "street", "road", "car park", "business park", "industrial park", "parking",
                "stock", "cemetery", "company", "church", "residential")
_WD_NEVER = ("adult",)   # rules the whole place out, whatever its other types


def _wd_slot(t: str) -> str | None:
    t = t.lower()
    if any(bad in t for bad in _WD_NOT_STOP):
        return None
    for slot, words in WIKIDATA_TYPES.items():
        if any(t == w or t.endswith(" " + w) for w in words):
            return slot
    return None


def wikidata_slots(types: list[str]) -> list[str]:
    """Planner stops for a Wikidata place from its types ([] = not a stop: stations, offices…)."""
    if any(bad in t.lower() for t in types for bad in _WD_NEVER):
        return []
    return list(dict.fromkeys(s for s in map(_wd_slot, types) if s))


def slots_for(primary: str | None, hierarchy: list[str] | None, notable: bool = False) -> list[str]:
    """Planner stops a place with this Overture category can be (may be empty)."""
    p, hier = primary or "", list(hierarchy or [])
    out: list[str] = []
    if (hier[:2] == ["food_and_drink", "restaurant"]) or p in _RESTAURANT_EXTRA:
        out += ["dinner", "lunch"]
    if p in _LUNCH_ONLY:
        out.append("lunch")
    if p in _CAFE:
        out.append("cafe")
    if p in _DESSERT:
        out.append("dessert")
    if p not in _NOT_LEISURE and (p in _LEISURE or any(h in _LEISURE_ROOTS for h in hier)):
        out += ["leisure", "drinks"]
    if p in _ACTIVITY or any(w in p for w in _ACTIVITY_WORDS):
        out.append("activity")
    if p in _SCENIC:
        out.append("scenic")
    if notable and p in _NOTABLE_ONLY:
        out.append("activity")
    return list(dict.fromkeys(out))


def readable(category: str | None) -> str:
    return (category or "").replace("_", " ").strip()


# ---- finding candidates ----------------------------------------------------------
# Planner activity aliases (from planner.activity_categories) → words in our categories.
_ALIAS_WORDS = {"arcades": "arcade", "escapegames": "escape", "gokarts": "kart", "lasertag": "laser",
                "paintball": "paintball", "axethrowing": "axe", "minigolf": "mini golf", "bowling": "bowl",
                "trampoline": "trampoline", "karaoke": "karaoke", "aquariums": "aquarium",
                "galleries": "galler", "observatories": "observ", "wineries": "winer",
                "museums": "museum", "zoos": "zoo", "amusementparks": "amusement", "poolhalls": "billiard",
                "rafting": "raft", "boattours": "boat", "beaches": "beach", "lakes": "lake",
                "waterparks": "water park", "marinas": "marina"}

NOTABILITY_WEIGHT = 0.5   # × log2(1 + Wikipedia editions): CN Tower ≈ +3.2, a 1-edition park ≈ +0.5
CHAIN_PENALTY = 1.2       # meals/drinks/dessert: a local place over a chain branch
FAST_FOOD_DINNER = 2.5    # fast food isn't dinner unless asked for
KIND_MATCH = 2.0          # the kinds of activity this group/wish is routed to (arcades, museums…)
_FAST_OK = re.compile(r"\b(fast food|burger|cheap|quick|fries|wings|shawarma|pizza|comfort|mcdonald'?s?)\b")
# Fast-food chains, by name: Overture sometimes lists a McDonald's as a plain
# "burger restaurant" with no brand, which would dodge both penalties.
_FAST_FOOD_BRANDS = re.compile(
    r"\b(mcdonald'?s|burger king|wendy'?s|kfc|kentucky fried|popeyes|taco bell|a&w|harvey'?s|subway|"
    r"tim hortons|pizza pizza|domino'?s|pizza hut|little caesars|dairy queen|mary brown'?s|five guys|"
    r"chick-fil-a|papa john'?s|pizzaville|pizza nova|241 pizza|quesada|osmow'?s|mr\.? sub|new york fries|"
    r"arby'?s|carl'?s jr|church'?s (?:texas )?chicken|wingstop|jollibee|panda express|manchu wok|"
    r"thai express|krispy kreme|cinnabon|coffee time|country style|chipotle|freshii|booster juice)\b", re.I)


def is_fast_food(c: dict) -> bool:
    """A fast-food place, by its flag, its category, or a fast-food chain's name."""
    return (bool(c.get("fast_food")) or bool(_FAST_FOOD_BRANDS.search(c.get("name") or ""))
            or any("fast food" in str(x).lower() for x in c.get("categories") or []))

# Words in a wish that describe a mood, not a thing to search names for
# ("something more active" mustn't narrow to places named "Moretti").
_FILLER = set("""the and with for bar restaurant place places spot spots food something anything
more less else different another other new better nice good great fun cool instead some thing
kind like active sporty energetic physical exciting chill relaxing relaxed quiet lively fancy
fancier cheap cheaper upscale casual""".split())

# What people say → what the categories call it ("nightclub" is "dance club" in Overture).
_TERM_ALIASES = {"nightclub": ("dance club",), "nightclubs": ("dance club",), "clubbing": ("dance club",),
                 "club": ("dance club",), "clubs": ("dance club",), "rave": ("dance club",)}

# Concert halls and theatres are for a show (the events list), not a drinks stop. Judged
# by the main category (every music venue and comedy club also has the parent
# "performing arts venue"), or by a hall's name unless it's also listed as a bar.
_SHOW_PRIMARY = re.compile(r"performing arts|concert hall|opera|theat(?:re|er)|stadium|arena")
_SHOW_NAME = re.compile(r"\b(hall|amphithea(?:tre|ter)|arena|stadium|centre for the performing arts|"
                        r"opera house|theatre|theater)\b", re.I)
_DRINK_WORDS = re.compile(r"\b(bar|pub|lounge|club|tavern|brewery|karaoke)\b")


def is_show_venue(name: str, categories) -> bool:
    cats = [str(c).lower() for c in categories or []]
    if cats and _SHOW_PRIMARY.search(cats[0]):
        return True
    return bool(_SHOW_NAME.search(name or "")) and not _DRINK_WORDS.search(" ".join(cats))
# Places like these keep daytime hours; without listed hours, assume roughly these
# (no art gallery at 9:45 PM).
_DAYTIME = re.compile(r"museum|gallery|zoo|farmers market|public market|historic|library|botanical")
DAYTIME_HOURS = "10:00-17:30"

_loaded_cache: dict[tuple[int, int], tuple[float, bool]] = {}


def _box(lat: float, lng: float, radius_m: float):
    dlat = radius_m / 111_000
    dlng = radius_m / (111_000 * max(0.2, math.cos(math.radians(lat))))
    return lat - dlat, lat + dlat, lng - dlng, lng + dlng


def _km(a_lat, a_lng, b_lat, b_lng):
    dlat, dlng = math.radians(b_lat - a_lat), math.radians(b_lng - a_lng)
    x = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(a_lat)) * math.cos(math.radians(b_lat)) * math.sin(dlng / 2) ** 2)
    return 2 * 6371 * math.asin(math.sqrt(x))


def covers(lat: float, lng: float) -> bool:
    """Does the stored list have anything around this point? Cached ~10 min per ~10 km cell."""
    cell = (round(lat * 10), round(lng * 10))
    hit = _loaded_cache.get(cell)
    if hit and time.monotonic() - hit[0] < 600:
        return hit[1]
    s, n, w, e = _box(lat, lng, 5000)
    try:
        with SessionLocal() as db:
            found = db.scalar(select(func.count()).select_from(
                select(Venue.id).where(Venue.lat.between(s, n), Venue.lng.between(w, e)).limit(1).subquery())) > 0
    except Exception:
        found = False
    _loaded_cache[cell] = (time.monotonic(), found)
    return found


def _row_dict(v: Venue) -> dict:
    return {"source": "overture", "id": v.id, "name": v.name, "lat": v.lat, "lng": v.lng,
            "rating": None, "review_count": None, "price": None,
            "categories": list(v.categories or [v.category]), "address": v.address, "image": v.image or None,
            "url": v.website, "notability": v.notability, "chain": v.chain, "fast_food": v.fast_food,
            "hours": v.hours}


def candidates(slot_key: str, lat: float, lng: float, radius_m: float, *, term: str | None = None,
               cats: str | None = None, at: datetime | None = None, limit: int = 300) -> list[dict] | None:
    """Stored places that can be `slot_key` near (lat, lng), each with a `boost` for
    the planner's ranking. None when this area isn't in the stored list."""
    if not covers(lat, lng):
        return None
    s, n, w, e = _box(lat, lng, radius_m)
    near = Venue.lat.between(s, n), Venue.lng.between(w, e), Venue.slots.contains([slot_key])
    dist2 = (Venue.lat - lat) * (Venue.lat - lat) + (Venue.lng - lng) * (Venue.lng - lng) * 0.53
    words = [x for x in re.findall(r"[a-z]{3,}", (term or "").lower()) if x not in _FILLER]
    words = [p for x in words for p in (x, *_TERM_ALIASES.get(x, ()))]
    with SessionLocal() as db:
        rows = {v.id: v for v in db.scalars(select(Venue).where(*near).order_by(dist2).limit(limit))}
        # the best-known ones in range, even if not the nearest
        rows.update({v.id: v for v in db.scalars(
            select(Venue).where(*near, Venue.notability > 0).order_by(Venue.notability.desc()).limit(60))})
        if words:   # what they asked for ("sushi", "escape room"), wherever it is in range
            like = [or_(Venue.name.ilike(f"%{x}%"), func.cast(Venue.categories, String).ilike(f"%{x}%"))
                    for x in words]
            rows.update({v.id: v for v in db.scalars(select(Venue).where(*near, or_(*like)).limit(150))})
    out = []
    alias = [_ALIAS_WORDS.get(a.strip(), a.strip()) for a in (cats or "").split(",") if a.strip()]
    for v in rows.values():
        c = _row_dict(v)
        if _km(lat, lng, v.lat, v.lng) * 1000 > radius_m:
            continue
        kinds = " ".join(c["categories"]).lower()
        if slot_key in ("leisure", "drinks") and is_show_venue(v.name, c["categories"]):
            continue
        hay = v.name.lower() + " " + kinds
        c["term_hit"] = bool(words) and any(x in hay for x in words)
        c["fast_food"] = is_fast_food(c)
        c["chain"] = bool(v.chain) or c["fast_food"]
        boost = NOTABILITY_WEIGHT * math.log2(1 + (v.notability or 0))
        if c["chain"] and slot_key in ("dinner", "lunch", "cafe", "dessert", "leisure", "drinks"):
            boost -= CHAIN_PENALTY
        if c["fast_food"] and slot_key in ("dinner", "lunch") and not _FAST_OK.search(term or ""):
            boost -= FAST_FOOD_DINNER if slot_key == "dinner" else 1.0
        if v.website:
            boost += 0.3
        if alias and any(a in hay for a in alias):
            boost += KIND_MATCH
        if at is not None:
            open_now = listed = is_open(v.hours, at)
            if open_now is None and _DAYTIME.search(kinds):
                open_now = is_open(DAYTIME_HOURS, at)  # no (readable) hours: assume daytime
            if open_now is False:
                continue                     # closed at the planned time
            boost += 0.2 if listed else -0.2           # listed as open beats unknown
        c["boost"] = round(boost, 2)
        out.append(c)
    if words and any(c["term_hit"] for c in out):
        out = [c for c in out if c["term_hit"]]      # like a real search: the term narrows it
    return out


def in_city(lat, lng) -> bool:
    """Is this point inside a city the stored list covers?"""
    return city_of(lat, lng) is not None


def city_of(lat, lng) -> str | None:
    if lat is None or lng is None:
        return None
    return next((c for c, (s, w, n, e) in CITIES.items() if s <= lat <= n and w <= lng <= e), None)


def tz_for(lat, lng, fallback: str | None = None) -> str | None:
    """The time zone of a place we know ("tonight" in Toronto is Toronto's night,
    whatever zone the phone is in); else the caller's."""
    return CITY_TZ.get(city_of(lat, lng) or "", fallback)


def find_area(name: str, near: tuple[float, float] | None = None) -> tuple[float, float] | None:
    """Centre of a neighbourhood or well-known place (see find_place); None if not known."""
    found = find_place(name, near)
    return found[:2] if found else None


def find_place(name: str, near: tuple[float, float] | None = None) -> tuple[float, float, str] | None:
    """(lat, lng, kind) of a neighbourhood ("Kensington Market": kind "neighborhood")
    or a well-known place ("CN Tower": kind "venue") from the stored list; None if
    not known. Partial names and lesser-known places count only for someone already
    in the city, so "Victoria" typed in BC doesn't land on Toronto's Victoria Village."""
    q = (name or "").strip()
    if len(q) < 3:
        return None
    local = bool(near) and in_city(*near)
    try:
        with SessionLocal() as db:
            areas = db.scalars(select(Area).where(func.lower(Area.name) == q.lower())).all()
            if not areas and local:
                areas = db.scalars(select(Area).where(Area.name.ilike(f"%{q}%"))
                                   .order_by(func.length(Area.name)).limit(5)).all()
            pts = [(a.lat, a.lng, a.kind) for a in areas]
            if not pts:
                v = db.scalars(select(Venue).where(func.lower(Venue.name) == q.lower(),
                                                   Venue.notability >= (0 if local else 5))
                               .order_by(Venue.notability.desc()).limit(3)).all()
                pts = [(x.lat, x.lng, "venue") for x in v]
    except Exception:
        return None
    if not pts:
        return None
    if local:
        return min(pts, key=lambda p: _km(near[0], near[1], p[0], p[1]))
    return pts[0]



# ---- nightlife: the club index ------------------------------------------------------
# Kinds of night spot, checked in order (a gay bar is LGBTQ+ before it's a bar).
NIGHT_KINDS = [
    ("clubs", "Clubs", re.compile(r"dance club|night ?club|salsa club|cabaret")),
    ("lgbtq", "LGBTQ+", re.compile(r"gay bar|lgbt")),
    ("music", "Live music", re.compile(r"music venue|jazz|blues")),
    ("comedy", "Comedy & karaoke", re.compile(r"comedy|karaoke")),
    ("lounges", "Lounges", re.compile(r"lounge|hookah|cocktail bar|wine bar|speakeasy")),
    ("bars", "Bars & pubs", re.compile(r"\bpub\b|\bbar\b|brewery|tavern|beer|taproom")),
]
NIGHT_KIND_LABELS = {k: label for k, label, _ in NIGHT_KINDS}
_areas_cache: tuple[float, list[tuple[str, float, float]]] | None = None


def night_kind(name: str, categories) -> str | None:
    if is_show_venue(name, categories):
        return None
    kinds = " ".join(str(c).lower() for c in categories or [])
    return next((k for k, _, rx in NIGHT_KINDS if rx.search(kinds)), None)


def area_of(lat: float, lng: float) -> str | None:
    """The neighbourhood a point is in (the nearest stored one, within ~1.5 km)."""
    global _areas_cache
    if _areas_cache is None or time.monotonic() - _areas_cache[0] > 3600:
        try:
            with SessionLocal() as db:
                rows = db.execute(select(Area.name, Area.lat, Area.lng)
                                  .where(Area.kind.in_(("neighborhood", "microhood")))).all()
        except Exception:
            rows = []
        _areas_cache = (time.monotonic(), [tuple(r) for r in rows])
    best = min(_areas_cache[1], key=lambda a: _km(lat, lng, a[1], a[2]), default=None)
    return best[0] if best and _km(lat, lng, best[1], best[2]) <= 1.5 else None


def nightlife(lat: float, lng: float, radius_m: float, *, limit: int = 400) -> list[dict] | None:
    """Night spots near a point (clubs, lounges, live music, bars…), each with its
    kind, neighbourhood and distance. None when this area isn't in the stored list."""
    if not covers(lat, lng):
        return None
    s, n, w, e = _box(lat, lng, radius_m)
    night = or_(Venue.slots.contains(["leisure"]), Venue.slots.contains(["drinks"]))
    dist2 = (Venue.lat - lat) * (Venue.lat - lat) + (Venue.lng - lng) * (Venue.lng - lng) * 0.53
    with SessionLocal() as db:
        rows = db.scalars(select(Venue).where(Venue.lat.between(s, n), Venue.lng.between(w, e), night)
                          .order_by(dist2).limit(limit)).all()
    out = []
    for v in rows:
        kind = night_kind(v.name, v.categories or [v.category])
        km = _km(lat, lng, v.lat, v.lng)
        if not kind or km * 1000 > radius_m:
            continue
        c = _row_dict(v)
        c.update(kind=kind, area=area_of(v.lat, v.lng), distance_km=round(km, 1))
        out.append(c)
    return out


def find_venue(name: str, lat: float, lng: float, radius_m: float = 8000) -> dict | None:
    """A stored place by (roughly) its name near a point: for "plan a night at X"."""
    q = (name or "").strip()
    if len(q) < 3:
        return None
    s, n, w, e = _box(lat, lng, radius_m)
    try:
        with SessionLocal() as db:
            rows = db.scalars(select(Venue).where(Venue.lat.between(s, n), Venue.lng.between(w, e),
                                                  Venue.name.ilike(f"%{q}%")).limit(20)).all()
    except Exception:
        return None
    if not rows:
        return None
    exact = [v for v in rows if v.name.lower() == q.lower()]
    best = min(exact or rows, key=lambda v: (-(v.notability or 0), _km(lat, lng, v.lat, v.lng)))
    return _row_dict(best)
