"""Yelp Fusion — ratings, reviews, prices and photos for eat/date/nightlife.

Paid since 2024 (30-day trial, then a monthly plan), so it's switched off in
code (services/usage.DISABLED) and never called, even with YELP_API_KEY set.
With no key this module returns [] so the planner falls back to OpenStreetMap.

Yelp's terms restrict long-term caching of their content, so results are cached
only briefly (to smooth a single planning session), not for days.
"""
import json

import httpx

from app.config import settings
from app.redis_client import redis_client
from app.services import usage

_SEARCH_URL = "https://api.yelp.com/v3/businesses/search"
_CACHE_TTL = 60 * 30  # 30 min — short, per Yelp content policy


def available() -> bool:
    # Switched off as a paid product (services/usage.DISABLED): never called,
    # even with a key set.
    return bool(settings.yelp_api_key) and not usage.disabled("yelp")


def search(lat: float, lng: float, *, term: str | None = None,
           categories: str | None = None, price: str | None = None,
           radius: int = 3000, limit: int = 12, sort_by: str = "best_match") -> list[dict]:
    """Return normalised Yelp businesses near a point. [] if no key or on error."""
    if not available():
        return []
    key = f"yelp:{round(lat,3)}:{round(lng,3)}:{term or categories}:{price}:{radius}:{sort_by}"
    cached = redis_client.get(key)
    if cached is not None:
        return json.loads(cached)
    if not usage.allow("yelp"):
        return []

    params = {
        "latitude": lat, "longitude": lng,
        "radius": min(max(radius, 100), 40000),
        "limit": min(limit, 50), "sort_by": sort_by,
    }
    if term:
        params["term"] = term
    if categories:
        params["categories"] = categories
    if price:
        params["price"] = price  # e.g. "1,2" for $ and $$
    try:
        with usage.counted("yelp"):
            resp = httpx.get(_SEARCH_URL, params=params, timeout=15.0,
                             headers={"Authorization": f"Bearer {settings.yelp_api_key}"})
            resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return []

    out = []
    for b in data.get("businesses", []):
        coords = b.get("coordinates") or {}
        if coords.get("latitude") is None:
            continue
        out.append({
            "source": "yelp",
            "name": b.get("name"),
            "lat": coords["latitude"], "lng": coords["longitude"],
            "rating": b.get("rating"),
            "review_count": b.get("review_count"),
            "price": b.get("price"),  # "$".."$$$$" or None
            "categories": [c["title"] for c in b.get("categories", [])],
            "address": ", ".join(b.get("location", {}).get("display_address", []) or []),
            "image": b.get("image_url") or None,
            "url": b.get("url"),  # deep link to the Yelp page (reviews)
            "phone": b.get("display_phone"),
        })
    redis_client.setex(key, _CACHE_TTL, json.dumps(out))
    return out
