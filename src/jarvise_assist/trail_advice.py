"""Shared closed-1h trail advice for assist watch, menu, and move-stop tap."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from jarvise_assist.sizing import round_level_to_tick
from jarvise_paper.profit_lock import trail_stop_with_source

ONE_H_MS = 3_600_000
TrailSource = Literal["breakeven", "trail"]


def candle_extremes_after_entry(
    candles: list[dict[str, Any]],
    *,
    entry_time_ms: int,
    now_ms: int,
    bar_ms: int = ONE_H_MS,
) -> tuple[float | None, float | None]:
    """Best high/low of bars opened at or after entry and closed by now.

    Bars that opened before entry are excluded so pre-entry extremes cannot
    trip breakeven / 1R trail advice (same leak class as replay PR #179).
    """
    best_high: float | None = None
    best_low: float | None = None
    for row in candles:
        ts = int(row["timestamp"])
        close_ms = ts + int(bar_ms)
        if ts < int(entry_time_ms) or close_ms > int(now_ms):
            continue
        high = row.get("high")
        low = row.get("low")
        if high is not None:
            h = float(high)
            best_high = h if best_high is None else max(best_high, h)
        if low is not None:
            lo = float(low)
            best_low = lo if best_low is None else min(best_low, lo)
    return best_low, best_high


def current_trail_advice(
    *,
    side: str,
    entry: Decimal,
    initial_stop: Decimal,
    candles_1h: list[dict[str, Any]],
    entry_time_ms: int,
    now_ms: int,
    tick: Decimal,
) -> tuple[Decimal, TrailSource] | None:
    """Rounded trail/breakeven advice from closed 1h candles, or None.

    Uses the frozen initial stop as the R anchor and as ``stop_price`` for
    ``trail_stop_with_source`` (never the live mark as an extreme).
    """
    best_low, best_high = candle_extremes_after_entry(
        candles_1h,
        entry_time_ms=entry_time_ms,
        now_ms=now_ms,
        bar_ms=ONE_H_MS,
    )
    trail_pos = {
        "side": side,
        "entry_price": float(entry),
        "initial_stop_price": float(initial_stop),
        "stop_price": float(initial_stop),
    }
    trail = trail_stop_with_source(trail_pos, best_low=best_low, best_high=best_high)
    if trail is None:
        return None
    suggested, source = trail
    if source not in {"breakeven", "trail"}:
        return None
    rounded = round_level_to_tick(Decimal(str(suggested)), tick, side=side, kind="stop")
    return rounded, source  # type: ignore[return-value]
