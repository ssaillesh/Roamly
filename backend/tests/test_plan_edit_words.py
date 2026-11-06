"""The plan page's buttons send fixed phrases ("swap X for something similar", "add a
late-night bite at the end of the night"). With the AI off they go through keyword
routing, so those exact phrases must turn into the right edits."""
from app.services.plan_edit import real_wish, slot_for_text
from app.services.plan_intent import route_rules

PLAN = {"stops": [
    {"name": "Bar Raval", "slot": "dinner"},
    {"name": "The Rex Hotel Jazz", "slot": "leisure"},
    {"name": "Sweet Olenka's", "slot": "dessert"},
]}


def test_late_bite_is_food_not_an_activity():
    assert slot_for_text("late-night bite at the end of the night") == "dessert"
    assert slot_for_text("a snack") == "dessert"
    # …but a bite to eat for dinner is still dinner
    assert slot_for_text("a bite to eat") == "dinner"


def test_the_late_bite_button():
    r = route_rules("add a late-night bite at the end of the night", PLAN)
    assert r["intent"] == "edit_plan"
    op = r["ops"][0]
    assert op["op"] == "add" and slot_for_text(op["wish"], "activity") == "dessert"


def test_the_swap_button_asks_for_another_like_it():
    r = route_rules("swap the rex hotel jazz for something similar", PLAN)
    assert r["intent"] == "edit_plan"
    op = r["ops"][0]
    assert op["op"] == "swap" and op.get("name") == "The Rex Hotel Jazz"
    assert real_wish(op["wish"]) == ""          # "something similar" is no wish: same kind of stop


def test_real_wishes_are_kept():
    assert real_wish("sushi") == "sushi"
    assert real_wish("something with a patio") == "something with a patio"
    for none in ("something similar", "something else", "another one", "somewhere different", "anything"):
        assert real_wish(none) == ""


def test_remove_and_cheaper():
    assert route_rules("remove the dessert", PLAN)["ops"] == [{"op": "remove", "slot": "dessert"}]
    swap = route_rules("make the dinner cheaper", PLAN)["ops"][0]
    assert swap["op"] == "swap" and swap["wish"] == "cheaper"


def test_no_plan_or_start_over_is_a_new_plan():
    assert route_rules("add bowling", None)["intent"] == "new_plan"
    assert route_rules("start over", PLAN)["intent"] == "new_plan"


PLAN4 = {"stops": PLAN["stops"] + [{"name": "Bowl-a-rama", "slot": "activity"}]}


def test_whole_plan_changes_are_edits_not_new_plans():
    cases = {
        "can we start at 8pm": [{"op": "set_start", "time": "8pm"}],
        "begin around 7:30": [{"op": "set_start", "time": "7:30"}],
        "make it for 6": [{"op": "set_party", "party_size": 6}],
        "6 of us now": [{"op": "set_party", "party_size": 6}],
        "it's just me now": [{"op": "set_party", "party_size": 1}],
        "make it shorter": [{"op": "set_length", "stops": 3}],
        "add one more stop": [{"op": "set_length", "stops": 5}],
        "just 3 stops": [{"op": "set_length", "stops": 3}],
        "move the dessert to the start": [{"op": "move", "slot": "dessert", "to": 1}],
        "move bar raval to the end": [{"op": "move", "name": "Bar Raval", "to": 4}],
    }
    for text, ops in cases.items():
        r = route_rules(text, PLAN4)
        assert (r["intent"], r["ops"]) == ("edit_plan", ops), text


def test_stop_edits_still_work_alongside():
    assert route_rules("add bowling", PLAN4)["ops"] == [{"op": "add", "wish": "bowling"}]
    assert route_rules("swap the dinner for sushi", PLAN4)["ops"] == [{"op": "swap", "slot": "dinner", "wish": "sushi"}]
