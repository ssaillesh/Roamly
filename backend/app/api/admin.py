"""Admin dashboard API: stats on users, reach, the planner and server health,
plus user management, waitlist invites and CSV exports.

Every route needs an admin (require_admin). Stats leave out test accounts
(is_test) and admins unless ?include_test=true. Every change an admin makes
is written to admin_audit_log.
"""
import csv
import io
import time
import uuid
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import Numeric, and_, distinct, func, or_, select, text
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.middleware.auth import require_admin
from app.models import (
    AdminAuditLog, ApiUsage, DailyRollup, HealthSnapshot, NightlifeEvent, PlanLog, PlanReview, SharedPlan,
    TasteProfile, User, UserActivityDay, Venue, WaitlistSignup,
)
from app.api.nightlife import _as_utc, check_times, link_venue
from app.schemas.nightlife import AdminEventIn, AdminEventPatch
from app.redis_client import redis_client
from app.services import feedback, ga4, llm, taste_profile, usage
from app.services import events as events_svc
from app.services import foursquare, google_places, seatgeek, yelp
from app.services.analytics import RETENTION_DAYS, utc_today
from app.services.email import email_enabled, email_problem, send_email, send_password_reset
from app.services.security import create_reset_token
from app.services.planner import LOCAL_TZ

router = APIRouter(prefix="/admin", tags=["admin"])

_STARTED = time.time()

Days = Query(30, ge=1, le=730)
IncludeTest = Query(False, description="Include test accounts and admins in the stats")


# --- helpers ------------------------------------------------------------------

def _real(include_test: bool):
    """Condition on User for the accounts that count in the stats."""
    return True if include_test else and_(User.is_test.is_(False), User.is_admin.is_(False))


def _since(days: int) -> datetime:
    start = utc_today() - timedelta(days=days - 1)
    return datetime(start.year, start.month, start.day, tzinfo=timezone.utc)


def _cutoff() -> date:
    """Raw plan logs / health snapshots exist from this day on; older days are rollups."""
    return utc_today() - timedelta(days=RETENTION_DAYS)


def _day_range(days: int) -> list[str]:
    start = utc_today() - timedelta(days=days - 1)
    return [(start + timedelta(days=i)).isoformat() for i in range(days)]


def _audit(db: Session, admin: User, action: str, target: str | None = None, **details) -> None:
    db.add(AdminAuditLog(admin_id=admin.id, action=action, target=target, details=details or None))


def _user_or_404(db: Session, user_id: uuid.UUID) -> User:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return user


def _rollups(db: Session, metrics: list[str], since: date) -> list:
    """Daily rollups (days older than the raw-data window) for the given metrics."""
    return db.execute(select(DailyRollup.day, DailyRollup.metric, DailyRollup.key, DailyRollup.value)
                      .where(DailyRollup.metric.in_(metrics), DailyRollup.day >= since,
                             DailyRollup.day < _cutoff())).all()


def _ga4(fn):
    """Run a GA4 lookup, turning 'not connected' / API errors into a payload the
    page can show instead of failing the whole section."""
    if not ga4.configured():
        return {"connected": False, "error": f"Google Analytics isn't connected: {ga4.problem()}."}
    try:
        return {"connected": True, **fn()}
    except ga4.GA4Error as e:
        return {"connected": True, "error": str(e)}
    except Exception as e:   # network etc.
        return {"connected": True, "error": f"{type(e).__name__}: {str(e)[:200]}"}


# --- home: the overview's numbers, the review queue -----------------------------
# "Today" here is Toronto's day, the one the people making plans are living in.

_MADE = (PlanLog.response_type == "itinerary", PlanLog.endpoint.in_(("chat", "build")))
LOW_STARS = 2          # plan ratings at or under this go to the review queue


def _plan_of(resp: dict | None) -> dict | None:
    """The (first) plan in a logged answer: a night, or a trip's first day."""
    resp = resp or {}
    return resp.get("plan") or next(iter(resp.get("days") or []), None)


def _start_hour(plan: dict) -> int | None:
    start = plan.get("start")
    if isinstance(start, str):
        try:
            t = datetime.fromisoformat(start)
            return (t.astimezone(LOCAL_TZ) if t.tzinfo else t).hour
        except ValueError:
            pass
    first = (plan.get("stops") or [{}])[0].get("time")
    if isinstance(first, str):
        try:
            return datetime.strptime(first.strip().upper(), "%I:%M %p").hour
        except ValueError:
            return None
    return None


def _queue(db: Session, include_done: bool = False, limit: int = 50) -> dict:
    """What needs an admin's decision: event submissions waiting for approval, and
    low plan ratings nobody has read yet. With include_done, recent decided ones too."""
    items = []
    ev = select(NightlifeEvent).order_by(NightlifeEvent.created_at.desc()).limit(limit)
    ev = ev.where(NightlifeEvent.source == "submission") if include_done else ev.where(NightlifeEvent.status == "pending")
    events = db.scalars(ev).all()
    images = dict(db.execute(select(Venue.id, Venue.image).where(
        Venue.id.in_({e.venue_id for e in events if e.venue_id}))).all()) if events else {}
    for e in events:
        when = e.starts_at.astimezone(LOCAL_TZ)
        price = "Free" if e.free else f"${e.price_min:g}" if e.price_min else None
        items.append({
            "kind": "event", "id": str(e.id), "label": "Event", "title": e.title,
            "sub": " · ".join(filter(None, [when.strftime("%a %-I:%M %p").replace(":00", ""), e.venue_name, price])),
            "from": e.submitter_name or ("Public form" if e.source == "submission" else "Admin"),
            "at": e.created_at, "signal": e.kind.replace("_", " ").capitalize(),
            "image": images.get(e.venue_id), "status": e.status,
            "pending": e.status == "pending"})
    rv = (select(PlanReview, User.username).join(User, User.id == PlanReview.user_id)
          .where(PlanReview.stars <= LOW_STARS).order_by(PlanReview.created_at.desc()).limit(limit))
    if not include_done:
        rv = rv.where(PlanReview.reviewed_at.is_(None))
    for r, name in db.execute(rv).all():
        plan = (r.plan or {}).get("plan") or next(iter((r.plan or {}).get("days") or []), {}) or {}
        stops = plan.get("stops") or []
        items.append({
            "kind": "review", "id": str(r.id), "label": "Low rating",
            "title": " · ".join(s.get("name") for s in stops[:3] if s.get("name")) or "A plan",
            "sub": (f"\u201c{r.comment[:90]}\u201d" if r.comment else "No comment, just the stars"),
            "from": f"@{name}" if name else "Someone", "at": r.created_at,
            "signal": f"{r.stars} star{'s' if r.stars > 1 else ''}", "stars": r.stars,
            "image": next((s.get("image") for s in stops if s.get("image")), None),
            "status": "read" if r.reviewed_at else "new", "pending": r.reviewed_at is None})
    items.sort(key=lambda i: i["at"], reverse=True)
    pending = [i for i in items if i["pending"]]
    return {"items": items[:limit], "pending": len(pending),
            "oldest": min((i["at"] for i in pending), default=None)}


@router.get("/queue")
def queue(show: str = Query("pending", pattern="^(pending|all)$"), db: Session = Depends(get_db),
          _admin: User = Depends(require_admin)):
    out = _queue(db, include_done=show == "all")
    if show == "all":                 # the badge always counts what's waiting
        waiting = _queue(db)
        out.update(pending=waiting["pending"], oldest=waiting["oldest"])
    return out


