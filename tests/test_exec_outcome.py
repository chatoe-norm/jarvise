"""Unit tests for assist order outcome classification and stop read-back."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from jarvise_assist.exec_outcome import (
    CLEAR_REJECTION_CODES,
    OrderOutcome,
    QueryResultKind,
    StopVerifyResult,
    classify_exception,
    classify_order_body,
    classify_send,
    is_clear_rejection_code,
    query_fill_after_unknown,
    verify_our_stop,
)


class _FakeExc(Exception):
    def __init__(self, *, code: int | None = None, http_status: int | None = None) -> None:
        super().__init__("x")
        self.binance_code = code
        self.http_status = http_status


class _FakeTrade:
    def __init__(
        self,
        *,
        open_rows: list[dict[str, Any]] | BaseException | None = None,
        algo: dict[str, Any] | BaseException | None = None,
        order: dict[str, Any] | BaseException | None = None,
    ) -> None:
        self.open_rows = open_rows if open_rows is not None else []
        self.algo = algo
        self.order = order
        self.query_calls = 0

    def open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, Any]]:
        if isinstance(self.open_rows, BaseException):
            raise self.open_rows
        return list(self.open_rows)

    def query_algo_by_client_id(self, *, symbol: str, client_id: str) -> dict[str, Any]:
        if isinstance(self.algo, BaseException):
            raise self.algo
        if self.algo is None:
            raise _FakeExc(code=-2013, http_status=400)
        return self.algo

    def query_order_by_client_id(self, *, symbol: str, client_id: str) -> dict[str, Any]:
        self.query_calls += 1
        if isinstance(self.order, BaseException):
            raise self.order
        if self.order is None:
            raise _FakeExc(code=-2013, http_status=400)
        return self.order

    def find_algo_by_client_id(self, *, symbol: str, client_id: str) -> dict[str, Any] | None:
        for row in self.open_algo_orders(symbol=symbol):
            if str(row.get("clientAlgoId") or "") == client_id:
                return row
        return None


def _noop(_s: float) -> None:
    return None


def test_allowlist_codes_are_clear_rejections() -> None:
    for code in CLEAR_REJECTION_CODES:
        assert is_clear_rejection_code(code, http_status=400) is True
    assert is_clear_rejection_code(-1007, http_status=400) is False
    assert is_clear_rejection_code(-2010, http_status=503) is False
    assert is_clear_rejection_code(-4116, http_status=400) is False


def test_classify_send_filled_not_filled_unknown() -> None:
    assert classify_order_body({"executedQty": "10", "status": "FILLED"}) is OrderOutcome.FILLED
    assert classify_order_body({"executedQty": "0", "status": "CANCELED"}) is OrderOutcome.NOT_FILLED
    assert classify_order_body({"executedQty": "0", "status": "NEW"}) is OrderOutcome.UNKNOWN
    assert classify_exception(_FakeExc(code=-2019, http_status=400)) is OrderOutcome.NOT_FILLED
    assert classify_exception(_FakeExc(code=-1007, http_status=400)) is OrderOutcome.UNKNOWN
    assert classify_send(exc=_FakeExc(code=-2010, http_status=400)) is OrderOutcome.NOT_FILLED


def test_query_fill_not_found_only_after_all_2013() -> None:
    trade = _FakeTrade(order=_FakeExc(code=-2013, http_status=400))
    out = query_fill_after_unknown(trade, symbol="DOGEUSDT", client_id="jv-x-mkt", sleep=_noop)
    assert out.kind is QueryResultKind.NOT_FOUND
    assert trade.query_calls == 3


def test_query_fill_unreadable_on_mixed_errors() -> None:
    calls = {"n": 0}

    class _Trade(_FakeTrade):
        def query_order_by_client_id(self, *, symbol: str, client_id: str) -> dict[str, Any]:
            calls["n"] += 1
            if calls["n"] == 1:
                raise _FakeExc(code=-2013, http_status=400)
            raise ConnectionError("down")

    out = query_fill_after_unknown(_Trade(), symbol="DOGEUSDT", client_id="jv-x-mkt", sleep=_noop)
    assert out.kind is QueryResultKind.UNREADABLE


def test_verify_our_stop_states() -> None:
    row = {
        "symbol": "DOGEUSDT",
        "type": "STOP_MARKET",
        "side": "BUY",
        "positionSide": "BOTH",
        "reduceOnly": True,
        "triggerPrice": "0.083",
        "quantity": "100",
        "algoId": 11,
        "clientAlgoId": "jv-x-sl0",
        "algoStatus": "NEW",
        "workingType": "MARK_PRICE",
    }
    ok = verify_our_stop(
        _FakeTrade(open_rows=[row]),
        symbol="DOGEUSDT",
        client_id="jv-x-sl0",
        closing_side="BUY",
        planned_stop=Decimal("0.083"),
        position_qty=Decimal("100"),
        position_flat=False,
    )
    assert ok is StopVerifyResult.VERIFIED

    missing = verify_our_stop(
        _FakeTrade(open_rows=[], algo=_FakeExc(code=-2013, http_status=400)),
        symbol="DOGEUSDT",
        client_id="jv-x-sl0",
        closing_side="BUY",
        planned_stop=Decimal("0.083"),
        position_qty=Decimal("100"),
        position_flat=False,
    )
    assert missing is StopVerifyResult.NOT_PROTECTED

    bad_get = verify_our_stop(
        _FakeTrade(open_rows=ConnectionError("down")),
        symbol="DOGEUSDT",
        client_id="jv-x-sl0",
        closing_side="BUY",
        planned_stop=Decimal("0.083"),
        position_qty=Decimal("100"),
        position_flat=False,
    )
    assert bad_get is StopVerifyResult.UNVERIFIABLE

    triggered = dict(row)
    triggered["algoStatus"] = "TRIGGERED"
    closed = verify_our_stop(
        _FakeTrade(open_rows=[], algo=triggered),
        symbol="DOGEUSDT",
        client_id="jv-x-sl0",
        closing_side="BUY",
        planned_stop=Decimal("0.083"),
        position_qty=Decimal("100"),
        position_flat=True,
    )
    assert closed is StopVerifyResult.CLOSED_BY_STOP
