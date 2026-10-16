"""CORS: only the web app's own origin(s) may call the API from a browser."""
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from app.config import ConfigError, Settings, check_production, settings

BACKEND = Path(__file__).resolve().parents[1]
SITE, WWW = "https://app.example.com", "https://www.example.com"
EVIL = ["https://evil.example", "null", "http://localhost.evil.com", "https://app.example.com.evil.com",
        "http://app.example.com"]


def _acao(r):
    return r.headers.get_list("access-control-allow-origin")


def _preflight(client, origin, method="PATCH", headers="authorization,content-type"):
    return client.options("/api/v1/users/me", headers={
        "Origin": origin, "Access-Control-Request-Method": method, "Access-Control-Request-Headers": headers})


# ---- this (development) process ---------------------------------------------------------

def test_the_frontend_origin_is_allowed(client):
    origin = settings.cors_origins()[0]
    r = client.get("/health", headers={"Origin": origin})
    assert _acao(r) == [origin]                                  # exactly one, never "*"
    assert "origin" in r.headers.get("vary", "").lower()
    assert "access-control-allow-credentials" not in r.headers


@pytest.mark.parametrize("origin", ["http://localhost:5173", "http://127.0.0.1:8080", "http://localhost"])
def test_local_dev_servers_are_allowed_outside_production(client, origin):
    assert _acao(client.get("/health", headers={"Origin": origin})) == [origin]


@pytest.mark.parametrize("origin", EVIL)
def test_other_origins_get_no_access(client, origin):
    r = client.get("/health", headers={"Origin": origin})
    assert _acao(r) == []
    pre = _preflight(client, origin)
    assert pre.status_code == 400 and _acao(pre) == []


def test_preflight_allows_only_what_the_app_uses(client):
    origin = settings.cors_origins()[0]
    ok = _preflight(client, origin)
    assert ok.status_code == 200 and _acao(ok) == [origin]
    assert "*" not in ok.headers["access-control-allow-methods"]
    assert "access-control-allow-credentials" not in ok.headers
    assert _preflight(client, origin, headers="x-something-else").status_code == 400
    assert _preflight(client, origin, method="TRACE").status_code == 400


def test_a_rate_limited_reply_is_still_readable_by_the_page(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_requests", 1)
    monkeypatch.setattr(settings, "rate_limit_window_seconds", 3600)
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1)
    origin, ip = settings.cors_origins()[0], f"192.0.2.{uuid.uuid4().hex[:8]}"
    codes = [client.get("/health", headers={"Origin": origin, "X-Forwarded-For": ip}) for _ in range(2)]
    assert codes[-1].status_code == 429
    assert _acao(codes[-1]) == [origin]


# ---- the configuration rules ------------------------------------------------------------

def _prod(**kw) -> Settings:
    return Settings(_env_file=None, environment="production", jwt_secret="c0ffee" * 11, **kw)


def test_production_origins_come_from_the_settings():
    s = _prod(frontend_base_url=SITE + "/", cors_allow_origins=f" {WWW} , {SITE}")
    check_production(s)
    assert s.cors_origins() == [SITE, WWW]


@pytest.mark.parametrize("kw", [
    {},                                                          # FRONTEND_BASE_URL left at localhost
    {"frontend_base_url": "http://app.example.com"},             # not https
    {"frontend_base_url": SITE, "cors_allow_origins": "*"},
    {"frontend_base_url": SITE, "cors_allow_origins": "https://*.example.com"},
    {"frontend_base_url": SITE, "cors_allow_origins": "http://localhost:5173"},
    {"frontend_base_url": SITE, "cors_allow_origins": "https://127.0.0.1"},
    {"frontend_base_url": SITE, "cors_allow_origins": "https://www.example.com/app"},
])
def test_production_refuses_unsafe_origins(kw):
    with pytest.raises(ConfigError, match="CORS origin"):
        check_production(_prod(**kw))


def test_a_wildcard_is_refused_everywhere():
    with pytest.raises(ConfigError, match="wildcard"):
        check_production(Settings(_env_file=None, environment="development", cors_allow_origins="*"))


# ---- the real app, started in production --------------------------------------------------

PROBE = r"""
import json
from fastapi.testclient import TestClient
from app.main import app
c = TestClient(app)
out = {}
for o in %s:
    r = c.get("/health", headers={"Origin": o})
    p = c.options("/api/v1/auth/login", headers={"Origin": o, "Access-Control-Request-Method": "POST",
                                                 "Access-Control-Request-Headers": "content-type"})
    out[o] = [r.headers.get_list("access-control-allow-origin"), p.status_code,
              p.headers.get_list("access-control-allow-origin"), "access-control-allow-credentials" in p.headers]
print(json.dumps(out))
"""


def test_the_production_app_allows_exactly_its_configured_origins():
    origins = [SITE, WWW, "http://localhost:5173", "http://127.0.0.1:8080", *EVIL]
    env = {**os.environ, "ENVIRONMENT": "production", "JWT_SECRET": "c0ffee" * 11,
           "FRONTEND_BASE_URL": SITE, "CORS_ALLOW_ORIGINS": WWW}
    run = subprocess.run([sys.executable, "-B", "-c", PROBE % json.dumps(origins)], cwd=BACKEND, env=env,
                         capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stderr[-2000:]
    got = json.loads(run.stdout.strip().splitlines()[-1])
    for o in (SITE, WWW):
        assert got[o] == [[o], 200, [o], False], o
    for o in origins[2:]:                                        # dev servers and strangers
        assert got[o] == [[], 400, [], False], o
