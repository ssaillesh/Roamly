"""Application configuration loaded from environment variables."""
from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_JWT_SECRET = "dev-secret-change-me"
# Values that are public (in this repo's code, .env.example, docker-compose.yml), so
# anyone could sign a login with them.
INSECURE_JWT_SECRETS = {DEV_JWT_SECRET}


class ConfigError(RuntimeError):
    """The settings aren't safe to run with. Messages never include secret values."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Core
    app_name: str = "Rova"
    api_v1_prefix: str = "/api/v1"
    environment: str = "development"     # "production" on the live server (see check_production)

    @field_validator("environment", mode="before")
    @classmethod
    def _normalise_environment(cls, v):
        # "Production " from a dashboard must still count as production everywhere.
        return v.strip().lower() if isinstance(v, str) else v

    # Database
    database_url: str = "postgresql+psycopg2://saillesh@localhost:5432/trekrank"

    # Redis (broker + cache + leaderboards + rate limiting)
    redis_url: str = "redis://localhost:6379/0"

    @field_validator("database_url", "redis_url", mode="before")
    @classmethod
    def _strip_urls(cls, v: str) -> str:
        # Env vars pasted via dashboards can carry stray whitespace/newlines,
        # which corrupt the connection (e.g. db name becomes "railway\n").
        if not isinstance(v, str):
            return v
        v = v.strip()
        # Managed Postgres (Render/Heroku/Railway) hands out postgres:// or
        # postgresql:// with no driver; SQLAlchemy needs one. Normalise it so the
        # same code runs on any host without per-platform tweaks.
        for scheme in ("postgres://", "postgresql://"):
            if v.startswith(scheme):
                return "postgresql+psycopg2://" + v[len(scheme):]
        return v

    # JWT. The default is for local development only: production refuses to start
    # with it (check_production). Set JWT_SECRET to a long random value there.
    jwt_secret: str = DEV_JWT_SECRET
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24  # 1 day
    refresh_token_expire_minutes: int = 60 * 24 * 30  # 30 days

    # Geocoding (Nominatim / OpenStreetMap — free, no key)
    nominatim_url: str = "https://nominatim.openstreetmap.org/search"
    geocode_user_agent: str = "TrekRank/0.1 (mvp; contact dev@trekrank.app)"
    geocode_cache_ttl: int = 60 * 60 * 24 * 30  # 30 days

    # Email (Resend, resend.com). Leave the key blank to disable sending: outside
    # production the reset link then goes to the server log instead.
    resend_api_key: str = ""
    # The sender address: it must be on a domain verified in Resend (Resend → Domains).
    email_from: str = ""
    email_from_name: str = "Rova"

    @field_validator("resend_api_key", "email_from", "email_from_name", mode="before")
    @classmethod
    def _strip_email_settings(cls, v):
        # Pasted into a dashboard, these often pick up a stray space or newline.
        # (Never raise here: a validation error would print the API key.)
        return v.strip() if isinstance(v, str) else v
    # Where the web UI is hosted; used to build the reset link in emails, and its
    # origin is the one browsers may call this API from (CORS). In production it must
    # be the live https site (check_production).
    frontend_base_url: str = "http://127.0.0.1:8080"
    # More exact origins allowed to call the API from a browser, comma-separated
    # (e.g. "https://www.example.com"). Outside production, http://localhost and
    # http://127.0.0.1 on any port are allowed as well.
    cors_allow_origins: str = ""

    def cors_origins(self) -> list[str]:
        """Exact origins (scheme://host[:port]) browsers may call the API from."""
        listed = [o.strip().rstrip("/") for o in self.cors_allow_origins.split(",") if o.strip()]
        return list(dict.fromkeys([_origin_of(self.frontend_base_url), *listed]))

    # --- Itinerary planner ("Wander") ---------------------------------------
    # LLM via any OpenAI-compatible endpoint. Works with Groq or Google Gemini's
    # OpenAI-compatible API (both free-tier). Leave llm_api_key blank to run the
    # deterministic rule-based planner (no LLM, still functional).
    #   Groq:   base=https://api.groq.com/openai/v1        model=openai/gpt-oss-120b
    #   Gemini: base=https://generativelanguage.googleapis.com/v1beta/openai  model=gemini-2.0-flash
    # Groq retired llama-3.3-70b-versatile on 2026-08-16 (calls to it now 404);
    # check console.groq.com/docs/deprecations if every AI call starts failing.
    llm_base_url: str = "https://api.groq.com/openai/v1"
    llm_api_key: str = ""
    llm_model: str = "openai/gpt-oss-120b"

    # Yelp Fusion (free tier: no card, ~500 calls/day). Blank → OSM only.
    yelp_api_key: str = ""
    # Foursquare Places — venue `popularity` (foot-traffic trendiness signal)
    foursquare_api_key: str = ""

    # Ticketmaster Discovery (free: concerts, sports, comedy). Use the Consumer Key.
    ticketmaster_api_key: str = ""

    # SeatGeek Platform API (free developer access): club shows and concerts for the
    # Nightlife tab only. A credential: set it on the API server, never in the page.
    # Turn it on only once webui/seatgeek-logo.svg exists (their terms require the logo).
    seatgeek_client_id: str = ""

    # City of Toronto "Festivals & Events" open-data feed (free/public festivals).
    # Blank disables it. As of 2026-09 the endpoint answers 403 for everyone; the
    # client stays quiet until the City restores it (dataset 9201059e-… on
    # open.toronto.ca lists the current URL if it moves).
    toronto_events_feed_url: str = (
        "https://secure.toronto.ca/c3api_data/v2/DataAccess.svc/festivals_events/events"
        "?$format=application/json;odata.metadata=none&$skip=0&$top=5000"
    )

    # Google Places API (New) — lets the planner search Google Maps for
    # restaurants/activities (ratings, prices, photos). Blank → Yelp/FSQ/OSM only.
    google_places_api_key: str = ""

    # Free-tier blocker (services/usage.py): never call a metered API past its
    # free allowance. Only turn this off on purpose, once you're ready to pay.
    free_tier_only: bool = True

    # Admin dashboard.
    # Google Analytics 4: the numeric property ID (Admin → Property details) and
    # a service-account key (the whole JSON, pasted as one env var) whose email
    # has Viewer access on the property. Blank → the dashboard's traffic section
    # shows "connect Google Analytics" instead.
    ga4_property_id: str = ""
    ga4_credentials_json: str = ""
    # Lets a Prometheus scraper read /metrics with "Authorization: Bearer <token>".
    # Blank → /metrics is admin-only.
    metrics_token: str = ""

    # Rate limiting (requests per window per client: the signed-in user, else the address)
    rate_limit_requests: int = 120
    rate_limit_window_seconds: int = 60
    # Sign-in attempts (POST /auth/login) per window, counted before the password is
    # checked: per account (by email, any capitalisation; a successful sign-in clears
    # it) and, when above 0, per client address across all accounts. Keep the address
    # limit at 0 until TRUSTED_PROXY_HOPS is right, or every visitor shares the
    # proxy's address and one person could block sign-in for everyone.
    login_window_seconds: int = 900
    login_max_attempts_per_account: int = 10
    login_max_attempts_per_ip: int = 0
    # How many of our own proxies append to X-Forwarded-For in front of the API.
    # 0 = ignore the header (anyone can write it) and use the connecting address.
    trusted_proxy_hops: int = 0

    # Leaderboard cache TTL
    leaderboard_ttl_seconds: int = 300  # 5 minutes


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"}


def _origin_of(url: str) -> str:
    parts = urlsplit(url.strip())
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else url.strip()


def _bad_origin(origin: str, production: bool) -> str | None:
    """Why this can't be an allowed CORS origin, or None if it's fine."""
    parts = urlsplit(origin)
    if "*" in origin:
        return "a wildcard"
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.path or parts.query:
        return "not an exact origin like https://example.com"
    if production and (parts.scheme != "https" or parts.hostname in _LOCAL_HOSTS):
        return "not a public https origin"
    return None


def check_production(s: Settings) -> None:
    """Refuse to run with settings that would make it unsafe. Runs when the settings
    load, i.e. before the API or `alembic upgrade` can start."""
    production = s.environment == "production"
    if production and (not s.jwt_secret.strip() or s.jwt_secret.strip() in INSECURE_JWT_SECRETS):
        raise ConfigError("JWT_SECRET is missing, empty or the public development default. "
                          "Set it to a long random value (e.g. `openssl rand -hex 32`) before "
                          "starting in production.")
    for origin in s.cors_origins():
        why = _bad_origin(origin, production)
        if why:
            # Origins aren't secret, so naming the bad one is fine.
            raise ConfigError(f"CORS origin {origin!r} (FRONTEND_BASE_URL / CORS_ALLOW_ORIGINS) is {why}.")


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    check_production(s)
    return s


settings = get_settings()
