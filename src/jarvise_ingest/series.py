"""Derive indicator columns from the whole stored candle series.

Indicators are a materialised view over `market_technicals` OHLCV: recomputing
always reads the full series for one (symbol, timeframe), so the value stored for
a candle does not depend on which ingest window happened to contain it.
"""

from __future__ import annotations

import sqlite3

from jarvise_ingest.db import load_candle_series, write_indicators
from jarvise_ingest.indicators import indicator_series


def recompute_indicators(
    conn: sqlite3.Connection, symbol: str, timeframe: str
) -> int:
    """Rewrite indicator columns for every stored candle; return rows touched."""
    candles = load_candle_series(conn, symbol, timeframe)
    if not candles:
        return 0

    highs: list[float] = []
    lows: list[float] = []
    closes: list[float] = []
    for candle in candles:
        if candle["high"] is None or candle["low"] is None or candle["close"] is None:
            raise ValueError(
                f"incomplete candle for {symbol} {timeframe} at {candle['timestamp']}; "
                f"re-ingest before recomputing indicators"
            )
        highs.append(float(candle["high"]))
        lows.append(float(candle["low"]))
        closes.append(float(candle["close"]))

    computed = indicator_series(highs, lows, closes)
    updates = [
        {
            "symbol": symbol,
            "timeframe": timeframe,
            "timestamp": candle["timestamp"],
            "atr_14": computed["atr_14"][i],
            "rsi_14": computed["rsi_14"][i],
            "ema_20": computed["ema_20"][i],
            "ema_200": computed["ema_200"][i],
        }
        for i, candle in enumerate(candles)
    ]
    return write_indicators(conn, updates)
