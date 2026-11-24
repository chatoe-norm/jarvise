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
from jarvise_assist.menu import (
    compute_position_unrealized,
    handle_menu_text,
    render_open_positions,
    render_open_positions_message,
)
from jarvise_assist.quick import _send_close_notice
from jarvise_assist.quick_card import quick_mode_tag
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


def _insert_quick_reg(
    conn: Any,
    *,
    reg_id: str,
    symbol: str,
    side: str = "long",
    planned_stop: float = 90.0,
    planned_entry: float = 100.0,
) -> None:
    conn.execute(
        """
        INSERT INTO assist_quick_registration (
            id, symbol, side, planned_entry, planned_stop, qty, status,
            created_at_ms, expires_at_ms, updated_at_ms
        ) VALUES (?, ?, ?, ?, ?, 1, 'open', 1, 999999999, 1)
        """,
        (reg_id, symbol, side, planned_entry, planned_stop),
    )
    conn.commit()


def _insert_quick_trade(
    conn: Any,
    *,
    trade_id: str,
    symbol: str,
    side: str = "long",
    planned_stop: float = 90.0,
    entry_time_ms: int = 1,
    status: str = "open",
    registration_id: str | None = None,
) -> None:
    rid = registration_id or f"r-{trade_id}"
    conn.execute(
        """
        INSERT INTO assist_quick_trade (
            id, registration_id, symbol, side, status,
            entry_avg, entry_qty, risk_usdt, entry_time_ms, updated_at_ms, planned_stop
        ) VALUES (?, ?, ?, ?, ?, 100, 1, 10, ?, 1, ?)
        """,
        (trade_id, rid, symbol, side, status, entry_time_ms, planned_stop),
    )
    conn.commit()


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


def test_positions_markup_live_includes_quick_and_respects_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / "markup.db")
    _insert_quick_reg(conn, reg_id="q1", symbol="DOGEUSDT", planned_stop=0.9, planned_entry=1)
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
    assert "xc:DOGEUSDT:l" in cbs
    assert "xc:1000PEPEUSDT:l" in cbs
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


def test_xc_refusals_position_changed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    conn.close()


def test_xc_quick_builds_confirm_with_registered_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / "xc-quick.db")
    write_settings(conn, enabled=True)
    _insert_quick_reg(conn, reg_id="q1", symbol="BTCUSDT", planned_stop=90.0, planned_entry=100.0)
    owner_stop = {
        "symbol": "BTCUSDT",
        "type": "STOP_MARKET",
        "side": "SELL",
        "stopPrice": "95",
        "origQty": "0.01",
        "reduceOnly": True,
        "positionSide": "BOTH",
        "clientOrderId": "manual-sl-1",
    }
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
        read_futures=FakeFutures(positions=[_long_pos()], orders=[owner_stop]),
        now_ms_value=3,
    )
    assert out.get("preview")
    text = sent[-1]["text"]
    assert "ยืนยันปิดไม้" in text
    assert "⚡ ไม้ quick · stop ที่ลงทะเบียน" in text
    assert "จาก stop ที่ลงทะเบียน" in text
    assert OWNER_CLOSING_HEADER in text
    assert OWNER_CLOSING_ORDER_NOTE in text
    intent = get_exec_intent(conn, str(out["preview"]))
    assert intent is not None
    preview = intent["preview"]
    assert preview.get("source") == "menu"
    assert preview.get("cohort") == "quick"
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


def test_menu_xx_off_still_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    conn.close()


