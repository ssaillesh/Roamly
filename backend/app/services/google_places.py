"""Google Places (New) — the richest venue source when keyed.

Lets the planner "look through Google Maps": real restaurants, activities,
ratings, price levels and photos straight from Places API (New) Text Search.
Set GOOGLE_PLACES_API_KEY (Places API (New) must be enabled on the key).
With no key this module returns [] and the planner falls back to
Yelp / Foursquare / OpenStreetMap exactly as before.
"""
import json

import httpx

from app.config import settings
from app.redis_client import redis_client
from app.services import usage

_URL = "https://places.googleapis.com/v1/places:searchText"
_FIELDS = ",".join(f"places.{f}" for f in (
    "displayName", "formattedAddress", "location", "rating", "userRatingCount",
    "priceLevel", "types", "googleMapsUri", "photos",
))
_PRICE = {
    "PRICE_LEVEL_INEXPENSIVE": "$", "PRICE_LEVEL_MODERATE": "$$",
    "PRICE_LEVEL_EXPENSIVE": "$$$", "PRICE_LEVEL_VERY_EXPENSIVE": "$$$$",
}
_TTL = 60 * 60 * 3  # 3h


def available() -> bool:
    return bool(settings.google_places_api_key)


def _photo_url(p: dict) -> str | None:
    photos = p.get("photos") or []
    name = photos[0].get("name") if photos else None
    if not name:
        return None
    return (f"https://places.googleapis.com/v1/{name}/media"
            f"?key={settings.google_places_api_key}&maxWidthPx=640")


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
        "image": _photo_url(p),
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
