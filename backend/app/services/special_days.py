"""Special days worth planning around (St. Patrick's Day, Pride, Halloween, NYE…).

Each day knows when it is, how early to start mentioning it, which kinds of
places fit it (Irish pubs on St. Patrick's Day), what to search ticketed events
for, and what a night-out plan should lean towards that day. Toronto dates.

Days that follow the moon (Lunar New Year, Diwali) use a date table: extend
_LUNAR when it runs out (it's checked in tests/test_special_days.py).
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Toronto")


def _nth_weekday(year, month, weekday, n):
    """The n-th `weekday` (0=Mon) of a month; n=-1 for the last one."""
    if n > 0:
        d = date(year, month, 1)
        d += timedelta(days=(weekday - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    d = nxt - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _fixed(month, day):
    return lambda y: (date(y, month, day), date(y, month, day))


def _pride(y):                       # Pride weekend: Fri–Sun of the parade (last Sunday of June)
    sun = _nth_weekday(y, 6, 6, -1)
    return sun - timedelta(days=2), sun


def _carnival(y):                    # Grand Parade: the Saturday of the August long weekend, to Monday
    mon = _nth_weekday(y, 8, 0, 1)
    return mon - timedelta(days=2), mon


def _nuit_blanche(y):                # dusk to dawn, the first Saturday of October
    sat = _nth_weekday(y, 10, 5, 1)
    return sat, sat


_LUNAR = {
    "lunar_new_year": {2026: date(2026, 2, 17), 2027: date(2027, 2, 6), 2028: date(2028, 1, 26),
                       2029: date(2029, 2, 13), 2030: date(2030, 2, 3)},
    "diwali": {2026: date(2026, 11, 8), 2027: date(2027, 10, 29), 2028: date(2028, 10, 17),
               2029: date(2029, 11, 5), 2030: date(2030, 10, 26)},
}


def _table(key):
    def rule(y):
        d = _LUNAR[key].get(y)
        return (d, d) if d else None
    return rule


@dataclass(frozen=True)
class SpecialDay:
    key: str
    name: str
    emoji: str
    blurb: str
    rule: object                      # year → (first day, last day) or None
    lead_days: int = 5                # start mentioning it this many days before
    categories: tuple = ()            # venue categories that fit ("irish pub")
    areas: tuple = ()                 # neighbourhoods that fit ("Church and Wellesley")
    keyword: str | None = None        # what to search ticketed events for
    theme: str | None = None          # what a plan's stop leans to that day
    theme_slot: str = "leisure"       # …and which stop


DAYS = [
    SpecialDay("st_patricks", "St. Patrick's Day", "☘️", "Pubs fill up early — go green.", _fixed(3, 17),
               categories=("irish pub",), keyword="St. Patrick", theme="irish pub"),
    SpecialDay("valentines", "Valentine's Day", "💘", "Book ahead — date spots go fast.", _fixed(2, 14),
               categories=("cocktail bar", "wine bar", "lounge"), keyword="Valentine", theme="cocktail"),
    SpecialDay("lunar_new_year", "Lunar New Year", "🧧", "Lion dances, dumplings and Chinatown nights.",
               _table("lunar_new_year"), categories=("dim sum", "chinese"),
               areas=("Chinatown", "East Chinatown", "Kensington-Chinatown"),
               keyword="Lunar New Year", theme="dim sum", theme_slot="dinner"),
    SpecialDay("cinco_de_mayo", "Cinco de Mayo", "🌮", "Tacos, margaritas and mariachi.", _fixed(5, 5),
               lead_days=3, categories=("mexican",), keyword="Cinco de Mayo", theme="mexican",
               theme_slot="dinner"),
    SpecialDay("pride", "Pride Toronto", "🏳️‍🌈", "Parade weekend — the Village is the place to be.", _pride,
               lead_days=7, categories=("gay bar", "lgbt"), areas=("Church-Wellesley", "Church-Wellesley Village"),
               keyword="Pride", theme="gay bar"),
    SpecialDay("canada_day", "Canada Day", "🍁", "Fireworks tonight on the waterfront.", _fixed(7, 1),
               lead_days=3, keyword="Canada Day"),
    SpecialDay("carnival", "Toronto Caribbean Carnival", "🎉", "Parade weekend — soca, mas bands and jerk.",
               _carnival, lead_days=7, categories=("caribbean", "jamaican", "trinidadian"),
               keyword="Carnival", theme="caribbean", theme_slot="dinner"),
    SpecialDay("nuit_blanche", "Nuit Blanche", "🌙", "Free art all night, dusk to dawn.", _nuit_blanche,
               categories=("art gallery", "art museum"), keyword="Nuit Blanche"),
    SpecialDay("halloween", "Halloween", "🎃", "Costume parties and haunted nights.", _fixed(10, 31),
               lead_days=7, categories=("dance club", "haunted house"), keyword="Halloween", theme="nightclub"),
    SpecialDay("diwali", "Diwali", "🪔", "Festival of lights — sweets and celebrations.", _table("diwali"),
               categories=("indian",), keyword="Diwali", theme="indian", theme_slot="dinner"),
    SpecialDay("nye", "New Year's Eve", "🥂", "Countdown parties — tickets sell out.", _fixed(12, 31),
               categories=("dance club", "lounge", "rooftop"), keyword="New Year", theme="nightclub"),
]
BY_KEY = {d.key: d for d in DAYS}


def local_today(now: datetime | None = None) -> date:
    return (now or datetime.now(timezone.utc)).astimezone(TZ).date()


def active(today: date | None = None) -> dict | None:
    """The special day happening now, or coming up within its lead time (the
    nearest one), as {key, name, emoji, blurb, start, end, today, days_until}."""
    today = today or local_today()
    best = None
    for d in DAYS:
        for y in (today.year, today.year + 1):        # NYE's lead time crosses into January's check
            span = d.rule(y)
            if not span:
                continue
            start, end = span
            if start - timedelta(days=d.lead_days) <= today <= end:
                until = max(0, (start - today).days)
                if best is None or until < best[1]:
                    best = (d, until, start, end)
    if not best:
        return None
    d, until, start, end = best
    return {"key": d.key, "name": d.name, "emoji": d.emoji, "blurb": d.blurb,
            "start": start.isoformat(), "end": end.isoformat(), "today": until == 0,
            "days_until": until, "keyword": d.keyword}


def matches(key: str, categories, area: str | None = None) -> bool:
    """Does a place (its categories, its neighbourhood) fit this special day?"""
    d = BY_KEY.get(key)
    if not d:
        return False
    kinds = " ".join(str(c).lower() for c in categories or [])
    if any(c in kinds for c in d.categories):
        return True
    return bool(area) and any(a.lower() == area.lower() for a in d.areas)


def theme_for(key: str | None) -> tuple[str, str] | None:
    """(stop, what to lean towards) for plans made on this day, e.g. ("leisure", "irish pub")."""
    d = BY_KEY.get(key or "")
    return (d.theme_slot, d.theme) if d and d.theme else None
