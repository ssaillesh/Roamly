"""Load a city's venues into the `venues` / `areas` tables the planner reads.

    pip install -r requirements-venues.txt          # duckdb (loader only, not the API)
    python -m scripts.load_venues                   # Toronto, newest Overture release
    python -m scripts.load_venues --skip-osm --dry-run

Runs monthly from .github/workflows/venues.yml against the production database
(DATABASE_URL = the database's External URL). Steps, each logged:
  1. Overture Maps places in the city's box (free, open data; includes Foursquare's
     open places) → planner stop types via venue_index.slots_for. Closed and
     low-confidence places are dropped.
  2. Wikidata: places with Wikipedia articles, and how many languages cover them
     → `notability` (the "well known" signal). Matched by name + distance.
  3. OpenStreetMap opening hours (best effort: the public servers are often busy;
     a tile that fails keeps no hours, and the plan treats hours as unknown).
  4. Overture neighbourhood names → `areas` (so "Kensington Market" is placed
     without a live geocoder).
  5. Replace the city's rows in one transaction.
"""
import argparse
import json
import math
import re
import sys
import time

import httpx

from app.database import SessionLocal
from app.models import Area, Venue
from app.services.venue_index import CITIES, readable, slots_for, wikidata_slots

UA = "Roamly-venue-loader/1.0 (+https://github.com/ssaillesh/Roamly)"
S3 = "s3://overturemaps-us-west-2/release/{release}/theme={theme}/type={type}/*"
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter",
            "https://overpass.private.coffee/api/interpreter"]
MIN_CONFIDENCE = 0.5


def log(*a):
    print(*a, flush=True)


def norm(s: str) -> str:
    """Name key for matching: "The Royal Ontario Museum" ≈ "Royal Ontario Museum (ROM)".
    Filler words go only if enough is left ("Toronto Zoo" stays torontozoo, not zoo)."""
    base = re.sub(r"\(.*?\)", "", (s or "").lower())
    short = re.sub(r"[^a-z0-9]", "", re.sub(r"\b(the|restaurant|toronto|bar|cafe|café|inc|ltd)\b", "", base))
    return short if len(short) >= 5 else re.sub(r"[^a-z0-9]", "", base)


def km(a_lat, a_lng, b_lat, b_lng):
    dlat, dlng = math.radians(b_lat - a_lat), math.radians(b_lng - a_lng)
    x = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(a_lat)) * math.cos(math.radians(b_lat)) * math.sin(dlng / 2) ** 2)
    return 2 * 6371 * math.asin(math.sqrt(x))


def latest_release() -> str:
    r = httpx.get("https://stac.overturemaps.org/catalog.json", timeout=20, headers={"User-Agent": UA})
    r.raise_for_status()
    return r.json()["latest"]


def overture(con, release: str, box) -> tuple[list[dict], list[dict]]:
    s, w, n, e = box
    where = f"bbox.xmin BETWEEN {w} AND {e} AND bbox.ymin BETWEEN {s} AND {n}"
    places = con.execute(f"""
        SELECT id, names.primary AS name, confidence, operating_status,
               taxonomy.primary AS cat, taxonomy.hierarchy AS hier,
               brand.wikidata AS brand_wd, brand.names.primary AS brand,
               websites[1] AS website, addresses[1].freeform AS address,
               ST_Y(ST_Centroid(geometry)) AS lat, ST_X(ST_Centroid(geometry)) AS lng
        FROM read_parquet('{S3.format(release=release, theme='places', type='place')}', hive_partitioning=1)
        WHERE {where} AND confidence >= {MIN_CONFIDENCE}
          AND coalesce(operating_status, 'open') = 'open' AND names.primary IS NOT NULL""").fetchall()
    cols = ["id", "name", "confidence", "status", "cat", "hier", "brand_wd", "brand", "website",
            "address", "lat", "lng"]
    places = [dict(zip(cols, r)) for r in places]
    areas = con.execute(f"""
        SELECT id, names.primary, subtype, ST_Y(ST_Centroid(geometry)), ST_X(ST_Centroid(geometry))
        FROM read_parquet('{S3.format(release=release, theme='divisions', type='division')}', hive_partitioning=1)
        WHERE {where} AND subtype IN ('neighborhood','macrohood','microhood','locality')
          AND names.primary IS NOT NULL""").fetchall()
    areas = [dict(zip(["id", "name", "kind", "lat", "lng"], r)) for r in areas]
    return places, areas


