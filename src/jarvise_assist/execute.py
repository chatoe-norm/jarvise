"""Owner-confirm assist execution. Writes only with the trade key when gated on."""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import replace as dc_replace
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from jarvise_assist import ASSIST_SYMBOLS, ASSIST_TIMEFRAME
from jarvise_assist.futures import (
    FAPI_BASE,
    FuturesPosition,
    SymbolFilters,
    find_protective_stop,
    is_protective_stop,
    order_stop_price,
)
from jarvise_assist.keyboard import entry_confirm_keyboard, move_stop_keyboard
from jarvise_assist.messages import fmt_price, fmt_qty, format_move_stop_advice, format_signal_message
from jarvise_assist.pause import is_paused
from jarvise_assist.sizing import (
    ExistingIdea,
    SignalCandidate,
    SizedSignal,
    admit_signals,
    analysis_is_fresh,
    assign_shared_leverage,
    below_min_notional,
    cap_requote_qty,
    chase_exceeded,
    passes_signal_floor,
    round_level_to_tick,
    size_signal,
)
from jarvise_assist.store import (
    cancel_exec_intent,
    claim_exec_intent,
    ensure_settings,
    finish_exec_intent,
    get_exec_intent,
    get_signal,
    get_watch_position,
    insert_exec_intent,
    insert_exec_log,
    latest_seen_idea,
    mark_signal_taken,
    now_ms,
    set_exec_intent_message_id,
)
from jarvise_assist.trail_advice import current_trail_advice
from jarvise_exchange.binance_spot import (
    BinanceAuth,
    load_private_key_pem,
    sign_payload,
)
from jarvise_ingest.db import load_candles_in_range, load_latest_analysis
from jarvise_ingest.http import ProviderError
from jarvise_risk.caps import kill_switch_state

logger = logging.getLogger(__name__)

DEMO_FAPI_BASE = "https://demo-fapi.binance.com"
SAPI_BASE = "https://api.binance.com"
PREVIEW_TTL_MS = 120_000
MOVE_STOP_TTL_MS = 10 * 60_000
TRADE_KEY_CACHE_MS = 10 * 60_000
CLIENT_ID_MAX = 36
MSG_MOVE_STOP_BUFFER = "stop ใหม่ใกล้ราคาเกินไป — ไม่เลื่อน"
MSG_MOVE_STOP_NO_ADVICE = "ตอนนี้ไม่มี stop แนะนำแล้ว — ไม่เลื่อน"
MSG_MOVE_STOP_UNKNOWN = "ไม่แน่ใจว่าวาง stop ใหม่สำเร็จ — stop เดิมยังอยู่ เช็ก Open Orders"
MSG_MOVE_STOP_KEEP = "เลื่อน stop ไม่ได้ — stop เดิมยังอยู่"
MSG_MOVE_STOP_TWO = "⚠️ ยกเลิก stop เดิมไม่ได้ — ตอนนี้มี 2 stop (ไม่อันตราย, reduce-only) เช็ก Open Orders"

MSG_OFF = "ปิดอยู่"
MSG_EXPIRED = "หมดเวลา — กดเหรียญใหม่"
MSG_MARK_PASSED = "ราคาผ่านจุดนี้แล้ว"
MSG_ALREADY_CLOSED = "ไม้นี้ปิดไปแล้ว"
MSG_KILL = "kill switch เปิดอยู่ — ไม่ส่งคำสั่ง"
MSG_KEY_REFUSED = "คีย์เทรดไม่ปลอดภัย — ไม่ส่งคำสั่ง"
MSG_NO_TRADE_KEY = "ยังไม่มีคีย์เทรด — ไม่ส่งคำสั่ง"
MSG_TRADE_FILE_MISSING = "หาไฟล์คีย์เทรดไม่เจอ"
MSG_TRADE_REUSES_READ = "คีย์เทรดซ้ำกับคีย์อ่าน — ไม่ส่งคำสั่ง"
MSG_TRADE_PEM_FAILED = "โหลดคีย์เทรดไม่ได้"
MSG_ONE_WAY = "บัญชีต้องเป็น one-way mode"
MSG_MULTI_ASSETS = "บัญชีต้องปิด Multi-Assets (Single-Asset เท่านั้น)"
MSG_CANCELLED = "ยกเลิกแล้ว"
MSG_STOP_CLOSED = "วาง stop ไม่ได้ — ปิดไม้แล้วเพื่อความปลอดภัย"
MSG_TP_KEEP = "เข้าแล้วและมี stop — วาง TP ไม่ได้ ตั้งเองได้"
MSG_HANDLER_ERROR = "ทำคำสั่งไม่สำเร็จ — ลองใหม่หรือปิดเอง"
MSG_BINANCE_RETRY = "อ่าน Binance ไม่ได้ — ลองใหม่อีกครั้ง"
MSG_BINANCE_NOT_SENT = "อ่าน Binance ไม่ได้ — ยังไม่ได้ส่งคำสั่ง กดเหรียญใหม่เพื่อดู preview ใหม่"

_TRADE_AUTH_REASON_TH: dict[str, str] = {
    "no_key": MSG_NO_TRADE_KEY,
    "no_path": MSG_NO_TRADE_KEY,
    "file_missing": MSG_TRADE_FILE_MISSING,
    "reuses_read_key": MSG_TRADE_REUSES_READ,
    "pem_load_failed": MSG_TRADE_PEM_FAILED,
}

SendFn = Callable[..., Any]
EditFn = Callable[..., Any]
AnswerFn = Callable[..., Any]

_trade_key_ok_until_ms: int = 0
_trade_key_ok: bool = False


def assist_execute_mode() -> str:
    raw = (os.environ.get("JARVISE_ASSIST_EXECUTE") or "off").strip().lower()
    if raw in {"testnet", "live"}:
        return raw
    return "off"


def execution_buttons_enabled(conn: Any) -> bool:
    if assist_execute_mode() == "off":
        return False
    return bool(ensure_settings(conn).get("enabled"))


def trade_auth_reason_th(reason: str | None) -> str:
    """Map typed resolve_trade_auth failure reasons to Thai owner copy."""
    if reason is None:
        return MSG_NO_TRADE_KEY
    return _TRADE_AUTH_REASON_TH.get(reason, MSG_NO_TRADE_KEY)


def resolve_trade_auth() -> tuple[BinanceAuth | None, str | None]:
    """Trade key only. Never falls back to the read-only assist key.

    Returns (auth, None) on success, or (None, reason) with one of:
    no_key, no_path, file_missing, reuses_read_key, pem_load_failed.
    """
    api_key = (os.environ.get("BINANCE_TRADE_API_KEY") or "").strip()
    if not api_key:
        return None, "no_key"
    key_path = (os.environ.get("BINANCE_TRADE_PRIVATE_KEY_PATH") or "").strip()
    if not key_path:
        return None, "no_path"
    read_key = (os.environ.get("BINANCE_API_KEY") or "").strip()
    read_path = (os.environ.get("BINANCE_API_PRIVATE_KEY_PATH") or "").strip()
    if (read_key and api_key == read_key) or (read_path and key_path == read_path):
        logger.error("trade key reuses the read-only BINANCE_API_* key; refusing to trade")
        return None, "reuses_read_key"
    path = Path(key_path)
    if not path.is_file():
        logger.error("BINANCE_TRADE_PRIVATE_KEY_PATH file missing (path=%s)", "set")
        return None, "file_missing"
    passphrase = os.environ.get("BINANCE_TRADE_PRIVATE_KEY_PASSPHRASE") or None
    try:
        private_key = load_private_key_pem(path, passphrase=passphrase)
    except Exception:  # noqa: BLE001
        logger.exception("BINANCE_TRADE_PRIVATE_KEY_PATH pem load failed (path=%s)", "set")
        return None, "pem_load_failed"
    return BinanceAuth(api_key=api_key, private_key=private_key), None


def trade_base_url(mode: str | None = None) -> str:
    m = mode if mode is not None else assist_execute_mode()
    if m == "testnet":
        return DEMO_FAPI_BASE
    return FAPI_BASE


def client_order_id(intent_id: str, step: str) -> str:
    """jv- prefix + short intent + step, within Binance 36-char limit."""
    short = intent_id.replace("aei_", "")[:12]
    raw = f"jv-{short}-{step}"
    return raw[:CLIENT_ID_MAX]


class TradeClientError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        binance_code: int | None = None,
        body: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.binance_code = binance_code
        self.body = body or {}


