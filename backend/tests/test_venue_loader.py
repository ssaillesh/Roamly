"""The monthly venue loader's matching rules (no network): a place's fame goes
to the right row, landmarks Overture lacks are added, and junk stays out."""
from scripts import load_venues as L


def _place(pid, name, cat, hier, lat, lng, brand=None):
    return {"id": pid, "name": name, "confidence": 0.9, "status": "open", "cat": cat, "hier": hier,
            "brand_wd": None, "brand": brand, "website": None, "address": None, "lat": lat, "lng": lng}


def _item(qid, name, types, links, lat, lng):
    return {"qid": qid, "name": name, "types": types, "links": links, "lat": lat, "lng": lng}


RESTAURANT = ["food_and_drink", "restaurant"]
TOWER, KENSINGTON, WILLOWDALE, MASSEY = (43.6426, -79.3871), (43.6547, -79.4005), (43.77, -79.41), (43.6541, -79.379)


def _build():
    places = [
        _place("cn", "CN Tower", "hiking_trail", ["attractions_and_activities", "hiking_trail"], *TOWER),
        _place("360", "360 The Restaurant at the CN Tower", "restaurant", RESTAURANT, TOWER[0] + 0.0002, TOWER[1]),
        _place("sub", "Subway", "fast_food_restaurant", RESTAURANT, 43.6500, -79.3800, brand="Subway"),
        _place("rom", "Royal Ontario Museum", "museum", ["arts_and_entertainment", "museum"], 43.6677, -79.3948),
        _place("mh1", "Massey Hall", "music_venue", ["arts_and_entertainment", "music_venue"], *MASSEY),
        _place("mh2", "Massey Hall", "theatre_venue", ["arts_and_entertainment", "theatre_venue"], 43.6468, -79.3862),
        _place("arena", "Scotiabank Arena", "music_venue", ["arts_and_entertainment", "music_venue"], 43.6435, -79.3791),
        _place("km", "Kensington Market", "grocery_store", ["retail", "grocery_store"], *KENSINGTON),
    ]
    # a lively neighbourhood: lots of places to eat around Kensington
    places += [_place(f"r{i}", f"Spot {i}", "restaurant", RESTAURANT, KENSINGTON[0] + i * 0.00005, KENSINGTON[1])
               for i in range(L.MIN_NEIGHBOURHOOD_SPOTS)]
    notable = [
        _item("Q1", "CN Tower", ["television tower", "observation tower"], 80, *TOWER),
        _item("Q2", "Toronto subway", ["rapid transit system"], 30, 43.6500, -79.3800),
        _item("Q3", "Royal Ontario Museum", ["museum"], 38, 43.6677, -79.3948),
        _item("Q4", "Massey Hall", ["concert hall"], 9, *MASSEY),
        _item("Q5", "Scotiabank Arena", ["indoor arena"], 40, 43.6435, -79.3791),
        _item("Q6", "Kensington Market", ["neighborhood"], 8, *KENSINGTON),
        _item("Q7", "Willowdale", ["neighborhood"], 8, *WILLOWDALE),
        _item("Q8", "Scotia Plaza", ["skyscraper", "office building"], 20, 43.6490, -79.3790),
    ]
    rows = L.build(places, notable, [], "toronto")
    return {v["name"]: v for v in rows}, rows


def test_names_match_despite_filler_words():
    assert L.norm("The Royal Ontario Museum (ROM)") == L.norm("Royal Ontario Museum")
    assert L.norm("Toronto Zoo") == "torontozoo"          # not just "zoo"


def test_fame_goes_to_the_right_place():
    by_name, rows = _build()
    assert by_name["CN Tower"]["id"] == "wd:Q1" and by_name["CN Tower"]["slots"] == ["activity"]
    assert by_name["360 The Restaurant at the CN Tower"]["notability"] == 0   # not the tower's fame
    assert by_name["Subway"]["notability"] == 0                               # the transit system's
    assert by_name["Royal Ontario Museum"]["notability"] == 38
    massey = [v for v in rows if v["name"] == "Massey Hall"]
    assert len(massey) == 1 and massey[0]["notability"] == 9                  # stray copy dropped


def test_junk_stays_out_and_only_lively_neighbourhoods_count():
    by_name, _ = _build()
    assert "Scotia Plaza" not in by_name                 # an office tower
    assert "Scotiabank Arena" not in by_name             # events, not a drinks stop
    assert by_name["Kensington Market"]["slots"] == ["scenic"]
    assert "Willowdale" not in by_name                   # nothing to do there
