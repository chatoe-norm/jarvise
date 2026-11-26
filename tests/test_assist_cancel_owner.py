"""M1a-2 — cancel leftover owner reduce-only orders after a verified flat."""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.test_assist import FakeFutures
from tests.test_assist_execute import (
    RecordingTransport,
    _client,
    _open_pos,
    _verified_stop,
)
from tests.test_assist_menu_close import _insert_quick_reg, _menu_close_handlers
from tests.test_assist_menu_reduce import _filters

from jarvise_assist.execute import (
    OWNER_CLOSING_HEADER,
    OWNER_CLOSING_HEADER_WILL_CANCEL,
    OWNER_CLOSING_ORDER_NOTE,
    OWNER_CLOSING_ORDER_NOTE_WILL_CANCEL,
    PREVIEW_TTL_MS,
    RECOVER_MAX_AGE_MS,
    BinanceTradeClient,
    _append_exit_retained_leftovers,
    _finish_flat_exit,
    _format_menu_exit_confirm,
    _owner_order_create_ms,
    assist_cancel_owner_enabled,
    cancel_jv_orders_for_symbol,
    cancel_owner_reducing_orders_for_symbol,
    handle_exec_callback,
    run_exec_recovery,
)
from jarvise_assist.futures import FuturesPosition
from jarvise_assist.messages import _fmt_price_tick, fmt_price
from jarvise_assist.store import (
    finish_exec_intent,
    get_exec_intent,
    insert_exec_intent,
    insert_exec_log,
    now_ms,
    symbol_has_in_flight_exec,
    upsert_watch_position,
    write_settings,
)
from jarvise_assist.watch import run_assist_watch
from jarvise_ingest.db import SCHEMA_VERSION, open_db


def test_schema_stays_38() -> None:
    assert SCHEMA_VERSION == 39


def test_assist_cancel_owner_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "ON")
    assert assist_cancel_owner_enabled() is True
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "off")
    assert assist_cancel_owner_enabled() is False
    monkeypatch.delenv("JARVISE_ASSIST_CANCEL_OWNER", raising=False)
    assert assist_cancel_owner_enabled() is False


def test_flag_on_cancels_owner_algo_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")
    monkeypatch.setattr(BinanceTradeClient, "assert_trade_key_safe", lambda self, **_: {"ok": True})
    monkeypatch.setattr(BinanceTradeClient, "assert_account_modes", lambda self: None)
    conn = open_db(tmp_path / "xx-owner-algo-on.db")
    _insert_quick_reg(
        conn, reg_id="q-algo-on", symbol="DOGEUSDT", side="short", planned_stop=2.0, planned_entry=1.0
    )
    ts = now_ms()
    intent_id = insert_exec_intent(
        conn,
        kind="exit",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "source": "menu",
            "cohort": "quick",
            "qty": "100",
            "entry": "0.08",
            "position_side": "BOTH",
            "confirm_text": "c",
        },
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    owner_algo = {
        "symbol": "DOGEUSDT",
        "algoId": 55,
        "type": "STOP_MARKET",
        "orderType": "STOP_MARKET",
        "side": "BUY",
        "stopPrice": "0.09",
        "origQty": "100",
        "reduceOnly": True,
        "positionSide": "BOTH",
        "clientAlgoId": "owner-sl-1",
        "createTime": ts - 60_000,
    }
    handlers = _menu_close_handlers(open_orders=(200, []))
    handlers[("GET", "/fapi/v1/openAlgoOrders")] = (200, [owner_algo])
    handlers[("DELETE", "/fapi/v1/algoOrder")] = (200, {"code": "200", "msg": "success"})
    transport = RecordingTransport(handlers)
    handle_exec_callback(
        conn,
        f"xx:{intent_id}",
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(transport),
        now_ms_value=ts,
        message={"message_id": 1},
    )
    algo_deletes = [c for c in transport.calls if c[0] == "DELETE" and c[1] == "/fapi/v1/algoOrder"]
    assert any(str(c[2].get("algoId")) == "55" for c in algo_deletes)
    row = get_exec_intent(conn, intent_id)
    assert row is not None and row["status"] == "done"
    conn.close()


def test_flag_on_cancels_owner_regular_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")
    monkeypatch.setattr(BinanceTradeClient, "assert_trade_key_safe", lambda self, **_: {"ok": True})
    monkeypatch.setattr(BinanceTradeClient, "assert_account_modes", lambda self: None)
    conn = open_db(tmp_path / "xx-owner-limit-on.db")
    ts = now_ms()
    intent_id = insert_exec_intent(
        conn,
        kind="exit",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "source": "menu",
            "qty": "100",
            "entry": "0.08",
            "position_side": "BOTH",
            "confirm_text": "c",
        },
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    owner_limit = {
        "symbol": "DOGEUSDT",
        "orderId": 77,
        "type": "LIMIT",
        "side": "BUY",
        "price": "0.075",
        "origQty": "100",
        "reduceOnly": True,
        "positionSide": "BOTH",
        "clientOrderId": "owner-tp-1",
        "time": ts - 60_000,
    }
    handlers = _menu_close_handlers(open_orders=(200, [owner_limit]))
    handlers[("DELETE", "/fapi/v1/order")] = (200, {"orderId": 77})
    transport = RecordingTransport(handlers)
    handle_exec_callback(
        conn,
        f"xx:{intent_id}",
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(transport),
        now_ms_value=ts,
        message={"message_id": 1},
    )
    order_deletes = [c for c in transport.calls if c[0] == "DELETE" and c[1] == "/fapi/v1/order"]
    assert any(str(c[2].get("orderId")) == "77" for c in order_deletes)
    # Regular id must never hit the algo endpoint.
    assert not any(
        c[0] == "DELETE" and c[1] == "/fapi/v1/algoOrder" and str(c[2].get("algoId")) == "77"
        for c in transport.calls
    )
    # Replay: no second market, no second DELETE of the same regular id.
    n_posts = len([c for c in transport.calls if c[0] == "POST" and c[1] == "/fapi/v1/order"])
    n_order_dels = len(order_deletes)
    handle_exec_callback(
        conn,
        f"xx:{intent_id}",
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(transport),
        now_ms_value=ts + 1,
        message={"message_id": 1},
    )
    assert len([c for c in transport.calls if c[0] == "POST" and c[1] == "/fapi/v1/order"]) == n_posts
    replay_order_dels = [
        c
        for c in transport.calls
        if c[0] == "DELETE" and c[1] == "/fapi/v1/order" and str(c[2].get("orderId")) == "77"
    ]
    assert len(replay_order_dels) == n_order_dels
    conn.close()


def test_flag_on_recover_reduce_flat_cancels_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")
    conn = open_db(tmp_path / "rec-flat-on.db")
    ts = now_ms()
    rid = insert_exec_intent(
        conn,
        kind="reduce",
        symbol="DOGEUSDT",
        side="short",
        preview={"qty": "100", "entry": "1", "close_qty": "50", "leave_qty": "50"},
        created_at_ms=ts,
        expires_at_ms=ts + 1,
    )
    finish_exec_intent(conn, rid, status="unknown", result_text="u", expect_status="preview")
    insert_exec_log(conn, intent_id=rid, step="reduce_market", endpoint="/fapi/v1/order", created_at_ms=ts)
    jv_sl = {
        **_verified_stop(client_id="jv-old-sl", algo_id=11, trigger="1.1", qty="50"),
        "createTime": ts - 10_000,
    }
    owner_sl = {
        **_verified_stop(client_id="owner-sl", algo_id=99, trigger="1.2", qty="50"),
        "createTime": ts - 10_000,
    }
    handlers = {
        ("GET", "/fapi/v1/order"): (
            200,
            {"orderId": 1, "avgPrice": "0.07", "executedQty": "100", "status": "FILLED"},
        ),
        ("GET", "/fapi/v3/positionRisk"): (200, []),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, [jv_sl, owner_sl]),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _filters()),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "1"}),
        ("DELETE", "/fapi/v1/algoOrder"): (200, {"code": "200", "msg": "success"}),
    }
    transport = RecordingTransport(handlers)
    run_exec_recovery(conn, _client(transport), now_ms_value=ts + 1000)
    row = get_exec_intent(conn, rid)
    assert row["status"] == "done"
    deletes = [c for c in transport.calls if c[0] == "DELETE"]
    assert any(str(c[2].get("algoId")) == "11" for c in deletes)
    assert any(str(c[2].get("algoId")) == "99" for c in deletes)
    assert not any(c[0] == "POST" and c[1] == "/fapi/v1/order" for c in transport.calls)
    conn.close()


def test_flag_on_flat_mid_reduce_cancels_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_REDUCE", "on")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")
    monkeypatch.setattr(BinanceTradeClient, "assert_trade_key_safe", lambda self, **_: {"ok": True})
    monkeypatch.setattr(BinanceTradeClient, "assert_account_modes", lambda self: None)
    conn = open_db(tmp_path / "flat-mid-on.db")
    ts = now_ms()
    rid = insert_exec_intent(
        conn,
        kind="reduce",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "qty": "100",
            "entry": "1",
            "close_qty": "50",
            "leave_qty": "50",
            "source": "menu",
            "confirm_text": "c",
            "position_side": "BOTH",
        },
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    jv_sl = {**_verified_stop(client_id="jv-sl", algo_id=11, trigger="1.1", qty="100"), "createTime": ts - 1}
    owner_sl = {
        **_verified_stop(client_id="owner-sl", algo_id=99, trigger="1.2", qty="100"),
        "createTime": ts - 1,
    }
    state = {"n": 0}

    def _pos(_clean: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
        state["n"] += 1
        if state["n"] <= 1:
            return (200, [_open_pos(amt="-100", entry="1", mark="1")])
        return (200, [])

    handlers = {
        ("GET", "/fapi/v1/positionSide/dual"): (200, {"dualSidePosition": False}),
        ("GET", "/fapi/v1/multiAssetsMargin"): (200, {"multiAssetsMargin": False}),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _filters()),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "1"}),
        ("GET", "/fapi/v3/positionRisk"): _pos,
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, [jv_sl, owner_sl]),
        ("GET", "/fapi/v1/userTrades"): (200, [{"realizedPnl": "1"}]),
        ("POST", "/fapi/v1/order"): (
            200,
            {"orderId": 42, "avgPrice": "0.99", "executedQty": "100", "status": "FILLED"},
        ),
        ("GET", "/fapi/v1/order"): (
            200,
            {"orderId": 42, "avgPrice": "0.99", "executedQty": "100", "status": "FILLED"},
        ),
        ("DELETE", "/fapi/v1/algoOrder"): (200, {"code": "200", "msg": "success"}),
    }
    transport = RecordingTransport(handlers)
    handle_exec_callback(
        conn,
        f"xr:{rid}",
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(transport),
        now_ms_value=ts,
        message={"message_id": 1},
    )
    deletes = [c for c in transport.calls if c[0] == "DELETE"]
    assert any(str(c[2].get("algoId")) == "11" for c in deletes)
    assert any(str(c[2].get("algoId")) == "99" for c in deletes)
    assert get_exec_intent(conn, rid)["status"] == "done"
    conn.close()


def test_reread_raise_unknown_pending_zero_owner_deletes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")
    monkeypatch.setattr(BinanceTradeClient, "assert_trade_key_safe", lambda self, **_: {"ok": True})
    monkeypatch.setattr(BinanceTradeClient, "assert_account_modes", lambda self: None)
    conn = open_db(tmp_path / "reread-raise.db")
    ts = now_ms()
    intent_id = insert_exec_intent(
        conn,
        kind="exit",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "source": "menu",
            "qty": "100",
            "entry": "0.08",
            "position_side": "BOTH",
            "confirm_text": "c",
        },
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    owner_algo = {
        "symbol": "DOGEUSDT",
        "algoId": 55,
        "type": "STOP_MARKET",
        "side": "BUY",
        "stopPrice": "0.09",
        "origQty": "100",
        "reduceOnly": True,
        "positionSide": "BOTH",
        "clientAlgoId": "owner-sl-1",
        "createTime": ts - 1,
    }
    state = {"n": 0}

    def _pos(_clean: dict[str, Any]) -> Any:
        state["n"] += 1
        if state["n"] <= 1:
            return (200, [_open_pos()])
        # Flat for the post-fill check, then raise on owner-phase re-read.
        if state["n"] == 2:
            return (200, [])
        raise RuntimeError("positionRisk down")

    handlers = _menu_close_handlers(open_orders=(200, []))
    handlers[("GET", "/fapi/v3/positionRisk")] = _pos
    handlers[("GET", "/fapi/v1/openAlgoOrders")] = (200, [owner_algo])
    handlers[("DELETE", "/fapi/v1/algoOrder")] = (200, {"code": "200", "msg": "success"})
    transport = RecordingTransport(handlers)
    handle_exec_callback(
        conn,
        f"xx:{intent_id}",
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(transport),
        now_ms_value=ts,
        message={"message_id": 1},
    )
    row = get_exec_intent(conn, intent_id)
    assert row is not None
    assert row["status"] == "unknown"
    steps = [
        r[0]
        for r in conn.execute(
            "SELECT step FROM assist_exec_log WHERE intent_id=? ORDER BY id", (intent_id,)
        ).fetchall()
    ]
    assert "exit_cancel_pending" in steps
    owner_deletes = [
        c
        for c in transport.calls
        if c[0] == "DELETE" and c[1] == "/fapi/v1/algoOrder" and str(c[2].get("algoId")) == "55"
    ]
    assert owner_deletes == []
    assert symbol_has_in_flight_exec(conn, "DOGEUSDT") is True
    conn.close()


