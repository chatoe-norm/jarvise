"""Position sizing for trade-assistant signals. Pure functions, no I/O."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from typing import Any

from jarvise_analyze.engine import CONFIDENCE_THRESHOLD

MAX_LEVERAGE = 5
CHASE_R = Decimal("0.3")
TARGET_R = Decimal("2")
BANGKOK = timezone(timedelta(hours=7), name="Asia/Bangkok")
# Assist cron: 25 0,4,8,12,16,20 Asia/Bangkok
ASSIST_HOURS = (0, 4, 8, 12, 16, 20)
ASSIST_MINUTE = 25
SAME_IDEA_MS = 24 * 3_600_000
ANALYSIS_MAX_AGE_MS = int(8.5 * 3_600_000)


@dataclass(frozen=True)
class SizedSignal:
    symbol: str
    side: str
    entry: Decimal
    stop: Decimal
    target: Decimal
    qty: Decimal
    notional: Decimal
    leverage: int
    loss_at_stop: Decimal
    risk_pct: Decimal
    r_distance: Decimal
    leverage_capped: bool
    uncapped_leverage: int
    analysis_id: str | None


def floor_to_step(qty: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("step size must be positive")
    steps = (qty / step).to_integral_value(rounding=ROUND_DOWN)
    return steps * step


def passes_signal_floor(analysis: dict[str, Any]) -> bool:
    """Same effective gate as stored analyzer rows for the approval queue (0.55)."""
    action = str(analysis.get("action") or "").lower()
    if action not in {"long", "short"}:
        return False
    conf = float(analysis.get("confidence_score") or 0.0)
    if conf < CONFIDENCE_THRESHOLD:
        return False
    if analysis.get("invalidation_price") is None:
        return False
    return True


def size_signal(
    *,
    symbol: str,
    side: str,
    entry: Decimal,
    stop: Decimal,
    balance: Decimal,
    risk_pct: Decimal,
    step_size: Decimal,
    analysis_id: str | None = None,
) -> SizedSignal | None:
    """Return sized levels, or None when stop is invalid / qty rounds to zero."""
    side_l = side.lower()
    if side_l not in {"long", "short"}:
        return None
    if entry <= 0 or balance <= 0 or risk_pct <= 0:
        return None
    if side_l == "long" and stop >= entry:
        return None
    if side_l == "short" and stop <= entry:
        return None
    r = abs(entry - stop)
    if r == 0:
        return None
    risk_usdt = balance * (risk_pct / Decimal("100"))
    raw_qty = risk_usdt / r
    qty = floor_to_step(raw_qty, step_size)
    if qty <= 0:
        return None
    notional = qty * entry
    uncapped = int(math.ceil(float(notional / balance)))
    leverage_capped = uncapped > MAX_LEVERAGE
    if leverage_capped:
        max_notional = balance * Decimal(MAX_LEVERAGE)
        qty = floor_to_step(max_notional / entry, step_size)
        if qty <= 0:
            return None
        notional = qty * entry
        leverage = MAX_LEVERAGE
    else:
        leverage = max(1, uncapped)
    loss = qty * r
    if side_l == "long":
        target = entry + TARGET_R * r
    else:
        target = entry - TARGET_R * r
    return SizedSignal(
        symbol=symbol.upper(),
        side=side_l,
        entry=entry,
        stop=stop,
        target=target,
        qty=qty,
        notional=notional,
        leverage=leverage,
        loss_at_stop=loss,
        risk_pct=risk_pct,
        r_distance=r,
        leverage_capped=leverage_capped,
        uncapped_leverage=uncapped,
        analysis_id=analysis_id,
    )


def chase_exceeded(*, side: str, entry: Decimal, stop: Decimal, mark: Decimal) -> bool:
    """True when mark has moved more than 0.3R from entry toward the target."""
    r = abs(entry - stop)
    if r == 0:
        return True
    limit = CHASE_R * r
    side_l = side.lower()
    if side_l == "long":
        return mark > entry + limit
    if side_l == "short":
        return mark < entry - limit
    return True


def next_4h_close_ms(now_ms: int) -> int:
    """Next UTC 4h boundary (00/04/08/12/16/20). Kept for tests; assist uses next_assist_run_ms."""
    four_h = 4 * 3_600_000
    bucket = (int(now_ms) // four_h) * four_h
    nxt = bucket + four_h
    if nxt <= int(now_ms):
        nxt += four_h
    return nxt


def next_assist_run_ms(now_ms: int) -> int:
    """Next Asia/Bangkok assist slot at :25 on hours 0/4/8/12/16/20, strictly after now."""
    local = datetime.fromtimestamp(int(now_ms) / 1000.0, tz=BANGKOK)
    for day_offset in (0, 1):
        day = (local + timedelta(days=day_offset)).date()
        for hour in ASSIST_HOURS:
            candidate = datetime(
                day.year,
                day.month,
                day.day,
                hour,
                ASSIST_MINUTE,
                tzinfo=BANGKOK,
            )
            cand_ms = int(candidate.timestamp() * 1000)
            if cand_ms > int(now_ms):
                return cand_ms
    # Fallback: first slot two days out (should be unreachable).
    day = (local + timedelta(days=2)).date()
    candidate = datetime(day.year, day.month, day.day, ASSIST_HOURS[0], ASSIST_MINUTE, tzinfo=BANGKOK)
    return int(candidate.timestamp() * 1000)


def analysis_is_fresh(analysis: dict[str, Any] | None, *, now_ms: int) -> bool:
    if analysis is None:
        return False
    raw = analysis.get("timestamp")
    if raw is None:
        return False
    age = int(now_ms) - int(raw)
    return 0 <= age <= ANALYSIS_MAX_AGE_MS
