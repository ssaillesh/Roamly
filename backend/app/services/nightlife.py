"""The Nightlife tab: club nights and special guests in a time window, the special
day (if one is on or coming up), the places that fit it, and the club index.

Sources, merged: Ticketmaster and SeatGeek (music events that are nightlife by
genre, by name, by being 19+ or by being at a known night spot; plus a Ticketmaster
keyword search on special days), Roamly's own approved events (admin-added or
venue-submitted), and the stored venue list for the places themselves. SeatGeek
events stay in this tab: their terms don't allow passing them to an AI model. No scraping: Resident Advisor's terms
forbid it and Eventbrite has no public search.
"""
import math
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta, timezone

from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import NightlifeEvent
from app.services import events as ticketmaster
from app.services import live_events, seatgeek, special_days, venue_index
from app.services.opening_hours import is_open

WINDOWS = ("tonight", "weekend", "week")
KIND_LABELS = {"club_night": "Club night", "special_guest": "Special guest", "party": "Party",
               "holiday": "Themed night", "live_music": "Live music", "comedy": "Comedy"}
# Genres that mean a night out (Ticketmaster "Dance/Electronic", SeatGeek "Electronic", "Hip-Hop"…)
_NIGHT_GENRE_RE = re.compile(r"electronic|dance|house|techno|hip-?hop|rap|r&b|rnb|latin|reggae|afro|dancehall|edm", re.I)
_PARTY_RE = re.compile(r"\b(party|club night|dj|rave|after ?party|nightclub|dance night|takeover)\b", re.I)
VENUE_MATCH_KM = 0.3        # an event "at" a stored night spot: same name, this close
_POOL = ThreadPoolExecutor(max_workers=6, thread_name_prefix="nightlife")


def name_key(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower().replace("&", "and").replace("the ", ""))


def _local(dt: datetime | None) -> datetime | None:
    return dt.astimezone(special_days.TZ) if dt else None


def own_events(start_utc: datetime, end_utc: datetime) -> list[dict]:
    """Approved Roamly-listed events overlapping the window, shaped like other events."""
    with SessionLocal() as db:
        rows = db.scalars(select(NightlifeEvent).where(
            NightlifeEvent.status == "approved", NightlifeEvent.starts_at < end_utc,
            func.coalesce(NightlifeEvent.ends_at, NightlifeEvent.starts_at) >= start_utc)
            .order_by(NightlifeEvent.starts_at).limit(100)).all()
    out = []
    for e in rows:
        st = _local(e.starts_at)
        out.append({
            "id": e.id, "name": e.title, "venue": e.venue_name, "lat": e.lat, "lng": e.lng,
            "date": st.date().isoformat(), "time": st.strftime("%H:%M:%S"),
            "start": e.starts_at.astimezone(timezone.utc).isoformat(),
            "end": e.ends_at.astimezone(timezone.utc).isoformat() if e.ends_at else None,
            "free": e.free, "price_min": e.price_min, "price_max": e.price_max, "url": e.url,
            "source": "roamly", "category": KIND_LABELS.get(e.kind, "Nightlife"), "kind": e.kind,
            "special_day": e.special_day, "description": e.description,
        })
    return out


def _spot_for(ev: dict, spots: list[dict]) -> dict | None:
    key = name_key(ev.get("venue"))
    if len(key) < 3:
        return None
    for s in spots:
        sk = name_key(s["name"])
        same = sk == key or (min(len(sk), len(key)) >= 5 and (sk in key or key in sk))
        near = ev.get("lat") is None or live_events._km(ev["lat"], ev["lng"], s["lat"], s["lng"]) <= VENUE_MATCH_KM
        if same and near:
            return s
    return None


def is_night_event(ev: dict, spots: list[dict]) -> bool:
    return (bool(_NIGHT_GENRE_RE.search(ev.get("genre") or "")) or bool(_PARTY_RE.search(ev.get("name") or ""))
            or bool(ev.get("adults_only")) or _spot_for(ev, spots) is not None)


def _result(job, default):
    try:
        return job.result(timeout=20)
    except Exception:            # one slow/broken source never sinks the tab
        return default


def _spot_score(s: dict) -> float:
    return (2.0 * bool(s.get("events")) + 0.5 * math.log2(1 + (s.get("notability") or 0))
            + {True: 0.4, False: -1.0}.get(s.get("open_tonight"), 0.0) - 0.12 * (s.get("distance_km") or 0))


