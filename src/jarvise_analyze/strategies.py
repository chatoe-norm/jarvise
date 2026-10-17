"""Replay-only signal formulas. Production ``analyze_snapshot`` is not changed.

``donchian_long`` confidence is a fixed rule flag, not a model score.
"""

from __future__ import annotations

import math
from typing import Any, Literal

from jarvise_analyze.engine import (
    ATR_STOP_MULT,
    BASE_SIZE_PCT,
    MAX_SIZE_PCT,
    _result,
    analyze_snapshot,
)

StrategyName = Literal["current", "trend_long", "donchian_long"]
STRATEGY_NAMES: tuple[str, ...] = ("current", "trend_long", "donchian_long")
DONCHIAN_LOOKBACK = 20
DONCHIAN_EXIT_LOOKBACK = 10
DONCHIAN_MIN_BARS = DONCHIAN_LOOKBACK + 1
# Constant so a rules fill clears the 0.55 floor. Not a calibrated score.
DONCHIAN_CONFIDENCE = 0.75


def decide(
    name: str,
    history: list[dict[str, Any]],
    position_side: str | None,
) -> dict[str, Any]:
    """Classify the last bar in ``history``. Later bars are not in the list."""
    if name not in STRATEGY_NAMES:
        raise ValueError(f"strategy must be one of {STRATEGY_NAMES}")
    if not history:
        raise ValueError("history must include the current closed bar")
    if name == "current":
        return analyze_snapshot(history[-1])
    if name == "trend_long":
        return _trend_long(history[-1])
    return _donchian_long(history, position_side)


def _trend_long(candle: dict[str, Any]) -> dict[str, Any]:
    result = analyze_snapshot(candle)
    if str(result.get("action") or "") != "short":
        return result
    thesis = f"{result.get('thesis') or ''} Long-only: short is flattened.".strip()
    return _result(
        str(result["symbol"]),
        str(result["timeframe"]),
        int(result["timestamp"]),
        regime=str(result["regime_state"]),
        action="flat",
        confidence=float(result["confidence_score"]),
        invalidation=None,
        size=0.0,
        thesis=thesis,
    )


def _donchian_long(history: list[dict[str, Any]], position_side: str | None) -> dict[str, Any]:
    current = history[-1]
    symbol = str(current["symbol"]).upper()
    timeframe = str(current.get("timeframe") or "")
    timestamp = int(current["timestamp"])
    close = _num(current.get("close"))
    atr = _num(current.get("atr_14"))
    if len(history) < DONCHIAN_MIN_BARS or close is None or close <= 0 or atr is None or atr <= 0:
        return _result(
            symbol,
            timeframe,
            timestamp,
            regime="range",
            action="flat",
            confidence=0.0,
            invalidation=None,
            size=0.0,
            thesis="donchian_long: fewer than 21 bars or missing atr_14; flat.",
        )
    side = (position_side or "").lower()
    if side == "long":
        lows = _levels(history[-(DONCHIAN_EXIT_LOOKBACK + 1) : -1], "low")
        if lows is None:
            return _flat_channel(symbol, timeframe, timestamp, "donchian_long: missing exit lows; flat.")
        if close < min(lows):
            return _flat_channel(
                symbol,
                timeframe,
                timestamp,
                "donchian_long: close below the prior 10-bar low; flat.",
            )
        return _long_signal(symbol, timeframe, timestamp, close, atr)
    highs = _levels(history[-(DONCHIAN_LOOKBACK + 1) : -1], "high")
    if highs is None:
        return _flat_channel(symbol, timeframe, timestamp, "donchian_long: missing channel highs; flat.")
    if close > max(highs):
        return _long_signal(symbol, timeframe, timestamp, close, atr)
    return _flat_channel(
        symbol,
        timeframe,
        timestamp,
        "donchian_long: close is not above the prior 20-bar high; flat.",
    )


def _long_signal(
    symbol: str,
    timeframe: str,
    timestamp: int,
    close: float,
    atr: float,
) -> dict[str, Any]:
    size = min(MAX_SIZE_PCT, round(BASE_SIZE_PCT * DONCHIAN_CONFIDENCE, 4))
    return _result(
        symbol,
        timeframe,
        timestamp,
        regime="trend_up",
        action="long",
        confidence=DONCHIAN_CONFIDENCE,
        invalidation=round(close - ATR_STOP_MULT * atr, 8),
        size=size,
        thesis=(
            "donchian_long: close above the prior 20-bar high. "
            f"Confidence {DONCHIAN_CONFIDENCE:.2f} is a fixed rule flag, not a score."
        ),
    )


def _flat_channel(symbol: str, timeframe: str, timestamp: int, thesis: str) -> dict[str, Any]:
    return _result(
        symbol,
        timeframe,
        timestamp,
        regime="range",
        action="flat",
        confidence=0.0,
        invalidation=None,
        size=0.0,
        thesis=thesis,
    )


def _levels(bars: list[dict[str, Any]], key: str) -> list[float] | None:
    levels: list[float] = []
    for bar in bars:
        level = _num(bar.get(key))
        if level is None:
            return None
        levels.append(level)
    return levels


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number
