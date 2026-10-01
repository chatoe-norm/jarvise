"""Binance spot MARKET order POST — allowlisted path only. No withdraw/cancel."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlencode

import httpx

from jarvise_exchange.binance_spot import (
    BINANCE_BASE,
    BinanceAuth,
    signature_for_query,
)

ORDER_PATH = "/api/v3/order"
ALLOWED_TRADE_CALLS = frozenset({("POST", ORDER_PATH)})


def assert_trade_allowlisted(method: str, path: str) -> None:
    key = (method.upper(), path)
    if key not in ALLOWED_TRADE_CALLS:
        raise PermissionError(f"trade path not allowlisted: {method} {path}")


def place_spot_market_order(
    auth: BinanceAuth,
    *,
    symbol: str,
    side: str,
    quote_order_qty: float | None = None,
    quantity: float | None = None,
    client: httpx.Client | None = None,
    base_url: str = BINANCE_BASE,
    timestamp_ms: int | None = None,
) -> dict[str, Any]:
    """POST /api/v3/order type=MARKET. Prefer quoteOrderQty for BUY (USDT notional)."""
    assert_trade_allowlisted("POST", ORDER_PATH)
    side_u = side.upper()
    if side_u not in {"BUY", "SELL"}:
        raise ValueError(f"invalid side: {side}")
    if quote_order_qty is None and quantity is None:
        raise ValueError("quote_order_qty or quantity required")
    if quote_order_qty is not None and quantity is not None:
        raise ValueError("provide only one of quote_order_qty or quantity")

    params: dict[str, Any] = {
        "symbol": symbol.upper(),
        "side": side_u,
        "type": "MARKET",
        "timestamp": int(timestamp_ms if timestamp_ms is not None else time.time() * 1000),
    }
    if quote_order_qty is not None:
        params["quoteOrderQty"] = f"{float(quote_order_qty):.8f}".rstrip("0").rstrip(".")
    else:
        params["quantity"] = f"{float(quantity):.8f}".rstrip("0").rstrip(".")

    query = urlencode(params)
    signature = signature_for_query(auth, query)
    headers = {"X-MBX-APIKEY": auth.api_key}
    url = f"{base_url.rstrip('/')}{ORDER_PATH}?{query}&signature={signature}"

    own = client is None
    http = client or httpx.Client(timeout=30.0)
    try:
        resp = http.post(url, headers=headers)
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, dict):
            raise ValueError("unexpected order response type")
        return payload
    finally:
        if own:
            http.close()
