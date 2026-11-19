"""M0.5 — close open positions from the 📋 menu (live mode, one confirm)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.test_assist import FakeFutures, _ms_bangkok
from tests.test_assist_execute import RecordingTransport, _client, _open_pos, _open_then_flat

from jarvise_assist.execute import (
    MSG_ALREADY_CLOSED,
    MSG_EXIT_POSITION_CHANGED,
    MSG_MENU_QUICK,
    MSG_OFF,
    PREVIEW_TTL_MS,
    BinanceTradeClient,
    handle_exec_callback,
)
from jarvise_assist.exit_log import record_intent_action
from jarvise_assist.journal import collect_auto_checks
from jarvise_assist.listen import process_update
from jarvise_assist.menu import render_open_positions, render_open_positions_message
from jarvise_assist.store import insert_exec_intent, insert_exec_log, now_ms, write_settings
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
        ("xc:BTCUSDT:l", "off", MSG_OFF),
        ("xc:BTCUSDT:l", "testnet", MSG_OFF),
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
    assert sent[-1] == MSG_EXIT_POSITION_CHANGED

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


def test_journal_menu_source_checks(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "journal.db")
    entry_ms = _ms_bangkok(2026, 11, 20, 12, 0)
    exit_ms = entry_ms + 60_000
    conn.execute(
        """
        INSERT INTO assist_real_trade (
            symbol, side, entry_order_id, status, updated_at_ms, entry_time_ms, entry_qty,
            entry_fill_avg, exit_time_ms, exit_kind
        ) VALUES ('BTCUSDT', 'long', 'm1', 'closed', ?, ?, 1, 100, ?, 'owner_exit')
        """,
        (exit_ms, entry_ms, exit_ms),
    )
    intent_id = insert_exec_intent(
        conn,
        kind="exit",
        symbol="BTCUSDT",
        side="long",
        preview={"source": "menu", "exit_advice_active": True, "qty": "1", "entry": "1"},
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
    trade = dict(conn.execute("SELECT * FROM assist_real_trade WHERE entry_order_id='m1'").fetchone())
    checks = collect_auto_checks(conn, trade)
    assert checks.get("exit_source") == "menu"
    assert checks.get("exit_followed_plan") is True
    conn.close()
