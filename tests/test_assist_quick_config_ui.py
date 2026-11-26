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
from jarvise_assist.quick_config import (
    QUICK_CONFIG,
    get_quick_config,
    needs_confirm,
    quick_config_history,
    set_quick_config,
)
from jarvise_assist.quick_config_ui import (
    _cb,
    handle_quick_config_callback,
    parse_quick_config_command,
    render_key,
    render_settings_home,
)
from jarvise_assist.store import ensure_settings, set_awaiting_quick_input, write_settings
from jarvise_ingest.db import open_db

NOW = _ms_bangkok(2026, 11, 26, 12, 0)


def _cq(data: str) -> dict[str, Any]:
    return {
        "callback_query": {
            "id": f"cq-{data[:40]}",
            "data": data,
            "message": {"chat": {"id": 42}, "message_id": 1},
        }
    }


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
    assert rows[0][0]["callback_data"] == "qk:cs:BTCUSDT"
    # risk / cap / settings sit after the coin grid + "other" row.
    assert any(b["callback_data"].startswith("qs:risk:") for row in rows for b in row)
    assert any(b["callback_data"].startswith("qs:cap:") for row in rows for b in row)
    assert rows[-2] == [{"text": "⚙️ ตั้งค่า quick", "callback_data": "qc:h"}]
    assert rows[-1] == [{"text": "✖️ ปิด", "callback_data": "qk:mx"}]
    conn.close()


# Frozen literals from b46da90 quick_card.py with QuickFake BTCUSDT long stop 0.5%.
_PARITY_1_1_TEXT = (
    "⚡ Quick · BTCUSDT · LONG\n"
    "💰 เงินจริง · บอทไม่ส่งคำสั่ง\n"
    "เข้า (mark ตอนนี้): 100.00000000\n"
    "Stop: 99.50000000 (-0.50%)\n"
    "ขนาด: 16 BTC ≈ 1600 USDT\n"
    "ฐาน: Futures 1000 USDT × 1% = งบ 10 USDT\n"
    "risk จริง: −9.92 USDT = 0.99% ของ Futures 1000 USDT (เป้า 1%)\n"
    "Leverage ที่ต้องใช้: 2x · margin ≈ 800 USDT\n"
    "ถ้าชน stop: −9.92 USDT ≈ −1.24R รวมค่าธรรมเนียม (ราคา −8 USDT = 1R · ค่าธรรมเนียม ≈ 1.92 USDT)\n"
    "ค่าธรรมเนียมไป-กลับ ≈ 0.24R ≈ 1.92 USDT (โมเดล 6 bps/ข้าง — ยังไม่อ่านค่าจริงจากบัญชี)\n"
    "เป้า 1R: 100.50000000 → สุทธิ +0.76R · ต้องชนะ ≥ 62% ถึงคุ้มทุน\n"
    "เป้า 1.5R: 100.75000000 → สุทธิ +1.26R · ต้องชนะ ≥ 50% ถึงคุ้มทุน\n"
    "ความเสี่ยงเปิดอยู่: หลัก 0% · quick 0% + ไม้นี้ 0.8% (ไม่รวมค่าธรรมเนียม) (เพดาน quick 1% · รวม 2%)\n"
    "การ์ดนี้ใช้ได้ถึง 12:02:00 (หมดแล้วกด 🔄 อัปเดตราคา) · บอทไม่ส่งคำสั่ง — กดลงทะเบียน แล้วเปิดเองใน Binance ภายใน 10 นาที\n"
    "ตั้ง Stop-Market (Reduce-Only, trigger = Mark Price) ทันทีหลังเข้า"
)
_PARITY_1_1_MARKUP = {
    "inline_keyboard": [[{"text": "⚡ เริ่มไม้ quick", "callback_data": "qr:BTCUSDT:l:99.50000000:1795669200"}]]
}
_PARITY_075_2_TEXT = (
    "⚡ Quick · BTCUSDT · LONG\n"
    "💰 เงินจริง · บอทไม่ส่งคำสั่ง\n"
    "เข้า (mark ตอนนี้): 100.00000000\n"
    "Stop: 99.50000000 (-0.50%)\n"
    "ขนาด: 12 BTC ≈ 1200 USDT\n"
    "ฐาน: Futures 1000 USDT × 0.75% = งบ 7.5 USDT\n"
    "risk จริง: −7.44 USDT = 0.74% ของ Futures 1000 USDT (เป้า 0.75%)\n"
    "Leverage ที่ต้องใช้: 2x · margin ≈ 600 USDT\n"
    "ถ้าชน stop: −7.44 USDT ≈ −1.24R รวมค่าธรรมเนียม (ราคา −6 USDT = 1R · ค่าธรรมเนียม ≈ 1.44 USDT)\n"
    "ค่าธรรมเนียมไป-กลับ ≈ 0.24R ≈ 1.44 USDT (โมเดล 6 bps/ข้าง — ยังไม่อ่านค่าจริงจากบัญชี)\n"
    "เป้า 1R: 100.50000000 → สุทธิ +0.76R · ต้องชนะ ≥ 62% ถึงคุ้มทุน\n"
    "เป้า 1.5R: 100.75000000 → สุทธิ +1.26R · ต้องชนะ ≥ 50% ถึงคุ้มทุน\n"
    "ความเสี่ยงเปิดอยู่: หลัก 0% · quick 0% + ไม้นี้ 0.6% (ไม่รวมค่าธรรมเนียม) (เพดาน quick 2% · รวม 2%)\n"
    "การ์ดนี้ใช้ได้ถึง 12:02:00 (หมดแล้วกด 🔄 อัปเดตราคา) · บอทไม่ส่งคำสั่ง — กดลงทะเบียน แล้วเปิดเองใน Binance ภายใน 10 นาที\n"
    "ตั้ง Stop-Market (Reduce-Only, trigger = Mark Price) ทันทีหลังเข้า"
)
_PARITY_075_2_MARKUP = {
    "inline_keyboard": [[{"text": "⚡ เริ่มไม้ quick", "callback_data": "qr:BTCUSDT:l:99.50000000:1795669200"}]]
}


