"""Classify Binance USDⓈ-M order outcomes and verify our own protective stops.

An unknown outcome is never treated as failed. Stop protection is proven only
after a field-level read-back of our jv- algo ids.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Protocol

# Explicit final-rejection allowlist from Binance USDⓈ-M error-code docs.
# Codes not listed (including -1001/-1006/-1007/-4116) are UNKNOWN.
CLEAR_REJECTION_CODES: frozenset[int] = frozenset(
    {
        -1021,  # INVALID_TIMESTAMP
        -1022,  # INVALID_SIGNATURE
        -1102,  # MANDATORY_PARAM_EMPTY_OR_MALFORMED
        -1106,  # PARAM_NOT_REQUIRED
        -1111,  # BAD_PRECISION
        -1116,  # INVALID_ORDER_TYPE
        -1117,  # INVALID_SIDE
        -2010,  # NEW_ORDER_REJECTED
        -2014,  # BAD_API_KEY_FMT
        -2015,  # REJECTED_MBX_KEY
        -2019,  # MARGIN_NOT_SUFFICIENT
        -2021,  # ORDER_WOULD_IMMEDIATELY_TRIGGER
        -2022,  # REDUCE_ONLY_REJECT
        -4003,  # QTY_LESS_THAN_ZERO
        -4061,  # POSITION_SIDE_NOT_MATCH
        -4164,  # MIN_NOTIONAL
    }
)

_TERMINAL_ZERO_STATUSES = frozenset({"CANCELED", "CANCELLED", "EXPIRED", "REJECTED"})
_ACTIVE_ALGO = "NEW"
_TRIGGERED_ALGO = frozenset({"TRIGGERED", "FINISHED"})


class OrderOutcome(str, Enum):
    FILLED = "filled"
    NOT_FILLED = "not_filled"
    UNKNOWN = "unknown"


class QueryResultKind(str, Enum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    UNREADABLE = "unreadable"


class StopVerifyResult(str, Enum):
    VERIFIED = "verified"
    NOT_PROTECTED = "not_protected"
    UNVERIFIABLE = "unverifiable"
    CLOSED_BY_STOP = "closed_by_stop"


@dataclass(frozen=True)
class QueryFillResult:
    kind: QueryResultKind
    order: dict[str, Any] | None = None


class _TradeClientProto(Protocol):
    def query_order_by_client_id(self, *, symbol: str, client_id: str) -> dict[str, Any]: ...

    def open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, Any]]: ...

    def query_algo_by_client_id(self, *, symbol: str, client_id: str) -> dict[str, Any]: ...

    def find_algo_by_client_id(self, *, symbol: str, client_id: str) -> dict[str, Any] | None: ...


def _as_decimal(raw: Any) -> Decimal:
    try:
        return Decimal(str(raw or "0"))
    except Exception:  # noqa: BLE001
        return Decimal("0")


def executed_qty(order: dict[str, Any] | None) -> Decimal:
    if not isinstance(order, dict):
        return Decimal("0")
    return _as_decimal(order.get("executedQty"))


def is_clear_rejection_code(code: int | None, *, http_status: int | None = None) -> bool:
    """True only for an allowlisted final rejection with HTTP < 500."""
    if code is None:
        return False
    if http_status is not None and http_status >= 500:
        return False
    return int(code) in CLEAR_REJECTION_CODES


def classify_exception(exc: BaseException) -> OrderOutcome:
    """Map a send exception to FILLED / NOT_FILLED / UNKNOWN. Never FILLED here."""
    code = getattr(exc, "binance_code", None)
    http_status = getattr(exc, "http_status", None)
    if is_clear_rejection_code(code if isinstance(code, int) else None, http_status=http_status):
        return OrderOutcome.NOT_FILLED
    return OrderOutcome.UNKNOWN


def classify_order_body(order: dict[str, Any] | None) -> OrderOutcome:
    """Classify a successful or queried order body."""
    if not isinstance(order, dict):
        return OrderOutcome.UNKNOWN
    qty = executed_qty(order)
    if qty > 0:
        return OrderOutcome.FILLED
    status = str(order.get("status") or "").upper()
    if status in _TERMINAL_ZERO_STATUSES:
        return OrderOutcome.NOT_FILLED
    return OrderOutcome.UNKNOWN


def classify_send(
    *,
    response: dict[str, Any] | None = None,
    exc: BaseException | None = None,
) -> OrderOutcome:
    if exc is not None:
        return classify_exception(exc)
    return classify_order_body(response)


def query_fill_after_unknown(
    trade: _TradeClientProto,
    *,
    symbol: str,
    client_id: str,
    sleep: Callable[[float], None],
    attempts: int = 3,
    delay_s: float = 0.5,
    on_attempt: Callable[[int, Exception | None, dict[str, Any] | None], None] | None = None,
) -> QueryFillResult:
    """Distinct found / not_found / unreadable. A lone early -2013 is not not_found."""
    saw_other = False
    all_not_found = True
    last_order: dict[str, Any] | None = None
    for i in range(attempts):
        if i > 0:
            sleep(delay_s)
        try:
            order = trade.query_order_by_client_id(symbol=symbol, client_id=client_id)
            if on_attempt is not None:
                on_attempt(i, None, order)
            last_order = order
            return QueryFillResult(kind=QueryResultKind.FOUND, order=order)
        except Exception as exc:  # noqa: BLE001
            if on_attempt is not None:
                on_attempt(i, exc, None)
            code = getattr(exc, "binance_code", None)
            if code == -2013:
                continue
            all_not_found = False
            saw_other = True
            # Keep trying; a later pass may succeed.
            continue
    if all_not_found and not saw_other:
        return QueryFillResult(kind=QueryResultKind.NOT_FOUND, order=None)
    return QueryFillResult(kind=QueryResultKind.UNREADABLE, order=last_order)


def _order_type(order: dict[str, Any]) -> str:
    return str(order.get("type") or order.get("orderType") or "").upper()


def _trigger_price(order: dict[str, Any]) -> Decimal | None:
    for key in ("triggerPrice", "stopPrice"):
        raw = order.get(key)
        if raw is None or str(raw) in {"", "0", "0.0", "0.00000000"}:
            continue
        px = _as_decimal(raw)
        if px > 0:
            return px
    return None


def _order_qty(order: dict[str, Any]) -> Decimal | None:
    for key in ("quantity", "origQty"):
        if key not in order:
            continue
        raw = order.get(key)
        if raw is None or str(raw) in {"", "0", "0.0", "0.00000000"}:
            continue
        qty = _as_decimal(raw)
        if qty > 0:
            return qty
    return None


def _reduce_only(order: dict[str, Any]) -> bool:
    raw = order.get("reduceOnly")
    if raw is True:
        return True
    return str(raw or "").lower() in {"true", "1"}


def _matches_verified_fields(
    order: dict[str, Any],
    *,
    symbol: str,
    closing_side: str,
    planned_stop: Decimal,
    position_qty: Decimal,
) -> bool:
    if str(order.get("symbol") or "").upper() != symbol.upper():
        return False
    if str(order.get("side") or "").upper() != closing_side.upper():
        return False
    if _order_type(order) != "STOP_MARKET":
        return False
    status = str(order.get("algoStatus") or order.get("status") or "").upper()
    if status != _ACTIVE_ALGO:
        return False
    trigger = _trigger_price(order)
    if trigger is None or trigger != planned_stop:
        return False
    if str(order.get("workingType") or "").upper() != "MARK_PRICE":
        return False
    if not _reduce_only(order):
        return False
    if str(order.get("positionSide") or "BOTH").upper() != "BOTH":
        return False
    qty = _order_qty(order)
    if qty is None or qty < position_qty:
        return False
    return True


def verify_our_stop(
    trade: _TradeClientProto,
    *,
    symbol: str,
    client_id: str,
    closing_side: str,
    planned_stop: Decimal,
    position_qty: Decimal,
    position_flat: bool,
    open_list: list[dict[str, Any]] | None = None,
    query_if_missing: bool = True,
) -> StopVerifyResult:
    """Prove our own stop by client id. A failed GET is UNVERIFIABLE, never missing."""
    rows: list[dict[str, Any]]
    if open_list is None:
        try:
            rows = trade.open_algo_orders(symbol=symbol)
        except Exception:  # noqa: BLE001
            return StopVerifyResult.UNVERIFIABLE
    else:
        rows = open_list

    found: dict[str, Any] | None = None
    for order in rows:
        cid = str(order.get("clientAlgoId") or order.get("clientOrderId") or "")
        if cid == client_id:
            found = order
            break

    if found is None:
        if not query_if_missing:
            return StopVerifyResult.NOT_PROTECTED
        try:
            found = trade.query_algo_by_client_id(symbol=symbol, client_id=client_id)
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "binance_code", None)
            if code in {-2011, -2013}:
                return StopVerifyResult.NOT_PROTECTED
            return StopVerifyResult.UNVERIFIABLE

    if found is None:
        return StopVerifyResult.NOT_PROTECTED

    status = str(found.get("algoStatus") or found.get("status") or "").upper()
    if status in _TRIGGERED_ALGO and position_flat:
        return StopVerifyResult.CLOSED_BY_STOP

    if _matches_verified_fields(
        found,
        symbol=symbol,
        closing_side=closing_side,
        planned_stop=planned_stop,
        position_qty=position_qty,
    ):
        return StopVerifyResult.VERIFIED
    return StopVerifyResult.NOT_PROTECTED
