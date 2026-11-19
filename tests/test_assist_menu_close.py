"""M0.5 — close open positions from the 📋 menu (live mode, one confirm)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.test_assist import FakeFutures, _ms_bangkok
from tests.test_assist_execute import RecordingTransport, _client, _open_pos, _open_then_flat

from jarvise_assist.execute import (
    MSG_ALREADY_CLOSED,
    MSG_MENU_LIVE_ONLY,
    MSG_MENU_POSITION_CHANGED,
    MSG_MENU_QUICK,
    OWNER_CLOSING_HEADER,
    OWNER_CLOSING_ORDER_NOTE,
    PREVIEW_TTL_MS,
    BinanceTradeClient,
    handle_exec_callback,
)
from jarvise_assist.exit_log import record_intent_action
from jarvise_assist.journal import collect_auto_checks
from jarvise_assist.keyboard import BTN_POSITIONS
from jarvise_assist.listen import process_update
from jarvise_assist.menu import handle_menu_text, render_open_positions, render_open_positions_message
from jarvise_assist.store import (
    claim_exec_intent,
    get_exec_intent,
    insert_exec_intent,
    insert_exec_log,
    insert_watch_advice,
    now_ms,
    upsert_watch_position,
    write_settings,
)
from jarvise_ingest.db import open_db


def _long_pos(symbol: str = "BTCUSDT", qty: str = "0.01") -> Any:
    from jarvise_assist.futures import FuturesPosition

    return FuturesPosition(
        symbol=symbol,
        position_side="BOTH",
        position_amt=Decimal(qty),
        entry_price=Decimal("100"),
        mark_price=Decimal("101"),
        update_time_ms=1,
        liquidation_price=None,
    )


def test_positions_markup_live_skips_quick_and_respects_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / "markup.db")
    conn.execute(
        """
        INSERT INTO assist_quick_registration (
            id, symbol, side, planned_entry, planned_stop, qty, status,
            created_at_ms, expires_at_ms, updated_at_ms
        ) VALUES ('q1', 'DOGEUSDT', 'long', 1, 0.9, 1, 'open', 1, 999999999, 1)
        """
    )
    conn.commit()
    positions = [
        _long_pos("BTCUSDT"),
        _long_pos("DOGEUSDT"),
        _long_pos("1000PEPEUSDT", "1000"),
    ]
    fake = FakeFutures(positions=positions, orders=[])
    _body, markup = render_open_positions_message(conn, futures=fake, now_ms=1)
    assert markup is not None
    buttons = [b for row in markup["inline_keyboard"] for b in row]
    cbs = [str(b["callback_data"]) for b in buttons]
    assert "xc:BTCUSDT:l" in cbs
    assert "xc:1000PEPEUSDT:l" in cbs
    assert all("DOGE" not in cb for cb in cbs)
    assert all(len(cb) <= 64 for cb in cbs)
    assert all(str(b["text"]).startswith("ปิดไม้ ") for b in buttons)
    conn.close()


@pytest.mark.parametrize("mode", ["off", "testnet"])
def test_positions_no_markup_when_not_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", mode)
    conn = open_db(tmp_path / f"nomark-{mode}.db")
    fake = FakeFutures(positions=[_long_pos()], orders=[])
    text_only = render_open_positions(conn, futures=fake, now_ms=1)
    _body, markup = render_open_positions_message(conn, futures=fake, now_ms=1)
    assert markup is None
    assert text_only == _body
    conn.close()


def test_xc_callback_creates_confirm_card(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / "xc.db")
    write_settings(conn, enabled=True)
    pos = _long_pos()
    fake = FakeFutures(positions=[pos], orders=[])
    sent: list[dict[str, Any]] = []

    def send(text: str, **kw: Any) -> int:
        sent.append({"text": text, **kw})
        return len(sent)

    out = handle_exec_callback(
        conn,
        "xc:BTCUSDT:l",
        send=send,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        read_futures=fake,
        now_ms_value=1,
    )
    assert out.get("preview")
    assert len(sent) == 1
    assert "ยืนยันปิดไม้" in sent[0]["text"]
    markup = sent[0].get("reply_markup") or {}
    buttons = [b for row in markup.get("inline_keyboard", []) for b in row]
    assert any(b.get("callback_data", "").startswith("xx:") for b in buttons)
    assert any(b.get("callback_data", "").startswith("en:") for b in buttons)
    assert "💰 เงินจริง" in sent[0]["text"]
    conn.close()


@pytest.mark.parametrize(
    ("data", "env", "reason_field"),
    [
        ("xc:BTCUSDT:l", "off", MSG_MENU_LIVE_ONLY),
        ("xc:BTCUSDT:l", "testnet", MSG_MENU_LIVE_ONLY),
    ],
)
def test_xc_refuses_when_not_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data: str, env: str, reason_field: str
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", env)
    conn = open_db(tmp_path / "refuse.db")
    sent: list[str] = []
    handle_exec_callback(
        conn,
        data,
        send=lambda t, **_k: sent.append(t) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        read_futures=FakeFutures(positions=[_long_pos()], orders=[]),
        now_ms_value=1,
    )
    assert sent == [reason_field]
    conn.close()


def test_xc_refusals_position_and_quick(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / "refuse2.db")
    sent: list[str] = []

    handle_exec_callback(
        conn,
        "xc:BTCUSDT:l",
        send=lambda t, **_k: sent.append(t) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        read_futures=FakeFutures(positions=[], orders=[]),
        now_ms_value=1,
    )
    assert sent[-1] == MSG_ALREADY_CLOSED

    sent.clear()
    from jarvise_assist.futures import FuturesPosition

    short = FuturesPosition(
        symbol="BTCUSDT",
        position_side="BOTH",
        position_amt=Decimal("-0.01"),
        entry_price=Decimal("100"),
        mark_price=Decimal("101"),
        update_time_ms=1,
        liquidation_price=None,
    )
    handle_exec_callback(
        conn,
        "xc:BTCUSDT:l",
        send=lambda t, **_k: sent.append(t) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        read_futures=FakeFutures(positions=[short], orders=[]),
        now_ms_value=2,
    )
    assert sent[-1] == MSG_MENU_POSITION_CHANGED

    conn.execute(
        """
        INSERT INTO assist_quick_registration (
            id, symbol, side, planned_entry, planned_stop, qty, status,
            created_at_ms, expires_at_ms, updated_at_ms
        ) VALUES ('q1', 'BTCUSDT', 'long', 1, 0.9, 1, 'open', 1, 999999999, 1)
        """
    )
    conn.commit()
    sent.clear()
    handle_exec_callback(
        conn,
        "xc:BTCUSDT:l",
        send=lambda t, **_k: sent.append(t) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        read_futures=FakeFutures(positions=[_long_pos()], orders=[]),
        now_ms_value=3,
    )
    assert sent[-1] == MSG_MENU_QUICK
    conn.close()


def test_process_update_routes_xc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "99")
    conn = open_db(tmp_path / "route.db")
    write_settings(conn, enabled=True)
    fake = FakeFutures(positions=[_long_pos()], orders=[])
    sent: list[str] = []

    def send(text: str, **_: Any) -> int:
        sent.append(text)
        return 1

    upd = {
        "callback_query": {
            "id": "1",
            "from": {"id": 1},
            "message": {"message_id": 7, "chat": {"id": 99}, "text": "x"},
            "data": "xc:BTCUSDT:l",
        }
    }
    process_update(conn, upd, send=send, futures=fake, now_ms_value=1)
    assert any("ยืนยันปิดไม้" in t for t in sent)
    conn.close()


def test_menu_xx_closes_once_and_replays(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setattr(
        BinanceTradeClient,
        "assert_trade_key_safe",
        lambda self, **_: {"ok": True},
    )
    monkeypatch.setattr(BinanceTradeClient, "assert_account_modes", lambda self: None)
    conn = open_db(tmp_path / "xx.db")
    ts = now_ms()
    intent_id = insert_exec_intent(
        conn,
        kind="exit",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "qty": "100",
            "entry": "0.08",
            "source": "menu",
            "confirm_text": "confirm",
        },
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/positionSide/dual"): (200, {"dualSidePosition": False}),
        ("GET", "/fapi/v1/multiAssetsMargin"): (200, {"multiAssetsMargin": False}),
        ("GET", "/fapi/v3/positionRisk"): _open_then_flat([_open_pos()]),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/userTrades"): (200, [{"realizedPnl": "0.9"}]),
        ("POST", "/fapi/v1/order"): (
            200,
            {"orderId": 9, "avgPrice": "0.07", "executedQty": "100", "status": "FILLED"},
        ),
        ("GET", "/fapi/v1/order"): (
            200,
            {"orderId": 9, "avgPrice": "0.07", "executedQty": "100", "status": "FILLED"},
        ),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, []),
    }
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    sent: list[str] = []
    edits: list[Any] = []

    def send(t: str, **_: Any) -> bool:
        sent.append(t)
        return True

    def edit(mid: int, text: str, **kw: Any) -> None:
        edits.append((mid, text, kw))

    handle_exec_callback(
        conn,
        f"xx:{intent_id}",
        send=send,
        answer=lambda *_a, **_k: None,
        edit=edit,
        trade=trade,
        now_ms_value=ts,
        message={"message_id": 1},
    )
    posts = [c for c in transport.calls if c[0] == "POST" and c[1] == "/fapi/v1/order"]
    assert len(posts) == 1
    handle_exec_callback(
        conn,
        f"xx:{intent_id}",
        send=send,
        answer=lambda *_a, **_k: None,
        edit=edit,
        trade=trade,
        now_ms_value=ts + 1,
        message={"message_id": 1},
    )
    posts_after = [c for c in transport.calls if c[0] == "POST" and c[1] == "/fapi/v1/order"]
    assert len(posts_after) == 1
    conn.close()


def test_watch_xx_still_skips_expiry_for_preview(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setattr(
        BinanceTradeClient,
        "assert_trade_key_safe",
        lambda self, **_: {"ok": True},
    )
    monkeypatch.setattr(BinanceTradeClient, "assert_account_modes", lambda self: None)
    conn = open_db(tmp_path / "watch-xx.db")
    write_settings(conn, enabled=True)
    ts = 1
    expired_id = insert_exec_intent(
        conn,
        kind="exit",
        symbol="DOGEUSDT",
        side="short",
        preview={"qty": "100", "entry": "0.08"},
        created_at_ms=ts,
        expires_at_ms=ts - 1,
    )
    handlers = {
        ("GET", "/fapi/v1/positionSide/dual"): (200, {"dualSidePosition": False}),
        ("GET", "/fapi/v1/multiAssetsMargin"): (200, {"multiAssetsMargin": False}),
        ("GET", "/fapi/v3/positionRisk"): _open_then_flat([_open_pos()]),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/userTrades"): (200, [{"realizedPnl": "0.9"}]),
        ("POST", "/fapi/v1/order"): (
            200,
            {"orderId": 9, "avgPrice": "0.07", "executedQty": "100", "status": "FILLED"},
        ),
        ("GET", "/fapi/v1/order"): (
            200,
            {"orderId": 9, "avgPrice": "0.07", "executedQty": "100", "status": "FILLED"},
        ),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, []),
    }
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    handle_exec_callback(
        conn,
        f"xx:{expired_id}",
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=trade,
        now_ms_value=ts + PREVIEW_TTL_MS + 1000,
    )
    posts = [c for c in transport.calls if c[0] == "POST"]
    assert posts
    conn.close()


def test_menu_exit_telemetry_skipped(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "tel.db")
    ts = 1
    intent = {
        "id": "aei_x",
        "kind": "exit",
        "symbol": "BTCUSDT",
        "side": "long",
        "preview": {"source": "menu"},
        "created_at_ms": ts,
    }
    record_intent_action(conn, intent, action="tap", action_at_ms=ts + 100)
    row = conn.execute("SELECT 1 FROM assist_exit_response LIMIT 1").fetchone()
    assert row is None
    conn.close()


def _menu_close_journal(
    conn: Any,
    *,
    entry_order_id: str,
    entry_ms: int,
    exit_ms: int,
    advice_at_ms: int,
    preview_extra: dict[str, Any],
) -> dict[str, Any]:
    upsert_watch_position(
        conn,
        {
            "symbol": "BTCUSDT",
            "position_side": "BOTH",
            "side": "long",
            "entry": 100.0,
            "qty": 1.0,
            "in_universe": 1,
            "signal_id": "sig1",
            "initial_stop": 90.0,
            "initial_stop_source": "signal",
            "entry_time_ms": entry_ms,
            "no_stop_latched": 0,
            "last_seen_ms": entry_ms,
            "closed_at_ms": None,
        },
    )
    insert_watch_advice(
        conn,
        symbol="BTCUSDT",
        position_side="BOTH",
        kind="exit",
        reason="flat",
        level=None,
        approx_r=0.5,
        status="sent",
        created_at_ms=advice_at_ms,
    )
    conn.execute(
        """
        INSERT INTO assist_real_trade (
            symbol, side, entry_order_id, status, updated_at_ms, entry_time_ms, entry_qty,
            entry_fill_avg, exit_time_ms, exit_kind
        ) VALUES ('BTCUSDT', 'long', ?, 'closed', ?, ?, 1, 100, ?, 'owner_exit')
        """,
        (entry_order_id, exit_ms, entry_ms, exit_ms),
    )
    intent_id = insert_exec_intent(
        conn,
        kind="exit",
        symbol="BTCUSDT",
        side="long",
        preview={
            "source": "menu",
            "qty": "1",
            "entry": "100",
            "position_side": "BOTH",
            **preview_extra,
        },
        created_at_ms=exit_ms - 500,
        expires_at_ms=exit_ms + 120_000,
    )
    insert_exec_log(
        conn,
        intent_id=intent_id,
        step="exit_market",
        endpoint="/fapi/v1/order",
        order_or_algo_id="99",
        created_at_ms=exit_ms - 100,
    )
    conn.commit()
    return dict(
        conn.execute("SELECT * FROM assist_real_trade WHERE entry_order_id=?", (entry_order_id,)).fetchone()
    )


def test_journal_menu_source_follows_advice(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "journal-advice.db")
    entry_ms = _ms_bangkok(2026, 11, 20, 12, 0)
    exit_ms = entry_ms + 60_000
    trade = _menu_close_journal(
        conn,
        entry_order_id="m1",
        entry_ms=entry_ms,
        exit_ms=exit_ms,
        advice_at_ms=entry_ms + 1_000,
        preview_extra={"exit_advice_active": True},
    )
    checks = collect_auto_checks(conn, trade)
    assert checks.get("exit_source") == "menu"
    assert checks.get("exit_followed_plan") is True
    conn.close()


def test_journal_menu_source_ignores_stale_advice(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "journal-stale.db")
    entry_ms = _ms_bangkok(2026, 11, 20, 12, 0)
    exit_ms = entry_ms + 60_000
    trade = _menu_close_journal(
        conn,
        entry_order_id="m2",
        entry_ms=entry_ms,
        exit_ms=exit_ms,
        advice_at_ms=entry_ms - 60_000,
        preview_extra={"exit_advice_active": False},
    )
    checks = collect_auto_checks(conn, trade)
    assert checks.get("exit_source") == "menu"
    assert checks.get("exit_followed_plan") is False
    conn.close()


def test_handle_menu_positions_sends_markup_only_in_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = open_db(tmp_path / "menu-send.db")
    fake = FakeFutures(positions=[_long_pos()], orders=[])
    captured: list[dict[str, Any]] = []

    def send(text: str, **kw: Any) -> bool:
        captured.append({"text": text, **kw})
        return True

    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    handle_menu_text(conn, BTN_POSITIONS, futures=fake, now_ms=1, send=send)
    assert captured[0].get("reply_markup") is not None

    captured.clear()
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "off")
    handle_menu_text(conn, BTN_POSITIONS, futures=fake, now_ms=1, send=send)
    assert captured[0].get("reply_markup") is None
    conn.close()


def test_xc_lists_owner_limit_and_algo_orders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / "owner-orders.db")
    pos = _long_pos()
    owner_orders = [
        {
            "symbol": "BTCUSDT",
            "type": "LIMIT",
            "side": "SELL",
            "price": "120",
            "origQty": "0.01",
            "reduceOnly": True,
            "positionSide": "BOTH",
        },
        {
            "symbol": "BTCUSDT",
            "type": "STOP_MARKET",
            "side": "SELL",
            "stopPrice": "90",
            "closePosition": True,
            "positionSide": "BOTH",
        },
        {
            "symbol": "BTCUSDT",
            "type": "STOP_MARKET",
            "side": "SELL",
            "stopPrice": "88",
            "reduceOnly": True,
            "positionSide": "BOTH",
            "clientAlgoId": "jv-stop1",
        },
    ]
    fake = FakeFutures(positions=[pos], orders=owner_orders)
    sent: list[str] = []
    handle_exec_callback(
        conn,
        "xc:BTCUSDT:l",
        send=lambda t, **_k: sent.append(t) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        read_futures=fake,
        now_ms_value=1,
    )
    body = sent[0]
    assert OWNER_CLOSING_HEADER in body
    assert "LIMIT SELL 120" in body
    assert "STOP_MARKET SELL 90" in body
    assert OWNER_CLOSING_ORDER_NOTE in body
    assert "jv-stop1" not in body
    conn.close()


def test_menu_xx_expired_sends_no_post(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / "xx-exp.db")
    ts = 1
    intent_id = insert_exec_intent(
        conn,
        kind="exit",
        symbol="DOGEUSDT",
        side="short",
        preview={"source": "menu", "qty": "100", "entry": "0.08", "confirm_text": "confirm"},
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    transport = RecordingTransport({})
    trade = _client(transport)
    edits: list[Any] = []
    handle_exec_callback(
        conn,
        f"xx:{intent_id}",
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda mid, text, **kw: edits.append((mid, text, kw)),
        trade=trade,
        now_ms_value=ts + PREVIEW_TTL_MS + 1,
        message={"message_id": 9},
    )
    assert not [c for c in transport.calls if c[0] == "POST"]
    assert edits and edits[0][2].get("reply_markup") is not None
    assert "🔄 อัปเดต" in str(edits[0][2]["reply_markup"])
    conn.close()


def test_menu_in_flight_refusal_messages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / "inflight.db")
    ts = now_ms()
    other = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview={},
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    claim_exec_intent(conn, other)
    sent: list[str] = []
    handle_exec_callback(
        conn,
        "xc:BTCUSDT:l",
        send=lambda t, **_k: sent.append(t) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        read_futures=FakeFutures(positions=[_long_pos()], orders=[]),
        now_ms_value=ts,
    )
    assert "ยังไม่ปิดไม้" in sent[-1]

    menu_id = insert_exec_intent(
        conn,
        kind="exit",
        symbol="BTCUSDT",
        side="long",
        preview={"source": "menu", "qty": "0.01", "entry": "100"},
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    sent.clear()
    handle_exec_callback(
        conn,
        f"xx:{menu_id}",
        send=lambda t, **_k: sent.append(t) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(RecordingTransport({})),
        now_ms_value=ts,
    )
    assert "ยังไม่ปิดไม้" in sent[-1]
    conn.close()


def test_menu_xx_qty_mismatch_refresh_button(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setattr(BinanceTradeClient, "assert_trade_key_safe", lambda self, **_: {"ok": True})
    monkeypatch.setattr(BinanceTradeClient, "assert_account_modes", lambda self: None)
    conn = open_db(tmp_path / "qty-change.db")
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
    changed_pos = _open_pos(amt="-50", entry="0.08")
    transport = RecordingTransport(
        {
            ("GET", "/fapi/v1/positionSide/dual"): (200, {"dualSidePosition": False}),
            ("GET", "/fapi/v1/multiAssetsMargin"): (200, {"multiAssetsMargin": False}),
            ("GET", "/fapi/v3/positionRisk"): (200, [changed_pos]),
        }
    )
    markups: list[Any] = []

    def send(_t: str, **kw: Any) -> bool:
        markups.append(kw.get("reply_markup"))
        return True

    handle_exec_callback(
        conn,
        f"xx:{intent_id}",
        send=send,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(transport),
        now_ms_value=ts,
        message={"message_id": 1},
    )
    assert not [c for c in transport.calls if c[0] == "POST"]
    assert markups and markups[0] is not None
    conn.close()


def test_menu_en_cancels_and_strips_buttons(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / "en.db")
    ts = now_ms()
    intent_id = insert_exec_intent(
        conn,
        kind="exit",
        symbol="BTCUSDT",
        side="long",
        preview={"source": "menu", "confirm_text": "hold card"},
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    edits: list[Any] = []
    handle_exec_callback(
        conn,
        f"en:{intent_id}",
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda mid, text, **kw: edits.append((mid, text, kw)),
        now_ms_value=ts,
        message={"message_id": 3},
    )
    assert edits[0][2]["reply_markup"] == {"inline_keyboard": []}
    conn.close()


def test_menu_close_under_kill_switch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": True, "known": True},
    )
    monkeypatch.setattr(BinanceTradeClient, "assert_trade_key_safe", lambda self, **_: {"ok": True})
    monkeypatch.setattr(BinanceTradeClient, "assert_account_modes", lambda self: None)
    conn = open_db(tmp_path / "kill.db")
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
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/positionSide/dual"): (200, {"dualSidePosition": False}),
        ("GET", "/fapi/v1/multiAssetsMargin"): (200, {"multiAssetsMargin": False}),
        ("GET", "/fapi/v3/positionRisk"): _open_then_flat([_open_pos()]),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/userTrades"): (200, [{"realizedPnl": "0.9"}]),
        ("POST", "/fapi/v1/order"): (
            200,
            {"orderId": 9, "avgPrice": "0.07", "executedQty": "100", "status": "FILLED"},
        ),
        ("GET", "/fapi/v1/order"): (
            200,
            {"orderId": 9, "avgPrice": "0.07", "executedQty": "100", "status": "FILLED"},
        ),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, []),
    }
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
    posts = [c for c in transport.calls if c[0] == "POST"]
    assert len(posts) == 1
    conn.close()


def test_menu_xx_off_and_quick_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = open_db(tmp_path / "xx-refuse.db")
    ts = now_ms()
    intent_id = insert_exec_intent(
        conn,
        kind="exit",
        symbol="DOGEUSDT",
        side="short",
        preview={"source": "menu", "qty": "1", "entry": "1"},
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    sent: list[str] = []
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "off")
    handle_exec_callback(
        conn,
        f"xx:{intent_id}",
        send=lambda t, **_k: sent.append(t) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(RecordingTransport({})),
        now_ms_value=ts,
    )
    assert sent[-1] == MSG_MENU_LIVE_ONLY

    conn.execute(
        """
        INSERT INTO assist_quick_registration (
            id, symbol, side, planned_entry, planned_stop, qty, status,
            created_at_ms, expires_at_ms, updated_at_ms
        ) VALUES ('q9', 'DOGEUSDT', 'short', 1, 2, 1, 'open', 1, 9, 1)
        """
    )
    conn.commit()
    sent.clear()
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    handle_exec_callback(
        conn,
        f"xx:{intent_id}",
        send=lambda t, **_k: sent.append(t) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(RecordingTransport({})),
        now_ms_value=ts,
    )
    assert sent[-1] == MSG_MENU_QUICK
    conn.close()


def _menu_close_handlers(*, open_orders: Any) -> dict[tuple[str, str], Any]:
    return {
        ("GET", "/fapi/v1/positionSide/dual"): (200, {"dualSidePosition": False}),
        ("GET", "/fapi/v1/multiAssetsMargin"): (200, {"multiAssetsMargin": False}),
        ("GET", "/fapi/v3/positionRisk"): _open_then_flat([_open_pos()]),
        ("GET", "/fapi/v1/openOrders"): open_orders,
        ("GET", "/fapi/v1/userTrades"): (200, [{"realizedPnl": "0.9"}]),
        ("POST", "/fapi/v1/order"): (
            200,
            {"orderId": 9, "avgPrice": "0.07", "executedQty": "100", "status": "FILLED"},
        ),
        ("GET", "/fapi/v1/order"): (
            200,
            {"orderId": 9, "avgPrice": "0.07", "executedQty": "100", "status": "FILLED"},
        ),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, []),
    }


def test_menu_xx_close_appends_owner_limit_and_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setattr(BinanceTradeClient, "assert_trade_key_safe", lambda self, **_: {"ok": True})
    monkeypatch.setattr(BinanceTradeClient, "assert_account_modes", lambda self: None)
    conn = open_db(tmp_path / "xx-owner.db")
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
        "type": "LIMIT",
        "side": "BUY",
        "price": "0.075",
        "origQty": "100",
        "reduceOnly": True,
        "positionSide": "BOTH",
    }
    handlers = _menu_close_handlers(open_orders=(200, [owner_limit]))
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    sent: list[str] = []

    handle_exec_callback(
        conn,
        f"xx:{intent_id}",
        send=lambda t, **_k: sent.append(t) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=trade,
        now_ms_value=ts,
        message={"message_id": 1},
    )
    row = get_exec_intent(conn, intent_id)
    assert row is not None
    assert row["status"] == "done"
    result_text = str(row["result_text"])
    assert "✅" in result_text
    assert "LIMIT BUY 0.075" in result_text
    assert OWNER_CLOSING_ORDER_NOTE in result_text
    assert sent[-1] == result_text

    handle_exec_callback(
        conn,
        f"xx:{intent_id}",
        send=lambda t, **_k: sent.append(t) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=trade,
        now_ms_value=ts + 1,
        message={"message_id": 1},
    )
    posts = [c for c in transport.calls if c[0] == "POST"]
    assert len(posts) == 1
    assert sent[-1] == result_text
    conn.close()


def test_menu_xx_close_plain_when_owner_append_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setattr(BinanceTradeClient, "assert_trade_key_safe", lambda self, **_: {"ok": True})
    monkeypatch.setattr(BinanceTradeClient, "assert_account_modes", lambda self: None)
    conn = open_db(tmp_path / "xx-plain.db")
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

    counts = {"open": 0}

    def open_orders(_params: dict[str, Any]) -> tuple[int, list[Any]]:
        counts["open"] += 1
        if counts["open"] < 3:
            return (200, [])
        raise RuntimeError("openOrders failed")

    transport = RecordingTransport(_menu_close_handlers(open_orders=open_orders))
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
    assert row["status"] == "done"
    assert str(row["result_text"]).startswith("✅ ปิด")
    assert OWNER_CLOSING_ORDER_NOTE not in str(row["result_text"])
    assert len([c for c in transport.calls if c[0] == "POST"]) == 1
    conn.close()


def test_menu_xx_close_plain_when_owner_line_malformed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setattr(BinanceTradeClient, "assert_trade_key_safe", lambda self, **_: {"ok": True})
    monkeypatch.setattr(BinanceTradeClient, "assert_account_modes", lambda self: None)
    conn = open_db(tmp_path / "xx-badqty.db")
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
    bad = {
        "symbol": "DOGEUSDT",
        "type": "LIMIT",
        "side": "BUY",
        "price": "0.075",
        "origQty": "not-a-qty",
        "reduceOnly": True,
        "positionSide": "BOTH",
    }
    counts = {"open": 0}

    def open_orders(_params: dict[str, Any]) -> tuple[int, list[Any]]:
        counts["open"] += 1
        if counts["open"] < 3:
            return (200, [])
        return (200, [bad])

    transport = RecordingTransport(_menu_close_handlers(open_orders=open_orders))
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
    assert row["status"] == "done"
    assert str(row["result_text"]).startswith("✅ ปิด")
    assert OWNER_CLOSING_ORDER_NOTE not in str(row["result_text"])
    assert len([c for c in transport.calls if c[0] == "POST"]) == 1
    conn.close()


@pytest.mark.parametrize("advice_ms,expect_active", [(999_000, False), (1_001_000, True)])
def test_xc_exit_advice_active_and_r_at_tap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    advice_ms: int,
    expect_active: bool,
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / f"xc-advice-{advice_ms}.db")
    entry_ms = 1_000_000
    upsert_watch_position(
        conn,
        {
            "symbol": "BTCUSDT",
            "position_side": "BOTH",
            "side": "long",
            "entry": 100.0,
            "qty": 0.01,
            "in_universe": 1,
            "signal_id": "s1",
            "initial_stop": 90.0,
            "initial_stop_source": "signal",
            "entry_time_ms": entry_ms,
            "no_stop_latched": 0,
            "last_seen_ms": entry_ms,
            "closed_at_ms": None,
        },
    )
    insert_watch_advice(
        conn,
        symbol="BTCUSDT",
        position_side="BOTH",
        kind="exit",
        reason="flat",
        level=None,
        approx_r=0.5,
        status="sent",
        created_at_ms=advice_ms,
    )
    out = handle_exec_callback(
        conn,
        "xc:BTCUSDT:l",
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        read_futures=FakeFutures(positions=[_long_pos()], orders=[]),
        now_ms_value=entry_ms + 5_000,
    )
    intent_id = str(out["preview"])
    row = get_exec_intent(conn, intent_id)
    assert row is not None
    preview = json.loads(str(row["preview_json"]))
    assert preview.get("exit_advice_active") is expect_active
    r_at = preview.get("r_at_tap")
    assert r_at is None or isinstance(r_at, float)
    if expect_active:
        assert r_at == pytest.approx(0.1)
    conn.close()


def test_menu_xx_testnet_refuses_no_post(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "xx-testnet.db")
    ts = now_ms()
    intent_id = insert_exec_intent(
        conn,
        kind="exit",
        symbol="DOGEUSDT",
        side="short",
        preview={"source": "menu", "qty": "1", "entry": "1"},
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    transport = RecordingTransport({})
    sent: list[str] = []
    handle_exec_callback(
        conn,
        f"xx:{intent_id}",
        send=lambda t, **_k: sent.append(t) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(transport),
        now_ms_value=ts,
    )
    assert sent == [MSG_MENU_LIVE_ONLY]
    assert not [c for c in transport.calls if c[0] == "POST"]
    conn.close()


def test_menu_xx_wrong_side_stores_menu_wording(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setattr(BinanceTradeClient, "assert_trade_key_safe", lambda self, **_: {"ok": True})
    monkeypatch.setattr(BinanceTradeClient, "assert_account_modes", lambda self: None)
    conn = open_db(tmp_path / "xx-wrong.db")
    ts = now_ms()
    intent_id = insert_exec_intent(
        conn,
        kind="exit",
        symbol="BTCUSDT",
        side="long",
        preview={"source": "menu", "qty": "0.01", "entry": "100", "confirm_text": "c"},
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    short_row = {
        "symbol": "BTCUSDT",
        "positionSide": "BOTH",
        "positionAmt": "-0.01",
        "entryPrice": "100",
        "markPrice": "101",
    }
    transport = RecordingTransport(
        {
            ("GET", "/fapi/v1/positionSide/dual"): (200, {"dualSidePosition": False}),
            ("GET", "/fapi/v1/multiAssetsMargin"): (200, {"multiAssetsMargin": False}),
            ("GET", "/fapi/v3/positionRisk"): (200, [short_row]),
        }
    )
    markups: list[Any] = []
    handle_exec_callback(
        conn,
        f"xx:{intent_id}",
        send=lambda _t, **kw: (markups.append(kw.get("reply_markup")), True)[1],
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(transport),
        now_ms_value=ts,
        message={"message_id": 1},
    )
    row = get_exec_intent(conn, intent_id)
    assert row is not None
    assert str(row["result_text"]) == MSG_MENU_POSITION_CHANGED
    assert markups and "🔄 อัปเดต" in str(markups[-1])
    conn.close()