@pytest.mark.parametrize(
    ("risk", "cap", "qty", "text", "markup"),
    [
        (1.0, 1.0, 16, _PARITY_1_1_TEXT, _PARITY_1_1_MARKUP),
        (0.75, 2.0, 12, _PARITY_075_2_TEXT, _PARITY_075_2_MARKUP),
    ],
)
def test_record_only_card_parity_in_range(
    tmp_path: Path,
    risk: float,
    cap: float,
    qty: int,
    text: str,
    markup: dict[str, Any],
) -> None:
    """In-range risk≤cap: card matches frozen b46da90 quick_card literals."""
    conn = open_db(tmp_path / f"card_{risk}_{cap}.db")
    ensure_settings(conn)
    write_quick_settings(conn, risk_pct=risk, cap_pct=cap)
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
    assert card.get("ok") is True
    assert card.get("qty") == qty
    assert card.get("size_mode") == "risk"
    assert card.get("text") == text
    assert card.get("reply_markup") == markup
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


def test_reset_all_confirm_respects_ttl_and_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """qc:ok:all must honor TTL 60s, future ts, and post-card changes (#1)."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "ra_ttl.db")
    ensure_settings(conn)
    write_quick_settings(conn, risk_pct=0.25, cap_pct=1.0, updated_at_ms=NOW)
    sent, send = _send_capture()
    answers, answer = _answers()

    process_update(
        conn,
        {"message": {"chat": {"id": 42}, "text": "q reset all"}},
        send=send,
        now_ms_value=NOW,
    )
    assert any("ยืนยันรีเซ็ต" in s["text"] for s in sent)
    ts = NOW  # ms card
    # +59s applies
    process_update(
        conn,
        _cq(f"qc:ok:all:1:{ts}"),
        send=send,
        answer=answer,
        now_ms_value=NOW + 59_000,
    )
    assert get_quick_settings(conn)["risk_pct"] == 0.5

    write_quick_settings(conn, risk_pct=0.25, cap_pct=1.0, updated_at_ms=NOW + 100_000)
    process_update(
        conn,
        {"message": {"chat": {"id": 42}, "text": "q reset all"}},
        send=send,
        now_ms_value=NOW + 100_000,
    )
    ts2 = NOW + 100_000
    process_update(
        conn,
        _cq(f"qc:ok:all:1:{ts2}"),
        send=send,
        answer=answer,
        now_ms_value=NOW + 100_000 + 61_000,
    )
    assert get_quick_settings(conn)["risk_pct"] == 0.25
    assert any("หมดอายุ" in s["text"] for s in sent[-3:])

    process_update(
        conn,
        {"message": {"chat": {"id": 42}, "text": "q reset all"}},
        send=send,
        now_ms_value=NOW + 200_000,
    )
    ts3 = NOW + 200_000
    process_update(
        conn,
        _cq(f"qc:ok:all:1:{ts3 + 30_000}"),  # 30s in the future → stale
        send=send,
        answer=answer,
        now_ms_value=NOW + 200_000,
    )
    assert get_quick_settings(conn)["risk_pct"] == 0.25

    process_update(
        conn,
        {"message": {"chat": {"id": 42}, "text": "q reset all"}},
        send=send,
        now_ms_value=NOW + 300_000,
    )
    ts4 = NOW + 300_000
    write_quick_settings(conn, risk_pct=0.25, updated_at_ms=NOW + 300_000 + 5_000)
    process_update(
        conn,
        _cq(f"qc:ok:all:1:{ts4}"),
        send=send,
        answer=answer,
        now_ms_value=NOW + 300_000 + 10_000,
    )
    assert get_quick_settings(conn)["risk_pct"] == 0.25
    conn.close()


def test_qc_no_invalidates_kv_confirm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancel must stale outstanding KV qc:ok without history rows (#2)."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "qc_no.db")
    ensure_settings(conn)
    sent, send = _send_capture()
    answers, answer = _answers()
    process_update(
        conn,
        {"message": {"chat": {"id": 42}, "text": "q set counter 0.5"}},
        send=send,
        now_ms_value=NOW,
    )
    assert any("ยืนยัน" in s["text"] for s in sent)
    ts = NOW  # ms card
    hist_before = len(quick_config_history(conn))
    process_update(
        conn,
        _cq("qc:no"),
        send=send,
        answer=answer,
        now_ms_value=NOW + 1_000,
    )
    assert len(quick_config_history(conn)) == hist_before
    process_update(
        conn,
        _cq(f"qc:ok:counter:0.5:{ts}"),
        send=send,
        answer=answer,
        now_ms_value=NOW + 5_000,
    )
    assert get_quick_config(conn).values["quick_counter_risk_pct"] == Decimal("0.25")
    conn.close()


def test_universe_enum_encode_and_confirm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """universe any must render / set / confirm without Decimal crash (#3)."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "univ.db")
    ensure_settings(conn)
    sent, send = _send_capture()
    answers, answer = _answers()
    text, kb = render_key(conn, "universe", now_ms=NOW)
    assert "universe" in text.lower() or "จักรวาล" in text
    for row in kb["inline_keyboard"]:
        for btn in row:
            assert len(btn["callback_data"].encode()) <= 64
    process_update(
        conn,
        {"message": {"chat": {"id": 42}, "text": "q set universe any"}},
        send=send,
        now_ms_value=NOW,
    )
    assert any("ยืนยัน" in s["text"] for s in sent)
    process_update(
        conn,
        _cq("qc:v:universe:any"),
        send=send,
        answer=answer,
        now_ms_value=NOW + 1_000,
    )
    assert any("ยืนยัน" in s["text"] for s in sent[-2:])
    ts = NOW + 2_000  # ms card
    process_update(
        conn,
        _cq(f"qc:ok:universe:any:{ts}"),
        send=send,
        answer=answer,
        now_ms_value=NOW + 2_000,
    )
    assert get_quick_config(conn).values["quick_exec_universe"] == "any"
    # Opening key after raise also works
    render_key(conn, "universe", now_ms=NOW + 3_000)
    conn.close()


@pytest.mark.parametrize("entry", list(QUICK_CONFIG), ids=[e.alias for e in QUICK_CONFIG])
def test_registry_key_render_and_qc_v_no_exception(
    tmp_path: Path, entry: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every registry key: render_key + qc:v (+ confirm when needed) ≤ 64 bytes (#3)."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / f"reg_{entry.alias}.db")
    ensure_settings(conn)
    write_quick_settings(conn, risk_pct=0.5, cap_pct=1.0)
    sent, send = _send_capture()
    answers, answer = _answers()
    text, kb = render_key(conn, entry.alias, now_ms=NOW)
    assert text
    for row in kb["inline_keyboard"]:
        for btn in row:
            assert len(btn["callback_data"].encode()) <= 64, btn["callback_data"]

    # Drive one qc:v toward a non-default (or default) preset/step value
    from jarvise_assist.quick_config_ui import _encode_val, _preset_buttons, _step_value

    target = entry.default
    presets = _preset_buttons(entry)
    if presets:
        target = presets[-1]
    else:
        stepped = _step_value(entry, entry.default, 1)
        if stepped is not None:
            target = stepped
    enc = _encode_val(entry, target)
    data = f"qc:v:{entry.alias}:{enc}"
    assert len(data.encode()) <= 64
    process_update(
        conn,
        _cq(data),
        send=send,
        answer=answer,
        now_ms_value=NOW,
    )
    if any("ยืนยัน" in s["text"] for s in sent) and needs_confirm(entry.key, entry.default, target):
        ts = NOW  # ms card
        ok_data = f"qc:ok:{entry.alias}:{enc}:{ts}"
        assert len(ok_data.encode()) <= 64
        process_update(
            conn,
            _cq(ok_data),
            send=send,
            answer=answer,
            now_ms_value=NOW + 1_000,
        )
    conn.close()


def test_qc_ok_per_key_ttl_future_stale_via_process_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-key qc:ok TTL / future / changed-after via process_update (#7a)."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "qc_ttl.db")
    ensure_settings(conn)
    sent, send = _send_capture()
    answers, answer = _answers()
    process_update(
        conn,
        {"message": {"chat": {"id": 42}, "text": "q set counter 0.5"}},
        send=send,
        now_ms_value=NOW,
    )
    ts = NOW  # ms card
    process_update(
        conn,
        _cq(f"qc:ok:counter:0.5:{ts}"),
        send=send,
        answer=answer,
        now_ms_value=NOW + 61_000,
    )
    assert get_quick_config(conn).values["quick_counter_risk_pct"] == Decimal("0.25")
    assert any("หมดอายุ" in s["text"] for s in sent[-2:])

    process_update(
        conn,
        {"message": {"chat": {"id": 42}, "text": "q set counter 0.5"}},
        send=send,
        now_ms_value=NOW + 100_000,
    )
    ts2 = NOW + 100_000
    process_update(
        conn,
        _cq(f"qc:ok:counter:0.5:{ts2 + 30_000}"),  # 30s future → stale
        send=send,
        answer=answer,
        now_ms_value=NOW + 100_000,
    )
    assert get_quick_config(conn).values["quick_counter_risk_pct"] == Decimal("0.25")

    process_update(
        conn,
        {"message": {"chat": {"id": 42}, "text": "q set counter 0.5"}},
        send=send,
        now_ms_value=NOW + 200_000,
    )
    ts3 = NOW + 200_000
    set_quick_config(
        conn,
        "quick_counter_risk_pct",
        "0.1",
        source="t",
        now_ms=NOW + 200_000 + 5_000,
    )
    process_update(
        conn,
        _cq(f"qc:ok:counter:0.5:{ts3}"),
        send=send,
        answer=answer,
        now_ms_value=NOW + 200_000 + 10_000,
    )
    assert get_quick_config(conn).values["quick_counter_risk_pct"] == Decimal("0.1")
    conn.close()


def test_reset_all_requires_confirm_when_raise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """risk 0.25 → default 0.5 is a raise: q reset all must confirm, not apply (#7b)."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "ra_raise.db")
    ensure_settings(conn)
    write_quick_settings(conn, risk_pct=0.25, cap_pct=1.0)
    sent, send = _send_capture()
    process_update(
        conn,
        {"message": {"chat": {"id": 42}, "text": "q reset all"}},
        send=send,
        now_ms_value=NOW,
    )
    assert get_quick_settings(conn)["risk_pct"] == 0.25
    assert any("ยืนยันรีเซ็ต" in s["text"] for s in sent)
    assert any(
        any(b.get("callback_data", "").startswith("qc:ok:all:") for b in row)
        for s in sent
        if s.get("reply_markup")
        for row in s["reply_markup"].get("inline_keyboard", [])
    )
    conn.close()


def test_cb_raises_over_64_bytes() -> None:
    """_cb must reject callback_data longer than 64 bytes (#7d)."""
    with pytest.raises(ValueError, match="64"):
        _cb("x" * 65)


def test_reset_all_night_pair_via_process_update(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """q reset all with wide night window must apply (Thai error if invalid) (#5)."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "night_ra.db")
    ensure_settings(conn)
    set_quick_config(conn, "quick_night_start", "01:00", source="t", now_ms=1)
    set_quick_config(conn, "quick_night_end", "12:00", source="t", now_ms=2)
    write_quick_settings(conn, risk_pct=0.5, cap_pct=1.0)
    sent, send = _send_capture()
    answers, answer = _answers()
    # No raise among defaults vs current risk/cap — may apply without confirm if
    # only night differs (confirm_dir none). Drive via handle path.
    process_update(
        conn,
        {"message": {"chat": {"id": 42}, "text": "q reset all"}},
        send=send,
        now_ms_value=NOW,
    )
    # If confirm shown (other keys), confirm; else already applied
    if any("ยืนยันรีเซ็ต" in s["text"] for s in sent):
        ts = NOW  # ms card
        process_update(
            conn,
            _cq(f"qc:ok:all:1:{ts}"),
            send=send,
            answer=answer,
            now_ms_value=NOW + 1_000,
        )
    view = get_quick_config(conn)
    assert view.values["quick_night_start"] == "23:30"
    assert view.values["quick_night_end"] == "07:30"
    assert not any("Traceback" in s["text"] for s in sent)
    conn.close()


def test_qc_ok_ms_staleness_same_second_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Card at T+100ms stale after change at T+800ms (per-key + reset-all); 10-digit → stale."""
    from jarvise_assist.quick import MSG_QUICK_CONFIRM_STALE

    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "ms_stale.db")
    ensure_settings(conn)
    sent, send = _send_capture()
    answers, answer = _answers()

    # Per-key: card at base+100, change at base+800 → stale
    base = NOW
    card_ms = base + 100
    process_update(
        conn,
        {"message": {"chat": {"id": 42}, "text": "q set counter 0.5"}},
        send=send,
        now_ms_value=card_ms,
    )
    set_quick_config(
        conn,
        "quick_counter_risk_pct",
        "0.1",
        source="t",
        now_ms=base + 800,
    )
    process_update(
        conn,
        _cq(f"qc:ok:counter:0.5:{card_ms}"),
        send=send,
        answer=answer,
        now_ms_value=base + 1_500,
    )
    assert get_quick_config(conn).values["quick_counter_risk_pct"] == Decimal("0.1")
    assert any(MSG_QUICK_CONFIRM_STALE in s["text"] or "หมดอายุ" in s["text"] for s in sent[-3:])

    # Reset-all: card at base2+100, settings change at base2+800 → stale
    base2 = NOW + 1_000_000
    write_quick_settings(conn, risk_pct=0.25, cap_pct=1.0, updated_at_ms=base2)
    process_update(
        conn,
        {"message": {"chat": {"id": 42}, "text": "q reset all"}},
        send=send,
        now_ms_value=base2 + 100,
    )
    write_quick_settings(conn, risk_pct=0.25, cap_pct=1.0, updated_at_ms=base2 + 800)
    process_update(
        conn,
        _cq(f"qc:ok:all:1:{base2 + 100}"),
        send=send,
        answer=answer,
        now_ms_value=base2 + 1_500,
    )
    assert get_quick_settings(conn)["risk_pct"] == 0.25

    # 10-digit qc:ok is always stale
    n = len(sent)
    process_update(
        conn,
        _cq(f"qc:ok:counter:0.5:{base // 1000}"),
        send=send,
        answer=answer,
        now_ms_value=base + 2_000,
    )
    assert any(s["text"] == MSG_QUICK_CONFIRM_STALE for s in sent[n:])
    conn.close()


def test_qc_ok_fresh_after_cancel_same_second(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Card +200ms after cancel in same second applies; card before cancel is stale."""
    from jarvise_assist.quick_config import mark_confirm_cancelled

    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "ms_cancel.db")
    ensure_settings(conn)
    sent, send = _send_capture()
    answers, answer = _answers()

    # Same second: cancel at +100, fresh card at +200 → applies
    t0 = NOW
    # Align to a second boundary so cancel and card share the same wall second.
    t0 = (t0 // 1000) * 1000
    mark_confirm_cancelled(conn, now_ms=t0 + 100)
    process_update(
        conn,
        {"message": {"chat": {"id": 42}, "text": "q set counter 0.5"}},
        send=send,
        now_ms_value=t0 + 200,
    )
    process_update(
        conn,
        _cq(f"qc:ok:counter:0.5:{t0 + 200}"),
        send=send,
        answer=answer,
        now_ms_value=t0 + 500,
    )
    assert get_quick_config(conn).values["quick_counter_risk_pct"] == Decimal("0.5")

    # Card before cancel → stale
    set_quick_config(conn, "quick_counter_risk_pct", "0.25", source="t", now_ms=t0 + 10_000)
    card_before = t0 + 20_000
    process_update(
        conn,
        {"message": {"chat": {"id": 42}, "text": "q set counter 0.5"}},
        send=send,
        now_ms_value=card_before,
    )
    mark_confirm_cancelled(conn, now_ms=card_before + 50)
    process_update(
        conn,
        _cq(f"qc:ok:counter:0.5:{card_before}"),
        send=send,
        answer=answer,
        now_ms_value=card_before + 100,
    )
    assert get_quick_config(conn).values["quick_counter_risk_pct"] == Decimal("0.25")
    conn.close()


def test_qc_ok_callback_data_le_64_incl_max_presets() -> None:
    """Every registry qc:ok (incl. longest 4-value list_pct) stays ≤ 64 bytes with 13-digit ts."""
    from decimal import Decimal as D

    from jarvise_assist.quick_config_ui import _encode_val

    ts = "1732622400123"  # 13 digits
    assert len(ts) == 13
    for entry in QUICK_CONFIG:
        if entry.type == "list_pct":
            vals = (D("0.05"), D("6.65"), D("13.25"), D("19.9"))
            enc = _encode_val(entry, vals)
        elif entry.hard_max is not None:
            enc = _encode_val(entry, entry.hard_max)
        else:
            enc = _encode_val(entry, entry.default)
        data = f"qc:ok:{entry.alias}:{enc}:{ts}"
        assert len(data.encode()) <= 64, (entry.alias, data, len(data.encode()))
    all_data = f"qc:ok:all:1:{ts}"
    assert len(all_data.encode()) <= 64
