"""The AI client: the right request for reasoning models, failures recorded for
the dashboard (not swallowed silently), and the daily token cap."""
import json

import httpx
import pytest

from app.config import settings
from app.redis_client import redis_client
from app.services import llm, usage


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.text = json.dumps(body)
        self.request = httpx.Request("POST", "https://api.example/chat/completions")

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("bad", request=self.request, response=self)


@pytest.fixture
def ai(monkeypatch):
    """A configured client with a fresh failure record and token count."""
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_model", "openai/gpt-oss-120b")
    monkeypatch.setattr(settings, "free_tier_only", True)
    keys = [llm._LAST_ERROR, llm._LAST_OK, usage._token_key("llm"),
            f"quota:llm:{usage._period_key('day')[0]}"]
    saved = {k: redis_client.get(k) for k in keys}
    redis_client.delete(*keys)
    sent = []

    def reply(status, body):
        def post(url, json=None, **kw):
            sent.append(json)
            return _Resp(status, body)
        monkeypatch.setattr(httpx, "post", post)
    yield reply, sent
    redis_client.delete(*keys)
    for k, v in saved.items():
        if v is not None:
            redis_client.set(k, v)


def test_reasoning_model_request(ai):
    reply, sent = ai
    reply(200, {"choices": [{"message": {"content": "ok"}}], "usage": {"total_tokens": 321}})
    assert llm.chat([{"role": "user", "content": "hi"}], max_tokens=500) == "ok"
    body = sent[0]
    assert body["model"] == "openai/gpt-oss-120b"
    assert body["reasoning_effort"] == "low" and body["include_reasoning"] is False
    assert body["max_tokens"] == 1500                     # room for the thinking
    assert usage.tokens_used("llm") == 321
    assert llm.health()["failing"] is False


def test_retired_model_is_reported_not_swallowed(ai):
    reply, _ = ai
    reply(404, {"error": {"message": "The model `llama-3.3-70b-versatile` does not exist"}})
    assert llm.chat([{"role": "user", "content": "hi"}]) is None
    h = llm.health()
    assert h["failing"] and "404" in h["last_error"]["error"] and "does not exist" in h["last_error"]["error"]


def test_empty_reply_counts_as_a_failure(ai):
    reply, _ = ai
    reply(200, {"choices": [{"message": {"content": ""}}]})
    assert llm.chat([{"role": "user", "content": "hi"}]) is None
    assert "empty reply" in llm.health()["last_error"]["error"]


def test_daily_token_cap_stops_calls(ai):
    reply, sent = ai
    reply(200, {"choices": [{"message": {"content": "ok"}}]})
    usage.record_tokens("llm", usage.TOKEN_CAPS["llm"][1])
    assert llm.chat([{"role": "user", "content": "hi"}]) is None
    assert sent == []                                     # never reached Groq
