"""Binance public order book — GET only. No order placement."""

from __future__ import annotations

import time
from typing import Any

import httpx

BINANCE_BASE = "https://api.binance.com"


def _get(
    path: str,
    params: dict[str, Any],
    *,
    client: httpx.Client | None = None,
) -> Any:
    own = client is None
    http = client or httpx.Client(timeout=30.0)
    try:
        resp = http.get(f"{BINANCE_BASE}{path}", params=params)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as exc:
        raise RuntimeError(
            f"binance book GET {path} failed for {params.get('symbol')}: {exc}. "
            "Retry: jarvise ingest --symbol SYM --skip-book"
        ) from exc
    finally:
        if own:
            http.close()


def _depth_notional_within_pct(
    levels: list,
    *,
    mid: float,
    side: str,
    pct: float = 0.01,
) -> float:
    """Sum price*qty for levels within pct of mid (bids below, asks above)."""
    if mid <= 0:
        return 0.0
    total = 0.0
    for level in levels:
        price = float(level[0])
        qty = float(level[1])
        if side == "bid":
            if price < mid * (1.0 - pct):
                break
            if price <= mid:
                total += price * qty
        else:
            if price > mid * (1.0 + pct):
                break
            if price >= mid:
                total += price * qty
    return total


def fetch_order_book_snapshot(
    symbol: str,
    *,
    depth_limit: int = 100,
    client: httpx.Client | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Latest bookTicker + depth → order_book_microstructure row."""
    sym = symbol.upper()
    own = client is None
    http = client or httpx.Client(timeout=30.0)
    try:
        ticker = _get("/api/v3/bookTicker", {"symbol": sym}, client=http)
        depth = _get(
            "/api/v3/depth",
            {"symbol": sym, "limit": depth_limit},
            client=http,
        )
    finally:
        if own:
            http.close()

    bid = float(ticker["bidPrice"])
    ask = float(ticker["askPrice"])
    if bid <= 0 or ask <= 0 or ask < bid:
        raise RuntimeError(f"binance bookTicker anomalous for {sym}: bid={bid} ask={ask}")
    mid = (bid + ask) / 2.0
    spread = (ask - bid) / mid

    bids = depth.get("bids") or []
    asks = depth.get("asks") or []
    bid_depth = _depth_notional_within_pct(bids, mid=mid, side="bid")
    ask_depth = _depth_notional_within_pct(asks, mid=mid, side="ask")

    ts = int(now_ms if now_ms is not None else time.time() * 1000)
    return {
        "symbol": sym,
        "timestamp": ts,
        "bid_ask_spread": spread,
        "bid_depth_1pct_usd": bid_depth,
        "ask_depth_1pct_usd": ask_depth,
        "largest_buy_wall_price": None,
        "largest_sell_wall_price": None,
        "spoof_wall_detected": 0,
    }
