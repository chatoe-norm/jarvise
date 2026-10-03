"""Venue-agnostic paper/live risk caps + fail-closed kill-switch access.

Kill-switch state lives in Redis. Reads are fail-closed: when the state cannot be
determined (no ``REDIS_URL`` in strict mode, driver missing, connection error) the
switch is reported as engaged so paper/live actions stop. Engaging alerts the owner.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from jarvise_notify import notify_kill_switch

logger = logging.getLogger(__name__)

KILL_SWITCH_KEY = "jarvise:kill_switch"
KILL_SWITCH_REASON_KEY = "jarvise:kill_switch:reason"
_TRUE = frozenset({"1", "true", "on", "yes"})
_REDIS_TIMEOUT_S = 3.0


class KillSwitchUnavailable(RuntimeError):
    """Kill-switch state could not be read or written (Redis unset or unreachable)."""


def _fenv(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# Conservative paper defaults ($10k starting equity).
DEFAULT_MAX_NOTIONAL = 2000.0
DEFAULT_MAX_DAILY_LOSS = 100.0
DEFAULT_DRAWDOWN_LOCK_PCT = 5.0


@dataclass(frozen=True)
class RiskCaps:
    max_notional_per_order: float
    max_daily_loss_usd: float
    drawdown_lock_pct: float

    def as_dict(self) -> dict[str, float]:
        return {
            "max_notional_per_order": self.max_notional_per_order,
            "max_daily_loss_usd": self.max_daily_loss_usd,
            "drawdown_lock_pct": self.drawdown_lock_pct,
        }


def load_risk_caps() -> RiskCaps:
    return RiskCaps(
        max_notional_per_order=_fenv(
            "JARVISE_MAX_NOTIONAL_PER_ORDER", DEFAULT_MAX_NOTIONAL
        ),
        max_daily_loss_usd=_fenv("JARVISE_MAX_DAILY_LOSS_USD", DEFAULT_MAX_DAILY_LOSS),
        drawdown_lock_pct=_fenv(
            "JARVISE_DRAWDOWN_LOCK_PCT", DEFAULT_DRAWDOWN_LOCK_PCT
        ),
    )


def estimated_notional(
    *,
    equity: float,
    size_pct_equity: float,
) -> float:
    return max(0.0, float(equity)) * (max(0.0, float(size_pct_equity)) / 100.0)


def check_caps(
    caps: RiskCaps,
    *,
    equity: float,
    starting_equity: float,
    size_pct_equity: float | None = None,
    action: str | None = None,
) -> str | None:
    """Return breach reason or None if OK."""
    start = float(starting_equity) if starting_equity else 0.0
    eq = float(equity)
    if start > 0:
        dd_pct = max(0.0, (start - eq) / start * 100.0)
        if dd_pct >= caps.drawdown_lock_pct:
            return (
                f"drawdown_lock: {dd_pct:.2f}% >= {caps.drawdown_lock_pct:.2f}%"
            )
        loss = start - eq
        if loss >= caps.max_daily_loss_usd:
            return (
                f"max_daily_loss: loss ${loss:.2f} >= ${caps.max_daily_loss_usd:.2f}"
            )

    act = (action or "").lower()
    if act in {"long", "short"} and size_pct_equity is not None:
        notional = estimated_notional(equity=eq, size_pct_equity=float(size_pct_equity))
        if notional > caps.max_notional_per_order:
            return (
                f"max_notional: ${notional:.2f} > ${caps.max_notional_per_order:.2f}"
            )
    return None


def _redis_client(url: str) -> Any:
    import redis

    return redis.Redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=_REDIS_TIMEOUT_S,
        socket_timeout=_REDIS_TIMEOUT_S,
    )


def kill_switch_state(*, strict: bool = True) -> dict[str, Any]:
    """Read the kill-switch. ``known`` is False when the state could not be determined.

    strict=True (services: jobs, web): unknown state → ``engaged`` True (fail closed).
    strict=False (local CLI without Redis): missing ``REDIS_URL`` → standalone, not
    engaged; a configured-but-unreachable Redis is still fail closed.
    """
    url = os.environ.get("REDIS_URL")
    if not url:
        return {
            "engaged": bool(strict),
            "known": False,
            "reason": None,
            "error": "REDIS_URL unset",
        }
    try:
        client = _redis_client(url)
        raw = client.get(KILL_SWITCH_KEY)
        engaged = str(raw or "0").strip().lower() in _TRUE
        reason = client.get(KILL_SWITCH_REASON_KEY) if engaged else None
        return {"engaged": engaged, "known": True, "reason": reason, "error": None}
    except Exception as exc:  # noqa: BLE001 — any failure to read → fail closed
        return {
            "engaged": True,
            "known": False,
            "reason": None,
            "error": f"{type(exc).__name__}: {exc}"[:200],
        }


def read_kill_switch(*, strict: bool = True) -> bool:
    """Boolean view of kill_switch_state(); unknown counts as engaged in strict mode."""
    return bool(kill_switch_state(strict=strict)["engaged"])


def engage_kill_switch(*, reason: str = "risk_cap_breach") -> bool:
    """Set jarvise:kill_switch=1 in Redis and alert the owner. Returns True if set.

    Alerts once per transition (not when already engaged) and always when the set
    fails, so a silent no-op can never pass for an engaged switch.
    """
    url = os.environ.get("REDIS_URL")
    if not url:
        logger.error("kill-switch engage failed: REDIS_URL unset (reason=%s)", reason)
        notify_kill_switch(reason, engaged=False, error="REDIS_URL unset")
        return False
    try:
        client = _redis_client(url)
        already = str(client.get(KILL_SWITCH_KEY) or "0").strip().lower() in _TRUE
        client.set(KILL_SWITCH_KEY, "1")
        client.set(KILL_SWITCH_REASON_KEY, reason)
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"[:200]
        logger.error("kill-switch engage failed: %s (reason=%s)", err, reason)
        notify_kill_switch(reason, engaged=False, error=err)
        return False
    if not already:
        notify_kill_switch(reason, engaged=True)
    return True


def engage_kill_switch_strict(*, reason: str = "risk_cap_breach") -> None:
    """Engage or raise KillSwitchUnavailable for callers that must not continue."""
    if not engage_kill_switch(reason=reason):
        raise KillSwitchUnavailable(f"kill-switch could not be engaged: {reason}")
