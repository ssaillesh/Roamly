"""Regression tests for two account-takeover holes:

1. POST /auth/apple trusted any client-supplied identity token and linked the
   account by email — anyone could log in as anyone. The endpoint is removed.
2. POST /auth/forgot-password returned the reset token in the response when no
   email service was configured — anyone knowing an email could reset it.
"""
import logging

from app.config import settings
from app.services.security import create_reset_token
from tests.conftest import make_user

GENERIC = "If an account exists for that email, password reset instructions have been sent."


def test_apple_sign_in_endpoint_is_gone(client):
    victim, _ = make_user(client, "victim")
    email = f"{victim['username']}@example.com"     # make_user's email scheme

    r = client.post("/api/v1/auth/apple", json={"identity_token": "forged", "email": email})
    assert r.status_code in (404, 405)
    assert "access_token" not in r.text


def test_forgot_password_never_returns_a_token(client):
    user, _ = make_user(client, "reset")
    email = f"{user['username']}@example.com"
    known = client.post("/api/v1/auth/forgot-password", json={"email": email})
    unknown = client.post("/api/v1/auth/forgot-password", json={"email": "nobody_xyz@example.com"})
    assert known.status_code == unknown.status_code == 200
    # Same body either way: no token, and no way to tell which emails exist.
    assert known.json() == unknown.json() == {"message": GENERIC}


def test_reset_link_only_logged_outside_production(client, monkeypatch, caplog):
    user, _ = make_user(client, "reset")
    email = f"{user['username']}@example.com"

    monkeypatch.setattr(settings, "environment", "development")
    with caplog.at_level(logging.WARNING):
        client.post("/api/v1/auth/forgot-password", json={"email": email})
    assert "reset_token=" in caplog.text            # dev: link in the server log

    caplog.clear()
    monkeypatch.setattr(settings, "environment", "production")
    with caplog.at_level(logging.WARNING):
        client.post("/api/v1/auth/forgot-password", json={"email": email})
    assert "reset_token=" not in caplog.text        # prod: never logged or returned


def test_emailed_token_still_resets_the_password(client):
    user, _ = make_user(client, "reset")
    token = create_reset_token(user["id"])          # what the email would contain
    r = client.post("/api/v1/auth/reset-password",
                    json={"reset_token": token, "new_password": "brand-new-pass"})
    assert r.status_code == 200, r.text
    login = client.post("/api/v1/auth/login", json={
        "email": f"{user['username']}@example.com", "password": "brand-new-pass"})
    assert login.status_code == 200