def test_post_anchor_and_missing_create_time_listed_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")
    monkeypatch.setattr(BinanceTradeClient, "assert_trade_key_safe", lambda self, **_: {"ok": True})
    monkeypatch.setattr(BinanceTradeClient, "assert_account_modes", lambda self: None)
    conn = open_db(tmp_path / "anchor.db")
    ts = now_ms()
    intent_id = insert_exec_intent(
        conn,
        kind="exit",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "source": "menu",
            "qty": "100",
            "entry": "0.08",
            "position_side": "BOTH",
            "confirm_text": "c",
        },
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    post_anchor = {
        "symbol": "DOGEUSDT",
        "algoId": 61,
        "type": "STOP_MARKET",
        "side": "BUY",
        "stopPrice": "0.09",
        "origQty": "100",
        "reduceOnly": True,
        "positionSide": "BOTH",
        "clientAlgoId": "owner-new",
        "createTime": ts + 10_000_000,  # after any market-log anchor
    }
    missing_time = {
        "symbol": "DOGEUSDT",
        "algoId": 62,
        "type": "TAKE_PROFIT_MARKET",
        "side": "BUY",
        "stopPrice": "0.05",
        "origQty": "100",
        "reduceOnly": True,
        "positionSide": "BOTH",
        "clientAlgoId": "owner-no-time",
    }
    handlers = _menu_close_handlers(open_orders=(200, []))
    handlers[("GET", "/fapi/v1/openAlgoOrders")] = (200, [post_anchor, missing_time])
    handlers[("DELETE", "/fapi/v1/algoOrder")] = (200, {"code": "200", "msg": "success"})
    transport = RecordingTransport(handlers)
    handle_exec_callback(
        conn,
        f"xx:{intent_id}",
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(transport),
        now_ms_value=ts,
        message={"message_id": 1},
    )
    algo_deletes = [c for c in transport.calls if c[0] == "DELETE" and c[1] == "/fapi/v1/algoOrder"]
    assert algo_deletes == []
    text = str(get_exec_intent(conn, intent_id)["result_text"])
    assert OWNER_CLOSING_ORDER_NOTE in text
    assert "STOP_MARKET" in text and "TAKE_PROFIT_MARKET" in text
    conn.close()


def test_untouched_opposite_entry_and_reduce_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")
    monkeypatch.setattr(BinanceTradeClient, "assert_trade_key_safe", lambda self, **_: {"ok": True})
    monkeypatch.setattr(BinanceTradeClient, "assert_account_modes", lambda self: None)
    conn = open_db(tmp_path / "untouched.db")
    ts = now_ms()
    intent_id = insert_exec_intent(
        conn,
        kind="exit",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "source": "menu",
            "qty": "100",
            "entry": "0.08",
            "position_side": "BOTH",
            "confirm_text": "c",
        },
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    opposite = {
        "symbol": "DOGEUSDT",
        "orderId": 1,
        "type": "STOP_MARKET",
        "side": "SELL",  # wrong close side for short
        "stopPrice": "0.1",
        "origQty": "100",
        "reduceOnly": True,
        "positionSide": "BOTH",
        "time": ts - 1,
    }
    entry = {
        "symbol": "DOGEUSDT",
        "orderId": 2,
        "type": "LIMIT",
        "side": "BUY",
        "price": "0.07",
        "origQty": "100",
        "reduceOnly": False,
        "positionSide": "BOTH",
        "time": ts - 1,
    }
    reduce_false = {
        "symbol": "DOGEUSDT",
        "orderId": 3,
        "type": "LIMIT",
        "side": "BUY",
        "price": "0.06",
        "origQty": "100",
        "reduceOnly": "false",
        "positionSide": "BOTH",
        "time": ts - 1,
    }
    handlers = _menu_close_handlers(open_orders=(200, [opposite, entry, reduce_false]))
    handlers[("DELETE", "/fapi/v1/order")] = (200, {"orderId": 0})
    transport = RecordingTransport(handlers)
    handle_exec_callback(
        conn,
        f"xx:{intent_id}",
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(transport),
        now_ms_value=ts,
        message={"message_id": 1},
    )
    assert [c for c in transport.calls if c[0] == "DELETE" and c[1] == "/fapi/v1/order"] == []
    conn.close()


