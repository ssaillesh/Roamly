"""Email through Resend (services/email.py), with the SDK's send call replaced:
nothing here reaches resend.com."""
import logging

import pytest
import resend
from resend.exceptions import InvalidApiKeyError
from resend.http_client import HTTPClient

from app.api import auth as auth_api
from app.config import Settings, settings
from app.services import email as email_svc
from app.services import usage
from tests.conftest import make_user

KEY = "re_test_0123456789abcdef"


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "resend_api_key", KEY)
    monkeypatch.setattr(settings, "email_from", "hello@roamly.example")
    monkeypatch.setattr(settings, "email_from_name", "Roamly")
    monkeypatch.setattr(usage, "allow", lambda provider: True)
    recorded = []
    monkeypatch.setattr(usage, "record", lambda provider, ok=True: recorded.append((provider, ok)))
    return recorded


@pytest.fixture
def outbox(monkeypatch):
    sent = []

    def fake_send(params, *a, **kw):
        sent.append((dict(params), resend.api_key))
        return {"id": f"email-{len(sent)}"}
    monkeypatch.setattr(resend.Emails, "send", fake_send)
    return sent


# ---- configuration ---------------------------------------------------------------------

@pytest.mark.parametrize("key, sender, problem", [
    ("", "", "RESEND_API_KEY and EMAIL_FROM not set"),
    ("", "hello@roamly.example", "RESEND_API_KEY not set"),
    (KEY, "", "EMAIL_FROM not set"),
    (KEY, "not-an-address", "EMAIL_FROM isn't a valid email address"),
    (KEY, "Roamly <hello@roamly.example>", "EMAIL_FROM isn't a valid email address"),   # name goes in EMAIL_FROM_NAME
    (KEY, "hello@roamly.example", None),
])
def test_email_is_only_on_when_properly_configured(monkeypatch, key, sender, problem):
    monkeypatch.setattr(settings, "resend_api_key", key)
    monkeypatch.setattr(settings, "email_from", sender)
    got = email_svc.email_problem()
    assert (got is None) if problem is None else got.startswith(problem)
    assert email_svc.email_enabled() is (problem is None)


def test_settings_trim_pasted_whitespace_and_read_resend_variables(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", f"  {KEY}\n")
    monkeypatch.setenv("EMAIL_FROM", " hello@roamly.example ")
    s = Settings(_env_file=None)
    assert (s.resend_api_key, s.email_from) == (KEY, "hello@roamly.example")
    assert not hasattr(s, "sendgrid_api_key")


def test_free_tier_cap_is_under_resends_free_plan():
    period, free, cap, _ = usage.FREE_TIER["resend"]
    assert (period, free) == ("day", 100) and cap < free and cap * 31 <= 3000   # 100/day and 3,000/month
    assert "sendgrid" not in usage.FREE_TIER


def test_requests_time_out_quickly():
    assert resend.default_http_client._timeout == 10


# ---- sending ------------------------------------------------------------------------------

def test_send_email_goes_through_resend(configured, outbox):
    assert email_svc.send_email("sam@example.com", "Hi", "<p>Hello <b>Sam</b></p>") is True
    params, key_used = outbox[0]
    assert params == {"from": "Roamly <hello@roamly.example>", "to": ["sam@example.com"], "subject": "Hi",
                      "html": "<p>Hello <b>Sam</b></p>", "text": "Hello Sam"}
    assert key_used == KEY
    assert configured == [("resend", True)]


class _FakeNetwork(HTTPClient):
    """Stands in for the SDK's HTTP layer, so the real resend.Emails.send runs offline."""

    def __init__(self, status=200, body=b'{"id": "4ef9a417-02e9-4d39-ad75-9611e0fcc33c"}'):
        self.calls, self.status, self.body = [], status, body

    def request(self, method, url, headers, json=None, files=None, data=None):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "json": json})
        return self.body, self.status, {"content-type": "application/json"}