class BinanceTradeClient:
    """Signed futures client for assist writes. Separate from the read-only client."""

    def __init__(
        self,
        auth: BinanceAuth,
        *,
        client: httpx.Client | None = None,
        base_url: str | None = None,
        sapi_base: str = SAPI_BASE,
    ) -> None:
        self._auth = auth
        self._client = client
        self._base_url = (base_url or trade_base_url()).rstrip("/")
        self._sapi_base = sapi_base.rstrip("/")
        self._exchange_info: dict[str, Any] | None = None
        self._filters: dict[str, SymbolFilters] = {}

    @property
    def api_key(self) -> str:
        return self._auth.api_key

    @property
    def base_url(self) -> str:
        return self._base_url

    def _sign_params(self, params: dict[str, Any]) -> dict[str, Any]:
        merged = {k: v for k, v in params.items() if v is not None}
        merged["timestamp"] = int(time.time() * 1000)
        query = urlencode(merged)
        # Raw signature: httpx encodes params once. signature_for_query is already
        # percent-encoded for Ed25519 and would be encoded twice (sapi rejects, -1022).
        merged["signature"] = sign_payload(self._auth, query)
        return merged

    def _headers(self) -> dict[str, str]:
        return {"X-MBX-APIKEY": self._auth.api_key}

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        base: str | None = None,
    ) -> tuple[int, dict[str, Any] | list[Any] | Any]:
        root = (base or self._base_url).rstrip("/")
        signed = self._sign_params(dict(params or {}))
        url = f"{root}{path}"
        http = self._client or httpx.Client(timeout=20.0)
        owns = self._client is None
        try:
            resp = http.request(method, url, params=signed, headers=self._headers())
        finally:
            if owns:
                http.close()
        try:
            body: Any = resp.json()
        except Exception:  # noqa: BLE001
            body = {"raw": resp.text[:200]}
        return resp.status_code, body

    def _raise_if_error(
        self,
        status: int,
        body: Any,
        *,
        allow_codes: set[int] | None = None,
    ) -> dict[str, Any] | list[Any] | Any:
        code = None
        if isinstance(body, dict) and "code" in body and body.get("code") not in (None, 0, "0"):
            try:
                code = int(body["code"])
            except Exception:  # noqa: BLE001
                code = None
            if allow_codes and code in allow_codes:
                return body
            raise TradeClientError(
                str(body.get("msg") or body.get("code") or "binance error"),
                http_status=status,
                binance_code=code,
                body=body if isinstance(body, dict) else {},
            )
        if status >= 400:
            raise TradeClientError(
                f"http {status}",
                http_status=status,
                binance_code=code,
                body=body if isinstance(body, dict) else {},
            )
        return body

    def signed_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        status, body = self._request("GET", path, params)
        return self._raise_if_error(status, body)

    def signed_post(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        allow_codes: set[int] | None = None,
    ) -> Any:
        status, body = self._request("POST", path, params)
        return self._raise_if_error(status, body, allow_codes=allow_codes)

    def signed_delete(self, path: str, params: dict[str, Any] | None = None) -> Any:
        status, body = self._request("DELETE", path, params)
        return self._raise_if_error(status, body)

    def assert_trade_key_safe(self, *, mode: str | None = None) -> dict[str, Any]:
        """Live: futures on, withdrawals off, IP restricted. Testnet skips sapi."""
        global _trade_key_ok, _trade_key_ok_until_ms
        m = mode if mode is not None else assist_execute_mode()
        if m == "testnet":
            return {"ok": True, "skipped": "testnet"}
        now = now_ms()
        if _trade_key_ok and now < _trade_key_ok_until_ms:
            return {"ok": True, "cached": True}
        status, body = self._request(
            "GET",
            "/sapi/v1/account/apiRestrictions",
            base=self._sapi_base,
        )
        payload = self._raise_if_error(status, body)
        if not isinstance(payload, dict):
            raise TradeClientError("unexpected apiRestrictions")
        reasons: list[str] = []
        if not bool(payload.get("enableFutures")):
            reasons.append("enableFutures=false")
        if bool(payload.get("enableWithdrawals")):
            reasons.append("enableWithdrawals=true")
        if not bool(payload.get("ipRestrict")):
            reasons.append("ipRestrict=false")
        if reasons:
            _trade_key_ok = False
            raise TradeClientError("; ".join(reasons), body=payload)
        _trade_key_ok = True
        _trade_key_ok_until_ms = now + TRADE_KEY_CACHE_MS
        return {"ok": True}

    def assert_account_modes(self) -> None:
        dual = self.signed_get("/fapi/v1/positionSide/dual")
        if isinstance(dual, dict) and bool(dual.get("dualSidePosition")):
            raise TradeClientError(MSG_ONE_WAY, body=dual if isinstance(dual, dict) else {})
        multi = self.signed_get("/fapi/v1/multiAssetsMargin")
        if isinstance(multi, dict) and bool(multi.get("multiAssetsMargin")):
            raise TradeClientError(MSG_MULTI_ASSETS, body=multi if isinstance(multi, dict) else {})

    def usdt_wallet_and_available(self) -> tuple[Decimal, Decimal]:
        payload = self.signed_get("/fapi/v3/balance")
        if not isinstance(payload, list):
            return Decimal("0"), Decimal("0")
        for row in payload:
            if str(row.get("asset") or "") == "USDT":
                balance = Decimal(str(row.get("balance") or "0"))
                raw_avail = row.get("availableBalance")
                available = balance if raw_avail in (None, "") else Decimal(str(raw_avail))
                return balance, available
        return Decimal("0"), Decimal("0")

    def position_risk(self, *, symbol: str | None = None) -> list[FuturesPosition]:
        params = {"symbol": symbol} if symbol else None
        payload = self.signed_get("/fapi/v3/positionRisk", params)
        if not isinstance(payload, list):
            raise TradeClientError("unexpected positionRisk")
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

    def mark_price(self, symbol: str) -> Decimal:
        payload = self.signed_get("/fapi/v1/premiumIndex", {"symbol": symbol.upper()})
        if not isinstance(payload, dict):
            raise TradeClientError("unexpected premiumIndex")
        return Decimal(str(payload.get("markPrice") or "0"))

    def symbol_filters(self, symbol: str) -> SymbolFilters:
        sym = symbol.upper()
        cached = self._filters.get(sym)
        if cached is not None:
            return cached
        if self._exchange_info is None:
            info = self.signed_get("/fapi/v1/exchangeInfo")
            if not isinstance(info, dict):
                raise TradeClientError("unexpected exchangeInfo")
            self._exchange_info = info
        row = None
        for item in self._exchange_info.get("symbols") or []:
            if isinstance(item, dict) and str(item.get("symbol") or "").upper() == sym:
                row = item
                break
        if row is None:
            raise TradeClientError(f"exchangeInfo missing {symbol}")
        step = Decimal("1")
        min_qty = Decimal("0")
        min_notional = Decimal("0")
        tick_size = Decimal("0.01")
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
            elif ftype == "PRICE_FILTER":
                tick_size = Decimal(str(filt.get("tickSize") or "0.01"))
        filters = SymbolFilters(
            step_size=step,
            min_qty=min_qty,
            min_notional=min_notional,
            tick_size=tick_size,
        )
        self._filters[sym] = filters
        return filters

    def open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"algoType": "CONDITIONAL"}
        if symbol:
            params["symbol"] = symbol
        payload = self.signed_get("/fapi/v1/openAlgoOrders", params)
        return list(payload) if isinstance(payload, list) else []

    def open_orders(self, *, symbol: str | None = None) -> list[dict[str, Any]]:
        params = {"symbol": symbol} if symbol else None
        payload = self.signed_get("/fapi/v1/openOrders", params)
        return list(payload) if isinstance(payload, list) else []

    def all_open_orders(self, *, symbol: str | None = None) -> list[dict[str, Any]]:
        return [*self.open_orders(symbol=symbol), *self.open_algo_orders(symbol=symbol)]

    def set_isolated(self, symbol: str) -> Any:
        # Binance may return code 200 on success, or -4046 when already isolated.
        return self.signed_post(
            "/fapi/v1/marginType",
            {"symbol": symbol.upper(), "marginType": "ISOLATED"},
            allow_codes={200, -4046},
        )

    def query_order_by_client_id(self, *, symbol: str, client_id: str) -> dict[str, Any]:
        body = self.signed_get(
            "/fapi/v1/order",
            {"symbol": symbol.upper(), "origClientOrderId": client_id},
        )
        if not isinstance(body, dict):
            raise TradeClientError("unexpected order query response")
        return body

    def find_algo_by_client_id(self, *, symbol: str, client_id: str) -> dict[str, Any] | None:
        for order in self.open_algo_orders(symbol=symbol):
            cid = str(order.get("clientAlgoId") or order.get("clientOrderId") or "")
            if cid == client_id:
                return order
        return None

    def set_leverage(self, symbol: str, leverage: int) -> Any:
        return self.signed_post(
            "/fapi/v1/leverage",
            {"symbol": symbol.upper(), "leverage": int(leverage)},
        )

    def market_order(
        self,
        *,
        symbol: str,
        side: str,
        qty: Decimal,
        reduce_only: bool = False,
        client_id: str,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "symbol": symbol.upper(),
            "side": side.upper(),
            "type": "MARKET",
            "quantity": format(qty, "f"),
            "newClientOrderId": client_id,
            "newOrderRespType": "RESULT",
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        body = self.signed_post("/fapi/v1/order", params)
        if not isinstance(body, dict):
            raise TradeClientError("unexpected order response")
        return body

    def place_algo_stop(
        self,
        *,
        symbol: str,
        side: str,
        qty: Decimal,
        trigger: Decimal,
        order_type: str,
        client_id: str,
    ) -> dict[str, Any]:
        params = {
            "algoType": "CONDITIONAL",
            "symbol": symbol.upper(),
            "side": side.upper(),
            "positionSide": "BOTH",
            "type": order_type,
            "triggerPrice": format(trigger, "f"),
            "quantity": format(qty, "f"),
            "reduceOnly": "true",
            "workingType": "MARK_PRICE",
            "clientAlgoId": client_id,
        }
        body = self.signed_post("/fapi/v1/algoOrder", params)
        if not isinstance(body, dict):
            raise TradeClientError("unexpected algoOrder response")
        return body

    def cancel_algo(self, *, symbol: str, algo_id: str | int) -> Any:
        return self.signed_delete(
            "/fapi/v1/algoOrder",
            {"symbol": symbol.upper(), "algoId": algo_id},
        )

    def cancel_algo_by_client_id(self, *, symbol: str, client_algo_id: str) -> Any:
        return self.signed_delete(
            "/fapi/v1/algoOrder",
            {"symbol": symbol.upper(), "clientAlgoId": client_algo_id},
        )

    def realized_pnl_for_order(self, *, symbol: str, order_id: Any) -> Decimal:
        trades = self.signed_get(
            "/fapi/v1/userTrades",
            {"symbol": symbol.upper(), "orderId": order_id},
        )
        if not isinstance(trades, list):
            return Decimal("0")
        total = Decimal("0")
        for row in trades:
            total += Decimal(str(row.get("realizedPnl") or "0"))
        return total


def build_trade_client(
    *,
    http_client: httpx.Client | None = None,
    auth: BinanceAuth | None = None,
    base_url: str | None = None,
) -> tuple[BinanceTradeClient | None, str | None]:
    if auth is not None:
        return BinanceTradeClient(auth, client=http_client, base_url=base_url or trade_base_url()), None
    resolved, reason = resolve_trade_auth()
    if resolved is None:
        return None, reason
    return (
        BinanceTradeClient(resolved, client=http_client, base_url=base_url or trade_base_url()),
        None,
    )


def reset_trade_key_cache() -> None:
    global _trade_key_ok, _trade_key_ok_until_ms
    _trade_key_ok = False
    _trade_key_ok_until_ms = 0


def _kill_blocks() -> bool:
    return bool(kill_switch_state(strict=True)["engaged"])


def _log_write(
    conn: Any,
    *,
    intent_id: str,
    step: str,
    endpoint: str,
    client_order_id: str | None = None,
    http_status: int | None = None,
    binance_code: int | None = None,
    order_or_algo_id: str | None = None,
) -> None:
    insert_exec_log(
        conn,
        intent_id=intent_id,
        step=step,
        endpoint=endpoint,
        client_order_id=client_order_id,
        http_status=http_status,
        binance_code=binance_code,
        order_or_algo_id=str(order_or_algo_id) if order_or_algo_id is not None else None,
    )


def _is_clear_rejection(exc: BaseException) -> bool:
    """True when Binance returned a typed rejection (safe to treat as not filled)."""
    return isinstance(exc, TradeClientError) and exc.binance_code is not None


def _query_fill_after_unknown(
    trade: BinanceTradeClient,
    *,
    symbol: str,
    client_id: str,
    conn: Any,
    intent_id: str,
    step: str,
) -> dict[str, Any] | None:
    """Recover a market fill after a timeout/connection error. None = not found."""
    last_exc: BaseException | None = None
    for _ in range(3):
        try:
            order = trade.query_order_by_client_id(symbol=symbol, client_id=client_id)
            _log_write(
                conn,
                intent_id=intent_id,
                step=step,
                endpoint="/fapi/v1/order",
                client_order_id=client_id,
                http_status=200,
                order_or_algo_id=str(order.get("orderId") or ""),
            )
            return order
        except TradeClientError as exc:
            last_exc = exc
            _log_write(
                conn,
                intent_id=intent_id,
                step=step,
                endpoint="/fapi/v1/order",
                client_order_id=client_id,
                http_status=exc.http_status,
                binance_code=exc.binance_code,
            )
            if exc.binance_code == -2013:
                return None
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.exception("order query failed for %s", client_id)
    if last_exc is not None:
        logger.warning("order query exhausted for %s: %s", client_id, last_exc)
    return None


def _fill_from_position(
    trade: BinanceTradeClient,
    *,
    symbol: str,
) -> tuple[Decimal, Decimal] | None:
    """(qty, entry) when a position exists after an unknown market result."""
    positions = trade.position_risk(symbol=symbol)
    if not positions:
        return None
    pos = positions[0]
    if pos.qty <= 0:
        return None
    return pos.qty, pos.entry_price


def _emergency_close_step(attempt: int) -> str:
    return "emergency_close" if attempt == 0 else f"emergency_close_{attempt + 1}"


def _emergency_close_query_step(attempt: int) -> str:
    return "emergency_close_query" if attempt == 0 else f"emergency_close_query_{attempt + 1}"


def _position_gone(trade: BinanceTradeClient, *, symbol: str) -> bool | None:
    """True when flat, False when still open, None when the check failed."""
    try:
        return len(trade.position_risk(symbol=symbol)) == 0
    except Exception:  # noqa: BLE001
        logger.exception("position check failed during emergency close for %s", symbol)
        return None


def _cls_filled(trade: BinanceTradeClient, *, symbol: str, client_id: str) -> bool | None:
    """True when executedQty > 0, False when not filled, None when the check failed."""
    try:
        order = trade.query_order_by_client_id(symbol=symbol, client_id=client_id)
        return Decimal(str(order.get("executedQty") or "0")) > 0
    except TradeClientError as exc:
        if exc.binance_code == -2013:
            return False
        logger.warning("cls query failed for %s: %s", client_id, type(exc).__name__)
        return None
    except Exception:  # noqa: BLE001
        logger.exception("cls query failed for %s", client_id)
        return None


def _emergency_close_verified(
    trade: BinanceTradeClient,
    *,
    conn: Any,
    intent_id: str,
    symbol: str,
    side: str,
    qty: Decimal,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """True only when the close filled or the position is gone. Up to 3 reduceOnly attempts."""
    ccid = client_order_id(intent_id, "cls")
    close_side = _closing_side(side)
    backoffs = (1.0, 2.0)
    for attempt in range(3):
        if attempt > 0:
            sleep(backoffs[attempt - 1])
            gone = _position_gone(trade, symbol=symbol)
            if gone is True:
                return True
            filled = _cls_filled(trade, symbol=symbol, client_id=ccid)
            if filled is True and _position_gone(trade, symbol=symbol) is True:
                return True
            # Unknown (None), still open, or not filled: resend same client id.
        step = _emergency_close_step(attempt)
        query_step = _emergency_close_query_step(attempt)
        try:
            fill = trade.market_order(
                symbol=symbol,
                side=close_side,
                qty=qty,
                reduce_only=True,
                client_id=ccid,
            )
            _log_write(
                conn,
                intent_id=intent_id,
                step=step,
                endpoint="/fapi/v1/order",
                client_order_id=ccid,
                http_status=200,
                order_or_algo_id=str(fill.get("orderId") or ""),
            )
            if Decimal(str(fill.get("executedQty") or "0")) > 0:
                return True
        except Exception as exc:  # noqa: BLE001
            code = exc.binance_code if isinstance(exc, TradeClientError) else None
            _log_write(
                conn,
                intent_id=intent_id,
                step=step,
                endpoint="/fapi/v1/order",
                client_order_id=ccid,
                http_status=exc.http_status if isinstance(exc, TradeClientError) else None,
                binance_code=code,
            )
            if not _is_clear_rejection(exc):
                recovered = _query_fill_after_unknown(
                    trade,
                    symbol=symbol,
                    client_id=ccid,
                    conn=conn,
                    intent_id=intent_id,
                    step=query_step,
                )
                if recovered is not None and Decimal(str(recovered.get("executedQty") or "0")) > 0:
                    return True
        # -2022 (reduceOnly, no position) counts as closed only after positionRisk confirms flat.
        if _position_gone(trade, symbol=symbol) is True:
            return True
    return False


def _msg_close_unconfirmed(
    symbol: str,
    side: str,
    *,
    qty: Decimal,
    stop: Decimal,
) -> str:
    side_l = "LONG" if side == "long" else "SHORT"
    return (
        f"🚨 ไม้ {symbol.upper()} {side_l} เปิดอยู่ ยังไม่มี stop และปิดไม่ได้ — เข้า Binance ปิดทันที\n"
        f"qty {qty} · stop {stop}"
    )


def _msg_check_now(symbol: str) -> str:
    return f"⚠️ ไม่แน่ใจผลคำสั่ง — เปิด Binance เช็ค {symbol.upper()} ทันทีว่ามีไม้และ stop ครบไหม"


def _cancel_algo_client_ok(
    trade: BinanceTradeClient,
    *,
    symbol: str,
    client_algo_id: str,
    conn: Any,
    intent_id: str,
    step: str,
) -> bool:
    """True when cancelled or already gone (-2011 / -2013)."""
    try:
        trade.cancel_algo_by_client_id(symbol=symbol, client_algo_id=client_algo_id)
        _log_write(
            conn,
            intent_id=intent_id,
            step=step,
            endpoint="/fapi/v1/algoOrder",
            client_order_id=client_algo_id,
            http_status=200,
        )
        return True
    except TradeClientError as exc:
        _log_write(
            conn,
            intent_id=intent_id,
            step=step,
            endpoint="/fapi/v1/algoOrder",
            client_order_id=client_algo_id,
            http_status=exc.http_status,
            binance_code=exc.binance_code,
        )
        if exc.binance_code in {-2011, -2013}:
            return True
        logger.warning(
            "direct algo cancel failed %s %s: %s",
            symbol,
            client_algo_id,
            type(exc).__name__,
        )
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "direct algo cancel failed %s %s: %s",
            symbol,
            client_algo_id,
            type(exc).__name__,
        )
        return False


def _orphan_cancel_after_close(
    trade: BinanceTradeClient,
    *,
    conn: Any,
    intent_id: str,
    symbol: str,
) -> bool:
    """List-cancel jv- orders, then always try sl0/sl1 by clientAlgoId. True when all ok."""
    all_ok = True
    list_failed = False
    try:
        _cancelled, list_ok = cancel_jv_orders_for_symbol(trade, symbol)
        if not list_ok:
            all_ok = False
    except Exception as exc:  # noqa: BLE001
        list_failed = True
        logger.warning("orphan list cancel failed %s: %s", symbol, type(exc).__name__)
    _log_write(
        conn,
        intent_id=intent_id,
        step="orphan_cancel",
        endpoint="/fapi/v1/openAlgoOrders",
        client_order_id=None,
    )
    direct_ok = True
    for tag in ("sl0", "sl1"):
        cid = client_order_id(intent_id, tag)
        if not _cancel_algo_client_ok(
            trade,
            symbol=symbol,
            client_algo_id=cid,
            conn=conn,
            intent_id=intent_id,
            step="orphan_cancel",
        ):
            direct_ok = False
            all_ok = False
    # When the list call fails, direct sl0/sl1 cancels are the fallback confirmation.
    if list_failed:
        return direct_ok
    return all_ok


def _orphan_cancel_warning(symbol: str) -> str:
    return f"⚠️ ยกเลิก stop ค้างไม่ได้ — เช็ก Open Orders ของ {symbol.upper()}"


def _safe_find_algo(trade: BinanceTradeClient, *, symbol: str, client_id: str) -> dict[str, Any] | None:
    """Lookup after an unknown result. A failed lookup counts as not found, so the
    caller retries or closes instead of escaping with a filled, unprotected position."""
    try:
        return trade.find_algo_by_client_id(symbol=symbol, client_id=client_id)
    except Exception:  # noqa: BLE001
        logger.exception("algo lookup failed for %s", client_id)
        return None


def _existing_ideas_from_trade(
    conn: Any,
    trade: BinanceTradeClient,
    *,
    balance: Decimal,
    risk_pct: Decimal,
    now_ms_value: int,
) -> list[ExistingIdea]:
    """Reuse signals.build_existing_ideas with trade-account positions/orders."""
    from jarvise_assist.signals import build_existing_ideas

    positions = trade.position_risk()
    try:
        orders = trade.all_open_orders()
    except Exception:  # noqa: BLE001
        orders = []
    return build_existing_ideas(
        conn,
        positions=positions,
        orders=orders,
        balance=balance,
        risk_pct=risk_pct,
        now_ms=now_ms_value,
        include_sent_held=False,
    )


_REASON_TH: dict[str, str] = {
    "flat": "ไม่มีแนวโน้ม",
    "low_confidence": "ความมั่นใจต่ำ",
    "stale_analysis": "ข้อมูลยังไม่อัปเดต",
    "open_position": "ถืออยู่แล้ว",
    "risk_cap": "เต็มเพดานความเสี่ยง",
    "below_min_notional": "ไม้เล็กกว่าขั้นต่ำ",
    "margin_cap": "margin ไม่พอ",
    "size_failed": "คำนวณขนาดไม้ไม่ได้",
    "balance_zero": "ยอด USDT ในบัญชีเป็น 0",
    "same_idea": "ส่งไปแล้วใน 24 ชม.",
    "kill_switch": MSG_KILL,
    "key_refused": MSG_KEY_REFUSED,
    "no_trade_key": MSG_NO_TRADE_KEY,
    "chase": "ราคาวิ่งไปแล้ว",
    "paused": "หยุดชั่วคราวจากแพ้ติดกัน",
    "expired": MSG_EXPIRED,
    "off": MSG_OFF,
    "disabled": MSG_OFF,
    "one_way": MSG_ONE_WAY,
    "multi_assets": MSG_MULTI_ASSETS,
    "zero_fill": "เข้าไม่สำเร็จ (filled 0)",
    "already_closed": MSG_ALREADY_CLOSED,
    "mark_passed": MSG_MARK_PASSED,
    "not_tighter": "ระดับใหม่ไม่กระชับกว่าเดิม",
    "no_position": "ไม่มีไม้นี้แล้ว",
    "wrong_side": "ด้านไม่ตรงกับไม้ที่เปิด",
}


def _reason_th(reason: str) -> str:
    if reason.startswith("fapi_read_failed:"):
        step = reason.split(":", 1)[1] or "?"
        return f"อ่านบัญชีไม่ได้ ({step})"
    return _REASON_TH.get(reason, reason)


def _positive_fill_price(payload: dict[str, Any] | None) -> Decimal | None:
    """avgPrice/price from a fill or order query; None when missing or <= 0."""
    if not payload:
        return None
    for key in ("avgPrice", "price"):
        raw = payload.get(key)
        if raw in (None, ""):
            continue
        price = Decimal(str(raw))
        if price > 0:
            return price
    return None


def _closing_side(trade_side: str) -> str:
    return "SELL" if trade_side == "long" else "BUY"


def _opening_side(trade_side: str) -> str:
    return "BUY" if trade_side == "long" else "SELL"


def _preview_from_sized(
    sized: SizedSignal,
    *,
    seen_entry: Decimal | None = None,
    seen_stop: Decimal | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "qty": str(sized.qty),
        "stop": str(sized.stop),
        "target": str(sized.target),
        "leverage": int(sized.leverage),
        "entry": str(sized.entry),
        "loss_at_stop": str(sized.loss_at_stop),
        "r_distance": str(sized.r_distance),
        "analysis_id": sized.analysis_id,
        "step_size": str(sized.step_size) if sized.step_size is not None else None,
    }
    if seen_entry is not None and seen_stop is not None:
        out["seen_entry"] = str(seen_entry)
        out["seen_stop"] = str(seen_stop)
    return out


def _apply_pct_levels(
    *,
    mainnet_entry: Decimal,
    mainnet_stop: Decimal,
    mainnet_target: Decimal,
    testnet_mark: Decimal,
    side: str,
) -> tuple[Decimal, Decimal]:
    """Map mainnet stop/target percentage distances onto the testnet mark."""
    if mainnet_entry <= 0:
        return mainnet_stop, mainnet_target
    stop_pct = (mainnet_stop - mainnet_entry) / mainnet_entry
    target_pct = (mainnet_target - mainnet_entry) / mainnet_entry
    return testnet_mark * (Decimal("1") + stop_pct), testnet_mark * (Decimal("1") + target_pct)


def size_for_execution(
    conn: Any,
    *,
    trade: BinanceTradeClient,
    symbol: str,
    side: str,
    now_ms_value: int,
    read_mark: Decimal | None = None,
) -> tuple[SizedSignal | None, str | None]:
    """Build sized preview for symbol/side. Returns (sized, skip_reason)."""
    settings = ensure_settings(conn)
    if not settings["enabled"]:
        return None, "disabled"
    if is_paused(settings, now_ms=now_ms_value):
        return None, "paused"
    analysis = load_latest_analysis(conn, symbol, ASSIST_TIMEFRAME)
    if analysis is None or not passes_signal_floor(analysis):
        return None, "flat" if analysis is None or str(
            analysis.get("action") or ""
        ).lower() == "flat" else "low_confidence"
    if str(analysis.get("action") or "").lower() != side.lower():
        return None, "flat"
    if not analysis_is_fresh(analysis, now_ms=now_ms_value):
        return None, "stale_analysis"
    mode = assist_execute_mode()

    def _fapi_step(step: str, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            logger.warning("size_for_execution fapi step=%s type=%s", step, type(exc).__name__)
            raise

    try:
        mark = _fapi_step("mark", lambda: trade.mark_price(symbol))
    except Exception:  # noqa: BLE001
        return None, "fapi_read_failed:mark"
    try:
        filters = _fapi_step("filters", lambda: trade.symbol_filters(symbol))
    except Exception:  # noqa: BLE001
        return None, "fapi_read_failed:filters"
    try:
        positions = _fapi_step("positions", lambda: trade.position_risk())
    except Exception:  # noqa: BLE001
        return None, "fapi_read_failed:positions"
    try:
        balance, available = _fapi_step("balance", lambda: trade.usdt_wallet_and_available())
    except Exception:  # noqa: BLE001
        return None, "fapi_read_failed:balance"
    if balance <= 0:
        return None, "balance_zero"
    if any(p.symbol == symbol.upper() for p in positions):
        return None, "open_position"
    stop = Decimal(str(analysis["invalidation_price"]))
    entry = mark
    # Analysis target is 2R from size_signal; for testnet remap percentages.
    mainnet_mark = read_mark if read_mark is not None else mark
    if mode == "testnet" and read_mark is not None and read_mark > 0:
        # Provisional target via mainnet distances using analysis stop and 2R.
        r = abs(mainnet_mark - stop)
        if side.lower() == "long":
            main_target = mainnet_mark + Decimal("2") * r
        else:
            main_target = mainnet_mark - Decimal("2") * r
        stop, target_hint = _apply_pct_levels(
            mainnet_entry=mainnet_mark,
            mainnet_stop=stop,
            mainnet_target=main_target,
            testnet_mark=mark,
            side=side,
        )
    else:
        target_hint = None
    stop = round_level_to_tick(stop, filters.tick_size, side=side, kind="stop")
    risk_pct = Decimal(str(settings["risk_pct"]))
    sized = size_signal(
        symbol=symbol,
        side=side,
        entry=entry,
        stop=stop,
        balance=balance,
        risk_pct=risk_pct,
        step_size=filters.step_size,
        analysis_id=str(analysis.get("analysis_id") or "") or None,
    )
    if sized is None:
        return None, "size_failed"
    if target_hint is not None:
        rounded_target = round_level_to_tick(target_hint, filters.tick_size, side=side, kind="target")
    else:
        rounded_target = round_level_to_tick(sized.target, filters.tick_size, side=side, kind="target")
    sized = dc_replace(sized, stop=stop, target=rounded_target)
    seen = latest_seen_idea(conn, symbol=symbol, side=side, now_ms=now_ms_value)
    if seen is not None:
        seen_entry = Decimal(str(seen["entry"]))
        seen_stop = Decimal(str(seen["stop"]))
        # Seen levels are mainnet. In testnet compare against read_mark; skip if absent.
        if mode == "testnet":
            chase_mark = read_mark if read_mark is not None and read_mark > 0 else None
        else:
            chase_mark = entry
        if chase_mark is not None and chase_exceeded(
            side=side, entry=seen_entry, stop=seen_stop, mark=chase_mark
        ):
            return None, "chase"
        first_qty = Decimal(str(seen["qty"])) if seen.get("qty") is not None else None
        first_loss = Decimal(str(seen["loss_at_stop"])) if seen.get("loss_at_stop") is not None else None
        sized = cap_requote_qty(
            sized,
            first_qty=first_qty,
            first_loss=first_loss,
            balance=balance,
        )
    if below_min_notional(
        qty=sized.qty,
        notional=sized.notional,
        min_qty=filters.min_qty,
        min_notional=filters.min_notional,
    ):
        return None, "below_min_notional"
    existing = _existing_ideas_from_trade(
        conn,
        trade,
        balance=balance,
        risk_pct=risk_pct,
        now_ms_value=now_ms_value,
    )
    cand = SignalCandidate(
        sized=sized, confidence=float(analysis.get("confidence_score") or 0.0), symbol_rank=0
    )
    admitted, rejected = admit_signals(existing=existing, candidates=[cand], balance=balance)
    if rejected:
        return None, "risk_cap"
    admitted, margin_dropped = assign_shared_leverage(admitted, available_balance=available)
    if margin_dropped or not admitted:
        return None, "margin_cap"
    final = admitted[0].sized
    # Confirm must send the preview numbers; keep qty/stop/target from sizing, leverage shared.
    return (
        SizedSignal(
            symbol=sized.symbol,
            side=sized.side,
            entry=sized.entry,
            stop=sized.stop,
            target=sized.target,
            qty=sized.qty,
            notional=sized.notional,
            leverage=final.leverage,
            loss_at_stop=sized.loss_at_stop,
            risk_pct=sized.risk_pct,
            r_distance=sized.r_distance,
            leverage_capped=final.leverage_capped,
            uncapped_leverage=final.uncapped_leverage,
            analysis_id=sized.analysis_id,
            step_size=filters.step_size,
        ),
        None,
    )


def create_entry_preview(
    conn: Any,
    *,
    symbol: str,
    side: str,
    trade: BinanceTradeClient | None = None,
    read_futures: Any = None,
    now_ms_value: int | None = None,
) -> dict[str, Any]:
    ts = int(now_ms_value if now_ms_value is not None else now_ms())
    if not execution_buttons_enabled(conn):
        return {"ok": False, "reason": "off", "text": MSG_OFF}
    if _kill_blocks():
        return {"ok": False, "reason": "kill_switch", "text": MSG_KILL}
    auth_reason: str | None = None
    client: BinanceTradeClient | None
    if trade is not None:
        client = trade
    else:
        client, auth_reason = build_trade_client()
    if client is None:
        return {
            "ok": False,
            "reason": auth_reason or "no_key",
            "text": trade_auth_reason_th(auth_reason),
        }
    try:
        try:
            client.assert_trade_key_safe()
            client.assert_account_modes()
        except TradeClientError as exc:
            msg = str(exc) if str(exc) in {MSG_ONE_WAY, MSG_MULTI_ASSETS} else MSG_KEY_REFUSED
            return {"ok": False, "reason": "key_refused", "text": msg}
        read_mark = None
        if assist_execute_mode() == "testnet" and read_futures is not None:
            try:
                read_mark = read_futures.mark_price(symbol)
            except Exception:  # noqa: BLE001
                read_mark = None
        sized, reason = size_for_execution(
            conn,
            trade=client,
            symbol=symbol,
            side=side,
            now_ms_value=ts,
            read_mark=read_mark,
        )
        if sized is None:
            return {"ok": False, "reason": reason or "flat", "text": _reason_th(reason or "flat")}
        balance, _ = client.usdt_wallet_and_available()
        tick_size: Decimal | None = None
        try:
            tick_size = client.symbol_filters(symbol).tick_size
        except Exception:  # noqa: BLE001
            tick_size = None
    except (httpx.HTTPError, ProviderError) as exc:
        logger.warning("entry preview network error %s", type(exc).__name__)
        return {"ok": False, "reason": "binance_unreachable", "text": MSG_BINANCE_RETRY}
    expires = ts + PREVIEW_TTL_MS
    seen = latest_seen_idea(conn, symbol=symbol, side=side, now_ms=ts)
    seen_entry = Decimal(str(seen["entry"])) if seen is not None else None
    seen_stop = Decimal(str(seen["stop"])) if seen is not None else None
    intent_id = insert_exec_intent(
        conn,
        kind="entry",
        symbol=symbol,
        side=side,
        preview=_preview_from_sized(sized, seen_entry=seen_entry, seen_stop=seen_stop),
        created_at_ms=ts,
        expires_at_ms=expires,
    )
    text = format_signal_message(
        sized,
        expires_at_ms=expires,
        balance=balance,
        tick_size=tick_size,
        confirm_within_min=2,
    )
    return {
        "ok": True,
        "intent_id": intent_id,
        "text": text,
        "expires_at_ms": expires,
        "sized": sized,
    }


def _confirm_guards_entry(
    conn: Any,
    intent: dict[str, Any],
    trade: BinanceTradeClient,
    *,
    now_ms_value: int,
) -> str | None:
    if intent["expires_at_ms"] < now_ms_value:
        return "expired"
    if not ensure_settings(conn).get("enabled"):
        return "disabled"
    if is_paused(ensure_settings(conn), now_ms=now_ms_value):
        return "paused"
    if _kill_blocks():
        return "kill_switch"
    preview = intent["preview"]
    entry = Decimal(str(preview["entry"]))
    stop = Decimal(str(preview["stop"]))
    qty = Decimal(str(preview["qty"]))
    symbol = str(intent["symbol"])
    side = str(intent["side"])
    analysis = load_latest_analysis(conn, symbol, ASSIST_TIMEFRAME)
    if analysis is None or not passes_signal_floor(analysis):
        return "low_confidence"
    if str(analysis.get("action") or "").lower() != side:
        return "flat"
    if not analysis_is_fresh(analysis, now_ms=now_ms_value):
        return "stale_analysis"
    positions = trade.position_risk()
    if any(p.symbol == symbol for p in positions):
        return "open_position"
    mark = trade.mark_price(symbol)
    if chase_exceeded(side=side, entry=entry, stop=stop, mark=mark):
        return "chase"
    # Seen levels are mainnet; demo mark cannot be compared in testnet.
    if assist_execute_mode() != "testnet" and "seen_entry" in preview and "seen_stop" in preview:
        seen_entry = Decimal(str(preview["seen_entry"]))
        seen_stop = Decimal(str(preview["seen_stop"]))
        if chase_exceeded(side=side, entry=seen_entry, stop=seen_stop, mark=mark):
            return "chase"
    balance, available = trade.usdt_wallet_and_available()
    filters = trade.symbol_filters(symbol)
    if below_min_notional(
        qty=qty, notional=qty * entry, min_qty=filters.min_qty, min_notional=filters.min_notional
    ):
        return "below_min_notional"
    risk_pct = Decimal(str(ensure_settings(conn)["risk_pct"]))
    existing = _existing_ideas_from_trade(
        conn,
        trade,
        balance=balance,
        risk_pct=risk_pct,
        now_ms_value=now_ms_value,
    )
    sized = SizedSignal(
        symbol=symbol,
        side=side,
        entry=entry,
        stop=stop,
        target=Decimal(str(preview["target"])),
        qty=qty,
        notional=qty * entry,
        leverage=int(preview["leverage"]),
        loss_at_stop=Decimal(str(preview.get("loss_at_stop") or qty * abs(entry - stop))),
        risk_pct=risk_pct,
        r_distance=Decimal(str(preview.get("r_distance") or abs(entry - stop))),
        leverage_capped=False,
        uncapped_leverage=int(preview["leverage"]),
        analysis_id=preview.get("analysis_id"),
        step_size=filters.step_size,
    )
    cand = SignalCandidate(sized=sized, confidence=1.0, symbol_rank=0)
    admitted, rejected = admit_signals(existing=existing, candidates=[cand], balance=balance)
    if rejected:
        return "risk_cap"
    admitted, dropped = assign_shared_leverage(admitted, available_balance=available)
    if dropped:
        return "margin_cap"
    return None


def run_entry_sequence(
    conn: Any,
    intent_id: str,
    *,
    trade: BinanceTradeClient,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    intent = get_exec_intent(conn, intent_id)
    if intent is None:
        return "ไม่พบคำสั่ง"
    preview = intent["preview"]
    symbol = str(intent["symbol"])
    side = str(intent["side"])
    qty = Decimal(str(preview["qty"]))
    stop = Decimal(str(preview["stop"]))
    target = Decimal(str(preview["target"]))
    leverage = int(preview["leverage"])
    filters = trade.symbol_filters(symbol)
    stop = round_level_to_tick(stop, filters.tick_size, side=side, kind="stop")
    target = round_level_to_tick(target, filters.tick_size, side=side, kind="target")
    open_side = _opening_side(side)
    close_side = _closing_side(side)

    # Clear stale jv- stop/TP before opening (never blocks the entry).
    try:
        cancel_jv_orders_for_symbol(trade, symbol)
    except Exception as exc:  # noqa: BLE001
        logger.warning("pre-market jv- cancel failed %s: %s", symbol, type(exc).__name__)

    try:
        trade.set_isolated(symbol)
        _log_write(
            conn, intent_id=intent_id, step="marginType", endpoint="/fapi/v1/marginType", http_status=200
        )
    except (httpx.HTTPError, ProviderError) as exc:
        logger.warning("pre-market marginType network error %s", type(exc).__name__)
        return MSG_BINANCE_NOT_SENT
    except TradeClientError as exc:
        _log_write(
            conn,
            intent_id=intent_id,
            step="marginType",
            endpoint="/fapi/v1/marginType",
            http_status=exc.http_status,
            binance_code=exc.binance_code,
        )
        if exc.binance_code != -4046:
            return f"ตั้ง isolated ไม่ได้: {_reason_th(str(exc))}"

    try:
        trade.set_leverage(symbol, leverage)
        _log_write(conn, intent_id=intent_id, step="leverage", endpoint="/fapi/v1/leverage", http_status=200)
    except (httpx.HTTPError, ProviderError) as exc:
        logger.warning("pre-market leverage network error %s", type(exc).__name__)
        return MSG_BINANCE_NOT_SENT
    except TradeClientError as exc:
        _log_write(
            conn,
            intent_id=intent_id,
            step="leverage",
            endpoint="/fapi/v1/leverage",
            http_status=exc.http_status,
            binance_code=exc.binance_code,
        )
        return f"ตั้ง leverage ไม่ได้: {exc}"

    cid = client_order_id(intent_id, "mkt")
    fill: dict[str, Any] | None = None
    try:
        fill = trade.market_order(symbol=symbol, side=open_side, qty=qty, client_id=cid)
        _log_write(
            conn,
            intent_id=intent_id,
            step="market",
            endpoint="/fapi/v1/order",
            client_order_id=cid,
            http_status=200,
            order_or_algo_id=str(fill.get("orderId") or ""),
        )
    except Exception as exc:  # noqa: BLE001
        code = exc.binance_code if isinstance(exc, TradeClientError) else None
        _log_write(
            conn,
            intent_id=intent_id,
            step="market",
            endpoint="/fapi/v1/order",
            client_order_id=cid,
            http_status=exc.http_status if isinstance(exc, TradeClientError) else None,
            binance_code=code,
        )
        if _is_clear_rejection(exc):
            return f"เข้าไม้ไม่สำเร็จ: {exc}"
        recovered = _query_fill_after_unknown(
            trade,
            symbol=symbol,
            client_id=cid,
            conn=conn,
            intent_id=intent_id,
            step="market_query",
        )
        if recovered is not None and Decimal(str(recovered.get("executedQty") or "0")) > 0:
            fill = recovered
        else:
            try:
                pos_fill = _fill_from_position(trade, symbol=symbol)
            except Exception:  # noqa: BLE001
                logger.exception("position read failed after unknown market result")
                return _msg_check_now(symbol)
            if pos_fill is None:
                return "เข้าไม้ไม่สำเร็จ"
            fill = {
                "executedQty": str(pos_fill[0]),
                "avgPrice": str(pos_fill[1]),
                "orderId": "",
            }

    assert fill is not None
    exec_qty = Decimal(str(fill.get("executedQty") or "0"))
    avg = _positive_fill_price(fill)
    if exec_qty <= 0:
        return _reason_th("zero_fill")

    stop_ok = False
    stop_err: str | None = None
    for attempt in range(2):
        scid = client_order_id(intent_id, f"sl{attempt}")
        try:
            mark = trade.mark_price(symbol)
            if side == "long" and mark <= stop:
                raise TradeClientError("would trigger", binance_code=-2021)
            if side == "short" and mark >= stop:
                raise TradeClientError("would trigger", binance_code=-2021)
            algo = trade.place_algo_stop(
                symbol=symbol,
                side=close_side,
                qty=exec_qty,
                trigger=stop,
                order_type="STOP_MARKET",
                client_id=scid,
            )
            _log_write(
                conn,
                intent_id=intent_id,
                step="stop",
                endpoint="/fapi/v1/algoOrder",
                client_order_id=scid,
                http_status=200,
                order_or_algo_id=str(algo.get("algoId") or ""),
            )
            stop_ok = True
            break
        except Exception as exc:  # noqa: BLE001
            stop_err = str(exc)
            code = exc.binance_code if isinstance(exc, TradeClientError) else None
            _log_write(
                conn,
                intent_id=intent_id,
                step="stop",
                endpoint="/fapi/v1/algoOrder",
                client_order_id=scid,
                http_status=exc.http_status if isinstance(exc, TradeClientError) else None,
                binance_code=code,
            )
            # Unknown result: the stop may already exist under this clientAlgoId.
            if not _is_clear_rejection(exc):
                found = _safe_find_algo(trade, symbol=symbol, client_id=scid)
                if found is not None:
                    stop_ok = True
                    break
                # sl0 may still be landing — wait once and look again before sl1.
                if attempt == 0:
                    sleep(1)
                    found = _safe_find_algo(trade, symbol=symbol, client_id=scid)
                    if found is not None:
                        stop_ok = True
                        break
            if isinstance(exc, TradeClientError) and exc.binance_code == -2021:
                break

    if not stop_ok:
        # Final confirm: either stop may have landed after an unknown result.
        sl0_id = client_order_id(intent_id, "sl0")
        sl1_id = client_order_id(intent_id, "sl1")
        found_sl0 = _safe_find_algo(trade, symbol=symbol, client_id=sl0_id)
        found_sl1 = _safe_find_algo(trade, symbol=symbol, client_id=sl1_id)
        if found_sl0 is not None or found_sl1 is not None:
            stop_ok = True
            if found_sl0 is not None and found_sl1 is not None:
                algo_id = found_sl1.get("algoId")
                if algo_id is not None:
                    try:
                        trade.cancel_algo(symbol=symbol, algo_id=algo_id)
                        _log_write(
                            conn,
                            intent_id=intent_id,
                            step="dup_stop_cancel",
                            endpoint="/fapi/v1/algoOrder",
                            client_order_id=sl1_id,
                            http_status=200,
                            order_or_algo_id=str(algo_id),
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "dup stop cancel failed %s: %s",
                            sl1_id,
                            type(exc).__name__,
                        )
                        _log_write(
                            conn,
                            intent_id=intent_id,
                            step="dup_stop_cancel",
                            endpoint="/fapi/v1/algoOrder",
                            client_order_id=sl1_id,
                            http_status=(exc.http_status if isinstance(exc, TradeClientError) else None),
                            binance_code=(exc.binance_code if isinstance(exc, TradeClientError) else None),
                            order_or_algo_id=str(algo_id),
                        )

    if not stop_ok:
        # No stop confirmed (a stop that did land was found by lookup above).
        # Network errors included: close reduce-only rather than leave a filled,
        # unprotected position while the owner may not be watching.
        closed = _emergency_close_verified(
            trade,
            conn=conn,
            intent_id=intent_id,
            symbol=symbol,
            side=side,
            qty=exec_qty,
            sleep=sleep,
        )
        if closed:
            cancel_ok = _orphan_cancel_after_close(trade, conn=conn, intent_id=intent_id, symbol=symbol)
            text = f"{MSG_STOP_CLOSED} ({stop_err or 'stop failed'})"
            if not cancel_ok:
                text = f"{text}\n{_orphan_cancel_warning(symbol)}"
            return text
        return _msg_close_unconfirmed(symbol, side, qty=exec_qty, stop=stop)

    tp_note = ""
    tcid = client_order_id(intent_id, "tp")
    try:
        algo_tp = trade.place_algo_stop(
            symbol=symbol,
            side=close_side,
            qty=exec_qty,
            trigger=target,
            order_type="TAKE_PROFIT_MARKET",
            client_id=tcid,
        )
        _log_write(
            conn,
            intent_id=intent_id,
            step="tp",
            endpoint="/fapi/v1/algoOrder",
            client_order_id=tcid,
            http_status=200,
            order_or_algo_id=str(algo_tp.get("algoId") or ""),
        )
    except Exception as exc:  # noqa: BLE001
        code = exc.binance_code if isinstance(exc, TradeClientError) else None
        _log_write(
            conn,
            intent_id=intent_id,
            step="tp",
            endpoint="/fapi/v1/algoOrder",
            client_order_id=tcid,
            http_status=exc.http_status if isinstance(exc, TradeClientError) else None,
            binance_code=code,
        )
        if not _is_clear_rejection(exc):
            found_tp = _safe_find_algo(trade, symbol=symbol, client_id=tcid)
            if found_tp is None:
                tp_note = f"\n{MSG_TP_KEEP}"
        else:
            tp_note = f"\n{MSG_TP_KEEP}"

    # RESULT can return avgPrice "0.000000". Recover after stop/TP so a slow query
    # cannot leave the position unprotected.
    if avg is None:
        queried = _query_fill_after_unknown(
            trade,
            symbol=symbol,
            client_id=cid,
            conn=conn,
            intent_id=intent_id,
            step="entry_price_query",
        )
        avg = _positive_fill_price(queried)
        if avg is not None:
            logger.info("entry fill price source=order_query intent_id=%s", intent_id)
        else:
            try:
                pos_fill = _fill_from_position(trade, symbol=symbol)
            except Exception:  # noqa: BLE001
                logger.exception("position read failed while resolving entry price")
                pos_fill = None
            if pos_fill is not None and pos_fill[1] > 0:
                avg = pos_fill[1]
                logger.info("entry fill price source=position_risk intent_id=%s", intent_id)
            else:
                logger.info("entry fill price source=unknown intent_id=%s", intent_id)

    # Live-only: link watch / mark signal taken. Testnet must not touch live watch.
    # Never write entry=0 when fill price is still unknown.
    if assist_execute_mode() == "live":
        signal_id = preview.get("signal_id")
        if signal_id:
            try:
                mark_signal_taken(conn, str(signal_id))
            except Exception:  # noqa: BLE001
                logger.exception("mark_signal_taken failed")
        if avg is not None and avg > 0:
            try:
                conn.execute(
                    """
                    INSERT INTO assist_watch_position (
                        symbol, position_side, side, entry, qty, in_universe,
                        signal_id, initial_stop, initial_stop_source, entry_time_ms,
                        no_stop_latched, last_seen_ms, closed_at_ms
                    ) VALUES (?, 'BOTH', ?, ?, ?, 1, ?, ?, 'signal', ?, 0, ?, NULL)
                    ON CONFLICT(symbol, position_side) DO UPDATE SET
                        side=excluded.side,
                        entry=excluded.entry,
                        qty=excluded.qty,
                        signal_id=COALESCE(excluded.signal_id, assist_watch_position.signal_id),
                        initial_stop=excluded.initial_stop,
                        initial_stop_source='signal',
                        entry_time_ms=excluded.entry_time_ms,
                        no_stop_latched=0,
                        last_seen_ms=excluded.last_seen_ms,
                        closed_at_ms=NULL
                    """,
                    (
                        symbol,
                        side,
                        float(avg),
                        float(exec_qty),
                        str(signal_id) if signal_id else None,
                        float(stop),
                        int(now_ms()),
                        int(now_ms()),
                    ),
                )
                conn.commit()
            except Exception:  # noqa: BLE001
                logger.exception("watch link after entry failed")
        else:
            logger.warning(
                "skip assist_watch_position insert: entry price unknown intent_id=%s",
                intent_id,
            )

    side_l = "LONG" if side == "long" else "SHORT"
    if avg is None or avg <= 0:
        return f"✅ เข้า {symbol} {side_l}\nfill ราคายังไม่ยืนยัน × {exec_qty}\nstop {stop} · TP {target}{tp_note}"
    loss = abs(avg - stop) * exec_qty
    return (
        f"✅ เข้า {symbol} {side_l}\n"
        f"fill {avg} × {exec_qty}\n"
        f"stop {stop} · TP {target}\n"
        f"ถ้าชน stop ≈ {loss.quantize(Decimal('0.01'))} USDT"
        f"{tp_note}"
    )


def _move_stop_buffer_ok(*, side: str, mark: Decimal, new_stop: Decimal, tick: Decimal) -> bool:
    """True when the new stop is far enough from mark (max(0.1% mark, 2×tick))."""
    min_gap = max(mark * Decimal("0.001"), tick * Decimal("2"))
    if side == "long":
        return mark - new_stop >= min_gap
    return new_stop - mark >= min_gap


def _stop_owner_label(order: dict[str, Any]) -> str:
    cid = str(order.get("clientAlgoId") or order.get("clientOrderId") or "")
    return "jv-" if cid.startswith("jv-") else "owner"


def _cancel_old_stop_label(order: dict[str, Any]) -> str:
    px = order_stop_price(order)
    px_s = fmt_price(px) if px is not None else "?"
    return f"{px_s} ({_stop_owner_label(order)})"


def _send_fresh_move_stop_advice(
    conn: Any,
    *,
    symbol: str,
    side: str,
    pos: FuturesPosition,
    new_stop: Decimal,
    source: str,
    initial_stop: Decimal,
    protective: Decimal | None,
    mark: Decimal,
    tick: Decimal,
    now_ms_value: int,
    send: Callable[..., Any] | None,
) -> str | None:
    """Insert a new move_stop intent and send advice. Returns new intent id or None."""
    expires_at = int(now_ms_value) + MOVE_STOP_TTL_MS
    text = format_move_stop_advice(
        symbol=symbol,
        side=side,
        qty=pos.qty,
        old_stop=protective,
        new_stop=new_stop,
        mark=mark,
        entry=pos.entry_price,
        initial_stop=initial_stop,
        expires_at_ms=expires_at,
        tick=tick,
    )
    new_id = insert_exec_intent(
        conn,
        kind="move_stop",
        symbol=symbol,
        side=side,
        preview={
            "stop": str(new_stop),
            "qty": str(pos.qty),
            "advice_text": text,
            "source": source,
        },
        created_at_ms=int(now_ms_value),
        expires_at_ms=expires_at,
    )
    if send is not None:
        mid = send(text, reply_markup=move_stop_keyboard(new_id, new_stop=fmt_price(new_stop)))
        if mid is not None:
            set_exec_intent_message_id(conn, new_id, int(mid))
    return new_id


def run_move_stop_sequence(
    conn: Any,
    intent_id: str,
    *,
    trade: BinanceTradeClient,
    send: Callable[..., Any] | None = None,
    now_ms_value: int | None = None,
) -> str:
    """Place a tighter STOP_MARKET (qty+reduceOnly), then cancel old protective stops."""
    intent = get_exec_intent(conn, intent_id)
    if intent is None:
        return "ไม่พบคำสั่ง"
    preview = intent["preview"]
    symbol = str(intent["symbol"])
    side = str(intent["side"])
    button_stop = Decimal(str(preview["stop"]))
    ts = int(now_ms_value if now_ms_value is not None else now_ms())
    positions = trade.position_risk(symbol=symbol)
    if not positions:
        return MSG_ALREADY_CLOSED
    pos = positions[0]
    if pos.side != side:
        return _reason_th("wrong_side")
    filters = trade.symbol_filters(symbol)
    tick = filters.tick_size if filters.tick_size > 0 else Decimal("0.01")
    button_stop = round_level_to_tick(button_stop, tick, side=side, kind="stop")

    watch = get_watch_position(conn, symbol, pos.position_side)
    if watch is None or watch.get("initial_stop") is None:
        return MSG_MOVE_STOP_NO_ADVICE
    initial_stop = Decimal(str(watch["initial_stop"]))
    entry_time = int(watch.get("entry_time_ms") or ts)
    candles_1h = load_candles_in_range(conn, symbol, "1h", since_ms=entry_time, until_ms=ts)
    advice = current_trail_advice(
        side=side,
        entry=pos.entry_price,
        initial_stop=initial_stop,
        candles_1h=candles_1h,
        entry_time_ms=entry_time,
        now_ms=ts,
        tick=tick,
    )
    if advice is None:
        return MSG_MOVE_STOP_NO_ADVICE
    fresh_stop, source = advice
    if abs(fresh_stop - button_stop) > tick:
        orders = trade.all_open_orders(symbol=symbol)
        protective = find_protective_stop(
            orders, symbol=symbol, position_side=pos.position_side, trade_side=side
        )
        mark = trade.mark_price(symbol)
        _send_fresh_move_stop_advice(
            conn,
            symbol=symbol,
            side=side,
            pos=pos,
            new_stop=fresh_stop,
            source=source,
            initial_stop=initial_stop,
            protective=protective,
            mark=mark,
            tick=tick,
            now_ms_value=ts,
            send=send,
        )
        return f"stop แนะนำเปลี่ยนเป็น {fmt_price(fresh_stop)}"

    new_stop = button_stop
    mark = trade.mark_price(symbol)
    if not _move_stop_buffer_ok(side=side, mark=mark, new_stop=new_stop, tick=tick):
        return MSG_MOVE_STOP_BUFFER

    orders = trade.all_open_orders(symbol=symbol)
    old_stops = [
        o
        for o in orders
        if is_protective_stop(o, position_side=pos.position_side, trade_side=side)
        and order_stop_price(o) is not None
    ]
    current = find_protective_stop(orders, symbol=symbol, position_side=pos.position_side, trade_side=side)
    compare = current if current is not None else initial_stop
    if side == "long" and new_stop <= compare:
        return _reason_th("not_tighter")
    if side == "short" and new_stop >= compare:
        return _reason_th("not_tighter")

    cid = client_order_id(intent_id, "ms")
    placed = False
    clear_reject_code: int | None = None
    lookup_confirmed_absent = False
    try:
        algo = trade.place_algo_stop(
            symbol=symbol,
            side=_closing_side(side),
            qty=pos.qty,
            trigger=new_stop,
            order_type="STOP_MARKET",
            client_id=cid,
        )
        _log_write(
            conn,
            intent_id=intent_id,
            step="move_stop_place",
            endpoint="/fapi/v1/algoOrder",
            client_order_id=cid,
            http_status=200,
            order_or_algo_id=str(algo.get("algoId") or ""),
        )
        placed = True
    except Exception as exc:  # noqa: BLE001
        code = exc.binance_code if isinstance(exc, TradeClientError) else None
        _log_write(
            conn,
            intent_id=intent_id,
            step="move_stop_place",
            endpoint="/fapi/v1/algoOrder",
            client_order_id=cid,
            http_status=exc.http_status if isinstance(exc, TradeClientError) else None,
            binance_code=code,
        )
        if _is_clear_rejection(exc):
            clear_reject_code = code
        else:
            found = _safe_find_algo(trade, symbol=symbol, client_id=cid)
            if found is not None:
                placed = True
            else:
                # Lookup empty or failed — do not cancel old. Empty list = confirmed absent.
                try:
                    again = trade.find_algo_by_client_id(symbol=symbol, client_id=cid)
                    lookup_confirmed_absent = again is None
                except Exception:  # noqa: BLE001
                    lookup_confirmed_absent = False
        if not placed:
            if clear_reject_code is not None:
                return f"{MSG_MOVE_STOP_KEEP} ({clear_reject_code})"
            if lookup_confirmed_absent:
                return MSG_MOVE_STOP_KEEP
            return MSG_MOVE_STOP_UNKNOWN

    cancelled: list[str] = []
    cancel_failed = False
    for old in old_stops:
        algo_id = old.get("algoId") or old.get("orderId")
        if algo_id is None:
            continue
        label = _cancel_old_stop_label(old)
        try:
            trade.cancel_algo(symbol=symbol, algo_id=algo_id)
            _log_write(
                conn,
                intent_id=intent_id,
                step="move_stop_cancel",
                endpoint="/fapi/v1/algoOrder",
                http_status=200,
                order_or_algo_id=str(algo_id),
            )
            cancelled.append(label)
        except Exception as exc:  # noqa: BLE001
            cancel_failed = True
            code = exc.binance_code if isinstance(exc, TradeClientError) else None
            _log_write(
                conn,
                intent_id=intent_id,
                step="move_stop_cancel",
                endpoint="/fapi/v1/algoOrder",
                http_status=exc.http_status if isinstance(exc, TradeClientError) else None,
                binance_code=code,
                order_or_algo_id=str(algo_id),
            )
    head = f"✅ เลื่อน stop {symbol} → {fmt_price(new_stop)} · qty ทั้งไม้ {fmt_qty(pos.qty)}"
    if cancel_failed:
        return f"{head} · {MSG_MOVE_STOP_TWO}"
    if cancelled:
        return f"{head} · ยกเลิก {', '.join(cancelled)}"
    return head


def run_exit_sequence(
    conn: Any,
    intent_id: str,
    *,
    trade: BinanceTradeClient,
) -> str:
    intent = get_exec_intent(conn, intent_id)
    if intent is None:
        return "ไม่พบคำสั่ง"
    symbol = str(intent["symbol"])
    side = str(intent["side"])
    positions = trade.position_risk(symbol=symbol)
    if not positions:
        return MSG_ALREADY_CLOSED
    pos = positions[0]
    if pos.side != side:
        return _reason_th("wrong_side")
    cid = client_order_id(intent_id, "ex")
    fill: dict[str, Any] | None = None
    try:
        fill = trade.market_order(
            symbol=symbol,
            side=_closing_side(side),
            qty=pos.qty,
            reduce_only=True,
            client_id=cid,
        )
        _log_write(
            conn,
            intent_id=intent_id,
            step="exit_market",
            endpoint="/fapi/v1/order",
            client_order_id=cid,
            http_status=200,
            order_or_algo_id=str(fill.get("orderId") or ""),
        )
    except Exception as exc:  # noqa: BLE001
        code = exc.binance_code if isinstance(exc, TradeClientError) else None
        _log_write(
            conn,
            intent_id=intent_id,
            step="exit_market",
            endpoint="/fapi/v1/order",
            client_order_id=cid,
            http_status=exc.http_status if isinstance(exc, TradeClientError) else None,
            binance_code=code,
        )
        if _is_clear_rejection(exc):
            return f"ปิดไม้ไม่สำเร็จ: {exc}"
        recovered = _query_fill_after_unknown(
            trade,
            symbol=symbol,
            client_id=cid,
            conn=conn,
            intent_id=intent_id,
            step="exit_query",
        )
        if recovered is not None and Decimal(str(recovered.get("executedQty") or "0")) > 0:
            fill = recovered
        else:
            try:
                still = trade.position_risk(symbol=symbol)
            except Exception:  # noqa: BLE001
                still = [pos]
            if still:
                return "ปิดไม้ไม่สำเร็จ"
            fill = {
                "executedQty": str(pos.qty),
                "avgPrice": str(pos.entry_price),
                "orderId": "",
            }

    assert fill is not None
    avg = _positive_fill_price(fill)
    if avg is None:
        queried = _query_fill_after_unknown(
            trade,
            symbol=symbol,
            client_id=cid,
            conn=conn,
            intent_id=intent_id,
            step="exit_price_query",
        )
        avg = _positive_fill_price(queried)
        if avg is not None:
            logger.info("exit fill price source=order_query intent_id=%s", intent_id)
        else:
            logger.info("exit fill price source=unknown intent_id=%s", intent_id)
    pnl = Decimal("0")
    try:
        order_id = fill.get("orderId")
        if order_id not in (None, ""):
            pnl = trade.realized_pnl_for_order(symbol=symbol, order_id=order_id)
    except TradeClientError:
        pass

    for order in trade.all_open_orders(symbol=symbol):
        if not is_protective_stop(order, position_side=pos.position_side, trade_side=side):
            # Also cancel TP (same closing side reduce-only conditionals)
            otype = str(order.get("type") or order.get("orderType") or "").upper()
            if otype not in {
                "STOP_MARKET",
                "TAKE_PROFIT_MARKET",
                "STOP",
                "TAKE_PROFIT",
                "TRAILING_STOP_MARKET",
            }:
                continue
            if str(order.get("side") or "").upper() != _closing_side(side):
                continue
        algo_id = order.get("algoId")
        if algo_id is None:
            continue
        try:
            trade.cancel_algo(symbol=symbol, algo_id=algo_id)
            _log_write(
                conn,
                intent_id=intent_id,
                step="exit_cancel",
                endpoint="/fapi/v1/algoOrder",
                http_status=200,
                order_or_algo_id=str(algo_id),
            )
        except TradeClientError as exc:
            _log_write(
                conn,
                intent_id=intent_id,
                step="exit_cancel",
                endpoint="/fapi/v1/algoOrder",
                http_status=exc.http_status,
                binance_code=exc.binance_code,
                order_or_algo_id=str(algo_id),
            )
    pnl_s = pnl.quantize(Decimal("0.01"))
    if avg is None or avg <= 0:
        return f"✅ ปิด {symbol} · PnL {pnl_s} USDT"
    return f"✅ ปิด {symbol} @ {avg} · PnL {pnl_s} USDT"


def cancel_jv_orders_for_symbol(trade: BinanceTradeClient, symbol: str) -> tuple[list[str], bool]:
    """Cancel leftover jv- reduce-only algo orders. Never touches non-jv ids.

    Returns (cancelled_algo_ids, all_confirmed). Unknown-order (-2011/-2013) counts
    as confirmed. Any other failure logs the exception class and is not confirmed.
    """
    cancelled: list[str] = []
    all_ok = True
    for order in trade.open_algo_orders(symbol=symbol):
        cid = str(order.get("clientAlgoId") or order.get("clientOrderId") or "")
        if not cid.startswith("jv-"):
            continue
        algo_id = order.get("algoId")
        if algo_id is None:
            continue
        try:
            trade.cancel_algo(symbol=symbol, algo_id=algo_id)
            cancelled.append(str(algo_id))
        except TradeClientError as exc:
            if exc.binance_code in {-2011, -2013}:
                continue
            all_ok = False
            logger.warning(
                "orphan cancel failed %s %s: %s",
                symbol,
                algo_id,
                type(exc).__name__,
            )
        except Exception as exc:  # noqa: BLE001
            all_ok = False
            logger.warning(
                "orphan cancel failed %s %s: %s",
                symbol,
                algo_id,
                type(exc).__name__,
            )
    return cancelled, all_ok


def handle_exec_callback(
    conn: Any,
    data: str,
    *,
    send: SendFn,
    answer: AnswerFn,
    edit: EditFn,
    trade: BinanceTradeClient | None = None,
    read_futures: Any = None,
    now_ms_value: int | None = None,
    message: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Route ec:/ey:/en:/ms:/xx: callbacks. Caller already enforced owner chat."""
    ts = int(now_ms_value if now_ms_value is not None else now_ms())
    msg = message or {}
    mid = msg.get("message_id")

    if not execution_buttons_enabled(conn) and not data.startswith("en:"):
        answer("ปิดอยู่")
        send(MSG_OFF)
        return {"ok": False, "reason": "off"}

    if data.startswith("ec:"):
        answer()
        payload = data[3:]
        # ec:SYMBOL:l|s  or  ec:signal_id
        symbol = ""
        side = ""
        signal_id = None
        if ":" in payload and payload.rsplit(":", 1)[-1] in {"l", "s"}:
            symbol, side_code = payload.rsplit(":", 1)
            side = "long" if side_code == "l" else "short"
        else:
            row = get_signal(conn, payload)
            if row is None:
                send("ไม่พบสัญญาณ")
                return {"ok": False, "reason": "no_signal"}
            symbol = str(row["symbol"])
            side = str(row["side"])
            signal_id = payload
        if symbol.upper() not in ASSIST_SYMBOLS:
            send("ไม่อยู่ในจักรวาล")
            return {"ok": False, "reason": "universe"}
        preview = create_entry_preview(
            conn,
            symbol=symbol,
            side=side,
            trade=trade,
            read_futures=read_futures,
            now_ms_value=ts,
        )
        if not preview.get("ok"):
            send(str(preview.get("text") or MSG_OFF))
            return preview
        intent_id = str(preview["intent_id"])
        if signal_id:
            # Attach signal id into preview for taken marking
            intent = get_exec_intent(conn, intent_id)
            if intent is not None:
                p = dict(intent["preview"])
                p["signal_id"] = signal_id
                conn.execute(
                    "UPDATE assist_exec_intent SET preview_json=? WHERE id=?",
                    (json.dumps(p, separators=(",", ":"), default=str), intent_id),
                )
                conn.commit()
        new_mid = send(preview["text"], reply_markup=entry_confirm_keyboard(intent_id))
        if new_mid is not None:
            set_exec_intent_message_id(conn, intent_id, int(new_mid))
        return {"ok": True, "preview": intent_id}

    if data.startswith("en:") or data.startswith("mn:"):
        answer()
        intent_id = data[3:]
        intent = get_exec_intent(conn, intent_id)
        cancel_exec_intent(conn, intent_id)
        advice_body = None
        if intent is not None:
            preview = intent.get("preview") or {}
            if isinstance(preview, dict):
                advice_body = preview.get("advice_text")
        edit_body = str(advice_body) if advice_body else MSG_CANCELLED
        if mid is not None:
            edit(int(mid), edit_body, reply_markup={"inline_keyboard": []})
        else:
            send(MSG_CANCELLED)
        return {"ok": True, "cancelled": intent_id}

    if data.startswith("ey:") or data.startswith("ms:") or data.startswith("xx:"):
        prefix = data[:3]
        intent_id = data[3:]
        intent = get_exec_intent(conn, intent_id)
        if intent is None:
            answer()
            send("ไม่พบคำสั่ง")
            return {"ok": False}
        if intent["status"] in {"done", "failed"}:
            answer()
            text = str(intent.get("result_text") or "ทำไปแล้ว")
            send(text)
            return {"ok": True, "replay": True, "text": text}
        if intent["status"] == "cancelled":
            answer()
            send(MSG_CANCELLED)
            return {"ok": False, "reason": "cancelled"}
        if intent["status"] == "confirmed":
            answer()
            send("กำลังทำอยู่")
            return {"ok": False, "reason": "in_flight"}
        if prefix in {"ey:", "ms:"} and intent["expires_at_ms"] < ts:
            finish_exec_intent(conn, intent_id, status="expired", result_text=MSG_EXPIRED)
            answer()
            advice_body = None
            preview = intent.get("preview") or {}
            if isinstance(preview, dict):
                advice_body = preview.get("advice_text")
            edit_body = str(advice_body) if (prefix == "ms:" and advice_body) else MSG_EXPIRED
            if mid is not None:
                edit(int(mid), edit_body, reply_markup={"inline_keyboard": []})
            send(MSG_EXPIRED)
            return {"ok": False, "reason": "expired"}
        if not claim_exec_intent(conn, intent_id):
            # Lost race — replay stored or in-flight
            again = get_exec_intent(conn, intent_id)
            answer()
            if again and again.get("result_text"):
                send(str(again["result_text"]))
                return {"ok": True, "replay": True}
            send("กำลังทำอยู่")
            return {"ok": False, "reason": "race"}

        answer()
        auth_reason: str | None = None
        client: BinanceTradeClient | None
        if trade is not None:
            client = trade
        else:
            client, auth_reason = build_trade_client()
        if client is None:
            text = trade_auth_reason_th(auth_reason)
            finish_exec_intent(conn, intent_id, status="failed", result_text=text)
            send(text)
            return {"ok": False, "reason": auth_reason or "no_key"}
        try:
            client.assert_trade_key_safe()
            if prefix == "ey:":
                client.assert_account_modes()
        except TradeClientError as exc:
            text = str(exc) if str(exc) in {MSG_ONE_WAY, MSG_MULTI_ASSETS} else MSG_KEY_REFUSED
            finish_exec_intent(conn, intent_id, status="failed", result_text=text)
            send(text)
            return {"ok": False, "reason": "key"}
        except (httpx.HTTPError, ProviderError) as exc:
            logger.warning("exec pre-order network error %s", type(exc).__name__)
            text = MSG_BINANCE_NOT_SENT
            finish_exec_intent(conn, intent_id, status="failed", result_text=text)
            if mid is not None and prefix == "ey:":
                edit(int(mid), text, reply_markup={"inline_keyboard": []})
            send(text)
            return {"ok": False, "reason": "binance_unreachable"}

        def _strip_ms_buttons() -> None:
            if mid is None or prefix != "ms:":
                return
            preview = intent.get("preview") or {}
            body = preview.get("advice_text") if isinstance(preview, dict) else None
            edit(int(mid), str(body) if body else MSG_CANCELLED, reply_markup={"inline_keyboard": []})

        def _finish_send(result: str, *, markup: dict[str, Any] | None = None) -> dict[str, Any]:
            status = "done" if result.startswith("✅") else "failed"
            finish_exec_intent(conn, intent_id, status=status, result_text=result)
            if prefix == "ms:":
                _strip_ms_buttons()
                if markup is not None:
                    send(result, reply_markup=markup)
                else:
                    send(result)
            elif mid is not None and markup is None and prefix == "ey:":
                edit(int(mid), result, reply_markup={"inline_keyboard": []})
                # Telegram does not notify on edits — send a new message for failures.
                if not result.startswith("✅"):
                    send(result)
            elif markup is not None:
                send(result, reply_markup=markup)
            else:
                send(result)
            return {"ok": status == "done", "result": result}

        try:
            if prefix == "ey:":
                try:
                    guard = _confirm_guards_entry(conn, intent, client, now_ms_value=ts)
                except (httpx.HTTPError, ProviderError) as exc:
                    logger.warning("confirm guards network error %s", type(exc).__name__)
                    text = MSG_BINANCE_NOT_SENT
                    finish_exec_intent(conn, intent_id, status="failed", result_text=text)
                    if mid is not None:
                        edit(int(mid), text, reply_markup={"inline_keyboard": []})
                    send(text)
                    return {"ok": False, "reason": "binance_unreachable"}
                if guard:
                    text = _reason_th(guard)
                    finish_exec_intent(conn, intent_id, status="failed", result_text=text)
                    if mid is not None:
                        edit(int(mid), text, reply_markup={"inline_keyboard": []})
                        send(text)
                    else:
                        send(text)
                    return {"ok": False, "reason": guard}
                result = run_entry_sequence(conn, intent_id, trade=client)
                return _finish_send(result)

            if prefix == "ms:":
                result = run_move_stop_sequence(
                    conn,
                    intent_id,
                    trade=client,
                    send=send,
                    now_ms_value=ts,
                )
                return _finish_send(result)

            result = run_exit_sequence(conn, intent_id, trade=client)
            return _finish_send(result)
        except Exception:  # noqa: BLE001
            logger.exception("exec callback failed after claim intent_id=%s", intent_id)
            # An entry or exit may have reached Binance; tell the owner to look.
            text = MSG_HANDLER_ERROR if prefix == "ms:" else _msg_check_now(str(intent["symbol"]))
            finish_exec_intent(conn, intent_id, status="failed", result_text=text)
            if prefix == "ms:":
                _strip_ms_buttons()
            send(text)
            return {"ok": False, "reason": "handler_error"}

    return {"ok": False, "unhandled": data}
