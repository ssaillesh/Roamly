"""Shared by the browser tests: webui/ served like Vercel serves it, and Chrome."""
import functools
import http.server
import threading
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

WEBUI = Path(__file__).resolve().parents[3] / "webui"
API = "http://127.0.0.1:8001/api/v1"          # config.js's API on localhost


class _CleanUrls(http.server.SimpleHTTPRequestHandler):
    """Serves /auth as auth.html, like Vercel's cleanUrls (webui/vercel.json)."""

    def translate_path(self, path):
        local = Path(super().translate_path(path))
        return str(local.with_suffix(".html")) if not local.suffix and local.with_suffix(".html").exists() else str(local)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def site():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_CleanUrls, directory=str(WEBUI)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True)
        yield b
        b.close()
