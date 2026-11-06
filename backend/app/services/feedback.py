"""Learning from what people keep: which stored venues they drop from plans and which
they keep in plans they save or share, summarised daily into a small ranking nudge.

What is recorded (StopFeedback): a stored venue id, its slot, and one of
  shown    it was in a plan someone was given
  removed  they took it out
  swapped  they asked for something else instead, with no particular wish ("swap
           dinner for sushi" is about what they want tonight, not about the place)
  saved    it was in a plan they saved
  shared   it was in a plan they shared
Changing the length or the start time drops stops too, but says nothing about a place,
so it isn't recorded.

How it's used: each night `aggregate` counts, per venue, the distinct people who saw it,
kept it (saved/shared) and dropped it (removed/swapped), and stores a score shrunk
toward "no lean":   score = (kept + PRIOR·0.5) / (kept + dropped + PRIOR).
`bonus` turns that into at most ±SIGNAL_CAP on the planner's score, and only once
MIN_USERS people have seen the place. SIGNAL_CAP is the size of PHOTO_BONUS, under half
a star: it settles near-ties and can't lift a clearly worse place over a better one.
No data means no change at all, so a new venue ranks exactly as it would without this.

Guard rails: one vote per person per venue per action per plan (a unique key), at most
HOURLY_LIMIT actions an hour from one account, admin and test accounts never count, and
rows older than RETENTION_DAYS are deleted. SIGNAL_WEIGHT = 0 switches the nudge off.
"""
import hashlib
import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert

from app.database import SessionLocal
from app.models import StopFeedback, User, VenueSignal

log = logging.getLogger(__name__)

POSITIVE = ("saved", "shared")
NEGATIVE = ("removed", "swapped")
EVENTS = ("shown", *POSITIVE, *NEGATIVE)

PRIOR = 10                # pretend every place starts with 10 people who had no lean
MIN_USERS = 20            # distinct people who saw it before it counts at all
SIGNAL_WEIGHT = 2.0       # (score − 0.5) × this; 0 switches the nudge off
SIGNAL_CAP = 0.8          # same scale as planner.PHOTO_BONUS
HOURLY_LIMIT = 30         # removes/swaps/saves/shares from one account in an hour
RETENTION_DAYS = 365
CACHE_SECONDS = 600


def counts_for(user) -> bool:
    """Admin and test accounts try things out; what they do says nothing about a place."""
    from app.services.analytics import looks_like_test
    return not (getattr(user, "is_admin", False) or getattr(user, "is_test", False)
                or looks_like_test(user.email or "", user.username or ""))


def plan_key(stops: list[dict]) -> str:
    """A short digest naming one plan, so the same action on the same plan counts once."""
    ids = "|".join(str(s.get("vid") or s.get("name") or "").lower() for s in stops or [])
    return hashlib.sha256(ids.encode()).hexdigest()[:32]


def _vid(s: dict) -> str | None:
    v = s.get("vid")
    return v if isinstance(v, str) and 0 < len(v) <= 80 else None


def rows_for(event: str, stops: list[dict]) -> list[tuple[str, str, str | None]]:
    """(event, vid, slot) for every stored venue in `stops`; events and free-typed
    places have no vid and are skipped."""
    return [(event, v, (s.get("slot") or None) and str(s["slot"])[:20]) for s in stops or [] if (v := _vid(s))]


def record(user_id, key: str, rows: list[tuple[str, str, str | None]]) -> None:
    """Store feedback rows. Runs as a background task: never slows or breaks a request."""
    rows = [r for r in rows if r[0] in EVENTS]
    if not rows or user_id is None:
        return
    try:
        with SessionLocal() as db:
            acts = [r for r in rows if r[0] != "shown"]
            if acts:
                hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
                recent = db.scalar(select(func.count()).select_from(StopFeedback).where(
                    StopFeedback.user_id == user_id, StopFeedback.event != "shown",
                    StopFeedback.created_at >= hour_ago)) or 0
                room = max(0, HOURLY_LIMIT - recent)
                rows = [r for r in rows if r[0] == "shown"] + acts[:room]
            if not rows:
                return
            values = [{"user_id": user_id, "vid": vid, "slot": slot, "event": event, "plan_key": key[:32]}
                      for event, vid, slot in dict.fromkeys(rows)]
            db.execute(insert(StopFeedback).values(values).on_conflict_do_nothing(
                constraint="uq_stop_feedback_once"))
            db.commit()
    except Exception:
        log.exception("recording stop feedback failed")


