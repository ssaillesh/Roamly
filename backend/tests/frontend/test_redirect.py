"""?redirect= on the sign-in and taste pages: only a page on this site, never another
site or a script URL that could read the stored sign-in token. Real Chrome; run as
described in test_reset_password_page.py."""
from urllib.parse import quote, urlsplit

import pytest

pytest.importorskip("playwright.sync_api")

from webui_fixtures import API, browser, site  # noqa: E402,F401  (fixtures)

TOKEN = "SECRET-SESSION-TOKEN"
OK = {"access_token": TOKEN, "refresh_token": "r", "token_type": "bearer",
      "user": {"id": "1", "username": "sam", "display_name": "Sam", "is_new_user": False}}
EVIL = "evil.example"

ATTACKS = [
    "javascript:alert(localStorage.getItem('rova_token'))",
    "JaVaScRiPt:alert(localStorage.getItem('rova_token'))",
    " javascript:alert(1)",
    "java\tscript:alert(1)",
    "java\nscript:alert(1)",
    "\x00javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
    "blob:https://evil.example/x",
    f"https://{EVIL}/steal",
    f"http:{EVIL}/steal",
    f"//{EVIL}/steal",
    f"///{EVIL}/steal",
    f"/\\{EVIL}/steal",
    f"\\\\{EVIL}/steal",
    f"\\/{EVIL}/steal",
    f"/.//{EVIL}/steal",
    f"/..//{EVIL}/steal",
    f"/%2e//{EVIL}/steal",
    f"/x/..//{EVIL}/steal",
    "x" * 3000,
]
LEGIT = {
    "rova.html": "/rova.html",
    "/rova.html": "/rova.html",
    "admin.html": "/admin.html",
    "rova.html?q=date%20night#top": "/rova.html?q=date%20night#top",
    "profile.html?redirect=rova.html": "/profile.html?redirect=rova.html",
    "/": "/",
}


@pytest.fixture
def ctx(browser, site):
    """A browser context that records dialogs and every request off this site; the
    pages people land on are stubbed (they'd otherwise call the API)."""
    made = []

    def make(signed_in=False):
        c = browser.new_context()
        c.add_init_script("try{localStorage.setItem('rova_consent','no');"
                          + (f"localStorage.setItem('rova_token','{TOKEN}');" if signed_in else "") + "}catch(e){}")
        c.dialogs, c.offsite = [], []

        def route(r, req):
            if not req.url.startswith(site):
                c.offsite.append(req.url)
            if req.url.startswith(f"{API}/auth/login") or req.url.startswith(f"{API}/auth/register"):
                return r.fulfill(status=200, json=OK)
            if req.url.startswith(site):
                path = urlsplit(req.url).path
                if path in ("/rova", "/rova.html", "/admin", "/admin.html", "/profile", "/profile.html") \
                        and req.is_navigation_request() and c.stub_landing:
                    return r.fulfill(status=200, content_type="text/html", body="<title>landed</title>")
                return r.continue_()
            return r.abort()
        c.stub_landing = True
        c.route("**/*", route)
        made.append(c)
        return c
    yield make
    for c in made:
        c.close()


def _page(c):
    p = c.new_page()
    p.on("dialog", lambda d: (c.dialogs.append(d.message), d.dismiss()))
    return p


def _landed(page, site, path):
    page.wait_for_function("() => document.title === 'landed'", timeout=10000)
    url = urlsplit(page.url)
    assert f"{url.scheme}://{url.netloc}" == site
    return url.path + (f"?{url.query}" if url.query else "") + (f"#{url.fragment}" if url.fragment else "")


# ---- the check itself ---------------------------------------------------------------

