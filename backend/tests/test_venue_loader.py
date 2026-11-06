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


# ---- only real photos: every candidate is looked at ----
import io
import random

import httpx
from PIL import Image, ImageDraw


def _jpeg(img):
    buf = io.BytesIO(); img.convert("RGB").save(buf, "JPEG"); return buf.getvalue()


def _photo(w=900, h=600, dark=False):
    """Stands in for a photo: lots of colours, no flat field."""
    rnd = random.Random(7)
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(0, w, 3):
            base = 40 if dark else 120
            c = (base + (x * 90 // w) + rnd.randint(0, 40), base // 2 + (y * 70 // h) + rnd.randint(0, 40), base // 3 + rnd.randint(0, 50))
            for dx in range(3):
                if x + dx < w:
                    px[x + dx, y] = c
    return img


def _logo(w=1200, h=630):
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    d.ellipse([w // 2 - 150, h // 2 - 150, w // 2 + 150, h // 2 + 150], fill=(200, 20, 30))
    d.rectangle([w // 2 - 300, h - 120, w // 2 + 300, h - 80], fill=(20, 20, 20))
    return img


def test_photos_pass_and_logos_cards_and_cutouts_dont():
    ok = lambda img: L.looks_like_photo(L.photo_stats(img))
    assert ok(_photo())
    assert ok(_photo(dark=True))                       # a dim dining room is still a photo
    assert not ok(_logo())                             # one flat colour with a mark on it
    cut = Image.new("RGBA", (800, 600), (0, 0, 0, 0)); cut.paste(_photo(400, 300), (200, 150))
    assert not ok(cut)                                 # a transparent cut-out
    assert not ok(_photo(300, 200))                    # too small to show
    assert not ok(_photo(1600, 300))                   # a banner strip


def test_every_photo_a_page_offers_in_order():
    page = """<html><head>
      <meta property="og:image" content="/brand/LOGO.png">
      <meta property="og:image" content="/share/patio.jpg">
      <link rel="image_src" href="https://cdn.example.com/room.jpg">
      <script type="application/ld+json">{"@graph":[{"@type":"Restaurant","image":["/ld/dining.jpg",{"url":"/ld/bar.jpg"}]}]}</script>
      </head><body>
      <img src="/small-thumb.jpg" width="120">
      <img srcset="/hero-800.jpg 800w, /hero-1600.jpg 1600w" alt="">
      <img src="data:image/png;base64,AAAA" width="900">
      <img src="/icons/x.png" width="900">
      </body></html>"""
    assert L.page_photos(page, "http://bar.example.com/") == [
        "https://bar.example.com/share/patio.jpg",      # the logo share image is skipped
        "https://bar.example.com/ld/dining.jpg", "https://bar.example.com/ld/bar.jpg",
        "https://cdn.example.com/room.jpg",
        "https://bar.example.com/hero-1600.jpg",        # the widest of a srcset
    ]


def test_a_website_logo_is_passed_over_for_a_real_photo_on_the_page():
    logo, photo = _jpeg(_logo()), _jpeg(_photo())
    page = (b'<html><head><meta property="og:image" content="/og-share.jpg"></head>'
            b'<body><img src="/gallery/dining-room.jpg"></body></html>')

    def handler(req):
        path = req.url.path
        if path == "/":
            return httpx.Response(200, content=page, headers={"content-type": "text/html; charset=utf-8"})
        if path == "/og-share.jpg":
            return httpx.Response(200, content=logo, headers={"content-type": "image/jpeg"})
        if path == "/gallery/dining-room.jpg":
            return httpx.Response(200, content=photo, headers={"content-type": "image/jpeg"})
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as c:
        assert L.fetch_og_image(c, "https://bar.example.com") == "https://bar.example.com/gallery/dining-room.jpg"

    # only a logo on the site → "" (looked, nothing worth showing), never the logo
    only_logo = b'<html><head><meta property="og:image" content="/og-share.jpg"></head><body></body></html>'
    def handler2(req):
        if req.url.path == "/":
            return httpx.Response(200, content=only_logo, headers={"content-type": "text/html"})
        return httpx.Response(200, content=logo, headers={"content-type": "image/jpeg"})
    with httpx.Client(transport=httpx.MockTransport(handler2), follow_redirects=True) as c:
        assert L.fetch_og_image(c, "https://logo-only.example.com") == ""


def test_commons_photo_must_name_the_venue_as_a_whole_phrase():
    titles = ["File:Construction west side of Victoria, south of Dundas.jpg",
              "File:Barrio Cerveceria (37562563021).jpg",
              "File:Velvet Underground logo.png",
              "File:Inner Wave at the Velvet Underground.jpg"]
    assert L.commons_match("Velvet Underground", titles) == "Inner Wave at the Velvet Underground.jpg"
    assert L.commons_match("Barrio Lounge", titles) is None
    assert L.commons_match("Restaurant 20 Victoria", titles) is None
    assert L.commons_match("The Royal", ["File:Royal Cinema.jpg"]) == "Royal Cinema.jpg"
    assert L.commons_match("Massey Hall", ["File:Massey Hall map.jpg", "File:Massey Hall plan.svg"]) is None
    assert L.name_phrase("The Rex") == "rex"
    assert L.name_phrase("The") is None and L.name_phrase("A&W") is None


def test_last_months_commons_find_is_kept():
    commons = "https://commons.wikimedia.org/wiki/Special:FilePath/X.jpg?width=640"
    venues = [{"id": "a", "image": "", "slots": ["drinks"], "notability": 0, "name": "Bar A", "lat": 1, "lng": 2}]
    L.add_commons(venues, {"a": (None, commons)}, budget_s=0)
    assert venues[0]["image"] == commons


def test_a_lapsed_domain_taken_over_by_spam_gives_no_photo():
    spam = (b'<html><head><title>Situs Slot Gacor Maxwin</title><meta property="og:image" content="/promo.jpg"></head></html>')
    photo = _jpeg(_photo())
    def handler(req):
        if req.url.path == "/":
            return httpx.Response(200, content=spam, headers={"content-type": "text/html"})
        return httpx.Response(200, content=photo, headers={"content-type": "image/jpeg"})
    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as c:
        assert L.fetch_og_image(c, "https://old-bistro.example.com") == ""


def test_one_photo_on_many_places_is_not_a_photo_of_any_of_them():
    sky = "https://city.example/skyline.jpg"
    venues = [{"id": str(i), "image": sky} for i in range(5)] + [{"id": "x", "image": "https://bar.example/room.jpg"},
                                                                {"id": "y", "image": ""}]
    assert L.drop_shared_photos(venues) == 5
    assert [v["image"] for v in venues] == [""] * 5 + ["https://bar.example/room.jpg", ""]
