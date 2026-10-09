"""Free geocoding via Nominatim (OpenStreetMap), with Redis caching + polite rate limiting.

Nominatim's usage policy asks for <= 1 request/second and a descriptive User-Agent.
Results are cached in Redis for 30 days so repeated cities don't hit the API.
"""
import json
import math
import time

import httpx

from app.config import settings
from app.redis_client import redis_client
from app.services import usage

_LAST_CALL_KEY = "geocode:last_call_ts"


def _cache_key(city: str, country: str) -> str:
    return f"geocode:{(country or '').upper()}:{city.strip().lower()}"


def _throttle() -> None:
    """Ensure at least ~1.1s between live Nominatim calls (process-safe via Redis)."""
    last = redis_client.get(_LAST_CALL_KEY)
    if last:
        elapsed = time.time() - float(last)
        if elapsed < 1.1:
            time.sleep(1.1 - elapsed)
    redis_client.set(_LAST_CALL_KEY, str(time.time()))


def geocode(city: str, country: str | None) -> tuple[float, float] | None:
    """Return (lat, lng) for a city/country, or None if not found.

    Cached results (including negative lookups) avoid repeat API calls.
    """
    if not city:
        return None
    key = _cache_key(city, country or "")
    cached = redis_client.get(key)
    if cached is not None:
        data = json.loads(cached)
        return (data["lat"], data["lng"]) if data else None

    params = {
        "q": f"{city}, {country}" if country else city,
        "format": "json",
        "limit": 1,
        "addressdetails": 0,
    }
    if country:
        params["countrycodes"] = country.lower()

    headers = {"User-Agent": settings.geocode_user_agent}
    try:
        _throttle()
        with usage.counted("nominatim"):
            resp = httpx.get(settings.nominatim_url, params=params, headers=headers, timeout=15.0)
            resp.raise_for_status()
        results = resp.json()
    except (httpx.HTTPError, ValueError):
        return None

    if not results:
        redis_client.setex(key, settings.geocode_cache_ttl, json.dumps(None))
        return None

    lat = float(results[0]["lat"])
    lng = float(results[0]["lon"])
    redis_client.setex(key, settings.geocode_cache_ttl, json.dumps({"lat": lat, "lng": lng}))
    return lat, lng


def geocode_place(name: str) -> tuple[float, float] | None:
    """Geocode restricted to settlements (cities/towns/villages) — safe for
    guessing place names out of free text, since ordinary words won't match."""
    if not name or len(name) < 3:
        return None
    key = f"geoplace:{name.strip().lower()}"
    cached = redis_client.get(key)
    if cached is not None:
        data = json.loads(cached)
        return (data["lat"], data["lng"]) if data else None

    params = {"q": name, "format": "json", "limit": 1,
              "addressdetails": 0, "featuretype": "settlement"}
    try:
        _throttle()
        with usage.counted("nominatim"):
            resp = httpx.get(settings.nominatim_url, params=params,
                             headers={"User-Agent": settings.geocode_user_agent}, timeout=15.0)
            resp.raise_for_status()
        results = resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not results:
        redis_client.setex(key, settings.geocode_cache_ttl, json.dumps(None))
        return None
    lat, lng = float(results[0]["lat"]), float(results[0]["lon"])
    redis_client.setex(key, settings.geocode_cache_ttl, json.dumps({"lat": lat, "lng": lng}))
    return lat, lng


def find_place(name: str, lat: float, lng: float, radius_km: float = 10.0) -> dict | None:
    """Look up a specific named venue near a point (OSM/Nominatim, keyless).
    Returns a normalised candidate dict like yelp/foursquare search results, or
    None when no such place exists nearby — used to verify user-requested venues
    when no paid source is configured."""
    if not name or len(name.strip()) < 3:
        return None
    key = f"geovenue:{name.strip().lower()}:{round(lat,2)}:{round(lng,2)}"
    cached = redis_client.get(key)
    if cached is not None:
        return json.loads(cached)

    # viewbox bounds the search to ~radius_km around the point; bounded=1 makes
    # it a hard filter, so a same-named place in another city can't match.
    dlat = radius_km / 111.0
    dlng = radius_km / (111.0 * max(0.1, math.cos(math.radians(lat))))
    params = {"q": name, "format": "json", "limit": 3, "addressdetails": 0,
              "bounded": 1,
              "viewbox": f"{lng - dlng},{lat + dlat},{lng + dlng},{lat - dlat}"}
    try:
        _throttle()
        with usage.counted("nominatim"):
            resp = httpx.get(settings.nominatim_url, params=params,
                             headers={"User-Agent": settings.geocode_user_agent}, timeout=15.0)
            resp.raise_for_status()
        results = resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    out = None
    for r in results or []:
        found = (r.get("display_name") or "").split(",")[0]
        out = {
            "source": "osm", "name": found or name.strip(),
            "lat": float(r["lat"]), "lng": float(r["lon"]),
            "rating": None, "review_count": None, "price": None,
            "categories": [t for t in [r.get("type", "").replace("_", " ")] if t],
            "address": r.get("display_name"), "image": None, "url": None,
        }
        break
    redis_client.setex(key, settings.geocode_cache_ttl, json.dumps(out))
    return out




def reverse_area(lat: float, lng: float) -> str | None:
    """'Neighbourhood/City, Country' for a point, for the admin "where users are"
    view. Cached per ~1km cell for 30 days (including misses)."""
    key = f"geocode:rev:{round(lat, 2)}:{round(lng, 2)}"
    try:
        cached = redis_client.get(key)
    except Exception:
        cached = None
    if cached is not None:
        return json.loads(cached)
    params = {"lat": lat, "lon": lng, "format": "json", "zoom": 10, "addressdetails": 1}
    try:
        _throttle()
        with usage.counted("nominatim"):
            resp = httpx.get(settings.nominatim_url.rsplit("/", 1)[0] + "/reverse", params=params,
                             headers={"User-Agent": settings.geocode_user_agent}, timeout=10.0)
            resp.raise_for_status()
        addr = resp.json().get("address") or {}
    except (httpx.HTTPError, ValueError):
        return None
    city = (addr.get("city") or addr.get("town") or addr.get("village")
            or addr.get("municipality") or addr.get("county") or addr.get("state"))
    area = ", ".join(x for x in (city, addr.get("country_code", "").upper()) if x) or None
    redis_client.setex(key, settings.geocode_cache_ttl, json.dumps(area))
    return area
