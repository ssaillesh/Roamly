"""Counts calls to outside APIs (Groq, Yelp, Google Places...) for the admin dashboard.

Calls happen on planner worker threads, so counts are kept in memory under a
lock and saved to `api_usage` by the stats flusher (services/analytics.py) once
a minute, rather than costing a DB write per call. A restart loses at most the
last minute's counts.

Each planner request also gets a tracker (see `request_tracker`) so its plan log
can say whether the AI ran and whether it failed.
"""
import threading
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timezone

# Free-tier limits as of 2026 — check your own plan; these only drive the
# "close to the limit" warnings. period is "day" or "month".
LIMITS = {
    "llm":           ("day", 1000),     # Groq llama-3.3-70b free tier: 1,000 requests/day
    "yelp":          ("day", 500),
    "ticketmaster":  ("day", 5000),
    "foursquare":    ("month", 10000),
    "google_places": ("month", 5000),   # free monthly calls before billing starts
    "overpass":      ("day", 10000),
    "open_meteo":    ("day", 10000),
}
# Estimated $ per call once past the free allowance (Text Search, Pro fields).
COST_PER_CALL = {"google_places": 0.032}

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