def test_reappeared_position_done_no_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")
    monkeypatch.setattr(BinanceTradeClient, "assert_trade_key_safe", lambda self, **_: {"ok": True})
    monkeypatch.setattr(BinanceTradeClient, "assert_account_modes", lambda self: None)
    conn = open_db(tmp_path / "reopen.db")
    ts = now_ms()
    intent_id = insert_exec_intent(
        conn,
        kind="exit",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "source": "menu",
            "qty": "100",
            "entry": "0.08",
            "position_side": "BOTH",
            "confirm_text": "c",
        },
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    owner_a = {
        "symbol": "DOGEUSDT",
        "algoId": 10,
        "type": "STOP_MARKET",
        "side": "BUY",
        "stopPrice": "0.09",
        "origQty": "50",
        "reduceOnly": True,
        "positionSide": "BOTH",
        "clientAlgoId": "owner-a",
        "createTime": ts - 1,
    }
    owner_b = {
        "symbol": "DOGEUSDT",
        "algoId": 11,
        "type": "TAKE_PROFIT_MARKET",
        "side": "BUY",
        "stopPrice": "0.05",
        "origQty": "50",
        "reduceOnly": True,
        "positionSide": "BOTH",
        "clientAlgoId": "owner-b",
        "createTime": ts - 1,
    }
    state = {"n": 0, "cancels": 0}

    def _pos(_clean: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
        state["n"] += 1
        if state["n"] <= 1:
            return (200, [_open_pos()])
        # Flat until after the first owner cancel, then reappear.
        if state["cancels"] >= 1:
            return (200, [_open_pos(amt="-10", entry="0.08")])
        return (200, [])

    def _del_algo(clean: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        state["cancels"] += 1
        return (200, {"code": "200", "msg": "success"})

    handlers = _menu_close_handlers(open_orders=(200, []))
    handlers[("GET", "/fapi/v3/positionRisk")] = _pos
    handlers[("GET", "/fapi/v1/openAlgoOrders")] = (200, [owner_a, owner_b])
    handlers[("DELETE", "/fapi/v1/algoOrder")] = _del_algo
    transport = RecordingTransport(handlers)
    handle_exec_callback(
        conn,
        f"xx:{intent_id}",
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(transport),
        now_ms_value=ts,
        message={"message_id": 1},
    )
    row = get_exec_intent(conn, intent_id)
    assert row is not None and row["status"] == "done"
    steps = [
        r[0]
        for r in conn.execute("SELECT step FROM assist_exec_log WHERE intent_id=?", (intent_id,)).fetchall()
    ]
    assert "exit_cancel_pending" not in steps
    result_text = str(row["result_text"])
    assert "ไม้กลับมาแล้ว" in result_text
    assert "ลบในแอป" in result_text
    # Only the first owner cancel should have fired before abort.
    owner_dels = [c for c in transport.calls if c[0] == "DELETE" and c[1] == "/fapi/v1/algoOrder"]
    assert len(owner_dels) == 1
    conn.close()


def test_flag_off_position_risk_count_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag off: no extra positionRisk GETs beyond today's jv- phase."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.delenv("JARVISE_ASSIST_CANCEL_OWNER", raising=False)
    monkeypatch.setattr(BinanceTradeClient, "assert_trade_key_safe", lambda self, **_: {"ok": True})
    monkeypatch.setattr(BinanceTradeClient, "assert_account_modes", lambda self: None)
    conn = open_db(tmp_path / "pr-count.db")
    ts = now_ms()
    intent_id = insert_exec_intent(
        conn,
        kind="exit",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "source": "menu",
            "qty": "100",
            "entry": "0.08",
            "position_side": "BOTH",
            "confirm_text": "c",
        },
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    owner_algo = {
        "symbol": "DOGEUSDT",
        "algoId": 55,
        "type": "STOP_MARKET",
        "side": "BUY",
        "stopPrice": "0.09",
        "origQty": "100",
        "reduceOnly": True,
        "positionSide": "BOTH",
        "clientAlgoId": "owner-sl-1",
        "createTime": ts - 1,
    }
    handlers = _menu_close_handlers(open_orders=(200, []))
    handlers[("GET", "/fapi/v1/openAlgoOrders")] = (200, [owner_algo])
    transport = RecordingTransport(handlers)
    handle_exec_callback(
        conn,
        f"xx:{intent_id}",
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(transport),
        now_ms_value=ts,
        message={"message_id": 1},
    )
    pr = [c for c in transport.calls if c[0] == "GET" and c[1] == "/fapi/v3/positionRisk"]
    # open check + post-fill flat check (same as pre-M1a-2).
    assert len(pr) == 2
    assert [c for c in transport.calls if c[0] == "DELETE"] == []
    conn.close()


def test_confirm_copy_split_from_after_close(monkeypatch: pytest.MonkeyPatch) -> None:
    from decimal import Decimal

    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")
    pos = FuturesPosition(
        symbol="BTCUSDT",
        position_side="BOTH",
        position_amt=Decimal("1"),
        entry_price=Decimal("100"),
        mark_price=Decimal("101"),
    )
    owner = {
        "symbol": "BTCUSDT",
        "type": "STOP_MARKET",
        "side": "SELL",
        "stopPrice": "90",
        "origQty": "1",
        "reduceOnly": True,
        "positionSide": "BOTH",
    }
    body = _format_menu_exit_confirm(pos=pos, orders=[owner], unrealized_line="+1R", tick=None)
    assert OWNER_CLOSING_HEADER_WILL_CANCEL in body
    assert OWNER_CLOSING_ORDER_NOTE_WILL_CANCEL in body
    assert OWNER_CLOSING_HEADER not in body
    assert "ลบในแอป" not in body
    # After-close note stays the leftover wording.
    assert "ลบในแอป" in OWNER_CLOSING_ORDER_NOTE


def test_recover_no_cancel_after_max_age(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")
    conn = open_db(tmp_path / "aged.db")
    ts = now_ms()
    rid = insert_exec_intent(
        conn,
        kind="exit",
        symbol="DOGEUSDT",
        side="short",
        preview={"qty": "100", "entry": "0.08"},
        created_at_ms=ts - RECOVER_MAX_AGE_MS - 1,
        expires_at_ms=ts,
    )
    finish_exec_intent(conn, rid, status="unknown", result_text="u", expect_status="preview")
    insert_exec_log(
        conn, intent_id=rid, step="exit_cancel_pending", endpoint="/fapi/v1/algoOrder", created_at_ms=ts
    )
    owner_sl = {
        **_verified_stop(client_id="owner-sl", algo_id=99, trigger="0.09", qty="100"),
        "createTime": ts - RECOVER_MAX_AGE_MS - 10,
    }
    handlers = {
        ("GET", "/fapi/v1/order"): (400, {"code": -2013, "msg": "Order does not exist."}),
        ("GET", "/fapi/v3/positionRisk"): (200, []),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, [owner_sl]),
        ("DELETE", "/fapi/v1/algoOrder"): (200, {"code": "200", "msg": "success"}),
    }
    transport = RecordingTransport(handlers)
    run_exec_recovery(conn, _client(transport), now_ms_value=ts)
    assert [c for c in transport.calls if c[0] == "DELETE"] == []
    row = get_exec_intent(conn, rid)
    # Aged-readonly: stale when the snapshot reads, else unknown unread — never cancel.
    assert row["status"] in {"stale", "unknown"}
    conn.close()


def test_quick_stopout_cancels_owner_live_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")
    monkeypatch.setattr(
        "jarvise_assist.watch.build_trade_client",
        lambda: (_FakeTrade(close_side="BUY"), None),
    )
    conn = open_db(tmp_path / "quick-hook.db")
    write_settings(conn, enabled=True)
    ts = now_ms()
    closed_info = {
        "closed": [{"symbol": "DOGEUSDT", "side": "short", "closed_at_ms": ts, "trade_id": "aqt_1"}]
    }
    monkeypatch.setattr(
        "jarvise_assist.watch.sync_quick_trades",
        lambda *_a, **_k: closed_info,
    )
    sent: list[str] = []
    result = run_assist_watch(
        conn,
        futures=FakeFutures(positions=[]),
        now_ms_value=ts,
        send=lambda t, **_k: sent.append(t) or True,
    )
    assert result["ok"] is True
    assert _FakeTrade.cancelled_algo == ["55"]
    assert _FakeTrade.cancelled_order == ["77"]
    assert any("DOGEUSDT" in m for m in sent)

    # testnet / off: no owner DELETE
    _FakeTrade.reset()
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    run_assist_watch(
        conn,
        futures=FakeFutures(positions=[]),
        now_ms_value=ts + 1,
        send=lambda *_a, **_k: True,
    )
    assert _FakeTrade.cancelled_algo == []
    assert _FakeTrade.cancelled_order == []
    conn.close()


def test_watch_in_universe_0_and_in_flight_skip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")
    trade = _FakeTrade(close_side="SELL")  # long → close SELL
    monkeypatch.setattr("jarvise_assist.watch.build_trade_client", lambda: (trade, None))
    conn = open_db(tmp_path / "outside.db")
    write_settings(conn, enabled=False)  # assistant off still cancels in live
    ts = now_ms()
    upsert_watch_position(
        conn,
        {
            "symbol": "XRPUSDT",
            "position_side": "BOTH",
            "side": "long",
            "entry": 0.5,
            "qty": 10.0,
            "in_universe": 0,
            "signal_id": None,
            "initial_stop": 0.4,
            "initial_stop_source": "signal",
            "entry_time_ms": ts - 10_000,
            "no_stop_latched": 0,
            "last_seen_ms": ts - 5_000,
            "closed_at_ms": None,
        },
    )
    sent: list[str] = []
    result = run_assist_watch(
        conn,
        futures=FakeFutures(positions=[]),
        now_ms_value=ts,
        send=lambda t, **_k: sent.append(t) or True,
    )
    assert result["ok"] is True
    assert trade.cancelled_algo or trade.cancelled_order
    assert any("XRPUSDT" in m for m in sent)

    # In-flight skip: insert unknown intent, no owner cancel / no notice spam.
    trade.reset()
    sent.clear()
    upsert_watch_position(
        conn,
        {
            "symbol": "ADAUSDT",
            "position_side": "BOTH",
            "side": "short",
            "entry": 0.4,
            "qty": 100.0,
            "in_universe": 1,
            "signal_id": None,
            "initial_stop": 0.43,
            "initial_stop_source": "signal",
            "entry_time_ms": ts,
            "no_stop_latched": 0,
            "last_seen_ms": ts,
            "closed_at_ms": None,
        },
    )
    eid = insert_exec_intent(
        conn,
        kind="exit",
        symbol="ADAUSDT",
        side="short",
        preview={"qty": "100", "entry": "0.4"},
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    finish_exec_intent(conn, eid, status="unknown", result_text="u", expect_status="preview")
    assert symbol_has_in_flight_exec(conn, "ADAUSDT") is True
    run_assist_watch(
        conn,
        futures=FakeFutures(positions=[]),
        now_ms_value=ts + 1,
        send=lambda t, **_k: sent.append(t) or True,
    )
    # jv orphan may still run; owner cancel must not (FakeTrade.owner path uses cancel_order).
    assert trade.cancelled_order == []
    conn.close()


def test_watch_failure_does_not_raise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")

    class _BoomTrade(_FakeTrade):
        def open_orders(self, **_k: Any) -> list[Any]:
            raise RuntimeError("boom")

        def open_algo_orders(self, **_k: Any) -> list[Any]:
            raise RuntimeError("boom")

        def position_risk(self, **_k: Any) -> list[Any]:
            raise RuntimeError("boom")

    monkeypatch.setattr("jarvise_assist.watch.build_trade_client", lambda: (_BoomTrade(), None))
    conn = open_db(tmp_path / "watch-boom.db")
    write_settings(conn, enabled=True)
    ts = now_ms()
    upsert_watch_position(
        conn,
        {
            "symbol": "BTCUSDT",
            "position_side": "BOTH",
            "side": "long",
            "entry": 1.0,
            "qty": 1.0,
            "in_universe": 1,
            "signal_id": None,
            "initial_stop": 0.9,
            "initial_stop_source": "signal",
            "entry_time_ms": ts,
            "no_stop_latched": 0,
            "last_seen_ms": ts,
            "closed_at_ms": None,
        },
    )
    result = run_assist_watch(
        conn,
        futures=FakeFutures(positions=[]),
        now_ms_value=ts + 1,
        send=lambda *_a, **_k: True,
    )
    assert result["ok"] is True
    conn.close()


def test_no_message_when_nothing_happened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")

    class _Empty(_FakeTrade):
        def open_orders(self, **_k: Any) -> list[Any]:
            return []

        def open_algo_orders(self, **_k: Any) -> list[Any]:
            return []

    monkeypatch.setattr("jarvise_assist.watch.build_trade_client", lambda: (_Empty(), None))
    conn = open_db(tmp_path / "noop.db")
    write_settings(conn, enabled=True)
    ts = now_ms()
    upsert_watch_position(
        conn,
        {
            "symbol": "BTCUSDT",
            "position_side": "BOTH",
            "side": "long",
            "entry": 1.0,
            "qty": 1.0,
            "in_universe": 1,
            "signal_id": None,
            "initial_stop": 0.9,
            "initial_stop_source": "signal",
            "entry_time_ms": ts,
            "no_stop_latched": 0,
            "last_seen_ms": ts,
            "closed_at_ms": None,
        },
    )
    sent: list[str] = []
    run_assist_watch(
        conn,
        futures=FakeFutures(positions=[]),
        now_ms_value=ts + 1,
        send=lambda t, **_k: sent.append(t) or True,
    )
    assert not any("🧹" in m for m in sent)
    conn.close()


def test_cancel_jv_unchanged() -> None:
    """Widening cancel_jv would fail this pin (mutation probe 10)."""
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/openAlgoOrders"): (
            200,
            [
                {"algoId": 1, "clientAlgoId": "jv-abc-sl0", "symbol": "DOGEUSDT"},
                {"algoId": 2, "clientAlgoId": "manual-stop", "symbol": "DOGEUSDT"},
            ],
        ),
        ("DELETE", "/fapi/v1/algoOrder"): (200, {"algoId": 1}),
    }
    transport = RecordingTransport(handlers)
    cancelled, ok = cancel_jv_orders_for_symbol(_client(transport), "DOGEUSDT")
    assert cancelled == ["1"]
    assert ok is True
    assert len([c for c in transport.calls if c[0] == "DELETE"]) == 1


class _FakeTrade:
    cancelled_algo: list[str] = []
    cancelled_order: list[str] = []

    def __init__(
        self,
        *,
        close_side: str = "BUY",
        positions: list[Any] | None = None,
        extra_algos: list[dict[str, Any]] | None = None,
        owner_create_ms: int = 1,
    ) -> None:
        type(self).cancelled_algo = []
        type(self).cancelled_order = []
        self._close_side = close_side.upper()
        self._positions = list(positions) if positions is not None else []
        self._extra_algos = list(extra_algos or [])
        self._owner_create_ms = int(owner_create_ms)

    @classmethod
    def reset(cls) -> None:
        cls.cancelled_algo = []
        cls.cancelled_order = []

    def position_risk(self, **_k: Any) -> list[Any]:
        return list(self._positions)

    def open_orders(self, *, symbol: str | None = None, strict: bool = False) -> list[Any]:
        sym = (symbol or "DOGEUSDT").upper()
        return [
            {
                "symbol": sym,
                "orderId": 77,
                "type": "LIMIT",
                "side": self._close_side,
                "price": "0.07",
                "origQty": "100",
                "reduceOnly": True,
                "positionSide": "BOTH",
                "clientOrderId": "owner-lim",
                "time": self._owner_create_ms,
            }
        ]

    def open_algo_orders(self, *, symbol: str | None = None, strict: bool = False) -> list[Any]:
        sym = (symbol or "DOGEUSDT").upper()
        out: list[dict[str, Any]] = [
            {
                "symbol": sym,
                "algoId": 55,
                "type": "STOP_MARKET",
                "side": self._close_side,
                "stopPrice": "0.09",
                "origQty": "100",
                "reduceOnly": True,
                "positionSide": "BOTH",
                "clientAlgoId": "owner-sl",
                "createTime": self._owner_create_ms,
            }
        ]
        for algo in self._extra_algos:
            if str(algo.get("symbol") or "").upper() == sym:
                out.append(algo)
        return out

    def cancel_algo(self, *, symbol: str, algo_id: str | int) -> Any:
        self.cancelled_algo.append(str(algo_id))
        return {"code": "200"}

    def cancel_order(self, *, symbol: str, order_id: str | int) -> Any:
        self.cancelled_order.append(str(order_id))
        return {"orderId": order_id}

    def all_open_orders(self, **_k: Any) -> list[Any]:
        return [*self.open_orders(**_k), *self.open_algo_orders(**_k)]


def _pepe_pos() -> FuturesPosition:
    return FuturesPosition(
        symbol="PEPEUSDT",
        position_side="BOTH",
        position_amt=Decimal("1000000"),
        entry_price=Decimal("0.00001"),
        mark_price=Decimal("0.00001"),
    )


def _jv_algo_901(symbol: str = "PEPEUSDT") -> dict[str, Any]:
    return {
        "symbol": symbol,
        "algoId": 901,
        "type": "STOP_MARKET",
        "side": "SELL",
        "stopPrice": "0.000009",
        "origQty": "1000000",
        "reduceOnly": True,
        "positionSide": "BOTH",
        "clientAlgoId": "jv-pepe-sl0",
        "createTime": 1,
    }


def test_h1_quick_close_while_position_open_must_not_delete_jv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H1: Quick closed while PEPEUSDT still open → zero DELETEs of 901/55/77."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")
    pepe = _pepe_pos()
    trade = _FakeTrade(
        close_side="SELL",
        positions=[pepe],
        extra_algos=[_jv_algo_901()],
        owner_create_ms=1,
    )
    monkeypatch.setattr("jarvise_assist.watch.build_trade_client", lambda: (trade, None))
    conn = open_db(tmp_path / "h1-pepe.db")
    write_settings(conn, enabled=True)
    ts = now_ms()
    monkeypatch.setattr(
        "jarvise_assist.watch.sync_quick_trades",
        lambda *_a, **_k: {
            "closed": [
                {
                    "symbol": "PEPEUSDT",
                    "side": "long",
                    "closed_at_ms": ts,
                    "trade_id": "aqt_pepe",
                }
            ]
        },
    )
    result = run_assist_watch(
        conn,
        futures=FakeFutures(positions=[pepe]),
        now_ms_value=ts,
        send=lambda *_a, **_k: True,
    )
    assert result["ok"] is True
    assert trade.cancelled_algo == []
    assert trade.cancelled_order == []
    assert "901" not in trade.cancelled_algo
    assert "55" not in trade.cancelled_algo
    assert "77" not in trade.cancelled_order
    conn.close()


def test_quick_flat_cancels_jv_algo_when_flag_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Plan case 1: flat Quick close DELETEs jv- algo when flag on."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")
    trade = _FakeTrade(
        close_side="BUY",
        positions=[],
        extra_algos=[_jv_algo_901("DOGEUSDT")],
        owner_create_ms=1,
    )
    monkeypatch.setattr("jarvise_assist.watch.build_trade_client", lambda: (trade, None))
    conn = open_db(tmp_path / "quick-jv-flat.db")
    write_settings(conn, enabled=True)
    ts = now_ms()
    monkeypatch.setattr(
        "jarvise_assist.watch.sync_quick_trades",
        lambda *_a, **_k: {
            "closed": [
                {
                    "symbol": "DOGEUSDT",
                    "side": "short",
                    "closed_at_ms": ts,
                    "trade_id": "aqt_jv",
                }
            ]
        },
    )
    result = run_assist_watch(
        conn,
        futures=FakeFutures(positions=[]),
        now_ms_value=ts,
        send=lambda *_a, **_k: True,
    )
    assert result["ok"] is True
    assert "901" in trade.cancelled_algo
    assert "55" in trade.cancelled_algo
    assert "77" in trade.cancelled_order
    conn.close()


def test_m3_watch_anchor_lists_post_last_seen_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Owner createTime after last_seen_ms but <= tick → listed, not DELETEd."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")
    last_seen = 1_000_000
    tick = 1_000_000 + 60_000
    create_ms = last_seen + 30_000  # after last_seen, before tick
    trade = _FakeTrade(close_side="SELL", owner_create_ms=create_ms)
    monkeypatch.setattr("jarvise_assist.watch.build_trade_client", lambda: (trade, None))
    conn = open_db(tmp_path / "anchor-watch.db")
    write_settings(conn, enabled=False)
    upsert_watch_position(
        conn,
        {
            "symbol": "XRPUSDT",
            "position_side": "BOTH",
            "side": "long",
            "entry": 0.5,
            "qty": 10.0,
            "in_universe": 0,
            "signal_id": None,
            "initial_stop": 0.4,
            "initial_stop_source": "signal",
            "entry_time_ms": last_seen - 10_000,
            "no_stop_latched": 0,
            "last_seen_ms": last_seen,
            "closed_at_ms": None,
        },
    )
    sent: list[str] = []
    result = run_assist_watch(
        conn,
        futures=FakeFutures(positions=[]),
        now_ms_value=tick,
        send=lambda t, **_k: sent.append(t) or True,
    )
    assert result["ok"] is True
    assert trade.cancelled_algo == []
    assert trade.cancelled_order == []
    assert any("XRPUSDT" in m and ("owner-sl" in m or "owner-lim" in m or "ค้าง" in m) for m in sent)
    conn.close()


