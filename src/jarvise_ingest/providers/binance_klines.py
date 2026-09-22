"""Binance public klines — GET only. No order placement."""

from __future__ import annotations

import time

import httpx

from jarvise_ingest.timeframes import ALLOWED_INTERVALS, INTERVAL_MS

BINANCE_BASE = "https://api.binance.com"
MAX_PAGE_LIMIT = 1000


def _request_page(
    http: httpx.Client,
    symbol: str,
    interval: str,
    limit: int,
    start_ms: int | None = None,
) -> list:
    params: dict[str, object] = {
        "symbol": symbol.upper(),
        "interval": interval,
        "limit": limit,
    }
    if start_ms is not None:
        params["startTime"] = start_ms
    try:
        resp = http.get(f"{BINANCE_BASE}/api/v3/klines", params=params)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as exc:
        raise RuntimeError(
            f"binance klines failed for {symbol}: {exc}. Retry: jarvise ingest --symbol {symbol} --skip-derivatives"
        ) from exc


def _is_closed(row: list, now_ms: int) -> bool:
    """row[6] is closeTime; the newest kline is still forming and its OHLC keeps
    changing, so storing it would make every derived value unreproducible."""
    return int(row[6]) < now_ms


def _to_candle(symbol: str, interval: str, row: list) -> dict:
    return {
        "symbol": symbol.upper(),
        "timestamp": int(row[0]),
        "timeframe": interval,
        "open": float(row[1]),
        "high": float(row[2]),
        "low": float(row[3]),
        "close": float(row[4]),
        "volume": float(row[5]),
    }


def fetch_klines(
    symbol: str,
    interval: str,
    limit: int = 200,
    *,
    client: httpx.Client | None = None,
) -> list[dict]:
    """Newest `limit` candles, excluding the one still forming."""
    if interval not in ALLOWED_INTERVALS:
        raise ValueError(f"unsupported timeframe: {interval}")
    if limit < 1 or limit > MAX_PAGE_LIMIT:
        raise ValueError(f"limit must be 1..{MAX_PAGE_LIMIT}")
    own = client is None
    http = client or httpx.Client(timeout=30.0)
    try:
        raw = _request_page(http, symbol, interval, limit)
    finally:
        if own:
            http.close()
    if not raw:
        raise RuntimeError(f"binance returned empty klines for {symbol}")
    now_ms = int(time.time() * 1000)
    candles = [
        _to_candle(symbol, interval, row) for row in raw if _is_closed(row, now_ms)
    ]
    if not candles:
        raise RuntimeError(
            f"binance returned no closed candles for {symbol}; "
            f"the current {interval} candle is still open"
        )
    return candles


def fetch_klines_range(
    symbol: str,
    interval: str,
    start_ms: int,
    *,
    until_ms: int | None = None,
    client: httpx.Client | None = None,
    page_limit: int = MAX_PAGE_LIMIT,
    max_pages: int = 200,
) -> list[dict]:
    """Every closed candle from `start_ms` forward, paging past the request cap.

    One request returns at most 1000 candles, which is 41 days at 1h. History
    beyond that needs `startTime` paging, advancing one interval past the last
    candle of each page.
    """
    if interval not in ALLOWED_INTERVALS:
        raise ValueError(f"unsupported timeframe: {interval}")
    if page_limit < 1 or page_limit > MAX_PAGE_LIMIT:
        raise ValueError(f"page_limit must be 1..{MAX_PAGE_LIMIT}")

    step = INTERVAL_MS[interval]
    now_ms = int(time.time() * 1000)
    own = client is None
    http = client or httpx.Client(timeout=30.0)
    candles: list[dict] = []
    seen: set[int] = set()
    cursor = start_ms
    pages = 0
    try:
        while True:
            if pages >= max_pages:
                raise RuntimeError(
                    f"too many pages fetching {symbol} {interval}; "
                    f"narrow the window or raise max_pages"
                )
            rows = _request_page(http, symbol, interval, page_limit, cursor)
            pages += 1
            if not rows:
                break
            reached_until = False
            for row in rows:
                open_ms = int(row[0])
                if until_ms is not None and open_ms >= until_ms:
                    reached_until = True
                    break
                if open_ms in seen or not _is_closed(row, now_ms):
                    continue
                seen.add(open_ms)
                candles.append(_to_candle(symbol, interval, row))
            if reached_until or len(rows) < page_limit:
                break
            cursor = int(rows[-1][0]) + step
    finally:
        if own:
            http.close()

    if not candles:
        raise RuntimeError(
            f"binance returned no closed candles for {symbol} {interval} "
            f"from {start_ms}"
        )
    return candles