def wikidata(box) -> list[dict]:
    s, w, n, e = box
    q = f"""SELECT ?item ?itemLabel ?lat ?lng ?links
             (GROUP_CONCAT(DISTINCT ?typeLabel; separator="|") AS ?types) WHERE {{
      SERVICE wikibase:box {{ ?item wdt:P625 ?loc .
        bd:serviceParam wikibase:cornerSouthWest "Point({w} {s})"^^geo:wktLiteral .
        bd:serviceParam wikibase:cornerNorthEast "Point({e} {n})"^^geo:wktLiteral . }}
      ?item wikibase:sitelinks ?links . FILTER(?links >= 1)
      OPTIONAL {{ ?item wdt:P31 ?type . ?type rdfs:label ?typeLabel . FILTER(lang(?typeLabel) = "en") }}
      ?item rdfs:label ?itemLabel . FILTER(lang(?itemLabel) = "en")
      BIND(geof:latitude(?loc) AS ?lat) BIND(geof:longitude(?loc) AS ?lng)
    }} GROUP BY ?item ?itemLabel ?lat ?lng ?links"""
    r = httpx.get("https://query.wikidata.org/sparql", params={"query": q}, timeout=120,
                  headers={"Accept": "application/sparql-results+json", "User-Agent": UA})
    r.raise_for_status()
    out = []
    for b in r.json()["results"]["bindings"]:
        try:
            out.append({"qid": b["item"]["value"].rsplit("/", 1)[-1], "name": b["itemLabel"]["value"],
                        "lat": float(b["lat"]["value"]), "lng": float(b["lng"]["value"]),
                        "links": int(b["links"]["value"]),
                        "types": [t for t in b.get("types", {}).get("value", "").split("|") if t]})
        except (KeyError, ValueError):
            continue
    return out


def osm_hours(box, tile=0.08, budget_s=480) -> list[dict]:
    """Named places with opening_hours, tile by tile; a tile that fails is skipped,
    and the whole step stops after `budget_s` (the public servers may be down)."""
    s, w, n, e = box
    out, failed, lat = [], 0, s
    stop = time.monotonic() + budget_s
    while lat < n:
        lng = w
        while lng < e:
            if time.monotonic() > stop:
                log(f"  OSM: time limit reached; {len(out)} places with hours so far")
                return out
            q = (f"[out:json][timeout:60];nwr[name][opening_hours]"
                 f"({lat},{lng},{min(lat + tile, n)},{min(lng + tile, e)});out center tags;")
            got = None
            for url in OVERPASS:
                if time.monotonic() > stop:
                    break
                try:
                    r = httpx.post(url, data={"data": q}, timeout=60, headers={"User-Agent": UA})
                    if r.status_code == 200:
                        got = r.json().get("elements", [])
                        break
                except (httpx.HTTPError, ValueError):
                    pass
                time.sleep(2)
            if got is None:
                failed += 1
            for el in got or []:
                t = el.get("tags") or {}
                c = el.get("center") or el
                if c.get("lat") is not None and t.get("name") and t.get("opening_hours"):
                    out.append({"name": t["name"], "lat": c["lat"], "lng": c["lon"],
                                "hours": t["opening_hours"][:300]})
            lng += tile
        lat += tile
    log(f"  OSM: {len(out)} places with hours ({failed} tiles unavailable)")
    return out


def match(rows: list[dict], items: list[dict], within_km, apply, loose: bool = False,
          fits=lambda row, item: True) -> set[int]:
    """For each item, the nearest row that fits(row, item) with the same name (or, if
    `loose`, one name containing the other and at least half its length) within
    within_km(row) km; apply(row, item). Returns the indexes of the items that matched."""
    grid: dict[tuple[int, int], list[dict]] = {}
    for r in rows:
        r["_key"] = norm(r["name"])
        grid.setdefault((round(r["lat"] * 50), round(r["lng"] * 50)), []).append(r)
    hit = set()
    for i, it in enumerate(items):
        key = norm(it["name"])
        if len(key) < 3:
            continue
        gy, gx = round(it["lat"] * 50), round(it["lng"] * 50)       # ~2 km cells
        near = [r for dy in (-1, 0, 1) for dx in (-1, 0, 1) for r in grid.get((gy + dy, gx + dx), [])]
        cands = [r for r in near if r["_key"] == key or (
            loose and min(len(key), len(r["_key"])) >= 6
            and min(len(key), len(r["_key"])) * 2 >= max(len(key), len(r["_key"]))
            and (key in r["_key"] or r["_key"] in key))]
        cands = [r for r in cands if km(r["lat"], r["lng"], it["lat"], it["lng"]) <= within_km(r)
                 and fits(r, it)]
        if cands:
            best = min(cands, key=lambda r: (r["_key"] != key, km(r["lat"], r["lng"], it["lat"], it["lng"])))
            apply(best, it)
            hit.add(i)
    return hit