def test_m3_quick_anchor_lists_post_closed_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Quick: createTime > closed_at_ms → listed only, not cancelled."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")
    closed_at = 2_000_000
    trade = _FakeTrade(close_side="BUY", owner_create_ms=closed_at + 50_000)
    monkeypatch.setattr("jarvise_assist.watch.build_trade_client", lambda: (trade, None))
    conn = open_db(tmp_path / "anchor-quick.db")
    write_settings(conn, enabled=True)
    monkeypatch.setattr(
        "jarvise_assist.watch.sync_quick_trades",
        lambda *_a, **_k: {
            "closed": [
                {
                    "symbol": "DOGEUSDT",
                    "side": "short",
                    "closed_at_ms": closed_at,
                    "trade_id": "aqt_anchor",
                }
            ]
        },
    )
    sent: list[str] = []
    run_assist_watch(
        conn,
        futures=FakeFutures(positions=[]),
        now_ms_value=closed_at + 100_000,
        send=lambda t, **_k: sent.append(t) or True,
    )
    assert trade.cancelled_order == []
    assert "55" not in trade.cancelled_algo
    assert any("DOGEUSDT" in m for m in sent)
    conn.close()


def test_m3_in_flight_skip_with_matching_close_side(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ADAUSDT short → close side BUY; in-flight → zero owner DELETEs, no 🧹."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")
    trade = _FakeTrade(close_side="BUY")  # short → close BUY
    monkeypatch.setattr("jarvise_assist.watch.build_trade_client", lambda: (trade, None))
    conn = open_db(tmp_path / "inflight-buy.db")
    write_settings(conn, enabled=True)
    ts = now_ms()
    upsert_watch_position(
        conn,
        {
            "symbol": "ADAUSDT",
            "position_side": "BOTH",
            "side": "short",
            "entry": 0.4,
            "qty": 100.0,
            "in_universe": 1,
            "signal_id": None,
            "initial_stop": 0.43,
            "initial_stop_source": "signal",
            "entry_time_ms": ts - 5_000,
            "no_stop_latched": 0,
            "last_seen_ms": ts - 1_000,
            "closed_at_ms": None,
        },
    )
    eid = insert_exec_intent(
        conn,
        kind="exit",
        symbol="ADAUSDT",
        side="short",
        preview={"qty": "100", "entry": "0.4"},
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    finish_exec_intent(conn, eid, status="unknown", result_text="u", expect_status="preview")
    assert symbol_has_in_flight_exec(conn, "ADAUSDT") is True
    sent: list[str] = []
    run_assist_watch(
        conn,
        futures=FakeFutures(positions=[]),
        now_ms_value=ts + 1,
        send=lambda t, **_k: sent.append(t) or True,
    )
    assert trade.cancelled_order == []
    assert "55" not in trade.cancelled_algo
    assert not any("🧹" in m and "ADAUSDT" in m for m in sent)
    conn.close()


def test_flag_off_watch_and_quick_no_owner_delete_but_lists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flag off + live: zero owner DELETEs; Quick close notice still lists leftovers."""
    from tests.test_assist_quick_guards import _insert_quick_trade

    from jarvise_assist.quick import _send_close_notice

    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.delenv("JARVISE_ASSIST_CANCEL_OWNER", raising=False)
    trade = _FakeTrade(close_side="BUY", owner_create_ms=1)
    monkeypatch.setattr("jarvise_assist.watch.build_trade_client", lambda: (trade, None))
    conn = open_db(tmp_path / "flag-off-list.db")
    write_settings(conn, enabled=True)
    ts = now_ms()
    upsert_watch_position(
        conn,
        {
            "symbol": "BTCUSDT",
            "position_side": "BOTH",
            "side": "long",
            "entry": 1.0,
            "qty": 1.0,
            "in_universe": 1,
            "signal_id": None,
            "initial_stop": 0.9,
            "initial_stop_source": "signal",
            "entry_time_ms": ts - 10_000,
            "no_stop_latched": 0,
            "last_seen_ms": ts - 5_000,
            "closed_at_ms": None,
        },
    )
    monkeypatch.setattr(
        "jarvise_assist.watch.sync_quick_trades",
        lambda *_a, **_k: {
            "closed": [
                {
                    "symbol": "DOGEUSDT",
                    "side": "short",
                    "closed_at_ms": ts,
                    "trade_id": "aqt_flag_off",
                }
            ]
        },
    )
    run_assist_watch(
        conn,
        futures=FakeFutures(positions=[]),
        now_ms_value=ts,
        send=lambda *_a, **_k: True,
    )
    assert trade.cancelled_algo == []
    assert trade.cancelled_order == []

    # Flag off → Quick notice still lists leftovers (M2c / flag-off listing).
    tid = _insert_quick_trade(conn, symbol="DOGEUSDT", side="short", now_ms=ts, status="closed")
    conn.execute("UPDATE assist_quick_trade SET close_notice_at_ms=NULL WHERE id=?", (tid,))
    conn.commit()

    class _ListClient:
        def all_open_orders(self, *, symbol: str | None = None) -> list[dict[str, Any]]:
            return [
                {
                    "symbol": "DOGEUSDT",
                    "algoId": 55,
                    "type": "STOP_MARKET",
                    "side": "BUY",
                    "stopPrice": "0.09",
                    "origQty": "100",
                    "reduceOnly": True,
                    "positionSide": "BOTH",
                    "clientAlgoId": "owner-sl",
                    "clientOrderId": "owner-sl",
                }
            ]

    quick_sent: list[str] = []
    _send_close_notice(
        conn,
        lambda t, **_k: quick_sent.append(t) or True,
        trade_id=tid,
        client=_ListClient(),
        payload={
            "symbol": "DOGEUSDT",
            "side": "short",
            "status": "closed",
            "closed_at_ms": ts,
            "net_usdt": 0.0,
            "net_r": 0.0,
            "entry_time_ms": ts - 1,
        },
        now_ms=ts + 1,
    )
    assert any("ค้าง" in m and "DOGEUSDT" in m for m in quick_sent)
    conn.close()


def test_m1_watch_db_error_must_not_escape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """M1: DB error in owner lookups must not raise out of run_assist_watch."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")
    trade = _FakeTrade(close_side="SELL")
    monkeypatch.setattr("jarvise_assist.watch.build_trade_client", lambda: (trade, None))
    conn = open_db(tmp_path / "m1-db.db")
    write_settings(conn, enabled=True)
    ts = now_ms()
    upsert_watch_position(
        conn,
        {
            "symbol": "BTCUSDT",
            "position_side": "BOTH",
            "side": "long",
            "entry": 1.0,
            "qty": 1.0,
            "in_universe": 1,
            "signal_id": None,
            "initial_stop": 0.9,
            "initial_stop_source": "signal",
            "entry_time_ms": ts - 10_000,
            "no_stop_latched": 0,
            "last_seen_ms": ts - 5_000,
            "closed_at_ms": None,
        },
    )
    journal_calls: list[Any] = []

    def _boom_in_flight(*_a: Any, **_k: Any) -> bool:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("jarvise_assist.watch.symbol_has_in_flight_exec", _boom_in_flight)
    monkeypatch.setattr(
        "jarvise_assist.watch.journal_on_close",
        lambda *a, **k: journal_calls.append((a, k)) or {},
    )
    result = run_assist_watch(
        conn,
        futures=FakeFutures(positions=[]),
        now_ms_value=ts,
        send=lambda *_a, **_k: True,
    )
    assert result["ok"] is True
    assert journal_calls, "journal_on_close must still run after DB error in owner block"
    conn.close()


def test_m2_read_failure_must_notify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """M2(a): read failure on Quick path → notice with อ่านคำสั่งค้างไม่ได้."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")

    class _BoomTrade(_FakeTrade):
        def position_risk(self, **_k: Any) -> list[Any]:
            raise RuntimeError("positionRisk down")

        def open_orders(self, **_k: Any) -> list[Any]:
            raise RuntimeError("orders down")

        def open_algo_orders(self, **_k: Any) -> list[Any]:
            raise RuntimeError("algos down")

    monkeypatch.setattr("jarvise_assist.watch.build_trade_client", lambda: (_BoomTrade(), None))
    conn = open_db(tmp_path / "m2-read.db")
    write_settings(conn, enabled=True)
    ts = now_ms()
    monkeypatch.setattr(
        "jarvise_assist.watch.sync_quick_trades",
        lambda *_a, **_k: {
            "closed": [
                {
                    "symbol": "DOGEUSDT",
                    "side": "short",
                    "closed_at_ms": ts,
                    "trade_id": "aqt_m2",
                }
            ]
        },
    )
    sent: list[str] = []
    result = run_assist_watch(
        conn,
        futures=FakeFutures(positions=[]),
        now_ms_value=ts,
        send=lambda t, **_k: sent.append(t) or True,
    )
    assert result["ok"] is True
    assert any("อ่านคำสั่งค้างไม่ได้" in m for m in sent)
    conn.close()


def test_owner_delete_fail_unknown_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """L6.3: owner DELETE non −2011/−2013 → unknown + pending + in-flight."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")
    monkeypatch.setattr(BinanceTradeClient, "assert_trade_key_safe", lambda self, **_: {"ok": True})
    monkeypatch.setattr(BinanceTradeClient, "assert_account_modes", lambda self: None)
    conn = open_db(tmp_path / "owner-del-fail.db")
    ts = now_ms()
    intent_id = insert_exec_intent(
        conn,
        kind="exit",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "source": "menu",
            "qty": "100",
            "entry": "0.08",
            "position_side": "BOTH",
            "confirm_text": "c",
        },
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    owner_algo = {
        "symbol": "DOGEUSDT",
        "algoId": 55,
        "type": "STOP_MARKET",
        "side": "BUY",
        "stopPrice": "0.09",
        "origQty": "100",
        "reduceOnly": True,
        "positionSide": "BOTH",
        "clientAlgoId": "owner-sl-1",
        "createTime": ts - 1,
    }
    handlers = _menu_close_handlers(open_orders=(200, []))
    handlers[("GET", "/fapi/v1/openAlgoOrders")] = (200, [owner_algo])
    handlers[("DELETE", "/fapi/v1/algoOrder")] = (400, {"code": -1000, "msg": "unknown error"})
    transport = RecordingTransport(handlers)
    handle_exec_callback(
        conn,
        f"xx:{intent_id}",
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(transport),
        now_ms_value=ts,
        message={"message_id": 1},
    )
    row = get_exec_intent(conn, intent_id)
    assert row is not None and row["status"] == "unknown"
    steps = [
        r[0]
        for r in conn.execute(
            "SELECT step FROM assist_exec_log WHERE intent_id=? ORDER BY id", (intent_id,)
        ).fetchall()
    ]
    assert "exit_cancel_pending" in steps
    assert symbol_has_in_flight_exec(conn, "DOGEUSDT") is True
    conn.close()


def test_l2_update_time_only_listed_not_cancelled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """L2: updateTime alone is not create time → list only."""
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")
    order = {
        "symbol": "DOGEUSDT",
        "algoId": 55,
        "type": "STOP_MARKET",
        "side": "BUY",
        "stopPrice": "0.09",
        "origQty": "100",
        "reduceOnly": True,
        "positionSide": "BOTH",
        "clientAlgoId": "owner-ut",
        "updateTime": 1_000,
    }
    assert _owner_order_create_ms(order) is None

    class _UtTrade:
        def position_risk(self, **_k: Any) -> list[Any]:
            return []

        def open_orders(self, **_k: Any) -> list[Any]:
            return []

        def open_algo_orders(self, **_k: Any) -> list[Any]:
            return [order]

        def cancel_algo(self, **_k: Any) -> Any:
            raise AssertionError("must not cancel updateTime-only order")

        def cancel_order(self, **_k: Any) -> Any:
            raise AssertionError("must not cancel")

    result = cancel_owner_reducing_orders_for_symbol(
        _UtTrade(),  # type: ignore[arg-type]
        symbol="DOGEUSDT",
        side="short",
        position_side="BOTH",
        anchor_ms=2_000,
    )
    assert result.cancelled == []
    assert any(o.get("clientAlgoId") == "owner-ut" for o in result.leftover_orders)


