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
  5. Photos (best effort, time-boxed), only real photos of the place: the venue
     website's own (its share image, schema.org photos, then big pictures on the
     page), else its Wikimedia Commons photo from Wikidata, else a geotagged Commons
     photo taken at the venue whose title names it. Every candidate is downloaded
     and checked to be a photo, not a logo or text card (looks_like_photo). Sites
     checked on an earlier run keep that result, so each month only fetches new or
     changed websites (--refresh-images redoes all; --recheck-photos re-checks the
     stored ones).
  6. Replace the city's rows in one transaction.
"""
import argparse
import hashlib
import html as htmllib
import io
import json
import math
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, unquote, urljoin, urlsplit

import httpx
from sqlalchemy import inspect, select

from app.database import SessionLocal, engine
from app.models import Area, Venue
from app.services.venue_index import CITIES, readable, slots_for, wikidata_slots

UA = "Rova-venue-loader/1.0 (+https://github.com/ssaillesh/Roamly)"
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
    q = f"""SELECT ?item ?itemLabel ?lat ?lng ?links (SAMPLE(?img) AS ?image)
             (GROUP_CONCAT(DISTINCT ?typeLabel; separator="|") AS ?types) WHERE {{
      SERVICE wikibase:box {{ ?item wdt:P625 ?loc .
        bd:serviceParam wikibase:cornerSouthWest "Point({w} {s})"^^geo:wktLiteral .
        bd:serviceParam wikibase:cornerNorthEast "Point({e} {n})"^^geo:wktLiteral . }}
      ?item wikibase:sitelinks ?links . FILTER(?links >= 1)
      OPTIONAL {{ ?item wdt:P18 ?img . }}
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
                        "image": commons_thumb(b.get("image", {}).get("value")),
                        "types": [t for t in b.get("types", {}).get("value", "").split("|") if t]})
        except (KeyError, ValueError):
            continue
    return out


# ---------------------------------------------------------------- photos
# Every photo shown for a venue is a real photo of that place, from one of two free
# sources: the venue's own website, or a geotagged Wikimedia Commons photo taken there.
# Each candidate is downloaded and looked at (photo_stats / looks_like_photo) before
# it's kept: logos, text cards, cut-outs and screenshots are dropped, since the app's
# category icon reads better than a picture that isn't one.
IMAGE_BUDGET_S = 600            # the website step; what's left waits for next month
IMAGE_WORKERS = 24
COMMONS_BUDGET_S = 240          # the Commons step (Wikimedia asks for gentle, few-at-a-time use)
COMMONS_WORKERS = 4
COMMONS_RADIUS_M = 75
# Only the URL is stored, never the picture (the browser loads it from the venue's
# site or Commons), and it's capped: ~120 bytes a photo, well under 1 MB a city.
MAX_IMAGE_URL = 500
_PAGE_BYTES = 256 * 1024        # never download a whole site
_PHOTO_BYTES = 8 * 1024 * 1024  # a candidate photo bigger than this isn't worth the wait
_MAX_PIXELS = 40_000_000
_TRIES_PER_SITE = 5             # candidate photos checked per website
# Links that aren't the venue's own site: login walls, aggregators, link-in-bio pages.
_NOT_OWN_SITE = ("facebook.com", "instagram.com", "twitter.com", "x.com", "tiktok.com", "youtube.com",
                 "linktr.ee", "yelp.com", "yelp.ca", "google.com", "goo.gl", "g.page", "tripadvisor.com",
                 "tripadvisor.ca", "ubereats.com", "doordash.com", "skipthedishes.com", "toasttab.com")
# A URL that names itself a logo or placeholder isn't even downloaded.
_NOT_A_PHOTO = re.compile(r"logo|favicon|icon|sprite|placeholder|blank|default[-_]?(?:image|share|og)|\.(?:svg|ico|gif)(?:$|\?)", re.I)
_META = re.compile(r"<meta\b[^>]*>", re.I)
_LINK = re.compile(r"<link\b[^>]*>", re.I)
_IMG = re.compile(r"<img\b[^>]*>", re.I)
_LD = re.compile(r"<script\b[^>]*application/ld\+json[^>]*>(.*?)</script>", re.I | re.S)
_ATTR = re.compile(r"""([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""")
_OG_KEYS = ("og:image:secure_url", "og:image", "og:image:url", "twitter:image", "twitter:image:src")
# A venue domain that lapsed and was taken over: its "photos" are real but not the venue.
_HIJACKED = re.compile(r"\b(?:togel|gacor|judi|slot (?:online|gacor)|situs slot|maxwin|bandar (?:togel|judi)|casino online|online casino)\b", re.I)
# One photo on this many venues is a stock or chain picture, not a photo of each place.
SHARED_PHOTO_LIMIT = 4
# A big picture in the page body: sized wide, or named like a room / food / hero shot.
_BODY_HINT = re.compile(r"hero|banner|interior|dining|gallery|slide|patio|room|bar|food|dish|space|venue", re.I)