@router.get("/home")
def home(include_test: bool = IncludeTest, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    real = _real(include_test)
    now = datetime.now(LOCAL_TZ)
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)

    def plans_between(a, b):
        return db.scalar(select(func.count(PlanLog.id)).join(User, User.id == PlanLog.user_id)
                         .where(real, *_MADE, PlanLog.created_at >= a, PlanLog.created_at < b)) or 0
    today = plans_between(day0, now + timedelta(seconds=1))
    last_week = plans_between(day0 - timedelta(days=7), now - timedelta(days=7))    # same weekday, same hour
    shared = db.scalar(select(func.count(SharedPlan.id)).join(User, User.id == SharedPlan.user_id)
                       .where(real, SharedPlan.created_at >= day0)) or 0
    venues = db.scalar(select(func.count()).select_from(Venue)) or 0
    with_photos = db.scalar(select(func.count()).select_from(Venue).where(Venue.image.is_not(None))) or 0
    tonight_end = day0 + timedelta(days=1, hours=6)
    events_tonight = db.scalar(select(func.count()).select_from(NightlifeEvent).where(
        NightlifeEvent.status == "approved", NightlifeEvent.starts_at >= now - timedelta(hours=3),
        NightlifeEvent.starts_at < tonight_end)) or 0

    hours, added, images = Counter(), Counter(), {}
    for resp in db.scalars(select(PlanLog.response).join(User, User.id == PlanLog.user_id)
                           .where(real, *_MADE, PlanLog.created_at >= day0)):
        plan = _plan_of(resp)
        if not plan:
            continue
        h = _start_hour(plan)
        if h is not None:
            hours[h] += 1
        for st in plan.get("stops") or []:
            if st.get("name") and st.get("slot") != "event":
                added[st["name"]] += 1
                images.setdefault(st["name"], st.get("image"))
    order = [(6 + i) % 24 for i in range(24)]          # the day as a night planner sees it: 6am → 5am
    q = _queue(db, limit=5)
    return {
        "now": now.isoformat(), "name": admin.display_name or admin.username,
        "plans_today": today, "plans_last_week": last_week,
        "shared_today": shared, "share_rate": round(shared / today, 3) if today else None,
        "venues": venues, "venues_with_photos": with_photos, "events_tonight": events_tonight,
        "queue": q,
        "start_hours": [{"hour": h, "count": hours.get(h, 0)} for h in order],
        "most_added": [{"name": n, "count": c, "image": images.get(n)} for n, c in added.most_common(5)],
    }


# --- overview -----------------------------------------------------------------

@router.get("/overview")
def overview(include_test: bool = IncludeTest, db: Session = Depends(get_db),
             _admin: User = Depends(require_admin)):
    real = _real(include_test)
    today = utc_today()

    def active_since(n):
        return db.scalar(select(func.count(distinct(UserActivityDay.user_id)))
                         .join(User, User.id == UserActivityDay.user_id)
                         .where(real, UserActivityDay.day > today - timedelta(days=n)))

    def plans_since(dt):
        return db.scalar(select(func.count(PlanLog.id)).join(User, User.id == PlanLog.user_id)
                         .where(real, PlanLog.created_at >= dt))

    day_ago = datetime.now(timezone.utc) - timedelta(hours=24)
    h = db.execute(select(func.coalesce(func.sum(HealthSnapshot.requests), 0),
                          func.coalesce(func.sum(HealthSnapshot.status_5xx), 0),
                          func.max(HealthSnapshot.ts))
                   .where(HealthSnapshot.ts >= day_ago)).one()
    p95 = db.scalar(select(func.percentile_cont(0.5).within_group(HealthSnapshot.p95_ms))
                    .where(HealthSnapshot.ts >= day_ago))
    surveyed = db.scalar(select(func.count()).select_from(TasteProfile)
                         .join(User, User.id == TasteProfile.user_id)
                         .where(real, TasteProfile.completed_at.is_not(None)))
    total = db.scalar(select(func.count(User.id)).where(real))
    return {
        "users_total": total,
        "users_new_today": db.scalar(select(func.count(User.id)).where(real, User.created_at >= _since(1))),
        "users_new_7d": db.scalar(select(func.count(User.id)).where(real, User.created_at >= _since(7))),
        "users_new_30d": db.scalar(select(func.count(User.id)).where(real, User.created_at >= _since(30))),
        "users_disabled": db.scalar(select(func.count(User.id)).where(real, User.is_disabled.is_(True))),
        "test_accounts": db.scalar(select(func.count(User.id)).where(User.is_test.is_(True))),
        "dau": active_since(1), "wau": active_since(7), "mau": active_since(30),
        "survey_completed": surveyed,
        "survey_rate": round(surveyed / total, 3) if total else None,
        "plans_today": plans_since(_since(1)),
        "plans_7d": plans_since(_since(7)),
        "waitlist_total": db.scalar(select(func.count(WaitlistSignup.id))),
        "requests_24h": int(h[0]),
        "errors_5xx_24h": int(h[1]),
        "error_rate_24h": round(h[1] / h[0], 4) if h[0] else None,
        "p95_ms_24h": round(p95, 1) if p95 is not None else None,
        "last_snapshot_at": h[2],
        "status": _live_status(db),
    }


def _live_status(db: Session) -> dict:
    out = {}
    t = time.perf_counter()
    try:
        db.execute(text("SELECT 1"))
        out["database"] = {"ok": True, "ms": round((time.perf_counter() - t) * 1000, 1)}
    except Exception as e:
        out["database"] = {"ok": False, "error": str(e)[:200]}
    t = time.perf_counter()
    try:
        redis_client.ping()
        out["redis"] = {"ok": True, "ms": round((time.perf_counter() - t) * 1000, 1)}
    except Exception as e:
        out["redis"] = {"ok": False, "error": str(e)[:200]}
    out["uptime_s"] = int(time.time() - _STARTED)
    out["environment"] = settings.environment
    out["configured"] = {
        "llm": llm.available(), "yelp": yelp.available(), "foursquare": foursquare.available(),
        "google_places": google_places.available(), "ticketmaster": events_svc.available(),
        "seatgeek": seatgeek.available(), "email": email_enabled(), "ga4": ga4.configured(),
    }
    def missing(*names):
        return " and ".join(n for n in names if not getattr(settings, n.lower())) + \
            " not set on the API service"

    ai = llm.health()
    out["llm"] = ai
    out["why_off"] = {k: v for k, v in {
        "llm": (missing("LLM_API_KEY") + ": plans use the rule-based planner" if not llm.available()
                else f"on, but failing ({ai['model']}): {ai['last_error']['error']}" if ai["failing"]
                else None),
        "yelp": None if yelp.available() else "switched off in code: " + usage.DISABLED["yelp"],
        "foursquare": None if foursquare.available() else "switched off in code: " + usage.DISABLED["foursquare"],
        "google_places": None if google_places.available() else "switched off in code: "
                         + usage.DISABLED["google_places"],
        "ticketmaster": None if events_svc.available() else missing("TICKETMASTER_API_KEY"),
        "seatgeek": (None if seatgeek.available()
                     else f"paused: {usage.paused('seatgeek')['reason']}" if usage.paused("seatgeek")
                     else missing("SEATGEEK_CLIENT_ID") + " (Nightlife tab only)"),
        "email": email_problem(),
        "ga4": ga4.problem(),
    }.items() if v}
    out["free_tier_only"] = settings.free_tier_only
    return out


# --- activity & retention -------------------------------------------------------

@router.get("/activity")
def activity(days: int = Days, include_test: bool = IncludeTest, db: Session = Depends(get_db),
             _admin: User = Depends(require_admin)):
    real = _real(include_test)
    since = _since(days)
    signup_day = func.date(func.timezone("UTC", User.created_at))
    signups = dict(db.execute(select(signup_day, func.count(User.id))
                              .where(real, User.created_at >= since).group_by(signup_day)).all())
    active = dict(db.execute(select(UserActivityDay.day, func.count(UserActivityDay.user_id))
                             .join(User, User.id == UserActivityDay.user_id)
                             .where(real, UserActivityDay.day >= since.date())
                             .group_by(UserActivityDay.day)).all())
    series = [{"day": d, "signups": signups.get(date.fromisoformat(d), 0),
               "active": active.get(date.fromisoformat(d), 0)} for d in _day_range(days)]
    return {"series": series, "retention": _retention(db, include_test)}