def test_l3_quick_missing_closed_at_lists_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """L3: Quick closed_at_ms missing/0 → list only, no owner cancel."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")
    trade = _FakeTrade(close_side="BUY", owner_create_ms=1)
    monkeypatch.setattr("jarvise_assist.watch.build_trade_client", lambda: (trade, None))
    conn = open_db(tmp_path / "l3-none.db")
    write_settings(conn, enabled=True)
    ts = now_ms()
    monkeypatch.setattr(
        "jarvise_assist.watch.sync_quick_trades",
        lambda *_a, **_k: {
            "closed": [
                {
                    "symbol": "DOGEUSDT",
                    "side": "short",
                    "closed_at_ms": None,
                    "trade_id": "aqt_none",
                }
            ]
        },
    )
    sent: list[str] = []
    run_assist_watch(
        conn,
        futures=FakeFutures(positions=[]),
        now_ms_value=ts,
        send=lambda t, **_k: sent.append(t) or True,
    )
    # No anchor → owner phase lists only; never DELETE owner 55/77.
    assert "55" not in trade.cancelled_algo
    assert "77" not in trade.cancelled_order
    assert any("ค้าง" in m or "owner-sl" in m or "owner-lim" in m or "ลบในแอป" in m for m in sent), (
        f"expected leftover listing, got {sent!r}"
    )
    conn.close()


def test_l5_already_gone_not_also_cancelled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """L5.3: −2011/−2013 → already_gone only, not also cancelled."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")
    monkeypatch.setattr(BinanceTradeClient, "assert_trade_key_safe", lambda self, **_: {"ok": True})
    monkeypatch.setattr(BinanceTradeClient, "assert_account_modes", lambda self: None)
    conn = open_db(tmp_path / "gone.db")
    ts = now_ms()
    intent_id = insert_exec_intent(
        conn,
        kind="exit",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "source": "menu",
            "qty": "100",
            "entry": "0.08",
            "position_side": "BOTH",
            "confirm_text": "c",
        },
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    owner_algo = {
        "symbol": "DOGEUSDT",
        "algoId": 55,
        "type": "STOP_MARKET",
        "side": "BUY",
        "stopPrice": "0.09",
        "origQty": "100",
        "reduceOnly": True,
        "positionSide": "BOTH",
        "clientAlgoId": "owner-sl-1",
        "createTime": ts - 1,
    }
    handlers = _menu_close_handlers(open_orders=(200, []))
    handlers[("GET", "/fapi/v1/openAlgoOrders")] = (200, [owner_algo])
    handlers[("DELETE", "/fapi/v1/algoOrder")] = (400, {"code": -2011, "msg": "Unknown order sent."})
    transport = RecordingTransport(handlers)
    sent: list[str] = []
    handle_exec_callback(
        conn,
        f"xx:{intent_id}",
        send=lambda t, **_k: sent.append(str(t)) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(transport),
        now_ms_value=ts,
        message={"message_id": 1},
    )
    row = get_exec_intent(conn, intent_id)
    assert row is not None and row["status"] == "done"

    # Direct helper check for structured lists.
    class _GoneTrade:
        def position_risk(self, **_k: Any) -> list[Any]:
            return []

        def open_orders(self, **_k: Any) -> list[Any]:
            return []

        def open_algo_orders(self, **_k: Any) -> list[Any]:
            return [owner_algo]

        def cancel_algo(self, **_k: Any) -> Any:
            from jarvise_assist.execute import TradeClientError

            raise TradeClientError("gone", http_status=400, binance_code=-2011)

        def cancel_order(self, **_k: Any) -> Any:
            raise AssertionError("unexpected")

    got = cancel_owner_reducing_orders_for_symbol(
        _GoneTrade(),  # type: ignore[arg-type]
        symbol="DOGEUSDT",
        side="short",
        position_side="BOTH",
        anchor_ms=ts,
    )
    assert "owner-sl-1" in got.already_gone
    assert "owner-sl-1" not in got.cancelled
    conn.close()


def test_mid_loop_reread_raise_unknown_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mid-loop positionRisk raise is independently fail-closed (L6.5)."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")
    monkeypatch.setattr(BinanceTradeClient, "assert_trade_key_safe", lambda self, **_: {"ok": True})
    monkeypatch.setattr(BinanceTradeClient, "assert_account_modes", lambda self: None)
    conn = open_db(tmp_path / "mid-raise.db")
    ts = now_ms()
    intent_id = insert_exec_intent(
        conn,
        kind="exit",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "source": "menu",
            "qty": "100",
            "entry": "0.08",
            "position_side": "BOTH",
            "confirm_text": "c",
        },
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    owner_a = {
        "symbol": "DOGEUSDT",
        "algoId": 10,
        "type": "STOP_MARKET",
        "side": "BUY",
        "stopPrice": "0.09",
        "origQty": "50",
        "reduceOnly": True,
        "positionSide": "BOTH",
        "clientAlgoId": "owner-a",
        "createTime": ts - 1,
    }
    owner_b = {
        "symbol": "DOGEUSDT",
        "algoId": 11,
        "type": "TAKE_PROFIT_MARKET",
        "side": "BUY",
        "stopPrice": "0.05",
        "origQty": "50",
        "reduceOnly": True,
        "positionSide": "BOTH",
        "clientAlgoId": "owner-b",
        "createTime": ts - 1,
    }
    state = {"n": 0}

    def _pos(_clean: dict[str, Any]) -> Any:
        state["n"] += 1
        if state["n"] <= 1:
            return (200, [_open_pos()])
        if state["n"] <= 3:
            # post-fill flat + pre-batch flat
            return (200, [])
        # mid-loop re-read before second DELETE
        raise RuntimeError("mid-loop down")

    handlers = _menu_close_handlers(open_orders=(200, []))
    handlers[("GET", "/fapi/v3/positionRisk")] = _pos
    handlers[("GET", "/fapi/v1/openAlgoOrders")] = (200, [owner_a, owner_b])
    handlers[("DELETE", "/fapi/v1/algoOrder")] = (200, {"code": "200", "msg": "success"})
    transport = RecordingTransport(handlers)
    handle_exec_callback(
        conn,
        f"xx:{intent_id}",
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(transport),
        now_ms_value=ts,
        message={"message_id": 1},
    )
    row = get_exec_intent(conn, intent_id)
    assert row is not None and row["status"] == "unknown"
    steps = [
        r[0]
        for r in conn.execute("SELECT step FROM assist_exec_log WHERE intent_id=?", (intent_id,)).fetchall()
    ]
    assert "exit_cancel_pending" in steps
    assert symbol_has_in_flight_exec(conn, "DOGEUSDT") is True
    conn.close()


# --- L6.1 flag-on twins (must pass unchanged with CANCEL_OWNER=on) ---


def test_twin_successful_partial_zero_algo_posts_flag_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")
    from tests.test_assist_menu_reduce import test_successful_partial_zero_algo_posts

    test_successful_partial_zero_algo_posts(tmp_path, monkeypatch)


def test_twin_xr_reduces_once_reduce_only_flag_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")
    from tests.test_assist_menu_reduce import test_xr_reduces_once_reduce_only

    test_xr_reduces_once_reduce_only(tmp_path, monkeypatch)


def test_twin_partial_be_place_then_cancel_owner_keep_tp_flag_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")
    from tests.test_assist_menu_reduce import test_partial_be_place_then_cancel_owner_keep_tp

    test_partial_be_place_then_cancel_owner_keep_tp(tmp_path, monkeypatch)


def test_twin_recover_partial_be_cancels_owner_keeps_tp_flag_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")
    from tests.test_assist_menu_reduce import test_recover_partial_be_cancels_owner_keeps_tp

    test_recover_partial_be_cancels_owner_keeps_tp(tmp_path, monkeypatch)


def test_twin_reduce_coverage_lines_flag_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")
    from tests.test_assist_menu_reduce import test_reduce_coverage_lines

    test_reduce_coverage_lines(tmp_path, monkeypatch)


def test_twin_orphan_cancels_only_jv_flag_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")
    from tests.test_assist_execute import test_orphan_cancels_only_jv

    test_orphan_cancels_only_jv(tmp_path)


# --- Fix-up #2: Medium reporting holes + Low test tightenings ---


def test_m_missing_trade_client_must_warn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Medium 1: flag-on + live + no trade client → warn, zero DELETEs, watch ok."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")
    monkeypatch.setattr(
        "jarvise_assist.watch.build_trade_client",
        lambda: (None, "missing_trade_key"),
    )
    conn = open_db(tmp_path / "m-missing-client.db")
    write_settings(conn, enabled=True)
    ts = now_ms()
    monkeypatch.setattr(
        "jarvise_assist.watch.sync_quick_trades",
        lambda *_a, **_k: {
            "closed": [
                {
                    "symbol": "DOGEUSDT",
                    "side": "short",
                    "closed_at_ms": ts,
                    "trade_id": "aqt_miss",
                }
            ]
        },
    )
    sent: list[str] = []
    result = run_assist_watch(
        conn,
        futures=FakeFutures(positions=[]),
        now_ms_value=ts,
        send=lambda t, **_k: sent.append(t) or True,
    )
    assert result["ok"] is True
    assert any("อ่านคำสั่งค้างไม่ได้" in m for m in sent), f"expected cleanup warning, got {sent!r}"
    conn.close()


def test_n_watch_lookup_db_error_must_notify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Medium 2: DB error on watch lookup → notice, journal still runs, zero DELETEs."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")
    trade = _FakeTrade(close_side="SELL")
    monkeypatch.setattr("jarvise_assist.watch.build_trade_client", lambda: (trade, None))
    conn = open_db(tmp_path / "n-db-notify.db")
    write_settings(conn, enabled=True)
    ts = now_ms()
    upsert_watch_position(
        conn,
        {
            "symbol": "BTCUSDT",
            "position_side": "BOTH",
            "side": "long",
            "entry": 1.0,
            "qty": 1.0,
            "in_universe": 1,
            "signal_id": None,
            "initial_stop": 0.9,
            "initial_stop_source": "signal",
            "entry_time_ms": ts - 10_000,
            "no_stop_latched": 0,
            "last_seen_ms": ts - 5_000,
            "closed_at_ms": None,
        },
    )
    journal_calls: list[Any] = []

    def _boom_in_flight(*_a: Any, **_k: Any) -> bool:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("jarvise_assist.watch.symbol_has_in_flight_exec", _boom_in_flight)
    monkeypatch.setattr(
        "jarvise_assist.watch.journal_on_close",
        lambda *a, **k: journal_calls.append((a, k)) or {},
    )
    sent: list[str] = []
    result = run_assist_watch(
        conn,
        futures=FakeFutures(positions=[]),
        now_ms_value=ts,
        send=lambda t, **_k: sent.append(t) or True,
    )
    assert result["ok"] is True
    assert journal_calls, "journal_on_close must still run"
    assert trade.cancelled_algo == []
    assert trade.cancelled_order == []
    assert any("อ่านคำสั่งค้างไม่ได้" in m for m in sent), f"expected read-failed notice, got {sent!r}"
    conn.close()


def test_n_helper_outer_except_must_notify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Medium 2: helper outer except (Quick path) → notice, zero DELETEs, watch ok."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")
    trade = _FakeTrade(close_side="BUY", owner_create_ms=1)
    monkeypatch.setattr("jarvise_assist.watch.build_trade_client", lambda: (trade, None))
    conn = open_db(tmp_path / "n-helper-outer.db")
    write_settings(conn, enabled=True)
    ts = now_ms()

    def _boom_in_flight(*_a: Any, **_k: Any) -> bool:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("jarvise_assist.watch.symbol_has_in_flight_exec", _boom_in_flight)
    monkeypatch.setattr(
        "jarvise_assist.watch.sync_quick_trades",
        lambda *_a, **_k: {
            "closed": [
                {
                    "symbol": "DOGEUSDT",
                    "side": "short",
                    "closed_at_ms": ts,
                    "trade_id": "aqt_n_outer",
                }
            ]
        },
    )
    sent: list[str] = []
    result = run_assist_watch(
        conn,
        futures=FakeFutures(positions=[]),
        now_ms_value=ts,
        send=lambda t, **_k: sent.append(t) or True,
    )
    assert result["ok"] is True
    assert trade.cancelled_algo == []
    assert trade.cancelled_order == []
    assert any("อ่านคำสั่งค้างไม่ได้" in m for m in sent), f"expected helper notice, got {sent!r}"
    conn.close()


