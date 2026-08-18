"""Sway API entrypoint — the itinerary planner."""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator

from app.config import settings
from app.middleware.rate_limit import RateLimitMiddleware
from app.api import auth, plan

app = FastAPI(
    title="Sway API",
    version="0.1.0",
    description="Chat-driven itinerary planning from real, open-right-now venues.",
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

P = settings.api_v1_prefix
for r in (auth, plan):
    app.include_router(r.router, prefix=P)


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok", "app": settings.app_name, "env": settings.environment}


@app.get("/", tags=["meta"])
def root():
    return {"name": "Sway API", "docs": "/docs", "version": "0.1.0"}