def _retention(db: Session, include_test: bool, weeks: int = 12) -> list[dict]:
    """Weekly sign-up cohorts: % who came back on day 1, in days 7–13 and in days 30–59."""
    rows = db.execute(text("""
        WITH cohort AS (
            SELECT u.id, (u.created_at AT TIME ZONE 'UTC')::date AS d0
            FROM users u
            WHERE u.created_at >= :since AND (:all OR (NOT u.is_test AND NOT u.is_admin))
        )
        SELECT date_trunc('week', c.d0)::date AS week, count(*) AS size,
               count(*) FILTER (WHERE EXISTS (SELECT 1 FROM user_activity_days a
                     WHERE a.user_id = c.id AND a.day = c.d0 + 1)) AS d1,
               count(*) FILTER (WHERE EXISTS (SELECT 1 FROM user_activity_days a
                     WHERE a.user_id = c.id AND a.day BETWEEN c.d0 + 7 AND c.d0 + 13)) AS w1,
               count(*) FILTER (WHERE EXISTS (SELECT 1 FROM user_activity_days a
                     WHERE a.user_id = c.id AND a.day BETWEEN c.d0 + 30 AND c.d0 + 59)) AS m1,
               max(c.d0) AS newest
        FROM cohort c GROUP BY 1 ORDER BY 1
    """), {"since": _since(weeks * 7), "all": include_test}).all()
    today = utc_today()
    out = []
    for r in rows:
        age = (today - r.newest).days          # the youngest member decides what's measurable

        def pct(n, need):
            return round(n / r.size, 3) if r.size and age >= need else None
        out.append({"week": r.week, "size": r.size, "day1": pct(r.d1, 1),
                    "week2": pct(r.w1, 13), "month2": pct(r.m1, 59)})
    return out


# --- funnel & traffic ------------------------------------------------------------

@router.get("/funnel")
def funnel(days: int = Days, include_test: bool = IncludeTest, db: Session = Depends(get_db),
           _admin: User = Depends(require_admin)):
    real = _real(include_test)
    since = _since(days)
    cohort = select(User.id).where(real, User.created_at >= since).subquery()
    signed_up = db.scalar(select(func.count()).select_from(cohort))
    surveyed = db.scalar(select(func.count()).select_from(TasteProfile)
                         .where(TasteProfile.user_id.in_(select(cohort.c.id)),
                                TasteProfile.completed_at.is_not(None)))
    per_user = (select(PlanLog.user_id, func.count(PlanLog.id).label("n"),
                       func.count(PlanLog.id).filter(PlanLog.response_type == "itinerary").label("it"))
                .where(PlanLog.user_id.in_(select(cohort.c.id))).group_by(PlanLog.user_id).subquery())
    planned, planned2, got_itinerary = db.execute(select(
        func.count(), func.count().filter(per_user.c.n >= 2),
        func.count().filter(per_user.c.it >= 1)).select_from(per_user)).one()
    waitlist = db.scalar(select(func.count(WaitlistSignup.id)).where(WaitlistSignup.created_at >= since))

    def web():
        rows = ga4.run_report(dimensions=["eventName"], metrics=["eventCount", "totalUsers"],
                              days=days, event_filter=["session_start", "waitlist_join", "sign_up",
                                                       "survey_complete", "plan_built"])
        by = {r["eventName"]: r for r in rows}
        return {"visitors": by.get("session_start", {}).get("totalUsers", 0),
                "events": {k: v["eventCount"] for k, v in by.items()}}

    return {
        "steps": [
            {"key": "signed_up", "label": "Signed up", "count": signed_up},
            {"key": "survey", "label": "Finished taste survey", "count": surveyed},
            {"key": "planned", "label": "Made a first plan", "count": planned},
            {"key": "itinerary", "label": "Got an itinerary", "count": got_itinerary},
            {"key": "planned2", "label": "Came back for a 2nd plan", "count": planned2},
        ],
        "waitlist_signups": waitlist,
        "note": (f"Plan steps only see the last {RETENTION_DAYS} days of plan logs."
                 if days > RETENTION_DAYS else None),
        "web": _ga4(web),
    }


@router.get("/traffic")
def traffic(days: int = Days, db: Session = Depends(get_db), _admin: User = Depends(require_admin)):
    since = _since(days)
    source = func.coalesce(WaitlistSignup.source, "(none)")
    waitlist_sources = db.execute(
        select(source, func.count(WaitlistSignup.id))
        .where(WaitlistSignup.created_at >= since).group_by(source)
        .order_by(func.count(WaitlistSignup.id).desc())
    ).all()

    def web():
        return {
            "sources": ga4.run_report(dimensions=["sessionSource", "sessionMedium"],
                                      metrics=["sessions", "totalUsers", "engagementRate"], days=days),
            "campaigns": ga4.run_report(dimensions=["sessionCampaignName"],
                                        metrics=["sessions", "totalUsers"], days=days),
            "landing_pages": ga4.run_report(dimensions=["landingPage"],
                                            metrics=["sessions", "bounceRate"], days=days),
            "devices": ga4.run_report(dimensions=["deviceCategory"], metrics=["sessions"], days=days),
            "daily": ga4.run_report(dimensions=["date"], metrics=["sessions", "totalUsers"],
                                    days=days, limit=days + 1),
        }

    return {"waitlist_sources": [{"source": s, "count": n} for s, n in waitlist_sources],
            "web": _ga4(web)}


# --- where users are -----------------------------------------------------------

@router.get("/geo")
def geo(days: int = Days, include_test: bool = IncludeTest, db: Session = Depends(get_db),
        _admin: User = Depends(require_admin)):
    real = _real(include_test)
    since = _since(days)
    areas = Counter(dict(db.execute(
        select(PlanLog.area, func.count(PlanLog.id)).join(User, User.id == PlanLog.user_id)
        .where(real, PlanLog.created_at >= since, PlanLog.area.is_not(None))
        .group_by(PlanLog.area)).all()))
    for _, _, key, value in _rollups(db, ["area"], since.date()):
        areas[key] += int(value)
    # ~1 km cells, so the map shows where plans cluster rather than exact spots.
    cell_lat = func.round(PlanLog.lat.cast(Numeric), 2)
    cell_lng = func.round(PlanLog.lng.cast(Numeric), 2)
    points = db.execute(
        select(cell_lat, cell_lng, func.count(PlanLog.id))
        .join(User, User.id == PlanLog.user_id)
        .where(real, PlanLog.created_at >= since, PlanLog.lat.is_not(None))
        .group_by(cell_lat, cell_lng).order_by(func.count(PlanLog.id).desc()).limit(2000)).all()
    home = db.execute(select(User.home_city, func.count(User.id))
                      .where(real, User.home_city.is_not(None), User.home_city != "")
                      .group_by(User.home_city).order_by(func.count(User.id).desc()).limit(25)).all()

    def web():
        return {"countries": ga4.run_report(dimensions=["country"], metrics=["totalUsers"], days=days),
                "cities": ga4.run_report(dimensions=["city", "country"], metrics=["totalUsers"], days=days)}

    return {
        "plan_areas": [{"area": a, "plans": n} for a, n in areas.most_common(30)],
        "plan_points": [{"lat": float(a), "lng": float(b), "plans": n} for a, b, n in points],
        "home_cities": [{"city": c, "users": n} for c, n in home],
        "web": _ga4(web),
    }


# --- reviews ------------------------------------------------------------------
# What people thought of the plans they were given: 1–5 stars and a comment
# (POST /plan/review). New ones are unread until an admin marks them reviewed.

