"""Binance spot order POST / cancel + read-only order, balance and filter reads — allowlisted paths only.

Cancel is by our own ``origClientOrderId`` only (never cancel-all). No withdraw, no
transfer. Every order carries a deterministic ``newClientOrderId`` derived from the
approval id so a retry can be detected on the venue instead of double-ordering.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Any
from urllib.parse import urlencode

import httpx

from jarvise_exchange.binance_spot import (
    ACCOUNT_PATH,
    BINANCE_BASE,
    BinanceAuth,
    balances_from_account_payload,
    signature_for_query,
)
from jarvise_ingest.http import get_json

ORDER_PATH = "/api/v3/order"
EXCHANGE_INFO_PATH = "/api/v3/exchangeInfo"
ALLOWED_TRADE_CALLS = frozenset(
    {
        ("POST", ORDER_PATH),
        ("GET", ORDER_PATH),
        ("DELETE", ORDER_PATH),
        ("GET", ACCOUNT_PATH),
        ("GET", EXCHANGE_INFO_PATH),
    }
)
CLIENT_ORDER_PREFIX = "jrv-"
# Binance newClientOrderId: max 36 chars, ^[\.A-Z\:/a-z0-9_-]{1,36}$
_CLIENT_ORDER_MAX = 36
ORDER_NOT_FOUND_CODE = -2013
UNKNOWN_ORDER_CODE = -2011
TERMINAL_VENUE_STATUSES = frozenset({"FILLED", "CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH"})


@dataclass(frozen=True)
class SymbolFilters:
    """Venue sizing rules for one spot symbol (zero means "no constraint")."""

    symbol: str
    base_asset: str
    step_size: Decimal
    min_qty: Decimal
    min_notional: Decimal
    tick_size: Decimal


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
    quantity: float | Decimal,
    stop_price: float | Decimal,
    limit_price: float | Decimal,
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


def _signed_url(auth: BinanceAuth, base_url: str, params: dict[str, Any], path: str = ORDER_PATH) -> str:
    query = urlencode(params)
    signature = signature_for_query(auth, query)
    return f"{base_url.rstrip('/')}{path}?{query}&signature={signature}"


def _format_qty(value: float | Decimal) -> str:
    text = format(value, "f") if isinstance(value, Decimal) else f"{float(value):.8f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def floor_to_step(value: float | Decimal, step: Decimal) -> Decimal:
    """Round down to a multiple of the venue step (LOT_SIZE stepSize / PRICE_FILTER tickSize)."""
    amount = Decimal(str(value))
    if amount <= 0:
        return Decimal("0")
    if step <= 0:
        return amount
    return (amount / step).to_integral_value(rounding=ROUND_DOWN) * step


def place_spot_market_order(
    auth: BinanceAuth,
    *,
    symbol: str,
    side: str,
    quote_order_qty: float | None = None,
    quantity: float | Decimal | None = None,
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


def cancel_order(
    auth: BinanceAuth,
    *,
    symbol: str,
    orig_client_order_id: str,
    client: httpx.Client | None = None,
    base_url: str = BINANCE_BASE,
    timestamp_ms: int | None = None,
) -> dict[str, Any] | None:
    """DELETE /api/v3/order by our origClientOrderId. None when the venue reports no open order.

    ``-2011`` also covers an order that already filled or was canceled, so callers
    re-query before trusting the inventory.
    """
    assert_trade_allowlisted("DELETE", ORDER_PATH)
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
        resp = http.delete(url, headers=headers)
        if resp.status_code == 400:
            try:
                body = resp.json()
            except ValueError:
                body = {}
            if isinstance(body, dict) and int(body.get("code") or 0) == UNKNOWN_ORDER_CODE:
                return None
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, dict):
            raise ValueError("unexpected cancel response type")
        return payload
    finally:
        if own:
            http.close()


def fetch_symbol_filters(
    symbol: str,
    *,
    client: httpx.Client | None = None,
    base_url: str = BINANCE_BASE,
) -> SymbolFilters:
    """Public GET /api/v3/exchangeInfo for one symbol. Raises when LOT_SIZE is absent."""
    assert_trade_allowlisted("GET", EXCHANGE_INFO_PATH)
    sym = symbol.upper()
    payload = get_json(
        f"{base_url.rstrip('/')}{EXCHANGE_INFO_PATH}",
        params={"symbol": sym},
        client=client,
        provider="binance_exchange_info",
    )
    rows = payload.get("symbols") if isinstance(payload, dict) else None
    info = next((row for row in rows or [] if str(row.get("symbol") or "").upper() == sym), None)
    if info is None:
        raise ValueError(f"exchangeInfo has no symbol {sym}")
    by_type = {str(f.get("filterType")): f for f in info.get("filters") or []}
    lot = by_type.get("LOT_SIZE")
    if lot is None:
        raise ValueError(f"exchangeInfo {sym} has no LOT_SIZE filter")
    notional = by_type.get("NOTIONAL") or by_type.get("MIN_NOTIONAL") or {}
    price = by_type.get("PRICE_FILTER") or {}
    return SymbolFilters(
        symbol=sym,
        base_asset=str(info["baseAsset"]).upper(),
        step_size=Decimal(str(lot.get("stepSize") or "0")).normalize(),
        min_qty=Decimal(str(lot.get("minQty") or "0")).normalize(),
        min_notional=Decimal(str(notional.get("minNotional") or "0")).normalize(),
        tick_size=Decimal(str(price.get("tickSize") or "0")).normalize(),
    )


def fetch_free_balance(
    auth: BinanceAuth,
    asset: str,
    *,
    client: httpx.Client | None = None,
    base_url: str = BINANCE_BASE,
) -> Decimal:
    """Signed GET /api/v3/account with the trade key; free (unlocked) amount of one asset."""
    assert_trade_allowlisted("GET", ACCOUNT_PATH)

    def signed_url() -> str:
        return _signed_url(auth, base_url, {"timestamp": int(time.time() * 1000)}, path=ACCOUNT_PATH)

    payload = get_json(
        signed_url,
        headers={"X-MBX-APIKEY": auth.api_key},
        client=client,
        provider="binance_trade_account",
    )
    wanted = asset.upper()
    for balance in balances_from_account_payload(payload):
        if balance.asset.upper() == wanted:
            return balance.free
    return Decimal("0")


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
