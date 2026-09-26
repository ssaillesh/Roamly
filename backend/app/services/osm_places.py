"""Nearby places from the OpenStreetMap Overpass API (free, no API key).

The planner's keyless fallback: when the keyed venue APIs (Yelp / Foursquare /
Google) return too few candidates for a stop, this pulls named points of interest
around a lat/lng. Results are normalised and cached in Redis (POIs change slowly)
so we stay friendly to the public Overpass instances.
"""
import json
import re
import threading
import time

import httpx

from app.config import settings
from app.redis_client import redis_client

# Public Overpass endpoints, tried in order, each with its own timeout. The
# canonical instance answers a whole plan's combined query in ~6-8s; the mirror
# often hangs, so it only gets a short try.
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
_TIMEOUTS = {OVERPASS_ENDPOINTS[0]: 15.0, OVERPASS_ENDPOINTS[1]: 6.0}

# Planner slot categories → Overpass tag selectors. We query nodes (and the centre
# of ways) so we get coordinates for everything.
CATEGORIES = {
    "food": ['"amenity"="restaurant"', '"amenity"="cafe"', '"amenity"="fast_food"',
             '"amenity"="ice_cream"', '"shop"="bakery"'],
    "activities": ['"tourism"="attraction"', '"tourism"="theme_park"', '"tourism"="gallery"',
                   '"amenity"="cinema"', '"amenity"="theatre"', '"amenity"="nightclub"',
                   '"leisure"="bowling_alley"', '"leisure"="escape_game"', '"leisure"="amusement_arcade"',
                   '"leisure"="trampoline_park"', '"leisure"="miniature_golf"', '"leisure"="water_park"',
                   '"sport"="laser_tag"', '"sport"="paintball"', '"sport"="karting"'],
    "party": ['"amenity"="nightclub"', '"amenity"="bar"', '"amenity"="pub"'],
    "nature": ['"leisure"="park"', '"tourism"="viewpoint"', '"natural"="beach"',
               '"natural"="waterfall"', '"leisure"="nature_reserve"'],
}

_CACHE_TTL = 60 * 60 * 24 * 7  # 7 days — POIs barely move

# Hard ceiling for a search: nothing further than 3km away.
MAX_RADIUS = 3000

# The public Overpass instances are free but flaky (504s when overloaded, no
# answer at all, and 429 past ~2 concurrent queries per IP). Fail fast, skip an
# instance that just failed instead of making every stop wait on it again, and
# fetch all of a plan's categories in ONE query (prefetch) rather than one each.
_COOLDOWN = 30.0           # after a timeout / connection error: the instance is struggling
_COOLDOWN_BUSY = 30.0      # after a 429: we were just too many at once
_down_until: dict[str, float] = {}
# One Overpass query at a time per process: the public instances allow ~2
# concurrent queries per IP, and a plan searches its stops in parallel.
_lock = threading.Lock()


def degraded() -> bool:
    """True while every Overpass instance is cooling down after a failure — i.e.
    the free venue fallback is currently unavailable (callers can say so)."""
    now = time.monotonic()
    return all(_down_until.get(e, 0) > now for e in OVERPASS_ENDPOINTS)


def _cache_key(category: str, lat: float, lng: float, radius: int) -> str:
    # Round coords to ~1km so nearby requests share a cache entry.
    return f"osm_places:v1:{round(lat, 2)}:{round(lng, 2)}:{radius}:{category}"


def _clamp(radius: int) -> int:
    return max(500, min(radius, MAX_RADIUS))


def _union(category: str, lat: float, lng: float, rad: int) -> str:
    parts = []
    for s in CATEGORIES[category]:
        parts.append(f'node[{s}](around:{rad},{lat},{lng});')
        parts.append(f'way[{s}](around:{rad},{lat},{lng});')
    # `out center` gives ways a representative lat/lng; tags come along for names.
    return "(" + "\n".join(parts) + "\n);\nout center tags 200;"


def _build_query(categories: list[str], lat: float, lng: float, rad: int) -> str:
    # One output block per category, so a dense category (restaurants) can't use
    # up the result cap of a sparse one (parks).
    return "[out:json][timeout:25];\n" + "\n".join(_union(c, lat, lng, rad) for c in categories)


