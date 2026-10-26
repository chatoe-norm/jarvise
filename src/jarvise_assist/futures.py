"""Binance USDⓈ-M futures read-only client. Signed GETs only. Never places orders."""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import httpx

from jarvise_exchange.binance_spot import (
    BinanceAuth,
    resolve_binance_auth,
    signature_for_query,
)
from jarvise_exchange.permissions import audit_key_permissions, fetch_api_restrictions
from jarvise_ingest.http import ProviderError, get_json

FAPI_BASE = "https://fapi.binance.com"
BALANCE_PATH = "/fapi/v3/balance"
POSITION_RISK_PATH = "/fapi/v3/positionRisk"
PREMIUM_INDEX_PATH = "/fapi/v1/premiumIndex"
EXCHANGE_INFO_PATH = "/fapi/v1/exchangeInfo"
OPEN_ORDERS_PATH = "/fapi/v1/openOrders"
OPEN_ALGO_ORDERS_PATH = "/fapi/v1/openAlgoOrders"

# Binance codes that mean the key cannot use this endpoint.
PERMISSION_ERROR_CODES = frozenset({-2014, -2015, -1022})

STOP_ORDER_TYPES = frozenset(
    {
        "STOP",
        "STOP_MARKET",
        "STOP_LOSS",
        "STOP_LOSS_LIMIT",
    }
)


class FuturesPermissionError(RuntimeError):
    """Key lacks futures read access or has trading/withdraw scope."""


class FuturesKeyUnsafeError(RuntimeError):
    """API key has trading or withdrawal permission; refuse all futures use."""


@dataclass(frozen=True)
class FuturesPosition:
    symbol: str
    position_side: str
    position_amt: Decimal
    entry_price: Decimal
    mark_price: Decimal
    update_time_ms: int | None = None

    @property
    def side(self) -> str:
        """Trade side: long or short (hedge positionSide or one-way amt sign)."""
        ps = self.position_side.upper()
        if ps == "LONG":
            return "long"
        if ps == "SHORT":
            return "short"
        return "long" if self.position_amt > 0 else "short"

    @property
    def qty(self) -> Decimal:
        return abs(self.position_amt)


@dataclass(frozen=True)
class SymbolFilters:
    step_size: Decimal
    min_qty: Decimal
    min_notional: Decimal


def audit_key_for_assist(perms: dict[str, Any]) -> dict[str, Any]:
    """Refuse withdraw, transfer, spot trading, and futures trading flags."""
    base = audit_key_permissions(perms)
    withdraw = bool(base.get("enableWithdrawals"))
    internal = bool(base.get("enableInternalTransfer"))
    universal = bool(base.get("permitsUniversalTransfer"))
    spot = bool(base.get("enableSpotAndMarginTrading"))
    futures_trade = bool(base.get("enableFutures"))
    reading = bool(base.get("enableReading", True))
    reasons: list[str] = []
    if not reading:
        reasons.append("enableReading=false")
    if withdraw:
        reasons.append("enableWithdrawals=true")
    if internal:
        reasons.append("enableInternalTransfer=true")
    if universal:
        reasons.append("permitsUniversalTransfer=true")
    if spot:
        reasons.append("spot trading enabled")
    if futures_trade:
        reasons.append("enableFutures=true")
    return {
        "ok_for_assist": reading and not reasons,
        "block_reasons": reasons,
        "enableReading": reading,
        "enableWithdrawals": withdraw,
        "enableFutures": futures_trade,
        "enableSpotAndMarginTrading": spot,
    }


def _is_permission_failure(exc: BaseException) -> bool:
    if isinstance(exc, FuturesPermissionError):
        return True
    if isinstance(exc, ProviderError):
        if exc.status in {401, 403}:
            return True
        msg = str(exc)
        for code in PERMISSION_ERROR_CODES:
            if str(code) in msg:
                return True
    return False


def order_stop_price(order: dict[str, Any]) -> Decimal | None:
    """Stop / trigger price from a regular or algo order row."""
    for key in ("stopPrice", "triggerPrice", "activatePrice"):
        raw = order.get(key)
        if raw is None or str(raw) in {"", "0", "0.0", "0.00000000"}:
            continue
        try:
            px = Decimal(str(raw))
        except Exception:  # noqa: BLE001
            continue
        if px > 0:
            return px
    return None