def _attrs(tag: str) -> dict[str, str]:
    return {m.group(1).lower(): (m.group(2) if m.group(2) is not None else m.group(3) if m.group(3) is not None else m.group(4))
            for m in _ATTR.finditer(tag)}


def _photo_url(raw: str | None, base: str) -> str | None:
    """A candidate as an absolute https URL, unless it can't be a photo."""
    raw = htmllib.unescape(raw or "").strip()
    if not raw or raw.startswith("data:"):
        return None
    url = urljoin(base, raw)
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]      # the app is https: no mixed content
    parts = urlsplit(url)
    if not url.startswith("https://") or len(url) > MAX_IMAGE_URL or _NOT_A_PHOTO.search(parts.path + "?" + parts.query):
        return None
    return url


def commons_thumb(url: str | None) -> str | None:
    """Wikidata's P18 ("http://commons.wikimedia.org/wiki/Special:FilePath/X.jpg") →
    a 640px https thumbnail. Drawings (.svg: maps, logos) are skipped."""
    if not url or "Special:FilePath/" not in url:
        return None
    name = unquote(url.split("Special:FilePath/", 1)[1])
    if name.lower().endswith((".svg", ".pdf", ".djvu", ".webm", ".ogv")):
        return None
    return f"https://commons.wikimedia.org/wiki/Special:FilePath/{quote(name)}?width=640"


def is_commons(url: str | None) -> bool:
    return bool(url) and urlsplit(url).netloc.endswith("wikimedia.org")


def own_site(url: str) -> bool:
    host = urlsplit(url).netloc.lower().split(":")[0]
    return bool(host) and not any(host == d or host.endswith("." + d) for d in _NOT_OWN_SITE)


def og_image(page: str, base: str) -> str | None:
    """The page's share image (og:image, then twitter:image), unless it looks like a logo."""
    found: dict[str, str] = {}
    for tag in _META.findall(page):
        a = _attrs(tag)
        key = (a.get("property") or a.get("name") or "").strip().lower()
        if key in _OG_KEYS and a.get("content") and key not in found:
            found[key] = a["content"]
    for key in _OG_KEYS:
        url = _photo_url(found.get(key), base)
        if url:
            return url
    return None


def _ld_images(obj, out: list[str]) -> None:
    if isinstance(obj, list):
        for x in obj:
            _ld_images(x, out)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if k == "image" or k == "photo":
                for x in (v if isinstance(v, list) else [v]):
                    if isinstance(x, str):
                        out.append(x)
                    elif isinstance(x, dict) and isinstance(x.get("url") or x.get("contentUrl"), str):
                        out.append(x.get("url") or x.get("contentUrl"))
            elif isinstance(v, (dict, list)):
                _ld_images(v, out)


def page_photos(page: str, base: str) -> list[str]:
    """Every photo the page offers for itself, best first: its share image (og/twitter),
    the photos in its schema.org data, <link rel=image_src>, then big pictures in the body."""
    out: list[str] = []
    def add(raw):
        url = _photo_url(raw, base)
        if url and url not in out:
            out.append(url)
    found: dict[str, list[str]] = {}
    for tag in _META.findall(page):
        a = _attrs(tag)
        key = (a.get("property") or a.get("name") or "").strip().lower()
        if key in _OG_KEYS and a.get("content"):
            found.setdefault(key, []).append(a["content"])
    for key in _OG_KEYS:
        for raw in found.get(key, []):
            add(raw)
    for blob in _LD.findall(page):
        imgs: list[str] = []
        try:
            _ld_images(json.loads(htmllib.unescape(blob.strip())), imgs)
        except (ValueError, RecursionError):
            continue
        for raw in imgs:
            add(raw)
    for tag in _LINK.findall(page):
        a = _attrs(tag)
        if "image_src" in (a.get("rel") or "").lower().split():
            add(a.get("href"))
    body = 0
    for tag in _IMG.findall(page)[:120]:
        a = _attrs(tag)
        srcs = [a.get("src"), a.get("data-src"), a.get("data-lazy-src")]
        srcset = a.get("srcset") or a.get("data-srcset")
        if srcset:      # the widest entry ("…/room-1600.jpg 1600w")
            parts = [p.strip().split() for p in srcset.split(",") if p.strip()]
            parts.sort(key=lambda p: int(re.sub(r"\D", "", p[1]) or 0) if len(p) > 1 else 0, reverse=True)
            if parts and parts[0]:
                srcs.insert(0, parts[0][0])
        try:
            width = int(re.sub(r"\D", "", a.get("width") or "") or 0)
        except ValueError:
            width = 0
        src = next((s for s in srcs if s and not s.startswith("data:")), None)
        if src and (width >= 500 or (not width and _BODY_HINT.search(src))):
            before = len(out)
            add(src)
            body += len(out) > before
            if body >= 6:
                break
    return out


