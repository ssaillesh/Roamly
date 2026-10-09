"""Recording side of the admin dashboard: who's active, what they planned,
how the server and outside APIs are doing, and the daily cleanup.

Everything here is best-effort: a failure to record a stat is logged and
swallowed, never surfaced to the user whose request triggered it.
"""
import asyncio
import logging
import math
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.middleware import request_stats
from app.models import (
    ApiUsage, DailyRollup, HealthSnapshot, PlanLog, User, UserActivityDay,
)
from app.redis_client import redis_client
from app.services import usage

log = logging.getLogger(__name__)

RETENTION_DAYS = 90          # raw plan logs + health snapshots
FLUSH_SECONDS = 60

# Keep in sync with the backfill in alembic 0010_admin_analytics.
TEST_EMAIL_DOMAINS = {"example.com", "example.org", "example.net", "ex.com",
                      "test.trekrank.app", "demo.trekrank.app"}


def looks_like_test(email: str, username: str) -> bool:
    domain = email.rsplit("@", 1)[-1].lower()
    return (domain in TEST_EMAIL_DOMAINS or domain.endswith(".test")
            or username.lower().startswith("smoke_"))


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


# --- activity -----------------------------------------------------------------

def mark_active(db: Session, user: User) -> None:
    """Record that `user` used the app today. One DB write per user per day:
    a Redis key remembers who's already been recorded."""
    today = utc_today()
    try:
        if not redis_client.set(f"seen:{user.id}:{today}", 1, nx=True, ex=2 * 86400):
            return
    except Exception:
        pass   # Redis down: fall through and write (the insert is idempotent)
    try:
        db.execute(insert(UserActivityDay).values(user_id=user.id, day=today)
                   .on_conflict_do_nothing())
        user.last_seen_at = datetime.now(timezone.utc)
        db.commit()
    except Exception:
        db.rollback()
        log.exception("mark_active failed")


# --- plan logs ----------------------------------------------------------------

def _stops(resp) -> int:
    if getattr(resp, "plan", None):
        return len(resp.plan.stops)
    if getattr(resp, "days", None):
        return sum(len(d.stops) for d in resp.days)
    return sum(len(s.options) for s in getattr(resp, "sections", None) or [])


def save_plan_log(*, user_id, endpoint: str, user_text: str | None, prefs: dict | None,
                  lat: float | None, lng: float | None, resp, tracker: dict,
                  duration_ms: int) -> None:
    """Write one plan_logs row. Runs as a background task after the response
    has been sent, so it can't slow down or break planning."""
    try:
        from app.services.geocoding import reverse_area
        area = None
        if isinstance((prefs or {}).get("location"), str) and prefs["location"].strip():
            area = prefs["location"].strip()[:100]
        elif lat is not None and lng is not None:
            area = (reverse_area(lat, lng) or "")[:100] or None
        rtype = getattr(resp, "type", None) or ("options" if getattr(resp, "ok", False) else "message")
        stops = _stops(resp)
        with SessionLocal() as db:
            db.add(PlanLog(
                user_id=user_id, endpoint=endpoint, user_text=(user_text or None),
                prefs=_jsonable(prefs), lat=lat, lng=lng, area=area, response_type=rtype,
                stops=stops,
                # Searched (a location was known) but came back empty — not a
                # follow-up question, an events answer, or "where are you?".
                found_nothing=(stops == 0 and lat is not None
                               and rtype in ("message", "options", "itinerary")),
                llm_calls=tracker["llm_calls"], llm_failures=tracker["llm_failures"],
                duration_ms=duration_ms, response=resp.model_dump(mode="json"),
            ))
            db.commit()
    except Exception:
        log.exception("save_plan_log failed")


def _jsonable(d):
    if not isinstance(d, dict):
        return None
    out = {}
    for k, v in d.items():
        if isinstance(v, (set, frozenset)):
            v = sorted(v)
        if isinstance(v, (str, int, float, bool, list, dict)) or v is None:
            out[k] = v
    return out


# --- server health + API usage (once a minute) ---------------------------------

