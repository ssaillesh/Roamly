"""Application configuration loaded from environment variables."""
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Core
    app_name: str = "Sway"
    api_v1_prefix: str = "/api/v1"
    environment: str = "development"

    # Database — accounts only (the planner itself persists nothing).
    database_url: str = "postgresql+psycopg2://saillesh@localhost:5432/sway"

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

    # JWT
    jwt_secret: str = "dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24  # 1 day
    refresh_token_expire_minutes: int = 60 * 24 * 30  # 30 days

    # Geocoding (Nominatim / OpenStreetMap — free, no key)
    nominatim_url: str = "https://nominatim.openstreetmap.org/search"
    geocode_user_agent: str = "Sway/0.1 (mvp; contact dev@sway.app)"
    geocode_cache_ttl: int = 60 * 60 * 24 * 30  # 30 days

    # Email (SendGrid Web API). Leave key blank to disable sending — the
    # password-reset endpoint then returns the token directly (dev fallback).
    sendgrid_api_key: str = ""
    email_from: str = ""  # must be a SendGrid-verified sender (Single Sender or domain)
    email_from_name: str = "Sway"
    # Where the web UI is hosted; used to build the reset link in emails.
    frontend_base_url: str = "http://127.0.0.1:8080"

    # --- Itinerary planner ("Wander") ---------------------------------------
    # LLM via any OpenAI-compatible endpoint. Works with Groq or Google Gemini's
    # OpenAI-compatible API (both free-tier). Leave llm_api_key blank to run the
    # deterministic rule-based planner (no LLM, still functional).
    #   Groq:   base=https://api.groq.com/openai/v1        model=llama-3.3-70b-versatile
    #   Gemini: base=https://generativelanguage.googleapis.com/v1beta/openai  model=gemini-2.0-flash
    llm_base_url: str = "https://api.groq.com/openai/v1"
    llm_api_key: str = ""
    llm_model: str = "llama-3.3-70b-versatile"

    # Yelp Fusion (free tier: no card, ~500 calls/day). Blank → OSM only.
    yelp_api_key: str = ""
    # Foursquare Places — venue `popularity` (foot-traffic trendiness signal)
    foursquare_api_key: str = ""

    # Ticketmaster Discovery (free: concerts, sports, comedy). Use the Consumer Key.
    ticketmaster_api_key: str = ""

    # Google Places API (New) — lets the planner search Google Maps for
    # restaurants/activities (ratings, prices, photos). Blank → Yelp/FSQ/OSM only.
    google_places_api_key: str = ""

    # Rate limiting (requests per window per client)
    rate_limit_requests: int = 120
    rate_limit_window_seconds: int = 60


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
