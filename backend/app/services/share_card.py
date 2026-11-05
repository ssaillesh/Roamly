"""The "Share the night" story card: a 1080x1920 JPEG of a shared plan.

Layout follows the Rova design (screen 4f, drawn at 3x): a photo collage of the
first stops on top, then on charcoal the day and area, the plan's title (Inter
SemiBold with an Instrument Serif italic tail), the stops with their times, and
the wordmark with the link back to the plan.

Stop photos are fetched from the web, from URLs that came from the browser, so
fetching is locked down: https only, every hop's host must resolve to public
addresses only (no localhost / private / link-local ranges), redirects are
followed by hand and re-checked, and size, time and pixel count are capped.
"""
from __future__ import annotations

import io
import ipaddress
import logging
import socket
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from functools import lru_cache
from pathlib import Path
from threading import Lock
from urllib.parse import urljoin, urlparse

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageOps

log = logging.getLogger(__name__)

W, H = 1080, 1920
COLLAGE_H, GAP, PAD = 990, 9, 72
CHARCOAL = (0x14, 0x13, 0x11)
SURFACE = (0x1C, 0x1B, 0x18)
IVORY = (0xF2, 0xED, 0xE4)
MUTED = (0x8A, 0x84, 0x78)
CHAMPAGNE = (0xD4, 0xB4, 0x7A)
RULE = tuple(round(i * 0.12 + c * 0.88) for i, c in zip(IVORY, CHARCOAL))

FONTS = Path(__file__).resolve().parent.parent / "assets" / "fonts"
SANS, SANS_SB, SERIF_I = "Inter-Regular.ttf", "Inter-SemiBold.ttf", "InstrumentSerif-Italic.ttf"

MAX_BYTES = 8_000_000
MAX_PIXELS = 40_000_000
UA = "Rova/1.0 (share card renderer)"


@lru_cache(maxsize=None)
def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / name), size)


# ---------------------------------------------------------------- photos
def _public_host(host: str) -> bool:
    """Every address the host resolves to must be a public one."""
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        return False
    if not infos:
        return False
    for *_, sockaddr in infos:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            return False
        if not ip.is_global or ip.is_multicast:
            return False
    return True


def fetch_image(url: str | None, timeout: float = 4.0) -> Image.Image | None:
    """Download a photo for the card, or None (any failure just leaves a blank tile)."""
    if not url:
        return None
    try:
        for _ in range(4):                      # the URL plus up to 3 redirects
            p = urlparse(url)
            if p.scheme != "https" or not p.hostname or not _public_host(p.hostname):
                return None
            with httpx.stream("GET", url, timeout=timeout, follow_redirects=False,
                              headers={"User-Agent": UA, "Accept": "image/*"}) as r:
                if r.is_redirect:
                    loc = r.headers.get("location")
                    if not loc:
                        return None
                    url = urljoin(url, loc)
                    continue
                if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/"):
                    return None
                buf = bytearray()
                for chunk in r.iter_bytes():
                    buf += chunk
                    if len(buf) > MAX_BYTES:
                        return None
            img = Image.open(io.BytesIO(bytes(buf)))
            if img.width * img.height > MAX_PIXELS:
                return None
            img.draft("RGB", (1600, 1600))      # JPEG: decode at a smaller scale, faster
            return img.convert("RGB")
        return None
    except Exception as e:                      # bad image, timeout, TLS, decoder bomb…
        log.info("share card: photo skipped (%s)", type(e).__name__)
        return None


# ---------------------------------------------------------------- type
def _tracked_width(text: str, font, tracking: float) -> float:
    return sum(font.getlength(ch) + tracking for ch in text) - (tracking if text else 0)


def _draw_tracked(draw: ImageDraw.ImageDraw, xy, text: str, font, fill, tracking: float = 0.0) -> float:
    """Draw text with letter-spacing (Pillow has none); returns the advance."""
    x, y = xy
    if not tracking:
        draw.text((x, y), text, font=font, fill=fill)
        return font.getlength(text)
    start = x
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += font.getlength(ch) + tracking
    return x - start - tracking