_STAR_FILTERS = {"all": (1, 5), "low": (1, 2), "mid": (3, 3), "high": (4, 5),
                 **{str(n): (n, n) for n in range(1, 6)}}


def _plans_of(r: PlanReview) -> list[dict]:
    return [r.plan["plan"]] if r.plan.get("plan") else list(r.plan.get("days") or [])


def _review_row(r: PlanReview, username: str | None) -> dict:
    plans = _plans_of(r)
    first = plans[0] if plans else {}
    stops = [s for p in plans for s in p.get("stops") or []]
    return {"id": r.id, "created_at": r.created_at, "updated_at": r.updated_at, "user_id": r.user_id,
            "username": username, "stars": r.stars, "comment": r.comment, "reviewed_at": r.reviewed_at,
            "vibe": first.get("vibe"), "days": len(plans) if len(plans) > 1 else None,
            "stops": len(stops), "names": [s.get("name") for s in stops][:8],
            "quick": bool(first.get("quick")), "cost": first.get("estimated_cost"),
            "party_size": first.get("party_size")}


@router.get("/reviews")
def reviews(days: int = Days, include_test: bool = IncludeTest,
            stars: str = Query("all", pattern="^(all|low|mid|high|[1-5])$"),
            show: str = Query("all", pattern="^(all|new|reviewed)$"), comments: bool = False,
            page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=200),
            db: Session = Depends(get_db), _admin: User = Depends(require_admin)):
    base = [_real(include_test), PlanReview.created_at >= _since(days)]
    joined = lambda q: q.select_from(PlanReview).join(User, User.id == PlanReview.user_id)
    n, avg, with_comment, unread = db.execute(joined(select(
        func.count(), func.avg(PlanReview.stars),
        func.count().filter(PlanReview.comment.is_not(None)),
        func.count().filter(PlanReview.reviewed_at.is_(None)))).where(*base)).one()
    by_stars = dict(db.execute(joined(select(PlanReview.stars, func.count())).where(*base)
                               .group_by(PlanReview.stars)).all())
    lo, hi = _STAR_FILTERS[stars]
    where = [*base, PlanReview.stars.between(lo, hi)]
    if show == "new":
        where.append(PlanReview.reviewed_at.is_(None))
    elif show == "reviewed":
        where.append(PlanReview.reviewed_at.is_not(None))
    if comments:
        where.append(PlanReview.comment.is_not(None))
    total = db.scalar(joined(select(func.count())).where(*where)) or 0
    rows = db.execute(select(PlanReview, User.username).join(User, User.id == PlanReview.user_id)
                      .where(*where).order_by(PlanReview.created_at.desc())
                      .offset((page - 1) * per_page).limit(per_page)).all()
    return {"summary": {"count": n, "avg": round(float(avg), 2) if avg is not None else None,
                        "with_comment": with_comment, "unread": unread,
                        "by_stars": [{"key": f"{s} star{'s' if s > 1 else ''}", "stars": s,
                                      "count": by_stars.get(s, 0)} for s in range(5, 0, -1)]},
            "total": total, "page": page, "per_page": per_page,
            "reviews": [_review_row(r, name) for r, name in rows]}


def _review_or_404(db: Session, review_id: uuid.UUID) -> PlanReview:
    r = db.get(PlanReview, review_id)
    if not r:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Review not found")
    return r


@router.get("/reviews/{review_id}")
def review_detail(review_id: uuid.UUID, db: Session = Depends(get_db), _admin: User = Depends(require_admin)):
    r = _review_or_404(db, review_id)
    u = db.get(User, r.user_id)
    return {**_review_row(r, u.username if u else None), "plans": _plans_of(r), "session_id": r.session_id,
            "email": u.email if u else None}


class ReviewedBody(BaseModel):
    value: bool


@router.post("/reviews/{review_id}/reviewed")
def mark_reviewed(review_id: uuid.UUID, body: ReviewedBody, db: Session = Depends(get_db),
                  admin: User = Depends(require_admin)):
    r = _review_or_404(db, review_id)
    r.reviewed_at = datetime.now(timezone.utc) if body.value else None
    _audit(db, admin, "review_read" if body.value else "review_unread", str(r.id), stars=r.stars)
    db.commit()
    return {"id": r.id, "reviewed_at": r.reviewed_at}


# --- planner ------------------------------------------------------------------

_PLAN_DIMENSIONS = ("vibe", "time_of_day", "group_type")


@router.get("/planner")
def planner(days: int = Days, include_test: bool = IncludeTest, db: Session = Depends(get_db),
            _admin: User = Depends(require_admin)):
    real = _real(include_test)
    since = _since(days)
    day = func.date(func.timezone("UTC", PlanLog.created_at))
    rows = db.execute(
        select(day, PlanLog.endpoint, func.count(PlanLog.id),
               func.count(PlanLog.id).filter(PlanLog.found_nothing.is_(True)),
               func.count(PlanLog.id).filter(PlanLog.llm_calls > 0),
               func.count(PlanLog.id).filter(PlanLog.llm_failures > 0))
        .join(User, User.id == PlanLog.user_id)
        .where(real, PlanLog.created_at >= since).group_by(day, PlanLog.endpoint)).all()
    by_day = defaultdict(lambda: {"chat": 0, "options": 0, "build": 0, "edit": 0, "nothing": 0,
                                  "llm_used": 0, "llm_failed": 0})
    for d, endpoint, n, nothing, used, failed in rows:
        b = by_day[d.isoformat()]
        b[endpoint] = b.get(endpoint, 0) + n
        b["nothing"] += nothing
        b["llm_used"] += used
        b["llm_failed"] += failed
    old_metric = {"plans_nothing": "nothing", "plans_llm_used": "llm_used", "plans_llm_failed": "llm_failed"}
    for d, metric, key, value in _rollups(db, ["plans", *old_metric], since.date()):
        by_day[d.isoformat()][key if metric == "plans" else old_metric[metric]] += int(value)
    series = [{"day": d, **by_day[d]} for d in _day_range(days)]

    base = (select(PlanLog).join(User, User.id == PlanLog.user_id)
            .where(real, PlanLog.created_at >= since).subquery())
    t = db.execute(select(
        func.count(), func.count(distinct(base.c.user_id)),
        func.count().filter(base.c.found_nothing.is_(True)),
        func.count().filter(base.c.llm_calls > 0),
        func.count().filter(base.c.llm_failures > 0),
        func.percentile_cont(0.5).within_group(base.c.duration_ms),
        func.percentile_cont(0.95).within_group(base.c.duration_ms),
        func.count().filter(base.c.response_type == "itinerary"),
        func.count().filter(base.c.response["plan"]["quick"].astext == "true"),
    ).select_from(base)).one()
    total = t[0]
    latency = {e: {"p50": p50, "p95": p95} for e, p50, p95 in db.execute(select(
        base.c.endpoint, func.percentile_cont(0.5).within_group(base.c.duration_ms),
        func.percentile_cont(0.95).within_group(base.c.duration_ms)).group_by(base.c.endpoint)).all()}

    dims = {k: Counter() for k in (*_PLAN_DIMENSIONS, "response_type", "budget", "radius_km", "edit_ops")}
    for prefs, rtype, endpoint in db.execute(select(base.c.prefs, base.c.response_type, base.c.endpoint)).all():
        prefs = prefs or {}
        dims["response_type"][rtype or "?"] += 1
        if endpoint == "edit":           # which on-screen changes people make (POST /plan/edit)
            for o in prefs.get("ops") or []:
                if isinstance(o, dict) and o.get("op"):
                    dims["edit_ops"][str(o["op"])] += 1
        for k in _PLAN_DIMENSIONS:
            if prefs.get(k):
                dims[k][str(prefs[k])] += 1
        dims["budget"][_bucket(prefs.get("budget"), [25, 50, 100, 200, 400], "$")] += 1
        dims["radius_km"][_bucket(prefs.get("radius_km"), [2, 5, 10, 20], "", " km")] += 1
    for _, metric, key, value in _rollups(db, list(_PLAN_DIMENSIONS), since.date()):
        dims[metric][key] += int(value)
    dims["budget"].pop(None, None)
    dims["radius_km"].pop(None, None)

    # Conversations (one session_id per conversation on the page): how long they run,
    # and whether Rova ever asked the same question twice in one.
    per_session = (select(base.c.session_id, func.count().label("turns"))
                   .where(base.c.session_id.is_not(None)).group_by(base.c.session_id).subquery())
    s_count, s_turns = db.execute(select(func.count(), func.avg(per_session.c.turns))
                                  .select_from(per_session)).one()
    asks = (select(base.c.session_id, base.c.response["ask"].astext.label("ask"), func.count().label("n"))
            .where(base.c.session_id.is_not(None), base.c.response["ask"].astext.is_not(None))
            .group_by(base.c.session_id, base.c.response["ask"].astext).subquery())
    repeated = db.scalar(select(func.count(distinct(asks.c.session_id))).where(asks.c.n > 1))
    edits = db.execute(select(func.count(), func.count().filter(base.c.response_type == "itinerary"))
                       .where(base.c.intent == "edit_plan")).one()
    intents = db.execute(select(func.coalesce(base.c.intent, "(chips)"), func.count())
                         .group_by(func.coalesce(base.c.intent, "(chips)"))
                         .order_by(func.count().desc())).all()

    recent = db.execute(select(base.c.id, base.c.created_at, base.c.user_id, base.c.endpoint,
                               base.c.user_text, base.c.area, base.c.response_type, base.c.stops,
                               base.c.found_nothing, base.c.llm_failures, base.c.duration_ms)
                        .order_by(base.c.created_at.desc()).limit(25)).mappings().all()
    names = dict(db.execute(select(User.id, User.username)
                            .where(User.id.in_({r["user_id"] for r in recent}))).all())
    return {
        "series": series,
        "totals": {"plans": total, "users": t[1], "nothing": t[2], "llm_used": t[3], "llm_failed": t[4],
                   "nothing_rate": round(t[2] / total, 3) if total else None,
                   "backup_rate": round(1 - t[3] / total, 3) if total and llm.available() else None,
                   "llm_fail_rate": round(t[4] / t[3], 3) if t[3] else None,
                   "p50_ms": t[5], "p95_ms": t[6],
                   # plans made without the AI's picks and words (it was off or failed)
                   "quick_rate": round(t[8] / t[7], 3) if t[7] else None},
        "latency": latency,
        "breakdown": {k: [{"key": a, "count": n} for a, n in c.most_common(12)] for k, c in dims.items()},
        "recent": [{**r, "username": names.get(r["user_id"])} for r in recent],
        "sessions": {"count": s_count, "avg_turns": round(float(s_turns), 1) if s_turns else None,
                     "repeated_question": repeated,
                     "edits": edits[0], "edits_applied": edits[1],
                     "intents": [{"key": k, "count": n} for k, n in intents]},
        "llm_configured": llm.available(),
        "learned": _learned(db),
    }


