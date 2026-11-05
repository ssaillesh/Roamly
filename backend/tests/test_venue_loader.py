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


# ---- photos: website preview image, else Wikimedia Commons ----

def test_og_image_is_read_resolved_and_made_https():
    page = """<head><meta name="twitter:image" content="/tw.jpg">
      <meta content="http://cdn.example.com/a&amp;b/room.jpg" property="og:image"></head>"""
    assert L.og_image(page, "https://bar.example.com/menu/") == "https://cdn.example.com/a&b/room.jpg"
    # og:image missing → twitter:image, relative to the page
    assert L.og_image('<meta name="twitter:image" content="img/tw.jpg">', "https://bar.example.com/menu/") \
        == "https://bar.example.com/menu/img/tw.jpg"
    assert L.og_image("<head><title>No preview</title></head>", "https://x.example") is None


def test_logos_and_placeholders_are_not_photos():
    logo = '<meta property="og:image" content="https://x.example/wp-content/uploads/LOGO-dark.png">'
    svg = '<meta property="og:image" content="https://x.example/brand.svg">'
    assert L.og_image(logo, "https://x.example") is None
    assert L.og_image(svg, "https://x.example") is None
    # …but a real photo after a logo candidate still counts
    both = logo.replace("og:image", "og:image:secure_url") + '<meta property="og:image" content="/patio.jpg">'
    assert L.og_image(both, "https://x.example") == "https://x.example/patio.jpg"


def test_social_and_aggregator_links_are_not_fetched():
    assert not L.own_site("https://www.instagram.com/barraval/")
    assert not L.own_site("https://m.facebook.com/somebar")
    assert L.own_site("https://www.barraval.com/")
    assert L.fetch_og_image(None, "instagram.com/barraval") == ""      # never touches the network


def test_commons_thumbnail():
    p18 = "http://commons.wikimedia.org/wiki/Special:FilePath/Massey%20Hall%202021.jpg"
    assert L.commons_thumb(p18) == "https://commons.wikimedia.org/wiki/Special:FilePath/Massey%20Hall%202021.jpg?width=640"
    assert L.commons_thumb("http://commons.wikimedia.org/wiki/Special:FilePath/Map.svg") is None
    assert L.commons_thumb(None) is None


def _v(vid, website=None, wd=None, slots=("drinks",), notability=0.0):
    return {"id": vid, "website": website, "_wd_image": wd, "slots": list(slots), "notability": notability}


def test_earlier_results_are_reused_and_only_new_sites_fetched():
    commons = "https://commons.wikimedia.org/wiki/Special:FilePath/X.jpg?width=640"
    prev = {"a": ("https://a.example", "https://a.example/og.jpg"),   # found last time
            "b": ("https://b.example", ""),                           # looked, nothing
            "c": ("https://old-c.example", "https://old-c.example/og.jpg"),  # website changed
            "d": ("https://d.example", None),                         # never reached
            "e": ("https://e.example", commons)}                      # website gave nothing usable then
    venues = [_v("a", "https://a.example"), _v("b", "https://b.example"), _v("c", "https://c.example"),
              _v("d", "https://d.example"), _v("e", "https://e.example", wd=commons), _v("f"),
              _v("g", "https://c.example", slots=("activity",))]
    assert L.sites_to_fetch(venues, prev) == ["https://c.example", "https://d.example", "https://e.example"]
    fetched = {"https://c.example": "https://c.example/new.jpg", "https://d.example": None, "https://e.example": ""}
    L.assign_images(venues, prev, fetched)
    got = {v["id"]: v["image"] for v in venues}
    assert got == {"a": "https://a.example/og.jpg", "b": "", "c": "https://c.example/new.jpg",
                   "d": None,                      # still unreached: look again next month
                   "e": commons,                   # the website had nothing → Commons photo
                   "f": "",                        # no website, no Commons photo
                   "g": "https://c.example/new.jpg"}   # same website, fetched once
    assert all("_wd_image" not in v for v in venues)


def test_night_spots_are_looked_at_first():
    venues = [_v("museum", "https://m.example", slots=("activity",), notability=40),
              _v("diner", "https://d.example", slots=("dinner",)),
              _v("bar", "https://b.example", slots=("drinks",))]
    assert L.sites_to_fetch(venues, {}) == ["https://b.example", "https://d.example", "https://m.example"]


def test_wikidata_photo_follows_the_fame_match():
    by_name, rows = _build()
    assert all("_wd_image" in v for v in rows)