def test_menu_xx_closes_quick_full_qty_and_lists_owner_leftovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setattr(BinanceTradeClient, "assert_trade_key_safe", lambda self, **_: {"ok": True})
    monkeypatch.setattr(BinanceTradeClient, "assert_account_modes", lambda self: None)
    conn = open_db(tmp_path / "xx-quick.db")
    _insert_quick_reg(conn, reg_id="q9", symbol="DOGEUSDT", side="short", planned_stop=2.0, planned_entry=1.0)
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
    owner_limit = {
        "symbol": "DOGEUSDT",
        "type": "LIMIT",
        "side": "BUY",
        "price": "0.075",
        "origQty": "100",
        "reduceOnly": True,
        "positionSide": "BOTH",
        "clientOrderId": "owner-tp-1",
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
    posts = [c for c in transport.calls if c[0] == "POST" and c[1] == "/fapi/v1/order"]
    assert len(posts) == 1
    body = posts[0][2]
    assert body.get("reduceOnly") in {True, "true", "True"}
    assert str(body.get("quantity")) == "100"
    cancels = [c for c in transport.calls if c[0] == "DELETE"]
    assert cancels == []
    row = get_exec_intent(conn, intent_id)
    assert row is not None and row["status"] == "done"
    assert "LIMIT BUY 0.075" in str(row["result_text"])
    assert OWNER_CLOSING_ORDER_NOTE in str(row["result_text"])

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
    assert len([c for c in transport.calls if c[0] == "POST" and c[1] == "/fapi/v1/order"]) == 1
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


@pytest.mark.parametrize(
    ("side", "amt", "entry", "mark", "stop", "expect_r"),
    [
        ("long", "1", "100", "110", 90.0, 1.0),
        ("short", "-1", "100", "90", 110.0, 1.0),
    ],
)
def test_quick_r_from_open_registration(
    tmp_path: Path,
    side: str,
    amt: str,
    entry: str,
    mark: str,
    stop: float,
    expect_r: float,
) -> None:
    from jarvise_assist.futures import FuturesPosition

    conn = open_db(tmp_path / f"qr-reg-{side}.db")
    _insert_quick_reg(
        conn,
        reg_id="qr1",
        symbol="BTCUSDT",
        side=side,
        planned_stop=stop,
        planned_entry=float(entry),
    )
    pos = FuturesPosition(
        symbol="BTCUSDT",
        position_side="BOTH",
        position_amt=Decimal(amt),
        entry_price=Decimal(entry),
        mark_price=Decimal(mark),
        update_time_ms=1,
        liquidation_price=None,
    )
    line, r_mult, risk = compute_position_unrealized(conn, pos=pos, orders=[], now_ms=1, tick=None)
    assert risk == pytest.approx(abs(float(entry) - stop))
    assert r_mult == pytest.approx(expect_r)
    assert "จาก stop ที่ลงทะเบียน" in line
    conn.close()


@pytest.mark.parametrize(
    ("side", "amt", "entry", "mark", "stop", "expect_r"),
    [
        ("long", "1", "100", "105", 90.0, 0.5),
        ("short", "-1", "100", "95", 110.0, 0.5),
    ],
)
def test_quick_r_from_tagged_trade(
    tmp_path: Path,
    side: str,
    amt: str,
    entry: str,
    mark: str,
    stop: float,
    expect_r: float,
) -> None:
    from jarvise_assist.futures import FuturesPosition

    conn = open_db(tmp_path / f"qr-trade-{side}.db")
    _insert_quick_trade(
        conn,
        trade_id="qt1",
        symbol="BTCUSDT",
        side=side,
        planned_stop=stop,
        entry_time_ms=1,
    )
    pos = FuturesPosition(
        symbol="BTCUSDT",
        position_side="BOTH",
        position_amt=Decimal(amt),
        entry_price=Decimal(entry),
        mark_price=Decimal(mark),
        update_time_ms=1,
        liquidation_price=None,
    )
    line, r_mult, _risk = compute_position_unrealized(conn, pos=pos, orders=[], now_ms=1, tick=None)
    assert r_mult == pytest.approx(expect_r)
    assert "จาก stop ที่ลงทะเบียน" in line
    conn.close()


def test_outside_universe_quick_shows_registered_r_not_no_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / "out-quick.db")
    _insert_quick_reg(
        conn,
        reg_id="q-pepe",
        symbol="1000PEPEUSDT",
        planned_stop=90.0,
        planned_entry=100.0,
    )
    pos = _long_pos("1000PEPEUSDT", "1000")
    # entry 100 mark 101 → risk 10 → +0.1R
    body, markup = render_open_positions_message(
        conn, futures=FakeFutures(positions=[pos], orders=[]), now_ms=1
    )
    assert "no data, no advice" not in body
    assert "⚡ ไม้ quick · stop ที่ลงทะเบียน" in body
    assert "จาก stop ที่ลงทะเบียน" in body
    assert markup is not None
    cbs = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
    assert "xc:1000PEPEUSDT:l" in cbs
    conn.close()


def test_outside_universe_non_quick_keeps_no_data_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / "out-nonquick.db")
    pos = _long_pos("1000PEPEUSDT", "1000")
    body, markup = render_open_positions_message(
        conn, futures=FakeFutures(positions=[pos], orders=[]), now_ms=1
    )
    assert "no data, no advice" in body
    assert "⚡ ไม้ quick" not in body
    assert markup is not None
    cbs = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
    assert "xc:1000PEPEUSDT:l" in cbs
    conn.close()


