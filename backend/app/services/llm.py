"""Minimal OpenAI-compatible chat client.

Works with any OpenAI-compatible endpoint — Groq and Google Gemini both expose
one, and both have free tiers. Configure via LLM_BASE_URL / LLM_API_KEY / LLM_MODEL.
With no key, available() is False and callers fall back to non-LLM logic.

Failures don't raise (callers fall back to rule-based logic), but they are
logged and the latest one is kept in Redis so the admin dashboard can say why
the AI isn't answering — e.g. a retired model name 404ing on every call.
"""
import json
import logging
from datetime import datetime, timezone

import httpx

from app.config import settings
from app.redis_client import redis_client
from app.services import usage

log = logging.getLogger(__name__)

_LAST_ERROR = "llm:last_error"
_LAST_OK = "llm:last_ok"


def available() -> bool:
    return bool(settings.llm_api_key)


def _reasoning_model() -> bool:
    return "gpt-oss" in settings.llm_model


def _payload(messages: list[dict], temperature: float, max_tokens: int) -> dict:
    payload = {"model": settings.llm_model, "messages": messages,
               "temperature": temperature, "max_tokens": max_tokens}
    if _reasoning_model():
        # gpt-oss thinks before it answers. Keep the thinking short and out of the
        # reply, and leave room for it: it counts against max_tokens.
        payload.update(reasoning_effort="low", include_reasoning=False,
                       max_tokens=max_tokens + 1000)
    # NOTE: no response_format — Gemini's OpenAI-compat layer can 400 on it. We
    # instruct JSON in the prompt and parse defensively.
    return payload


def _note(key: str, value: dict | None = None) -> None:
    try:
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        redis_client.setex(key, 7 * 86400, json.dumps({"at": stamp, **(value or {})}))
    except Exception:
        pass


def _failed(why: str) -> None:
    log.warning("LLM call failed (%s): %s", settings.llm_model, why)
    _note(_LAST_ERROR, {"error": why[:300], "model": settings.llm_model})


def health() -> dict:
    """The latest success and failure, for the admin dashboard."""
    out = {"model": settings.llm_model}
    for key, name in ((_LAST_OK, "last_ok"), (_LAST_ERROR, "last_error")):
        try:
            raw = redis_client.get(key)
            out[name] = json.loads(raw) if raw else None
        except Exception:
            out[name] = None
    err, ok = out.get("last_error"), out.get("last_ok")
    out["failing"] = bool(err and (not ok or err["at"] > ok["at"]))
    return out


def _post(payload: dict, timeout: float) -> httpx.Response:
    return httpx.post(f"{settings.llm_base_url.rstrip('/')}/chat/completions", json=payload,
                      timeout=timeout, headers={"Authorization": f"Bearer {settings.llm_api_key}"})


def ping() -> tuple[bool, str]:
    """Tiny live call to surface config/auth problems. Never returns the key."""
    if not available():
        return (False, "LLM_API_KEY is empty — no key loaded")
    try:
        r = _post(_payload([{"role": "user", "content": "reply with: ok"}], 0, 5), 20.0)
        if r.status_code != 200:
            return (False, f"HTTP {r.status_code}: {r.text[:220]}")
        return (True, (r.json()["choices"][0]["message"]["content"] or "")[:60])
    except Exception as e:
        return (False, f"{type(e).__name__}: {str(e)[:200]}")


def chat(messages: list[dict], *, temperature: float = 0.7,
         max_tokens: int = 1200, want_json: bool = False, timeout: float = 45.0) -> str | None:
    """Return the assistant's reply text, or None on any failure (or when the
    free-tier blocker says the daily allowance is used up)."""
    if not available() or not usage.allow("llm"):
        return None
    try:
        with usage.counted("llm"):
            resp = _post(_payload(messages, temperature, max_tokens), timeout)
            if resp.status_code != 200:
                _failed(f"HTTP {resp.status_code}: {resp.text[:240]}")
                resp.raise_for_status()
            body = resp.json()
            content = body["choices"][0]["message"].get("content")
            if not content:
                _failed("empty reply (the model may have used its whole token allowance thinking)")
                raise ValueError("empty reply")
    except httpx.HTTPStatusError:
        return None                       # already noted with the server's message
    except (httpx.HTTPError, KeyError, ValueError, IndexError, TypeError) as e:
        if str(e) != "empty reply":
            _failed(f"{type(e).__name__}: {str(e)[:200]}")
        return None
    usage.record_tokens("llm", (body.get("usage") or {}).get("total_tokens") or 0)
    _note(_LAST_OK)
    return content


def chat_json(messages: list[dict], *, temperature: float = 0.4, timeout: float = 45.0) -> dict | None:
    """Chat expecting a JSON object reply; returns the parsed dict or None.

    Tolerates models that wrap JSON in prose or ```json fences.
    """
    raw = chat(messages, temperature=temperature, want_json=True, timeout=timeout)
    if not raw:
        return None
    raw = raw.strip()
    # strip code fences if present
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1] if "```" in raw[3:] else raw.strip("`")
        raw = raw[4:] if raw.lower().startswith("json") else raw
    # extract the outermost {...}
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end != -1:
        raw = raw[start:end + 1]
    try:
        return json.loads(raw)
    except ValueError:
        return None
