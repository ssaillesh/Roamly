"""Reading a typed request without the AI (it's off, over its limits, or answered badly):
the regex fallback must not mistake times, distances or other words for settings."""
import pytest

from app.api.plan import _budget, _extract_avoid, _extract_location, _heuristic_extract, _party_size, _target_stops


@pytest.mark.parametrize("text, budget", [
    ("$60 date night", 60), ("60 each", 60), ("60 bucks for us", 60), ("under 80", 80),
    ("budget of 120", 120), ("around 40", 40), ("50-100", 100), ("$50 - $100", 100),
])
def test_money_is_read(text, budget):
    assert _budget(text) == budget


@pytest.mark.parametrize("text", [
    "dinner at 10 pm", "meet at 7:30", "within 15 km", "20 minutes away", "4 friends",
    "2025 plans", "10-11pm", "for 12 people", "3 stops",
])
def test_times_distances_and_counts_are_not_money(text):
    assert _budget(text) is None


@pytest.mark.parametrize("text, key, value", [
    ("by car", "transport", "car"), ("take the ttc", "transport", "transit"),
    ("walking only", "transport", "walk"), ("relaxing night", "vibe", "chill"),
    ("a fancy evening", "vibe", "extravagant"), ("brunch then a walk", "time_of_day", "morning"),
])
def test_words_are_read(text, key, value):
    assert _heuristic_extract(text)[key] == value


@pytest.mark.parametrize("text, key", [
    ("pay by card", "transport"),             # "car" inside "card"
    ("a business dinner", "transport"),       # "bus" inside "business"
    ("training then dinner", "transport"),    # "train" inside "training"
    ("any update on tonight", "vibe"),        # "date" inside "update"
    ("any update on tonight", "group_type"),
])
def test_words_inside_other_words_are_not_read(text, key):
    assert key not in _heuristic_extract(text)


def test_party_size():
    assert _party_size("4 friends") == 4
    assert _party_size("me and 3 friends") == 4
    assert _party_size("for six") == 6
    assert _party_size("for 6 dollars") is None
    assert _party_size("dinner at 7") is None


def test_avoid():
    assert _extract_avoid("no bars please") == "bars"
    assert "museums" in _extract_avoid("i don't want to do museums")
    assert _extract_avoid("not sure yet") == ""
    assert _extract_avoid("no later than 10") == ""
    assert _extract_avoid("no limit") == ""


def test_location_guess():
    assert _extract_location("Dinner in Kensington Market tonight") == "Kensington Market"
    assert _extract_location("something fun nearby") is None


def test_stop_count():
    assert _target_stops("a full day out", None) == 6
    assert _target_stops("something tonight", "night") == 4
