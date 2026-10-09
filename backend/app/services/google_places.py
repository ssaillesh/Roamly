"""Google Places (New) — the richest venue source when keyed.

Lets the planner "look through Google Maps": real restaurants, activities,
ratings, price levels and photos straight from Places API (New) Text Search.
Set GOOGLE_PLACES_API_KEY (Places API (New) must be enabled on the key).
With no key this module returns [] and the planner falls back to
Yelp / Foursquare / OpenStreetMap exactly as before.

It needs a billing card and charges past the free monthly allowance, so it's
switched off in code (services/usage.DISABLED) and never called, even with a key.
"""
import json

import httpx

from app.config import settings
from app.redis_client import redis_client
from app.services import usage

_URL = "https://places.googleapis.com/v1/places:searchText"
_FIELDS = ",".join(f"places.{f}" for f in (
    "displayName", "formattedAddress", "location", "rating", "userRatingCount",
    "priceLevel", "types", "googleMapsUri",
))
# No photos: each photo the browser loads is a separately billed Place Photo, the
# server can't count those against the free cap, and the photo URL would carry
# the API key to every visitor.
_PRICE = {
    "PRICE_LEVEL_INEXPENSIVE": "$", "PRICE_LEVEL_MODERATE": "$$",
    "PRICE_LEVEL_EXPENSIVE": "$$$", "PRICE_LEVEL_VERY_EXPENSIVE": "$$$$",
}
_TTL = 60 * 60 * 3  # 3h


def available() -> bool:
    # Switched off as a paid product (services/usage.DISABLED): never called,
    # even with a key set.
    return bool(settings.google_places_api_key) and not usage.disabled("google_places")




def _parse(p: dict) -> dict | None:
    loc = p.get("location") or {}
    name = (p.get("displayName") or {}).get("text")
    if not name or loc.get("latitude") is None:
        return None
    return {
        "source": "google",
        "name": name,
        "lat": loc["latitude"], "lng": loc["longitude"],
        "rating": p.get("rating"),                       # already 0–5
        "review_count": p.get("userRatingCount"),
        "price": _PRICE.get(p.get("priceLevel")),
        "categories": [t.replace("_", " ") for t in (p.get("types") or [])
                       if t not in ("point_of_interest", "establishment")][:4],
        "address": p.get("formattedAddress"),
        "image": None,
        "url": p.get("googleMapsUri"),
    }


def search(lat: float, lng: float, *, query: str,
           radius: int = 5000, limit: int = 15) -> list[dict]:
    """Normalised Google places near a point for a text query. [] on any failure."""
    if not available() or not (query or "").strip():
        return []
    key = f"gplaces:{round(lat, 3)}:{round(lng, 3)}:{query.lower()}:{radius}"
    cached = redis_client.get(key)
    if cached is not None:
        return json.loads(cached)
    if not usage.allow("google_places"):
        return []
    try:
        with usage.counted("google_places"):
            r = httpx.post(
                _URL,
                json={
                    "textQuery": query,
                    "maxResultCount": min(limit, 20),
                    "locationBias": {"circle": {
                        "center": {"latitude": lat, "longitude": lng},
                        "radius": float(min(max(radius, 100), 50000)),
                    }},
                },
                headers={
                    "X-Goog-Api-Key": settings.google_places_api_key,
                    "X-Goog-FieldMask": _FIELDS,
                },
                timeout=15.0,
            )
            r.raise_for_status()
        results = r.json().get("places", [])
    except (httpx.HTTPError, ValueError):
        return []
    out = [x for x in (_parse(p) for p in results) if x]
    redis_client.setex(key, _TTL, json.dumps(out))
    return out
