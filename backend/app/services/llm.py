"""Minimal OpenAI-compatible chat client.

Works with any OpenAI-compatible endpoint — Groq and Google Gemini both expose
one, and both have free tiers. Configure via LLM_BASE_URL / LLM_API_KEY / LLM_MODEL.
With no key, available() is False and callers fall back to non-LLM logic.
"""
import json

import httpx

from app.config import settings


def available() -> bool:
    return bool(settings.llm_api_key)


def ping() -> tuple[bool, str]:
    """Tiny live call to surface config/auth problems. Never returns the key."""
    if not available():
        return (False, "LLM_API_KEY is empty — no key loaded")
    try:
        r = httpx.post(
            f"{settings.llm_base_url.rstrip('/')}/chat/completions",
            json={"model": settings.llm_model,
                  "messages": [{"role": "user", "content": "reply with: ok"}],
                  "max_tokens": 5},
            headers={"Authorization": f"Bearer {settings.llm_api_key}"}, timeout=20.0)
        if r.status_code != 200:
            return (False, f"HTTP {r.status_code}: {r.text[:220]}")
        return (True, r.json()["choices"][0]["message"]["content"][:60])
    except Exception as e:
        return (False, f"{type(e).__name__}: {str(e)[:200]}")


def chat(messages: list[dict], *, temperature: float = 0.7,
         max_tokens: int = 1200, want_json: bool = False, timeout: float = 45.0) -> str | None:
    """Return the assistant's reply text, or None on any failure."""
    if not available():
        return None
    payload = {
        "model": settings.llm_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    # NOTE: we intentionally do NOT send response_format — Gemini's OpenAI-compat
    # layer can 400 on it. We instruct JSON in the prompt and parse defensively.
    try:
        resp = httpx.post(
            f"{settings.llm_base_url.rstrip('/')}/chat/completions",
            json=payload, timeout=timeout,
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except (httpx.HTTPError, KeyError, ValueError, IndexError):
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