def test_o_exit_recovery_list_only_leftovers_notified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Medium 3: updateTime-only owner stop → done, not DELETEd, leftover in notify text."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")
    conn = open_db(tmp_path / "o-recovery-list.db")
    ts = now_ms()
    rid = insert_exec_intent(
        conn,
        kind="exit",
        symbol="DOGEUSDT",
        side="short",
        preview={"qty": "100", "entry": "0.08", "position_side": "BOTH"},
        created_at_ms=ts,
        expires_at_ms=ts + 1,
    )
    finish_exec_intent(conn, rid, status="unknown", result_text="u", expect_status="preview")
    insert_exec_log(conn, intent_id=rid, step="exit_market", endpoint="/fapi/v1/order", created_at_ms=ts)
    owner_ut = {
        "symbol": "DOGEUSDT",
        "algoId": 55,
        "type": "STOP_MARKET",
        "orderType": "STOP_MARKET",
        "side": "BUY",
        "stopPrice": "0.09",
        "origQty": "100",
        "reduceOnly": True,
        "positionSide": "BOTH",
        "clientAlgoId": "owner-ut-only",
        "updateTime": ts - 10_000,
    }
    handlers = {
        ("GET", "/fapi/v1/order"): (
            200,
            {"orderId": 1, "avgPrice": "0.07", "executedQty": "100", "status": "FILLED"},
        ),
        ("GET", "/fapi/v3/positionRisk"): (200, []),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, [owner_ut]),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _filters()),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "1"}),
        ("DELETE", "/fapi/v1/algoOrder"): (200, {"code": "200", "msg": "success"}),
    }
    transport = RecordingTransport(handlers)
    sent: list[str] = []

    def _noop_sleep(_seconds: float) -> None:
        return None

    run_exec_recovery(
        conn,
        _client(transport),
        now_ms_value=ts + 1000,
        send=lambda t, **_k: sent.append(str(t)) or True,
        sleep=_noop_sleep,
    )
    row = get_exec_intent(conn, rid)
    assert row is not None and row["status"] == "done"
    owner_deletes = [
        c
        for c in transport.calls
        if c[0] == "DELETE" and c[1] == "/fapi/v1/algoOrder" and str(c[2].get("algoId")) == "55"
    ]
    assert owner_deletes == []
    joined = "\n".join(sent)
    assert "คำสั่งที่ยังค้างบน" in joined
    assert any(ln.strip().startswith("•") for ln in joined.splitlines())
    assert "อ่านคำสั่งค้างไม่ได้" not in joined
    bare = [m for m in sent if m.strip() == "✅ ปิด DOGEUSDT"]
    assert not bare, f"must not notify only bare close, got {sent!r}"
    # tick 0.00001 → 0.09000; pins exit first-flat recovery (:5581)
    assert "BUY 0.09000 ·" in joined, f"expected tick-formatted leftover price, got {joined!r}"
    assert "BUY 0.09 ·" not in joined
    conn.close()


def test_o_finish_flat_exit_list_only_leftovers_notified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Medium 3: menu xx: lists updateTime-only leftovers once; status done."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")
    monkeypatch.setattr(BinanceTradeClient, "assert_trade_key_safe", lambda self, **_: {"ok": True})
    monkeypatch.setattr(BinanceTradeClient, "assert_account_modes", lambda self: None)
    conn = open_db(tmp_path / "o-finish-flat.db")
    ts = now_ms()
    intent_id = insert_exec_intent(
        conn,
        kind="exit",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "source": "menu",
            "qty": "100",
            "entry": "0.08",
            "position_side": "BOTH",
            "confirm_text": "c",
        },
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    owner_ut = {
        "symbol": "DOGEUSDT",
        "algoId": 55,
        "type": "STOP_MARKET",
        "side": "BUY",
        "stopPrice": "0.09",
        "origQty": "100",
        "reduceOnly": True,
        "positionSide": "BOTH",
        "clientAlgoId": "owner-ut-finish",
        "updateTime": ts - 10_000,
    }
    handlers = _menu_close_handlers(open_orders=(200, []))
    handlers[("GET", "/fapi/v1/openAlgoOrders")] = (200, [owner_ut])
    handlers[("DELETE", "/fapi/v1/algoOrder")] = (200, {"code": "200", "msg": "success"})
    transport = RecordingTransport(handlers)
    sent: list[str] = []
    handle_exec_callback(
        conn,
        f"xx:{intent_id}",
        send=lambda t, **_k: sent.append(str(t)) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(transport),
        now_ms_value=ts,
        message={"message_id": 1},
    )
    row = get_exec_intent(conn, intent_id)
    assert row is not None and row["status"] == "done"
    owner_deletes = [
        c
        for c in transport.calls
        if c[0] == "DELETE" and c[1] == "/fapi/v1/algoOrder" and str(c[2].get("algoId")) == "55"
    ]
    assert owner_deletes == []
    result_text = str(row.get("result_text") or "")
    assert "คำสั่งที่ยังค้างบน" in result_text
    bullets = [ln for ln in result_text.splitlines() if ln.strip().startswith("•")]
    assert len(bullets) == 1, f"expected one leftover bullet, got {bullets!r}"
    assert "อ่านคำสั่งค้างไม่ได้" not in result_text
    assert result_text.strip() != "✅ ปิด DOGEUSDT"
    conn.close()


def test_p_h1_stale_snapshot_fresh_open_blocks_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H1 independent: open_keys flat but fresh position_risk open → zero DELETEs."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")
    pepe = _pepe_pos()
    trade = _FakeTrade(
        close_side="SELL",
        positions=[pepe],
        extra_algos=[_jv_algo_901()],
        owner_create_ms=1,
    )
    monkeypatch.setattr("jarvise_assist.watch.build_trade_client", lambda: (trade, None))
    conn = open_db(tmp_path / "p-stale-open.db")
    write_settings(conn, enabled=True)
    ts = now_ms()
    monkeypatch.setattr(
        "jarvise_assist.watch.sync_quick_trades",
        lambda *_a, **_k: {
            "closed": [
                {
                    "symbol": "PEPEUSDT",
                    "side": "long",
                    "closed_at_ms": ts,
                    "trade_id": "aqt_pepe_stale",
                }
            ]
        },
    )
    # Futures snapshot is flat (Quick-only this tick); fresh trade read still open.
    result = run_assist_watch(
        conn,
        futures=FakeFutures(positions=[]),
        now_ms_value=ts,
        send=lambda *_a, **_k: True,
    )
    assert result["ok"] is True
    assert "901" not in trade.cancelled_algo
    assert "55" not in trade.cancelled_algo
    assert "77" not in trade.cancelled_order
    conn.close()


def test_p_h1_stale_snapshot_position_risk_raise_blocks_and_notifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H1 independent: open_keys flat + position_risk raise → no DELETE + read-failed notice."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")

    class _RaisePosTrade(_FakeTrade):
        def position_risk(self, **_k: Any) -> list[Any]:
            raise RuntimeError("positionRisk down")

    trade = _RaisePosTrade(
        close_side="SELL",
        positions=[],
        extra_algos=[_jv_algo_901()],
        owner_create_ms=1,
    )
    monkeypatch.setattr("jarvise_assist.watch.build_trade_client", lambda: (trade, None))
    conn = open_db(tmp_path / "p-stale-raise.db")
    write_settings(conn, enabled=True)
    ts = now_ms()
    monkeypatch.setattr(
        "jarvise_assist.watch.sync_quick_trades",
        lambda *_a, **_k: {
            "closed": [
                {
                    "symbol": "PEPEUSDT",
                    "side": "long",
                    "closed_at_ms": ts,
                    "trade_id": "aqt_pepe_raise",
                }
            ]
        },
    )
    sent: list[str] = []
    result = run_assist_watch(
        conn,
        futures=FakeFutures(positions=[]),
        now_ms_value=ts,
        send=lambda t, **_k: sent.append(t) or True,
    )
    assert result["ok"] is True
    assert "901" not in trade.cancelled_algo
    assert "55" not in trade.cancelled_algo
    assert "77" not in trade.cancelled_order
    assert any("อ่านคำสั่งค้างไม่ได้" in m for m in sent)
    conn.close()