def test_safe_redirect_rejects_attacks_and_keeps_real_pages(ctx, site):
    page = _page(ctx())
    page.goto(f"{site}/auth?mode=login")
    got = page.evaluate("xs => xs.map(x => safeRedirect(x))", ATTACKS)
    assert got == ["/rova.html"] * len(ATTACKS)
    assert page.evaluate("() => [safeRedirect(null), safeRedirect(undefined), safeRedirect(''), safeRedirect(42)]") \
        == ["/rova.html"] * 4
    assert page.evaluate("xs => xs.map(x => safeRedirect(x))", list(LEGIT)) == list(LEGIT.values())
    assert page.evaluate("() => safeRedirect('javascript:x', '/profile.html')") == "/profile.html"


# ---- a signed-in visitor opening a crafted link ---------------------------------------

@pytest.mark.parametrize("attack", ATTACKS[:-1])
def test_signed_in_visitor_is_never_sent_off_site_or_to_a_script(ctx, site, attack):
    c = ctx(signed_in=True)
    page = _page(c)
    page.goto(f"{site}/auth?redirect={quote(attack, safe='')}")
    assert _landed(page, site, "/rova.html") == "/rova.html"
    assert c.dialogs == []
    assert not [u for u in c.offsite if EVIL in u]


@pytest.mark.parametrize("value, path", [("admin.html", "/admin.html"), ("rova.html?q=date%20night", "/rova.html?q=date%20night"),
                                         ("/rova.html#saved", "/rova.html#saved")])
def test_signed_in_visitor_still_goes_where_the_link_meant(ctx, site, value, path):
    page = _page(ctx(signed_in=True))
    page.goto(f"{site}/auth?redirect={quote(value, safe='')}")
    assert _landed(page, site, path) == path


# ---- signing in / signing up from a crafted link --------------------------------------------

def _sign_in(page, site, redirect, mode="login"):
    page.goto(f"{site}/auth?mode={mode}&redirect={quote(redirect, safe='')}")
    if mode == "register":
        page.fill("#displayName", "Sam")
        page.check("#agree")
    page.fill("#email", "sam@example.com")
    page.fill("#password", "password123")
    page.click("#submitBtn")


@pytest.mark.parametrize("attack", ["javascript:alert(localStorage.getItem('rova_token'))",
                                    f"//{EVIL}/steal", f"https://{EVIL}/steal", f"/.//{EVIL}/steal"])
def test_signing_in_from_a_crafted_link_is_safe(ctx, site, attack):
    c = ctx()
    page = _page(c)
    _sign_in(page, site, attack)
    assert _landed(page, site, "/rova.html") == "/rova.html"
    assert c.dialogs == [] and not [u for u in c.offsite if EVIL in u]


def test_signing_in_returns_to_the_page_that_asked(ctx, site):
    page = _page(ctx())
    _sign_in(page, site, "admin.html")
    assert _landed(page, site, "/admin.html") == "/admin.html"


def test_signing_up_hands_only_a_safe_page_to_the_taste_survey(ctx, site):
    c = ctx()
    page = _page(c)
    _sign_in(page, site, "javascript:alert(document.domain)", mode="register")
    assert _landed(page, site, "") == "/profile.html?redirect=%2Frova.html"
    assert c.dialogs == []


# ---- the taste page's own ?redirect= -------------------------------------------------------

@pytest.mark.parametrize("value, expect", [("javascript:alert(localStorage.getItem('rova_token'))", "/rova.html"),
                                           (f"//{EVIL}/steal", "/rova.html"),
                                           ("rova.html?q=brunch", "/rova.html?q=brunch")])
def test_taste_page_only_links_back_to_this_site(ctx, site, value, expect):
    c = ctx(signed_in=True)
    c.stub_landing = False                       # load the real taste page (its API calls just fail)
    page = _page(c)
    page.goto(f"{site}/profile.html?redirect={quote(value, safe='')}")
    page.wait_for_load_state("load")
    assert page.evaluate("() => REDIRECT") == expect
    hrefs = page.evaluate("() => [...document.querySelectorAll('a[href]')].map(a => a.getAttribute('href'))")
    assert not [h for h in hrefs if h.lower().lstrip().startswith(("javascript:", "data:", "//"))]
    assert c.dialogs == []