def split_title(title: str) -> tuple[str, str]:
    """'Tapas, jazz and a last drink' -> ('Tapas, jazz', 'and a last drink').
    The tail is set in the serif italic, as in the design."""
    t = " ".join(title.split())
    words = t.split(" ")
    if len(words) < 2:
        return t, ""
    for kw in (" and ", " with ", " by ", " in ", " at "):
        i = t.rfind(kw)
        if i > 0:
            return t[:i], t[i + 1:]
    k = 1 if len(words) <= 3 else 2
    return " ".join(words[:-k]), " ".join(words[-k:])


def _layout_title(head: str, tail: str, size: int, width: int):
    """Greedy word wrap over two typefaces. Returns lines of [(word, font, tracking)]."""
    sans, serif = _font(SANS_SB, size), _font(SERIF_I, round(size * 1.1))
    words = [(w, sans, -0.04 * size) for w in head.split()] + [(w, serif, -0.02 * size) for w in tail.split()]
    lines, cur, cur_w = [], [], 0.0
    for w, f, tr in words:
        ww = _tracked_width(w, f, tr)
        sp = f.getlength(" ") * 0.9
        if cur and cur_w + sp + ww > width:
            lines.append(cur)
            cur, cur_w = [], 0.0
        cur_w += (sp if cur else 0) + ww
        cur.append((w, f, tr))
    if cur:
        lines.append(cur)
    return lines


