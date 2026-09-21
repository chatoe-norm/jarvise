"""CoinGlass V4 derivatives — GET only. No order placement."""

from __future__ import annotations

import os
from typing import Any

import httpx

COINGLASS_BASE = "https://open-api-v4.coinglass.com"
INTERVAL_MAP = {"15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"}


def resolve_api_key() -> str | None:
    return os.environ.get("COINGLASS_API_KEY") or os.environ.get("CG-API-KEY")


def pair_to_coin(symbol: str) -> str:
    s = symbol.upper()
    for quote in ("USDT", "USD", "BUSD", "USDC"):
        if s.endswith(quote) and len(s) > len(quote):
            return s[: -len(quote)]
    return s


def _get(
    path: str,
    params: dict[str, Any],
    api_key: str,
    *,
    client: httpx.Client | None = None,
) -> Any:
    headers = {"CG-API-KEY": api_key, "Accept": "application/json"}
    own = client is None
    http = client or httpx.Client(timeout=30.0)
    try:
        resp = http.get(f"{COINGLASS_BASE}{path}", params=params, headers=headers)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as exc:
        raise RuntimeError(
            f"coinglass GET {path} failed: {exc}. Check COINGLASS_API_KEY or use --skip-derivatives"
        ) from exc
    finally:
        if own:
            http.close()


def _series_from_payload(payload: Any) -> list[dict]:
    if isinstance(payload, dict):
        data = payload.get("data", payload)
        if isinstance(data, dict) and "list" in data:
            data = data["list"]
        if isinstance(data, list):
            return data
        return []
    if isinstance(payload, list):
        return payload
    return []


def _ts(item: dict) -> int | None:
    for key in ("t", "time", "timestamp", "createTime"):
        if key in item and item[key] is not None:
            val = int(item[key])
            # seconds → ms
            if val < 10_000_000_000:
                return val * 1000
            return val
    return None


def fetch_derivatives(
    pair_symbol: str,
    interval: str,
    limit: int = 30,
    *,
    api_key: str | None = None,
    client: httpx.Client | None = None,
) -> list[dict]:
    key = api_key or resolve_api_key()
    if not key:
        raise RuntimeError(
            "COINGLASS_API_KEY not set. Export COINGLASS_API_KEY=... or pass --skip-derivatives"
        )
    coin = pair_to_coin(pair_symbol)
    cg_interval = INTERVAL_MAP.get(interval, "1h")
    params = {"symbol": coin, "interval": cg_interval, "limit": limit}

    oi_raw = _get("/api/futures/openInterest/ohlc-history", params, key, client=client)
    fr_raw = _get(
        "/api/futures/fundingRate/oi-weight-ohlc-history", params, key, client=client
    )
    liq_raw = _get(
        "/api/futures/liquidation/aggregated-history",
        {"symbol": coin, "interval": cg_interval, "limit": limit},
        key,
        client=client,
    )

    oi_by_ts: dict[int, float | None] = {}
    for item in _series_from_payload(oi_raw):
        ts = _ts(item)
        if ts is None:
            continue
        oi = item.get("c") or item.get("close") or item.get("openInterest")
        oi_by_ts[ts] = float(oi) if oi is not None else None

    fr_by_ts: dict[int, float | None] = {}
    for item in _series_from_payload(fr_raw):
        ts = _ts(item)
        if ts is None:
            continue
        fr = item.get("c") or item.get("close") or item.get("fundingRate")
        fr_by_ts[ts] = float(fr) if fr is not None else None

    liq_by_ts: dict[int, float | None] = {}
    for item in _series_from_payload(liq_raw):
        ts = _ts(item)
        if ts is None:
            continue
        liq = (
            item.get("aggregated_long_liquidation_usd")
            or item.get("longLiquidationUsd")
            or item.get("volUsd")
            or item.get("c")
        )
        short = item.get("aggregated_short_liquidation_usd") or item.get(
            "shortLiquidationUsd"
        )
        total = None
        if liq is not None:
            total = float(liq)
            if short is not None:
                total += float(short)
        liq_by_ts[ts] = total

    timestamps = sorted(set(oi_by_ts) | set(fr_by_ts) | set(liq_by_ts))
    rows: list[dict] = []
    for ts in timestamps[-limit:]:
        rows.append(
            {
                "symbol": coin,
                "timestamp": ts,
                "open_interest_usd": oi_by_ts.get(ts),
                "funding_rate": fr_by_ts.get(ts),
                "long_short_ratio": None,
                "liquidations_24h_usd": liq_by_ts.get(ts),
            }
        )
    return rows
