"""The password-reset page (webui/auth.html opened from the emailed link) in a real
browser, with the API mocked. Needs Playwright and Google Chrome, which the backend
venv doesn't have, so it is skipped there. Run it with a Python that has both:

    python3 -B -m pytest --noconftest -p no:cacheprovider backend/tests/frontend -q
"""
import json

import pytest

pytest.importorskip("playwright.sync_api")

from webui_fixtures import API, browser, site  # noqa: E402,F401  (fixtures)

RESET = f"{API}/auth/reset-password"
TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4IiwidHlwZSI6InJlc2V0In0.c2lnbmF0dXJlLXBhcnQ"
OK_BODY = {"access_token": "new-access", "refresh_token": "new-refresh", "token_type": "bearer",
           "user": {"id": "1", "username": "sam", "display_name": "Sam", "is_new_user": False}}


class Page:
    """A page on /auth with the API mocked: every request is recorded, nothing leaves localhost."""

    def __init__(self, browser, site, *, signed_in=False, consent="yes"):
        self.ctx = browser.new_context(viewport={"width": 390, "height": 844})
        self.ctx.add_init_script(
            f"try{{localStorage.setItem('rova_consent','{consent}');"
            + ("localStorage.setItem('rova_token','old-session');" if signed_in else "") + "}catch(e){}")
        self.requests, self.reset_calls, self.held = [], [], []
        self.reply = (200, OK_BODY)
        self.hold = False
        self.ctx.route("**/*", self._route)
        self.page = self.ctx.new_page()
        self.site = site

    def _route(self, route, request):
        if not request.is_navigation_request():        # opening the emailed link itself is expected
            self.requests.append((request.url, request.headers.get("referer", "")))
        if request.url.startswith(self.site):
            return route.continue_()
        if request.url == RESET and request.method == "POST":
            self.reset_calls.append(json.loads(request.post_data))
            if self.hold:
                return self.held.append(route)
            return self._answer(route)
        if request.url.startswith(API):
            return route.fulfill(status=404, json={"detail": "Not Found"})
        return route.abort()                       # fonts, analytics, the /health wake-up ping

    def _answer(self, route):
        status, body = self.reply
        if status is None:
            return route.abort("internetdisconnected")
        route.fulfill(status=status, json=body)

    def open(self, query):
        self.page.goto(f"{self.site}/auth{query}")
        self.page.wait_for_load_state("load")
        return self.page

    def fill(self, pw="new-password-1", confirm=None):
        self.page.fill("#rpPassword", pw)
        self.page.fill("#rpConfirm", pw if confirm is None else confirm)

    def close(self):
        self.ctx.close()


@pytest.fixture
def ui(browser, site):
    pages = []

    def make(**kw):
        pages.append(Page(browser, site, **kw))
        return pages[-1]
    yield make
    for p in pages:
        p.close()


def _visible(page, sel):
    return page.locator(sel).is_visible()


def _on(page, sel):
    """The success overlay is always in the layout (opacity 0); .on is what shows it."""
    return page.evaluate("s => document.querySelector(s).classList.contains('on')", sel)


def test_link_opens_the_reset_form_and_hides_the_token(ui):
    p = ui()
    page = p.open(f"?reset_token={TOKEN}")
    assert _visible(page, "#rpPanel") and _visible(page, "#rpForm")
    assert not _visible(page, "#mainPanel")
    # Out of the address bar before analytics loaded, and kept nowhere else.
    assert "reset_token" not in page.url and TOKEN not in page.url
    assert page.evaluate("() => window.__resetToken") is None
    storage = page.evaluate("() => JSON.stringify(localStorage) + JSON.stringify(sessionStorage)")
    assert TOKEN not in storage
    assert page.evaluate("() => document.querySelector('meta[name=referrer]').content") == "no-referrer"
    assert p.requests                                           # scripts, styles, the wake-up ping…
    for url, referer in p.requests:
        assert TOKEN not in url and TOKEN not in referer


@pytest.mark.parametrize("query", ["?reset_token=", "?reset_token=not-a-token", "?reset_token=a.b",
                                   "?reset_token=" + "a" * 3000 + ".b.c", "?reset_token=a.b.c%3Cscript%3E"])