def _learned(db: Session) -> dict:
    """The venues people keep most and drop most (services/feedback.py), with names."""
    out = feedback.top_and_bottom(db)
    vids = {r["vid"] for r in out["top"] + out["bottom"]}
    names = dict(db.execute(select(Venue.id, Venue.name).where(Venue.id.in_(vids))).all()) if vids else {}
    for r in out["top"] + out["bottom"]:
        r["name"] = names.get(r["vid"]) or r["vid"]
    out.update(min_users=feedback.MIN_USERS, weight=feedback.SIGNAL_WEIGHT)
    return out


def _bucket(v, edges, prefix="", suffix=""):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    lo = 0
    for e in edges:
        if v < e:
            return f"{prefix}{lo}–{prefix}{e}{suffix}" if lo else f"under {prefix}{e}{suffix}"
        lo = e
    return f"{prefix}{edges[-1]}+{suffix}"


@router.get("/plans/{plan_id}")
def plan_detail(plan_id: int, db: Session = Depends(get_db), _admin: User = Depends(require_admin)):
    p = db.get(PlanLog, plan_id)
    if not p:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plan log not found (older than "
                            f"{RETENTION_DAYS} days, or deleted with its user)")
    user = db.get(User, p.user_id) if p.user_id else None
    return {**_plan_row(p), "prefs": p.prefs, "response": p.response, "lat": p.lat, "lng": p.lng,
            "username": user.username if user else None}


def _plan_row(p: PlanLog) -> dict:
    return {"id": p.id, "created_at": p.created_at, "user_id": p.user_id, "endpoint": p.endpoint,
            "user_text": p.user_text, "area": p.area, "response_type": p.response_type,
            "stops": p.stops, "found_nothing": p.found_nothing, "llm_calls": p.llm_calls,
            "llm_failures": p.llm_failures, "duration_ms": p.duration_ms}


# --- taste insights -------------------------------------------------------------

@router.get("/taste")
def taste(include_test: bool = IncludeTest, db: Session = Depends(get_db),
          _admin: User = Depends(require_admin)):
    real = _real(include_test)
    rows = db.execute(select(TasteProfile.answers, TasteProfile.completed_at)
                      .join(User, User.id == TasteProfile.user_id).where(real)).all()
    users = db.scalar(select(func.count(User.id)).where(real))
    counts = {q["id"]: Counter() for q in taste_profile.QUESTIONS}
    personas = Counter()
    completed = 0
    for answers, done in rows:
        answers = answers or {}
        if done:
            completed += 1
            personas[taste_profile.archetype(answers)["title"]] += 1
        for qid, val in answers.items():
            if qid in counts:
                for v in (val if isinstance(val, list) else [val]):
                    counts[qid][v] += 1
    questions = []
    for q in taste_profile.QUESTIONS:
        answered = sum(1 for a, _ in rows if (a or {}).get(q["id"]) not in (None, []))
        questions.append({
            "id": q["id"], "question": q["q"], "section": q["section"], "type": q["type"],
            "answered": answered,
            "options": sorted(({"id": o["id"], "label": o["label"], "count": counts[q["id"]][o["id"]]}
                               for o in q["options"]), key=lambda o: -o["count"]),
        })
    return {"users": users, "started": len(rows), "completed": completed,
            "completion_rate": round(completed / users, 3) if users else None,
            "personas": [{"title": t, "count": n} for t, n in personas.most_common(15)],
            "questions": questions}


# --- server health --------------------------------------------------------------