def photo_stats(img) -> dict:
    """Size, how much is see-through, how many colours, and the share of the commonest one
    (on a 64×64 thumbnail, colours bucketed 16 per channel)."""
    w, h = img.size
    if img.mode == "P":
        img = img.convert("RGBA" if "transparency" in img.info else "RGB")
    alpha = 0.0
    if img.mode in ("RGBA", "LA"):
        a = img.convert("RGBA").getchannel("A").resize((64, 64))
        alpha = sum(1 for p in a.getdata() if p < 250) / 4096
    bins: dict[tuple, int] = {}
    for r, g, b in img.convert("RGB").resize((64, 64)).getdata():
        k = (r >> 4, g >> 4, b >> 4)
        bins[k] = bins.get(k, 0) + 1
    return {"w": w, "h": h, "alpha": alpha, "colors": len(bins), "top": max(bins.values()) / 4096}


def looks_like_photo(s: dict) -> bool:
    """A photo of a place, not a logo, text card or cut-out. Tuned on Toronto venues' own
    share images: logos sit on one flat colour, cut-outs are see-through, text cards use
    a handful of colours; photos have neither (dark rooms included)."""
    if s["w"] < 400 or s["h"] < 250:
        return False                                  # too small to show well
    if not 0.5 <= s["w"] / s["h"] <= 2.6:
        return False                                  # banners and strips
    if s["alpha"] > 0.25:
        return False                                  # a cut-out or transparent logo
    if s["top"] > 0.40:
        return False                                  # mostly one flat colour
    if s["top"] > 0.25 and s["colors"] < 300:
        return False                                  # a graphic: a flat field plus a few colours
    return s["colors"] >= 40


def check_photo(client: httpx.Client, url: str) -> bool | None:
    """True = a real photo, False = not a photo (or not an image), None = couldn't reach it."""
    try:
        with client.stream("GET", url, headers={"Accept": "image/*"}) as r:
            if r.status_code >= 500 or r.status_code == 429:
                return None
            if r.status_code >= 400 or not r.headers.get("content-type", "").lower().startswith("image/"):
                return False
            buf = bytearray()
            for chunk in r.iter_bytes():
                buf += chunk
                if len(buf) > _PHOTO_BYTES:
                    return False
        from PIL import Image    # loader-only use; the API has it too (requirements.txt)
        img = Image.open(io.BytesIO(bytes(buf)))
        if img.width * img.height > _MAX_PIXELS:
            return False
        img.draft("RGB", (800, 800))
        img.load()
        return looks_like_photo(photo_stats(img))
    except httpx.HTTPError:
        return None
    except Exception:            # not a picture Pillow can read (or a broken one)
        return False


