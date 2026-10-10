"""Budget steers the choice of places, not just how many: per-stop cost estimates,
each stop's share of the budget, cheaper swaps before dropping a stop, and a
free stop never dropped. Plus "no bars" meaning no drinking spots of any name."""
from app.services import costs, planner

LAT, LNG = 43.65, -79.38


def _c(name, cats, **kw):
    return {"name": name, "lat": LAT, "lng": LNG, "rating": 4.5, "review_count": 100,
            "categories": cats, **kw}


CITY = {
    "scenic": [_c("Quiet Park", ["park"])],
    "activity": [_c("Little Gallery", ["art gallery"]), _c("Arcade Zone", ["arcade"]),
                 _c("CN Tower", ["observation tower"])],
    "dinner": [_c("Pho Place", ["pho restaurant", "vietnamese restaurant"]),
               _c("Sushi Spot", ["sushi restaurant", "japanese restaurant"]),
               _c("Steak House", ["steakhouse"])],
}


def _offline(monkeypatch, city=CITY):
    monkeypatch.setattr(planner.weather_svc, "get_weather", lambda lat, lng: None)
    monkeypatch.setattr(planner.live_events, "available_for", lambda lat, lng: False)
    monkeypatch.setattr(planner.llm, "available", lambda: False)
    monkeypatch.setattr(planner, "_gather",
                        lambda anchor, slot_key, *a, **kw: [dict(c) for c in city.get(slot_key, [])])


def _plan(budget, party=2):
    return planner.build_plan(lat=LAT, lng=LNG, budget=budget, party_size=party, vibe="chill",
                              time_of_day="evening", target_stops=3)


def _by_slot(plan):
    return {s["slot"]: s["name"] for s in plan["stops"]}


def test_cost_estimates():
    est = costs.estimate
    assert est(_c("CN Tower", ["observation tower"]), "activity") == 45          # known admission
    assert est(_c("360 The Restaurant at the CN Tower", ["restaurant"]), "dinner") == 40   # not the tower's
    assert est(_c("Sushi Spot", ["sushi restaurant", "japanese restaurant"]), "dinner") == 45
    assert est(_c("Pizza Spot", ["pizza restaurant"]), "lunch") == 15           # lunch ~70% of dinner
    assert est(_c("High Park", ["park"]), "scenic") == 0
    assert est(_c("Toronto Islands", ["island"]), "scenic") == 9                 # the ferry
    assert est(_c("Someplace", ["restaurant"], price="$$$"), "dinner") == 65    # a real price tier wins
    assert est(_c("Mystery", ["unknown thing"]), "activity") == costs.TYPICAL["activity"]
    # "public market" isn't a pub; "barbecue" isn't a bar
    assert est(_c("St. Lawrence", ["public market"]), "activity") == 10
    assert est(_c("Smokehouse", ["barbecue restaurant"]), "dinner") == 35


def test_budget_is_shared_with_dinner_getting_the_most():
    share = costs.allowances(["scenic", "activity", "dinner"], 150, 2)    # $75 each
    assert share == {"scenic": 0, "activity": 25, "dinner": 50}
    assert costs.allowances(["dinner"], 0, 2) == {}
    assert costs.fit(45, 20) < 0 < costs.fit(15, 20) < costs.fit(20, 20)
    assert costs.fit(10, 30, "dinner") < costs.fit(25, 30, "dinner")      # a hot dog isn't dinner


def test_tight_budget_means_fewer_stops_not_cheaper_ones():
    night = ["scenic", "activity", "dinner", "leisure"]
    assert costs.fewer_stops(night, 90, 2) == ["scenic", "activity", "dinner"]       # $45 each
    assert costs.fewer_stops(night, 90, 2, keep=lambda s: s == "leisure") == ["scenic", "dinner", "leisure"]
    assert costs.fewer_stops(night, 400, 2) == night
    afternoon = ["scenic", "activity", "lunch", "activity", "dinner"]
    assert costs.fewer_stops(afternoon, 120, 4) == ["scenic", "activity", "lunch"]   # $30 each, kids


def test_a_bigger_budget_picks_nicer_places(monkeypatch):
    _offline(monkeypatch)
    assert _by_slot(_plan(40))["dinner"] == "Pho Place"
    assert _by_slot(_plan(400))["dinner"] == "Steak House"


def test_over_budget_swaps_cheaper_before_dropping(monkeypatch):
    city = dict(CITY, activity=[_c("CN Tower", ["observation tower"], boost=10),    # famous: picked first
                                _c("Arcade Zone", ["arcade"]), _c("Little Gallery", ["art gallery"])])
    _offline(monkeypatch, city)
    plan = _plan(100)
    stops = _by_slot(plan)
    assert set(stops) == {"scenic", "activity", "dinner"}          # nothing dropped
    assert stops["activity"] != "CN Tower"                          # swapped for a cheaper one
    assert plan["estimated_cost"] <= 110


def test_tight_budget_never_drops_the_free_stop(monkeypatch):
    _offline(monkeypatch)
    plan = _plan(40)
    assert "Quiet Park" in _by_slot(plan).values()
    assert plan["estimated_cost"] <= 44


def test_no_bars_means_no_drinking_spots_of_any_name():
    def avoided(name, cats, avoid="bars"):
        return planner.avoid_match({"name": name, "categories": cats}, avoid)
    assert avoided("The Office Pub", ["karaoke venue", "music venue"])
    assert avoided("Somewhere", ["cocktail bar", "bar"])
    assert not avoided("Kaito Sushi Bar", ["sushi restaurant"])      # a restaurant, not a bar
    assert not avoided("Barberian's Steak House", ["steakhouse"])
    assert avoided("High Park", ["park"], "parks")
    assert not avoided("Parkdale Pizza", ["pizza restaurant"], "parks")
    assert planner._is_alcohol_venue({"name": "The Office Pub", "categories": ["karaoke venue"]})
    assert not planner._is_alcohol_venue({"name": "Kaito Sushi Bar", "categories": ["sushi restaurant"]})


def test_no_fast_food_on_a_fancy_date(monkeypatch):
    """McDonald's listed as a plain burger restaurant (no brand) still isn't date night."""
    city = dict(CITY, dinner=[_c("McDonald's", ["burger restaurant", "restaurant"], boost=8),
                              _c("Sushi Spot", ["sushi restaurant", "japanese restaurant"])])
    _offline(monkeypatch, city)
    date = planner.build_plan(lat=LAT, lng=LNG, budget=40, party_size=2, vibe="romantic", group_type="date",
                              time_of_day="evening", target_stops=3)
    assert _by_slot(date)["dinner"] == "Sushi Spot"
    asked = planner.build_plan(lat=LAT, lng=LNG, budget=40, party_size=2, vibe="romantic", group_type="date",
                               time_of_day="evening", target_stops=3, interests="quick comfort food")
    assert _by_slot(asked)["dinner"] == "McDonald's"              # they asked for quick/comfort
    chill = planner.build_plan(lat=LAT, lng=LNG, budget=40, party_size=2, vibe="chill", group_type="friends",
                               time_of_day="evening", target_stops=3)
    assert "dinner" in _by_slot(chill)                            # a casual plan may still use it