def _post(query: str) -> list | None:
    """Run a query against the first healthy instance; None if all failed.

    The canonical instance gets a second try: its 502/503/504s come from load
    spikes and the very next attempt usually answers in a few seconds. Only
    timeouts / connection failures put an instance on cooldown."""
    attempts = [OVERPASS_ENDPOINTS[0], OVERPASS_ENDPOINTS[0], *OVERPASS_ENDPOINTS[1:]]
    for endpoint in attempts:
        if _down_until.get(endpoint, 0) > time.monotonic():
            continue
        try:
            resp = httpx.post(endpoint, data={"data": query},
                              headers={"User-Agent": settings.geocode_user_agent},
                              timeout=_TIMEOUTS.get(endpoint, 8.0))
        except httpx.HTTPError:
            _down_until[endpoint] = time.monotonic() + _COOLDOWN
            continue
        if resp.status_code == 429:
            _down_until[endpoint] = time.monotonic() + _COOLDOWN_BUSY
            continue
        if resp.status_code != 200:
            continue                                  # overloaded right now — next attempt
        try:
            return resp.json().get("elements", [])
        except ValueError:
            continue
    return None


def _selector_matches(tags: dict, selector: str) -> bool:
    m = re.match(r'"([^"]+)"(?:="([^"]+)")?$', selector)
    if not m:
        return False
    key, val = m.groups()
    return key in tags if val is None else tags.get(key) == val


def _in_category(tags: dict, category: str) -> bool:
    return any(_selector_matches(tags, s) for s in CATEGORIES[category])


def _normalise(elements: list, category: str) -> list[dict]:
    seen = set()
    out = []
    for e in elements:
        tags = e.get("tags") or {}
        name = tags.get("name")
        if not name:
            continue  # unnamed POIs can't be a stop in a plan
        lat = e.get("lat") or (e.get("center") or {}).get("lat")
        lng = e.get("lon") or (e.get("center") or {}).get("lon")
        if lat is None or lng is None:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        subtype = (tags.get("amenity") or tags.get("leisure") or tags.get("historic")
                   or tags.get("tourism") or tags.get("natural") or tags.get("shop") or category)
        # Compose a street line from OSM address tags when present.
        street = tags.get("addr:street")
        if street and tags.get("addr:housenumber"):
            street = f"{tags['addr:housenumber']} {street}"
        out.append({
            "name": name, "lat": lat, "lng": lng, "subtype": subtype,
            "address": street,
            "website": tags.get("website") or tags.get("contact:website"),
        })
    return out


def _store(category, lat, lng, radius, elements):
    result = _normalise([e for e in elements if _in_category(e.get("tags") or {}, category)], category)
    result.sort(key=lambda p: p["name"])
    redis_client.setex(_cache_key(category, lat, lng, radius), _CACHE_TTL, json.dumps(result))
    return result


def prefetch(categories, *, lat: float, lng: float, radius: int = 3000) -> None:
    """Warm the cache for several categories with a single Overpass request, so a
    plan's per-stop lookups become cache hits instead of parallel queries that
    trip the public instances' per-IP concurrency limit."""
    radius = _clamp(radius)
    missing = [c for c in dict.fromkeys(categories) if c in CATEGORIES
               and redis_client.get(_cache_key(c, lat, lng, radius)) is None]
    if not missing:
        return
    elements = _post(_build_query(missing, lat, lng, radius))
    if elements is None:
        return  # don't cache a transient failure
    for c in missing:
        _store(c, lat, lng, radius, elements)


def fetch_places(category: str, *, lat: float, lng: float, radius: int = 3000) -> list[dict]:
    """Named places of a planner category around lat/lng, Redis-cached.

    The radius is clamped to 500m..MAX_RADIUS so a search never surfaces a place
    further than 3km away.
    """
    if category not in CATEGORIES:
        return []
    radius = _clamp(radius)
    key = _cache_key(category, lat, lng, radius)
    cached = redis_client.get(key)
    if cached is not None:
        return json.loads(cached)
    with _lock:
        # A parallel search may have filled it while we waited. If not, fetch
        # every planner category here in ONE request, so the plan's other
        # stops become cache hits instead of more queries.
        if redis_client.get(key) is None:
            prefetch(list(CATEGORIES), lat=lat, lng=lng, radius=radius)
        cached = redis_client.get(key)
    return json.loads(cached) if cached is not None else []   # [] = search failed; not cached
