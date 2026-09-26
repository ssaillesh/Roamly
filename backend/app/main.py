"""TrekRank FastAPI application entrypoint."""
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from prometheus_fastapi_instrumentator import Instrumentator

from app.config import settings
from app.middleware.rate_limit import RateLimitMiddleware
from app.api import auth, users, waitlist, plan

app = FastAPI(
    title="TrekRank API",
    version="0.1.0",
    description="Travel logging, leaderboards, badges and share cards.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RateLimitMiddleware)

# Prometheus: per-route request count + latency histograms (p95 etc. via
# histogram_quantile in PromQL/Grafana), exposed at GET /metrics.
Instrumentator().instrument(app).expose(app, include_in_schema=False)

# Serve locally-stored media (share cards) when STORAGE_BACKEND=local.
if settings.storage_backend == "local":
    os.makedirs(settings.local_storage_dir, exist_ok=True)
    app.mount("/media", StaticFiles(directory=settings.local_storage_dir), name="media")

# Only what the web app uses is exposed. The travel-logging features (trips,
# friends, feed, leaderboards, challenges, share cards) have no screens yet, so
# their routes are switched off to keep the public API small; their code and
# tables stay. To bring one back, import it above and add it here.
P = settings.api_v1_prefix
for r in (auth, users, waitlist, plan):
    app.include_router(r.router, prefix=P)


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok", "app": settings.app_name, "env": settings.environment}


@app.get("/", tags=["meta"])
def root():
    return {"name": "TrekRank API", "docs": "/docs", "version": "0.1.0"}
