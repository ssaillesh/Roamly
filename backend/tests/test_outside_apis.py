"""When a free outside service says no: weather falls back to MET Norway (then to the
last good forecast), and a service that refuses Rova is paused instead of being
asked again on every request."""
import httpx
import pytest

from app.config import settings
from app.redis_client import redis_client
from app.services import seatgeek, usage, weather

SPOT = (10.03, 10.04)       # a weather square nothing else in the tests uses


def _clean():
    for pattern in ("wx:v2:10.0:10.0", "wx:last:10.0:10.0", "api:paused:open_meteo", "api:paused:met_norway",
                    "api:paused:seatgeek", "sg:*"):
        for k in redis_client.scan_iter(pattern):
            redis_client.delete(k)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(usage, "allow", lambda p: True)
    _clean()
    yield
    _clean()


class _Resp:
    def __init__(self, status, body, url="https://example.test"):
        self.status_code, self._body = status, body
        self.request = httpx.Request("GET", url)
        self.text = str(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(f"{self.status_code}", request=self.request,
                                        response=httpx.Response(self.status_code, json=self._body, request=self.request))


OPEN_METEO = {"daily": {"weathercode": [61], "temperature_2m_max": [7.5], "precipitation_probability_max": [80]}}


def _met(symbol="lightrain", temp=6.0, mm=0.4):
    hour = {"data": {"instant": {"details": {"air_temperature": temp}},
                     "next_1_hours": {"summary": {"symbol_code": symbol}, "details": {"precipitation_amount": mm}}}}
    return {"properties": {"timeseries": [hour] * 12}}


def _serve(monkeypatch, answers):
    """answers: host → (status, body). Records every host asked."""
    asked = []

    def get(url, params=None, headers=None, **kw):
        host = url.split("/")[2]
        asked.append(host)
        assert headers and "Rova" in headers["User-Agent"]                 # MET Norway requires one
        status, body = answers[host]
        return _Resp(status, body, url)
    monkeypatch.setattr(weather.httpx, "get", get)
    return asked


def test_open_meteo_first(monkeypatch):
    asked = _serve(monkeypatch, {"api.open-meteo.com": (200, OPEN_METEO)})
    wx = weather.get_weather(*SPOT)
    assert wx["rainy"] and wx["source"] == "open-meteo" and asked == ["api.open-meteo.com"]
    assert weather.get_weather(SPOT[0] + 0.02, SPOT[1]) == wx and len(asked) == 1   # same square: cached


def test_a_429_pauses_open_meteo_and_asks_met_norway(monkeypatch):
    asked = _serve(monkeypatch, {"api.open-meteo.com": (429, {"reason": "Too many requests"}),
                                 "api.met.no": (200, _met())})
    wx = weather.get_weather(*SPOT)
    assert wx["source"] == "met-norway" and wx["rainy"] and wx["temp_c"] == 6.0
    assert usage.paused("open_meteo")
    redis_client.delete("wx:v2:10.0:10.0")
    weather.get_weather(*SPOT)
    assert asked == ["api.open-meteo.com", "api.met.no", "api.met.no"]        # Open-Meteo left alone


def test_met_norway_reads_a_dry_night(monkeypatch):
    _serve(monkeypatch, {"api.open-meteo.com": (500, {}), "api.met.no": (200, _met("clearsky_night", 1.0, 0))})
    wx = weather.get_weather(*SPOT)
    assert not wx["rainy"] and wx["cold"] and wx["summary"] == "clear, 1°C"


def test_both_down_uses_the_last_good_forecast(monkeypatch):
    _serve(monkeypatch, {"api.open-meteo.com": (200, OPEN_METEO)})
    good = weather.get_weather(*SPOT)
    redis_client.delete("wx:v2:10.0:10.0")                                    # the fresh copy has expired
    _serve(monkeypatch, {"api.open-meteo.com": (503, {}), "api.met.no": (503, {})})
    assert weather.get_weather(*SPOT) == good


def test_no_forecast_at_all_is_none_and_isnt_retried_every_stop(monkeypatch):
    asked = _serve(monkeypatch, {"api.open-meteo.com": (503, {}), "api.met.no": (503, {})})
    assert weather.get_weather(*SPOT) is None
    assert weather.get_weather(*SPOT) is None and len(asked) == 2              # remembered for a few minutes


def test_a_refused_seatgeek_id_pauses_it(monkeypatch):
    monkeypatch.setattr(settings, "seatgeek_client_id", "bad-id")
    calls = []

    def get(url, params=None, **kw):
        calls.append(url)
        return _Resp(403, {"status": 403, "message": "Forbidden: client is not active"}, url)
    monkeypatch.setattr(seatgeek.httpx, "get", get)
    assert seatgeek.search_events(43.65, -79.38) == []
    p = usage.paused("seatgeek")
    assert p and "client is not active" in p["reason"] and "bad-id" not in p["reason"]
    assert not seatgeek.available()
    assert seatgeek.search_events(43.66, -79.39) == [] and len(calls) == 1      # not asked again


def test_the_dashboard_shows_a_pause(client):
    from tests.conftest import auth_headers
    from tests.test_admin import make_admin
    usage.pause("seatgeek", 60, "HTTP 403: refused")
    _, admin = make_admin(client)
    rows = client.get("/api/v1/admin/apis", headers=auth_headers(admin)).json()["providers"]
    sg = next(r for r in rows if r["provider"] == "seatgeek")
    assert sg["paused"]["reason"] == "HTTP 403: refused"
    assert any(r["provider"] == "met_norway" for r in rows)
