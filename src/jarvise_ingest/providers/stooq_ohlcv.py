"""Stooq public OHLCV CSV — GET only. Equity / ETF paper ingest (T2.6)."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from typing import Any

import httpx

from jarvise_ingest.http import ProviderError, get_text

STOOQ_BASE = "https://stooq.com/q/d/l/"
# Stooq interval codes we map from Jarvise timeframes.
_INTERVAL = {"1d": "d", "1h": "h"}
EQUITY_SYMBOLS = frozenset({"SPY", "QQQ"})


def stooq_ticker(symbol: str) -> str:
    """Jarvise SPY/QQQ → Stooq ``spy.us`` style ticker."""
    sym = str(symbol).upper().removesuffix(".US")
    return f"{sym.lower()}.us"


def is_equity_symbol(symbol: str) -> bool:
    return str(symbol).upper().removesuffix(".US") in EQUITY_SYMBOLS


def _parse_csv(symbol: str, timeframe: str, text: str) -> list[dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(text))
    out: list[dict[str, Any]] = []
    for row in reader:
        if not row or not row.get("Date"):
            continue
        try:
            day = datetime.strptime(row["Date"].strip(), "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            continue
        ts = int(day.timestamp() * 1000)
        try:
            o = float(row["Open"])
            h = float(row["High"])
            low = float(row["Low"])
            c = float(row["Close"])
            vol = float(row.get("Volume") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        out.append(
            {
                "symbol": str(symbol).upper().removesuffix(".US"),
                "timestamp": ts,
                "timeframe": timeframe,
                "open": o,
                "high": h,
                "low": low,
                "close": c,
                "volume": vol,
            }
        )
    out.sort(key=lambda r: int(r["timestamp"]))
    return out


def fetch_stooq_ohlcv(
    symbol: str,
    timeframe: str = "1d",
    *,
    limit: int = 200,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """Newest ``limit`` closed daily (or hourly) bars from Stooq CSV."""
    if timeframe not in _INTERVAL:
        raise ValueError(
            f"stooq supports timeframes {sorted(_INTERVAL)}; got {timeframe!r}"
        )
    if limit < 1:
        raise ValueError("limit must be >= 1")
    ticker = stooq_ticker(symbol)
    params = {"s": ticker, "i": _INTERVAL[timeframe]}
    own = client is None
    http = client or httpx.Client(timeout=30.0)
    try:
        try:
            text = get_text(
                STOOQ_BASE,
                params=params,
                client=http,
                provider="stooq_ohlcv",
            )
        except ProviderError as exc:
            raise RuntimeError(
                f"stooq ohlcv failed for {symbol}: {exc}. "
                f"Retry: jarvise ingest --symbol {symbol} --universe paper_equity --timeframe 1d "
                f"--skip-book --skip-derivatives"
            ) from exc
    finally:
        if own:
            http.close()
    if not text or "No data" in text[:80]:
        raise RuntimeError(f"stooq returned no data for {ticker}")
    candles = _parse_csv(symbol, timeframe, text)
    if not candles:
        raise RuntimeError(f"stooq CSV parse empty for {ticker}")
    return candles[-limit:]


def parse_stooq_csv_text(symbol: str, timeframe: str, text: str) -> list[dict[str, Any]]:
    """Test helper: parse fixture CSV without network."""
    return _parse_csv(symbol, timeframe, text)