def fetch_og_image(client: httpx.Client, site: str) -> str | None:
    """The website's own photo of the venue, checked to be a real photo.
    "" = looked, nothing usable (or not the venue's own site); a URL = found;
    None = couldn't reach the site this time (try again next month)."""
    url = site if re.match(r"https?://", site, re.I) else "https://" + site
    if not own_site(url):
        return ""
    try:
        with client.stream("GET", url) as r:
            if r.status_code >= 500 or r.status_code == 429:
                return None
            if r.status_code >= 400 or "html" not in r.headers.get("content-type", "").lower():
                return ""
            base, enc = str(r.url), r.encoding or "utf-8"
            chunks = r.iter_bytes()
            buf = b""
            for chunk in chunks:            # the <head> first: its share image is usually the one
                buf += chunk
                if len(buf) >= _PAGE_BYTES or b"</head>" in buf.lower():
                    break
            head = buf.decode(enc, errors="replace")
            if _HIJACKED.search(head):
                return ""                   # not the venue's site any more
            tried: list[str] = []
            for cand in page_photos(head, base):
                if len(tried) >= _TRIES_PER_SITE:
                    break
                tried.append(cand)
                if check_photo(client, cand):
                    return cand
            for chunk in chunks:            # nothing real there: the body's pictures next
                buf += chunk
                if len(buf) >= _PAGE_BYTES:
                    break
            for cand in page_photos(buf.decode(enc, errors="replace"), base):
                if cand in tried:
                    continue
                if len(tried) >= _TRIES_PER_SITE:
                    break
                tried.append(cand)
                if check_photo(client, cand):
                    return cand
            return ""
    except (httpx.HTTPError, UnicodeError, ValueError):
        return None


def _client(workers: int, accept: str) -> httpx.Client:
    return httpx.Client(follow_redirects=True, timeout=httpx.Timeout(8.0, connect=5.0),
                        limits=httpx.Limits(max_connections=workers * 2),
                        headers={"User-Agent": UA, "Accept": accept})


def website_images(sites: list[str], budget_s: float = IMAGE_BUDGET_S) -> dict[str, str | None]:
    """A checked photo for each site, best first, until the time budget runs out."""
    stop = time.monotonic() + budget_s
    out: dict[str, str | None] = {}
    with _client(IMAGE_WORKERS, "text/html,application/xhtml+xml") as client:
        def one(site):
            if time.monotonic() > stop:
                return site, None
            return site, fetch_og_image(client, site)
        with ThreadPoolExecutor(IMAGE_WORKERS) as pool:
            for site, img in pool.map(one, sites):
                out[site] = img
    return out


def recheck_photos(images: dict[str, str], budget_s: float = IMAGE_BUDGET_S) -> set[str]:
    """Stored website photos that turn out not to be photos (or are gone): their ids."""
    stop = time.monotonic() + budget_s
    bad: set[str] = set()
    by_url: dict[str, list[str]] = {}
    for vid, url in images.items():
        by_url.setdefault(url, []).append(vid)
    with _client(IMAGE_WORKERS, "image/*") as client:
        def one(url):
            return url, (None if time.monotonic() > stop else check_photo(client, url))
        with ThreadPoolExecutor(IMAGE_WORKERS) as pool:
            for url, ok in pool.map(one, list(by_url)):
                if ok is False:
                    bad.update(by_url[url])
    return bad


# ---- Commons: geotagged photos taken at the venue, whose title names it
_NAME_FILLER = {"the", "a", "an", "and", "of", "at", "on", "in", "inc", "ltd", "co", "toronto", "ontario", "canada"}
_NOT_OF_A_PLACE = re.compile(r"\b(?:map|logo|plan|diagram|flag|coat of arms|seal|chart|poster|screenshot)\b")
_COMMONS_API = "https://commons.wikimedia.org/w/api.php"


