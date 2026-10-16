"""Transactional email via Resend (resend.com), over HTTPS with its Python SDK.

If RESEND_API_KEY / EMAIL_FROM aren't set, or EMAIL_FROM isn't a valid address,
email_enabled() is False and callers fall back to other behavior (e.g. logging
the reset link outside production). email_problem() says which.
"""
import logging
import re
from urllib.parse import urlencode

import resend
from email_validator import EmailNotValidError, validate_email
from resend.http_client_requests import RequestsClient

from app.config import settings
from app.services import usage

logger = logging.getLogger(__name__)

# A slow email service mustn't hold up the request that sends (the SDK's default is 30 s).
resend.default_http_client = RequestsClient(timeout=10)


def email_problem() -> str | None:
    """Why email can't be sent, or None when it's set up (shown on the admin dashboard)."""
    missing = [name for name, value in (("RESEND_API_KEY", settings.resend_api_key),
                                        ("EMAIL_FROM", settings.email_from)) if not value]
    if missing:
        return " and ".join(missing) + " not set on the API service"
    try:
        validate_email(settings.email_from, check_deliverability=False)
    except EmailNotValidError:
        return "EMAIL_FROM isn't a valid email address"
    return None


def email_enabled() -> bool:
    return email_problem() is None


def _sender() -> str:
    """'Name <address>', with nothing in the name that could break the header."""
    name = re.sub(r'[<>"\r\n]', "", settings.email_from_name).strip()
    return f"{name} <{settings.email_from}>" if name else settings.email_from


def send_email(to_email: str, subject: str, html: str, text: str | None = None) -> bool:
    """Send one email. Returns True on success, False otherwise, and never raises:
    "forgot password" must answer the same whether or not an email went out (an
    error only for real accounts would reveal which addresses have one), and an
    invite run counts failures instead of stopping."""
    if not email_enabled():
        logger.warning("Email not configured; skipping send to %s", to_email)
        return False
    if not usage.allow("resend"):
        logger.warning("Daily free email allowance used up; skipping send to %s", to_email)
        return False

    resend.api_key = settings.resend_api_key
    params: resend.Emails.SendParams = {
        "from": _sender(),
        "to": [to_email],
        "subject": subject,
        "html": html,
        "text": text or _strip_html(html),
    }
    try:
        sent = resend.Emails.send(params)
    except Exception as exc:     # resend.exceptions.ResendError (refused, or the network failed), or anything else
        logger.error("Resend didn't send the email to %s: %s", to_email, exc)
        usage.record("resend", ok=False)
        return False
    usage.record("resend", ok=True)
    return bool(sent and sent.get("id"))


def reset_link(reset_token: str) -> str:
    """The emailed link: the sign-in page (webui/auth.html, served at /auth), which
    opens its "choose a new password" panel when it sees reset_token."""
    return f"{settings.frontend_base_url.rstrip('/')}/auth?{urlencode({'reset_token': reset_token})}"


def send_password_reset(to_email: str, reset_token: str) -> bool:
    subject = "Reset your Roamly password"
    html = f"""
      <p>Hi,</p>
      <p>We received a request to reset your Roamly password.
         Click the link below to choose a new one. This link expires in 15 minutes.</p>
      <p><a href="{reset_link(reset_token)}">Reset my password</a></p>
      <p>If you didn't request this, you can safely ignore this email.</p>
      <p>— Roamly</p>
    """
    return send_email(to_email, subject, html)


def _strip_html(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html).strip()
