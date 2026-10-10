"""Is a place open at a given moment, from its OpenStreetMap `opening_hours`?

Handles the forms that cover most real tags:
  "24/7" · "Mo-Fr 11:00-22:00" · "Mo-Th,Su 11:00-23:00; Fr-Sa 11:00-02:00" ·
  "Tu-Su 12:00-15:00,17:00-22:00; Mo off" · "10:00-18:00"
Later rules override earlier ones for the days they name (OSM semantics), and a
range past midnight ("18:00-02:00") counts toward the next morning.

Anything else (public-holiday rules, months, sunrise, comments…) → None,
meaning "unknown": the planner then neither trusts nor rejects the place.
"""
import re
from datetime import datetime, timedelta

_DAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
_RULE = re.compile(r"^(?:(?P<days>[A-Za-z]{2}(?:\s*[-,]\s*[A-Za-z]{2})*)\s+)?(?P<times>.+)$")
_TIME = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\+?$")


def _days(spec: str | None) -> set[int] | None:
    if not spec:
        return set(range(7))
    out = set()
    for part in spec.replace(" ", "").split(","):
        if "-" in part:
            a, b = part.split("-")
            if a not in _DAYS or b not in _DAYS:
                return None
            i, j = _DAYS.index(a), _DAYS.index(b)
            out.update(range(i, j + 1) if i <= j else [*range(i, 7), *range(0, j + 1)])
        elif part in _DAYS:
            out.add(_DAYS.index(part))
        else:
            return None
    return out


def _ranges(times: str) -> list[tuple[int, int]] | None:
    """[(start_min, end_min)] with end > 1440 for past-midnight; [] for off/closed."""
    times = times.strip().lower()
    if times in ("off", "closed"):
        return []
    out = []
    for part in times.split(","):
        m = _TIME.match(part.strip())
        if not m:
            return None
        s = int(m.group(1)) * 60 + int(m.group(2))
        e = int(m.group(3)) * 60 + int(m.group(4))
        if e <= s:
            e += 1440                      # 18:00-02:00 runs into the next day
        out.append((s, e))
    return out


def schedule(spec: str | None) -> dict[int, list[tuple[int, int]]] | None:
    """Weekday (0=Mon) → open ranges in minutes. None if the tag can't be read."""
    spec = (spec or "").strip()
    if not spec:
        return None
    if spec == "24/7":
        return {d: [(0, 1440)] for d in range(7)}
    week: dict[int, list[tuple[int, int]]] = {}
    for rule in filter(None, (r.strip() for r in spec.split(";"))):
        m = _RULE.match(rule)
        if not m:
            return None
        days, ranges = _days(m.group("days")), _ranges(m.group("times"))
        if days is None or ranges is None:
            return None
        for d in days:
            week[d] = ranges                   # a later rule wins for its days
    return week


def is_open(spec: str | None, at: datetime) -> bool | None:
    """True/False when the tag says; None when it's missing or unreadable."""
    week = schedule(spec)
    if week is None:
        return None
    minute = at.hour * 60 + at.minute
    today, yesterday = at.weekday(), (at - timedelta(days=1)).weekday()
    if any(s <= minute < e for s, e in week.get(today, [])):
        return True
    # yesterday's late range still running after midnight
    return any(e > 1440 and minute < e - 1440 for s, e in week.get(yesterday, []))
