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
            out.append(
                FuturesPosition(
                    symbol=str(row.get("symbol") or "").upper(),
                    position_side=str(row.get("positionSide") or "BOTH").upper(),
                    position_amt=amt,
                    entry_price=Decimal(str(row.get("entryPrice") or "0")),
                    mark_price=Decimal(str(row.get("markPrice") or "0")),
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

    def step_size(self, symbol: str) -> Decimal:
        url = f"{self._base_url}{EXCHANGE_INFO_PATH}"
        payload = get_json(url, client=self._client, provider="binance_fapi_exchange_info")
        if not isinstance(payload, dict):
            raise ValueError("unexpected exchangeInfo response type")
        for row in payload.get("symbols") or []:
            if str(row.get("symbol") or "").upper() != symbol.upper():
                continue
            for filt in row.get("filters") or []:
                if str(filt.get("filterType") or "") == "LOT_SIZE":
                    return Decimal(str(filt.get("stepSize") or "1"))
        raise ValueError(f"LOT_SIZE missing for {symbol}")

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


def build_futures_client(*, client: httpx.Client | None = None) -> BinanceFuturesClient | None:
    auth = resolve_binance_auth()
    if auth is None:
        return None
    return BinanceFuturesClient(auth, client=client)