def order_type(order: dict[str, Any]) -> str:
    return str(order.get("type") or order.get("orderType") or "").upper()


def is_protective_stop(
    order: dict[str, Any],
    *,
    position_side: str,
    trade_side: str,
) -> bool:
    """True when the order is a reduce-only / close stop on the closing side.

    One-way (missing positionSide or BOTH): requires reduceOnly or closePosition.
    Hedge (LONG/SHORT): matching positionSide + closing side is enough without reduceOnly.
    """
    if order_type(order) not in STOP_ORDER_TYPES:
        return False
    close_side = "SELL" if trade_side == "long" else "BUY"
    if str(order.get("side") or "").upper() != close_side:
        return False
    if order_stop_price(order) is None:
        return False
    ops = str(order.get("positionSide") or "BOTH").upper()
    pos_side = position_side.upper()
    reduce_only = bool(order.get("reduceOnly")) or str(order.get("closePosition") or "").lower() in {
        "true",
        "1",
    }
    if ops in {"LONG", "SHORT"}:
        return ops == pos_side
    # One-way: BOTH or missing.
    if ops not in {"BOTH", pos_side}:
        return False
    return reduce_only


def find_protective_stop(
    orders: list[dict[str, Any]],
    *,
    symbol: str,
    position_side: str,
    trade_side: str,
) -> Decimal | None:
    """Best (tightest for the side) protective stop price, or None."""
    prices: list[Decimal] = []
    for order in orders:
        if str(order.get("symbol") or "").upper() != symbol.upper():
            continue
        if not is_protective_stop(order, position_side=position_side, trade_side=trade_side):
            continue
        px = order_stop_price(order)
        if px is not None:
            prices.append(px)
    if not prices:
        return None
    if trade_side == "long":
        return max(prices)
    return min(prices)


