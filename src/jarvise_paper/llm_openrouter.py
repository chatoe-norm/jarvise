"""Minimal OpenRouter chat client that returns one JSON object.

Second-layer paper reviewer only — the model never sees exchange credentials and
nothing here places orders. One retry on transport/5xx, then fail (caller defers).
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_ATTEMPTS = 2


class OpenRouterError(RuntimeError):
    """Missing key, HTTP failure, timeout, or non-JSON-object content."""


def _parse_content(resp: httpx.Response) -> dict[str, Any]:
    try:
        content = resp.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise OpenRouterError("malformed response") from exc
    try:
        parsed = json.loads(_FENCE.sub("", str(content)).strip())
    except json.JSONDecodeError as exc:
        raise OpenRouterError("non-JSON content") from exc
    if not isinstance(parsed, dict):
        raise OpenRouterError("non-object JSON")
    return parsed


def chat_json(
    messages: list[dict[str, str]],
    *,
    model: str,
    timeout_s: float = 30.0,
    api_key: str | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    key = (api_key or os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if not key:
        raise OpenRouterError("missing OPENROUTER_API_KEY")
    body = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "HTTP-Referer": "https://github.com/chatoe-norm/jarvise",
        "X-Title": "Jarvise paper auto-decide",
    }
    own = client is None
    http = client or httpx.Client(timeout=timeout_s)
    last_error = "openrouter failed"
    try:
        for _ in range(_ATTEMPTS):
            try:
                resp = http.post(OPENROUTER_URL, json=body, headers=headers)
            except httpx.HTTPError as exc:
                last_error = f"transport: {type(exc).__name__}"
                continue
            if resp.status_code >= 500:
                last_error = f"status {resp.status_code}"
                continue
            if resp.status_code != 200:
                raise OpenRouterError(f"status {resp.status_code}")
            return _parse_content(resp)
    finally:
        if own:
            http.close()
    raise OpenRouterError(last_error)