def _pct(sorted_vals: list[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    i = min(len(sorted_vals) - 1, max(0, math.ceil(p * len(sorted_vals)) - 1))
    return round(sorted_vals[i], 1)


def flush_stats(cold_start: bool = False) -> None:
    window = request_stats.drain()
    counts = usage.drain()
    routes, every = {}, []
    n4 = n5 = 0
    for route, (lat, c4, c5) in window.items():
        lat.sort()
        every.extend(lat)
        n4, n5 = n4 + c4, n5 + c5
        routes[route] = {"n": len(lat), "4xx": c4, "5xx": c5,
                         "p50": _pct(lat, 0.5), "p95": _pct(lat, 0.95)}
    every.sort()
    try:
        with SessionLocal() as db:
            if every or cold_start:
                db.add(HealthSnapshot(
                    ts=datetime.now(timezone.utc), requests=len(every), status_4xx=n4,
                    status_5xx=n5, p50_ms=_pct(every, 0.5), p95_ms=_pct(every, 0.95),
                    routes=routes, cold_start=cold_start))
            for (day, provider), (calls, errors) in counts.items():
                stmt = insert(ApiUsage).values(day=day, provider=provider, calls=calls, errors=errors)
                db.execute(stmt.on_conflict_do_update(
                    index_elements=["day", "provider"],
                    set_={"calls": ApiUsage.calls + calls, "errors": ApiUsage.errors + errors}))
            db.commit()
    except Exception:
        usage.restore(counts)
        log.exception("flush_stats failed")


# --- daily rollups + retention --------------------------------------------------

def _rollup_day(db: Session, day: date) -> None:
    """Save the day's totals from the raw rows that are about to be deleted.
    Plan totals count real accounts only (not is_test / is_admin), like the
    dashboard's default view."""
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    rows: Counter = Counter()

    plans = db.execute(select(
        PlanLog.endpoint, PlanLog.found_nothing, PlanLog.llm_calls, PlanLog.llm_failures,
        PlanLog.duration_ms, PlanLog.area, PlanLog.prefs, PlanLog.user_id,
    ).join(User, User.id == PlanLog.user_id)
     .where(PlanLog.created_at >= start, PlanLog.created_at < end,
            User.is_test.is_(False), User.is_admin.is_(False))).all()
    durations = []
    for p in plans:
        rows[("plans", p.endpoint)] += 1
        rows[("plans_nothing", "")] += p.found_nothing
        rows[("plans_llm_used", "")] += p.llm_calls > 0
        rows[("plans_llm_failed", "")] += p.llm_failures > 0
        durations.append(p.duration_ms)
        if p.area:
            rows[("area", p.area[:100])] += 1
        prefs = p.prefs or {}
        for field in ("vibe", "time_of_day", "group_type"):
            if prefs.get(field):
                rows[(field, str(prefs[field])[:100])] += 1
    if plans:
        rows[("plan_users", "")] = len({p.user_id for p in plans})
        durations.sort()
        rows[("plan_p50_ms", "")] = _pct(durations, 0.5)
        rows[("plan_p95_ms", "")] = _pct(durations, 0.95)

    snaps = db.execute(select(
        HealthSnapshot.requests, HealthSnapshot.status_4xx, HealthSnapshot.status_5xx,
        HealthSnapshot.p95_ms, HealthSnapshot.cold_start,
    ).where(HealthSnapshot.ts >= start, HealthSnapshot.ts < end)).all()
    for s in snaps:
        rows[("requests", "")] += s.requests
        rows[("status_4xx", "")] += s.status_4xx
        rows[("status_5xx", "")] += s.status_5xx
        rows[("cold_starts", "")] += s.cold_start
    p95s = sorted(s.p95_ms for s in snaps if s.p95_ms is not None)
    if p95s:
        rows[("p95_ms", "")] = _pct(p95s, 0.5)    # the typical minute's p95

    for (metric, key), value in rows.items():
        db.execute(insert(DailyRollup).values(day=day, metric=metric, key=key, value=value or 0)
                   .on_conflict_do_update(index_elements=["day", "metric", "key"],
                                          set_={"value": value or 0}))


def daily_maintenance(db: Session, today: date | None = None) -> int:
    """Roll up, then delete, raw rows older than RETENTION_DAYS. Returns the
    number of days rolled up."""
    today = today or utc_today()
    cutoff = today - timedelta(days=RETENTION_DAYS)
    cutoff_ts = datetime(cutoff.year, cutoff.month, cutoff.day, tzinfo=timezone.utc)
    old_days = db.execute(text("""
        SELECT DISTINCT (created_at AT TIME ZONE 'UTC')::date FROM plan_logs WHERE created_at < :c
        UNION
        SELECT DISTINCT (ts AT TIME ZONE 'UTC')::date FROM health_snapshots WHERE ts < :c
    """), {"c": cutoff_ts}).scalars().all()
    for day in sorted(old_days):
        _rollup_day(db, day)
    db.execute(delete(PlanLog).where(PlanLog.created_at < cutoff_ts))
    db.execute(delete(HealthSnapshot).where(HealthSnapshot.ts < cutoff_ts))
    db.commit()
    return len(old_days)


def _maintenance_once_a_day() -> None:
    try:
        if not redis_client.set(f"admin:maintenance:{utc_today()}", 1, nx=True, ex=2 * 86400):
            return
    except Exception:
        return   # no Redis lock, no run: several processes mustn't race on deletes
    try:
        with SessionLocal() as db:
            daily_maintenance(db)
    except Exception:
        log.exception("daily maintenance failed")


async def stats_loop() -> None:
    """Started from main.py's lifespan: flush stats every minute, and run the
    daily cleanup on the first tick of each UTC day (or right after a wake-up)."""
    cold = True
    while True:
        await asyncio.to_thread(flush_stats, cold)
        await asyncio.to_thread(_maintenance_once_a_day)
        cold = False
        await asyncio.sleep(FLUSH_SECONDS)
