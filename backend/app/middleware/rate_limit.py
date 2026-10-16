"""Redis-backed fixed-window rate limiting: a limit on every request, and a
stricter one on sign-in attempts (/auth/login) against password guessing."""
import hashlib
import time

import redis
from fastapi import HTTPException, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.config import settings
from app.redis_client import redis_client
from app.services.security import decode_token

LOGIN_LIMITED = "Too many sign-in attempts. Try again in a few minutes."


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def client_ip(request: Request) -> str:
    """The caller's address. X-Forwarded-For is only believed for the entries our
    own proxies added (settings.trusted_proxy_hops, counted from the right): the
    rest of the header is whatever the client sent, so it can't be trusted."""
    hops = settings.trusted_proxy_hops
    if hops > 0:
        chain = [p.strip() for h in request.headers.getlist("x-forwarded-for")
                 for p in h.split(",") if p.strip()]
        if len(chain) >= hops:
            return chain[-hops]
    return request.client.host if request.client else "anon"


def rate_limit_identity(request: Request) -> str:
    """Who a request counts against: the user, when it carries a valid signed access
    token; otherwise its address. A missing, malformed or forged token never gets a
    bucket of its own, so made-up tokens can't be used to dodge the limit. Keys hold
    a user id or a digest, never a token."""
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        payload = decode_token(token.strip(), expected_type="access")   # signature + expiry
        if payload and payload.get("sub"):
            return f"user:{payload['sub']}"
    return f"ip:{_digest(client_ip(request))}"


def _retry_headers(seconds: int) -> dict:
    return {"Retry-After": str(max(1, seconds))}


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        now = int(time.time())
        window = now // settings.rate_limit_window_seconds
        key = f"ratelimit:{rate_limit_identity(request)}:{window}"

        try:
            count = redis_client.incr(key)
            if count == 1:
                redis_client.expire(key, settings.rate_limit_window_seconds)
        except Exception:
            # Fail open if Redis is unavailable.
            return await call_next(request)

        if count > settings.rate_limit_requests:
            return JSONResponse(
                {"detail": "Rate limit exceeded. Try again shortly."},
                status_code=429,
                headers=_retry_headers((window + 1) * settings.rate_limit_window_seconds - now),
            )
        return await call_next(request)


# ---- sign-in attempts ------------------------------------------------------------------

def _account_key(email: str) -> str:
    return f"login:acct:{_digest(email.strip().lower())}"


def _count(key: str, window: int) -> tuple[int, int]:
    """One more attempt in a fixed window that starts at the first attempt and is
    never extended, so a block always ends. (count so far, seconds left)."""
    pipe = redis_client.pipeline()
    pipe.set(key, 0, ex=window, nx=True)
    pipe.incr(key)
    pipe.ttl(key)
    _, count, ttl = pipe.execute()
    return int(count), int(ttl)


def check_login_attempt(request: Request, email: str) -> None:
    """Count a sign-in attempt, refusing it (429) once the account, or the client
    address when that limit is on, is over its limit. Counted before the password is
    checked and the same for unknown emails, so a block says nothing about the
    password or whether the account exists."""
    window = settings.login_window_seconds
    limits = [(_account_key(email), settings.login_max_attempts_per_account)]
    if settings.login_max_attempts_per_ip > 0:
        limits.append((f"login:ip:{_digest(client_ip(request))}", settings.login_max_attempts_per_ip))
    for key, limit in limits:
        try:
            count, ttl = _count(key, window)
        except redis.RedisError:
            return      # fail open, like the request limit: Redis down mustn't stop sign-ins
        if count > limit:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, LOGIN_LIMITED,
                                headers=_retry_headers(ttl))


def clear_login_attempts(email: str) -> None:
    """A successful sign-in clears that account's count (not the address's: signing in
    to your own account mustn't reset the count of guesses at others)."""
    try:
        redis_client.delete(_account_key(email))
    except redis.RedisError:
        pass
