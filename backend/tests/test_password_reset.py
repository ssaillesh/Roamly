"""The password-reset flow end to end on the API side: the emailed link, and
POST /auth/reset-password (expired, invalid, single use, hashing, no leaks)."""
import logging
import uuid
from urllib.parse import parse_qs, urlsplit

from app.api import auth as auth_api
from app.config import settings
from app.database import SessionLocal
from app.models import User
from app.services import email as email_svc
from app.services.security import (
    _create_token, _password_fingerprint, create_access_token, create_reset_token, verify_password,
)
from tests.conftest import auth_headers, make_user

RESET = "/api/v1/auth/reset-password"


def _hash(user_id: str) -> str | None:
    with SessionLocal() as db:
        return db.get(User, uuid.UUID(user_id)).password_hash


def _token(user) -> str:
    return create_reset_token(user["id"], _hash(user["id"]))


def _email(user) -> str:
    return f"{user['username']}@example.com"


def _login(client, user, password):
    return client.post("/api/v1/auth/login", json={"email": _email(user), "password": password})


# ---- the emailed link -------------------------------------------------------------

def test_reset_link_points_at_the_sign_in_page_with_the_token(monkeypatch):
    monkeypatch.setattr(settings, "frontend_base_url", "https://app.example.com/")
    link = email_svc.reset_link("aaa.bbb-c_d.e+f/g=")
    parts = urlsplit(link)
    assert (parts.scheme, parts.netloc, parts.path) == ("https", "app.example.com", "/auth")
    assert parse_qs(parts.query) == {"reset_token": ["aaa.bbb-c_d.e+f/g="]}   # encoded, round-trips
    assert set(parse_qs(parts.query)) == {"reset_token"}                        # nothing else in the URL


def test_forgot_password_emails_that_link(client, monkeypatch):
    user, _ = make_user(client, "rlink")
    monkeypatch.setattr(settings, "frontend_base_url", "https://app.example.com")
    sent = {}
    monkeypatch.setattr(auth_api, "email_enabled", lambda: True)
    monkeypatch.setattr(email_svc, "send_email", lambda to, subject, html, text=None: sent.update(to=to, html=html) or True)
    r = client.post("/api/v1/auth/forgot-password", json={"email": _email(user)})
    assert r.status_code == 200
    assert sent["to"] == _email(user)
    assert 'href="https://app.example.com/auth?reset_token=' in sent["html"]
    token = sent["html"].split("reset_token=")[1].split('"')[0]
    # The emailed token works (and is the only copy: no "paste this code" fallback).
    assert sent["html"].count(token) == 1
    assert client.post(RESET, json={"reset_token": token, "new_password": "fresh-pass-1"}).status_code == 200


# ---- the endpoint -----------------------------------------------------------------

def test_reset_stores_a_bcrypt_hash_and_signs_in(client):
    user, _ = make_user(client, "rhash")
    r = client.post(RESET, json={"reset_token": _token(user), "new_password": "brand-new-pass"})
    assert r.status_code == 200, r.text
    assert r.json()["access_token"] and "password" not in r.text
    stored = _hash(user["id"])
    assert stored.startswith("$2") and "brand-new-pass" not in stored     # bcrypt, never plaintext
    assert verify_password("brand-new-pass", stored)
    assert _login(client, user, "brand-new-pass").status_code == 200
    assert _login(client, user, "password123").status_code == 401


def test_reset_token_is_single_use(client):
    user, _ = make_user(client, "rreuse")
    token = _token(user)
    assert client.post(RESET, json={"reset_token": token, "new_password": "first-new-pass"}).status_code == 200
    again = client.post(RESET, json={"reset_token": token, "new_password": "second-new-pass"})
    assert again.status_code == 401
    assert again.json()["detail"] == auth_api.RESET_INVALID
    assert _login(client, user, "first-new-pass").status_code == 200     # the replay changed nothing


def test_older_links_die_when_the_password_changes(client):
    user, access = make_user(client, "rolder")
    first, second = _token(user), _token(user)          # e.g. "forgot password" pressed twice
    assert client.post(RESET, json={"reset_token": second, "new_password": "newest-pass-1"}).status_code == 200
    assert client.post(RESET, json={"reset_token": first, "new_password": "attacker-pass"}).status_code == 401

    # A link issued before a normal password change dies too.
    stale = _token(user)
    changed = client.post("/api/v1/auth/change-password", headers=auth_headers(access),
                          json={"current_password": "newest-pass-1", "new_password": "changed-pass-2"})
    assert changed.status_code == 200
    assert client.post(RESET, json={"reset_token": stale, "new_password": "attacker-pass"}).status_code == 401
    assert _login(client, user, "changed-pass-2").status_code == 200


def test_expired_token_gets_its_own_message(client):
    user, _ = make_user(client, "rexp")
    expired = _create_token(user["id"], -1, "reset", pwd=_password_fingerprint(_hash(user["id"])))
    r = client.post(RESET, json={"reset_token": expired, "new_password": "whatever-pass"})
    assert r.status_code == 401
    assert r.json()["detail"] == auth_api.RESET_EXPIRED
    assert _login(client, user, "password123").status_code == 200       # unchanged


def test_invalid_tokens_are_rejected_without_saying_why(client):
    user, _ = make_user(client, "rbad")
    good = _token(user)
    head, body, sig = good.split(".")
    forged_sig = f"{head}.{body}.{'A' * len(sig)}"
    access = create_access_token(user["id"])                               # wrong token type
    unknown = create_reset_token(str(uuid.uuid4()), None)                   # no such account
    for bad in ("not-a-token", forged_sig, access, unknown):
        r = client.post(RESET, json={"reset_token": bad, "new_password": "attacker-pass"})
        assert r.status_code == 401, bad
        assert r.json() == {"detail": auth_api.RESET_INVALID}, bad
    assert client.post(RESET, json={"reset_token": "", "new_password": "attacker-pass"}).status_code == 422
    assert _login(client, user, "password123").status_code == 200


def test_too_short_password_leaves_the_link_usable(client):
    user, _ = make_user(client, "rshort")
    token = _token(user)
    assert client.post(RESET, json={"reset_token": token, "new_password": "12345"}).status_code == 422
    assert client.post(RESET, json={"reset_token": token, "new_password": "long-enough"}).status_code == 200


def test_reset_never_logs_the_token_or_password(client, monkeypatch, caplog):
    monkeypatch.setattr(settings, "environment", "production")
    user, _ = make_user(client, "rlog")
    token = _token(user)
    with caplog.at_level(logging.DEBUG):
        client.post("/api/v1/auth/forgot-password", json={"email": _email(user)})
        client.post(RESET, json={"reset_token": token, "new_password": "secret-new-pass"})
        client.post(RESET, json={"reset_token": token, "new_password": "secret-other-pass"})
    for secret in (token, "secret-new-pass", "secret-other-pass"):
        assert secret not in caplog.text
