"""Hotspot discovery via the OpenStreetMap Overpass API (free, no API key).

Pulls categorised points of interest — food, activities, nightlife, nature, history,
shops — around major Canadian cities, or any arbitrary lat/lng (e.g. the current map
viewport). Results are normalised and cached in Redis (POIs change slowly) so we stay
friendly to the public Overpass instances and serve the feed fast.
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

# Canada-only for now. lat/lng = rough city centre; radius in metres.
CANADA_CITIES = {
    "toronto":   {"label": "Toronto",   "lat": 43.6532, "lng": -79.3832, "radius": 7000},
    "vancouver": {"label": "Vancouver", "lat": 49.2827, "lng": -123.1207, "radius": 7000},
    "montreal":  {"label": "Montréal",  "lat": 45.5019, "lng": -73.5674, "radius": 7000},
    "calgary":   {"label": "Calgary",   "lat": 51.0447, "lng": -114.0719, "radius": 7000},
    "ottawa":    {"label": "Ottawa",    "lat": 45.4215, "lng": -75.6972, "radius": 7000},
    "quebec":    {"label": "Québec City", "lat": 46.8139, "lng": -71.2080, "radius": 6000},
    "halifax":   {"label": "Halifax",   "lat": 44.6488, "lng": -63.5752, "radius": 6000},
}

# Each category maps to a list of Overpass tag selectors. We query nodes (and the
# centre of ways) so we get coordinates for everything.
CATEGORIES = {
    "food": {
        "label": "Eat", "icon": "🍽️",
        "selectors": ['"amenity"="restaurant"', '"amenity"="cafe"', '"amenity"="fast_food"',
                      '"amenity"="ice_cream"', '"shop"="bakery"'],
    },
    "activities": {
        "label": "Things to do", "icon": "🎟️",
        "selectors": ['"tourism"="attraction"', '"tourism"="theme_park"', '"tourism"="gallery"',
                      '"amenity"="cinema"', '"amenity"="theatre"', '"amenity"="nightclub"',
                      '"leisure"="bowling_alley"', '"leisure"="escape_game"', '"leisure"="amusement_arcade"',
                      '"leisure"="trampoline_park"', '"leisure"="miniature_golf"', '"leisure"="water_park"',
                      '"sport"="laser_tag"', '"sport"="paintball"', '"sport"="karting"'],
    },
    "party": {
        "label": "Nightlife", "icon": "🎉",
        "selectors": ['"amenity"="nightclub"', '"amenity"="bar"', '"amenity"="pub"'],
    },
    "nature": {
        "label": "Nature", "icon": "🌿",
        "selectors": ['"leisure"="park"', '"tourism"="viewpoint"', '"natural"="beach"',
                      '"natural"="waterfall"', '"leisure"="nature_reserve"'],
    },
    "history": {
        "label": "Historical", "icon": "🏛️",
        "selectors": ['"historic"="monument"', '"historic"="memorial"', '"historic"="castle"',
                      '"historic"="fort"', '"historic"="archaeological_site"', '"historic"="ruins"',
                      '"tourism"="museum"'],
    },
    "shops": {
        "label": "Shops", "icon": "🛍️",
        # Existence-only selector (no "="value") — matches every OSM node/way tagged
        # with any shop=* value, i.e. every kind of local retail in one category.
        "selectors": ['"shop"'],
    },
}

_CACHE_TTL = 60 * 60 * 24 * 7  # 7 days — POIs barely move

# Hard ceiling for a lat/lng ("near me") search: nothing further than 3km away.
MAX_RADIUS = 3000


def _build_query(category: str, lat: float, lng: float, rad: int) -> str:
    sel = CATEGORIES[category]["selectors"]
    parts = []
    for s in sel:
        parts.append(f'node[{s}](around:{rad},{lat},{lng});')
        parts.append(f'way[{s}](around:{rad},{lat},{lng});')
    body = "\n".join(parts)
    # `out center` gives ways a representative lat/lng; tags come along for names.
    # A high cap means a larger radius reliably returns more (client sorts by distance).
    return f"[out:json][timeout:25];\n({body}\n);\nout center tags 200;"


def _normalise(elements: list, category: str) -> list[dict]:
    seen = set()
    out = []
    for e in elements:
        tags = e.get("tags") or {}
        name = tags.get("name")
        if not name:
            continue  # unnamed POIs aren't useful in a feed
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
            "name": name, "lat": lat, "lng": lng,
            "category": category, "subtype": subtype,
            "address": street,
            "website": tags.get("website") or tags.get("contact:website"),
            # Free enrichment hooks: OSM often links places to Wikipedia/Wikidata,
            # which we resolve to a photo + blurb client-side (no API key).
            "wikipedia": tags.get("wikipedia"),
            "wikidata": tags.get("wikidata"),
            "image": tags.get("image"),
        })
    return out


def fetch_hotspots(category: str, *, city_key: str | None = None,
                   lat: float | None = None, lng: float | None = None,
                   radius: int = 3000) -> list[dict]:
    """Return normalised hotspots for a category, around either a preset Canadian
    city or arbitrary lat/lng (e.g. the user's exact location). Redis-cached.

    In lat/lng mode the radius is clamped to MAX_RADIUS (3km), so a "near me"
    search never surfaces a spot further than 3km away.
    """
    if category not in CATEGORIES:
        return []
    if city_key:
        city = CANADA_CITIES.get(city_key)
        if not city:
            return []
        lat, lng, radius = city["lat"], city["lng"], city["radius"]
        cache_key = f"hotspots:v6:{city_key}:{category}"
    elif lat is not None and lng is not None:
        radius = max(500, min(radius, MAX_RADIUS))
        # Round coords to ~1km so nearby requests share a cache entry.
        cache_key = f"hotspots:v6:{round(lat, 2)}:{round(lng, 2)}:{radius}:{category}"
    else:
        return []

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
    result.sort(key=lambda h: h["name"])
    redis_client.setex(cache_key, _CACHE_TTL, json.dumps(result))
    return result
