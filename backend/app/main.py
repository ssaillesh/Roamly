"""Roamly FastAPI application entrypoint."""
import asyncio
import contextlib
import hmac
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from prometheus_fastapi_instrumentator import Instrumentator
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.middleware.auth import get_current_user, require_admin
from app.middleware.rate_limit import RateLimitMiddleware
from app.middleware.request_stats import RequestStatsMiddleware
from app.api import admin, auth, nightlife, users, waitlist, plan
from app.services import analytics


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Saves request stats + outside-API counts every minute and runs the daily
    # cleanup (services/analytics.py). The last minute is flushed on shutdown.
    task = asyncio.create_task(analytics.stats_loop())
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await asyncio.to_thread(analytics.flush_stats)


app = FastAPI(
    title="Roamly API",
    version="0.1.0",
    description="AI outing planner: accounts, taste profiles, plans and live events.",
    lifespan=lifespan,
)

app.add_middleware(RateLimitMiddleware)
app.add_middleware(RequestStatsMiddleware)
# Added last, so it runs first: preflights are answered before the rate limit, and
# a 429 still carries the headers the page needs to read it.
# Only the web app's own origin(s) (config.cors_origins; localhost too outside
# production). Sign-in is a bearer token in a header, never a cookie, so no
# credentialed cross-origin requests.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins(),
    allow_origin_regex=None if settings.environment == "production" else r"http://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)

# Prometheus: per-route request count + latency histograms (p95 etc. via
# histogram_quantile in PromQL/Grafana). GET /metrics is admin-only, or open to a
# scraper holding METRICS_TOKEN.
Instrumentator().instrument(app)


@app.get("/metrics", include_in_schema=False)
def metrics(request: Request, db: Session = Depends(get_db)):
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not (settings.metrics_token and hmac.compare_digest(token, settings.metrics_token)):
        # Not the scraper's token, so it has to be an admin's access token.
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token) if token else None
        require_admin(get_current_user(creds, db))
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# Only what the web app uses is exposed. The travel-logging features (trips,
# friends, feed, leaderboards, challenges, share cards) have no screens yet, so
# their routes are switched off to keep the public API small; their code and
# tables stay. To bring one back, import it above and add it here.
P = settings.api_v1_prefix
for r in (auth, users, waitlist, plan, nightlife, admin):
    app.include_router(r.router, prefix=P)


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok", "app": settings.app_name, "env": settings.environment}


@app.get("/", tags=["meta"])
def root():
    return {"name": "TrekRank API", "docs": "/docs", "version": "0.1.0"}
