"""Binance public klines — GET only. No order placement."""

from __future__ import annotations

import time

import httpx

BINANCE_BASE = "https://api.binance.com"
ALLOWED_INTERVALS = {"15m", "1h", "4h", "1d"}


def fetch_klines(
    symbol: str,
    interval: str,
    limit: int = 200,
    *,
    client: httpx.Client | None = None,
) -> list[dict]:
    if interval not in ALLOWED_INTERVALS:
        raise ValueError(f"unsupported timeframe: {interval}")
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be 1..1000")
    params = {"symbol": symbol.upper(), "interval": interval, "limit": limit}
    own = client is None
    http = client or httpx.Client(timeout=30.0)
    try:
        resp = http.get(f"{BINANCE_BASE}/api/v3/klines", params=params)
        resp.raise_for_status()
        raw = resp.json()
    except httpx.HTTPError as exc:
        raise RuntimeError(
            f"binance klines failed for {symbol}: {exc}. Retry: jarvise ingest --symbol {symbol} --skip-derivatives"
        ) from exc
    finally:
        if own:
            http.close()
    if not raw:
        raise RuntimeError(f"binance returned empty klines for {symbol}")
    now_ms = int(time.time() * 1000)
    candles: list[dict] = []
    for row in raw:
        # row[6] is closeTime; the final kline is still forming and its OHLC
        # keeps changing, so storing it would make derived values unreproducible.
        if int(row[6]) >= now_ms:
            continue
        candles.append(
            {
                "symbol": symbol.upper(),
                "timestamp": int(row[0]),
                "timeframe": interval,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
            }
        )
    if not candles:
        raise RuntimeError(
            f"binance returned no closed candles for {symbol}; "
            f"the current {interval} candle is still open"
        )
    return candles
