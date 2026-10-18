"""Owner quiet mode. Default off. Redis down reads as off so alerts still send."""

from __future__ import annotations

import os
from typing import Any

QUIET_MODE_KEY = "jarvise:notify:quiet_mode"


def _client():
    url = (os.environ.get("REDIS_URL") or "").strip()
    if not url:
        return None
    import redis

    return redis.Redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=2.0,
        socket_timeout=2.0,
    )


def quiet_mode_enabled() -> bool:
    """True only when the Redis flag is explicitly on. Missing Redis is off."""
    try:
        client = _client()
        if client is None:
            return False
        raw = client.get(QUIET_MODE_KEY)
    except Exception:  # noqa: BLE001 — a quiet-mode read must not drop an alert
        return False
    return str(raw or "").strip().lower() in {"1", "true", "on", "yes"}


def set_quiet_mode(enabled: bool) -> None:
    """Persist the flag. Raises when Redis cannot be written."""
    client = _client()
    if client is None:
        raise RuntimeError("REDIS_URL is unset")
    client.set(QUIET_MODE_KEY, "1" if enabled else "0")


def pending_alert_allowed(row: dict[str, Any], *, open_symbols: set[str]) -> bool:
    """Quiet mode keeps a FLAT card that closes an open paper position. Everything else waits."""
    if not quiet_mode_enabled():
        return True
    action = str(row.get("action") or "").strip().lower()
    symbol = str(row.get("symbol") or "").strip().upper()
    open_set = {str(sym).strip().upper() for sym in open_symbols}
    return action == "flat" and symbol in open_set
