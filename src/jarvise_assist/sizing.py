"""Position sizing for trade-assistant signals. Pure functions, no I/O."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from typing import Any

from jarvise_analyze.engine import CONFIDENCE_THRESHOLD

MAX_LEVERAGE = 5
CHASE_R = Decimal("0.3")
TARGET_R = Decimal("2")
MAX_OPEN_IDEAS = 2
MAX_PORTFOLIO_RISK_PCT = Decimal("4")
IDEA_LOOKBACK_MS = 4 * 3_600_000
BANGKOK = timezone(timedelta(hours=7), name="Asia/Bangkok")
# Assist cron: 25 0,4,8,12,16,20 Asia/Bangkok
ASSIST_HOURS = (0, 4, 8, 12, 16, 20)
ASSIST_MINUTE = 25
SAME_IDEA_MS = 24 * 3_600_000
ANALYSIS_MAX_AGE_MS = int(8.5 * 3_600_000)


@dataclass(frozen=True)
class ExistingIdea:
    symbol: str
    side: str
    risk_usdt: Decimal


@dataclass(frozen=True)
class SignalCandidate:
    sized: SizedSignal
    confidence: float
    symbol_rank: int


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


def below_min_notional(
    *,
    qty: Decimal,
    notional: Decimal,
    min_qty: Decimal,
    min_notional: Decimal,
) -> bool:
    """True when the sized trade is below exchange lot or notional floors."""
    if min_qty > 0 and qty < min_qty:
        return True
    if min_notional > 0 and notional < min_notional:
        return True
    return False


def position_risk_usdt(
    *,
    entry: Decimal,
    qty: Decimal,
    stop: Decimal | None,
    balance: Decimal,
    risk_pct: Decimal,
) -> Decimal:
    """USDT at risk for an open idea. Falls back to risk_pct of balance."""
    if stop is not None and entry > 0 and qty > 0:
        r = abs(entry - stop)
        if r > 0:
            return qty * r
    if balance > 0 and risk_pct > 0:
        return balance * (risk_pct / Decimal("100"))
    return Decimal("0")


def admit_signals(
    *,
    existing: list[ExistingIdea],
    candidates: list[SignalCandidate],
    balance: Decimal,
    max_ideas: int = MAX_OPEN_IDEAS,
    max_risk_pct: Decimal = MAX_PORTFOLIO_RISK_PCT,
) -> tuple[list[SignalCandidate], list[SignalCandidate]]:
    """Admit by confidence desc (symbol_rank asc tie-break). Reject the rest as risk_cap.

    Once one candidate fails a slot or risk budget, every lower-ranked candidate is
    rejected — a smaller later idea is not backfilled.
    """
    budget = balance * (max_risk_pct / Decimal("100")) if balance > 0 else Decimal("0")
    used = sum((e.risk_usdt for e in existing), Decimal("0"))
    slots = len(existing)
    ordered = sorted(candidates, key=lambda c: (-c.confidence, c.symbol_rank, c.sized.symbol))
    admitted: list[SignalCandidate] = []
    rejected: list[SignalCandidate] = []
    blocked = False
    for cand in ordered:
        if blocked:
            rejected.append(cand)
            continue
        next_used = used + cand.sized.loss_at_stop
        if slots + 1 > max_ideas or next_used > budget:
            rejected.append(cand)
            blocked = True
            continue
        admitted.append(cand)
        used = next_used
        slots += 1
    return admitted, rejected


def shared_leverage_needed(
    *,
    total_notional: Decimal,
    available_balance: Decimal,
) -> int:
    """Leverage so batch margin fits availableBalance with a 5% buffer."""
    if available_balance <= 0:
        return MAX_LEVERAGE + 1
    denom = available_balance * Decimal("0.95")
    if denom <= 0:
        return MAX_LEVERAGE + 1
    if total_notional <= 0:
        return 1
    return max(1, int(math.ceil(float(total_notional / denom))))


def _shrink_for_max_leverage(
    sized: SizedSignal,
    *,
    available_balance: Decimal,
    uncapped: int,
    max_leverage: int = MAX_LEVERAGE,
) -> SizedSignal:
    """Cap notional to availableBalance × max_leverage (existing leverage_capped rule)."""
    max_notional = available_balance * Decimal(max_leverage) if available_balance > 0 else Decimal("0")
    if max_notional <= 0 or sized.entry <= 0:
        return replace(
            sized,
            leverage=max_leverage,
            leverage_capped=True,
            uncapped_leverage=max(sized.uncapped_leverage, uncapped),
        )
    if sized.notional <= max_notional:
        return replace(
            sized,
            leverage=max_leverage,
            leverage_capped=True,
            uncapped_leverage=max(sized.uncapped_leverage, uncapped),
        )
    qty = max_notional / sized.entry
    notional = qty * sized.entry
    loss = qty * sized.r_distance
    return replace(
        sized,
        qty=qty,
        notional=notional,
        leverage=max_leverage,
        loss_at_stop=loss,
        leverage_capped=True,
        uncapped_leverage=max(sized.uncapped_leverage, uncapped),
    )


def assign_shared_leverage(
    admitted: list[SignalCandidate],
    *,
    available_balance: Decimal,
    max_leverage: int = MAX_LEVERAGE,
) -> tuple[list[SignalCandidate], list[SignalCandidate]]:
    """Assign one shared L after risk admission. Drop lowest-confidence on margin_cap.

    ``L = max(1, ceil(sum(notional) / (availableBalance * 0.95)))``.
    When ``L > max_leverage``, drop the lowest-confidence idea and recompute.
    A lone survivor that still needs more than ``max_leverage`` is shrunk with
    the ``leverage_capped`` rule (notional at most availableBalance × max).
    """
    survivors = list(admitted)
    dropped: list[SignalCandidate] = []
    while survivors:
        total = sum((c.sized.notional for c in survivors), Decimal("0"))
        leverage = shared_leverage_needed(
            total_notional=total,
            available_balance=available_balance,
        )
        if leverage <= max_leverage:
            return (
                [
                    SignalCandidate(
                        sized=replace(c.sized, leverage=leverage),
                        confidence=c.confidence,
                        symbol_rank=c.symbol_rank,
                    )
                    for c in survivors
                ],
                dropped,
            )
        if len(survivors) == 1:
            only = survivors[0]
            shrunk = _shrink_for_max_leverage(
                only.sized,
                available_balance=available_balance,
                uncapped=leverage,
                max_leverage=max_leverage,
            )
            return (
                [
                    SignalCandidate(
                        sized=shrunk,
                        confidence=only.confidence,
                        symbol_rank=only.symbol_rank,
                    )
                ],
                dropped,
            )
        # Drop lowest confidence; higher symbol_rank (worse) loses ties.
        victim = min(
            survivors,
            key=lambda c: (c.confidence, -c.symbol_rank, c.sized.symbol),
        )
        survivors = [c for c in survivors if c is not victim]
        dropped.append(victim)
    return [], dropped
