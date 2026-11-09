"""Read-only digest freshness and OpenRouter credit checks for ingest health.

Thresholds are constants. The OpenRouter key is never logged, stored, or returned.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import httpx

from jarvise_ingest.db import set_paper_account_value

logger = logging.getLogger(__name__)

DIGEST_STALE_AFTER_H = 26.0
OPENROUTER_LOW_USD = 2.0
OPENROUTER_TIMEOUT_S = 10.0
OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"

DIGEST_STALE_KEY = "ingest_digest_stale"
OPENROUTER_STATE_KEY = "ingest_openrouter_state"

_STATE_CLEAR = 0.0
_STATE_LOW = 1.0
_STATE_ERROR = 2.0


def digest_status(directory: Path, *, now_s: float | None = None) -> dict[str, Any]:
    """Newest ``*.md`` in ``directory`` by mtime. Missing dir or files is stale."""
    now = time.time() if now_s is None else now_s
    newest: str | None = None
    age_h: float | None = None
    try:
        if directory.is_dir():
            files = [path for path in directory.iterdir() if path.is_file() and path.suffix == ".md"]
            if files:
                latest = max(files, key=lambda path: (path.stat().st_mtime, path.name))
                newest = latest.name
                age_h = (now - latest.stat().st_mtime) / 3600.0
    except OSError as exc:
        logger.warning("digest dir unread type=%s", type(exc).__name__)
        return {"digest_newest": None, "digest_age_h": None, "digest_stale": True}
    stale = newest is None or age_h is None or age_h > DIGEST_STALE_AFTER_H
    return {
        "digest_newest": newest,
        "digest_age_h": None if age_h is None else round(age_h, 2),
        "digest_stale": stale,
    }


def fetch_openrouter_credit(
    api_key: str,
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """GET ``/api/v1/key``. A failed read is an error, not a low balance."""
    key = (api_key or "").strip()
    if not key:
        return {"openrouter_remaining": None, "openrouter_low": False, "openrouter_error": "no_key"}
    headers = {"Authorization": f"Bearer {key}"}
    try:
        if client is None:
            response = httpx.get(OPENROUTER_KEY_URL, headers=headers, timeout=OPENROUTER_TIMEOUT_S)
        else:
            response = client.get(OPENROUTER_KEY_URL, headers=headers)
    except Exception as exc:  # noqa: BLE001 — a credit read must not fail ingest health
        logger.warning("openrouter key status unread type=%s", type(exc).__name__)
        return {
            "openrouter_remaining": None,
            "openrouter_low": False,
            "openrouter_error": type(exc).__name__,
        }
    if response.status_code != 200:
        logger.warning("openrouter key status unread status=%s", response.status_code)
        return {
            "openrouter_remaining": None,
            "openrouter_low": False,
            "openrouter_error": str(response.status_code),
        }
    try:
        body = response.json()
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict) or "limit_remaining" not in data:
            raise ValueError("missing limit_remaining")
        remaining = data.get("limit_remaining")
    except Exception:  # noqa: BLE001 — bad payload is an error state, not low credit
        logger.warning("openrouter key status unread type=bad_json")
        return {"openrouter_remaining": None, "openrouter_low": False, "openrouter_error": "bad_json"}
    if remaining is None:
        return {"openrouter_remaining": None, "openrouter_low": False, "openrouter_error": None}
    try:
        remaining_f = float(remaining)
    except (TypeError, ValueError):
        logger.warning("openrouter key status unread type=bad_json")
        return {"openrouter_remaining": None, "openrouter_low": False, "openrouter_error": "bad_json"}
    return {
        "openrouter_remaining": remaining_f,
        "openrouter_low": remaining_f < OPENROUTER_LOW_USD,
        "openrouter_error": None,
    }


def _read_flag(conn: Any, key: str) -> float | None:
    row = conn.execute("SELECT value FROM paper_account WHERE key=?", (key,)).fetchone()
    if row is None or row[0] is None:
        return None
    return float(row[0])


def _write_flag(conn: Any, key: str, value: float) -> None:
    set_paper_account_value(conn, key, value)
    conn.commit()


def digest_edge_alerted(conn: Any, stale_now: bool) -> str | None:
    """Return 'stale' or 'ok' when a Telegram edge should fire; else None."""
    prev = _read_flag(conn, DIGEST_STALE_KEY)
    prev_stale = None if prev is None else prev >= 0.5
    alerted: str | None = None
    if stale_now and prev_stale is not True:
        alerted = "stale"
    elif not stale_now and prev_stale is True:
        alerted = "ok"
    _write_flag(conn, DIGEST_STALE_KEY, 1.0 if stale_now else 0.0)
    return alerted


def _state_code(value: float | None) -> int | None:
    if value is None:
        return None
    return int(round(value))


def openrouter_edge_alerted(
    conn: Any, *, low: bool, error: str | None, remaining: float | None
) -> str | None:
    """Return 'low', 'error', or 'ok' on a state change. Recovery only when remaining >= 2."""
    prev = _state_code(_read_flag(conn, OPENROUTER_STATE_KEY))
    if error:
        state_now = "error"
        stored = _STATE_ERROR
    elif low:
        state_now = "low"
        stored = _STATE_LOW
    elif remaining is not None and remaining >= OPENROUTER_LOW_USD:
        state_now = "ok"
        stored = _STATE_CLEAR
    else:
        state_now = "unknown"
        stored = _STATE_CLEAR
    alerted: str | None = None
    if state_now == "low" and prev != 1:
        alerted = "low"
    elif state_now == "error" and prev != 2:
        alerted = "error"
    elif state_now == "ok" and prev in {1, 2}:
        alerted = "ok"
    _write_flag(conn, OPENROUTER_STATE_KEY, stored)
    return alerted
