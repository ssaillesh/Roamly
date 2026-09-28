"""Free public events from a city open-data calendar — Toronto's "Festivals &
Events" feed (street festivals, square programming, film-festival events…).

It's the City's official, open-licensed dataset: events submitted by organizers
and approved by Tourism Services, with a free/paid flag. Field names follow the
dataset readme (`calEvent` objects: eventName, dates[], locations[], freeEvent…);
parsing is defensive because the live feed was unreachable (HTTP 403 for every
client, 2026-09) when this was written. Until it's restored this returns [] and
costs one short request per 10 minutes.
"""
import json
import math
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from app.config import settings
from app.redis_client import redis_client

TORONTO = ZoneInfo("America/Toronto")
# Only consult Toronto's calendar for searches in and around the city.
_TORONTO_BBOX = (43.40, -79.90, 44.10, -78.90)   # lat_min, lng_min, lat_max, lng_max

_CACHE_KEY = "city_events:toronto:v1"
_DOWN_KEY = "city_events:toronto:down"
_CACHE_TTL = 60 * 60          # the feed changes a few times a day
_DOWN_TTL = 60 * 10           # after a failure, wait before trying again
_TIMEOUT = 15.0


def available_for(lat: float, lng: float) -> bool:
    la0, lo0, la1, lo1 = _TORONTO_BBOX
    return bool(settings.toronto_events_feed_url) and la0 <= lat <= la1 and lo0 <= lng <= lo1


def _km(a_lat, a_lng, b_lat, b_lng):
    dlat, dlng = math.radians(b_lat - a_lat), math.radians(b_lng - a_lng)
    x = (math.sin(dlat / 2) ** 2 + math.cos(math.radians(a_lat)) * math.cos(math.radians(b_lat))
         * math.sin(dlng / 2) ** 2)
    return 2 * 6371 * math.asin(math.sqrt(x))


def _dt(value, *, end_of_day=False):
    """Feed timestamps → aware UTC datetimes. Dates without a time become the
    start (or end) of that day in Toronto."""
    if not value:
        return None
    if isinstance(value, (int, float)):                     # epoch millis
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    s = str(value).strip().replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    if len(s) <= 10:                                        # a bare date
        d = datetime.combine(d.date(), dtime(23, 59) if end_of_day else dtime(0, 0))
    if d.tzinfo is None:
        d = d.replace(tzinfo=TORONTO)
    return d.astimezone(timezone.utc)


def _first_coords(locations):
    for loc in locations or []:
        coords = loc.get("coords")
        if isinstance(coords, list):
            coords = coords[0] if coords else None
        for src in (coords or {}, loc):
            lat = src.get("lat", src.get("latitude"))
            lng = src.get("lng", src.get("longitude"))
            if lat is not None and lng is not None:
                try:
                    return loc, float(lat), float(lng)
                except (TypeError, ValueError):
                    pass
    return None, None, None


def _occurrences(ev):
    """(start, end, all_day) spans for an event, from its dates[] list or its
    overall startDate..endDate."""
    spans = []
    for d in ev.get("dates") or []:
        start = _dt(d.get("startDateTime") or d.get("startDate"))
        end = _dt(d.get("endDateTime") or d.get("endDate"), end_of_day=True)
        if start:
            spans.append((start, end or start + timedelta(hours=3), bool(d.get("allDay"))))
    if not spans:
        start, end = _dt(ev.get("startDate")), _dt(ev.get("endDate"), end_of_day=True)
        if start:
            spans.append((start, end or start + timedelta(days=1), True))
    return spans


def _price_min(cost):
    vals = []
    for v in (cost or {}).values() if isinstance(cost, dict) else []:
        try:
            if float(v) > 0:
                vals.append(float(v))
        except (TypeError, ValueError):
            pass
    return min(vals) if vals else None


def _normalise(raw):
    rows = raw.get("value", raw) if isinstance(raw, dict) else raw
    out = []
    for row in rows or []:
        ev = row.get("calEvent", row) if isinstance(row, dict) else None
        if not ev or not ev.get("eventName"):
            continue
        loc, lat, lng = _first_coords(ev.get("locations"))
        if lat is None:
            continue                                        # can't place it on a plan
        spans = _occurrences(ev)
        if not spans:
            continue
        image = ev.get("image") if isinstance(ev.get("image"), dict) else {}
        free = ev.get("freeEvent")
        out.append({
            "name": ev["eventName"],
            "venue": (loc or {}).get("locationName") or (loc or {}).get("address"),
            "lat": lat, "lng": lng,
            "category": ev.get("categoryString"),
            "url": ev.get("eventWebsite"),
            "free": (free in (True, "Yes", "yes", "true", "True")) if free is not None else None,
            "price_min": _price_min(ev.get("cost")),
            "image": image.get("url") or image.get("fileUrl"),
            "spans": [(s.isoformat(), e.isoformat(), a) for s, e, a in spans],
        })
    return out


def _feed():
    cached = redis_client.get(_CACHE_KEY)
    if cached is not None:
        return json.loads(cached)
    if redis_client.get(_DOWN_KEY):
        return []
    try:
        r = httpx.get(settings.toronto_events_feed_url, timeout=_TIMEOUT,
                      headers={"User-Agent": settings.geocode_user_agent, "Accept": "application/json"})
        r.raise_for_status()
        events = _normalise(r.json())
    except (httpx.HTTPError, ValueError):
        redis_client.setex(_DOWN_KEY, _DOWN_TTL, "1")
        return []
    redis_client.setex(_CACHE_KEY, _CACHE_TTL, json.dumps(events))
    return events


def search(lat: float, lng: float, *, start_utc: datetime, end_utc: datetime,
           radius_km: float) -> list[dict]:
    """City-calendar events within radius_km whose dates overlap the window."""
    if not available_for(lat, lng):
        return []
    out = []
    for ev in _feed():
        if _km(lat, lng, ev["lat"], ev["lng"]) > radius_km:
            continue
        for s, e, all_day in ev["spans"]:
            start, end = datetime.fromisoformat(s), datetime.fromisoformat(e)
            if start <= end_utc and end >= start_utc:          # overlaps the window
                local = start.astimezone(TORONTO)
                out.append({
                    "name": ev["name"], "venue": ev["venue"], "lat": ev["lat"], "lng": ev["lng"],
                    "date": local.date().isoformat(),
                    "time": None if all_day else local.strftime("%H:%M:%S"),
                    "start": start.isoformat(), "end": end.isoformat(),
                    "category": ev["category"], "url": ev["url"],
                    "price_min": ev["price_min"], "price_max": None,
                    "free": ev["free"], "image": ev["image"], "source": "city_toronto",
                })
                break                                           # one entry per event
    return out