def feed(lat: float, lng: float, *, window: str = "tonight", radius_km: float = 10,
         tz: str | None = None, now: datetime | None = None) -> dict:
    window = window if window in WINDOWS else "tonight"
    now = now or datetime.now(timezone.utc)
    tz = venue_index.tz_for(lat, lng, tz)      # the place's night, not the phone's
    start_utc, end_utc = live_events.window_range(window, tz, now)
    day = special_days.active(special_days.local_today(now))
    jobs = {
        "music": _POOL.submit(ticketmaster.search_events, lat, lng, radius_km=radius_km, size=60,
                              classification="Music", start_utc=start_utc, end_utc=end_utc),
        "spots": _POOL.submit(venue_index.nightlife, lat, lng, radius_km * 1000),
        "own": _POOL.submit(own_events, start_utc, end_utc),
        "seatgeek": _POOL.submit(seatgeek.search_events, lat, lng, radius_km=radius_km, size=50,
                                 start_utc=start_utc, end_utc=end_utc),
    }
    if day and day.get("keyword"):
        # from now through the special day itself, so its parties show in the days before
        day_end = datetime.combine(datetime.fromisoformat(day["end"]).date(), time(23, 59),
                                   special_days.TZ).astimezone(timezone.utc)
        jobs["special"] = _POOL.submit(ticketmaster.search_events, lat, lng, radius_km=radius_km, size=20,
                                       keyword=day["keyword"], start_utc=start_utc,
                                       end_utc=max(end_utc, day_end))
    spots_found = _result(jobs["spots"], None)
    spots = spots_found or []
    special = [dict(e, special_day=day["key"]) for e in _result(jobs.get("special"), [])] if day else []
    ticketed = _result(jobs["music"], []) + _result(jobs["seatgeek"], [])
    candidates = _result(jobs["own"], []) + special + [e for e in ticketed if is_night_event(e, spots)]

    events, seen = [], set()
    for ev in candidates:
        key = (name_key(ev.get("name")), ev.get("date"))
        if not key[0] or key in seen:
            continue
        seen.add(key)
        if ev.get("lat") is not None and ev.get("lng") is not None:
            ev["distance_km"] = round(live_events._km(lat, lng, ev["lat"], ev["lng"]), 1)
            if ev["distance_km"] > radius_km:
                continue
        ev.setdefault("tz", tz)
        spot = _spot_for(ev, spots)
        if spot is not None:
            spot.setdefault("events", []).append({"name": ev["name"], "start": ev.get("start"),
                                                  "source": ev.get("source")})
        events.append(ev)
    far = datetime.max.replace(tzinfo=timezone.utc)
    events.sort(key=lambda e: (0 if live_events.is_on_now(e, now) else 1, live_events._start(e) or far))

    tonight = datetime.combine(special_days.local_today(now), time(23, 0), special_days.TZ)
    for s in spots:
        s["open_tonight"] = is_open(s.get("hours"), tonight)
        s["events"] = (s.get("events") or [])[:3]
    spots.sort(key=_spot_score, reverse=True)
    themed = ([s for s in spots if special_days.matches(day["key"], s.get("categories"), s.get("area"))][:12]
              if day else [])
    counts = Counter(s["kind"] for s in spots)
    return {
        "window": window,
        "special_day": day,
        "events": events[:40],
        "themed_spots": themed,
        "spots": spots[:150],
        "kinds": [{"key": k, "label": label, "count": counts[k]}
                  for k, label, _ in venue_index.NIGHT_KINDS if counts[k]],
        "covered": spots_found is not None,
        "ticketed": ticketmaster.available() or seatgeek.available(),
    }


def tonight_events_for_plan(lat: float, lng: float, radius_km: float, key: str | None) -> list[dict]:
    """A special day's ticketed events near a plan (for "plan made on St. Patrick's Day")."""
    day = special_days.BY_KEY.get(key or "")
    if not day or not day.keyword:
        return []
    start_utc, end_utc = live_events.window_range("tonight")
    return [dict(e, special_day=day.key) for e in ticketmaster.search_events(
        lat, lng, radius_km=radius_km, size=6, keyword=day.keyword, start_utc=start_utc,
        end_utc=end_utc + timedelta(hours=1))]
