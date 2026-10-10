"""Reading OpenStreetMap opening_hours: open/closed when the tag says, None
("unknown") when it can't be read — never a wrong "closed"."""
from datetime import datetime, timedelta

from app.services.opening_hours import is_open

MON = datetime(2026, 10, 5)          # a Monday


def at(day: int, hh: int, mm: int = 0) -> datetime:
    """day 0 = Monday … 6 = Sunday."""
    return MON + timedelta(days=day, hours=hh, minutes=mm)


def test_weekday_range():
    spec = "Mo-Fr 11:00-22:00"
    assert is_open(spec, at(4, 21, 59)) is True
    assert is_open(spec, at(4, 22, 0)) is False
    assert is_open(spec, at(5, 12)) is False          # Saturday isn't listed


def test_past_midnight_counts_toward_the_next_morning():
    spec = "Mo-Th 17:00-23:00; Fr-Sa 18:00-02:00"
    assert is_open(spec, at(5, 1, 30)) is True         # Friday night, 1:30 Saturday
    assert is_open(spec, at(6, 1, 30)) is True         # Saturday night
    assert is_open(spec, at(4, 1, 30)) is False        # Thursday closes at 23:00


def test_split_hours_and_days_off():
    spec = "Tu-Su 12:00-15:00,17:00-22:00; Mo off"
    assert is_open(spec, at(0, 13)) is False
    assert is_open(spec, at(1, 16)) is False           # between lunch and dinner
    assert is_open(spec, at(1, 18)) is True


def test_later_rule_wins_for_its_days():
    spec = "Mo-Su 10:00-22:00; Su 12:00-18:00"
    assert is_open(spec, at(6, 11)) is False
    assert is_open(spec, at(6, 13)) is True
    assert is_open(spec, at(2, 11)) is True


def test_day_range_wrapping_the_week():
    assert is_open("Sa-Mo 10:00-12:00", at(6, 11)) is True
    assert is_open("Sa-Mo 10:00-12:00", at(2, 11)) is False


def test_always_open_and_unknown():
    assert is_open("24/7", at(3, 4)) is True
    assert is_open("10:00-18:00", at(3, 12)) is True   # no days = every day
    for spec in (None, "", "sunrise-sunset", "Jan-Mar Mo-Fr 09:00-17:00", "by appointment"):
        assert is_open(spec, at(2, 12)) is None
