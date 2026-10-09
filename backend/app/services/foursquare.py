"""Foursquare Places — the trendiness signal.

Foursquare is the only major places API with a real `popularity` score
(0..1, computed from foot-traffic signals), so it tells us what's actually
buzzing right now — not just what's well-reviewed. We use it two ways:
  1. annotate Yelp candidates with popularity (matched by name), and
  2. add Foursquare-only venues to the candidate pool.

Set FOURSQUARE_API_KEY. Supports both key generations:
  - new "service key" → places-api.foursquare.com (Bearer + version header)
  - legacy fsq3… key  → api.foursquare.com/v3
With no key this module returns [] and the planner works as before.

rating, price and popularity are Foursquare *Premium* fields, billed from the
first call, so it's switched off in code (services/usage.DISABLED) and never
called, even with FOURSQUARE_API_KEY set.
"""
import json

import httpx

from app.config import settings
from app.redis_client import redis_client
from app.services import usage

_NEW_URL = "https://places-api.foursquare.com/places/search"
_NEW_VERSION = "2025-06-17"
_LEGACY_URL = "https://api.foursquare.com/v3/places/search"
_FIELDS = "fsq_id,name,geocodes,location,categories,rating,price,popularity,link"
_NEW_FIELDS = "fsq_place_id,name,latitude,longitude,location,categories,rating,price,popularity,link"
_TTL = 60 * 60 * 3  # 3h

# Remembers which endpoint generation the key works with ("new" / "legacy").
_mode: str | None = None


def available() -> bool:
    # Switched off as a paid product (services/usage.DISABLED): never called,
    # even with a key set.
    return bool(settings.foursquare_api_key) and not usage.disabled("foursquare")


def _request(url: str, params: dict, headers: dict) -> list[dict] | None:
    """One attempt against one endpoint. None = auth/endpoint mismatch (try the
    other generation); [] = genuine empty/error (don't retry)."""
    try:
        r = httpx.get(url, params=params, headers=headers, timeout=15.0)
        if r.status_code in (401, 403, 404):
            usage.record("foursquare", ok=False)
            return None
        r.raise_for_status()
        results = r.json().get("results", [])
    except (httpx.HTTPError, ValueError):
        usage.record("foursquare", ok=False)
        return []
    usage.record("foursquare", ok=True)
    return results


def _parse(p: dict) -> dict | None:
    # coordinates: new API = top-level latitude/longitude; v3 = geocodes.main
    lat = p.get("latitude")
    lng = p.get("longitude")
    if lat is None:
        main = (p.get("geocodes") or {}).get("main") or {}
        lat, lng = main.get("latitude"), main.get("longitude")
    if lat is None or not p.get("name"):
        return None
    rating = p.get("rating")  # Foursquare is 0-10; normalise to Yelp's 0-5
    price = p.get("price")    # 1-4
    fsq_id = p.get("fsq_place_id") or p.get("fsq_id")
    return {
        "source": "foursquare",
        "name": p["name"],
        "lat": lat, "lng": lng,
        "rating": round(rating / 2, 1) if rating else None,
        "review_count": None,
        "price": "$" * int(price) if price else None,
        "categories": [c.get("name") or c.get("short_name") for c in (p.get("categories") or []) if c],
        "address": (p.get("location") or {}).get("formatted_address"),
        "image": None,
        "url": p.get("link") or (f"https://foursquare.com/v/{fsq_id}" if fsq_id else None),
        "popularity": p.get("popularity"),  # 0..1 — the trendiness signal
    }


def search(lat: float, lng: float, *, query: str | None = None,
           radius: int = 3000, limit: int = 15) -> list[dict]:
    """Normalised Foursquare places near a point, with popularity. [] on any failure."""
    global _mode
    if not available():
        return []
    key = f"fsq:{round(lat,3)}:{round(lng,3)}:{query}:{radius}"
    cached = redis_client.get(key)
    if cached is not None:
        return json.loads(cached)
    if not usage.allow("foursquare"):
        return []

    params = {"ll": f"{lat},{lng}", "radius": min(max(radius, 100), 100000),
              "limit": min(limit, 50)}
    if query:
        params["query"] = query

    results = None
    if _mode in (None, "new"):
        results = _request(_NEW_URL, {**params, "fields": _NEW_FIELDS},
                           {"Authorization": f"Bearer {settings.foursquare_api_key}",
                            "X-Places-Api-Version": _NEW_VERSION, "Accept": "application/json"})
        if results is not None:
            _mode = "new"
    if results is None:
        results = _request(_LEGACY_URL, {**params, "fields": _FIELDS},
                           {"Authorization": settings.foursquare_api_key,
                            "Accept": "application/json"})
        _mode = "legacy" if results is not None else _mode

    out = [x for x in (_parse(p) for p in (results or [])) if x]
    redis_client.setex(key, _TTL, json.dumps(out))
    return out
