"""Real-vs-virtual report: window walk, matching, slippage, weekly lines (fakes only)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.test_assist import _ms_bangkok

from jarvise_assist.futures import FAPI_HISTORY_WINDOW_MS, SymbolFilters
from jarvise_assist.real_trades import (
    SPEC_START_MS,
    adverse_bps,
    classify_exit_kind,
    fetch_all_orders,
    fetch_all_user_trades,
    format_real_lines,
    run_real_report,
    summarize_real_vs_virtual,
    sync_real_trades,
    time_windows,
)
from jarvise_assist.store import insert_exec_intent, insert_exec_log, set_exec_intent_ref
from jarvise_assist.track import TRACK_COHORT
from jarvise_assist.weekly import format_weekly_summary
from jarvise_ingest.db import SCHEMA_VERSION, open_db


class FakeFutures:
    """In-memory stand-in for BinanceFuturesClient history GETs."""

    def __init__(
        self,
        *,
        trades: list[dict[str, Any]] | None = None,
        orders: list[dict[str, Any]] | None = None,
        income: list[dict[str, Any]] | None = None,
        tick_size: Decimal = Decimal("0.1"),
        fail_after_calls: int | None = None,
    ) -> None:
        self.trades = list(trades or [])
        self.orders = list(orders or [])
        self.income = list(income or [])
        self.user_trade_calls: list[dict[str, Any]] = []
        self.all_order_calls: list[dict[str, Any]] = []
        self.income_calls: list[dict[str, Any]] = []
        self.tick_size = tick_size
        self.fail_after_calls = fail_after_calls
        self._call_n = 0

    def _maybe_fail(self) -> None:
        self._call_n += 1
        if self.fail_after_calls is not None and self._call_n > self.fail_after_calls:
            raise RuntimeError("api_mid_sync")

    def symbol_filters(self, symbol: str) -> SymbolFilters:
        return SymbolFilters(
            step_size=Decimal("0.001"),
            min_qty=Decimal("0"),
            min_notional=Decimal("0"),
            tick_size=self.tick_size,
        )

    def user_trades(
        self,
        symbol: str,
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
        from_id: int | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        self._maybe_fail()
        if from_id is not None and (start_ms is not None or end_ms is not None):
            raise ValueError("fromId cannot be sent with startTime or endTime")
        self.user_trade_calls.append(
            {
                "symbol": symbol,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "from_id": from_id,
                "limit": limit,
            }
        )
        rows = [t for t in self.trades if str(t.get("symbol") or "").upper() == symbol.upper()]
        if from_id is not None:
            rows = [t for t in rows if int(t.get("id") or 0) >= int(from_id)]
        else:
            assert start_ms is not None and end_ms is not None
            rows = [t for t in rows if int(start_ms) <= int(t.get("time") or 0) <= int(end_ms)]
        rows.sort(key=lambda r: int(r.get("id") or 0))
        return rows[: int(limit)]

    def all_orders(
        self,
        symbol: str,
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
        order_id: int | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        self._maybe_fail()
        if order_id is not None and (start_ms is not None or end_ms is not None):
            raise ValueError("orderId cannot be sent with startTime or endTime")
        self.all_order_calls.append(
            {
                "symbol": symbol,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "order_id": order_id,
                "limit": limit,
            }
        )
        rows = [o for o in self.orders if str(o.get("symbol") or "").upper() == symbol.upper()]
        if order_id is not None:
            rows = [o for o in rows if int(o.get("orderId") or 0) >= int(order_id)]
        else:
            assert start_ms is not None and end_ms is not None
            rows = [o for o in rows if int(start_ms) <= int(o.get("time") or 0) <= int(end_ms)]
        rows.sort(key=lambda r: int(r.get("orderId") or 0))
        return rows[: int(limit)]

    def income_history(
        self,
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
        income_type: str | None = None,
        limit: int = 1000,
        symbol: str | None = None,
    ) -> list[dict[str, Any]]:
        self._maybe_fail()
        self.income_calls.append(
            {
                "start_ms": start_ms,
                "end_ms": end_ms,
                "income_type": income_type,
                "symbol": symbol,
            }
        )
        rows = []
        for row in self.income:
            if income_type and str(row.get("incomeType") or "") != income_type:
                continue
            if symbol and str(row.get("symbol") or "").upper() != symbol.upper():
                continue
            tms = int(row.get("time") or 0)
            if start_ms is not None and tms < int(start_ms):
                continue
            if end_ms is not None and tms > int(end_ms):
                continue
            rows.append(row)
        return rows[: int(limit)]


def test_time_windows_20_day_span_three_windows() -> None:
    start = 1_000_000_000_000
    end = start + 20 * 24 * 3_600_000
    wins = time_windows(start, end)
    assert len(wins) == 3
    assert wins[0][0] == start
    assert wins[0][1] - wins[0][0] == FAPI_HISTORY_WINDOW_MS
    assert wins[1][0] == wins[0][1]
    assert wins[2][1] == end
    for i in range(len(wins) - 1):
        assert wins[i][1] == wins[i + 1][0]


def test_paging_from_id_without_times() -> None:
    start = SPEC_START_MS
    end = start + 3_600_000
    trades = [
        {
            "id": i + 1,
            "orderId": 100 + i,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "price": "100",
            "qty": "0.01",
            "time": start + 1000 + i,
            "commission": "0",
            "commissionAsset": "USDT",
        }
        for i in range(1005)
    ]
    fake = FakeFutures(trades=trades)
    rows = fetch_all_user_trades(fake, "BTCUSDT", start_ms=start, end_ms=end)  # type: ignore[arg-type]
    assert len(rows) == 1005
    assert fake.user_trade_calls[0]["from_id"] is None
    assert fake.user_trade_calls[0]["start_ms"] is not None
    assert fake.user_trade_calls[1]["from_id"] == 1001
    assert fake.user_trade_calls[1]["start_ms"] is None
    assert fake.user_trade_calls[1]["end_ms"] is None


def test_paging_drops_rows_past_window_end() -> None:
    start = SPEC_START_MS
    end = start + 3_600_000
    inclusive_end = end - 1
    # Force single window + fromId page: fill 1000 in-window then one past on next page.
    bulk = [
        {
            "id": i + 1,
            "orderId": 10 + i,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "price": "100",
            "qty": "0.01",
            "time": start + i + 1,
            "commission": "0",
            "commissionAsset": "USDT",
        }
        for i in range(1000)
    ]
    bulk.append(
        {
            "id": 1001,
            "orderId": 9999,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "price": "100",
            "qty": "0.01",
            "time": inclusive_end + 50,
            "commission": "0",
            "commissionAsset": "USDT",
        }
    )
    fake = FakeFutures(trades=bulk)
    rows = fetch_all_user_trades(fake, "BTCUSDT", start_ms=start, end_ms=end)  # type: ignore[arg-type]
    ids = {int(r["id"]) for r in rows}
    assert 1000 in ids
    assert 1001 not in ids


def test_adverse_bps_entry_and_stop_exit() -> None:
    assert adverse_bps(fill=100.1, ref=100.0, side="long") == pytest.approx(10.0)
    assert adverse_bps(fill=99.9, ref=100.0, side="short") == pytest.approx(10.0)
    # Stop slip uses closing side: long stop is sell → lower fill adverse.
    assert adverse_bps(fill=89.6, ref=90.0, side="sell") == pytest.approx(44.444, abs=0.1)
    assert adverse_bps(fill=100.5, ref=100.0, side="buy") == pytest.approx(50.0)


def test_classify_exit_kinds() -> None:
    kind, detail = classify_exit_kind(
        {"clientOrderId": "jv-abc-sl0", "type": "MARKET", "origType": "MARKET", "stopPrice": ""}
    )
    assert kind == "stop" and detail == "initial"
    kind, detail = classify_exit_kind(
        {"clientOrderId": "jv-abc-sl1", "type": "MARKET", "origType": "MARKET", "stopPrice": ""}
    )
    assert kind == "stop" and detail == "initial"
    kind, detail = classify_exit_kind(
        {"clientOrderId": "jv-abc-ms", "type": "MARKET", "origType": "MARKET", "stopPrice": ""}
    )
    assert kind == "stop" and detail == "moved"
    kind, detail = classify_exit_kind({"clientOrderId": "jv-abc-ex", "type": "MARKET", "origType": "MARKET"})
    assert kind == "owner_exit" and detail is None
    kind, detail = classify_exit_kind({"clientOrderId": "jv-abc-cls", "type": "MARKET", "origType": "MARKET"})
    assert kind == "emergency" and detail is None
    kind, detail = classify_exit_kind(
        {"clientOrderId": "jv-abc-tp", "type": "TAKE_PROFIT_MARKET", "origType": "TAKE_PROFIT_MARKET"}
    )
    assert kind == "tp" and detail is None
    kind, detail = classify_exit_kind({"clientOrderId": "manual-1", "type": "MARKET", "origType": "MARKET"})
    assert kind == "manual" and detail is None
    kind, detail = classify_exit_kind({"clientOrderId": "jv-abc-xx", "type": "LIMIT", "origType": "LIMIT"})
    assert kind == "unknown" and detail is None


def _seed_jv_entry(
    conn: Any,
    *,
    intent_id: str,
    client_id: str,
    preview: dict[str, Any],
    ref_price: str | None = None,
    symbol: str = "BTCUSDT",
    side: str = "long",
) -> None:
    conn.execute(
        """
        INSERT INTO assist_exec_intent (
            id, kind, symbol, side, preview_json, created_at_ms, expires_at_ms, status
        ) VALUES (?, 'entry', ?, ?, ?, 1, 2, 'done')
        """,
        (intent_id, symbol, side, json.dumps(preview)),
    )
    if ref_price is not None:
        set_exec_intent_ref(conn, intent_id, ref_price=ref_price, ref_source="confirm_mark", ref_at_ms=10)
    insert_exec_log(
        conn,
        intent_id=intent_id,
        step="market",
        endpoint="/fapi/v1/order",
        client_order_id=client_id,
        http_status=200,
        created_at_ms=SPEC_START_MS + 1000,
    )
    conn.execute(
        """
        INSERT INTO assist_signal (
            id, symbol, side, entry, stop, target, qty, notional, leverage, loss_at_stop,
            created_at_ms, expires_at_ms, status
        ) VALUES (?, ?, ?, 100, 90, 120, 0.2, 20, 5, 2, ?, ?, 'taken')
        """,
        (preview["signal_id"], symbol, side, SPEC_START_MS, SPEC_START_MS + 1),
    )
    conn.execute(
        """
        INSERT INTO assist_track (
            signal_id, symbol, side, entry, stop, target, qty, sent_at_ms,
            exit_at_ms, exit_px, exit_reason, gross_r, net_r, fee_usdt, pnl_usdt,
            cohort, kind, funding_usdt
        ) VALUES (?, ?, ?, 100, 90, 120, 0.2, ?, ?, 110, 'flat', 1.0, 0.8, 0.1, 1.6, ?, 'sent', 0)
        """,
        (preview["signal_id"], symbol, side, SPEC_START_MS, SPEC_START_MS + 8 * 3_600_000, TRACK_COHORT),
    )
    conn.commit()


def test_sl0_stop_trigger_from_preview_tick(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "sl0.db")
    sid = "as_sl0"
    intent = "aei_sl0001"
    cid = "jv-sl0001-mkt"
    preview = {
        "entry": "100",
        "stop": "90.05",
        "target": "120",
        "qty": "0.2",
        "loss_at_stop": "2",
        "signal_id": sid,
        "leverage": 5,
    }
    _seed_jv_entry(conn, intent_id=intent, client_id=cid, preview=preview, ref_price="100")
    t0 = SPEC_START_MS + 60_000
    orders = [
        {
            "orderId": 11,
            "clientOrderId": cid,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "origType": "MARKET",
            "time": t0,
            "status": "FILLED",
            "reduceOnly": False,
        },
        {
            "orderId": 12,
            "clientOrderId": "jv-sl0001-sl0",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "type": "MARKET",
            "origType": "MARKET",
            "stopPrice": "",
            "time": t0 + 3_600_000,
            "status": "FILLED",
            "reduceOnly": True,
        },
    ]
    trades = [
        {
            "id": 1,
            "orderId": 11,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "price": "100.2",
            "qty": "0.2",
            "time": t0,
            "commission": "0.01",
            "commissionAsset": "USDT",
            "realizedPnl": "0",
        },
        {
            "id": 3,
            "orderId": 12,
            "symbol": "BTCUSDT",
            "side": "SELL",
            "price": "89.6",
            "qty": "0.2",
            "time": t0 + 3_600_000,
            "commission": "0.01",
            "commissionAsset": "USDT",
            "realizedPnl": "-2.1",
        },
    ]
    income = [
        {
            "symbol": "BTCUSDT",
            "incomeType": "REALIZED_PNL",
            "income": "-2.1",
            "time": t0 + 3_600_000,
            "tradeId": 3,
        },
        {
            "symbol": "BTCUSDT",
            "incomeType": "FUNDING_FEE",
            "income": "-0.05",
            "time": t0 + 1_800_000,
        },
    ]
    fake = FakeFutures(trades=trades, orders=orders, income=income, tick_size=Decimal("0.1"))
    sync_real_trades(conn, fake, since_ms=SPEC_START_MS, until_ms=t0 + 10_000_000)  # type: ignore[arg-type]
    row = conn.execute(
        """
        SELECT exit_kind, stop_trigger, slip_stop_bps, exit_order_ids, exit_fill_avg
        FROM assist_real_trade WHERE entry_order_id='11'
        """
    ).fetchone()
    assert row is not None
    assert row[0] == "stop"
    # long stop floors 90.05 → 90.0 with tick 0.1
    assert row[1] == pytest.approx(90.0)
    assert row[2] == pytest.approx(44.444, abs=0.1)
    meta = json.loads(str(row[3]))
    assert meta["stop_detail"] == "initial"
    assert meta["stop_source"] == "entry_preview_stop"
    assert row[4] == pytest.approx(89.6)
    conn.close()


def test_sl1_stop_trigger(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "sl1.db")
    sid = "as_sl1"
    intent = "aei_sl1001"
    cid = "jv-sl1001-mkt"
    preview = {
        "entry": "100",
        "stop": "90",
        "target": "120",
        "qty": "0.1",
        "loss_at_stop": "1",
        "signal_id": sid,
        "leverage": 5,
    }
    _seed_jv_entry(conn, intent_id=intent, client_id=cid, preview=preview, ref_price="100")
    t0 = SPEC_START_MS + 60_000
    orders = [
        {
            "orderId": 21,
            "clientOrderId": cid,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "time": t0,
            "status": "FILLED",
            "reduceOnly": False,
        },
        {
            "orderId": 22,
            "clientOrderId": "jv-sl1001-sl1",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "type": "MARKET",
            "origType": "MARKET",
            "stopPrice": "",
            "time": t0 + 1000,
            "status": "FILLED",
            "reduceOnly": True,
        },
    ]
    trades = [
        {
            "id": 1,
            "orderId": 21,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "price": "100",
            "qty": "0.1",
            "time": t0,
            "commission": "0",
            "commissionAsset": "USDT",
            "realizedPnl": "0",
        },
        {
            "id": 2,
            "orderId": 22,
            "symbol": "BTCUSDT",
            "side": "SELL",
            "price": "90",
            "qty": "0.1",
            "time": t0 + 1000,
            "commission": "0",
            "commissionAsset": "USDT",
            "realizedPnl": "-1",
        },
    ]
    income = [
        {
            "symbol": "BTCUSDT",
            "incomeType": "REALIZED_PNL",
            "income": "-1",
            "time": t0 + 1000,
            "tradeId": 2,
        },
        {
            "symbol": "BTCUSDT",
            "incomeType": "FUNDING_FEE",
            "income": "0",
            "time": t0 + 500,
        },
    ]
    fake = FakeFutures(trades=trades, orders=orders, income=income)
    sync_real_trades(conn, fake, since_ms=SPEC_START_MS, until_ms=t0 + 5000)  # type: ignore[arg-type]
    row = conn.execute(
        "SELECT stop_trigger, exit_order_ids FROM assist_real_trade WHERE entry_order_id='21'"
    ).fetchone()
    assert row is not None and row[0] == pytest.approx(90.0)
    assert json.loads(str(row[1]))["stop_detail"] == "initial"
    conn.close()


def test_ms_stop_trigger_from_move_intent(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "ms.db")
    sid = "as_ms"
    intent = "aei_ms0001"
    cid = "jv-ms0001-mkt"
    preview = {
        "entry": "100",
        "stop": "90",
        "target": "120",
        "qty": "0.1",
        "loss_at_stop": "1",
        "signal_id": sid,
        "leverage": 5,
    }
    _seed_jv_entry(conn, intent_id=intent, client_id=cid, preview=preview, ref_price="100")
    move_id = insert_exec_intent(
        conn,
        kind="move_stop",
        symbol="BTCUSDT",
        side="long",
        preview={"stop": "95.5", "qty": "0.1"},
        created_at_ms=SPEC_START_MS + 2000,
        expires_at_ms=SPEC_START_MS + 3000,
    )
    insert_exec_log(
        conn,
        intent_id=move_id,
        step="move_stop_place",
        endpoint="/fapi/v1/algoOrder",
        client_order_id="jv-ms0001-ms",
        http_status=200,
        created_at_ms=SPEC_START_MS + 2500,
    )
    conn.commit()
    t0 = SPEC_START_MS + 60_000
    orders = [
        {
            "orderId": 31,
            "clientOrderId": cid,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "time": t0,
            "status": "FILLED",
            "reduceOnly": False,
        },
        {
            "orderId": 32,
            "clientOrderId": "jv-ms0001-ms",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "type": "MARKET",
            "origType": "MARKET",
            "stopPrice": "",
            "time": t0 + 2000,
            "status": "FILLED",
            "reduceOnly": True,
        },
    ]
    trades = [
        {
            "id": 1,
            "orderId": 31,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "price": "100",
            "qty": "0.1",
            "time": t0,
            "commission": "0",
            "commissionAsset": "USDT",
            "realizedPnl": "0",
        },
        {
            "id": 2,
            "orderId": 32,
            "symbol": "BTCUSDT",
            "side": "SELL",
            "price": "95.5",
            "qty": "0.1",
            "time": t0 + 2000,
            "commission": "0",
            "commissionAsset": "USDT",
            "realizedPnl": "0.4",
        },
    ]
    income = [
        {
            "symbol": "BTCUSDT",
            "incomeType": "REALIZED_PNL",
            "income": "0.4",
            "time": t0 + 2000,
            "tradeId": 2,
        },
        {
            "symbol": "BTCUSDT",
            "incomeType": "FUNDING_FEE",
            "income": "0",
            "time": t0 + 1000,
        },
    ]
    fake = FakeFutures(trades=trades, orders=orders, income=income)
    sync_real_trades(conn, fake, since_ms=SPEC_START_MS, until_ms=t0 + 5000)  # type: ignore[arg-type]
    row = conn.execute(
        "SELECT stop_trigger, exit_order_ids FROM assist_real_trade WHERE entry_order_id='31'"
    ).fetchone()
    assert row is not None and row[0] == pytest.approx(95.5)
    meta = json.loads(str(row[1]))
    assert meta["stop_detail"] == "moved"
    assert meta["stop_source"] == "move_stop_preview"
    conn.close()


def test_unresolved_stop_trigger(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "miss.db")
    sid = "as_miss"
    intent = "aei_miss01"
    cid = "jv-miss01-mkt"
    preview = {
        "entry": "100",
        "stop": "90",
        "target": "120",
        "qty": "0.1",
        "loss_at_stop": "1",
        "signal_id": sid,
        "leverage": 5,
    }
    _seed_jv_entry(conn, intent_id=intent, client_id=cid, preview=preview, ref_price="100")
    t0 = SPEC_START_MS + 60_000
    # ms exit without move_stop_place log → trigger NULL
    orders = [
        {
            "orderId": 41,
            "clientOrderId": cid,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "time": t0,
            "status": "FILLED",
            "reduceOnly": False,
        },
        {
            "orderId": 42,
            "clientOrderId": "jv-miss01-ms",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "type": "MARKET",
            "stopPrice": "",
            "time": t0 + 1000,
            "status": "FILLED",
            "reduceOnly": True,
        },
    ]
    trades = [
        {
            "id": 1,
            "orderId": 41,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "price": "100",
            "qty": "0.1",
            "time": t0,
            "commission": "0",
            "commissionAsset": "USDT",
            "realizedPnl": "0",
        },
        {
            "id": 2,
            "orderId": 42,
            "symbol": "BTCUSDT",
            "side": "SELL",
            "price": "95",
            "qty": "0.1",
            "time": t0 + 1000,
            "commission": "0",
            "commissionAsset": "USDT",
            "realizedPnl": "0.5",
        },
    ]
    income = [
        {
            "symbol": "BTCUSDT",
            "incomeType": "REALIZED_PNL",
            "income": "0.5",
            "time": t0 + 1000,
            "tradeId": 2,
        },
        {
            "symbol": "BTCUSDT",
            "incomeType": "FUNDING_FEE",
            "income": "0",
            "time": t0 + 500,
        },
    ]
    fake = FakeFutures(trades=trades, orders=orders, income=income)
    sync_real_trades(conn, fake, since_ms=SPEC_START_MS, until_ms=t0 + 5000)  # type: ignore[arg-type]
    row = conn.execute(
        "SELECT stop_trigger, slip_stop_bps FROM assist_real_trade WHERE entry_order_id='41'"
    ).fetchone()
    assert row is not None and row[0] is None and row[1] is None
    s = summarize_real_vs_virtual(conn)
    assert s["stop_trigger_missing_n"] == 1
    conn.close()


def test_short_stop_slip_bps(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "short.db")
    sid = "as_short"
    intent = "aei_sh001"
    cid = "jv-sh001-mkt"
    preview = {
        "entry": "100",
        "stop": "100",
        "target": "80",
        "qty": "0.1",
        "loss_at_stop": "1",
        "signal_id": sid,
        "leverage": 5,
    }
    _seed_jv_entry(conn, intent_id=intent, client_id=cid, preview=preview, ref_price="100", side="short")
    t0 = SPEC_START_MS + 60_000
    orders = [
        {
            "orderId": 51,
            "clientOrderId": cid,
            "symbol": "BTCUSDT",
            "side": "SELL",
            "type": "MARKET",
            "time": t0,
            "status": "FILLED",
            "reduceOnly": False,
        },
        {
            "orderId": 52,
            "clientOrderId": "jv-sh001-sl0",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "stopPrice": "",
            "time": t0 + 1000,
            "status": "FILLED",
            "reduceOnly": True,
        },
    ]
    trades = [
        {
            "id": 1,
            "orderId": 51,
            "symbol": "BTCUSDT",
            "side": "SELL",
            "price": "100",
            "qty": "0.1",
            "time": t0,
            "commission": "0",
            "commissionAsset": "USDT",
            "realizedPnl": "0",
        },
        {
            "id": 2,
            "orderId": 52,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "price": "100.5",
            "qty": "0.1",
            "time": t0 + 1000,
            "commission": "0",
            "commissionAsset": "USDT",
            "realizedPnl": "-0.05",
        },
    ]
    income = [
        {
            "symbol": "BTCUSDT",
            "incomeType": "REALIZED_PNL",
            "income": "-0.05",
            "time": t0 + 1000,
            "tradeId": 2,
        },
        {
            "symbol": "BTCUSDT",
            "incomeType": "FUNDING_FEE",
            "income": "0",
            "time": t0 + 500,
        },
    ]
    fake = FakeFutures(trades=trades, orders=orders, income=income, tick_size=Decimal("0.1"))
    sync_real_trades(conn, fake, since_ms=SPEC_START_MS, until_ms=t0 + 5000)  # type: ignore[arg-type]
    slip = conn.execute("SELECT slip_stop_bps FROM assist_real_trade WHERE entry_order_id='51'").fetchone()[0]
    assert slip == pytest.approx(50.0)
    conn.close()


def test_short_entry_not_exit_of_prior_long(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "flip.db")
    long_preview = {
        "entry": "100",
        "stop": "90",
        "target": "120",
        "qty": "0.1",
        "loss_at_stop": "1",
        "signal_id": "as_flip_l",
        "leverage": 5,
    }
    short_preview = {
        "entry": "95",
        "stop": "100",
        "target": "80",
        "qty": "0.1",
        "loss_at_stop": "1",
        "signal_id": "as_flip_s",
        "leverage": 5,
    }
    _seed_jv_entry(
        conn,
        intent_id="aei_flip_l",
        client_id="jv-flipl-mkt",
        preview=long_preview,
        ref_price="100",
    )
    _seed_jv_entry(
        conn,
        intent_id="aei_flip_s",
        client_id="jv-flips-mkt",
        preview=short_preview,
        ref_price="95",
        side="short",
    )
    t0 = SPEC_START_MS + 60_000
    orders = [
        {
            "orderId": 61,
            "clientOrderId": "jv-flipl-mkt",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "time": t0,
            "status": "FILLED",
            "reduceOnly": False,
        },
        {
            "orderId": 62,
            "clientOrderId": "manual-close",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "type": "MARKET",
            "time": t0 + 500,
            "status": "FILLED",
            "reduceOnly": True,
        },
        {
            "orderId": 63,
            "clientOrderId": "jv-flips-mkt",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "type": "MARKET",
            "time": t0 + 1000,
            "status": "FILLED",
            "reduceOnly": False,
        },
    ]
    trades = [
        {
            "id": 1,
            "orderId": 61,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "price": "100",
            "qty": "0.1",
            "time": t0,
            "commission": "0",
            "commissionAsset": "USDT",
            "realizedPnl": "0",
        },
        {
            "id": 2,
            "orderId": 62,
            "symbol": "BTCUSDT",
            "side": "SELL",
            "price": "99",
            "qty": "0.1",
            "time": t0 + 500,
            "commission": "0",
            "commissionAsset": "USDT",
            "realizedPnl": "-0.1",
        },
        {
            "id": 3,
            "orderId": 63,
            "symbol": "BTCUSDT",
            "side": "SELL",
            "price": "95",
            "qty": "0.1",
            "time": t0 + 1000,
            "commission": "0",
            "commissionAsset": "USDT",
            "realizedPnl": "0",
        },
    ]
    income = [
        {
            "symbol": "BTCUSDT",
            "incomeType": "REALIZED_PNL",
            "income": "-0.1",
            "time": t0 + 500,
            "tradeId": 2,
        },
        {
            "symbol": "BTCUSDT",
            "incomeType": "FUNDING_FEE",
            "income": "0",
            "time": t0 + 200,
        },
    ]
    fake = FakeFutures(trades=trades, orders=orders, income=income)
    sync_real_trades(conn, fake, since_ms=SPEC_START_MS, until_ms=t0 + 5000)  # type: ignore[arg-type]
    long_row = conn.execute(
        "SELECT status, exit_kind FROM assist_real_trade WHERE entry_order_id='61'"
    ).fetchone()
    short_row = conn.execute("SELECT status FROM assist_real_trade WHERE entry_order_id='63'").fetchone()
    assert long_row is not None and long_row[0] == "closed"
    assert short_row is not None and short_row[0] == "open"
    conn.close()


def test_two_longs_exit_fills_used_once(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "two.db")
    for _i, intent, cid, sid in (
        (1, "aei_two1", "jv-two1-mkt", "as_two1"),
        (2, "aei_two2", "jv-two2-mkt", "as_two2"),
    ):
        _seed_jv_entry(
            conn,
            intent_id=intent,
            client_id=cid,
            preview={
                "entry": "100",
                "stop": "90",
                "target": "120",
                "qty": "0.1",
                "loss_at_stop": "1",
                "signal_id": sid,
                "leverage": 5,
            },
            ref_price="100",
        )
    t0 = SPEC_START_MS + 60_000
    orders = [
        {
            "orderId": 71,
            "clientOrderId": "jv-two1-mkt",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "time": t0,
            "status": "FILLED",
            "reduceOnly": False,
        },
        {
            "orderId": 72,
            "clientOrderId": "jv-two2-mkt",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "time": t0 + 100,
            "status": "FILLED",
            "reduceOnly": False,
        },
        {
            "orderId": 73,
            "clientOrderId": "exit-a",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "type": "MARKET",
            "time": t0 + 200,
            "status": "FILLED",
            "reduceOnly": True,
        },
        {
            "orderId": 74,
            "clientOrderId": "exit-b",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "type": "MARKET",
            "time": t0 + 300,
            "status": "FILLED",
            "reduceOnly": True,
        },
    ]
    trades = [
        {
            "id": 1,
            "orderId": 71,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "price": "100",
            "qty": "0.1",
            "time": t0,
            "commission": "0",
            "commissionAsset": "USDT",
            "realizedPnl": "0",
        },
        {
            "id": 2,
            "orderId": 72,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "price": "100",
            "qty": "0.1",
            "time": t0 + 100,
            "commission": "0",
            "commissionAsset": "USDT",
            "realizedPnl": "0",
        },
        {
            "id": 3,
            "orderId": 73,
            "symbol": "BTCUSDT",
            "side": "SELL",
            "price": "101",
            "qty": "0.1",
            "time": t0 + 200,
            "commission": "0",
            "commissionAsset": "USDT",
            "realizedPnl": "0.1",
        },
        {
            "id": 4,
            "orderId": 74,
            "symbol": "BTCUSDT",
            "side": "SELL",
            "price": "102",
            "qty": "0.1",
            "time": t0 + 300,
            "commission": "0",
            "commissionAsset": "USDT",
            "realizedPnl": "0.2",
        },
    ]
    income = [
        {
            "symbol": "BTCUSDT",
            "incomeType": "REALIZED_PNL",
            "income": "0.1",
            "time": t0 + 200,
            "tradeId": 3,
        },
        {
            "symbol": "BTCUSDT",
            "incomeType": "REALIZED_PNL",
            "income": "0.2",
            "time": t0 + 300,
            "tradeId": 4,
        },
        {
            "symbol": "BTCUSDT",
            "incomeType": "FUNDING_FEE",
            "income": "0",
            "time": t0 + 150,
        },
    ]
    fake = FakeFutures(trades=trades, orders=orders, income=income)
    sync_real_trades(conn, fake, since_ms=SPEC_START_MS, until_ms=t0 + 5000)  # type: ignore[arg-type]
    r1 = conn.execute(
        "SELECT exit_fill_avg, status FROM assist_real_trade WHERE entry_order_id='71'"
    ).fetchone()
    r2 = conn.execute(
        "SELECT exit_fill_avg, status FROM assist_real_trade WHERE entry_order_id='72'"
    ).fetchone()
    assert r1 is not None and r1[1] == "closed" and r1[0] == pytest.approx(101.0)
    assert r2 is not None and r2[1] == "closed" and r2[0] == pytest.approx(102.0)
    conn.close()


def test_partial_take_splits_fee(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "part.db")
    _seed_jv_entry(
        conn,
        intent_id="aei_part1",
        client_id="jv-part1-mkt",
        preview={
            "entry": "100",
            "stop": "90",
            "target": "120",
            "qty": "0.1",
            "loss_at_stop": "1",
            "signal_id": "as_part1",
            "leverage": 5,
        },
        ref_price="100",
    )
    _seed_jv_entry(
        conn,
        intent_id="aei_part2",
        client_id="jv-part2-mkt",
        preview={
            "entry": "100",
            "stop": "90",
            "target": "120",
            "qty": "0.1",
            "loss_at_stop": "1",
            "signal_id": "as_part2",
            "leverage": 5,
        },
        ref_price="100",
    )
    t0 = SPEC_START_MS + 60_000
    orders = [
        {
            "orderId": 81,
            "clientOrderId": "jv-part1-mkt",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "time": t0,
            "status": "FILLED",
            "reduceOnly": False,
        },
        {
            "orderId": 82,
            "clientOrderId": "jv-part2-mkt",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "time": t0 + 10,
            "status": "FILLED",
            "reduceOnly": False,
        },
        {
            "orderId": 83,
            "clientOrderId": "big-exit",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "type": "MARKET",
            "time": t0 + 100,
            "status": "FILLED",
            "reduceOnly": True,
        },
    ]
    trades = [
        {
            "id": 1,
            "orderId": 81,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "price": "100",
            "qty": "0.1",
            "time": t0,
            "commission": "0.02",
            "commissionAsset": "USDT",
            "realizedPnl": "0",
        },
        {
            "id": 2,
            "orderId": 82,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "price": "100",
            "qty": "0.1",
            "time": t0 + 10,
            "commission": "0.02",
            "commissionAsset": "USDT",
            "realizedPnl": "0",
        },
        {
            "id": 3,
            "orderId": 83,
            "symbol": "BTCUSDT",
            "side": "SELL",
            "price": "101",
            "qty": "0.2",
            "time": t0 + 100,
            "commission": "0.04",
            "commissionAsset": "USDT",
            "realizedPnl": "0.2",
        },
    ]
    income = [
        {
            "symbol": "BTCUSDT",
            "incomeType": "REALIZED_PNL",
            "income": "0.2",
            "time": t0 + 100,
            "tradeId": 3,
        },
        {
            "symbol": "BTCUSDT",
            "incomeType": "FUNDING_FEE",
            "income": "0",
            "time": t0 + 50,
        },
    ]
    fake = FakeFutures(trades=trades, orders=orders, income=income)
    sync_real_trades(conn, fake, since_ms=SPEC_START_MS, until_ms=t0 + 5000)  # type: ignore[arg-type]
    # First long takes 0.1 of 0.2 → half exit fee 0.02; entry fee 0.02 → total -0.04
    fee1 = conn.execute(
        "SELECT commission_usdt, status FROM assist_real_trade WHERE entry_order_id='81'"
    ).fetchone()
    assert fee1 is not None and fee1[1] == "closed"
    assert fee1[0] == pytest.approx(-0.04)
    conn.close()


def test_reduce_only_breakeven_exit(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "be.db")
    _seed_jv_entry(
        conn,
        intent_id="aei_be001",
        client_id="jv-be001-mkt",
        preview={
            "entry": "100",
            "stop": "90",
            "target": "120",
            "qty": "0.1",
            "loss_at_stop": "1",
            "signal_id": "as_be",
            "leverage": 5,
        },
        ref_price="100",
    )
    t0 = SPEC_START_MS + 60_000
    orders = [
        {
            "orderId": 91,
            "clientOrderId": "jv-be001-mkt",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "time": t0,
            "status": "FILLED",
            "reduceOnly": False,
        },
        {
            "orderId": 92,
            "clientOrderId": "jv-be001-sl0",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "type": "MARKET",
            "stopPrice": "",
            "time": t0 + 1000,
            "status": "FILLED",
            "reduceOnly": True,
        },
    ]
    trades = [
        {
            "id": 1,
            "orderId": 91,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "price": "100",
            "qty": "0.1",
            "time": t0,
            "commission": "0",
            "commissionAsset": "USDT",
            "realizedPnl": "0",
        },
        {
            "id": 2,
            "orderId": 92,
            "symbol": "BTCUSDT",
            "side": "SELL",
            "price": "100",
            "qty": "0.1",
            "time": t0 + 1000,
            "commission": "0",
            "commissionAsset": "USDT",
            "realizedPnl": "0",
        },
    ]
    income = [
        {
            "symbol": "BTCUSDT",
            "incomeType": "REALIZED_PNL",
            "income": "0",
            "time": t0 + 1000,
            "tradeId": 2,
        },
        {
            "symbol": "BTCUSDT",
            "incomeType": "FUNDING_FEE",
            "income": "0",
            "time": t0 + 500,
        },
    ]
    fake = FakeFutures(trades=trades, orders=orders, income=income)
    sync_real_trades(conn, fake, since_ms=SPEC_START_MS, until_ms=t0 + 5000)  # type: ignore[arg-type]
    status = conn.execute("SELECT status FROM assist_real_trade WHERE entry_order_id='91'").fetchone()[0]
    assert status == "closed"
    conn.close()


def test_no_ref_leaves_null_slip(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "noref.db")
    preview = {
        "stop": "90",
        "target": "120",
        "qty": "0.1",
        "loss_at_stop": "1",
        "signal_id": "as_noref",
        "leverage": 5,
    }
    # No entry in preview → no signal_entry; no ref_price.
    conn.execute(
        """
        INSERT INTO assist_exec_intent (
            id, kind, symbol, side, preview_json, created_at_ms, expires_at_ms, status
        ) VALUES ('aei_noref', 'entry', 'BTCUSDT', 'long', ?, 1, 2, 'done')
        """,
        (json.dumps(preview),),
    )
    insert_exec_log(
        conn,
        intent_id="aei_noref",
        step="market",
        endpoint="/fapi/v1/order",
        client_order_id="jv-noref-mkt",
        http_status=200,
        created_at_ms=SPEC_START_MS + 1000,
    )
    conn.execute(
        """
        INSERT INTO assist_signal (
            id, symbol, side, entry, stop, target, qty, notional, leverage, loss_at_stop,
            created_at_ms, expires_at_ms, status
        ) VALUES ('as_noref', 'BTCUSDT', 'long', 100, 90, 120, 0.1, 10, 5, 1, ?, ?, 'taken')
        """,
        (SPEC_START_MS, SPEC_START_MS + 1),
    )
    conn.execute(
        """
        INSERT INTO assist_track (
            signal_id, symbol, side, entry, stop, target, qty, sent_at_ms,
            exit_at_ms, exit_px, exit_reason, gross_r, net_r, fee_usdt, pnl_usdt,
            cohort, kind, funding_usdt
        ) VALUES ('as_noref', 'BTCUSDT', 'long', 100, 90, 120, 0.1, ?, ?, 110, 'flat',
                  1.0, 0.8, 0.1, 1.6, ?, 'sent', 0)
        """,
        (SPEC_START_MS, SPEC_START_MS + 8 * 3_600_000, TRACK_COHORT),
    )
    conn.commit()
    t0 = SPEC_START_MS + 60_000
    orders = [
        {
            "orderId": 101,
            "clientOrderId": "jv-noref-mkt",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "time": t0,
            "status": "FILLED",
            "reduceOnly": False,
        }
    ]
    trades = [
        {
            "id": 1,
            "orderId": 101,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "price": "100",
            "qty": "0.1",
            "time": t0,
            "commission": "0",
            "commissionAsset": "USDT",
            "realizedPnl": "0",
        }
    ]
    fake = FakeFutures(trades=trades, orders=orders)
    sync_real_trades(conn, fake, since_ms=SPEC_START_MS, until_ms=t0 + 1000)  # type: ignore[arg-type]
    row = conn.execute(
        "SELECT ref_price, ref_source, slip_entry_bps, status FROM assist_real_trade WHERE entry_order_id='101'"
    ).fetchone()
    assert row is not None
    assert row[0] is None and row[1] is None and row[2] is None and row[3] == "open"
    conn.close()


def test_unmatched_owner_side_buy_sell(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "own.db")
    t0 = SPEC_START_MS + 60_000
    orders = [
        {
            "orderId": 201,
            "clientOrderId": "human-1",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "time": t0,
            "status": "FILLED",
            "reduceOnly": False,
        }
    ]
    trades = [
        {
            "id": 1,
            "orderId": 201,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "price": "100",
            "qty": "0.1",
            "time": t0,
            "commission": "0",
            "commissionAsset": "USDT",
            "realizedPnl": "0",
        }
    ]
    fake = FakeFutures(trades=trades, orders=orders)
    sync_real_trades(conn, fake, since_ms=SPEC_START_MS, until_ms=t0 + 1000)  # type: ignore[arg-type]
    row = conn.execute(
        "SELECT status, side, ref_source FROM assist_real_trade WHERE entry_order_id='201'"
    ).fetchone()
    assert row is not None
    assert row[0] == "unmatched" and row[1] == "buy" and row[2] == "owner"
    conn.close()


def test_api_error_leaves_existing_rows(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "api.db")
    _seed_jv_entry(
        conn,
        intent_id="aei_api1",
        client_id="jv-api1-mkt",
        preview={
            "entry": "100",
            "stop": "90",
            "target": "120",
            "qty": "0.1",
            "loss_at_stop": "1",
            "signal_id": "as_api",
            "leverage": 5,
        },
        ref_price="100",
    )
    t0 = SPEC_START_MS + 60_000
    orders = [
        {
            "orderId": 301,
            "clientOrderId": "jv-api1-mkt",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "time": t0,
            "status": "FILLED",
            "reduceOnly": False,
        }
    ]
    trades = [
        {
            "id": 1,
            "orderId": 301,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "price": "100",
            "qty": "0.1",
            "time": t0,
            "commission": "0",
            "commissionAsset": "USDT",
            "realizedPnl": "0",
        }
    ]
    fake = FakeFutures(trades=trades, orders=orders)
    sync_real_trades(conn, fake, since_ms=SPEC_START_MS, until_ms=t0 + 1000)  # type: ignore[arg-type]
    before = conn.execute("SELECT COUNT(*), status FROM assist_real_trade").fetchone()
    assert before is not None and before[0] >= 1
    # Mutate a marker value then fail mid-sync.
    conn.execute("UPDATE assist_real_trade SET net_r_real=42 WHERE entry_order_id='301'")
    conn.commit()
    boom = FakeFutures(trades=trades, orders=orders, fail_after_calls=1)
    with pytest.raises(RuntimeError, match="api_mid_sync"):
        sync_real_trades(conn, boom, since_ms=SPEC_START_MS, until_ms=t0 + 1000)  # type: ignore[arg-type]
    after = conn.execute(
        "SELECT net_r_real, COUNT(*) FROM assist_real_trade WHERE entry_order_id='301'"
    ).fetchone()
    assert after is not None and after[0] == 42 and after[1] == 1
    conn.close()


def test_second_sync_no_duplicate_skips_income_refetch(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "idem.db")
    _seed_jv_entry(
        conn,
        intent_id="aei_idem",
        client_id="jv-idem-mkt",
        preview={
            "entry": "100",
            "stop": "90",
            "target": "120",
            "qty": "0.1",
            "loss_at_stop": "1",
            "signal_id": "as_idem",
            "leverage": 5,
        },
        ref_price="100",
    )
    t0 = SPEC_START_MS + 60_000
    orders = [
        {
            "orderId": 401,
            "clientOrderId": "jv-idem-mkt",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "time": t0,
            "status": "FILLED",
            "reduceOnly": False,
        },
        {
            "orderId": 402,
            "clientOrderId": "jv-idem-sl0",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "type": "MARKET",
            "stopPrice": "",
            "time": t0 + 1000,
            "status": "FILLED",
            "reduceOnly": True,
        },
    ]
    trades = [
        {
            "id": 1,
            "orderId": 401,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "price": "100",
            "qty": "0.1",
            "time": t0,
            "commission": "0",
            "commissionAsset": "USDT",
            "realizedPnl": "0",
        },
        {
            "id": 2,
            "orderId": 402,
            "symbol": "BTCUSDT",
            "side": "SELL",
            "price": "90",
            "qty": "0.1",
            "time": t0 + 1000,
            "commission": "0",
            "commissionAsset": "USDT",
            "realizedPnl": "-1",
        },
    ]
    income = [
        {
            "symbol": "BTCUSDT",
            "incomeType": "REALIZED_PNL",
            "income": "-1",
            "time": t0 + 1000,
            "tradeId": 2,
        },
        {
            "symbol": "BTCUSDT",
            "incomeType": "FUNDING_FEE",
            "income": "-0.01",
            "time": t0 + 500,
        },
    ]
    fake = FakeFutures(trades=trades, orders=orders, income=income)
    sync_real_trades(conn, fake, since_ms=SPEC_START_MS, until_ms=t0 + 5000)  # type: ignore[arg-type]
    n1 = conn.execute("SELECT COUNT(*) FROM assist_real_trade").fetchone()[0]
    income_n1 = len(fake.income_calls)
    sync_real_trades(conn, fake, since_ms=SPEC_START_MS, until_ms=t0 + 5000)  # type: ignore[arg-type]
    n2 = conn.execute("SELECT COUNT(*) FROM assist_real_trade").fetchone()[0]
    assert n1 == n2 == 1
    # Second sync should not add income calls for the cached closed row.
    assert len(fake.income_calls) == income_n1
    conn.close()


def test_non_usdt_excluded_from_mean_r(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "bnb.db")
    _seed_jv_entry(
        conn,
        intent_id="aei_bnb",
        client_id="jv-bnb-mkt",
        preview={
            "entry": "100",
            "stop": "90",
            "target": "120",
            "qty": "0.1",
            "loss_at_stop": "1",
            "signal_id": "as_bnb",
            "leverage": 5,
        },
        ref_price="100",
    )
    t0 = SPEC_START_MS + 60_000
    orders = [
        {
            "orderId": 501,
            "clientOrderId": "jv-bnb-mkt",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "time": t0,
            "status": "FILLED",
            "reduceOnly": False,
        },
        {
            "orderId": 502,
            "clientOrderId": "jv-bnb-sl0",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "type": "MARKET",
            "stopPrice": "",
            "time": t0 + 1000,
            "status": "FILLED",
            "reduceOnly": True,
        },
    ]
    trades = [
        {
            "id": 1,
            "orderId": 501,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "price": "100",
            "qty": "0.1",
            "time": t0,
            "commission": "0.001",
            "commissionAsset": "BNB",
            "realizedPnl": "0",
        },
        {
            "id": 2,
            "orderId": 502,
            "symbol": "BTCUSDT",
            "side": "SELL",
            "price": "90",
            "qty": "0.1",
            "time": t0 + 1000,
            "commission": "0",
            "commissionAsset": "USDT",
            "realizedPnl": "-1",
        },
    ]
    income = [
        {
            "symbol": "BTCUSDT",
            "incomeType": "REALIZED_PNL",
            "income": "-1",
            "time": t0 + 1000,
            "tradeId": 2,
        },
        {
            "symbol": "BTCUSDT",
            "incomeType": "FUNDING_FEE",
            "income": "0",
            "time": t0 + 500,
        },
    ]
    fake = FakeFutures(trades=trades, orders=orders, income=income)
    sync_real_trades(conn, fake, since_ms=SPEC_START_MS, until_ms=t0 + 5000)  # type: ignore[arg-type]
    s = summarize_real_vs_virtual(conn)
    assert s["closed_n"] == 1
    assert s["non_usdt_fee_n"] == 1
    assert s["mean_net_r"] is None
    conn.close()


def test_weekly_lines_include_open_and_split() -> None:
    summary = {
        "closed_n": 0,
        "mean_net_r": None,
        "mean_diff_r": None,
        "mean_slip_entry_bps": None,
        "p90_slip_entry_bps": None,
        "entry_slip_n": 0,
        "confirm_n": 0,
        "preview_n": 0,
        "mean_drift_bps": None,
        "mean_slip_stop_bps": None,
        "p90_slip_stop_bps": None,
        "stop_n": 0,
        "unmatched_n": 0,
        "unmatched_jv_before_start": 0,
        "unmatched_jv_no_log": 0,
        "unmatched_owner": 0,
        "unknown_n": 0,
        "open_n": 0,
        "unlinked_n": 0,
        "non_usdt_fee_n": 0,
        "stop_trigger_missing_n": 0,
        "target_n": 20,
    }
    lines = format_real_lines(summary)
    text = format_weekly_summary(
        counts={"sent": 0, "skipped": 0, "taken": 0},
        virtual={"n": 0},
        real={"universe_usdt": 0.0},
        closed_all=0,
        held={"n": 0, "mean_r": 0.0},
        real_vs_virtual=summary,
    )
    assert "ไม้จริง n=0 (ต้องการ 20) · net R เฉลี่ย — · ต่างจากไม้จำลอง —" in text
    assert "ไม้ที่ยังถือ 0" in text
    assert "จับคู่ไม่ได้: 0 ไม้" in text
    assert any("ไม่มี track: 0" in line for line in lines)


def test_p90_marks_small_n() -> None:
    summary = {
        "closed_n": 3,
        "mean_net_r": 0.1,
        "mean_diff_r": 0.0,
        "mean_slip_entry_bps": 1.0,
        "p90_slip_entry_bps": 2.0,
        "entry_slip_n": 3,
        "confirm_n": 3,
        "preview_n": 0,
        "mean_drift_bps": 1.0,
        "mean_slip_stop_bps": 4.0,
        "p90_slip_stop_bps": 5.0,
        "stop_n": 3,
        "unmatched_n": 0,
        "unmatched_jv_before_start": 0,
        "unmatched_jv_no_log": 0,
        "unmatched_owner": 0,
        "unknown_n": 0,
        "open_n": 0,
        "unlinked_n": 0,
        "non_usdt_fee_n": 0,
        "stop_trigger_missing_n": 0,
        "target_n": 20,
    }
    lines = format_real_lines(summary)
    assert "p90 2.0 (n<10)" in lines[1]
    assert "p90 5.0 (n<10)" in lines[2]


def test_default_cli_path_leaves_db_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "ro.db"
    conn = open_db(db_path)
    conn.close()
    before = db_path.read_bytes()

    class _Client:
        def user_trades(self, *_a: Any, **_k: Any) -> list[dict[str, Any]]:
            return []

        def all_orders(self, *_a: Any, **_k: Any) -> list[dict[str, Any]]:
            return []

        def income_history(self, *_a: Any, **_k: Any) -> list[dict[str, Any]]:
            return []

        def symbol_filters(self, symbol: str) -> SymbolFilters:
            return SymbolFilters(
                step_size=Decimal("1"),
                min_qty=Decimal("0"),
                min_notional=Decimal("0"),
                tick_size=Decimal("0.1"),
            )

    from jarvise_ingest.universe_cli import connect_readonly

    ro = connect_readonly(db_path)
    try:
        uv = int(ro.execute("PRAGMA user_version").fetchone()[0] or 0)
        assert uv == SCHEMA_VERSION
        payload = run_real_report(ro, _Client(), since_ms=SPEC_START_MS, sync=False)  # type: ignore[arg-type]
        assert payload["ok"] is True
        assert payload["sync"] is None
    finally:
        ro.close()
    assert db_path.read_bytes() == before


def test_confirm_guards_stores_ref_and_write_failure_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_assist_execute import RecordingTransport, _client, _exchange_info, _seed_doge_short

    from jarvise_assist.execute import _confirm_guards_entry
    from jarvise_assist.sizing import size_signal
    from jarvise_assist.store import insert_signal

    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    conn = open_db(tmp_path / "ref.db")
    bar = _ms_bangkok(2026, 11, 2, 3, 0)
    now = _ms_bangkok(2026, 11, 2, 8, 20)
    _seed_doge_short(conn, ts=bar)
    first = size_signal(
        symbol="DOGEUSDT",
        side="short",
        entry=Decimal("0.08"),
        stop=Decimal("0.083"),
        balance=Decimal("1000"),
        risk_pct=Decimal("1"),
        step_size=Decimal("1"),
    )
    assert first is not None
    insert_signal(
        conn,
        first,
        status="sent",
        created_at_ms=now - 60_000,
        expires_at_ms=now + 3_600_000,
        sent_at_ms=now - 60_000,
    )
    intent_id = insert_exec_intent(
        conn,
        kind="entry",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "qty": "100",
            "stop": "0.083",
            "target": "0.074",
            "leverage": 1,
            "entry": "0.08",
            "loss_at_stop": "0.3",
            "r_distance": "0.003",
        },
        created_at_ms=now,
        expires_at_ms=now + 120_000,
    )
    from jarvise_assist.store import get_exec_intent

    intent_row = get_exec_intent(conn, intent_id)
    assert intent_row is not None
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v3/balance"): (
            200,
            [{"asset": "USDT", "balance": "1000", "availableBalance": "1000"}],
        ),
        ("GET", "/fapi/v3/positionRisk"): (200, []),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.08"}),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
    }
    trade = _client(RecordingTransport(handlers))
    assert _confirm_guards_entry(conn, intent_row, trade, now_ms_value=now + 1000) is None
    stored = get_exec_intent(conn, intent_id)
    assert stored is not None
    assert stored["ref_price"] == "0.08"
    assert stored["ref_source"] == "confirm_mark"
    assert stored["ref_at_ms"] == now + 1000

    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("disk")

    monkeypatch.setattr("jarvise_assist.execute.set_exec_intent_ref", boom)
    assert _confirm_guards_entry(conn, intent_row, trade, now_ms_value=now + 2000) is None
    conn.close()


def test_real_trades_module_has_no_trade_client() -> None:
    src = Path("src/jarvise_assist/real_trades.py").read_text(encoding="utf-8")
    assert "BinanceTradeClient" not in src


def test_fetch_all_orders_pages() -> None:
    start = SPEC_START_MS
    end = start + 3_600_000
    orders = [
        {
            "orderId": i + 1,
            "clientOrderId": f"jv-x-{i}",
            "symbol": "ETHUSDT",
            "side": "BUY",
            "type": "MARKET",
            "origType": "MARKET",
            "time": start + i,
            "status": "FILLED",
        }
        for i in range(1005)
    ]
    fake = FakeFutures(orders=orders)
    rows = fetch_all_orders(fake, "ETHUSDT", start_ms=start, end_ms=end)  # type: ignore[arg-type]
    assert len(rows) == 1005
    assert fake.all_order_calls[0]["order_id"] is None
    assert fake.all_order_calls[1]["order_id"] == 1001
    assert fake.all_order_calls[1]["start_ms"] is None


def test_summarize_reads_empty_db(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "empty.db")
    s = summarize_real_vs_virtual(conn)
    assert s["closed_n"] == 0
    assert s["unmatched_n"] == 0
    assert s["unknown_n"] == 0
    assert s["open_n"] == 0
    conn.close()