def test_q_l3_real_normalization_zero_closed_at_lists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L3: real sync_quick_trades normalizes closed_at_ms=0 → None; watch lists, no owner DELETE."""
    from jarvise_assist.quick import sync_quick_trades

    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")
    conn = open_db(tmp_path / "q-l3-norm.db")
    write_settings(conn, enabled=True)
    ts = now_ms()
    _insert_quick_reg(
        conn, reg_id="r-l3", symbol="DOGEUSDT", side="short", planned_stop=0.09, planned_entry=0.08
    )
    conn.execute(
        """
        INSERT INTO assist_quick_trade (
            id, registration_id, symbol, side, status,
            entry_avg, entry_qty, risk_usdt, entry_time_ms, updated_at_ms, planned_stop,
            entry_trade_ids
        ) VALUES (?, ?, ?, ?, 'open', 0.08, 100, 1, ?, ?, 0.09, ?)
        """,
        ("aqt_l3", "r-l3", "DOGEUSDT", "short", ts - 60_000, ts - 60_000, "[1]"),
    )
    conn.commit()

    def _rebuild_closed(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {
            "status": "closed",
            "closed_at_ms": 0,
            "symbol": "DOGEUSDT",
            "side": "short",
        }

    entry_fill = {
        "id": 1,
        "side": "SELL",
        "price": "0.08",
        "qty": "100",
        "time": ts - 60_000,
    }
    monkeypatch.setattr("jarvise_assist.quick._rebuild_open_trade", _rebuild_closed)
    monkeypatch.setattr(
        "jarvise_assist.quick.fetch_all_user_trades",
        lambda *_a, **_k: [entry_fill],
    )
    monkeypatch.setattr(
        "jarvise_assist.quick._send_close_notice",
        lambda *_a, **_k: None,
    )

    class _SyncClient:
        pass

    info = sync_quick_trades(conn, _SyncClient(), now_ms=ts, send=lambda *_a, **_k: True)
    closed = info.get("closed") or []
    assert closed, "expected a closed Quick row from sync"
    assert closed[0].get("closed_at_ms") is None

    trade = _FakeTrade(close_side="BUY", owner_create_ms=1)
    monkeypatch.setattr("jarvise_assist.watch.build_trade_client", lambda: (trade, None))
    monkeypatch.setattr("jarvise_assist.watch.sync_quick_trades", lambda *_a, **_k: info)
    sent: list[str] = []
    run_assist_watch(
        conn,
        futures=FakeFutures(positions=[]),
        now_ms_value=ts,
        send=lambda t, **_k: sent.append(t) or True,
    )
    assert "55" not in trade.cancelled_algo
    assert "77" not in trade.cancelled_order
    listing = [m for m in sent if "คำสั่งที่ยังค้างบน" in m]
    assert listing, f"expected leftover listing after None anchor, got {sent!r}"
    assert any(ln.strip().startswith("•") for ln in listing[0].splitlines())
    assert all("อ่านคำสั่งค้างไม่ได้" not in m for m in listing)
    conn.close()


def test_q_l3_real_normalization_missing_closed_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """L3: real sync normalizes missing closed_at_ms → None."""
    from jarvise_assist.quick import sync_quick_trades

    conn = open_db(tmp_path / "q-l3-missing.db")
    ts = now_ms()
    _insert_quick_reg(
        conn, reg_id="r-l3m", symbol="DOGEUSDT", side="short", planned_stop=0.09, planned_entry=0.08
    )
    conn.execute(
        """
        INSERT INTO assist_quick_trade (
            id, registration_id, symbol, side, status,
            entry_avg, entry_qty, risk_usdt, entry_time_ms, updated_at_ms, planned_stop,
            entry_trade_ids
        ) VALUES (?, ?, ?, ?, 'open', 0.08, 100, 1, ?, ?, 0.09, ?)
        """,
        ("aqt_l3m", "r-l3m", "DOGEUSDT", "short", ts - 60_000, ts - 60_000, "[1]"),
    )
    conn.commit()

    def _rebuild_missing(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"status": "closed", "symbol": "DOGEUSDT", "side": "short"}

    entry_fill = {
        "id": 1,
        "side": "SELL",
        "price": "0.08",
        "qty": "100",
        "time": ts - 60_000,
    }
    monkeypatch.setattr("jarvise_assist.quick._rebuild_open_trade", _rebuild_missing)
    monkeypatch.setattr(
        "jarvise_assist.quick.fetch_all_user_trades",
        lambda *_a, **_k: [entry_fill],
    )
    monkeypatch.setattr("jarvise_assist.quick._send_close_notice", lambda *_a, **_k: None)
    info = sync_quick_trades(conn, object(), now_ms=ts, send=lambda *_a, **_k: True)
    closed = info.get("closed") or []
    assert closed and closed[0].get("closed_at_ms") is None
    conn.close()


def test_r_prebatch_only_raise_unknown_pending_zero_owner_deletes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-batch owner-phase read fails; later reads flat → zero owner DELETEs + pending."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")
    monkeypatch.setattr(BinanceTradeClient, "assert_trade_key_safe", lambda self, **_: {"ok": True})
    monkeypatch.setattr(BinanceTradeClient, "assert_account_modes", lambda self: None)
    conn = open_db(tmp_path / "r-prebatch-only.db")
    ts = now_ms()
    intent_id = insert_exec_intent(
        conn,
        kind="exit",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "source": "menu",
            "qty": "100",
            "entry": "0.08",
            "position_side": "BOTH",
            "confirm_text": "c",
        },
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    owner_algo = {
        "symbol": "DOGEUSDT",
        "algoId": 55,
        "type": "STOP_MARKET",
        "side": "BUY",
        "stopPrice": "0.09",
        "origQty": "100",
        "reduceOnly": True,
        "positionSide": "BOTH",
        "clientAlgoId": "owner-sl-1",
        "createTime": ts - 1,
    }
    state = {"n": 0}

    def _pos(_clean: dict[str, Any]) -> Any:
        state["n"] += 1
        if state["n"] <= 1:
            return (200, [_open_pos()])
        if state["n"] == 2:
            return (200, [])  # post-fill flat
        if state["n"] == 3:
            # Pre-batch owner-phase read fails once.
            raise RuntimeError("pre-batch positionRisk down")
        # Subsequent per-DELETE reads would be flat — must never reach them.
        return (200, [])

    handlers = _menu_close_handlers(open_orders=(200, []))
    handlers[("GET", "/fapi/v3/positionRisk")] = _pos
    handlers[("GET", "/fapi/v1/openAlgoOrders")] = (200, [owner_algo])
    handlers[("DELETE", "/fapi/v1/algoOrder")] = (200, {"code": "200", "msg": "success"})
    transport = RecordingTransport(handlers)
    handle_exec_callback(
        conn,
        f"xx:{intent_id}",
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(transport),
        now_ms_value=ts,
        message={"message_id": 1},
    )
    row = get_exec_intent(conn, intent_id)
    assert row is not None
    assert row["status"] == "unknown"
    steps = [
        r[0]
        for r in conn.execute(
            "SELECT step FROM assist_exec_log WHERE intent_id=? ORDER BY id", (intent_id,)
        ).fetchall()
    ]
    assert "exit_cancel_pending" in steps
    owner_deletes = [
        c
        for c in transport.calls
        if c[0] == "DELETE" and c[1] == "/fapi/v1/algoOrder" and str(c[2].get("algoId")) == "55"
    ]
    assert owner_deletes == []
    assert symbol_has_in_flight_exec(conn, "DOGEUSDT") is True
    conn.close()


def test_s_reduce_pending_retry_lists_leftovers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reduce exit_cancel_pending retry success lists updateTime-only leftovers."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")
    conn = open_db(tmp_path / "s-reduce-retry.db")
    ts = now_ms()
    rid = insert_exec_intent(
        conn,
        kind="reduce",
        symbol="DOGEUSDT",
        side="short",
        preview={"qty": "100", "entry": "0.08", "close_qty": "100", "leave_qty": "0"},
        created_at_ms=ts,
        expires_at_ms=ts + 1,
    )
    finish_exec_intent(conn, rid, status="unknown", result_text="u", expect_status="preview")
    insert_exec_log(conn, intent_id=rid, step="reduce_market", endpoint="/fapi/v1/order", created_at_ms=ts)
    insert_exec_log(
        conn, intent_id=rid, step="exit_cancel_pending", endpoint="/fapi/v1/algoOrder", created_at_ms=ts
    )
    owner_ut = {
        "symbol": "DOGEUSDT",
        "algoId": 55,
        "type": "STOP_MARKET",
        "orderType": "STOP_MARKET",
        "side": "BUY",
        "stopPrice": "0.09",
        "origQty": "100",
        "reduceOnly": True,
        "positionSide": "BOTH",
        "clientAlgoId": "owner-ut-reduce-retry",
        "updateTime": ts - 10_000,
    }
    handlers = {
        ("GET", "/fapi/v1/order"): (
            200,
            {"orderId": 1, "avgPrice": "0.07", "executedQty": "100", "status": "FILLED"},
        ),
        ("GET", "/fapi/v3/positionRisk"): (200, []),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, [owner_ut]),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _filters()),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "1"}),
        ("DELETE", "/fapi/v1/algoOrder"): (200, {"code": "200", "msg": "success"}),
    }
    transport = RecordingTransport(handlers)
    sent: list[str] = []

    def _noop_sleep(_seconds: float) -> None:
        return None

    run_exec_recovery(
        conn,
        _client(transport),
        now_ms_value=ts + 1000,
        send=lambda t, **_k: sent.append(str(t)) or True,
        sleep=_noop_sleep,
    )
    row = get_exec_intent(conn, rid)
    assert row is not None and row["status"] == "done"
    owner_deletes = [
        c
        for c in transport.calls
        if c[0] == "DELETE" and c[1] == "/fapi/v1/algoOrder" and str(c[2].get("algoId")) == "55"
    ]
    assert owner_deletes == []
    joined = "\n".join(sent)
    assert "คำสั่งที่ยังค้างบน" in joined
    assert any(ln.strip().startswith("•") for ln in joined.splitlines())
    assert "อ่านคำสั่งค้างไม่ได้" not in joined
    bare = [m for m in sent if m.strip() == "✅ ปิด DOGEUSDT"]
    assert not bare, f"must not notify only bare close, got {sent!r}"
    # tick 0.00001 → 0.09000; fmt_price alone would print 0.09
    assert "BUY 0.09000 ·" in joined, f"expected tick-formatted leftover price, got {joined!r}"
    assert "BUY 0.09 ·" not in joined
    conn.close()


def test_t_xr_and_xx_full_close_one_leftover_bullet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """xr: and xx: full close list a list-only leftover exactly once (dedup pin)."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_REDUCE", "on")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")
    monkeypatch.setattr(BinanceTradeClient, "assert_trade_key_safe", lambda self, **_: {"ok": True})
    monkeypatch.setattr(BinanceTradeClient, "assert_account_modes", lambda self: None)

    def _one_bullet_close(*, kind: str, prefix: str, db_name: str, preview: dict[str, Any]) -> None:
        conn = open_db(tmp_path / db_name)
        ts = now_ms()
        intent_id = insert_exec_intent(
            conn,
            kind=kind,
            symbol="DOGEUSDT",
            side="short",
            preview=preview,
            created_at_ms=ts,
            expires_at_ms=ts + PREVIEW_TTL_MS,
        )
        owner_ut = {
            "symbol": "DOGEUSDT",
            "algoId": 55,
            "type": "STOP_MARKET",
            "side": "BUY",
            "stopPrice": "0.09",
            "origQty": "100",
            "reduceOnly": True,
            "positionSide": "BOTH",
            "clientAlgoId": "owner-ut-dedup",
            "updateTime": ts - 10_000,
        }
        if kind == "reduce":
            state = {"n": 0}

            def _pos(_clean: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
                state["n"] += 1
                if state["n"] <= 1:
                    return (200, [_open_pos(amt="-100", entry="1", mark="1")])
                return (200, [])

            handlers: dict[tuple[str, str], Any] = {
                ("GET", "/fapi/v1/positionSide/dual"): (200, {"dualSidePosition": False}),
                ("GET", "/fapi/v1/multiAssetsMargin"): (200, {"multiAssetsMargin": False}),
                ("GET", "/fapi/v1/exchangeInfo"): (200, _filters()),
                ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "1"}),
                ("GET", "/fapi/v3/positionRisk"): _pos,
                ("GET", "/fapi/v1/openOrders"): (200, []),
                ("GET", "/fapi/v1/openAlgoOrders"): (200, [owner_ut]),
                ("GET", "/fapi/v1/userTrades"): (200, [{"realizedPnl": "1"}]),
                ("POST", "/fapi/v1/order"): (
                    200,
                    {"orderId": 42, "avgPrice": "0.07", "executedQty": "100", "status": "FILLED"},
                ),
                ("GET", "/fapi/v1/order"): (
                    200,
                    {"orderId": 42, "avgPrice": "0.07", "executedQty": "100", "status": "FILLED"},
                ),
                ("DELETE", "/fapi/v1/algoOrder"): (200, {"code": "200", "msg": "success"}),
            }
        else:
            handlers = _menu_close_handlers(open_orders=(200, []))
            handlers[("GET", "/fapi/v1/openAlgoOrders")] = (200, [owner_ut])
            handlers[("DELETE", "/fapi/v1/algoOrder")] = (200, {"code": "200", "msg": "success"})
        transport = RecordingTransport(handlers)
        handle_exec_callback(
            conn,
            f"{prefix}{intent_id}",
            send=lambda *_a, **_k: True,
            answer=lambda *_a, **_k: None,
            edit=lambda *_a, **_k: None,
            trade=_client(transport),
            now_ms_value=ts,
            message={"message_id": 1},
        )
        row = get_exec_intent(conn, intent_id)
        assert row is not None and row["status"] == "done"
        result_text = str(row.get("result_text") or "")
        assert "คำสั่งที่ยังค้างบน" in result_text
        bullets = [ln for ln in result_text.splitlines() if ln.strip().startswith("•")]
        assert len(bullets) == 1, f"{prefix} expected one leftover bullet, got {bullets!r} in {result_text!r}"
        assert "อ่านคำสั่งค้างไม่ได้" not in result_text
        owner_deletes = [
            c
            for c in transport.calls
            if c[0] == "DELETE" and c[1] == "/fapi/v1/algoOrder" and str(c[2].get("algoId")) == "55"
        ]
        assert owner_deletes == []
        conn.close()

    _one_bullet_close(
        kind="exit",
        prefix="xx:",
        db_name="t-xx-dedup.db",
        preview={
            "source": "menu",
            "qty": "100",
            "entry": "0.08",
            "position_side": "BOTH",
            "confirm_text": "c",
        },
    )
    _one_bullet_close(
        kind="reduce",
        prefix="xr:",
        db_name="t-xr-dedup.db",
        preview={
            # Half preview; post-fill position is flat → full-close finish + listing.
            "qty": "100",
            "entry": "1",
            "close_qty": "50",
            "leave_qty": "50",
            "source": "menu",
            "confirm_text": "c",
            "position_side": "BOTH",
        },
    )


def test_u_flag_off_missing_client_silent_on_watch_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flag off + live + watch close + missing trade client → no leftover warning."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "off")
    monkeypatch.setattr(
        "jarvise_assist.watch.build_trade_client",
        lambda: (None, "missing_trade_key"),
    )
    conn = open_db(tmp_path / "u-flag-off-missing.db")
    write_settings(conn, enabled=True)
    ts = now_ms()
    upsert_watch_position(
        conn,
        {
            "symbol": "BTCUSDT",
            "position_side": "BOTH",
            "side": "long",
            "entry": 1.0,
            "qty": 1.0,
            "in_universe": 1,
            "signal_id": None,
            "initial_stop": 0.9,
            "initial_stop_source": "signal",
            "entry_time_ms": ts - 10_000,
            "no_stop_latched": 0,
            "last_seen_ms": ts - 5_000,
            "closed_at_ms": None,
        },
    )
    sent: list[str] = []
    result = run_assist_watch(
        conn,
        futures=FakeFutures(positions=[]),
        now_ms_value=ts,
        send=lambda t, **_k: sent.append(t) or True,
    )
    assert result["ok"] is True
    assert not any("อ่านคำสั่งค้างไม่ได้" in m for m in sent), f"flag-off must stay silent, got {sent!r}"
    assert not any("คำสั่งที่ยังค้างบน" in m for m in sent)
    assert not any("🧹 หลังปิด" in m for m in sent)
    conn.close()


def test_v_non_menu_finish_flat_exit_lists_leftovers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Advice/non-menu xx: listing comes from _finish_flat_exit (no menu fallback)."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")
    monkeypatch.setattr(BinanceTradeClient, "assert_trade_key_safe", lambda self, **_: {"ok": True})
    monkeypatch.setattr(BinanceTradeClient, "assert_account_modes", lambda self: None)
    conn = open_db(tmp_path / "v-non-menu-finish.db")
    ts = now_ms()
    intent_id = insert_exec_intent(
        conn,
        kind="exit",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "qty": "100",
            "entry": "0.08",
            "position_side": "BOTH",
        },
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    owner_ut = {
        "symbol": "DOGEUSDT",
        "algoId": 55,
        "type": "STOP_MARKET",
        "side": "BUY",
        "stopPrice": "0.09",
        "origQty": "100",
        "reduceOnly": True,
        "positionSide": "BOTH",
        "clientAlgoId": "owner-ut-advice",
        "updateTime": ts - 10_000,
    }
    handlers = _menu_close_handlers(open_orders=(200, []))
    handlers[("GET", "/fapi/v1/openAlgoOrders")] = (200, [owner_ut])
    handlers[("GET", "/fapi/v1/exchangeInfo")] = (200, _filters())  # tickSize 0.00001
    handlers[("DELETE", "/fapi/v1/algoOrder")] = (200, {"code": "200", "msg": "success"})
    transport = RecordingTransport(handlers)
    handle_exec_callback(
        conn,
        f"xx:{intent_id}",
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(transport),
        now_ms_value=ts,
        message={"message_id": 1},
    )
    row = get_exec_intent(conn, intent_id)
    assert row is not None and row["status"] == "done"
    owner_deletes = [
        c
        for c in transport.calls
        if c[0] == "DELETE" and c[1] == "/fapi/v1/algoOrder" and str(c[2].get("algoId")) == "55"
    ]
    assert owner_deletes == []
    result_text = str(row.get("result_text") or "")
    assert "คำสั่งที่ยังค้างบน" in result_text
    assert any(ln.strip().startswith("•") for ln in result_text.splitlines())
    assert "อ่านคำสั่งค้างไม่ได้" not in result_text
    # tick 0.00001 → 0.09000; pins _finish_flat_exit (:3335)
    assert "BUY 0.09000 ·" in result_text, f"expected tick-formatted leftover price, got {result_text!r}"
    assert "BUY 0.09 ·" not in result_text
    conn.close()


