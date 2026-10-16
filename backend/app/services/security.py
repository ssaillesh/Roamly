"""Password hashing and JWT creation/verification."""
import hashlib
import hmac
from datetime import datetime, timedelta, timezone

from jose import jwt, ExpiredSignatureError, JWTError
from passlib.context import CryptContext

from app.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    # bcrypt has a 72-byte limit; truncate defensively.
    return pwd_context.hash(password[:72])


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password[:72], password_hash)


def _create_token(subject: str, minutes: int, token_type: str, **claims) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "type": token_type,
        "iat": now,
        "exp": now + timedelta(minutes=minutes),
        **claims,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_access_token(user_id: str) -> str:
    return _create_token(user_id, settings.access_token_expire_minutes, "access")


def create_refresh_token(user_id: str) -> str:
    return _create_token(user_id, settings.refresh_token_expire_minutes, "refresh")


def _password_fingerprint(password_hash: str | None) -> str:
    """A keyed digest of the stored password hash (the hash itself never goes in a token)."""
    return hmac.new(settings.jwt_secret.encode(), (password_hash or "").encode(),
                    hashlib.sha256).hexdigest()[:32]


def create_reset_token(user_id: str, password_hash: str | None) -> str:
    """Short-lived token used to authorize a password reset. It carries a fingerprint
    of the current password, so it works once: after the password changes (by this
    reset or any other way), every reset token issued before stops matching."""
    return _create_token(user_id, 15, "reset", pwd=_password_fingerprint(password_hash))


def decode_reset_token(token: str) -> tuple[dict | None, bool]:
    """(payload, expired). The payload is None unless this is a validly signed,
    unexpired reset token; `expired` is True only for a genuine one that ran out."""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except ExpiredSignatureError:      # raised only after the signature checked out
        return None, True
    except JWTError:
        return None, False
    if payload.get("type") != "reset":
        return None, False
    return payload, False


def reset_token_matches(payload: dict, password_hash: str | None) -> bool:
    """Was this reset token issued for the password the account has now?"""
    return hmac.compare_digest(str(payload.get("pwd", "")), _password_fingerprint(password_hash))


def decode_token(token: str, expected_type: str | None = None) -> dict | None:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError:
        return None
    if expected_type and payload.get("type") != expected_type:
        return None
    return payload
