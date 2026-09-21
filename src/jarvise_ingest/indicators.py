"""OHLC indicator helpers for paper analytics (ATR, RSI, EMA)."""

from __future__ import annotations

from collections.abc import Sequence


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
    true_ranges: list[float] = [highs[0] - lows[0]]
    for i in range(1, n):
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


def enrich_candles(candles: list[dict]) -> list[dict]:
    """Add atr_14, rsi_14, ema_20, ema_200 to candle dicts (in place + return)."""
    if not candles:
        return candles
    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    closes = [float(c["close"]) for c in candles]
    atrs = atr(highs, lows, closes, 14)
    rsis = rsi(closes, 14)
    ema20 = ema(closes, 20)
    ema200 = ema(closes, 200)
    for i, c in enumerate(candles):
        c["atr_14"] = atrs[i]
        c["rsi_14"] = rsis[i]
        c["ema_20"] = ema20[i]
        c["ema_200"] = ema200[i]
        c["vwap"] = None
        c["adx_14"] = None
    return candles
