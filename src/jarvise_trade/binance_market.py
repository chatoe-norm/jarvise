"""Binance spot MARKET order POST + read-only order query — allowlisted paths only.

No cancel, no withdraw, no transfer. Every order carries a deterministic
``newClientOrderId`` derived from the approval id so a retry can be detected on the
venue instead of double-ordering.
"""

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
ALLOWED_TRADE_CALLS = frozenset({("POST", ORDER_PATH), ("GET", ORDER_PATH)})
CLIENT_ORDER_PREFIX = "jrv-"
# Binance newClientOrderId: max 36 chars, ^[\.A-Z\:/a-z0-9_-]{1,36}$
_CLIENT_ORDER_MAX = 36
ORDER_NOT_FOUND_CODE = -2013
TERMINAL_VENUE_STATUSES = frozenset({"FILLED", "CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH"})


def assert_trade_allowlisted(method: str, path: str) -> None:
    key = (method.upper(), path)
    if key not in ALLOWED_TRADE_CALLS:
        raise PermissionError(f"trade path not allowlisted: {method} {path}")


def client_order_id_for_approval(approval_id: str) -> str:
    """Deterministic venue client order id for one approval (idempotency key)."""
    cleaned = "".join(ch for ch in str(approval_id) if ch.isalnum() or ch in "._-")
    body = cleaned[: _CLIENT_ORDER_MAX - len(CLIENT_ORDER_PREFIX)]
    if not body:
        raise ValueError("approval_id yields empty client order id")
    return f"{CLIENT_ORDER_PREFIX}{body}"


def client_order_id_for_stop(approval_id: str) -> str:
    return client_order_id_for_approval(f"x{approval_id}")


def place_spot_stop_loss_limit(
    auth: BinanceAuth,
    *,
    symbol: str,
    side: str,
    quantity: float,
    stop_price: float,
    limit_price: float,
    client: httpx.Client | None = None,
    base_url: str = BINANCE_BASE,
    timestamp_ms: int | None = None,
    new_client_order_id: str | None = None,
) -> dict[str, Any]:
    """POST /api/v3/order type=STOP_LOSS_LIMIT (GTC). Venue-side invalidation."""
    assert_trade_allowlisted("POST", ORDER_PATH)
    side_u = side.upper()
    if side_u not in {"BUY", "SELL"}:
        raise ValueError(f"invalid side: {side}")
    params: dict[str, Any] = {
        "symbol": symbol.upper(),
        "side": side_u,
        "type": "STOP_LOSS_LIMIT",
        "timeInForce": "GTC",
        "quantity": _format_qty(quantity),
        "price": _format_qty(limit_price),
        "stopPrice": _format_qty(stop_price),
        "newOrderRespType": "FULL",
        "timestamp": int(timestamp_ms if timestamp_ms is not None else time.time() * 1000),
    }
    if new_client_order_id:
        if len(new_client_order_id) > _CLIENT_ORDER_MAX:
            raise ValueError("new_client_order_id exceeds 36 chars")
        params["newClientOrderId"] = new_client_order_id
    url = _signed_url(auth, base_url, params)
    headers = {"X-MBX-APIKEY": auth.api_key}
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


def _signed_url(auth: BinanceAuth, base_url: str, params: dict[str, Any]) -> str:
    query = urlencode(params)
    signature = signature_for_query(auth, query)
    return f"{base_url.rstrip('/')}{ORDER_PATH}?{query}&signature={signature}"


def _format_qty(value: float) -> str:
    return f"{float(value):.8f}".rstrip("0").rstrip(".")


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
    new_client_order_id: str | None = None,
) -> dict[str, Any]:
    """POST /api/v3/order type=MARKET. Prefer quoteOrderQty for BUY (USDT notional).

    Single-shot by design: never retried here. Callers use ``query_order`` with the
    same ``new_client_order_id`` before any re-submit.
    """
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
        "newOrderRespType": "FULL",
        "timestamp": int(timestamp_ms if timestamp_ms is not None else time.time() * 1000),
    }
    if new_client_order_id:
        if len(new_client_order_id) > _CLIENT_ORDER_MAX:
            raise ValueError("new_client_order_id exceeds 36 chars")
        params["newClientOrderId"] = new_client_order_id
    if quote_order_qty is not None:
        params["quoteOrderQty"] = _format_qty(quote_order_qty)
    else:
        params["quantity"] = _format_qty(quantity or 0.0)

    url = _signed_url(auth, base_url, params)
    headers = {"X-MBX-APIKEY": auth.api_key}

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


def query_order(
    auth: BinanceAuth,
    *,
    symbol: str,
    orig_client_order_id: str,
    client: httpx.Client | None = None,
    base_url: str = BINANCE_BASE,
    timestamp_ms: int | None = None,
) -> dict[str, Any] | None:
    """GET /api/v3/order by origClientOrderId. Returns None when the venue has no such order.

    Read-only: used to detect an order that was placed but whose response was lost
    (timeout) and to reconcile fills after submit.
    """
    assert_trade_allowlisted("GET", ORDER_PATH)
    params: dict[str, Any] = {
        "symbol": symbol.upper(),
        "origClientOrderId": orig_client_order_id,
        "timestamp": int(timestamp_ms if timestamp_ms is not None else time.time() * 1000),
    }
    url = _signed_url(auth, base_url, params)
    headers = {"X-MBX-APIKEY": auth.api_key}
    own = client is None
    http = client or httpx.Client(timeout=30.0)
    try:
        resp = http.get(url, headers=headers)
        if resp.status_code == 400:
            try:
                body = resp.json()
            except ValueError:
                body = {}
            if isinstance(body, dict) and int(body.get("code") or 0) == ORDER_NOT_FOUND_CODE:
                return None
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, dict):
            raise ValueError("unexpected order query response type")
        return payload
    finally:
        if own:
            http.close()


def summarize_fill(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize a venue order payload (POST FULL or GET) into Jarvise audit fields."""
    venue_status = str(payload.get("status") or "").upper() or None
    executed = _float_or_none(payload.get("executedQty"))
    quote = _float_or_none(payload.get("cummulativeQuoteQty"))
    fills = payload.get("fills")
    fills_count = len(fills) if isinstance(fills, list) else None
    avg_price = None
    if executed and quote and executed > 0:
        avg_price = quote / executed
    return {
        "venue_order_id": str(payload.get("orderId") or "") or None,
        "client_order_id": str(payload.get("clientOrderId") or "") or None,
        "venue_status": venue_status,
        "executed_qty": executed,
        "cummulative_quote_qty": quote,
        "fills_count": fills_count,
        "avg_price": avg_price,
        "status": lifecycle_status(venue_status, executed_qty=executed),
    }


def lifecycle_status(venue_status: str | None, *, executed_qty: float | None = None) -> str:
    """Map venue status → Jarvise live_orders.status.

    submitted | partially_filled | filled | canceled. Partial fills never read as flat.
    """
    vs = (venue_status or "").upper()
    if vs == "FILLED":
        return "filled"
    if vs == "PARTIALLY_FILLED":
        return "partially_filled"
    if vs in {"CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH"}:
        if executed_qty and executed_qty > 0:
            return "partially_filled"
        return "canceled"
    return "submitted"


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