def _plain(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower().replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def name_phrase(name: str) -> str | None:
    """The venue's name as it would appear in a photo title ("The Rex Hotel" → "rex hotel");
    None when it's too short or too generic to match safely."""
    words = _plain(name).split()
    while words and words[0] in _NAME_FILLER:
        words = words[1:]
    phrase = " ".join(words)
    if len(phrase) < 3 or all(w in _NAME_FILLER or w.isdigit() for w in words):
        return None
    return phrase


def commons_match(name: str, titles: list[str]) -> str | None:
    """The first photo title that names the venue as a whole phrase ("Inner Wave at the
    Velvet Underground.jpg" for "Velvet Underground"; not "Barrio Cerveceria" for
    "Barrio Lounge"), skipping maps, logos and drawings."""
    phrase = name_phrase(name)
    if not phrase:
        return None
    pat = re.compile(r"(?:^| )" + re.escape(phrase) + r"(?: |$)")
    for title in titles:
        base = title.removeprefix("File:")
        if base.lower().endswith((".svg", ".pdf", ".djvu", ".webm", ".ogv", ".tif", ".tiff")):
            continue
        t = _plain(base.rsplit(".", 1)[0])
        if _NOT_OF_A_PLACE.search(t):
            continue
        if pat.search(t):
            return base
    return None


def commons_photo(client: httpx.Client, name: str, lat: float, lng: float) -> str | None:
    """"" = nothing found; a thumbnail URL = found (and checked); None = Commons unreachable."""
    if not name_phrase(name):
        return ""
    try:
        r = client.get(_COMMONS_API, params={
            "action": "query", "format": "json", "maxlag": 5, "generator": "geosearch",
            "ggscoord": f"{lat}|{lng}", "ggsradius": COMMONS_RADIUS_M, "ggsnamespace": 6, "ggslimit": 30,
            "prop": "imageinfo", "iiprop": "size|mime"})
        if r.status_code >= 400:
            return None
        pages = ((r.json().get("query") or {}).get("pages") or {}).values()
    except (httpx.HTTPError, ValueError):
        return None
    ok = [p["title"] for p in sorted(pages, key=lambda p: p.get("index", 99))
          if (p.get("imageinfo") or [{}])[0].get("mime") in ("image/jpeg", "image/png", "image/webp")
          and (p.get("imageinfo") or [{}])[0].get("width", 0) >= 500]
    base = commons_match(name, ok)
    if not base:
        return ""
    url = f"https://commons.wikimedia.org/wiki/Special:FilePath/{quote(base)}?width=640"
    return url if check_photo(client, url) else ""


def commons_photos(venues: list[dict], budget_s: float = COMMONS_BUDGET_S) -> dict[str, str]:
    """Commons photos for venues still without one, best first, until the budget runs out.
    Within each priority tier the order rotates by month, so the whole list gets covered
    over a few runs rather than the same venues being asked about every time."""
    month = time.strftime("%Y%m")
    todo = sorted((v for v in venues if not v.get("image") and v.get("lat") is not None and name_phrase(v.get("name") or "")),
                  key=lambda v: (_photo_priority(v)[0], -(v["notability"] > 0),
                                 hashlib.md5(f"{v['id']}{month}".encode()).hexdigest()))
    stop = time.monotonic() + budget_s
    out: dict[str, str] = {}
    with _client(COMMONS_WORKERS, "application/json") as client:
        def one(v):
            if time.monotonic() > stop:
                return v["id"], None
            return v["id"], commons_photo(client, v["name"], v["lat"], v["lng"])
        with ThreadPoolExecutor(COMMONS_WORKERS) as pool:
            for vid, url in pool.map(one, todo):
                if url:
                    out[vid] = url
    log(f"  Commons: {len(out)} more photos taken at the venue ({len(todo)} venues without one)")
    return out


def _photo_priority(v: dict) -> tuple:
    """Night spots first (the map shows them), then food and drink, then the rest; famous first."""
    slots = set(v["slots"])
    return (0 if slots & {"drinks", "leisure"} else 1 if slots & {"dinner", "dessert", "cafe", "lunch"} else 2,
            -v["notability"])


def sites_to_fetch(venues: list[dict], prev: dict[str, tuple]) -> list[str]:
    """Websites with no result from an earlier run, deduped, best first. A Commons photo
    from before means the website gave nothing usable *or* wasn't reached: look again."""
    todo, seen = [], set()
    for v in sorted(venues, key=_photo_priority):
        site = v.get("website")
        if not site or site in seen:
            continue
        old = prev.get(v["id"])
        if old and old[0] == site and old[1] is not None and not is_commons(old[1]):
            continue
        seen.add(site)
        todo.append(site)
    return todo


def assign_images(venues: list[dict], prev: dict[str, tuple], fetched: dict[str, str | None]) -> None:
    """v["image"] = the website's photo, else the Commons photo; "" when there's
    nothing to show, None when the website is still to be looked at."""
    for v in venues:
        wd = v.pop("_wd_image", None)
        site, site_img = v.get("website"), None
        if site:
            if fetched.get(site) is not None:
                site_img = fetched[site]
            else:
                old = prev.get(v["id"])
                if old and old[0] == site and old[1] is not None and not is_commons(old[1]):
                    site_img = old[1]
        if site_img:
            v["image"] = site_img
        elif wd:
            v["image"] = wd
        else:
            v["image"] = "" if (site_img == "" or not site) else None


def drop_shared_photos(venues: list[dict], limit: int = SHARED_PHOTO_LIMIT) -> int:
    """A photo used by `limit` or more venues (a city page's skyline on every park, a
    chain's brand shot) isn't a photo of each place: "" for all of them. Returns how many."""
    uses: dict[str, int] = {}
    for v in venues:
        if v.get("image"):
            uses[v["image"]] = uses.get(v["image"], 0) + 1
    shared = {u for u, n in uses.items() if n >= limit}
    n = 0
    for v in venues:
        if v.get("image") in shared:
            v["image"], n = "", n + 1
    return n


def add_commons(venues: list[dict], prev: dict[str, tuple], budget_s: float) -> None:
    """After the websites: a Commons photo for venues still without one. Last month's
    Commons find is kept for venues not asked about this time."""
    for v in venues:
        old = prev.get(v["id"])
        if not v.get("image") and old and is_commons(old[1]):
            v["image"] = old[1]
    found = commons_photos(venues, budget_s) if budget_s > 0 else {}
    for v in venues:
        if v["id"] in found:
            v["image"] = found[v["id"]]


def apply_recheck(rows: list[tuple], prev: dict[str, tuple], budget_s: float) -> None:
    """--recheck-photos: stored website photos that aren't photos are dropped (prev → None),
    so this run looks at those websites again with today's checks."""
    stored = {vid: img for vid, (site, img) in ((r[0], r[1:]) for r in rows) if img and not is_commons(img)}
    t = time.time()
    bad = recheck_photos(stored, budget_s)
    for vid in bad:
        prev[vid] = (prev[vid][0], None) if vid in prev else (None, None)
    log(f"  recheck: {len(bad)} of {len(stored)} stored website photos weren't photos ({time.time() - t:.0f}s)")


def wikidata_photos(qids: list[str]) -> dict[str, str]:
    """Commons photos (P18) for known Wikidata items, a few hundred per query."""
    out: dict[str, str] = {}
    for i in range(0, len(qids), 300):
        values = " ".join(f"wd:{q}" for q in qids[i:i + 300] if re.fullmatch(r"Q\d+", q))
        if not values:
            continue
        q = f"SELECT ?item (SAMPLE(?img) AS ?image) WHERE {{ VALUES ?item {{ {values} }} ?item wdt:P18 ?img . }} GROUP BY ?item"
        try:
            r = httpx.get("https://query.wikidata.org/sparql", params={"query": q}, timeout=120,
                          headers={"Accept": "application/sparql-results+json", "User-Agent": UA})
            r.raise_for_status()
        except httpx.HTTPError as e:
            log(f"  Wikidata photos unavailable ({e}); skipping the rest")
            break
        for b in r.json()["results"]["bindings"]:
            thumb = commons_thumb(b.get("image", {}).get("value"))
            if thumb:
                out[b["item"]["value"].rsplit("/", 1)[-1]] = thumb
    return out


def _photo_log(todo: list[str], fetched: dict[str, str | None], t: float, later: str) -> None:
    reached = sum(1 for x in fetched.values() if x is not None)
    log(f"  photos: {len(todo)} websites to look at, {reached} looked at, "
        f"{sum(1 for x in fetched.values() if x)} with a real photo ({time.time() - t:.0f}s)"
        + (f"; {len(todo) - reached} left for {later}" if reached < len(todo) else ""))


def images_only(city: str, budget_s: float, refresh: bool = False, recheck: bool = False,
                commons_s: float = COMMONS_BUDGET_S) -> int:
    """Fill in photos for the venues already stored, without reloading anything else."""
    with SessionLocal() as db:
        rows = db.execute(select(Venue.id, Venue.website, Venue.wikidata, Venue.slots, Venue.notability, Venue.image,
                                 Venue.name, Venue.lat, Venue.lng)
                          .where(Venue.city == city)).all()
    if not rows:
        log(f"No stored venues for {city}: run the full load first.")
        return 1
    wd = wikidata_photos(sorted({r.wikidata for r in rows if r.wikidata}))
    log(f"  Wikidata: {len(wd)} Commons photos for {sum(1 for r in rows if r.wikidata)} known places")
    venues = [{"id": r.id, "website": r.website, "slots": r.slots or [], "notability": r.notability or 0,
               "name": r.name, "lat": r.lat, "lng": r.lng, "_wd_image": wd.get(r.wikidata)} for r in rows]
    prev = {} if refresh else {r.id: (r.website, r.image) for r in rows}
    if recheck and not refresh:
        apply_recheck([(r.id, r.website, r.image) for r in rows], prev, budget_s)
    todo = sites_to_fetch(venues, prev)
    t = time.time()
    fetched = website_images(todo, budget_s) if todo else {}
    _photo_log(todo, fetched, t, "next time")
    assign_images(venues, prev, fetched)
    add_commons(venues, prev, commons_s)
    log(f"  {drop_shared_photos(venues)} venues dropped a photo shared by {SHARED_PHOTO_LIMIT}+ places (not of them)")
    # Write only the rows whose photo changed: every rewritten row leaves a dead copy
    # behind until vacuum, and on Neon the change history counts toward storage.
    before = {r.id: r.image for r in rows}
    changed = [{"id": v["id"], "image": v["image"]} for v in venues if v["image"] != before.get(v["id"])]
    with SessionLocal() as db:
        for i in range(0, len(changed), 1000):
            db.bulk_update_mappings(Venue, changed[i:i + 1000])
        db.commit()
    log(f"Updated {len(changed)} of {len(venues)} venues; {sum(1 for v in venues if v['image'])} have photos "
        f"({sum(1 for v in venues if is_commons(v['image']))} from Wikimedia Commons).")
    return 0


def previous_images(city: str) -> dict[str, tuple]:
    """Last load's {venue id: (website, image)}, so unchanged sites aren't fetched again."""
    with SessionLocal() as db:
        return {i: (w, img) for i, w, img in db.execute(
            select(Venue.id, Venue.website, Venue.image).where(Venue.city == city)).all()}


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
        p["notability"], p["wikidata"], p["hours"], p["drop"], p["_wd_image"] = 0, None, None, False, None
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
            row["notability"], row["wikidata"], row["_wd_image"] = it["links"], it["qid"], it.get("image")

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
                        "wikidata": it["qid"], "confidence": 1.0, "_wd_image": it.get("image")})
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
            "confidence": float(p["confidence"] or 0), "_wd_image": p["_wd_image"]})
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
    ap.add_argument("--skip-images", action="store_true", help="don't fetch website photos (keeps earlier ones)")
    ap.add_argument("--refresh-images", action="store_true", help="look at every website again")
    ap.add_argument("--image-budget", type=float, default=IMAGE_BUDGET_S, help="seconds for the website photo step")
    ap.add_argument("--commons-budget", type=float, default=COMMONS_BUDGET_S,
                    help="seconds for finding Wikimedia Commons photos taken at venues still without one (0 = skip)")
    ap.add_argument("--recheck-photos", action="store_true",
                    help="download every stored website photo again and drop the ones that aren't real photos")
    ap.add_argument("--images-only", action="store_true",
                    help="only fill in photos for the venues already stored (no Overture/OSM reload)")
    args = ap.parse_args(argv)

    # Fail before the slow downloads, not at the write, if migrations haven't run.
    try:
        has_image_col = "image" in {c["name"] for c in inspect(engine).get_columns("venues")}
    except Exception as e:      # no database reachable (fine for a dry run)
        has_image_col = None
        log(f"  database not reachable ({type(e).__name__}); earlier photos can't be reused")
    if has_image_col is False and not args.dry_run:
        log("The venues table has no `image` column: run `alembic upgrade head` (0015_venue_images) first.")
        return 1
    if args.images_only:
        if not has_image_col:
            log("--images-only needs the database (with migration 0015_venue_images).")
            return 1
        log(f"Filling in photos for {args.city} …")
        return images_only(args.city, args.image_budget, args.refresh_images, args.recheck_photos, args.commons_budget)
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
    prev = {} if (args.refresh_images or not has_image_col) else previous_images(args.city)
    if args.recheck_photos and prev and not args.skip_images:
        apply_recheck([(vid, site, img) for vid, (site, img) in prev.items()], prev, args.image_budget)
    todo = [] if args.skip_images else sites_to_fetch(venues, prev)
    t = time.time()
    fetched = website_images(todo, args.image_budget) if todo else {}
    _photo_log(todo, fetched, t, "next month")
    assign_images(venues, prev, fetched)
    add_commons(venues, prev, 0 if args.skip_images else args.commons_budget)
    log(f"  {drop_shared_photos(venues)} venues dropped a photo shared by {SHARED_PHOTO_LIMIT}+ places (not of them)")
    log(f"  with photos: {sum(1 for v in venues if v['image'])} "
        f"({sum(1 for v in venues if is_commons(v['image']))} from Wikimedia Commons)")
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
