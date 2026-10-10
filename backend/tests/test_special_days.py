"""Special days: the right dates, mentioned a few days ahead, with the places
that fit them."""
from datetime import date

from app.services import special_days as sd


def test_date_rules():
    assert sd._pride(2026) == (date(2026, 6, 26), date(2026, 6, 28))          # parade: last Sunday of June
    assert sd._pride(2027) == (date(2027, 6, 25), date(2027, 6, 27))
    assert sd._carnival(2026) == (date(2026, 8, 1), date(2026, 8, 3))         # Saturday of the August long weekend
    assert sd._carnival(2027) == (date(2027, 7, 31), date(2027, 8, 2))
    assert sd._nuit_blanche(2026) == (date(2026, 10, 3), date(2026, 10, 3))   # first Saturday of October
    assert sd._nuit_blanche(2027) == (date(2027, 10, 2), date(2027, 10, 2))


def test_mentioned_ahead_and_on_the_day():
    soon = sd.active(date(2027, 3, 15))
    assert soon["key"] == "st_patricks" and soon["days_until"] == 2 and not soon["today"]
    on = sd.active(date(2027, 3, 17))
    assert on["today"] and on["days_until"] == 0
    assert sd.active(date(2027, 3, 18)) is None
    assert sd.active(date(2026, 12, 28))["key"] == "nye"
    assert sd.active(date(2027, 6, 27))["key"] == "pride"                    # still on: the parade day
    assert sd.active(date(2026, 10, 10)) is None


def test_moon_dated_days_have_dates_ahead():
    """Lunar New Year and Diwali come from a table: extend _LUNAR before it runs out."""
    for key, table in sd._LUNAR.items():
        assert date.today().year + 1 in table, f"add next year's {key} date to special_days._LUNAR"


def test_places_that_fit():
    assert sd.matches("st_patricks", ["irish pub", "pub"])
    assert not sd.matches("st_patricks", ["cocktail bar"])
    assert sd.matches("pride", ["bar"], "Church-Wellesley Village")
    assert not sd.matches("pride", ["bar"], "The Annex")
    assert sd.theme_for("st_patricks") == ("leisure", "irish pub")
    assert sd.theme_for("cinco_de_mayo") == ("dinner", "mexican")
    assert sd.theme_for("canada_day") is None
