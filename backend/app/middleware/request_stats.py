"""Per-route request counts, status codes and latencies for the admin dashboard.

Plain ASGI (not BaseHTTPMiddleware) so it adds next to nothing per request.
Stats pile up in memory and services/analytics.flush_stats() saves them to
`health_snapshots` once a minute, which survives Render sleeping the server
(Prometheus' /metrics counters don't). A minute with no requests writes nothing.
"""
import threading
import time
from collections import defaultdict

_lock = threading.Lock()
_window: dict[str, list] = defaultdict(lambda: [[], 0, 0])   # route -> [latencies_ms, 4xx, 5xx]
_paths: dict = {}   # endpoint function -> route path, filled in lazily
SKIP_PATHS = frozenset({"/health"})   # never recorded (see RequestStatsMiddleware)


def _route_of(scope) -> str:
    endpoint = scope.get("endpoint")
    if endpoint is None:
        return "unmatched"          # 404 probes; one bucket so scanners can't flood the table
    path = _paths.get(endpoint)
    if path is None:
        app = scope.get("app")
        for r in getattr(app, "routes", []):
            if getattr(r, "endpoint", None) is endpoint:
                path = r.path
                break
        path = _paths.setdefault(endpoint, path or "unknown")
    return f"{scope.get('method', '')} {path}"


def record(route: str, status: int, ms: float) -> None:
    with _lock:
        bucket = _window[route]
        bucket[0].append(ms)
        if 400 <= status < 500:
            bucket[1] += 1
        elif status >= 500:
            bucket[2] += 1


def drain() -> dict[str, list]:
    """Take the stats gathered since the last drain."""
    global _window
    with _lock:
        out, _window = _window, defaultdict(lambda: [[], 0, 0])
    return dict(out)


class RequestStatsMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        # /health is the uptime pinger (cron-job.org, every 5 min), not a visitor. Counting
        # it would write a snapshot every few minutes and keep the database from ever
        # idling: Neon's free plan only sleeps after ~5 quiet minutes and has a monthly
        # compute allowance.
        if scope["type"] != "http" or scope.get("method") == "OPTIONS" or scope.get("path") in SKIP_PATHS:
            return await self.app(scope, receive, send)
        start = time.perf_counter()
        status = 500   # an exception that escapes the app is a 500

        async def send_wrapper(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            record(_route_of(scope), status, (time.perf_counter() - start) * 1000)