# Wikidata types that make an item a venue worth matching even without a stop type of its own.
_WD_VENUE = ("restaurant", "bar", "pub", "café", "cafe", "coffeehouse", "bakery", "brewery", "nightclub",
             "music venue", "event venue", "concert venue", "venue", "cultural center", "arena", "stadium")
# Big sports/concert venues: "What's on" covers their events; never a drinks stop.
_WD_BIG_VENUE = ("stadium", "arena", "sports venue", "ballpark")
# How many Wikipedia editions a Wikidata-only place needs to become a stop. A
# neighbourhood also needs to be lively: this many places to eat or drink within
# ~500 m (Kensington Market ~380; leafy residential ones like Bridle Path ~0).
MIN_LINKS, MIN_LINKS_NEIGHBOURHOOD, MIN_NEIGHBOURHOOD_SPOTS = 3, 5, 60
_FOOD_DRINK = {"dinner", "cafe", "drinks", "dessert"}


def _ends(types: list[str], words) -> bool:
    return any(t.lower() == w or t.lower().endswith(" " + w) for t in types for w in words)


def build(places: list[dict], notable: list[dict], hours: list[dict], city: str) -> list[dict]:
    # Notability first: it decides whether an attraction earns a stop.
    for p in places:
        p["notability"], p["wikidata"], p["hours"], p["drop"] = 0, None, None, False
        p["_slots"] = slots_for(p["cat"], p["hier"], notable=True)
    stops = [p for p in places if p["_slots"]]
    for it in notable:
        it["slots"] = wikidata_slots(it["types"])
    usable = [it for it in notable if it["slots"] or _ends(it["types"], _WD_VENUE)]

    def fits(row, it):
        if not it["slots"]:     # a famous restaurant/bar/venue: never a chain branch
            return not (row["brand_wd"] or row["brand"])
        # a museum's fame goes to the museum, not the café inside it
        want = set(it["slots"]) | ({"leisure"} if "activity" in it["slots"] else set())
        return bool(want & set(row["_slots"]))

    def set_notable(row, it):
        if _ends(it["types"], _WD_BIG_VENUE):
            row["drop"] = True
        elif it["links"] > row["notability"]:
            row["notability"], row["wikidata"] = it["links"], it["qid"]

    def reach(row):   # parks and attractions are big: their points can sit far apart
        return 1.5 if row["cat"] in ("park", "beach", "botanical_garden", "nature_reserve", "island") else 0.4
    matched = {id(usable[i]) for i in match(stops, usable, reach, set_notable, loose=True, fits=fits)}
    log(f"  Wikidata matches: {len(matched)} (of {len(usable)} stop-like items)")
    log(f"  hours matches: {len(match(places, hours, lambda r: 0.08, lambda row, it: row.update(hours=it['hours'])))}")
    out = []
    # Well-known places Overture doesn't list as stops (CN Tower, Distillery District…), by their Wikidata type.
    taken = {v["wikidata"] for v in places if v["wikidata"]}
    food = [(p["lat"], p["lng"]) for p in stops if _FOOD_DRINK & set(p["_slots"])]

    def lively(it):
        return sum(1 for y, x in food if abs(y - it["lat"]) < 0.0045 and abs(x - it["lng"]) < 0.006) \
            >= MIN_NEIGHBOURHOOD_SPOTS

    for it in usable:
        if not it["slots"] or id(it) in matched or it["qid"] in taken:
            continue
        kind = next(t for t in it["types"] if wikidata_slots([t]))
        hood = kind.endswith("neighborhood")
        if it["links"] >= (MIN_LINKS_NEIGHBOURHOOD if hood else MIN_LINKS) and (not hood or lively(it)):
            taken.add(it["qid"])
            slots = it["slots"]
            out.append({"id": f"wd:{it['qid']}", "city": city, "name": it["name"][:200],
                        "lat": it["lat"], "lng": it["lng"], "slots": slots, "category": kind[:80],
                        "categories": [kind], "chain": False, "fast_food": False, "website": None,
                        "address": None, "hours": None, "notability": float(it["links"]),
                        "wikidata": it["qid"], "confidence": 1.0})
    log(f"  added from Wikidata: {len(out)} well-known places Overture doesn't list")
    for p in places:
        slots = slots_for(p["cat"], p["hier"], notable=p["notability"] > 0)
        if not slots or p["drop"]:
            continue
        hier = [readable(h) for h in (p["hier"] or [])]
        cats = list(dict.fromkeys([readable(p["cat"]), *reversed(hier[1:])]))   # specific → general
        out.append({
            "id": f"ovt:{p['id']}"[:80], "city": city, "name": p["name"][:200],
            "lat": p["lat"], "lng": p["lng"], "slots": slots, "category": readable(p["cat"])[:80],
            "categories": [c for c in cats if c][:6],
            "chain": bool(p["brand_wd"] or p["brand"]), "fast_food": p["cat"] == "fast_food_restaurant",
            "website": (p["website"] or None) and p["website"][:500],
            "address": (p["address"] or None) and p["address"][:300],
            "hours": p["hours"], "notability": float(p["notability"]), "wikidata": p["wikidata"],
            "confidence": float(p["confidence"] or 0)})
    return one_per_famous_name(out)