class BinanceFuturesClient:
    """Read-only USDⓈ-M client. No POST/PUT/DELETE order methods."""

    def __init__(
        self,
        auth: BinanceAuth,
        *,
        client: httpx.Client | None = None,
        base_url: str = FAPI_BASE,
    ) -> None:
        self._auth = auth
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._exchange_info: dict[str, Any] | None = None
        self._filters: dict[str, SymbolFilters] = {}

    def _signed_url(self, path: str, params: dict[str, Any] | None = None) -> str:
        merged = dict(params or {})
        merged["timestamp"] = int(time.time() * 1000)
        query = urlencode(merged)
        signature = signature_for_query(self._auth, query)
        return f"{self._base_url}{path}?{query}&signature={signature}"

    def _headers(self) -> dict[str, str]:
        return {"X-MBX-APIKEY": self._auth.api_key}

    def _signed_get(self, path: str, params: dict[str, Any] | None = None, *, provider: str) -> Any:
        try:
            return get_json(
                lambda: self._signed_url(path, params),
                headers=self._headers(),
                client=self._client,
                provider=provider,
            )
        except ProviderError as exc:
            if _is_permission_failure(exc):
                raise FuturesPermissionError(str(exc)) from exc
            raise

    def assert_key_safe(self) -> dict[str, Any]:
        """Probe apiRestrictions; raise FuturesKeyUnsafeError when trade/withdraw is on."""
        raw = fetch_api_restrictions(self._auth, client=self._client)
        audit = audit_key_for_assist(raw)
        if not audit["ok_for_assist"]:
            raise FuturesKeyUnsafeError("; ".join(audit["block_reasons"]) or "key unsafe")
        return audit

    def usdt_wallet_balance(self) -> Decimal:
        payload = self._signed_get(BALANCE_PATH, provider="binance_fapi_balance")
        if not isinstance(payload, list):
            raise ValueError("unexpected balance response type")
        for row in payload:
            if str(row.get("asset") or "") == "USDT":
                return Decimal(str(row.get("balance") or "0"))
        return Decimal("0")

    def position_risk(self, *, symbol: str | None = None) -> list[FuturesPosition]:
        params = {"symbol": symbol} if symbol else None
        payload = self._signed_get(POSITION_RISK_PATH, params, provider="binance_fapi_position")
        if not isinstance(payload, list):
            raise ValueError("unexpected positionRisk response type")
        out: list[FuturesPosition] = []
        for row in payload:
            amt = Decimal(str(row.get("positionAmt") or "0"))
            if amt == 0:
                continue
            update_raw = row.get("updateTime")
            update_ms = int(update_raw) if update_raw not in (None, "") else None
            out.append(
                FuturesPosition(
                    symbol=str(row.get("symbol") or "").upper(),
                    position_side=str(row.get("positionSide") or "BOTH").upper(),
                    position_amt=amt,
                    entry_price=Decimal(str(row.get("entryPrice") or "0")),
                    mark_price=Decimal(str(row.get("markPrice") or "0")),
                    update_time_ms=update_ms,
                )
            )
        return out

    def open_symbols(self) -> set[str]:
        return {p.symbol for p in self.position_risk()}

    def mark_price(self, symbol: str) -> Decimal:
        url = f"{self._base_url}{PREMIUM_INDEX_PATH}?symbol={symbol.upper()}"
        payload = get_json(url, client=self._client, provider="binance_fapi_mark")
        if not isinstance(payload, dict):
            raise ValueError("unexpected premiumIndex response type")
        return Decimal(str(payload.get("markPrice") or "0"))

    def _load_exchange_info(self) -> dict[str, Any]:
        if self._exchange_info is not None:
            return self._exchange_info
        url = f"{self._base_url}{EXCHANGE_INFO_PATH}"
        payload = get_json(url, client=self._client, provider="binance_fapi_exchange_info")
        if not isinstance(payload, dict):
            raise ValueError("unexpected exchangeInfo response type")
        self._exchange_info = payload
        return payload

    def symbol_filters(self, symbol: str) -> SymbolFilters:
        sym = symbol.upper()
        cached = self._filters.get(sym)
        if cached is not None:
            return cached
        payload = self._load_exchange_info()
        for row in payload.get("symbols") or []:
            if str(row.get("symbol") or "").upper() != sym:
                continue
            step = Decimal("1")
            min_qty = Decimal("0")
            min_notional = Decimal("0")
            for filt in row.get("filters") or []:
                ftype = str(filt.get("filterType") or "")
                if ftype == "LOT_SIZE":
                    step = Decimal(str(filt.get("stepSize") or "1"))
                    min_qty = Decimal(str(filt.get("minQty") or "0"))
                elif ftype in {"MIN_NOTIONAL", "NOTIONAL"}:
                    raw = filt.get("notional")
                    if raw is None:
                        raw = filt.get("minNotional")
                    min_notional = Decimal(str(raw or "0"))
            filters = SymbolFilters(step_size=step, min_qty=min_qty, min_notional=min_notional)
            self._filters[sym] = filters
            return filters
        raise ValueError(f"exchangeInfo missing for {symbol}")

    def step_size(self, symbol: str) -> Decimal:
        return self.symbol_filters(symbol).step_size

    def open_orders(self, *, symbol: str | None = None) -> list[dict[str, Any]]:
        params = {"symbol": symbol} if symbol else None
        payload = self._signed_get(OPEN_ORDERS_PATH, params, provider="binance_fapi_open_orders")
        return list(payload) if isinstance(payload, list) else []

    def open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"algoType": "CONDITIONAL"}
        if symbol:
            params["symbol"] = symbol
        payload = self._signed_get(OPEN_ALGO_ORDERS_PATH, params, provider="binance_fapi_open_algo")
        return list(payload) if isinstance(payload, list) else []

    def all_open_orders(self, *, symbol: str | None = None) -> list[dict[str, Any]]:
        return [*self.open_orders(symbol=symbol), *self.open_algo_orders(symbol=symbol)]


def build_futures_client(*, client: httpx.Client | None = None) -> BinanceFuturesClient | None:
    auth = resolve_binance_auth()
    if auth is None:
        return None
    return BinanceFuturesClient(auth, client=client)
