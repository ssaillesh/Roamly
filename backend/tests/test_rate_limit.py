"""Rate limiting: who a request counts against (no dodging it with made-up tokens
or forwarded-for headers), and the sign-in attempt limit on /auth/login."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import redis
from jose import jwt
from starlette.requests import Request

from app.config import Settings, settings
from app.middleware import rate_limit
from app.middleware.rate_limit import LOGIN_LIMITED, _digest, client_ip, rate_limit_identity
from app.redis_client import redis_client
from app.services.security import create_access_token, create_refresh_token
from tests.conftest import auth_headers, make_user

LOGIN = "/api/v1/auth/login"
PEER = "198.51.100.7"


def _req(headers=(), peer=PEER) -> Request:
    return Request({"type": "http", "method": "GET", "path": "/", "query_string": b"",
                    "headers": [(k.lower().encode(), v.encode()) for k, v in headers],
                    "client": (peer, 5555)})


def _bearer(token):
    return [("Authorization", f"Bearer {token}")]


def _fresh_ip():
    return f"192.0.2.{uuid.uuid4().int % 250}-{uuid.uuid4().hex[:8]}"


# ---- who a request counts against --------------------------------------------------

def test_a_valid_token_counts_against_its_user():
    uid = str(uuid.uuid4())
    assert rate_limit_identity(_req(_bearer(create_access_token(uid)))) == f"user:{uid}"


@pytest.mark.parametrize("auth", [None, "", "Bearer", "Bearer ", "Basic abc", "garbage",
                                  "Bearer not.a.jwt", "Bearer " + "x" * 5000])
def test_no_or_unusable_credentials_count_against_the_address(auth):
    headers = [("Authorization", auth)] if auth is not None else []
    assert rate_limit_identity(_req(headers)) == f"ip:{_digest(PEER)}"


def test_made_up_tokens_no_longer_get_fresh_buckets():
    # Before: every distinct header (its last 32 chars) was a new bucket.
    ids = {rate_limit_identity(_req(_bearer(uuid.uuid4().hex * 3))) for _ in range(20)}
    assert ids == {f"ip:{_digest(PEER)}"}


def test_appending_to_a_real_token_does_not_keep_or_steal_its_identity():
    uid = str(uuid.uuid4())
    real = create_access_token(uid)
    for tampered in (real + "x", real + "." + "A" * 40, "junk" + real[-32:], "A" * 40 + real[-32:]):
        # the old key was auth[-32:]; a token sharing a user's last 32 chars shared their bucket
        assert rate_limit_identity(_req(_bearer(tampered))) == f"ip:{_digest(PEER)}", tampered


@pytest.mark.parametrize("make", [
    lambda uid: jwt.encode({"sub": uid, "type": "access",
                            "exp": datetime.now(timezone.utc) + timedelta(hours=1)}, "not-our-secret", "HS256"),
    lambda uid: jwt.encode({"sub": uid, "type": "access", "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
                           "", algorithm="HS256").rsplit(".", 1)[0] + ".",                  # unsigned
    lambda uid: create_refresh_token(uid),                                                  # wrong type
    lambda uid: jwt.encode({"sub": uid, "type": "access", "exp": datetime.now(timezone.utc) - timedelta(minutes=1)},
                           settings.jwt_secret, settings.jwt_algorithm),                     # expired
])
def test_forged_or_unusable_tokens_cannot_claim_a_user(make):
    victim = str(uuid.uuid4())
    assert rate_limit_identity(_req(_bearer(make(victim)))) == f"ip:{_digest(PEER)}"


def test_forwarded_for_is_ignored_unless_proxies_are_configured(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxy_hops", 0)
    for spoof in ("1.1.1.1", "2.2.2.2, 3.3.3.3"):
        assert client_ip(_req([("X-Forwarded-For", spoof)])) == PEER


def test_only_the_proxys_own_entry_is_believed(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1)
    # The client can prepend anything; the proxy appends the address it saw.
    assert client_ip(_req([("X-Forwarded-For", "6.6.6.6, 203.0.113.9")])) == "203.0.113.9"
    assert client_ip(_req([("X-Forwarded-For", "7.7.7.7, 8.8.8.8, 203.0.113.9")])) == "203.0.113.9"
    assert client_ip(_req([("X-Forwarded-For", "6.6.6.6"), ("X-Forwarded-For", "203.0.113.9")])) == "203.0.113.9"
    assert client_ip(_req([])) == PEER                         # didn't come through the proxy
    monkeypatch.setattr(settings, "trusted_proxy_hops", 2)
    assert client_ip(_req([("X-Forwarded-For", "6.6.6.6, 203.0.113.9, 10.0.0.2")])) == "203.0.113.9"
    assert client_ip(_req([("X-Forwarded-For", "10.0.0.2")])) == PEER


# ---- the request limit, end to end ------------------------------------------------

@pytest.fixture
def tighten(monkeypatch):
    """Call after any setup requests (sign-ups count against the test client's shared address)."""
    def go():
        monkeypatch.setattr(settings, "rate_limit_requests", 3)
        monkeypatch.setattr(settings, "rate_limit_window_seconds", 3600)
        monkeypatch.setattr(settings, "trusted_proxy_hops", 1)     # a fresh address per test
    return go


def test_rotating_fake_tokens_is_still_limited(client, tighten):
    tighten()
    ip = _fresh_ip()
    codes = [client.get("/health", headers={"X-Forwarded-For": ip, "Authorization": f"Bearer {uuid.uuid4().hex * 2}"})
             for _ in range(5)]
    assert [r.status_code for r in codes] == [200, 200, 200, 429, 429]
    assert int(codes[-1].headers["Retry-After"]) >= 1


def test_each_signed_in_user_has_their_own_limit(client, tighten):
    _, a = make_user(client, "rl")
    _, b = make_user(client, "rl")
    tighten()
    ip = {"X-Forwarded-For": _fresh_ip()}                       # same address for both
    assert [client.get("/health", headers={**ip, **auth_headers(a)}).status_code for _ in range(4)] == [200] * 3 + [429]
    assert client.get("/health", headers={**ip, **auth_headers(b)}).status_code == 200


def test_keys_never_hold_a_token(client, tighten):
    _, token = make_user(client, "rl")
    tighten()
    fake = "Zz" * 40
    client.get("/health", headers={"X-Forwarded-For": _fresh_ip(), **auth_headers(token)})
    client.get("/health", headers={"X-Forwarded-For": _fresh_ip(), "Authorization": f"Bearer {fake}"})
    keys = list(redis_client.scan_iter("ratelimit:*")) + list(redis_client.scan_iter("login:*"))
    assert keys
    for k in keys:
        assert token[-32:] not in k and fake[-32:] not in k


# ---- sign-in attempts ----------------------------------------------------------------

def _email(user):
    return f"{user['username']}@example.com"


def _login(client, email, password, ip=None):
    return client.post(LOGIN, json={"email": email, "password": password},
                       headers={"X-Forwarded-For": ip} if ip else {})


def test_defaults():
    s = Settings(_env_file=None)
    assert (s.login_window_seconds, s.login_max_attempts_per_account, s.login_max_attempts_per_ip,
            s.trusted_proxy_hops) == (900, 10, 0, 0)


def test_password_guessing_is_cut_off_and_the_right_password_doesnt_leak_through(client):
    user, _ = make_user(client, "brute")
    codes = [_login(client, _email(user), f"guess-{i}").status_code for i in range(10)]
    assert codes == [401] * 10
    blocked = _login(client, _email(user), "guess-10")
    assert blocked.status_code == 429
    assert blocked.json() == {"detail": LOGIN_LIMITED}
    assert 1 <= int(blocked.headers["Retry-After"]) <= settings.login_window_seconds
    # Even the right password is refused now, so a 429 can't confirm a guess.
    assert _login(client, _email(user), "password123").status_code == 429


def test_changing_the_emails_capitals_shares_the_count(client):
    user, _ = make_user(client, "brute")
    email = _email(user)
    variants = [email.upper(), email.capitalize(), email.swapcase(), email]
    codes = [_login(client, variants[i % 4], f"guess-{i}").status_code for i in range(10)]
    assert set(codes) == {401}
    assert _login(client, email.upper(), "guess").status_code == 429
    assert _login(client, email, "password123").status_code == 429


def test_unknown_emails_behave_exactly_like_real_ones(client):
    real, _ = make_user(client, "brute")
    ghost = f"ghost_{uuid.uuid4().hex[:8]}@example.com"
    for email in (_email(real), ghost):
        replies = [_login(client, email, f"guess-{i}") for i in range(11)]
        assert [r.status_code for r in replies] == [401] * 10 + [429]
        assert replies[0].json() == {"detail": "Invalid credentials"}
        assert replies[-1].json() == {"detail": LOGIN_LIMITED}


def test_one_locked_account_doesnt_lock_another(client):
    a, _ = make_user(client, "brute")
    b, _ = make_user(client, "brute")
    for i in range(11):
        _login(client, _email(a), f"guess-{i}")
    assert _login(client, _email(a), "password123").status_code == 429
    assert _login(client, _email(b), "password123").status_code == 200


def test_a_successful_sign_in_clears_the_accounts_count(client):
    user, _ = make_user(client, "brute")
    for i in range(9):
        assert _login(client, _email(user), f"guess-{i}").status_code == 401
    assert _login(client, _email(user), "password123").status_code == 200
    assert [_login(client, _email(user), f"again-{i}").status_code for i in range(10)] == [401] * 10
    assert _login(client, _email(user), "again").status_code == 429


def test_address_limit_across_accounts_cannot_be_dodged_with_forwarded_for(client, monkeypatch):
    monkeypatch.setattr(settings, "login_max_attempts_per_ip", 3)
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1)
    real_ip = _fresh_ip()

    def attempt(spoof):
        return _login(client, f"stuff_{uuid.uuid4().hex[:8]}@example.com", "guess", f"{spoof}, {real_ip}").status_code

    assert [attempt(f"10.0.0.{i}") for i in range(3)] == [401] * 3
    assert attempt("1.2.3.4") == 429                            # a new left-hand entry changes nothing
    assert attempt("5.6.7.8") == 429
    other = _login(client, f"stuff_{uuid.uuid4().hex[:8]}@example.com", "guess", _fresh_ip())
    assert other.status_code == 401                             # someone else's address isn't blocked


def test_without_a_trusted_proxy_forwarded_for_is_ignored_entirely(client, monkeypatch):
    monkeypatch.setattr(settings, "login_max_attempts_per_ip", 3)
    monkeypatch.setattr(settings, "trusted_proxy_hops", 0)
    redis_client.delete(f"login:ip:{_digest('testclient')}")   # TestClient's connecting address
    codes = [_login(client, f"stuff_{uuid.uuid4().hex[:8]}@example.com", "guess", _fresh_ip()).status_code
             for _ in range(5)]
    redis_client.delete(f"login:ip:{_digest('testclient')}")
    assert codes == [401] * 3 + [429] * 2


def test_sign_in_still_works_when_redis_is_down(client, monkeypatch):
    user, _ = make_user(client, "brute")

    def down(*a, **kw):
        raise redis.ConnectionError("down")
    monkeypatch.setattr(rate_limit.redis_client, "pipeline", down)
    monkeypatch.setattr(rate_limit.redis_client, "delete", down)
    assert _login(client, _email(user), "password123").status_code == 200