# ---------------------------------------------------------------- the mark
def _mark(height: int, ink=IVORY, accent=CHAMPAGNE) -> Image.Image:
    """The route mark (design viewBox 5 5 38 39), drawn 4x and scaled down for smooth edges."""
    s = 4 * height / 39
    w = round(38 * s)
    im = Image.new("RGBA", (w, round(39 * s)), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    P = lambda x, y: ((x - 5) * s, (y - 5) * s)

    def line(a, b, width, color):
        d.line([P(*a), P(*b)], fill=color, width=round(width * s))
        r = width * s / 2
        for x, y in (P(*a), P(*b)):
            d.ellipse([x - r, y - r, x + r, y + r], fill=color)

    def ring(cx, cy, r, sw, color):
        x, y = P(cx, cy)
        ro, ri = (r + sw / 2) * s, (r - sw / 2) * s
        d.ellipse([x - ro, y - ro, x + ro, y + ro], fill=color)
        d.ellipse([x - ri, y - ri, x + ri, y + ri], fill=(0, 0, 0, 0))

    line((8, 8), (24, 40), 2.6, accent)
    # punch the line out inside the rings, as the design's ivory-filled circles do
    for cx, cy in ((12.4, 16.8), (17.2, 26.4)):
        x, y = P(cx, cy)
        r = 2.4 * s
        d.ellipse([x - r, y - r, x + r, y + r], fill=(0, 0, 0, 0))
        ring(cx, cy, 3.6, 2.4, accent)
    line((40, 8), (24, 40), 5.6, ink)
    x, y = P(24, 38.5)
    d.ellipse([x - 5 * s, y - 5 * s, x + 5 * s, y + 5 * s], fill=accent)
    return im.resize((max(1, w // 4), height), Image.LANCZOS)


def _wordmark(img: Image.Image, draw: ImageDraw.ImageDraw, x: float, y: float, size: int) -> None:
    """R O [mark] A, with the design's spacing (letter-spacing .02em, gaps .12/.1/.08em)."""
    f = _font(SANS_SB, size)
    tr = 0.02 * size
    asc, _ = f.getmetrics()
    x += _draw_tracked(draw, (x, y), "R", f, IVORY) + tr + 0.12 * size
    x += _draw_tracked(draw, (x, y), "O", f, IVORY) + tr + 0.10 * size
    mh = round(0.76 * size)
    mark = _mark(mh)
    cap_top = y + asc - 0.727 * size          # Inter's cap height is .727em
    img.paste(mark, (round(x + (0.78 * size - mark.width) / 2), round(cap_top - 0.02 * size)), mark)
    x += 0.78 * size + 0.08 * size
    draw.text((x, y), "A", font=f, fill=IVORY)


# ---------------------------------------------------------------- the card
def _tile(img: Image.Image, photo: Image.Image | None, box: tuple[int, int, int, int]) -> None:
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    if photo is not None:
        img.paste(ImageOps.fit(photo, (w, h), Image.LANCZOS), (x0, y0))
        return
    ImageDraw.Draw(img).rectangle(box, fill=SURFACE)
    mark = _mark(min(120, h // 3), ink=(0x3A, 0x37, 0x32), accent=(0x5A, 0x50, 0x3C))
    img.paste(mark, (x0 + (w - mark.width) // 2, y0 + (h - mark.height) // 2), mark)


def _kicker(plan: dict) -> str:
    parts = []
    if plan.get("date"):
        try:
            parts.append(date.fromisoformat(plan["date"]).strftime("%A"))
        except ValueError:
            pass
    if plan.get("area"):
        parts.append(plan["area"])
    return " · ".join(parts).upper()


def render_card(plan: dict, link_text: str, photos: list[Image.Image | None] | None = None) -> bytes:
    """Draw the card. `photos` (for the first three stops) is fetched if not given."""
    stops = plan.get("stops") or []
    if photos is None:
        urls = [s.get("image") for s in stops if s.get("image")][:3]
        with ThreadPoolExecutor(max_workers=3) as ex:
            photos = list(ex.map(fetch_image, urls))
    photos = (list(photos) + [None, None, None])[:3]

    img = Image.new("RGB", (W, H), CHARCOAL)
    draw = ImageDraw.Draw(img)

    # collage: one wide photo over two
    top_h = round((COLLAGE_H - GAP) * 2 / 3)
    half = (W - GAP) // 2
    _tile(img, photos[0], (0, 0, W, top_h))
    _tile(img, photos[1], (0, top_h + GAP, half, COLLAGE_H))
    _tile(img, photos[2], (half + GAP, top_h + GAP, W, COLLAGE_H))

    y = COLLAGE_H + 66
    k = _kicker(plan)
    if k:
        _draw_tracked(draw, (PAD, y), k, _font(SANS, 30), CHAMPAGNE, tracking=4.8)
        y += 30 + 42

    head, tail = split_title(str(plan.get("title") or "Tonight"))
    for size in (90, 78, 66):
        lines = _layout_title(head, tail, size, W - 2 * PAD)
        if len(lines) <= 3:
            break
    lines = lines[:3]
    for line in lines:
        x = PAD
        for i, (word, f, tr) in enumerate(line):
            if i:
                x += f.getlength(" ") * 0.9
            # the serif sits on the same baseline as the sans
            dy = _font(SANS_SB, size).getmetrics()[0] - f.getmetrics()[0]
            x += _draw_tracked(draw, (x, y + dy), word, f, IVORY, tracking=tr)
        y += round(size * 1.02)
    y += 42

    sf = _font(SANS, 42)
    shown = stops[:5]
    for s in shown:
        t = str(s.get("time") or "").replace(" PM", "").replace(" AM", "").replace("PM", "").replace("AM", "")
        draw.text((PAD, y), t, font=sf, fill=MUTED)
        name = str(s.get("name") or "")
        while name and sf.getlength(name) > W - 2 * PAD - 144:
            name = name[:-2].rstrip() + "…"
        draw.text((PAD + 144, y), name, font=sf, fill=IVORY)
        y += 42 + 21
    if len(stops) > len(shown):
        draw.text((PAD + 144, y), f"+{len(stops) - len(shown)} more", font=sf, fill=MUTED)

    # footer: rule, wordmark, link
    fy = H - 66 - 45
    draw.rectangle([PAD, fy - 42 - 3, W - PAD, fy - 42], fill=RULE)
    _wordmark(img, draw, PAD, fy, 45)
    lf = _font(SANS, 36)
    draw.text((W - PAD - lf.getlength(link_text), fy + 6), link_text, font=lf, fill=MUTED)

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=88, optimize=True, progressive=True)
    return out.getvalue()


# A shared plan never changes (its code is tied to its content), so finished
# cards are kept in memory; a restart just redraws them.
_cache: OrderedDict[str, bytes] = OrderedDict()
_cache_lock = Lock()
_CACHE_MAX = 64


def card_for(code: str, plan: dict, link_text: str) -> bytes:
    with _cache_lock:
        if code in _cache:
            _cache.move_to_end(code)
            return _cache[code]
    data = render_card(plan, link_text)
    with _cache_lock:
        _cache[code] = data
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    return data
