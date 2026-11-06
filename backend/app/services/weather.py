"""Today's weather, free and keyless: Open-Meteo first, MET Norway as the backup.

Used to make itineraries weather-aware: if it's going to rain (or freeze), the
planner favours indoor activities and the concierge mentions it. Weather is a nice
extra, never required: with no forecast the plan is made as on a dry day.

Free weather APIs count calls per server address, and a shared host (Render's free
tier) shares its address with other apps, so Open-Meteo can say 429 after a handful
of Rova's own calls. So:
- one forecast per ~10 km square (the weather is the same across downtown), kept 3 h;
- after a 429, Open-Meteo is left alone for a while (usage.pause) and MET Norway asked;
- if both fail, the last good forecast for that square (up to a day old) is used.
"""
import json

import httpx

from app.config import settings
from app.redis_client import redis_client
from app.services import usage

_URL = "https://api.open-meteo.com/v1/forecast"
_MET_URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
_TTL = 60 * 60 * 3          # forecasts don't change minute to minute
_STALE_TTL = 60 * 60 * 24   # the last good one, for when both services fail
_PAUSE_429 = 60 * 30        # after "too many requests" from a shared address
_FAILED_TTL = 60 * 5        # both failed: don't ask again on every stop of the next plan


def _user_agent() -> str:
    # MET Norway refuses anonymous clients: name the app and where it lives.
    return f"Rova/1.0 (+{settings.frontend_base_url.rstrip('/')})"


def _cell(lat: float, lng: float) -> str:
    return f"{round(lat, 1)}:{round(lng, 1)}"


def get_weather(lat: float, lng: float) -> dict | None:
    """Return {rainy, cold, temp_c, pop, summary} for today, or None when unknown."""
    cell = _cell(lat, lng)
    key, stale_key = f"wx:v2:{cell}", f"wx:last:{cell}"
    try:
        cached = redis_client.get(key)
    except Exception:
        cached = None
    if cached is not None:
        return json.loads(cached) or None
    lat, lng = round(lat, 2), round(lng, 2)
    out = _open_meteo(lat, lng) or _met_norway(lat, lng)
    try:
        if out:
            redis_client.setex(key, _TTL, json.dumps(out))
            redis_client.setex(stale_key, _STALE_TTL, json.dumps(out))
            return out
        stale = redis_client.get(stale_key)
        redis_client.setex(key, _FAILED_TTL, stale or "null")
        return json.loads(stale) if stale else None
    except Exception:
        return out


def _open_meteo(lat: float, lng: float) -> dict | None:
    if usage.paused("open_meteo") or not usage.allow("open_meteo"):
        return None
    try:
        with usage.counted("open_meteo"):
            r = httpx.get(_URL, params={
                "latitude": lat, "longitude": lng,
                "daily": "weathercode,temperature_2m_max,precipitation_probability_max",
                "timezone": "auto", "forecast_days": 1,
            }, headers={"User-Agent": _user_agent()}, timeout=5.0)
            r.raise_for_status()
        d = r.json()["daily"]
        code = (d["weathercode"] or [0])[0] or 0
        tmax = (d["temperature_2m_max"] or [None])[0]
        pop = (d["precipitation_probability_max"] or [0])[0] or 0
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429:
            usage.pause("open_meteo", _PAUSE_429, "429 Too Many Requests (shared server address); using MET Norway")
        return None
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
        return None
    rainy = code >= 51 or pop >= 55          # drizzle/rain/snow/showers codes start at 51
    return {"rainy": rainy, "cold": tmax is not None and tmax <= 2, "temp_c": tmax, "pop": pop,
            "summary": _summary(code, tmax, pop), "source": "open-meteo"}


_WET = ("rain", "sleet", "snow", "showers", "drizzle")


def _met_norway(lat: float, lng: float) -> dict | None:
    """The next 12 hours from MET Norway's compact forecast."""
    if usage.paused("met_norway") or not usage.allow("met_norway"):
        return None
    try:
        with usage.counted("met_norway"):
            r = httpx.get(_MET_URL, params={"lat": lat, "lon": lng},
                          headers={"User-Agent": _user_agent()}, timeout=6.0)
            r.raise_for_status()
        hours = r.json()["properties"]["timeseries"][:12]
        temps = [h["data"]["instant"]["details"].get("air_temperature") for h in hours]
        nxt = [h["data"].get("next_1_hours") or {} for h in hours]
        symbols = [(n.get("summary") or {}).get("symbol_code") or "" for n in nxt]
        rain_mm = sum((n.get("details") or {}).get("precipitation_amount") or 0 for n in nxt)
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (403, 429):
            usage.pause("met_norway", _PAUSE_429, f"HTTP {e.response.status_code}")
        return None
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
        return None
    temps = [t for t in temps if t is not None]
    if not temps:
        return None
    tmax = max(temps)
    wet_hours = sum(1 for s in symbols if any(w in s for w in _WET))
    snowy = sum(1 for s in symbols if "snow" in s or "sleet" in s) >= 2
    rainy = wet_hours >= 2 or rain_mm >= 1.0
    if snowy:
        cond = "snow"
    elif rainy:
        cond = "rain likely"
    elif symbols and all(s.startswith(("clearsky", "fair")) for s in symbols if s):
        cond = "clear"
    elif symbols and all(("cloudy" in s or s.startswith(("clearsky", "fair"))) for s in symbols if s):
        cond = "cloudy"
    else:
        cond = "mixed"
    return {"rainy": rainy, "cold": tmax <= 2, "temp_c": tmax,
            "pop": 80 if rainy else 0,       # MET gives amounts, not chances: a stand-in
            "summary": f"{cond}, {round(tmax)}°C", "source": "met-norway"}


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
