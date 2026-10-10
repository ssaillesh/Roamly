"""Club shows and concerts from the SeatGeek Platform API, for the Nightlife tab.

Free developer access; the client ID goes in SEATGEEK_CLIENT_ID on the API server
only (it's a credential). Their API terms (seatgeek.com/api-terms) shape how it's
used here:
- their logo, linked to seatgeek.com, wherever their events show (the page adds it
  to every event with source "seatgeek");
- their data never goes to an AI model: SeatGeek events are listed in the
  Nightlife tab only, never passed to the planner, its narration or the chat;
- no storing: results are cached for 10 minutes and never written to the database;
- the minimum data: concerts only, and the search point rounded to ~1 km.
"""
import json
from datetime import datetime

import httpx

from app.config import settings
from app.redis_client import redis_client
from app.services import usage

_URL = "https://api.seatgeek.com/2/events"
_TTL = 60 * 10
# Some APIs turn away anonymous traffic from cloud servers; say who's asking.
_HEADERS = {"User-Agent": "Roamly/1.0 (outing planner; Nightlife tab)", "Accept": "application/json"}


def available() -> bool:
    return bool(settings.seatgeek_client_id) and not usage.disabled("seatgeek")


def _utc(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _event(e: dict) -> dict:
    venue = e.get("venue") or {}
    loc = venue.get("location") or {}
    local = e.get("datetime_local") or ""
    stats = e.get("stats") or {}
    genres = [g.get("name") for p in e.get("performers") or [] for g in p.get("genres") or [] if g.get("name")]
    title = e.get("title") or e.get("short_title") or ""
    return {
        "name": title,
        "venue": venue.get("name"),
        "lat": loc.get("lat"), "lng": loc.get("lon"),
        "date": local[:10] or None,
        "time": None if e.get("time_tbd") else (local[11:19] or None),
        "start": f"{e['datetime_utc']}Z" if e.get("datetime_utc") and not e.get("time_tbd") else None,
        "end": f"{e['enddatetime_utc']}Z" if e.get("enddatetime_utc") else None,
        "free": None,
        "price_min": stats.get("lowest_price"), "price_max": stats.get("highest_price"),
        "url": e.get("url"),
        "category": ((e.get("taxonomies") or [{}])[0].get("name") or "concert").replace("_", " ").title(),
        "genre": genres[0] if genres else None,
        "adults_only": True if "19+" in title else None,
        "source": "seatgeek",
    }


def search_events(lat: float, lng: float, *, radius_km: float = 10, start_utc: datetime | None = None,
                  end_utc: datetime | None = None, size: int = 50) -> list[dict]:
    """Concerts near a point in [start_utc, end_utc]. [] when off, capped or failing."""
    if not available():
        return []
    lat, lng, radius_km = round(lat, 2), round(lng, 2), max(1, int(round(radius_km)))
    win = (f"{int(start_utc.timestamp() // 600)}-{int(end_utc.timestamp() // 600)}"
           if start_utc and end_utc else "any")
    key = f"sg:{lat}:{lng}:{radius_km}:{win}:{size}"
    cached = redis_client.get(key)
    if cached is not None:
        return json.loads(cached)
    if not usage.allow("seatgeek"):
        return []
    params = {"client_id": settings.seatgeek_client_id, "lat": lat, "lon": lng, "range": f"{radius_km}km",
              "taxonomies.name": "concert", "per_page": min(size, 50), "sort": "datetime_utc.asc"}
    if start_utc and end_utc:
        params["datetime_utc.gte"], params["datetime_utc.lte"] = _utc(start_utc), _utc(end_utc)
    try:
        with usage.counted("seatgeek"):
            r = httpx.get(_URL, params=params, headers=_HEADERS, timeout=15.0)
            r.raise_for_status()
        out = [_event(e) for e in r.json().get("events", []) if e.get("title")]
    except (httpx.HTTPError, ValueError):
        return []
    redis_client.setex(key, _TTL, json.dumps(out))
    return out