def test_the_real_sdk_sends_the_expected_request(configured, monkeypatch):
    net = _FakeNetwork()
    monkeypatch.setattr(resend, "default_http_client", net)
    assert email_svc.send_email("sam@example.com", "Hi", "<p>Hello</p>") is True
    call = net.calls[0]
    assert (call["method"].lower(), call["url"]) == ("post", "https://api.resend.com/emails")
    assert call["headers"]["Authorization"] == f"Bearer {KEY}"
    assert call["json"] == {"from": "Roamly <hello@roamly.example>", "to": ["sam@example.com"],
                            "subject": "Hi", "html": "<p>Hello</p>", "text": "Hello"}


def test_the_real_sdk_reports_a_refusal_as_not_sent(configured, monkeypatch, caplog):
    net = _FakeNetwork(403, b'{"statusCode": 403, "name": "validation_error", '
                            b'"message": "The roamly.example domain is not verified."}')
    monkeypatch.setattr(resend, "default_http_client", net)
    with caplog.at_level(logging.ERROR):
        assert email_svc.send_email("sam@example.com", "Hi", "<p>x</p>") is False
    assert "domain is not verified" in caplog.text and KEY not in caplog.text
    assert configured == [("resend", False)]


def test_sender_name_cannot_inject_headers(configured, outbox, monkeypatch):
    monkeypatch.setattr(settings, "email_from_name", 'Roamly <x@evil.example>\r\nBcc: all@evil.example')
    email_svc.send_email("sam@example.com", "Hi", "<p>x</p>")
    sender = outbox[0][0]["from"]
    assert sender.endswith(" <hello@roamly.example>")
    assert "\r" not in sender and "\n" not in sender and sender.count("<") == 1


@pytest.mark.parametrize("error", [
    InvalidApiKeyError(message="API key is invalid", error_type="invalid_api_key", code=403),
    RuntimeError("Request failed: HTTPSConnectionPool(host='api.resend.com', port=443): timed out"),
])
def test_a_failed_send_is_logged_and_never_raises(configured, monkeypatch, caplog, error):
    def boom(params, *a, **kw):
        raise error
    monkeypatch.setattr(resend.Emails, "send", boom)
    with caplog.at_level(logging.ERROR):
        assert email_svc.send_email("sam@example.com", "Hi", "<p>x</p>") is False
    assert "Resend didn't send the email" in caplog.text
    assert KEY not in caplog.text
    assert configured == [("resend", False)]


def test_nothing_is_sent_when_not_configured_or_over_the_cap(configured, outbox, monkeypatch):
    monkeypatch.setattr(settings, "resend_api_key", "")
    assert email_svc.send_email("sam@example.com", "Hi", "<p>x</p>") is False
    monkeypatch.setattr(settings, "resend_api_key", KEY)
    monkeypatch.setattr(usage, "allow", lambda provider: False)
    assert email_svc.send_email("sam@example.com", "Hi", "<p>x</p>") is False
    assert outbox == []


def test_password_reset_email_carries_the_link(configured, outbox, monkeypatch):
    monkeypatch.setattr(settings, "frontend_base_url", "https://app.example.com")
    assert email_svc.send_password_reset("sam@example.com", "aaa.bbb.ccc") is True
    params = outbox[0][0]
    assert params["to"] == ["sam@example.com"]
    assert 'href="https://app.example.com/auth?reset_token=aaa.bbb.ccc"' in params["html"]
    assert "expires in 15 minutes" in params["text"]


def test_forgot_password_answers_the_same_when_resend_fails(client, configured, monkeypatch):
    user, _ = make_user(client, "mail")

    def boom(params, *a, **kw):
        raise RuntimeError("Request failed: connection reset")
    monkeypatch.setattr(resend.Emails, "send", boom)
    known = client.post("/api/v1/auth/forgot-password", json={"email": f"{user['username']}@example.com"})
    unknown = client.post("/api/v1/auth/forgot-password", json={"email": "nobody_mail_xyz@example.com"})
    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()
    assert auth_api.email_enabled() is True
