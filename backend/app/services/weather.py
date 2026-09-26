"""Weather via Open-Meteo — completely free, no API key.

Used to make itineraries weather-aware: if it's going to rain (or freeze), the
planner favours indoor activities and the concierge mentions it. Cached briefly
in Redis so a planning session doesn't re-hit the API.
"""
import json

import httpx

from app.redis_client import redis_client

_URL = "https://api.open-meteo.com/v1/forecast"
_TTL = 60 * 60 * 2  # 2h — forecasts don't change minute to minute


def get_weather(lat: float, lng: float) -> dict | None:
    """Return {rainy, cold, temp_c, pop, summary} for today, or None on failure."""
    key = f"wx:{round(lat, 2)}:{round(lng, 2)}"
    cached = redis_client.get(key)
    if cached is not None:
        return json.loads(cached) or None
    try:
        r = httpx.get(_URL, params={
            "latitude": lat, "longitude": lng,
            "daily": "weathercode,temperature_2m_max,precipitation_probability_max",
            "timezone": "auto", "forecast_days": 1,
        }, timeout=5.0)
        r.raise_for_status()
        d = r.json()["daily"]
        code = (d["weathercode"] or [0])[0]
        tmax = (d["temperature_2m_max"] or [None])[0]
        pop = (d["precipitation_probability_max"] or [0])[0] or 0
    except (httpx.HTTPError, ValueError, KeyError, IndexError):
        return None

    rainy = code >= 51 or pop >= 55          # drizzle/rain/snow/showers codes start at 51
    cold = tmax is not None and tmax <= 2
    out = {
        "rainy": rainy, "cold": cold, "temp_c": tmax, "pop": pop,
        "summary": _summary(code, tmax, pop),
    }
    redis_client.setex(key, _TTL, json.dumps(out))
    return out


def _summary(code, tmax, pop) -> str:
    if code >= 71 and code <= 77:
        cond = "snow"
    elif code >= 51 or pop >= 55:
        cond = "rain likely"
    elif code in (0, 1):
        cond = "clear"
    elif code in (2, 3):
        cond = "cloudy"
    else:
        cond = "mixed"
    t = f"{round(tmax)}°C" if tmax is not None else ""
    return f"{cond}{', ' + t if t else ''}".strip(", ")