@router.get("/health")
def health(hours: int = Query(24, ge=1, le=24 * 365), db: Session = Depends(get_db),
           _admin: User = Depends(require_admin)):
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    unit = "minute" if hours <= 6 else "hour" if hours <= 24 * 7 else "day"
    # UTC buckets (naive timestamps), so the page can lay out every bucket, empty ones included.
    bucket = func.date_trunc(unit, func.timezone("UTC", HealthSnapshot.ts))
    rows = db.execute(select(
        bucket, func.sum(HealthSnapshot.requests), func.sum(HealthSnapshot.status_4xx),
        func.sum(HealthSnapshot.status_5xx), func.max(HealthSnapshot.p95_ms),
        func.percentile_cont(0.5).within_group(HealthSnapshot.p95_ms),
        func.bool_or(HealthSnapshot.cold_start),
    ).where(HealthSnapshot.ts >= since).group_by(bucket).order_by(bucket)).all()
    series = [{"t": t, "requests": int(n or 0), "4xx": int(c4 or 0), "5xx": int(c5 or 0),
               "p95_ms": typical, "worst_p95_ms": worst, "cold_start": cold}
              for t, n, c4, c5, worst, typical, cold in rows]
    if unit == "day":   # add days older than the raw window from rollups
        old = defaultdict(dict)
        for d, metric, _, value in _rollups(db, ["requests", "status_4xx", "status_5xx", "p95_ms",
                                                 "cold_starts"], since.date()):
            old[d][metric] = value
        series = [{"t": datetime(d.year, d.month, d.day),
                   "requests": int(m.get("requests", 0)), "4xx": int(m.get("status_4xx", 0)),
                   "5xx": int(m.get("status_5xx", 0)), "p95_ms": m.get("p95_ms"),
                   "worst_p95_ms": None, "cold_start": bool(m.get("cold_starts"))}
                  for d, m in sorted(old.items())] + series

    routes = defaultdict(lambda: {"n": 0, "4xx": 0, "5xx": 0, "p95_weighted": 0.0, "worst_p95": 0.0})
    cold_starts = []
    for snap_routes, ts, cold in db.execute(select(HealthSnapshot.routes, HealthSnapshot.ts,
                                                   HealthSnapshot.cold_start)
                                            .where(HealthSnapshot.ts >= since)).all():
        if cold:
            cold_starts.append(ts)
        for route, s in (snap_routes or {}).items():
            r = routes[route]
            r["n"] += s["n"]
            r["4xx"] += s["4xx"]
            r["5xx"] += s["5xx"]
            r["p95_weighted"] += (s.get("p95") or 0) * s["n"]
            r["worst_p95"] = max(r["worst_p95"], s.get("p95") or 0)
    route_rows = sorted(({"route": k, "requests": v["n"], "4xx": v["4xx"], "5xx": v["5xx"],
                          "error_rate": round(v["5xx"] / v["n"], 4) if v["n"] else None,
                          "p95_ms": round(v["p95_weighted"] / v["n"], 1) if v["n"] else None,
                          "worst_p95_ms": v["worst_p95"]}
                         for k, v in routes.items()), key=lambda r: -r["requests"])
    return {"unit": unit, "series": series, "routes": route_rows,
            "cold_starts": sorted(cold_starts, reverse=True)[:50], "status": _live_status(db),
            "note": "Gaps in the chart are times the server was asleep (Render free tier) "
                    "or had no traffic."}


# --- outside APIs ---------------------------------------------------------------

_KEYLESS = {"overpass": "Free, no key (OpenStreetMap)", "nominatim": "Free, no key (OpenStreetMap); 1 request a second"}


@router.get("/apis")
def apis(days: int = Days, db: Session = Depends(get_db), _admin: User = Depends(require_admin)):
    today = utc_today()
    month_start = today.replace(day=1)
    since = today - timedelta(days=days - 1)
    rows = db.execute(select(ApiUsage.day, ApiUsage.provider, ApiUsage.calls, ApiUsage.errors)
                      .where(ApiUsage.day >= min(since, month_start))).all()
    # Counts from the last minute that haven't been flushed yet aren't included;
    # they appear within FLUSH_SECONDS. The blocker's own counters (quota) are live.
    configured = _live_status(db)["configured"]
    configured["resend"] = configured.pop("email")
    quotas = usage.quota_status()
    providers = sorted({r.provider for r in rows} | set(usage.FREE_TIER) | set(_KEYLESS))
    out = []
    for p in providers:
        mine = [r for r in rows if r.provider == p]
        t = [r for r in mine if r.day == today]
        m = [r for r in mine if r.day >= month_start]
        p_calls = [r for r in mine if r.day >= since]
        q = quotas.get(p)
        cost = None
        if p in usage.COST_PER_CALL and q:
            cost = round(max(0, sum(r.calls for r in m) - q["free"]) * usage.COST_PER_CALL[p], 2)
        status = "free"
        if q and q["disabled"]:
            status = "disabled"                # paid product, switched off in code
        elif q and q["enforced"]:
            if q["cap"] == 0:
                status = "never_free"          # blocked whatever happens
            elif q["used"] is not None and q["used"] >= q["cap"]:
                status = "capped"              # blocked until the period resets
            elif q["used"] is not None and q["used"] >= 0.8 * q["cap"]:
                status = "near"
        elif q:
            status = "unguarded"               # FREE_TIER_ONLY is off: billing possible
        out.append({
            "provider": p, "configured": configured.get(p, True),
            "today": sum(r.calls for r in t), "today_errors": sum(r.errors for r in t),
            "month": sum(r.calls for r in m),
            "period_calls": sum(r.calls for r in p_calls),
            "period_errors": sum(r.errors for r in p_calls),
            "quota": q, "status": status, "note": q["note"] if q else _KEYLESS.get(p),
            "error": (h["last_error"] or {}).get("error") if (h := usage.health(p))["failing"] else None,
            "paused": h["paused"],
            "est_cost_month": cost,
        })
    series = defaultdict(dict)
    for r in rows:
        if r.day >= since:
            series[r.day.isoformat()][r.provider] = r.calls
    return {"providers": out, "series": [{"day": d, **series.get(d, {})} for d in _day_range(days)],
            "llm_label": settings.llm_model, "free_tier_only": settings.free_tier_only}


# --- users ----------------------------------------------------------------------

@router.get("/users")
def users(q: str = "", filter: str = Query("all", pattern="^(all|real|test|admin|disabled)$"),
          page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=200),
          db: Session = Depends(get_db), _admin: User = Depends(require_admin)):
    stmt = _users_stmt()
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(User.email.ilike(like), User.username.ilike(like),
                              User.display_name.ilike(like)))
    counts = {k: db.scalar(select(func.count()).select_from(stmt.where(f()).subquery())) or 0
              for k, f in _USER_FILTERS.items()}
    stmt = stmt.where(_USER_FILTERS[filter]())
    total = counts[filter]
    rows = db.execute(stmt.order_by(User.created_at.desc())
                      .offset((page - 1) * per_page).limit(per_page)).all()
    return {"total": total, "page": page, "per_page": per_page, "counts": counts,
            "summary": _users_summary(db), "users": [_user_list_row(*r) for r in rows]}


def _users_stmt():
    """Users with their plan count, active-day count and survey completion."""
    plans = (select(PlanLog.user_id, func.count(PlanLog.id).label("plans"))
             .group_by(PlanLog.user_id).subquery())
    active = (select(UserActivityDay.user_id, func.count().label("active_days"))
              .group_by(UserActivityDay.user_id).subquery())
    return (select(User, func.coalesce(plans.c.plans, 0), func.coalesce(active.c.active_days, 0),
                   TasteProfile.completed_at, TasteProfile.user_id.is_not(None))
            .outerjoin(plans, plans.c.user_id == User.id)
            .outerjoin(active, active.c.user_id == User.id)
            .outerjoin(TasteProfile, TasteProfile.user_id == User.id))


def _user_list_row(u: User, plans: int, active_days: int, survey_completed_at, started: bool = False) -> dict:
    return {**_user_row(u), "plans": plans, "active_days": active_days,
            "survey_completed_at": survey_completed_at,
            "survey": "done" if survey_completed_at else "partial" if started else "none"}


_USER_FILTERS = {
    "all": lambda: True, "real": lambda: and_(User.is_test.is_(False), User.is_admin.is_(False)),
    "test": lambda: User.is_test.is_(True), "admin": lambda: User.is_admin.is_(True),
    "disabled": lambda: User.is_disabled.is_(True)}


