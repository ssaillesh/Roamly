"""Live events via the Ticketmaster Discovery API — free (concerts, sports,
comedy, festivals). Use your app's *Consumer Key* as TICKETMASTER_API_KEY.

Returns [] when no key is set, so the planner degrades gracefully.
"""
import json
from datetime import datetime

import httpx

from app.config import settings
from app.redis_client import redis_client

_URL = "https://app.ticketmaster.com/discovery/v2/events.json"
_TTL = 60 * 60 * 3        # 3h for open-ended "upcoming" searches
_TTL_WINDOW = 60 * 10     # 10 min when asking what's on now / tonight


def available() -> bool:
    return bool(settings.ticketmaster_api_key)


def classification_for(vibe: str | None, interests: str | None) -> str | None:
    it = (interests or "").lower()
    if any(w in it for w in ["concert", "music", "dj", "show", "band", "festival"]):
        return "Music"
    if any(w in it for w in ["game", "sports", "raptors", "jays", "leafs", "hockey", "basketball", "baseball"]):
        return "Sports"
    if any(w in it for w in ["comedy", "standup", "stand-up"]):
        return "Comedy"
    if vibe == "night_out":
        return "Music"
    return None  # all types


def _tm_time(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")     # Ticketmaster wants UTC, no offset


def search_events(lat: float, lng: float, *, radius_km: int = 20, size: int = 12,
                  classification: str | None = None,
                  start_utc: datetime | None = None, end_utc: datetime | None = None) -> list[dict]:
    """Events near lat/lng, optionally only those starting in [start_utc, end_utc]."""
    if not available():
        return []
    radius_km = max(1, int(round(radius_km)))
    # Window rounded to 10 min in the key so repeat "right now" asks share a cache entry.
    win = (f"{int(start_utc.timestamp() // 600)}-{int(end_utc.timestamp() // 600)}"
           if start_utc and end_utc else "any")
    key = f"tm:{round(lat, 2)}:{round(lng, 2)}:{radius_km}:{classification}:{win}:{size}"
    cached = redis_client.get(key)
    if cached is not None:
        return json.loads(cached)

    params = {
        "apikey": settings.ticketmaster_api_key,
        "latlong": f"{lat},{lng}", "radius": radius_km, "unit": "km",
        "size": size, "sort": "date,asc",
    }
    if classification:
        params["classificationName"] = classification
    if start_utc and end_utc:
        params["startDateTime"], params["endDateTime"] = _tm_time(start_utc), _tm_time(end_utc)
    try:
        r = httpx.get(_URL, params=params, timeout=15.0)
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError):
        return []

    out = []
    for e in data.get("_embedded", {}).get("events", []):
        venue = (e.get("_embedded", {}).get("venues") or [{}])[0]
        loc = venue.get("location") or {}
        start = e.get("dates", {}).get("start", {})
        seg = ((e.get("classifications") or [{}])[0].get("segment") or {}).get("name")
        prices = e.get("priceRanges") or []
        img = next((im["url"] for im in e.get("images", []) if im.get("width", 0) >= 600), None)
        out.append({
            "name": e.get("name"),
            "venue": venue.get("name"),
            "lat": float(loc["latitude"]) if loc.get("latitude") else None,
            "lng": float(loc["longitude"]) if loc.get("longitude") else None,
            "date": start.get("localDate"),
            "time": start.get("localTime"),
            "start": start.get("dateTime"),          # ISO UTC — the page shows "on now" / "in 40 min"
            "end": (e.get("dates", {}).get("end") or {}).get("dateTime"),
            "free": False if prices else None,       # Ticketmaster rarely lists free events
            "source": "ticketmaster",
            "category": seg,
            "url": e.get("url"),
            "price_min": prices[0]["min"] if prices else None,
            "price_max": prices[0]["max"] if prices else None,
            "image": img,
        })
    redis_client.setex(key, _TTL_WINDOW if start_utc else _TTL, json.dumps(out))
    return out
