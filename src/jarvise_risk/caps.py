"""Venue-agnostic paper/live risk caps. Paper enforcement only in this slice."""

from __future__ import annotations

import os
from dataclasses import dataclass


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


def engage_kill_switch(*, reason: str = "risk_cap_breach") -> bool:
    """Set jarvise:kill_switch=1 in Redis. Returns True if set."""
    url = os.environ.get("REDIS_URL")
    if not url:
        return False
    try:
        import redis

        client = redis.Redis.from_url(url, decode_responses=True)
        client.set("jarvise:kill_switch", "1")
        client.set("jarvise:kill_switch:reason", reason)
        return True
    except Exception:
        return False
