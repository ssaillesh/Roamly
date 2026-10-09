"""Outside APIs (Groq, Google Places, SendGrid...): paid products switched off,
the free-tier blocker, and call counts for the admin dashboard.

**Paid products are off.** Services in DISABLED are never called: their
`available()` is False whatever API key is set, and `allow()` always says no,
even with FREE_TIER_ONLY off. Turning one back on is a deliberate code change
(delete it from DISABLED), not a setting someone can flip on Render.

**Free-tier blocker.** While FREE_TIER_ONLY is on (the default), `allow()` must
say yes before any metered API is called. Each provider gets a cap a little under
its free allowance; past it, `allow()` says no and the caller behaves as if the
service weren't set up (the planner falls back to OpenStreetMap, the AI to the
rule-based planner, email to "not sent"). Services whose free tier doesn't cover
the way Roamly calls them have a cap of 0, so they're never called. The counters
live in Redis so every process shares them; if Redis is unreachable `allow()`
says no, because an uncounted call could be a billed one.

**Call counts.** Calls happen on planner worker threads, so counts are kept in
memory under a lock and saved to `api_usage` by the stats flusher
(services/analytics.py) once a minute. Each planner request also gets a tracker
(see `request_tracker`) so its plan log can say whether the AI ran and failed.
"""
import logging
import threading
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from app.config import settings
from app.redis_client import redis_client

log = logging.getLogger(__name__)

# What each metered service gives away (checked 2026-10) and where Roamly stops.
# period: "day" = UTC day; "month" = calendar month in US Pacific time, which is
# when Google Maps Platform resets its free monthly usage.
#   provider:       (period,  free,  cap,  note shown on the dashboard)
FREE_TIER = {
    "llm":           ("day",   1000,  950, "Groq free tier: 1,000 requests a day. Over it Groq refuses "
                                           "(429); it only bills if the account is upgraded."),
    "google_places": ("month", 1000,  900, "Text Search Enterprise (Roamly asks for rating and price): "
                                           "1,000 free a month, then $35 per 1,000."),
    "foursquare":    ("month",    0,    0, "Roamly asks for rating, price and popularity, which are "
                                           "Premium fields: billed from the first call ($18.75 per 1,000)."),
    "yelp":          ("month",    0,    0, "No free tier since 2024: a 30-day trial, then plans from "
                                           "$229 a month."),
    "sendgrid":      ("day",    100,   95, "Free trial: 100 emails a day for 60 days, then a paid plan "
                                           "is needed."),
    "ticketmaster":  ("day",   5000, 4900, "Free: 5,000 calls a day (over it they refuse; never billed)."),
    "open_meteo":    ("day",  10000, 9000, "Free for non-commercial use: 10,000 calls a day."),
}
# Estimated $ per call past the free allowance, for the dashboard's cost column.
COST_PER_CALL = {"google_places": 0.035, "foursquare": 0.01875}

_PACIFIC = ZoneInfo("America/Los_Angeles")

# Paid products Roamly doesn't use (yet). Each can charge a card on file, so
# they're switched off in code. To use one later, remove it here on purpose.
DISABLED = {
    "foursquare":    "Paid product: the fields Roamly needs are billed from the first call.",
    "yelp":          "Paid product: no free tier since 2024.",
    "google_places": "Paid product: needs a billing card and charges past 1,000 searches a month.",
}


def disabled(provider: str) -> bool:
    return provider in DISABLED

_lock = threading.Lock()
_pending: dict[tuple[date, str], list[int]] = defaultdict(lambda: [0, 0])   # [calls, errors]

_tracker: ContextVar[dict | None] = ContextVar("usage_tracker", default=None)


def record(provider: str, ok: bool) -> None:
    """Count one call to `provider`. Never raises."""
    day = datetime.now(timezone.utc).date()
    with _lock:
        counts = _pending[(day, provider)]
        counts[0] += 1
        if not ok:
            counts[1] += 1
    t = _tracker.get()
    if t is not None and provider == "llm":
        t["llm_calls"] += 1
        if not ok:
            t["llm_failures"] += 1


@contextmanager
def counted(provider: str):
    """Count the call made inside the block; an exception counts as an error
    (and still propagates to the caller's own error handling)."""
    ok = False
    try:
        yield
        ok = True
    finally:
        record(provider, ok)


def drain() -> dict[tuple[date, str], list[int]]:
    """Take the counts gathered since the last drain."""
    global _pending
    with _lock:
        out, _pending = _pending, defaultdict(lambda: [0, 0])
    return dict(out)


def restore(counts: dict[tuple[date, str], list[int]]) -> None:
    """Put drained counts back (the save failed), so they're retried next time."""
    with _lock:
        for k, (calls, errors) in counts.items():
            _pending[k][0] += calls
            _pending[k][1] += errors


@contextmanager
def request_tracker():
    """Track AI calls made while handling one planner request."""
    t = {"llm_calls": 0, "llm_failures": 0}
    token = _tracker.set(t)
    try:
        yield t
    finally:
        _tracker.reset(token)


# --- free-tier blocker -----------------------------------------------------------

def _period_key(period: str) -> tuple[str, int, date]:
    """(counter suffix, Redis TTL, first UTC day of the period) for now."""
    now = datetime.now(timezone.utc)
    if period == "day":
        return now.strftime("%Y-%m-%d"), 2 * 86400, now.date()
    pac = now.astimezone(_PACIFIC)
    return pac.strftime("%Y-%m"), 40 * 86400, pac.date().replace(day=1)


def _persisted_calls(provider: str, since: date) -> int:
    """Calls already saved to api_usage this period. Seeds a fresh counter, so a
    Redis restart (Render's free Redis keeps nothing on disk) can't reset a cap."""
    from sqlalchemy import func, select
    from app.database import SessionLocal
    from app.models import ApiUsage
    with SessionLocal() as db:
        return int(db.scalar(select(func.coalesce(func.sum(ApiUsage.calls), 0))
                             .where(ApiUsage.provider == provider, ApiUsage.day >= since)))


def allow(provider: str) -> bool:
    """Reserve one call to `provider` under its free cap. False means don't make
    the call: it would be past the free tier (or can't be counted)."""
    if provider in DISABLED:
        return False
    if not settings.free_tier_only or provider not in FREE_TIER:
        return True
    period, _free, cap, _note = FREE_TIER[provider]
    suffix, ttl, since = _period_key(period)
    key = f"quota:{provider}:{suffix}"
    try:
        n = redis_client.incr(key)
        if n == 1:
            redis_client.expire(key, ttl)
            seed = _persisted_calls(provider, since)
            if seed:
                n = redis_client.incrby(key, seed)
    except Exception:
        log.warning("free-tier blocker: can't count %s calls, so not calling it", provider)
        return False
    if n > cap:
        if n == cap + 1 or n % 100 == 0:
            log.warning("free-tier blocker: %s reached its cap of %s this %s", provider, cap, period)
        return False
    return True


def quota_status() -> dict[str, dict]:
    """Per provider: the cap, how much of it is used this period, and how many
    calls the blocker has turned away."""
    out = {}
    for provider, (period, free, cap, note) in FREE_TIER.items():
        suffix, _, _ = _period_key(period)
        try:
            n = int(redis_client.get(f"quota:{provider}:{suffix}") or 0)
        except Exception:
            n = None
        out[provider] = {
            "period": period, "free": free, "cap": cap, "note": DISABLED.get(provider, note),
            "used": None if n is None else min(n, cap), "blocked": None if n is None else max(0, n - cap),
            "enforced": settings.free_tier_only, "disabled": provider in DISABLED,
        }
    return out
