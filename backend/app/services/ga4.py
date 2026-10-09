"""Google Analytics 4 Data API, read-only, for the admin dashboard's traffic,
funnel and geography sections.

Plain REST over httpx: the service-account JWT is signed with python-jose
(already a dependency), which keeps Google's much heavier gRPC client off the
512 MB instance. Reports are cached in Redis for 15 minutes.
"""
import hashlib
import json
import time

import httpx
from jose import jwt

from app.config import settings
from app.redis_client import redis_client

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_SCOPE = "https://www.googleapis.com/auth/analytics.readonly"
_CACHE_TTL = 15 * 60
_token: dict = {"value": None, "exp": 0.0}


class GA4Error(Exception):
    pass


def configured() -> bool:
    return bool(settings.ga4_property_id and settings.ga4_credentials_json)


def _access_token() -> str:
    if _token["value"] and _token["exp"] - 60 > time.time():
        return _token["value"]
    try:
        creds = json.loads(settings.ga4_credentials_json)
        now = int(time.time())
        assertion = jwt.encode(
            {"iss": creds["client_email"], "scope": _SCOPE, "aud": _TOKEN_URL,
             "iat": now, "exp": now + 3600},
            creds["private_key"], algorithm="RS256")
    except (ValueError, KeyError) as e:
        raise GA4Error(f"GA4_CREDENTIALS_JSON isn't a valid service-account key ({e})")
    r = httpx.post(_TOKEN_URL, timeout=10.0, data={
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion})
    if r.status_code != 200:
        raise GA4Error(f"Google sign-in failed: HTTP {r.status_code} {r.text[:200]}")
    body = r.json()
    _token.update(value=body["access_token"], exp=time.time() + body.get("expires_in", 3600))
    return _token["value"]


def run_report(*, dimensions: list[str], metrics: list[str], days: int,
               limit: int = 25, event_filter: list[str] | None = None) -> list[dict]:
    """One runReport call → [{dimension: value, ..., metric: number, ...}]."""
    if not configured():
        raise GA4Error("Google Analytics isn't connected")
    body = {
        "dateRanges": [{"startDate": f"{days}daysAgo", "endDate": "today"}],
        "dimensions": [{"name": d} for d in dimensions],
        "metrics": [{"name": m} for m in metrics],
        "limit": limit,
        "orderBys": [{"metric": {"metricName": metrics[0]}, "desc": True}],
    }
    if event_filter:
        body["dimensionFilter"] = {"filter": {
            "fieldName": "eventName", "inListFilter": {"values": event_filter}}}
    key = "ga4:" + hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()
    try:
        cached = redis_client.get(key)
        if cached:
            return json.loads(cached)
    except Exception:
        pass
    r = httpx.post(
        f"https://analyticsdata.googleapis.com/v1beta/properties/{settings.ga4_property_id}:runReport",
        json=body, headers={"Authorization": f"Bearer {_access_token()}"}, timeout=20.0)
    if r.status_code != 200:
        raise GA4Error(f"GA4 report failed: HTTP {r.status_code} {r.text[:200]}")
    data = r.json()
    rows = []
    for row in data.get("rows", []):
        out = {d: v["value"] for d, v in zip(dimensions, row.get("dimensionValues", []))}
        for m, v in zip(metrics, row.get("metricValues", [])):
            out[m] = float(v["value"])
        rows.append(out)
    try:
        redis_client.setex(key, _CACHE_TTL, json.dumps(rows))
    except Exception:
        pass
    return rows
