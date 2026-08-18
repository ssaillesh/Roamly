"""Transactional email via the SendGrid Web API (HTTPS, no SMTP ports).

If SENDGRID_API_KEY / EMAIL_FROM are not configured, email_enabled() is False
and callers fall back to other behavior (e.g. returning the reset token).
"""
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_SENDGRID_URL = "https://api.sendgrid.com/v3/mail/send"


def email_enabled() -> bool:
    return bool(settings.sendgrid_api_key and settings.email_from)


def send_email(to_email: str, subject: str, html: str, text: str | None = None) -> bool:
    """Send one email. Returns True on success, False otherwise (never raises)."""
    if not email_enabled():
        logger.warning("Email not configured; skipping send to %s", to_email)
        return False

    payload = {
        "personalizations": [{"to": [{"email": to_email}]}],
        "from": {"email": settings.email_from, "name": settings.email_from_name},
        "subject": subject,
        "content": [
            {"type": "text/plain", "value": text or _strip_html(html)},
            {"type": "text/html", "value": html},
        ],
    }
    try:
        resp = httpx.post(
            _SENDGRID_URL,
            json=payload,
            headers={"Authorization": f"Bearer {settings.sendgrid_api_key}"},
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        logger.error("SendGrid request failed: %s", exc)
        return False
    if resp.status_code >= 300:
        logger.error("SendGrid error %s: %s", resp.status_code, resp.text)
        return False
    return True


def send_password_reset(to_email: str, reset_token: str) -> bool:
    reset_link = f"{settings.frontend_base_url}/?reset_token={reset_token}"
    subject = "Reset your Sway password"
    html = f"""
      <p>Hi,</p>
      <p>We received a request to reset your Sway password.
         Click the link below to choose a new one. This link expires in 15 minutes.</p>
      <p><a href="{reset_link}">Reset my password</a></p>
      <p>If the link doesn't work, paste this code into the app:</p>
      <p style="font-family:monospace;word-break:break-all">{reset_token}</p>
      <p>If you didn't request this, you can safely ignore this email.</p>
      <p>— Sway</p>
    """
    return send_email(to_email, subject, html)


def _strip_html(html: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", html).strip()
