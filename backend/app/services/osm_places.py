"""Nearby places from the OpenStreetMap Overpass API (free, no API key).

The planner's keyless fallback: when the keyed venue APIs (Yelp / Foursquare /
Google) return too few candidates for a stop, this pulls named points of interest
around a lat/lng. Results are normalised and cached in Redis (POIs change slowly)
so we stay friendly to the public Overpass instances.
"""
import json

import httpx

from app.config import settings
from app.redis_client import redis_client

# Public Overpass endpoints, tried in order (the first is the canonical instance).
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

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


def _build_query(category: str, lat: float, lng: float, rad: int) -> str:
    parts = []
    for s in CATEGORIES[category]:
        parts.append(f'node[{s}](around:{rad},{lat},{lng});')
        parts.append(f'way[{s}](around:{rad},{lat},{lng});')
    body = "\n".join(parts)
    # `out center` gives ways a representative lat/lng; tags come along for names.
    return f"[out:json][timeout:25];\n({body}\n);\nout center tags 200;"


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


def fetch_places(category: str, *, lat: float, lng: float, radius: int = 3000) -> list[dict]:
    """Named places of a planner category around lat/lng, Redis-cached.

    The radius is clamped to 500m..MAX_RADIUS so a search never surfaces a place
    further than 3km away.
    """
    if category not in CATEGORIES:
        return []
    radius = max(500, min(radius, MAX_RADIUS))
    # Round coords to ~1km so nearby requests share a cache entry.
    cache_key = f"osm_places:v1:{round(lat, 2)}:{round(lng, 2)}:{radius}:{category}"

    cached = redis_client.get(cache_key)
    if cached is not None:
        return json.loads(cached)

    query = _build_query(category, lat, lng, radius)
    elements = None
    for endpoint in OVERPASS_ENDPOINTS:
        try:
            resp = httpx.post(endpoint, data={"data": query},
                              headers={"User-Agent": settings.geocode_user_agent}, timeout=30.0)
            resp.raise_for_status()
            elements = resp.json().get("elements", [])
            break
        except (httpx.HTTPError, ValueError):
            continue
    if elements is None:
        return []  # don't cache a transient failure

    result = _normalise(elements, category)
    result.sort(key=lambda p: p["name"])
    redis_client.setex(cache_key, _CACHE_TTL, json.dumps(result))
    return result
