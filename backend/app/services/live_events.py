"""What's happening around a place, in a time window — merged from every event
source we have: Ticketmaster (ticketed: concerts, sports, comedy, theatre) and
the city's open-data calendar (free/public festivals). More sources plug in here.
"""
import math
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.services import city_events
from app.services import events as ticketmaster

WINDOWS = ("now", "tonight", "weekend", "day")
DEFAULT_TZ = "America/Toronto"      # the app is Toronto-first; the page sends its real zone
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="live-events")


def _zone(tz: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(tz or DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TZ)


def window_range(window: str, tz: str | None = None, now: datetime | None = None):
    """(start_utc, end_utc) for a window, in the user's local time.

    now      2h ago → 4h ahead (started-but-still-on counts)
    tonight  2h ago → 3am local
    weekend  Fri 5pm → Sun 11:59pm local (from now, if it's already the weekend)
    day      2h ago → 24h ahead (what a plan for today can use)
    """
    zone = _zone(tz)
    now = (now or datetime.now(timezone.utc)).astimezone(zone)
    back = now - timedelta(hours=2)
    if window == "tonight":
        cutoff = datetime.combine(now.date(), time(3, 0), zone)
        end = cutoff if now < cutoff else cutoff + timedelta(days=1)
        start = back
    elif window == "weekend":
        days_to_fri = (4 - now.weekday()) % 7
        fri = datetime.combine(now.date() + timedelta(days=days_to_fri), time(17, 0), zone)
        if now.weekday() in (5, 6) or (now.weekday() == 4 and now >= fri):
            fri = back                                          # already the weekend
        sun = datetime.combine(fri.date() + timedelta(days=(6 - fri.weekday()) % 7), time(23, 59), zone)
        start, end = fri, sun
    elif window == "day":
        start, end = back, now + timedelta(hours=24)
    else:                                                       # "now"
        start, end = back, now + timedelta(hours=4)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def available_for(lat: float, lng: float) -> bool:
    return ticketmaster.available() or city_events.available_for(lat, lng)


def _km(a_lat, a_lng, b_lat, b_lng):
    dlat, dlng = math.radians(b_lat - a_lat), math.radians(b_lng - a_lng)
    x = (math.sin(dlat / 2) ** 2 + math.cos(math.radians(a_lat)) * math.cos(math.radians(b_lat))
         * math.sin(dlng / 2) ** 2)
    return 2 * 6371 * math.asin(math.sqrt(x))


def _start(ev):
    try:
        return datetime.fromisoformat((ev.get("start") or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def is_on_now(ev: dict, now: datetime | None = None) -> bool:
    """Started already and (if the source gives an end) not over yet."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    st = _start(ev)
    if not st or st > now:
        return False
    try:
        end = datetime.fromisoformat((ev.get("end") or "").replace("Z", "+00:00"))
    except ValueError:
        end = None
    return end is None or end >= now


def happening(lat: float, lng: float, *, window: str = "now", radius_km: float = 10,
              tz: str | None = None, classification: str | None = None, size: int = 12,
              now: datetime | None = None) -> list[dict]:
    """Events near lat/lng in the window, from every source, deduplicated and
    sorted: already on (nearest first), then by start time."""
    start_utc, end_utc = window_range(window if window in WINDOWS else "now", tz, now)
    jobs = [
        _POOL.submit(ticketmaster.search_events, lat, lng, radius_km=radius_km, size=size,
                     classification=classification, start_utc=start_utc, end_utc=end_utc),
        _POOL.submit(city_events.search, lat, lng, start_utc=start_utc, end_utc=end_utc,
                     radius_km=radius_km),
    ]
    merged, seen = [], set()
    for job in jobs:
        try:
            found = job.result(timeout=20)
        except Exception:           # one slow/broken source never sinks the others
            found = []
        for ev in found:
            key = re.sub(r"[^a-z0-9]", "", (ev.get("name") or "").lower())
            if not key or key in seen:
                continue
            seen.add(key)
            if ev.get("lat") is not None and ev.get("lng") is not None:
                ev["distance_km"] = round(_km(lat, lng, ev["lat"], ev["lng"]), 1)
                if ev["distance_km"] > radius_km:
                    continue
            merged.append(ev)
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    far = datetime.max.replace(tzinfo=timezone.utc)

    def order(ev):
        st = _start(ev) or far
        dist = ev["distance_km"] if ev.get("distance_km") is not None else 99
        return (0, dist, st) if is_on_now(ev, now_utc) else (1, 0, st)
    merged.sort(key=order)
    return merged[:size]