def _users_summary(db: Session) -> dict:
    """The Users page's numbers, for real accounts only."""
    real = _USER_FILTERS["real"]()
    n = db.scalar(select(func.count(User.id)).where(real)) or 0
    week = utc_today() - timedelta(days=6)
    active = db.scalar(select(func.count(distinct(UserActivityDay.user_id))).join(User, User.id == UserActivityDay.user_id)
                       .where(real, UserActivityDay.day >= week)) or 0
    surveyed = db.scalar(select(func.count(TasteProfile.user_id)).join(User, User.id == TasteProfile.user_id)
                         .where(real, TasteProfile.completed_at.is_not(None))) or 0
    planned = db.scalar(select(func.count(distinct(PlanLog.user_id))).join(User, User.id == PlanLog.user_id)
                        .where(real, *_MADE, PlanLog.created_at >= _since(7))) or 0
    return {"real": n, "active_week": active,
            "survey_rate": round(surveyed / n, 3) if n else None,
            "planned_week_rate": round(planned / n, 3) if n else None}


def _user_row(u: User) -> dict:
    return {"id": u.id, "email": u.email, "username": u.username, "display_name": u.display_name,
            "home_city": u.home_city, "created_at": u.created_at, "last_seen_at": u.last_seen_at,
            "is_admin": u.is_admin, "is_disabled": u.is_disabled, "is_test": u.is_test}


@router.get("/users/{user_id}")
def user_detail(user_id: uuid.UUID, db: Session = Depends(get_db), _admin: User = Depends(require_admin)):
    u = _user_or_404(db, user_id)
    profile = db.get(TasteProfile, u.id)
    days = db.scalars(select(UserActivityDay.day).where(UserActivityDay.user_id == u.id)
                      .order_by(UserActivityDay.day.desc()).limit(120)).all()
    plans = db.scalars(select(PlanLog).where(PlanLog.user_id == u.id)
                       .order_by(PlanLog.created_at.desc()).limit(100)).all()
    answers = (profile.answers if profile else None) or {}
    return {
        **_user_row(u), "bio": u.bio, "avatar_url": u.avatar_url,
        "taste": {"answers": answers, "completed_at": profile.completed_at if profile else None,
                  "persona": taste_profile.archetype(answers)["title"] if answers else None,
                  "summary": taste_profile.summary(answers) if answers else []},
        "active_days": days,
        "active_days_total": db.scalar(select(func.count()).select_from(UserActivityDay)
                                       .where(UserActivityDay.user_id == u.id)),
        "plans": [_plan_row(p) for p in plans],
        "plans_total": db.scalar(select(func.count(PlanLog.id)).where(PlanLog.user_id == u.id, *_MADE)) or 0,
        "shared_total": db.scalar(select(func.count(SharedPlan.id)).where(SharedPlan.user_id == u.id)) or 0,
        "latest_plan": _latest_plan(db, u.id),
    }


def _latest_plan(db: Session, user_id) -> dict | None:
    row = db.execute(select(PlanLog.created_at, PlanLog.response).where(PlanLog.user_id == user_id, *_MADE)
                     .order_by(PlanLog.created_at.desc()).limit(1)).first()
    plan = _plan_of(row.response) if row else None
    if not plan or not plan.get("stops"):
        return None
    stops = plan["stops"]
    party = max(1, int(plan.get("party_size") or 1))
    return {"created_at": row.created_at, "stops": len(stops),
            "names": [s.get("name") for s in stops if s.get("name")][:4],
            "each": round((plan.get("estimated_cost") or 0) / party) or None,
            "image": next((s.get("image") for s in stops if s.get("image")), None),
            "days": len((row.response or {}).get("days") or []) or None}


class FlagBody(BaseModel):
    value: bool


def _change_user(db: Session, admin: User, user_id: uuid.UUID, action: str) -> User:
    u = _user_or_404(db, user_id)
    if u.id == admin.id and action in ("disable", "delete"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You can't do that to your own account here")
    return u


@router.post("/users/{user_id}/disabled")
def set_disabled(user_id: uuid.UUID, body: FlagBody, db: Session = Depends(get_db),
                 admin: User = Depends(require_admin)):
    u = _change_user(db, admin, user_id, "disable" if body.value else "enable")
    u.is_disabled = body.value
    _audit(db, admin, "disable" if body.value else "enable", u.email)
    db.commit()
    return _user_row(u)


@router.post("/users/{user_id}/test")
def set_test(user_id: uuid.UUID, body: FlagBody, db: Session = Depends(get_db),
             admin: User = Depends(require_admin)):
    u = _change_user(db, admin, user_id, "test")
    u.is_test = body.value
    _audit(db, admin, "mark_test" if body.value else "unmark_test", u.email)
    db.commit()
    return _user_row(u)


@router.post("/users/{user_id}/reset-password")
def admin_reset_password(user_id: uuid.UUID, db: Session = Depends(get_db),
                         admin: User = Depends(require_admin)):
    u = _change_user(db, admin, user_id, "reset")
    if not email_enabled():
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Email isn't configured (RESEND_API_KEY / EMAIL_FROM), so no reset can be sent")
    if not send_password_reset(u.email, create_reset_token(str(u.id), u.password_hash)):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Resend didn't accept the email")
    _audit(db, admin, "send_reset", u.email)
    db.commit()
    return {"sent": True}


@router.delete("/users/{user_id}", status_code=204)
def admin_delete_user(user_id: uuid.UUID, db: Session = Depends(get_db),
                      admin: User = Depends(require_admin)):
    u = _change_user(db, admin, user_id, "delete")
    _audit(db, admin, "delete_user", u.email, username=u.username)
    db.delete(u)
    db.commit()


# --- waitlist -------------------------------------------------------------------

@router.get("/waitlist")
def waitlist(q: str = "", db: Session = Depends(get_db), _admin: User = Depends(require_admin)):
    stmt = (select(WaitlistSignup, User.id)
            .outerjoin(User, func.lower(User.email) == func.lower(WaitlistSignup.email)))
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(WaitlistSignup.email.ilike(like), WaitlistSignup.name.ilike(like)))
    rows = db.execute(stmt.order_by(WaitlistSignup.created_at.desc()).limit(1000)).all()
    return {"email_enabled": email_enabled(),
            "signups": [{"id": w.id, "email": w.email, "name": w.name,
                         "desired_username": w.desired_username, "source": w.source,
                         "created_at": w.created_at, "invited_at": w.invited_at,
                         "joined_user_id": uid} for w, uid in rows]}


class InviteBody(BaseModel):
    ids: list[uuid.UUID] = Field(min_length=1, max_length=500)
    subject: str = Field("You're in — welcome to Rova", max_length=150)
    message: str = Field("", max_length=4000)
    mark_only: bool = False   # record as invited without sending (e.g. invited by hand)