def one_per_famous_name(venues: list[dict]) -> list[dict]:
    """A well-known name is one place: Overture often has stray copies of it
    ("Massey Hall" at Roy Thomson Hall's address, a second "High Park" downtown).
    Drop unknown same-name copies within 10 km of a known one."""
    famous: dict[str, list[dict]] = {}
    for v in venues:
        if v["notability"] >= 3 and not v["chain"]:
            famous.setdefault(norm(v["name"]), []).append(v)
    keep = []
    for v in venues:
        twins = famous.get(norm(v["name"]), [])
        if v["notability"] == 0 and any(km(t["lat"], t["lng"], v["lat"], v["lng"]) <= 10 for t in twins):
            continue
        keep.append(v)
    return keep


def write(city: str, venues: list[dict], areas: list[dict]) -> None:
    with SessionLocal() as db:
        db.query(Venue).filter(Venue.city == city).delete(synchronize_session=False)
        db.query(Area).filter(Area.city == city).delete(synchronize_session=False)
        for i in range(0, len(venues), 1000):
            db.bulk_insert_mappings(Venue, venues[i:i + 1000])
        db.bulk_insert_mappings(Area, [{"id": f"ovt:{a['id']}"[:80], "city": city, "name": a["name"][:200],
                                        "kind": a["kind"], "lat": a["lat"], "lng": a["lng"]} for a in areas])
        db.commit()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="toronto", choices=sorted(CITIES))
    ap.add_argument("--release", default=None, help="Overture release (default: newest)")
    ap.add_argument("--skip-osm", action="store_true", help="don't fetch opening hours")
    ap.add_argument("--dry-run", action="store_true", help="build everything, write nothing")
    args = ap.parse_args(argv)
    import duckdb   # loader-only dependency (requirements-venues.txt)

    box = CITIES[args.city]
    release = args.release or latest_release()
    log(f"Loading {args.city} from Overture {release} …")
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; INSTALL spatial; LOAD spatial; SET s3_region='us-west-2';")
    t = time.time()
    places, areas = overture(con, release, box)
    log(f"  Overture: {len(places)} open places, {len(areas)} neighbourhoods ({time.time() - t:.0f}s)")
    try:
        notable = wikidata(box)
    except httpx.HTTPError as e:
        log(f"  Wikidata unavailable ({e}); notability left at 0")
        notable = []
    log(f"  Wikidata: {len(notable)} places with Wikipedia articles")
    hours = [] if args.skip_osm else osm_hours(box)
    venues = build(places, notable, hours, args.city)
    by_slot: dict[str, int] = {}
    for v in venues:
        for s in v["slots"]:
            by_slot[s] = by_slot.get(s, 0) + 1
    log(f"  venues: {len(venues)} — " + json.dumps(by_slot))
    log(f"  with hours: {sum(1 for v in venues if v['hours'])}, "
        f"notable: {sum(1 for v in venues if v['notability'] > 0)}, chains: {sum(v['chain'] for v in venues)}")
    if len(venues) < 1000:
        log("Refusing to write: suspiciously few venues (a failed download?). Existing rows kept.")
        return 1
    if args.dry_run:
        log("Dry run: nothing written.")
        return 0
    write(args.city, venues, areas)
    log(f"Wrote {len(venues)} venues and {len(areas)} areas for {args.city}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
