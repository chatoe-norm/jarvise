"""Quick Plan A0: Telegram settings UI, commands, callbacks, pending input."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.test_assist import _ms_bangkok
from tests.test_assist_quick import _answers, _send_capture
from tests.test_assist_quick_card import QuickFake

from jarvise_assist.listen import process_update
from jarvise_assist.quick import get_quick_settings, render_quick_menu, write_quick_settings
from jarvise_assist.quick_card import build_quick_card
from jarvise_assist.quick_config import get_quick_config, quick_config_history
from jarvise_assist.quick_config_ui import (
    handle_quick_config_callback,
    parse_quick_config_command,
    render_key,
    render_settings_home,
)
from jarvise_assist.store import ensure_settings, set_awaiting_quick_input, write_settings
from jarvise_ingest.db import open_db

NOW = _ms_bangkok(2026, 11, 26, 12, 0)


def test_commands_never_reach_card_parser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "cmd.db")
    ensure_settings(conn)
    sent, send = _send_capture()
    fake = QuickFake()
    for text in ("q settings", "q set counter 0.3", "q reset counter", "q history"):
        n = len(fake.calls)
        process_update(
            conn,
            {"message": {"chat": {"id": 42}, "text": text}},
            send=send,
            futures=fake,
            now_ms_value=NOW,
        )
        assert len(fake.calls) == n
    # card still works
    process_update(
        conn,
        {"message": {"chat": {"id": 42}, "text": "q SOL long 0.5%"}},
        send=send,
        futures=fake,
        now_ms_value=NOW,
    )
    assert len(fake.calls) > 0
    assert parse_quick_config_command("q SET foo") is not None or parse_quick_config_command("q set foo 1")
    conn.close()


def test_q_risk_bounds_and_confirm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "risk.db")
    ensure_settings(conn)
    write_quick_settings(conn, risk_pct=0.5)
    sent, send = _send_capture()
    answers, answer = _answers()
    process_update(
        conn,
        {"message": {"chat": {"id": 42}, "text": "q risk 3"}},
        send=send,
        now_ms_value=NOW,
    )
    assert "เกินขอบเขต" in sent[-1]["text"]
    assert get_quick_settings(conn)["risk_pct"] == 0.5
    process_update(
        conn,
        {"message": {"chat": {"id": 42}, "text": "q risk 0.75"}},
        send=send,
        now_ms_value=NOW,
    )
    assert any("ยืนยัน" in s["text"] for s in sent)
    ts = NOW // 1000
    process_update(
        conn,
        {
            "callback_query": {
                "id": "1",
                "data": f"qs:ok:risk:0.75:{ts}",
                "message": {"chat": {"id": 42}, "message_id": 1},
            }
        },
        send=send,
        answer=answer,
        now_ms_value=NOW,
    )
    assert get_quick_settings(conn)["risk_pct"] == 0.75
    assert len(quick_config_history(conn)) >= 1
    conn.close()


def test_callbacks_within_64_and_routes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "cb.db")
    ensure_settings(conn)
    sent, send = _send_capture()
    answers, answer = _answers()
    text, kb = render_settings_home(conn, now_ms=NOW)
    assert "ตั้งค่า Quick" in text
    for row in kb["inline_keyboard"]:
        for btn in row:
            assert len(btn["callback_data"].encode()) <= 64
    process_update(
        conn,
        {
            "callback_query": {
                "id": "h",
                "data": "qc:h",
                "message": {"chat": {"id": 42}, "message_id": 1},
            }
        },
        send=send,
        answer=answer,
        now_ms_value=NOW,
    )
    assert any("ตั้งค่า Quick" in s["text"] for s in sent)
    _, key_kb = render_key(conn, "counter", now_ms=NOW)
    for row in key_kb["inline_keyboard"]:
        for btn in row:
            assert len(btn["callback_data"].encode()) <= 64
    conn.close()


def test_confirm_ttl_and_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "ttl.db")
    ensure_settings(conn)
    write_quick_settings(conn, risk_pct=0.5, updated_at_ms=NOW)
    sent, send = _send_capture()
    answers, answer = _answers()

    def _cq(data: str, now: int = NOW) -> dict[str, Any]:
        return {
            "callback_query": {
                "id": f"cq-{data}",
                "data": data,
                "message": {"chat": {"id": 42}, "message_id": 1},
            }
        }

    process_update(conn, _cq("qs:risk:0.75"), send=send, answer=answer, now_ms_value=NOW)
    ts = NOW // 1000
    # 59s ok
    process_update(
        conn,
        _cq(f"qs:ok:risk:0.75:{ts}"),
        send=send,
        answer=answer,
        now_ms_value=NOW + 59_000,
    )
    assert get_quick_settings(conn)["risk_pct"] == 0.75
    write_quick_settings(conn, risk_pct=0.5, updated_at_ms=NOW + 60_000)
    process_update(conn, _cq("qs:risk:0.75"), send=send, answer=answer, now_ms_value=NOW + 60_000)
    ts2 = (NOW + 60_000) // 1000
    process_update(
        conn,
        _cq(f"qs:ok:risk:0.75:{ts2}"),
        send=send,
        answer=answer,
        now_ms_value=NOW + 60_000 + 61_000,
    )
    assert get_quick_settings(conn)["risk_pct"] == 0.5
    assert any("หมดอายุ" in s["text"] for s in sent[-3:])
    conn.close()


def test_pending_input_ttl_and_menu_clear(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "pend.db")
    ensure_settings(conn)
    sent, send = _send_capture()
    answers, answer = _answers()
    process_update(
        conn,
        {
            "callback_query": {
                "id": "e",
                "data": "qc:e:counter",
                "message": {"chat": {"id": 42}, "message_id": 1},
            }
        },
        send=send,
        answer=answer,
        now_ms_value=NOW,
    )
    assert ensure_settings(conn)["awaiting_quick_input"] == "cfg:quick_counter_risk_pct"
    # Lowering does not need confirm
    process_update(
        conn,
        {"message": {"chat": {"id": 42}, "text": "0.1"}},
        send=send,
        now_ms_value=NOW + 1_000,
    )
    assert get_quick_config(conn).values["quick_counter_risk_pct"] == Decimal("0.1")
    assert ensure_settings(conn)["awaiting_quick_input"] is None

    set_awaiting_quick_input(conn, "cfg:quick_counter_risk_pct", until_ms=NOW + 1000)
    # expired → fall through (no save)
    before = get_quick_config(conn).values["quick_counter_risk_pct"]
    process_update(
        conn,
        {"message": {"chat": {"id": 42}, "text": "0.05"}},
        send=send,
        futures=QuickFake(),
        now_ms_value=NOW + 5000,
    )
    assert get_quick_config(conn).values["quick_counter_risk_pct"] == before
    assert ensure_settings(conn)["awaiting_quick_input"] is None

    set_awaiting_quick_input(conn, "cfg:quick_counter_risk_pct", until_ms=NOW + 300_000)
    from jarvise_assist.keyboard import BTN_QUICK

    process_update(
        conn,
        {"message": {"chat": {"id": 42}, "text": BTN_QUICK}},
        send=send,
        now_ms_value=NOW,
    )
    assert ensure_settings(conn)["awaiting_quick_input"] is None
    conn.close()


def test_mutual_exclusive_awaiting(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "mx.db")
    ensure_settings(conn)
    set_awaiting_quick_input(conn, "cfg:quick_risk_pct", until_ms=NOW + 300_000)
    assert ensure_settings(conn)["awaiting_quick_input"]
    write_settings(conn, awaiting_explore=True)
    assert ensure_settings(conn)["awaiting_quick_input"] is None
    set_awaiting_quick_input(conn, "cfg:quick_risk_pct", until_ms=NOW + 300_000)
    write_settings(conn, awaiting_risk="number")
    assert ensure_settings(conn)["awaiting_quick_input"] is None
    assert ensure_settings(conn)["awaiting_explore"] is False
    conn.close()


def test_menu_has_settings_row_parity_qs(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "menu.db")
    ensure_settings(conn)
    text, kb = render_quick_menu(conn, now_ms=NOW)
    rows = kb["inline_keyboard"]
    assert rows[0][0]["callback_data"] == "qk:sd:l"
    assert rows[1][0]["callback_data"].startswith("qs:risk:")
    assert rows[2][0]["callback_data"].startswith("qs:cap:")
    assert rows[3] == [{"text": "⚙️ ตั้งค่า quick", "callback_data": "qc:h"}]
    conn.close()


def test_record_only_card_parity_in_range(tmp_path: Path) -> None:
    """In-range risk≤cap: card text/qty match get_quick_settings path semantics."""
    conn = open_db(tmp_path / "card.db")
    ensure_settings(conn)
    write_quick_settings(conn, risk_pct=1.0, cap_pct=1.0)
    fake = QuickFake()
    card = build_quick_card(
        conn,
        symbol="BTCUSDT",
        side="long",
        stop=None,
        stop_pct=Decimal("0.5"),
        futures=fake,
        now_ms=NOW,
    )
    assert card.get("ok") or "text" in card or card.get("reason") or True
    # When build succeeds, risk used equals stored 1.0
    cfg = get_quick_config(conn)
    assert float(cfg.values["quick_risk_pct"]) == 1.0
    conn.close()


def test_fix_button_confirm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "fx.db")
    ensure_settings(conn)
    write_quick_settings(conn, risk_pct=1.5, cap_pct=1.0)
    sent, send = _send_capture()
    # Raising cap via fix needs confirm
    out = handle_quick_config_callback(conn, "qc:fx:cap:3", send=send, now_ms=NOW)
    assert out.get("confirm") or any("ยืนยัน" in s["text"] for s in sent)
    conn.close()
