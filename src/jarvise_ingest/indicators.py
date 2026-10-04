"""OHLC indicator helpers for paper analytics (ATR, RSI, EMA).

`ema`, `rsi` and `atr` are recursive: each value carries a share of the seed that
started the recursion. `indicator_series` withholds values while that share is
still material, so a stored number never depends on where the series happened to
begin.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

# Largest share of the seed an exported indicator value may still carry.
SEED_INFLUENCE_FLOOR = 0.01


def ema(values: Sequence[float], period: int) -> list[float | None]:
    if period < 1:
        raise ValueError("period must be >= 1")
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    mult = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = (values[i] - prev) * mult + prev
        out[i] = prev
    return out


def sma(values: Sequence[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period < 1:
        raise ValueError("period must be >= 1")
    if len(values) < period:
        return out
    window = 0.0
    for i, value in enumerate(values):
        window += value
        if i >= period:
            window -= values[i - period]
        if i >= period - 1:
            out[i] = window / period
    return out


def bollinger(
    values: Sequence[float], period: int = 20, num_std: float = 2.0
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    mid = sma(values, period)
    upper: list[float | None] = [None] * len(values)
    lower: list[float | None] = [None] * len(values)
    for i, center in enumerate(mid):
        if center is None:
            continue
        window = values[i - period + 1 : i + 1]
        mean = center
        var = sum((x - mean) ** 2 for x in window) / period  # population std, ddof=0
        std = math.sqrt(var)
        upper[i] = mean + num_std * std
        lower[i] = mean - num_std * std
    return mid, upper, lower


def macd(
    values: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    fast_ema = ema(values, fast)
    slow_ema = ema(values, slow)
    line: list[float | None] = [None] * len(values)
    for i, (a, b) in enumerate(zip(fast_ema, slow_ema, strict=True)):
        if a is None or b is None:
            continue
        line[i] = a - b
    compact = [x for x in line if x is not None]
    sig_compact = ema(compact, signal) if compact else []
    signal_out: list[float | None] = [None] * len(values)
    hist: list[float | None] = [None] * len(values)
    compact_i = 0
    for i, macd_val in enumerate(line):
        if macd_val is None:
            continue
        sig_val = sig_compact[compact_i] if compact_i < len(sig_compact) else None
        compact_i += 1
        signal_out[i] = sig_val
        if sig_val is not None:
            hist[i] = macd_val - sig_val
    return line, signal_out, hist


def rsi(closes: Sequence[float], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        delta = closes[i] - closes[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        out[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        out[period] = 100.0 - (100.0 / (1.0 + rs))
    for i in range(period + 1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gain = delta if delta > 0 else 0.0
        loss = -delta if delta < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100.0 - (100.0 / (1.0 + rs))
    return out


def atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> list[float | None]:
    n = len(closes)
    out: list[float | None] = [None] * n
    if n == 0:
        return out
    true_ranges: list[float] = []
    for i in range(n):
        if i == 0:
            tr = highs[0] - lows[0]
        else:
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
        true_ranges.append(tr)
    if n < period:
        return out
    first = sum(true_ranges[:period]) / period
    out[period - 1] = first
    prev = first
    for i in range(period, n):
        prev = (prev * (period - 1) + true_ranges[i]) / period
        out[i] = prev
    return out


def warm_from(seed_index: int, alpha: float) -> int:
    """First index whose value carries less than `SEED_INFLUENCE_FLOOR` of the seed."""
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be between 0 and 1")
    bars = math.ceil(math.log(SEED_INFLUENCE_FLOOR) / math.log(1.0 - alpha))
    return seed_index + bars


def _withhold_until(values: list[float | None], first: int) -> list[float | None]:
    return [value if i >= first else None for i, value in enumerate(values)]


def indicator_series(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
) -> dict[str, list[float | None]]:
    """Indicator columns for a whole series, warm-up values withheld as NULL."""
    bb_mid, bb_upper, bb_lower = bollinger(closes, 20, 2.0)
    macd_line, macd_signal, macd_hist = macd(closes)
    return {
        "atr_14": _withhold_until(atr(highs, lows, closes, 14), warm_from(13, 1 / 14)),
        "rsi_14": _withhold_until(rsi(closes, 14), warm_from(14, 1 / 14)),
        "ema_20": _withhold_until(ema(closes, 20), warm_from(19, 2 / 21)),
        "ema_200": _withhold_until(ema(closes, 200), warm_from(199, 2 / 201)),
        "sma_20": sma(closes, 20),
        "macd_line": macd_line,
        "macd_signal": macd_signal,
        "macd_hist": macd_hist,
        "bb_mid": bb_mid,
        "bb_upper": bb_upper,
        "bb_lower": bb_lower,
    }