def test_missing_or_malformed_token_shows_an_error_and_sends_nothing(ui, query):
    p = ui()
    page = p.open(query)
    assert _visible(page, "#rpPanel")
    assert "incomplete" in page.inner_text("#rpMsg")
    assert not _visible(page, "#rpForm")
    assert _visible(page, "#rpNewLink")
    page.click("#rpNewLink")                                   # → the "forgot password" panel
    assert _visible(page, "#fpPanel")
    assert p.reset_calls == []


def test_passwords_must_match_and_be_long_enough(ui):
    p = ui()
    page = p.open(f"?reset_token={TOKEN}")
    p.fill("new-password-1", "new-password-2")
    page.click("#rpSubmit")
    assert "match" in page.inner_text("#fRpConfirm .hint")
    p.fill("12345")
    page.click("#rpSubmit")
    assert "6 characters" in page.inner_text("#fRpPassword .hint")
    assert p.reset_calls == []


def test_valid_submission_posts_once_and_succeeds(ui):
    p = ui()
    p.hold = True                                              # keep the request in flight
    page = p.open(f"?reset_token={TOKEN}")
    p.fill()
    page.click("#rpSubmit")
    page.wait_for_function("() => document.getElementById('rpSubmit').disabled")
    page.press("#rpConfirm", "Enter")                         # double submit while waiting
    page.click("#rpSubmit", force=True)
    assert not _on(page, "#success")                      # no success before the server says so
    assert p.reset_calls == [{"reset_token": TOKEN, "new_password": "new-password-1"}]
    p._answer(p.held.pop())
    page.wait_for_selector("#success.on")
    assert page.inner_text("#successTitle") == "Password updated"
    assert page.input_value("#rpPassword") == "" and page.input_value("#rpConfirm") == ""
    assert page.evaluate("() => localStorage.getItem('rova_token')") == "new-access"
    assert len(p.reset_calls) == 1


@pytest.mark.parametrize("detail", ["This reset link has expired. Request a new one.",
                                    "This reset link is invalid or has already been used. Request a new one."])
def test_expired_or_invalid_token_shows_the_servers_message(ui, detail):
    p = ui()
    p.reply = (401, {"detail": detail})
    page = p.open(f"?reset_token={TOKEN}")
    p.fill()
    page.click("#rpSubmit")
    page.wait_for_function("() => document.getElementById('rpMsg').textContent.length > 0")
    assert page.inner_text("#rpMsg") == detail
    assert not _visible(page, "#rpForm") and _visible(page, "#rpNewLink")
    assert not _on(page, "#success")
    assert page.evaluate("() => localStorage.getItem('rova_token')") is None


@pytest.mark.parametrize("reply, text", [((None, None), "reach the server"),
                                         ((500, {"detail": "Internal Server Error"}), "Internal Server Error"),
                                         ((422, {"detail": [{"msg": "too short"}]}), "6 to 128 characters")])
def test_failed_requests_never_claim_success(ui, reply, text):
    p = ui()
    p.reply = reply
    page = p.open(f"?reset_token={TOKEN}")
    p.fill()
    page.click("#rpSubmit")
    page.wait_for_function("() => document.getElementById('rpMsg').textContent.length > 0")
    assert text in page.inner_text("#rpMsg")
    assert not _on(page, "#success")
    assert page.evaluate("() => localStorage.getItem('rova_token')") is None
    assert page.is_enabled("#rpSubmit")                       # they can try again
    p.reply = (200, OK_BODY)
    page.click("#rpSubmit")
    page.wait_for_selector("#success.on")
    assert len(p.reset_calls) == 2


def test_signed_in_visitor_is_not_bounced_away_from_the_link(ui):
    p = ui(signed_in=True)
    page = p.open(f"?reset_token={TOKEN}&redirect=rova.html")
    assert page.url.split("?")[0].endswith("/auth")
    assert _visible(page, "#rpForm")
    assert "redirect=rova.html" in page.url                  # other parameters are kept


def test_plain_sign_in_page_is_unchanged(ui):
    p = ui()
    page = p.open("?mode=login")
    assert _visible(page, "#mainPanel") and not _visible(page, "#rpPanel")
    q = ui(signed_in=True)
    page = q.open("")
    page.wait_for_url("**/rova.html")                         # a signed-in visitor still skips through