def score(kept: int, dropped: int) -> float:
    return (kept + PRIOR * 0.5) / (kept + dropped + PRIOR)


def bonus(users: int, value: float) -> float:
    """The planner's nudge for a venue with this summary: 0 until enough people saw it."""
    if users < MIN_USERS or not SIGNAL_WEIGHT:
        return 0.0
    return max(-SIGNAL_CAP, min(SIGNAL_CAP, (value - 0.5) * SIGNAL_WEIGHT))


def aggregate(db) -> int:
    """Nightly: drop rows past retention, then rebuild venue_signals from the rest.
    Returns the number of venues with a summary."""
    now = datetime.now(timezone.utc)
    db.execute(delete(StopFeedback).where(StopFeedback.created_at < now - timedelta(days=RETENTION_DAYS)))
    counted = db.execute(text("""
        SELECT f.vid,
               COUNT(DISTINCT f.user_id) AS users,
               COUNT(DISTINCT f.user_id) FILTER (WHERE f.event IN ('saved', 'shared')) AS kept,
               COUNT(DISTINCT f.user_id) FILTER (WHERE f.event IN ('removed', 'swapped')) AS dropped
        FROM stop_feedback f JOIN users u ON u.id = f.user_id
        WHERE NOT u.is_admin AND NOT u.is_test
        GROUP BY f.vid
    """)).all()
    db.execute(delete(VenueSignal))
    if counted:
        db.execute(insert(VenueSignal).values([
            {"vid": vid, "users": users, "positive": kept, "negative": dropped,
             "score": round(score(kept, dropped), 4), "updated_at": now}
            for vid, users, kept, dropped in counted]))
    db.commit()
    _cache.update(at=0.0)          # this process picks the new numbers up straight away
    return len(counted)


# ---- what the planner reads: an in-process copy, refreshed every few minutes ----

_cache: dict = {"at": 0.0, "bonus": {}}


def signals() -> dict[str, float]:
    """vid → bonus, for the venues that have one. Empty if the table can't be read."""
    if time.monotonic() - _cache["at"] < CACHE_SECONDS:
        return _cache["bonus"]
    found: dict[str, float] = {}
    try:
        with SessionLocal() as db:
            for vid, users, value in db.execute(select(VenueSignal.vid, VenueSignal.users, VenueSignal.score)
                                                .where(VenueSignal.users >= MIN_USERS)):
                if b := bonus(users, value):
                    found[vid] = round(b, 3)
    except Exception:
        log.warning("venue signals unavailable", exc_info=True)
    _cache.update(at=time.monotonic(), bonus=found)
    return found


def top_and_bottom(db, n: int = 10) -> dict:
    """For the admin page: the venues people keep most and drop most."""
    base = select(VenueSignal).where(VenueSignal.users >= MIN_USERS)
    row = lambda v: {"vid": v.vid, "users": v.users, "kept": v.positive, "dropped": v.negative,
                     "score": round(v.score, 3), "bonus": round(bonus(v.users, v.score), 2)}
    return {"top": [row(v) for v in db.scalars(base.order_by(VenueSignal.score.desc()).limit(n))],
            "bottom": [row(v) for v in db.scalars(base.order_by(VenueSignal.score.asc()).limit(n))],
            "venues": db.scalar(select(func.count()).select_from(VenueSignal)) or 0,
            "counting": db.scalar(select(func.count()).select_from(VenueSignal)
                                  .where(VenueSignal.users >= MIN_USERS)) or 0}
