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
import re
import time

import httpx

from app.config import settings
from app.services import usage

log = logging.getLogger(__name__)

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


def health() -> dict:
    """The latest success and failure, for the admin dashboard."""
    return {"model": settings.llm_model, **usage.health("llm")}


def _retry_after(resp) -> float | None:
    """Seconds Groq asks us to wait on a 429 ("Please try again in 3.27s")."""
    m = re.search(r"try again in (?:(\d+)m)?(\d+(?:\.\d+)?)s", resp.text or "")
    if m:
        return int(m.group(1) or 0) * 60 + float(m.group(2))
    try:
        return float(resp.headers.get("retry-after"))
    except (TypeError, ValueError):
        return None


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
    free-tier blocker says the daily allowance is used up). A short per-minute
    rate limit (Groq's free tier: 8,000 tokens a minute) is waited out once."""
    if not available() or not usage.allow("llm"):
        return None
    payload = _payload(messages, temperature, max_tokens)
    for attempt in range(2):
        try:
            resp = _post(payload, timeout)
        except httpx.HTTPError as e:
            return _failed(f"{type(e).__name__}: {str(e)[:200]}")
        wait = _retry_after(resp) if resp.status_code == 429 else None
        if attempt == 0 and wait is not None and wait <= 6:
            usage.record("llm", ok=False, error=f"HTTP 429, retried after {wait:.1f}s: {resp.text[:200]}")
            time.sleep(wait + 0.25)
            continue
        break
    if resp.status_code != 200:
        return _failed(f"HTTP {resp.status_code}: {resp.text[:240]}")
    try:
        body = resp.json()
        content = body["choices"][0]["message"].get("content")
    except (KeyError, ValueError, IndexError, TypeError) as e:
        return _failed(f"unreadable reply: {type(e).__name__}")
    if not content:
        return _failed("empty reply (the model may have used its whole token allowance thinking)")
    usage.record("llm", ok=True)
    usage.record_tokens("llm", (body.get("usage") or {}).get("total_tokens") or 0)
    return content


def _failed(why: str) -> None:
    log.warning("LLM call failed (%s): %s", settings.llm_model, why)
    usage.record("llm", ok=False, error=why)
    return None


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
