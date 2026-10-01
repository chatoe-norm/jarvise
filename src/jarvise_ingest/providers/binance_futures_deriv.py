"""Binance Futures public derivatives — GET only. No API key. No order placement."""

from __future__ import annotations

from typing import Any

import httpx

FAPI_BASE = "https://fapi.binance.com"
PERIOD_MAP = {"15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"}


def _get(
    path: str,
    params: dict[str, Any],
    *,
    client: httpx.Client | None = None,
) -> Any:
    own = client is None
    http = client or httpx.Client(timeout=30.0)
    try:
        resp = http.get(f"{FAPI_BASE}{path}", params=params)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as exc:
        raise RuntimeError(
            f"binance futures GET {path} failed: {exc}. "
            "Retry: jarvise ingest --symbol SYM --skip-derivatives"
        ) from exc
    finally:
        if own:
            http.close()


def fetch_derivatives(
    pair_symbol: str,
    interval: str,
    limit: int = 30,
    *,
    client: httpx.Client | None = None,
) -> list[dict]:
    """Funding + open interest history. Liquidations / L/S left None on this path."""
    sym = pair_symbol.upper()
    period = PERIOD_MAP.get(interval, "1h")
    lim = max(1, min(int(limit), 500))

    own = client is None
    http = client or httpx.Client(timeout=30.0)
    try:
        funding_raw = _get(
            "/fapi/v1/fundingRate",
            {"symbol": sym, "limit": lim},
            client=http,
        )
        oi_raw = _get(
            "/futures/data/openInterestHist",
            {"symbol": sym, "period": period, "limit": lim},
            client=http,
        )
    finally:
        if own:
            http.close()

    fr_by_ts: dict[int, float | None] = {}
    if isinstance(funding_raw, list):
        for item in funding_raw:
            if not isinstance(item, dict):
                continue
            ts = item.get("fundingTime")
            if ts is None:
                continue
            ts_i = int(ts)
            rate = item.get("fundingRate")
            fr_by_ts[ts_i] = float(rate) if rate is not None else None

    oi_by_ts: dict[int, float | None] = {}
    if isinstance(oi_raw, list):
        for item in oi_raw:
            if not isinstance(item, dict):
                continue
            ts = item.get("timestamp")
            if ts is None:
                continue
            ts_i = int(ts)
            # Prefer USD open interest when present.
            oi = item.get("sumOpenInterestValue")
            if oi is None:
                oi = item.get("sumOpenInterest")
            oi_by_ts[ts_i] = float(oi) if oi is not None else None

    timestamps = sorted(set(fr_by_ts) | set(oi_by_ts))
    if not timestamps:
        raise RuntimeError(f"binance futures returned empty derivatives for {sym}")

    # CoinGlass stores base coin; normalize pair → base for as_of lookups.
    coin = sym
    for quote in ("USDT", "USD", "BUSD", "USDC"):
        if coin.endswith(quote) and len(coin) > len(quote):
            coin = coin[: -len(quote)]
            break

    last_fr: float | None = None
    last_oi: float | None = None
    rows: list[dict] = []
    for ts in timestamps:
        if ts in fr_by_ts and fr_by_ts[ts] is not None:
            last_fr = fr_by_ts[ts]
        if ts in oi_by_ts and oi_by_ts[ts] is not None:
            last_oi = oi_by_ts[ts]
        rows.append(
            {
                "symbol": coin,
                "timestamp": ts,
                "open_interest_usd": last_oi,
                "funding_rate": last_fr,
                "long_short_ratio": None,
                "liquidations_24h_usd": None,
            }
        )
    return rows[-lim:]