@router.post("/waitlist/invite")
def invite(body: InviteBody, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    if not body.mark_only and not email_enabled():
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Email isn't configured (RESEND_API_KEY / EMAIL_FROM). "
                            "Set it up, or use “mark as invited” instead.")
    rows = db.scalars(select(WaitlistSignup).where(WaitlistSignup.id.in_(body.ids))).all()
    link = f"{settings.frontend_base_url}/auth?mode=register"
    sent, failed = [], []
    for w in rows:
        if not body.mark_only:
            hello = f"Hi {w.name}," if w.name else "Hi,"
            extra = "".join(f"<p>{_esc(p)}</p>" for p in body.message.split("\n\n") if p.strip())
            html = (f"<p>{_esc(hello)}</p>{extra or '<p>Your Rova early access is ready.</p>'}"
                    f'<p><a href="{link}">Create your account</a></p><p>— Rova</p>')
            if not send_email(w.email, body.subject, html):
                failed.append(w.email)
                continue
        w.invited_at = datetime.now(timezone.utc)
        sent.append(w.email)
    _audit(db, admin, "mark_invited" if body.mark_only else "invite", None,
           count=len(sent), failed=failed)
    db.commit()
    return {"invited": len(sent), "failed": failed}


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --- nightlife events (the Nightlife tab's own listings) ---------------------------

def _event_out(e: NightlifeEvent) -> dict:
    return {c: getattr(e, c) for c in (
        "id", "title", "venue_name", "venue_id", "lat", "lng", "address", "starts_at", "ends_at",
        "price_min", "price_max", "free", "url", "kind", "special_day", "description", "status",
        "source", "submitter_name", "submitter_email", "created_at", "reviewed_at")}


def _event_or_404(db: Session, event_id: int) -> NightlifeEvent:
    e = db.get(NightlifeEvent, event_id)
    if not e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such event")
    return e


def _apply_venue(db: Session, e: NightlifeEvent) -> None:
    """A linked stored venue fills in the map point and address."""
    if e.venue_id:
        v = db.get(Venue, e.venue_id)
        if not v:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unknown venue_id")
        e.lat, e.lng = v.lat, v.lng
        e.address = e.address or v.address


@router.get("/events")
def list_events(status_: str = Query("pending", alias="status", pattern="^(pending|approved|rejected|upcoming|all)$"),
                db: Session = Depends(get_db), _admin: User = Depends(require_admin)):
    stmt = select(NightlifeEvent)
    if status_ == "upcoming":
        stmt = stmt.where(NightlifeEvent.status == "approved",
                          func.coalesce(NightlifeEvent.ends_at, NightlifeEvent.starts_at)
                          >= datetime.now(timezone.utc) - timedelta(hours=6))
        stmt = stmt.order_by(NightlifeEvent.starts_at)
    else:
        if status_ != "all":
            stmt = stmt.where(NightlifeEvent.status == status_)
        stmt = stmt.order_by(NightlifeEvent.created_at.desc())
    pending = db.scalar(select(func.count()).select_from(NightlifeEvent)
                        .where(NightlifeEvent.status == "pending"))
    return {"pending": pending, "events": [_event_out(e) for e in db.scalars(stmt.limit(300))]}


@router.post("/events", status_code=201)
def create_event(body: AdminEventIn, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    if body.status not in ("pending", "approved"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "status must be pending or approved")
    start, end = check_times(body.starts_at, body.ends_at)
    e = NightlifeEvent(**body.model_dump(exclude={"starts_at", "ends_at", "status"}),
                       starts_at=start, ends_at=end, status=body.status, source="admin")
    if e.venue_id:
        _apply_venue(db, e)
    elif e.lat is None:
        link_venue(e)
    if e.status == "approved":
        e.reviewed_by, e.reviewed_at = admin.id, datetime.now(timezone.utc)
    db.add(e)
    db.flush()
    _audit(db, admin, "event_create", str(e.id), title=e.title)
    db.commit()
    return _event_out(e)


@router.patch("/events/{event_id}")
def edit_event(event_id: int, body: AdminEventPatch, db: Session = Depends(get_db),
               admin: User = Depends(require_admin)):
    e = _event_or_404(db, event_id)
    changes = body.model_dump(exclude_unset=True)
    for k, v in changes.items():
        setattr(e, k, _as_utc(v) if k in ("starts_at", "ends_at") and v else v)
    if "venue_id" in changes:
        if e.venue_id:
            _apply_venue(db, e)
        else:                                  # unlinked: match the (new) name again
            e.lat = e.lng = None
            link_venue(e)
    if e.ends_at and e.ends_at <= e.starts_at:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "The end time must be after the start.")
    _audit(db, admin, "event_edit", str(e.id), fields=sorted(changes))
    db.commit()
    return _event_out(e)


class EventStatusBody(BaseModel):
    status: str = Field(pattern="^(approved|rejected|pending)$")


@router.post("/events/{event_id}/status")
def set_event_status(event_id: int, body: EventStatusBody, db: Session = Depends(get_db),
                     admin: User = Depends(require_admin)):
    e = _event_or_404(db, event_id)
    e.status, e.reviewed_by, e.reviewed_at = body.status, admin.id, datetime.now(timezone.utc)
    _audit(db, admin, f"event_{body.status}", str(e.id), title=e.title)
    db.commit()
    return _event_out(e)


@router.delete("/events/{event_id}", status_code=204)
def delete_event(event_id: int, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    e = _event_or_404(db, event_id)
    _audit(db, admin, "event_delete", str(e.id), title=e.title)
    db.delete(e)
    db.commit()


@router.get("/venues/search")
def search_venues(q: str = Query(min_length=2, max_length=80), db: Session = Depends(get_db),
                  _admin: User = Depends(require_admin)):
    """Stored places by name, to link an event to (well-known first)."""
    rows = db.scalars(select(Venue).where(Venue.name.ilike(f"%{q.strip()}%"))
                      .order_by(Venue.notability.desc(), func.length(Venue.name)).limit(10)).all()
    return [{"id": v.id, "name": v.name, "category": v.category, "address": v.address,
             "lat": v.lat, "lng": v.lng} for v in rows]


# --- exports + audit ------------------------------------------------------------

_EXPORTS = {
    "users": ["id", "email", "username", "display_name", "home_city", "created_at", "last_seen_at",
              "is_admin", "is_disabled", "is_test", "survey_completed_at", "plans", "active_days"],
    "waitlist": ["id", "email", "name", "desired_username", "source", "created_at", "invited_at"],
    "reviews": ["id", "created_at", "updated_at", "username", "stars", "comment", "vibe", "stops", "names",
                "quick", "reviewed_at"],
    "plans": ["id", "created_at", "user_id", "username", "endpoint", "user_text", "area", "lat", "lng",
              "response_type", "stops", "found_nothing", "llm_calls", "llm_failures", "duration_ms",
              "prefs"],
}


@router.get("/export/{kind}.csv")
def export(kind: str, days: int = Days, db: Session = Depends(get_db),
           admin: User = Depends(require_admin)):
    if kind not in _EXPORTS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown export")
    cols = _EXPORTS[kind]
    if kind == "users":
        rows = [_user_list_row(*r) for r in db.execute(_users_stmt().order_by(User.created_at))]
    elif kind == "reviews":
        rows = [{**row, "names": " · ".join(filter(None, row["names"]))}
                for row in (_review_row(r, name) for r, name in db.execute(select(PlanReview, User.username).join(User, User.id == PlanReview.user_id)
                                          .where(PlanReview.created_at >= _since(days))
                                          .order_by(PlanReview.created_at)))]
    elif kind == "waitlist":
        rows = [{c: getattr(w, c) for c in cols} for w in
                db.scalars(select(WaitlistSignup).order_by(WaitlistSignup.created_at))]
    else:
        q = (select(PlanLog, User.username).outerjoin(User, User.id == PlanLog.user_id)
             .where(PlanLog.created_at >= _since(days)).order_by(PlanLog.created_at))
        rows = [{**{c: getattr(p, c, None) for c in cols if c != "username"}, "username": name}
                for p, name in db.execute(q)]
    _audit(db, admin, "export", kind, rows=len(rows))
    db.commit()

    def gen():
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        yield buf.getvalue()
        for r in rows:
            buf.seek(0)
            buf.truncate()
            w.writerow({k: ("" if v is None else v) for k, v in r.items()})
            yield buf.getvalue()

    stamp = utc_today().isoformat()
    return StreamingResponse(gen(), media_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="rova-{kind}-{stamp}.csv"'})


@router.get("/audit")
def audit(limit: int = Query(100, ge=1, le=500), db: Session = Depends(get_db),
          _admin: User = Depends(require_admin)):
    rows = db.execute(select(AdminAuditLog, User.username)
                      .outerjoin(User, User.id == AdminAuditLog.admin_id)
                      .order_by(AdminAuditLog.created_at.desc()).limit(limit)).all()
    return [{"id": a.id, "created_at": a.created_at, "admin": name, "action": a.action,
             "target": a.target, "details": a.details} for a, name in rows]