def test_w_exit_pending_retry_lists_leftovers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exit exit_cancel_pending retry success lists updateTime-only leftovers."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")
    conn = open_db(tmp_path / "w-exit-retry.db")
    ts = now_ms()
    rid = insert_exec_intent(
        conn,
        kind="exit",
        symbol="DOGEUSDT",
        side="short",
        preview={"qty": "100", "entry": "0.08", "position_side": "BOTH"},
        created_at_ms=ts,
        expires_at_ms=ts + 1,
    )
    finish_exec_intent(conn, rid, status="unknown", result_text="u", expect_status="preview")
    insert_exec_log(conn, intent_id=rid, step="exit_market", endpoint="/fapi/v1/order", created_at_ms=ts)
    insert_exec_log(
        conn, intent_id=rid, step="exit_cancel_pending", endpoint="/fapi/v1/algoOrder", created_at_ms=ts
    )
    owner_ut = {
        "symbol": "DOGEUSDT",
        "algoId": 55,
        "type": "STOP_MARKET",
        "orderType": "STOP_MARKET",
        "side": "BUY",
        "stopPrice": "0.09",
        "origQty": "100",
        "reduceOnly": True,
        "positionSide": "BOTH",
        "clientAlgoId": "owner-ut-exit-retry",
        "updateTime": ts - 10_000,
    }
    handlers = {
        ("GET", "/fapi/v1/order"): (
            200,
            {"orderId": 1, "avgPrice": "0.07", "executedQty": "100", "status": "FILLED"},
        ),
        ("GET", "/fapi/v3/positionRisk"): (200, []),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, [owner_ut]),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _filters()),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "1"}),
        ("DELETE", "/fapi/v1/algoOrder"): (200, {"code": "200", "msg": "success"}),
    }
    transport = RecordingTransport(handlers)
    sent: list[str] = []

    def _noop_sleep(_seconds: float) -> None:
        return None

    run_exec_recovery(
        conn,
        _client(transport),
        now_ms_value=ts + 1000,
        send=lambda t, **_k: sent.append(str(t)) or True,
        sleep=_noop_sleep,
    )
    row = get_exec_intent(conn, rid)
    assert row is not None and row["status"] == "done"
    owner_deletes = [
        c
        for c in transport.calls
        if c[0] == "DELETE" and c[1] == "/fapi/v1/algoOrder" and str(c[2].get("algoId")) == "55"
    ]
    assert owner_deletes == []
    joined = "\n".join(sent)
    assert "คำสั่งที่ยังค้างบน" in joined
    assert any(ln.strip().startswith("•") for ln in joined.splitlines())
    assert "อ่านคำสั่งค้างไม่ได้" not in joined
    bare = [m for m in sent if m.strip() == "✅ ปิด DOGEUSDT"]
    assert not bare, f"must not notify only bare close, got {sent!r}"
    # tick 0.00001 → 0.09000; pins exit pending-retry (:5524)
    assert "BUY 0.09000 ·" in joined, f"expected tick-formatted leftover price, got {joined!r}"
    assert "BUY 0.09 ·" not in joined
    conn.close()


def test_x_reduce_first_flat_lists_leftovers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reduce first-time-flat (no exit_cancel_pending) lists cancel.leftover_orders.

    Pins the flag-on else-branch that used to re-read via _append_menu_close_owner_orders:
    a later all_open_orders raise must not drop the leftover listing, and prices use tick.
    """
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")
    conn = open_db(tmp_path / "x-reduce-first-flat.db")
    ts = now_ms()
    rid = insert_exec_intent(
        conn,
        kind="reduce",
        symbol="DOGEUSDT",
        side="short",
        preview={"qty": "100", "entry": "0.08", "close_qty": "100", "leave_qty": "0"},
        created_at_ms=ts,
        expires_at_ms=ts + 1,
    )
    finish_exec_intent(conn, rid, status="unknown", result_text="u", expect_status="preview")
    insert_exec_log(conn, intent_id=rid, step="reduce_market", endpoint="/fapi/v1/order", created_at_ms=ts)
    # No exit_cancel_pending → first-time-flat branch, not the pending-retry path.
    owner_ut = {
        "symbol": "DOGEUSDT",
        "algoId": 55,
        "type": "STOP_MARKET",
        "orderType": "STOP_MARKET",
        "side": "BUY",
        "stopPrice": "0.09",
        "origQty": "100",
        "reduceOnly": True,
        "positionSide": "BOTH",
        "clientAlgoId": "owner-ut-reduce-first-flat",
        "updateTime": ts - 10_000,
    }
    handlers = {
        ("GET", "/fapi/v1/order"): (
            200,
            {"orderId": 1, "avgPrice": "0.07", "executedQty": "100", "status": "FILLED"},
        ),
        ("GET", "/fapi/v3/positionRisk"): (200, []),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, [owner_ut]),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _filters()),  # tickSize 0.00001
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "1"}),
        ("DELETE", "/fapi/v1/algoOrder"): (200, {"code": "200", "msg": "success"}),
    }
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    ao_n = {"n": 0}
    _orig_ao = trade.all_open_orders

    def _ao_once(*, symbol: str | None = None) -> list[dict[str, Any]]:
        ao_n["n"] += 1
        if ao_n["n"] > 1:
            # Old menu helper would catch this and return bare "✅ ปิด SYMBOL".
            raise RuntimeError("post-cancel all_open_orders must not be required for listing")
        return _orig_ao(symbol=symbol)

    trade.all_open_orders = _ao_once  # type: ignore[method-assign]
    sent: list[str] = []

    def _noop_sleep(_seconds: float) -> None:
        return None

    run_exec_recovery(
        conn,
        trade,
        now_ms_value=ts + 1000,
        send=lambda t, **_k: sent.append(str(t)) or True,
        sleep=_noop_sleep,
    )
    row = get_exec_intent(conn, rid)
    assert row is not None and row["status"] == "done"
    owner_deletes = [
        c
        for c in transport.calls
        if c[0] == "DELETE" and c[1] == "/fapi/v1/algoOrder" and str(c[2].get("algoId")) == "55"
    ]
    assert owner_deletes == []
    joined = "\n".join(sent)
    assert "คำสั่งที่ยังค้างบน" in joined
    assert any(ln.strip().startswith("•") for ln in joined.splitlines())
    assert "อ่านคำสั่งค้างไม่ได้" not in joined
    bare = [m for m in sent if m.strip() == "✅ ปิด DOGEUSDT"]
    assert not bare, f"must not notify only bare close, got {sent!r}"
    # tick 0.00001 → 0.09000; fmt_price alone would print 0.09
    assert "BUY 0.09000 ·" in joined, f"expected tick-formatted leftover price, got {joined!r}"
    assert "BUY 0.09 ·" not in joined
    conn.close()


def test_y_leftover_listing_uses_tick() -> None:
    """Unit pin: _append_exit_retained_leftovers formats with tick when provided."""
    tick = Decimal("0.00001")
    raw = fmt_price(Decimal("0.09"))
    tick_s = _fmt_price_tick(Decimal("0.09"), tick)
    assert raw == "0.09" and tick_s == "0.09000" and raw != tick_s
    order = {
        "type": "STOP_MARKET",
        "side": "BUY",
        "stopPrice": "0.09",
        "origQty": "100",
        "reduceOnly": True,
        "positionSide": "BOTH",
        "clientAlgoId": "owner-ut-tick-unit",
    }
    with_tick = _append_exit_retained_leftovers(
        "✅ ปิด DOGEUSDT",
        (order,),
        symbol="DOGEUSDT",
        tick=tick,
    )
    assert "คำสั่งที่ยังค้างบน DOGEUSDT:" in with_tick
    assert f"BUY {tick_s} ·" in with_tick
    assert f"BUY {raw} ·" not in with_tick
    without = _append_exit_retained_leftovers(
        "✅ ปิด DOGEUSDT",
        (order,),
        symbol="DOGEUSDT",
        tick=None,
    )
    assert f"BUY {raw} ·" in without


def test_z_flag_off_flat_close_no_extra_exchange_info(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flag off: flat-close paths must not call GET /fapi/v1/exchangeInfo for leftover tick."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.delenv("JARVISE_ASSIST_CANCEL_OWNER", raising=False)

    def _noop_sleep(_seconds: float) -> None:
        return None

    def _assert_no_exchange_info(transport: RecordingTransport) -> None:
        info = [c for c in transport.calls if c[0] == "GET" and c[1] == "/fapi/v1/exchangeInfo"]
        assert info == [], f"flag-off must not fetch exchangeInfo, got {info!r}"

    # 1) _finish_flat_exit (advice/non-menu path)
    conn = open_db(tmp_path / "z-finish.db")
    ts = now_ms()
    fid = insert_exec_intent(
        conn,
        kind="exit",
        symbol="DOGEUSDT",
        side="short",
        preview={"qty": "100", "entry": "0.08", "position_side": "BOTH"},
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    handlers = {
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, []),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _filters()),
    }
    transport = RecordingTransport(handlers)
    result = _finish_flat_exit(
        _client(transport),
        conn,
        intent_id=fid,
        symbol="DOGEUSDT",
        side="short",
        position_side="BOTH",
        avg=Decimal("0.07"),
        pnl=None,
        include_pnl=False,
    )
    assert result.status == "done"
    _assert_no_exchange_info(transport)
    conn.close()

    # 2) exit first-flat recovery (no exit_cancel_pending)
    conn = open_db(tmp_path / "z-exit-first.db")
    ts = now_ms()
    rid = insert_exec_intent(
        conn,
        kind="exit",
        symbol="DOGEUSDT",
        side="short",
        preview={"qty": "100", "entry": "0.08", "position_side": "BOTH"},
        created_at_ms=ts,
        expires_at_ms=ts + 1,
    )
    finish_exec_intent(conn, rid, status="unknown", result_text="u", expect_status="preview")
    insert_exec_log(conn, intent_id=rid, step="exit_market", endpoint="/fapi/v1/order", created_at_ms=ts)
    handlers = {
        ("GET", "/fapi/v1/order"): (
            200,
            {"orderId": 1, "avgPrice": "0.07", "executedQty": "100", "status": "FILLED"},
        ),
        ("GET", "/fapi/v3/positionRisk"): (200, []),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, []),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _filters()),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "1"}),
    }
    transport = RecordingTransport(handlers)
    run_exec_recovery(
        conn,
        _client(transport),
        now_ms_value=ts + 1000,
        send=lambda *_a, **_k: True,
        sleep=_noop_sleep,
    )
    row = get_exec_intent(conn, rid)
    assert row is not None and row["status"] == "done"
    _assert_no_exchange_info(transport)
    conn.close()

    # 3) exit pending-retry recovery
    conn = open_db(tmp_path / "z-exit-retry.db")
    ts = now_ms()
    rid = insert_exec_intent(
        conn,
        kind="exit",
        symbol="DOGEUSDT",
        side="short",
        preview={"qty": "100", "entry": "0.08", "position_side": "BOTH"},
        created_at_ms=ts,
        expires_at_ms=ts + 1,
    )
    finish_exec_intent(conn, rid, status="unknown", result_text="u", expect_status="preview")
    insert_exec_log(conn, intent_id=rid, step="exit_market", endpoint="/fapi/v1/order", created_at_ms=ts)
    insert_exec_log(
        conn, intent_id=rid, step="exit_cancel_pending", endpoint="/fapi/v1/algoOrder", created_at_ms=ts
    )
    handlers = {
        ("GET", "/fapi/v1/order"): (
            200,
            {"orderId": 1, "avgPrice": "0.07", "executedQty": "100", "status": "FILLED"},
        ),
        ("GET", "/fapi/v3/positionRisk"): (200, []),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, []),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _filters()),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "1"}),
    }
    transport = RecordingTransport(handlers)
    run_exec_recovery(
        conn,
        _client(transport),
        now_ms_value=ts + 1000,
        send=lambda *_a, **_k: True,
        sleep=_noop_sleep,
    )
    row = get_exec_intent(conn, rid)
    assert row is not None and row["status"] == "done"
    _assert_no_exchange_info(transport)
    conn.close()

    # 4) reduce pending-retry recovery
    conn = open_db(tmp_path / "z-reduce-retry.db")
    ts = now_ms()
    rid = insert_exec_intent(
        conn,
        kind="reduce",
        symbol="DOGEUSDT",
        side="short",
        preview={"qty": "100", "entry": "0.08", "close_qty": "100", "leave_qty": "0"},
        created_at_ms=ts,
        expires_at_ms=ts + 1,
    )
    finish_exec_intent(conn, rid, status="unknown", result_text="u", expect_status="preview")
    insert_exec_log(conn, intent_id=rid, step="reduce_market", endpoint="/fapi/v1/order", created_at_ms=ts)
    insert_exec_log(
        conn, intent_id=rid, step="exit_cancel_pending", endpoint="/fapi/v1/algoOrder", created_at_ms=ts
    )
    handlers = {
        ("GET", "/fapi/v1/order"): (
            200,
            {"orderId": 1, "avgPrice": "0.07", "executedQty": "100", "status": "FILLED"},
        ),
        ("GET", "/fapi/v3/positionRisk"): (200, []),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, []),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _filters()),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "1"}),
    }
    transport = RecordingTransport(handlers)
    run_exec_recovery(
        conn,
        _client(transport),
        now_ms_value=ts + 1000,
        send=lambda *_a, **_k: True,
        sleep=_noop_sleep,
    )
    row = get_exec_intent(conn, rid)
    assert row is not None and row["status"] == "done"
    _assert_no_exchange_info(transport)
    conn.close()
