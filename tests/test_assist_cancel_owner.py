"""M1a-2 — cancel leftover owner reduce-only orders after a verified flat."""

from __future__ import annotations

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
    _format_menu_exit_confirm,
    assist_cancel_owner_enabled,
    cancel_jv_orders_for_symbol,
    handle_exec_callback,
    run_exec_recovery,
)
from jarvise_assist.futures import FuturesPosition
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
    assert SCHEMA_VERSION == 38


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
    # Replay: no second market, no second DELETE of the same id.
    n_posts = len([c for c in transport.calls if c[0] == "POST" and c[1] == "/fapi/v1/order"])
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
    assert OWNER_CLOSING_ORDER_NOTE in text or "ลบในแอป" in text
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
    assert "ลบในแอป" in str(row["result_text"]) or "ยกเลิก" in str(row["result_text"])
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

    def __init__(self, *, close_side: str = "BUY") -> None:
        type(self).cancelled_algo = []
        type(self).cancelled_order = []
        self._close_side = close_side.upper()

    @classmethod
    def reset(cls) -> None:
        cls.cancelled_algo = []
        cls.cancelled_order = []

    def position_risk(self, **_k: Any) -> list[Any]:
        return []

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
                "time": 1,
            }
        ]

    def open_algo_orders(self, *, symbol: str | None = None, strict: bool = False) -> list[Any]:
        sym = (symbol or "DOGEUSDT").upper()
        return [
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
                "createTime": 1,
            }
        ]

    def cancel_algo(self, *, symbol: str, algo_id: str | int) -> Any:
        self.cancelled_algo.append(str(algo_id))
        return {"code": "200"}

    def cancel_order(self, *, symbol: str, order_id: str | int) -> Any:
        self.cancelled_order.append(str(order_id))
        return {"orderId": order_id}

    def all_open_orders(self, **_k: Any) -> list[Any]:
        return [*self.open_orders(**_k), *self.open_algo_orders(**_k)]