def test_in_universe_quick_menu_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / "in-quick.db")
    _insert_quick_reg(conn, reg_id="q-btc", symbol="BTCUSDT", planned_stop=90.0)
    body, _markup = render_open_positions_message(
        conn,
        futures=FakeFutures(positions=[_long_pos("BTCUSDT")], orders=[]),
        now_ms=1,
    )
    assert "⚡ ไม้ quick · stop ที่ลงทะเบียน" in body
    assert "จาก stop ที่ลงทะเบียน" in body
    conn.close()


def test_in_universe_quick_hides_main_stop_and_trail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """S1: Quick + same-side main signal must not show main 1R / trail advice."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / "in-quick-main.db")
    _insert_quick_reg(conn, reg_id="q-btc", symbol="BTCUSDT", planned_stop=90.0)
    # Main signal stop 95 would otherwise print "stop ตั้งต้น 95 (ฐาน 1R)".
    conn.execute(
        """
        INSERT INTO assist_signal (
            id, symbol, side, entry, stop, target, qty, notional, leverage, loss_at_stop,
            created_at_ms, expires_at_ms, status, sent_at_ms
        ) VALUES (
            'as_main_btc', 'BTCUSDT', 'long', 100, 95, 120, 0.01, 1, 1, 0.05,
            1, 999999999, 'sent', 1
        )
        """
    )
    upsert_watch_position(
        conn,
        {
            "symbol": "BTCUSDT",
            "position_side": "BOTH",
            "side": "long",
            "entry": 100.0,
            "qty": 0.01,
            "in_universe": 1,
            "signal_id": "as_main_btc",
            "initial_stop": 95.0,
            "initial_stop_source": "signal",
            "entry_time_ms": 1,
            "no_stop_latched": 0,
            "last_seen_ms": 1,
            "closed_at_ms": None,
        },
    )
    conn.commit()
    body, _markup = render_open_positions_message(
        conn,
        futures=FakeFutures(positions=[_long_pos("BTCUSDT")], orders=[]),
        now_ms=1,
    )
    assert "⚡ ไม้ quick · stop ที่ลงทะเบียน" in body
    assert "stop ตั้งต้น" not in body
    assert "stop แนะนำตอนนี้" not in body
    assert "จาก stop ที่ลงทะเบียน" in body
    # entry 100 mark 101, Quick stop 90 → +0.1R from registered stop (not main 95).
    assert "+0.1R (จาก stop ที่ลงทะเบียน)" in body
    conn.close()


def _close_notice_payload(
    *,
    symbol: str = "BTCUSDT",
    side: str = "long",
    entry_time_ms: int | None = 100,
    closed_at_ms: int | None = 300,
    net_usdt: float = 1.0,
    net_r: float = 0.1,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "side": side,
        "net_usdt": net_usdt,
        "net_r": net_r,
        "risk_usdt": 10.0,
        "entry_time_ms": entry_time_ms,
        "closed_at_ms": closed_at_ms,
        "commission_other_asset": None,
    }


def _seed_closed_quick_trade(
    conn: Any,
    *,
    trade_id: str,
    symbol: str = "BTCUSDT",
    side: str = "long",
    entry_time_ms: int = 100,
) -> None:
    _insert_quick_trade(
        conn,
        trade_id=trade_id,
        symbol=symbol,
        side=side,
        planned_stop=90.0,
        entry_time_ms=entry_time_ms,
        status="closed",
    )
    conn.execute(
        "UPDATE assist_quick_trade SET status='closed', net_usdt=1, net_r=0.1, "
        "close_notice_at_ms=NULL WHERE id=?",
        (trade_id,),
    )
    conn.commit()


def test_quick_close_notice_bot_vs_manual(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "close-notice.db")
    tag = quick_mode_tag()
    _seed_closed_quick_trade(conn, trade_id="t-bot")
    intent_id = insert_exec_intent(
        conn,
        kind="exit",
        symbol="BTCUSDT",
        side="long",
        preview={"source": "menu", "cohort": "quick", "qty": "1", "entry": "100"},
        created_at_ms=200,
        expires_at_ms=200 + PREVIEW_TTL_MS,
    )
    conn.execute(
        "UPDATE assist_exec_intent SET status='done', result_text=? WHERE id=?",
        ("✅ ปิด BTCUSDT", intent_id),
    )
    insert_exec_log(
        conn,
        intent_id=intent_id,
        step="exit_market",
        endpoint="/fapi/v1/order",
        http_status=200,
        order_or_algo_id="99",
        created_at_ms=210,
    )
    conn.commit()

    sent: list[str] = []
    _send_close_notice(
        conn,
        lambda m, **_: sent.append(m),
        trade_id="t-bot",
        client=None,
        payload=_close_notice_payload(),
        now_ms=300,
    )
    assert len(sent) == 1
    assert "(ปิดผ่านบอท)" in sent[0]
    assert "(ปิดใน Binance)" not in sent[0]
    assert tag in sent[0]

    _seed_closed_quick_trade(conn, trade_id="t-manual", symbol="ETHUSDT")
    sent.clear()
    _send_close_notice(
        conn,
        lambda m, **_: sent.append(m),
        trade_id="t-manual",
        client=None,
        payload=_close_notice_payload(symbol="ETHUSDT"),
        now_ms=400,
    )
    assert len(sent) == 1
    assert "(ปิดใน Binance)" in sent[0]
    assert "(ปิดผ่านบอท)" not in sent[0]
    conn.close()


@pytest.mark.parametrize(
    "case",
    [
        "already_closed_no_log",
        "non_quick_menu",
        "entry_time_none",
        "intent_before_entry",
    ],
)
def test_quick_close_notice_binance_when_no_exit_market(tmp_path: Path, case: str) -> None:
    """S2: (ปิดใน Binance) unless a Quick exit_market 200 log sits in the trade window."""
    conn = open_db(tmp_path / f"notice-binance-{case}.db")
    _seed_closed_quick_trade(conn, trade_id="t1")
    if case == "already_closed_no_log":
        intent_id = insert_exec_intent(
            conn,
            kind="exit",
            symbol="BTCUSDT",
            side="long",
            preview={"source": "menu", "cohort": "quick", "qty": "1", "entry": "100"},
            created_at_ms=200,
            expires_at_ms=200 + PREVIEW_TTL_MS,
        )
        conn.execute(
            "UPDATE assist_exec_intent SET status='done', result_text=? WHERE id=?",
            (MSG_ALREADY_CLOSED, intent_id),
        )
        # No exit_market log — Binance (or owner) already flat before confirm.
    elif case == "non_quick_menu":
        intent_id = insert_exec_intent(
            conn,
            kind="exit",
            symbol="BTCUSDT",
            side="long",
            preview={"source": "menu", "qty": "1", "entry": "100"},
            created_at_ms=200,
            expires_at_ms=200 + PREVIEW_TTL_MS,
        )
        conn.execute(
            "UPDATE assist_exec_intent SET status='done', result_text=? WHERE id=?",
            ("✅ ปิด BTCUSDT", intent_id),
        )
        insert_exec_log(
            conn,
            intent_id=intent_id,
            step="exit_market",
            endpoint="/fapi/v1/order",
            http_status=200,
            order_or_algo_id="99",
            created_at_ms=210,
        )
    elif case == "intent_before_entry":
        # Outside journal MATCH_PAD_MS (60s) before entry — must not match.
        intent_id = insert_exec_intent(
            conn,
            kind="exit",
            symbol="BTCUSDT",
            side="long",
            preview={"source": "menu", "cohort": "quick", "qty": "1", "entry": "100"},
            created_at_ms=1,
            expires_at_ms=1 + PREVIEW_TTL_MS,
        )
        conn.execute(
            "UPDATE assist_exec_intent SET status='done', result_text=? WHERE id=?",
            ("✅ ปิด BTCUSDT", intent_id),
        )
        insert_exec_log(
            conn,
            intent_id=intent_id,
            step="exit_market",
            endpoint="/fapi/v1/order",
            http_status=200,
            order_or_algo_id="99",
            created_at_ms=2,
        )
    elif case == "entry_time_none":
        # Clear DB fallback too; a would-match intent must still not count.
        conn.execute("UPDATE assist_quick_trade SET entry_time_ms=NULL WHERE id='t1'")
        intent_id = insert_exec_intent(
            conn,
            kind="exit",
            symbol="BTCUSDT",
            side="long",
            preview={"source": "menu", "cohort": "quick", "qty": "1", "entry": "100"},
            created_at_ms=200,
            expires_at_ms=200 + PREVIEW_TTL_MS,
        )
        conn.execute(
            "UPDATE assist_exec_intent SET status='done', result_text=? WHERE id=?",
            ("✅ ปิด BTCUSDT", intent_id),
        )
        insert_exec_log(
            conn,
            intent_id=intent_id,
            step="exit_market",
            endpoint="/fapi/v1/order",
            http_status=200,
            order_or_algo_id="99",
            created_at_ms=210,
        )
    conn.commit()

    if case == "entry_time_none":
        entry_ms: int | None = None
        closed_ms = 300
    elif case == "intent_before_entry":
        # entry 100_000; pad lower bound 40_000; intent at 1 is before window.
        conn.execute(
            "UPDATE assist_quick_trade SET entry_time_ms=? WHERE id='t1'",
            (100_000,),
        )
        conn.commit()
        entry_ms = 100_000
        closed_ms = 200_000
    else:
        entry_ms = 100
        closed_ms = 300
    sent: list[str] = []
    _send_close_notice(
        conn,
        lambda m, **_: sent.append(m),
        trade_id="t1",
        client=None,
        payload=_close_notice_payload(entry_time_ms=entry_ms, closed_at_ms=closed_ms),
        now_ms=400,
    )
    assert len(sent) == 1
    assert "(ปิดใน Binance)" in sent[0]
    assert "(ปิดผ่านบอท)" not in sent[0]
    conn.close()


@pytest.mark.parametrize("status", ["confirmed", "unknown"])
def test_quick_close_notice_bot_with_exit_market_log(tmp_path: Path, status: str) -> None:
    """S2: exit_market 200 proves bot close even when intent status is not done."""
    conn = open_db(tmp_path / f"notice-bot-{status}.db")
    _seed_closed_quick_trade(conn, trade_id="t1")
    intent_id = insert_exec_intent(
        conn,
        kind="exit",
        symbol="BTCUSDT",
        side="long",
        preview={"source": "menu", "cohort": "quick", "qty": "1", "entry": "100"},
        created_at_ms=200,
        expires_at_ms=200 + PREVIEW_TTL_MS,
    )
    conn.execute(
        "UPDATE assist_exec_intent SET status=?, result_text=? WHERE id=?",
        (status, "✅ ปิด BTCUSDT", intent_id),
    )
    insert_exec_log(
        conn,
        intent_id=intent_id,
        step="exit_market",
        endpoint="/fapi/v1/order",
        http_status=200,
        order_or_algo_id="99",
        created_at_ms=210,
    )
    conn.commit()
    sent: list[str] = []
    _send_close_notice(
        conn,
        lambda m, **_: sent.append(m),
        trade_id="t1",
        client=None,
        payload=_close_notice_payload(),
        now_ms=400,
    )
    assert len(sent) == 1
    assert "(ปิดผ่านบอท)" in sent[0]
    assert "(ปิดใน Binance)" not in sent[0]
    conn.close()


def test_menu_xx_quick_leaves_owner_algo_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """N5 / Q5=a: owner STOP_MARKET algo (non-jv-) must not be DELETE'd."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setattr(BinanceTradeClient, "assert_trade_key_safe", lambda self, **_: {"ok": True})
    monkeypatch.setattr(BinanceTradeClient, "assert_account_modes", lambda self: None)
    conn = open_db(tmp_path / "xx-quick-algo.db")
    _insert_quick_reg(
        conn, reg_id="q-algo", symbol="DOGEUSDT", side="short", planned_stop=2.0, planned_entry=1.0
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
    }
    handlers = _menu_close_handlers(open_orders=(200, []))
    handlers[("GET", "/fapi/v1/openAlgoOrders")] = (200, [owner_algo])
    handlers[("DELETE", "/fapi/v1/algoOrder")] = (200, {"code": "200", "msg": "success"})
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    handle_exec_callback(
        conn,
        f"xx:{intent_id}",
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=trade,
        now_ms_value=ts,
        message={"message_id": 1},
    )
    algo_deletes = [c for c in transport.calls if c[0] == "DELETE" and c[1] == "/fapi/v1/algoOrder"]
    assert algo_deletes == []
    row = get_exec_intent(conn, intent_id)
    assert row is not None and row["status"] == "done"
    conn.close()
