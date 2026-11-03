"""Trade assistant PR C — Telegram listener, virtual track, streak pause."""

from __future__ import annotations

from datetime import timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from tests.test_assist import FakeFutures, _ms_bangkok, _seed_analysis

from jarvise_assist import listen as listen_mod
from jarvise_assist.keyboard import BTN_ANYTHING, BTN_POSITIONS, BTN_TOGGLE, risk_button_label
from jarvise_assist.listen import poll_once, process_update, start_listener_thread
from jarvise_assist.menu import (
    _signed_r_one,
    _signed_usdt,
    handle_menu_text,
    handle_risk_callback,
    handle_risk_number,
    render_open_positions,
)
from jarvise_assist.pause import (
    RESUME_TEXT,
    STREAK_PAUSE_TEXT,
    clear_pause_manual,
    evaluate_pause,
    maybe_auto_resume,
    next_0820_bangkok_ms,
    week_loss_pause_text,
)
from jarvise_assist.signals import run_assist_morning, run_assist_signals
from jarvise_assist.sizing import size_signal
from jarvise_assist.store import (
    ensure_settings,
    get_edge,
    get_signal,
    get_telegram_state,
    insert_signal,
    write_edge,
    write_settings,
    write_telegram_state,
)
from jarvise_assist.track import gap_aware_stop_fill, resolve_open_tracks
from jarvise_assist.watch import run_assist_watch
from jarvise_assist.weekly import format_weekly_summary, run_assist_weekly, summarize_income
from jarvise_ingest.db import open_db, upsert_market_technicals
from jarvise_paper.stops import resting_stop_fill

BANGKOK = timezone(timedelta(hours=7), name="Asia/Bangkok")


def _send_capture() -> tuple[list[dict[str, Any]], Any]:
    sent: list[dict[str, Any]] = []

    def send(text: str, *, reply_markup: dict[str, Any] | None = None) -> int:
        mid = len(sent) + 1
        sent.append({"text": text, "reply_markup": reply_markup, "message_id": mid})
        return mid

    return sent, send


def test_other_chat_ignored_and_offset_persists(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    conn = open_db(tmp_path / "listen.db")
    write_telegram_state(conn, last_update_id=5)
    sent, send = _send_capture()

    foreign = {
        "update_id": 6,
        "message": {"chat": {"id": 999}, "text": BTN_POSITIONS},
    }
    assert process_update(conn, foreign, send=send)["ignored"] == "foreign_chat"
    assert sent == []

    def fake_updates(*, offset: int | None = None, timeout: int = 25) -> list[dict[str, Any]]:
        assert offset == 6
        return [
            {
                "update_id": 10,
                "message": {"chat": {"id": 999}, "text": "hi"},
            },
            {
                "update_id": 11,
                "message": {"chat": {"id": 111}, "text": BTN_TOGGLE},
            },
        ]

    def handler(c: Any, upd: dict[str, Any]) -> dict[str, Any]:
        return process_update(c, upd, send=send)

    result = poll_once(conn, get_updates_fn=fake_updates, process_fn=handler)
    assert result["ok"] is True
    assert get_telegram_state(conn)["last_update_id"] == 11
    assert any("ผู้ช่วย" in s["text"] for s in sent)
    conn.close()


def test_skip_callback_sets_skipped_and_edits(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "skip.db")
    day = _ms_bangkok(2026, 10, 27, 12, 0)
    sized = size_signal(
        symbol="BTCUSDT",
        side="long",
        entry=Decimal("100"),
        stop=Decimal("90"),
        balance=Decimal("1000"),
        risk_pct=Decimal("2"),
        step_size=Decimal("0.001"),
    )
    assert sized is not None
    sid = insert_signal(conn, sized, status="sent", created_at_ms=day, expires_at_ms=day + 1, sent_at_ms=day)
    from jarvise_assist.store import set_signal_telegram_message_id

    set_signal_telegram_message_id(conn, sid, 77)
    edits: list[tuple[Any, ...]] = []
    answers: list[str] = []

    def edit(mid: int, text: str, *, reply_markup: Any = None) -> bool:
        edits.append((mid, text, reply_markup))
        return True

    def answer(cq_id: str, *, text: str | None = None) -> bool:
        answers.append(cq_id)
        return True

    original = "🔺 BTCUSDT · LONG (4h)\nเข้า: 100"
    upd = {
        "update_id": 1,
        "callback_query": {
            "id": "cq1",
            "data": sid,
            "message": {
                "message_id": 77,
                "chat": {"id": 42},
                "text": original,
            },
        },
    }
    out = process_update(conn, upd, answer=answer, edit=edit)
    assert out["changed"] is True
    row = get_signal(conn, sid)
    assert row is not None and row["status"] == "skipped"
    assert edits and "(ข้ามแล้ว)" in edits[0][1]
    assert answers == ["cq1"]
    conn.close()


def test_risk_menu_confirms_above_2_and_rejects_out_of_range(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "risk.db")
    sent, send = _send_capture()
    out = handle_risk_callback(conn, "risk:other", send=send)
    assert out["awaiting"] == "number"
    assert ensure_settings(conn)["awaiting_risk"] == "number"

    bad = handle_risk_number(conn, "7.5", send=send)
    assert bad["rejected"] is True
    assert ensure_settings(conn)["risk_pct"] == 1.0
    assert "0.5–3" in sent[-1]["text"]

    write_settings(conn, awaiting_risk="number")
    apply = handle_risk_number(conn, "2", send=send)
    assert apply.get("risk_pct") == 2.0
    assert ensure_settings(conn)["risk_pct"] == 2.0

    write_settings(conn, awaiting_risk="number")
    conf = handle_risk_number(conn, "2.5", send=send)
    assert conf.get("confirm") is True
    assert "ยืนยัน 2.5%?" in sent[-1]["text"]
    assert ensure_settings(conn)["pending_risk_pct"] == 2.5

    yes = handle_risk_callback(conn, "risk_yes:2.5", send=send)
    assert yes["risk_pct"] == 2.5
    assert ensure_settings(conn)["risk_pct"] == 2.5

    refused = handle_risk_callback(conn, "risk_yes:7.5", send=send)
    assert refused.get("rejected") is True
    assert ensure_settings(conn)["risk_pct"] == 2.5
    conn.close()


def test_risk_buttons_include_usdt_and_clamp_stored(tmp_path: Path) -> None:
    from jarvise_assist.keyboard import risk_choice_keyboard
    from jarvise_assist.store import clamp_stored_risk_pct

    kb = risk_choice_keyboard(balance=Decimal("100"))
    labels = [b["text"] for row in kb["inline_keyboard"] for b in row]
    assert "2% · 2.00 USDT" in labels
    assert "0.5% · 0.50 USDT" in labels
    assert "5%" not in labels

    kb_pct = risk_choice_keyboard(balance=None)
    labels_pct = [b["text"] for row in kb_pct["inline_keyboard"] for b in row]
    assert "2%" in labels_pct

    conn = open_db(tmp_path / "clamp.db")
    write_settings(conn, risk_pct=5.0)
    sent, send = _send_capture()
    from jarvise_assist.keyboard import reply_keyboard

    out = clamp_stored_risk_pct(conn, send=send, reply_keyboard_fn=reply_keyboard)
    assert out == 3.0
    assert ensure_settings(conn)["risk_pct"] == 3.0
    assert "ปรับเป็น 3%" in sent[-1]["text"]
    n = len(sent)
    again = clamp_stored_risk_pct(conn, send=send, reply_keyboard_fn=reply_keyboard)
    assert again == 3.0
    assert len(sent) == n
    conn.close()


def test_enable_toggle(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "tog.db")
    sent, send = _send_capture()
    day = _ms_bangkok(2026, 10, 27, 12, 0)
    write_settings(conn, paused_until_ms=day + 10_000_000)
    r1 = handle_menu_text(conn, BTN_TOGGLE, now_ms=day, send=send)
    assert "ปิด" in (r1.get("text") or "")
    assert ensure_settings(conn)["enabled"] is False
    r2 = handle_menu_text(conn, BTN_TOGGLE, now_ms=day, send=send)
    assert "เปิด" in (r2.get("text") or "")
    assert ensure_settings(conn)["enabled"] is True
    assert ensure_settings(conn)["paused_until_ms"] is None
    assert get_edge(conn)["resume_alerted"] is True
    conn.close()


def test_anything_now_inserts_nothing_and_reports_pause(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "any.db")
    day = _ms_bangkok(2026, 10, 27, 12, 0)
    until = day + 3_600_000
    write_settings(conn, paused_until_ms=until)
    sent, send = _send_capture()
    fake = FakeFutures(marks={"BTCUSDT": Decimal("100")})
    _seed_analysis(conn, symbol="BTCUSDT", action="long", confidence=0.7, invalidation=90.0, ts=day - 60_000)
    out = handle_menu_text(conn, BTN_ANYTHING, futures=fake, now_ms=day, send=send)
    assert "หยุดพักถึง" in (out.get("text") or "")
    n = conn.execute("SELECT COUNT(*) FROM assist_signal").fetchone()[0]
    assert n == 0
    conn.close()


def test_virtual_track_stop_trail_and_2r_matches_resting_fill(tmp_path: Path) -> None:
    # Parity: copied gap fill vs resting_stop_fill two-line rule.
    cases = [
        ("long", 100.0, 99.0, 98.0, 101.0),
        ("long", 100.0, 101.0, 99.5, 102.0),
        ("long", 100.0, 101.0, 100.5, 102.0),
        ("short", 100.0, 101.0, 99.0, 102.0),
        ("short", 100.0, 99.0, 98.0, 101.0),
        ("short", 100.0, 99.0, 98.0, 99.0),
    ]
    for side, stop, open_px, low, high in cases:
        copied = gap_aware_stop_fill(side=side, stop_f=stop, open_px=open_px, low=low, high=high)
        if side == "long":
            expected = None if low > stop else (open_px if open_px <= stop else stop)
        else:
            expected = None if high < stop else (open_px if open_px >= stop else stop)
        assert copied == expected

    conn = open_db(tmp_path / "track.db")
    sent_at = _ms_bangkok(2026, 10, 27, 8, 0)
    now = sent_at + 5 * 3_600_000
    sized = size_signal(
        symbol="BTCUSDT",
        side="long",
        entry=Decimal("100"),
        stop=Decimal("90"),
        balance=Decimal("1000"),
        risk_pct=Decimal("2"),
        step_size=Decimal("0.001"),
    )
    assert sized is not None
    sid = insert_signal(
        conn, sized, status="sent", created_at_ms=sent_at, expires_at_ms=sent_at + 1, sent_at_ms=sent_at
    )
    # Bar that gaps through the stop (open below stop).
    bar_ts = sent_at + 3_600_000
    upsert_market_technicals(
        conn,
        [
            {
                "timestamp": bar_ts,
                "symbol": "BTCUSDT",
                "timeframe": "1h",
                "open": 85.0,
                "high": 95.0,
                "low": 84.0,
                "close": 90.0,
                "volume": 1.0,
            }
        ],
    )
    # resting_stop_fill on a paper-shaped position with the same bars.
    pos = {
        "symbol": "BTCUSDT",
        "side": "long",
        "stop_price": 90.0,
        "entry_ts": sent_at,
        "stop_moved_ts": sent_at,
    }
    hit = resting_stop_fill(conn, pos, mid=None, until_ms=now, timeframe="1h")
    assert hit is not None
    assert hit.fill_mid == gap_aware_stop_fill(side="long", stop_f=90.0, open_px=85.0, low=84.0, high=95.0)

    resolved = resolve_open_tracks(conn, now_ms=now)
    assert resolved["n"] == 1
    row = conn.execute(
        "SELECT exit_reason, exit_px, net_r, fee_usdt FROM assist_track WHERE signal_id=?",
        (sid,),
    ).fetchone()
    assert row is not None
    assert row[0] == "stop"
    assert float(row[1]) == 85.0
    assert row[3] is not None and float(row[3]) > 0
    conn.close()


def test_weekly_summary_includes_funding(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "week.db")
    day = _ms_bangkok(2026, 10, 27, 12, 0)  # Tuesday
    week_start = _ms_bangkok(2026, 10, 26, 0, 0)  # Monday
    income = [
        {
            "symbol": "BTCUSDT",
            "incomeType": "REALIZED_PNL",
            "income": "-10",
            "time": week_start + 1000,
        },
        {
            "symbol": "BTCUSDT",
            "incomeType": "FUNDING_FEE",
            "income": "-1.5",
            "time": week_start + 2000,
        },
        {
            "symbol": "BTCUSDT",
            "incomeType": "COMMISSION",
            "income": "-0.5",
            "time": week_start + 3000,
        },
        {
            "symbol": "AAAUSDT",
            "incomeType": "REALIZED_PNL",
            "income": "3",
            "time": week_start + 4000,
        },
    ]
    real = summarize_income(income)
    assert real["universe_usdt"] == -12.0
    assert real["realized_pnl"] == -10.0
    assert real["funding"] == -1.5
    assert real["commission"] == -0.5
    assert real["other_usdt"] == 3.0
    assert real["realized_n"] == 2
    fake = FakeFutures(balance=Decimal("1000"), income=income)
    sent, send = _send_capture()
    payload = run_assist_weekly(conn, futures=fake, now_ms_value=day, send=send)
    assert "จักรวาล" not in payload["text"]
    assert "กำไรปิด (REALIZED_PNL)" in payload["text"]
    assert "funding" in payload["text"]
    assert "ค่าธรรมเนียม (COMMISSION)" in payload["text"]
    assert "สุทธิ" in payload["text"]
    assert "เหรียญนอกรายการ" in payload["text"]
    assert "-12.00" in payload["text"] or "-12" in payload["text"]
    # Sample line shape for the report.
    sample = format_weekly_summary(
        counts={"sent": 1, "skipped": 1, "taken": 0},
        virtual={
            "n": 3,
            "win_rate": 0.0,
            "total_r": -1.0,
            "mean_r": -1.0,
            "total_usdt": -20.0,
            "exit_stop_original": 1,
            "exit_stop_trailed": 0,
            "exit_target_2r": 1,
            "exit_analysis": 1,
        },
        real=real,
        closed_all=1,
    )
    assert "ยังไม่พิสูจน์ edge — ตัวอย่าง 1 ไม้" in sample
    assert "ปิดที่ stop เดิม 1 · stop ที่เลื่อนแล้ว 0 · ถึง 2R 1 · ปิดตามสัญญาณ 1" in sample
    assert "-1.00R" in sample or "-1.0R" in sample
    conn.close()


def test_weekly_stop_split_original_vs_trailed() -> None:
    from jarvise_assist.weekly import format_weekly_summary, summarize_virtual

    rows = [
        {"exit_reason": "stop", "gross_r": -1.0, "net_r": -1.04, "pnl_usdt": -10.0},
        {"exit_reason": "stop", "gross_r": -1.0, "net_r": -1.03, "pnl_usdt": -10.0},
        {"exit_reason": "stop", "gross_r": 0.586, "net_r": 0.507, "pnl_usdt": 5.0},
        {"exit_reason": "stop", "gross_r": 0.079, "net_r": 0.058, "pnl_usdt": 0.5},
        {"exit_reason": "stop", "gross_r": 0.135, "net_r": 0.081, "pnl_usdt": 0.8},
        {"exit_reason": "stop", "gross_r": 0.051, "net_r": 0.031, "pnl_usdt": 0.3},
    ]
    virtual = summarize_virtual(rows)
    assert virtual["exit_stop_original"] == 2
    assert virtual["exit_stop_trailed"] == 4
    assert abs(virtual["win_rate"] - (4 / 6)) < 1e-9
    text = format_weekly_summary(
        counts={"sent": 6, "skipped": 0, "taken": 0},
        virtual=virtual,
        real={
            "realized_pnl": 0.0,
            "funding": 0.0,
            "commission": 0.0,
            "universe_usdt": 0.0,
            "other_usdt": 0.0,
        },
        closed_all=6,
    )
    assert "ชนะ 67%" in text
    assert "ปิดที่ stop เดิม 2 · stop ที่เลื่อนแล้ว 4 · ถึง 2R 0 · ปิดตามสัญญาณ 0" in text
    assert "ยังไม่พิสูจน์ edge — ตัวอย่าง 6 ไม้" in text


def _three_losses(now_ms: int) -> list[dict[str, Any]]:
    return [
        {"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "income": "-5", "time": now_ms - 3000},
        {"symbol": "ETHUSDT", "incomeType": "REALIZED_PNL", "income": "-4", "time": now_ms - 2000},
        {"symbol": "SOLUSDT", "incomeType": "REALIZED_PNL", "income": "-3", "time": now_ms - 1000},
    ]


def test_three_losses_pause_until_next_0820_once(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "pause.db")
    now = _ms_bangkok(2026, 10, 27, 15, 0)
    fake = FakeFutures(balance=Decimal("1000"), income=_three_losses(now))
    sent, send = _send_capture()
    r1 = evaluate_pause(conn, futures=fake, now_ms=now, send=send)
    assert r1["paused"] is True and r1["reason"] == "streak"
    assert ensure_settings(conn)["paused_until_ms"] == next_0820_bangkok_ms(now)
    assert STREAK_PAUSE_TEXT in sent[0]["text"]
    n_msgs = len(sent)
    write_settings(conn, paused_until_ms=None)
    r2 = evaluate_pause(conn, futures=fake, now_ms=now + 1000, send=send)
    assert r2.get("paused") is False
    assert len(sent) == n_msgs  # same streak anchor — once
    conn.close()


def test_signal_and_morning_return_streak_pause(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "sigpause.db")
    now = _ms_bangkok(2026, 10, 27, 12, 0)
    write_settings(conn, paused_until_ms=now + 3_600_000)
    fake = FakeFutures(marks={"BTCUSDT": Decimal("100")})
    _seed_analysis(conn, symbol="BTCUSDT", action="long", confidence=0.7, invalidation=90.0, ts=now - 60_000)
    p = run_assist_signals(conn, futures=fake, now_ms_value=now, send=lambda *_a, **_k: True)
    assert p["skipped"] == "streak_pause"
    assert conn.execute("SELECT COUNT(*) FROM assist_signal").fetchone()[0] == 0

    # Held overnight should also skip morning inserts/sends when paused.
    sized = size_signal(
        symbol="BTCUSDT",
        side="long",
        entry=Decimal("100"),
        stop=Decimal("90"),
        balance=Decimal("1000"),
        risk_pct=Decimal("2"),
        step_size=Decimal("0.001"),
    )
    assert sized is not None
    insert_signal(conn, sized, status="held", created_at_ms=now, expires_at_ms=now + 10_000_000)
    m = run_assist_morning(conn, futures=fake, now_ms_value=now, send=lambda *_a, **_k: True)
    assert m["skipped"] == "streak_pause"
    conn.close()


def test_watch_still_warns_no_stop_while_paused(tmp_path: Path) -> None:
    from jarvise_assist.futures import FuturesPosition

    conn = open_db(tmp_path / "watchpause.db")
    day = _ms_bangkok(2026, 10, 27, 12, 0)
    write_settings(conn, paused_until_ms=day + 3_600_000)
    # Seed ATR candles for stop suggestion.
    for i in range(30):
        ts = day - (30 - i) * 4 * 3_600_000
        upsert_market_technicals(
            conn,
            [
                {
                    "timestamp": ts,
                    "symbol": "BTCUSDT",
                    "timeframe": "4h",
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.0,
                    "volume": 1.0,
                }
            ],
        )
        conn.execute(
            "UPDATE market_technicals SET atr_14=? WHERE symbol=? AND timeframe=? AND timestamp=?",
            (2.0, "BTCUSDT", "4h", ts),
        )
    conn.commit()
    pos = FuturesPosition(
        symbol="BTCUSDT",
        position_side="BOTH",
        position_amt=Decimal("1"),
        entry_price=Decimal("100"),
        mark_price=Decimal("100"),
        update_time_ms=day,
    )
    fake = FakeFutures(positions=[pos], orders=[], income=[])
    sent: list[str] = []
    run_assist_watch(conn, futures=fake, now_ms_value=day, send=lambda t: sent.append(t) or True)
    assert any("ไม่มี stop" in t for t in sent)
    conn.close()


def test_auto_resume_message_once(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "resume.db")
    now = _ms_bangkok(2026, 10, 28, 8, 20)
    write_settings(conn, paused_until_ms=now - 1)
    write_edge(conn, resume_alerted=False)
    sent, send = _send_capture()
    assert maybe_auto_resume(conn, now_ms=now, send=send) is True
    assert sent[0]["text"] == RESUME_TEXT
    assert maybe_auto_resume(conn, now_ms=now + 1000, send=send) is False
    assert len(sent) == 1
    conn.close()


def test_manual_resume_clears_pause(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "manual.db")
    now = _ms_bangkok(2026, 10, 27, 12, 0)
    write_settings(conn, paused_until_ms=now + 9_000_000, enabled=False)
    write_edge(conn, resume_alerted=False)
    # Toggle on via menu.
    sent, send = _send_capture()
    handle_menu_text(conn, BTN_TOGGLE, now_ms=now, send=send)  # enable was False → opens
    # First toggle when enabled=False opens; settings already enabled=False so toggle enables.
    assert ensure_settings(conn)["enabled"] is True
    assert ensure_settings(conn)["paused_until_ms"] is None
    assert get_edge(conn)["resume_alerted"] is True
    # Expired pause must not send auto-resume after manual clear.
    write_settings(conn, paused_until_ms=now - 1)
    # Manual clear already set resume_alerted; simulate clear_pause_manual again.
    clear_pause_manual(conn)
    sent2, send2 = _send_capture()
    maybe_auto_resume(conn, now_ms=now, send=send2)
    assert sent2 == []
    assert RESUME_TEXT not in "".join(s["text"] for s in sent)
    conn.close()


def test_weekly_loss_pauses_once_per_week(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "wloss.db")
    now = _ms_bangkok(2026, 10, 27, 18, 0)
    # Balance 1000, week net -70 → week start 1070, loss ~6.54% > 6%.
    income = [
        {
            "symbol": "BTCUSDT",
            "incomeType": "REALIZED_PNL",
            "income": "-70",
            "time": now - 1000,
        }
    ]
    fake = FakeFutures(balance=Decimal("1000"), income=income)
    sent, send = _send_capture()
    r1 = evaluate_pause(conn, futures=fake, now_ms=now, send=send)
    assert r1["paused"] is True and r1["reason"] == "week_loss"
    assert "ขาดทุนสัปดาห์นี้" in sent[0]["text"]
    # Clear pause to re-evaluate same week — must not fire again.
    write_settings(conn, paused_until_ms=None)
    n = len(sent)
    r2 = evaluate_pause(conn, futures=fake, now_ms=now + 60_000, send=send)
    assert r2.get("paused") is False
    assert len(sent) == n
    # Sample pause line.
    assert "6%" in week_loss_pause_text(Decimal("6.5")) or "6.5" in week_loss_pause_text(Decimal("6.5"))
    conn.close()


def test_open_positions_menu_sample(tmp_path: Path) -> None:
    from jarvise_assist.futures import FuturesPosition

    conn = open_db(tmp_path / "pos.db")
    day = _ms_bangkok(2026, 10, 27, 12, 0)
    sized = size_signal(
        symbol="BTCUSDT",
        side="long",
        entry=Decimal("100"),
        stop=Decimal("90"),
        balance=Decimal("1000"),
        risk_pct=Decimal("2"),
        step_size=Decimal("0.001"),
    )
    assert sized is not None
    insert_signal(conn, sized, status="sent", created_at_ms=day, expires_at_ms=day + 10**9, sent_at_ms=day)
    pos = FuturesPosition(
        symbol="BTCUSDT",
        position_side="BOTH",
        position_amt=Decimal("1"),
        entry_price=Decimal("100"),
        mark_price=Decimal("105"),
        update_time_ms=day,
        liquidation_price=Decimal("80"),
    )
    other = FuturesPosition(
        symbol="PEPEUSDT",
        position_side="BOTH",
        position_amt=Decimal("1000"),
        entry_price=Decimal("0.001"),
        mark_price=Decimal("0.001"),
        update_time_ms=day,
    )
    fake = FakeFutures(
        positions=[pos, other],
        ticks={"BTCUSDT": Decimal("1"), "PEPEUSDT": Decimal("0.00000001")},
        steps={"BTCUSDT": Decimal("0.001"), "PEPEUSDT": Decimal("1")},
        orders=[
            {
                "symbol": "BTCUSDT",
                "type": "STOP_MARKET",
                "side": "SELL",
                "stopPrice": "92",
                "reduceOnly": True,
                "positionSide": "BOTH",
                "workingType": "MARK_PRICE",
            },
            {
                "symbol": "BTCUSDT",
                "type": "TAKE_PROFIT_MARKET",
                "side": "SELL",
                "stopPrice": "120",
                "reduceOnly": True,
                "positionSide": "BOTH",
                "workingType": "MARK_PRICE",
            },
        ],
    )
    text = render_open_positions(conn, futures=fake, now_ms=day)
    assert "📋 ไม้ที่ถืออยู่" in text
    assert "BTCUSDT LONG" in text
    assert "ยังไม่ปิด:" in text and "USDT · ≈" in text
    assert "stop ที่ตั้งไว้ 92 · trigger Mark" in text
    assert "ถ้าโดน stop: ล็อก ≈" in text
    assert "TP 120 · trigger Mark" in text
    assert "liquidation 80" in text
    assert "PEPEUSDT · no data, no advice" in text
    assert risk_button_label(2.0).startswith("⚙️")
    conn.close()


def test_open_positions_mark_and_liquidation_use_tick(tmp_path: Path) -> None:
    from jarvise_assist.futures import FuturesPosition

    conn = open_db(tmp_path / "tick_pos.db")
    day = _ms_bangkok(2026, 10, 27, 12, 0)
    pos = FuturesPosition(
        symbol="DOGEUSDT",
        position_side="BOTH",
        position_amt=Decimal("-259"),
        entry_price=Decimal("0.07527"),
        mark_price=Decimal("0.07543363"),
        update_time_ms=day,
        liquidation_price=Decimal("0.14953253"),
    )
    fake = FakeFutures(
        positions=[pos],
        orders=[],
        ticks={"DOGEUSDT": Decimal("0.00001")},
    )
    text = render_open_positions(conn, futures=fake, now_ms=day)
    assert "mark 0.07543" in text
    assert "liquidation 0.14953" in text
    assert "0.07543363" not in text
    assert "0.14953253" not in text
    conn.close()


def test_open_positions_mark_falls_back_when_filters_fail(tmp_path: Path) -> None:
    from jarvise_assist.futures import FuturesPosition, SymbolFilters

    conn = open_db(tmp_path / "filt_fail.db")
    day = _ms_bangkok(2026, 10, 27, 12, 0)
    pos = FuturesPosition(
        symbol="DOGEUSDT",
        position_side="BOTH",
        position_amt=Decimal("-259"),
        entry_price=Decimal("0.07527"),
        mark_price=Decimal("0.07543363"),
        update_time_ms=day,
        liquidation_price=Decimal("0.14953253"),
    )
    fake = FakeFutures(positions=[pos], orders=[], ticks={"DOGEUSDT": Decimal("0.00001")})

    def boom(symbol: str) -> SymbolFilters:
        raise RuntimeError("exchangeInfo down")

    fake.symbol_filters = boom  # type: ignore[method-assign]
    text = render_open_positions(conn, futures=fake, now_ms=day)
    assert "mark 0.07543363" in text
    assert "liquidation 0.14953253" in text
    conn.close()


def test_open_positions_stop_coverage_samples(tmp_path: Path) -> None:
    from jarvise_assist.futures import FuturesPosition

    conn = open_db(tmp_path / "cov.db")
    day = _ms_bangkok(2026, 10, 27, 12, 0)
    tick = {"DOGEUSDT": Decimal("0.00001"), "BTCUSDT": Decimal("0.1"), "PEPEUSDT": Decimal("0.00000001")}

    def render(pos: FuturesPosition, orders: list[dict[str, Any]]) -> str:
        fake = FakeFutures(positions=[pos], orders=orders, ticks=tick, steps={"DOGEUSDT": Decimal("1")})
        return render_open_positions(conn, futures=fake, now_ms=day)

    full = FuturesPosition(
        symbol="DOGEUSDT",
        position_side="BOTH",
        position_amt=Decimal("259"),
        entry_price=Decimal("0.07527"),
        mark_price=Decimal("0.07543"),
        update_time_ms=day,
        liquidation_price=Decimal("0.06000"),
    )
    full_text = render(
        full,
        [
            {
                "symbol": "DOGEUSDT",
                "type": "STOP_MARKET",
                "side": "SELL",
                "triggerPrice": "0.07400",
                "origQty": "259",
                "reduceOnly": True,
                "positionSide": "BOTH",
                "workingType": "MARK_PRICE",
            }
        ],
    )
    assert "stop ที่ตั้งไว้ 0.07400 · trigger Mark" in full_text
    assert "⚠️ ยังไม่มี stop" not in full_text

    trail = render(
        full,
        [
            {
                "symbol": "DOGEUSDT",
                "type": "TRAILING_STOP_MARKET",
                "side": "SELL",
                "origQty": "259",
                "reduceOnly": True,
                "positionSide": "BOTH",
            }
        ],
    )
    assert "stop แบบ trailing" in trail
    assert "ถ้าโดน stop" not in trail

    partial = render(
        full,
        [
            {
                "symbol": "DOGEUSDT",
                "type": "STOP_MARKET",
                "side": "SELL",
                "triggerPrice": "0.19710",
                "origQty": "100",
                "reduceOnly": True,
                "positionSide": "BOTH",
                "workingType": "MARK_PRICE",
            }
        ],
    )
    assert "stop ที่ตั้งไว้ 0.19710" in partial
    assert "⚠️ stop ครอบแค่ 100/259" in partial

    none = render(full, [])
    assert "⚠️ ยังไม่มี stop — ตั้ง Stop-Market (Reduce-Only, Mark) ทันที" in none
    assert "stop แนะนำไม่มี" not in none

    outsider = FuturesPosition(
        symbol="PEPEUSDT",
        position_side="BOTH",
        position_amt=Decimal("1000"),
        entry_price=Decimal("0.00001"),
        mark_price=Decimal("0.00001"),
        update_time_ms=day,
    )
    bare = render(outsider, [])
    assert "PEPEUSDT · no data, no advice" in bare
    assert "⚠️ ยังไม่มี stop — ตั้ง Stop-Market (Reduce-Only, Mark) ทันที" in bare
    assert "mark " not in bare

    covered_out = render(
        outsider,
        [
            {
                "symbol": "PEPEUSDT",
                "type": "STOP_MARKET",
                "side": "SELL",
                "stopPrice": "0.000009",
                "origQty": "1000",
                "reduceOnly": True,
                "positionSide": "BOTH",
            }
        ],
    )
    assert "PEPEUSDT · no data, no advice" in covered_out
    assert "⚠️" not in covered_out
    conn.close()


def test_suggested_stop_uses_tick_not_sig_digits(tmp_path: Path) -> None:
    from jarvise_assist.futures import FuturesPosition
    from jarvise_assist.store import upsert_watch_position

    conn = open_db(tmp_path / "sug.db")
    day = _ms_bangkok(2026, 10, 27, 12, 0)

    def one(symbol: str, entry: str, stop: str, tick: str) -> str:
        upsert_watch_position(
            conn,
            {
                "symbol": symbol,
                "position_side": "BOTH",
                "side": "long",
                "entry": float(entry),
                "qty": 1.0,
                "in_universe": 1,
                "signal_id": None,
                "initial_stop": float(stop),
                "initial_stop_source": "freeze",
                "entry_time_ms": day,
                "no_stop_latched": 0,
                "last_seen_ms": day,
                "closed_at_ms": None,
            },
        )
        pos = FuturesPosition(
            symbol=symbol,
            position_side="BOTH",
            position_amt=Decimal("1"),
            entry_price=Decimal(entry),
            mark_price=Decimal(entry),
            update_time_ms=day,
        )
        fake = FakeFutures(
            positions=[pos],
            orders=[
                {
                    "symbol": symbol,
                    "type": "STOP_MARKET",
                    "side": "SELL",
                    "stopPrice": stop,
                    "origQty": "1",
                    "reduceOnly": True,
                    "positionSide": "BOTH",
                    "workingType": "MARK_PRICE",
                }
            ],
            ticks={symbol: Decimal(tick)},
            steps={symbol: Decimal("0.001")},
        )
        return render_open_positions(conn, futures=fake, now_ms=day)

    btc = one("BTCUSDT", "105000", "104523.4", "0.1")
    assert "stop แนะนำ 104523.4 (freeze)" in btc
    assert "104523 " not in btc
    assert "e-05" not in btc
    tiny = one("DOGEUSDT", "0.00002000", "0.00001234", "0.00000001")
    assert "stop แนะนำ 0.00001234 (freeze)" in tiny
    assert "e-05" not in tiny
    conn.close()


def test_r_falls_back_to_loss_side_placed_stop(tmp_path: Path) -> None:
    from jarvise_assist.futures import FuturesPosition

    conn = open_db(tmp_path / "rbase.db")
    day = _ms_bangkok(2026, 10, 27, 12, 0)

    def render(stop: str) -> str:
        pos = FuturesPosition(
            symbol="BTCUSDT",
            position_side="BOTH",
            position_amt=Decimal("1"),
            entry_price=Decimal("100"),
            mark_price=Decimal("105"),
            update_time_ms=day,
        )
        fake = FakeFutures(
            positions=[pos],
            orders=[
                {
                    "symbol": "BTCUSDT",
                    "type": "STOP_MARKET",
                    "side": "SELL",
                    "stopPrice": stop,
                    "origQty": "1",
                    "reduceOnly": True,
                    "positionSide": "BOTH",
                    "workingType": "MARK_PRICE",
                }
            ],
            ticks={"BTCUSDT": Decimal("0.1")},
            steps={"BTCUSDT": Decimal("0.001")},
        )
        return render_open_positions(conn, futures=fake, now_ms=day)

    loss = render("90")
    assert "(จาก stop จริง)" in loss
    assert "R unknown" not in loss
    profit = render("110")
    assert "R unknown" in profit
    assert "จาก stop จริง" not in profit
    conn.close()


def test_signed_usdt_and_r_drop_minus_on_rounded_zero() -> None:
    assert _signed_usdt(-0.0026) == "0.00"
    assert _signed_usdt(0.004) == "0.00"
    assert _signed_usdt(-1.25) == "-1.25"
    assert _signed_usdt(1.25) == "+1.25"
    assert _signed_r_one(-0.04) == "0.0R"
    assert _signed_r_one(0.04) == "0.0R"
    assert _signed_r_one(-1.2) == "-1.2R"
    assert _signed_r_one(1.2) == "+1.2R"
    from jarvise_assist.menu import _signed_r_two

    assert _signed_r_two(-0.004) == "0.00R"
    assert _signed_r_two(0.004) == "0.00R"
    assert _signed_r_two(-1.2) == "-1.20R"


def test_open_positions_lock_r_vs_unrealized_and_trigger_mismatch(tmp_path: Path) -> None:
    from jarvise_assist.futures import FuturesPosition
    from jarvise_assist.store import upsert_watch_position

    conn = open_db(tmp_path / "ada_pos.db")
    day = _ms_bangkok(2026, 10, 27, 12, 0)
    # ADA SHORT: entry 0.2025, freeze stop 0.212078, mark 0.1909, protective 0.1971
    # R distance from freeze ≈ 0.009578; unrealized ≈ +1.21R; lock at 0.1971 ≈ +0.56R
    upsert_watch_position(
        conn,
        {
            "symbol": "ADAUSDT",
            "position_side": "BOTH",
            "side": "short",
            "entry": 0.2025,
            "qty": 1000.0,
            "in_universe": 1,
            "signal_id": None,
            "initial_stop": 0.212078,
            "initial_stop_source": "freeze",
            "entry_time_ms": day,
            "no_stop_latched": 0,
            "last_seen_ms": day,
            "closed_at_ms": None,
        },
    )
    pos = FuturesPosition(
        symbol="ADAUSDT",
        position_side="BOTH",
        position_amt=Decimal("-1000"),
        entry_price=Decimal("0.2025"),
        mark_price=Decimal("0.1909"),
        update_time_ms=day,
    )
    fake = FakeFutures(
        positions=[pos],
        ticks={"ADAUSDT": Decimal("0.0001")},
        steps={"ADAUSDT": Decimal("1")},
        orders=[
            {
                "symbol": "ADAUSDT",
                "type": "STOP_MARKET",
                "side": "BUY",
                "stopPrice": "0.1971",
                "reduceOnly": True,
                "positionSide": "BOTH",
                "workingType": "CONTRACT_PRICE",
            },
            {
                "symbol": "ADAUSDT",
                "type": "TAKE_PROFIT_MARKET",
                "side": "BUY",
                "stopPrice": "0.1833",
                "reduceOnly": True,
                "positionSide": "BOTH",
                "workingType": "MARK_PRICE",
            },
        ],
    )
    text = render_open_positions(conn, futures=fake, now_ms=day)
    assert "ADAUSDT SHORT" in text
    assert "ยังไม่ปิด: +11.60 USDT · ≈ +1.2R" in text
    assert "stop ที่ตั้งไว้ 0.1971 · trigger Last" in text
    assert "ล็อก ≈ +0.56R" in text
    assert "⚠️ stop กับ TP คนละ trigger (Last / Mark)" in text
    conn.close()


def test_empty_chat_id_does_not_start_listener_and_ignores_updates(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    listen_mod._listener_started = False
    assert start_listener_thread(db_path=tmp_path / "empty-chat.db") is None
    assert listen_mod._listener_started is False

    conn = open_db(tmp_path / "empty-chat.db")
    sent, send = _send_capture()
    msg = {"update_id": 1, "message": {"chat": {"id": 999}, "text": BTN_TOGGLE}}
    assert process_update(conn, msg, send=send)["ignored"] == "no_owner_chat"
    cb = {
        "update_id": 2,
        "callback_query": {
            "id": "cq",
            "data": "risk:2",
            "message": {"chat": {"id": 999}, "message_id": 1},
        },
    }
    assert process_update(conn, cb, send=send)["ignored"] == "no_owner_chat"
    assert sent == []
    conn.close()


def _seed_calm_1h_bar(conn: Any, *, symbol: str, bar_ts: int) -> None:
    """Bar that stays inside a 100/90 long stop and short of a 120 target."""
    upsert_market_technicals(
        conn,
        [
            {
                "timestamp": bar_ts,
                "symbol": symbol,
                "timeframe": "1h",
                "open": 100.0,
                "high": 105.0,
                "low": 95.0,
                "close": 101.0,
                "volume": 1.0,
            }
        ],
    )


def test_flat_analysis_available_before_bar_close_exits(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "asof-flat.db")
    sent_at = _ms_bangkok(2026, 10, 27, 8, 0)
    bar_ts = sent_at + 3_600_000
    close_ms = bar_ts + 3_600_000
    sized = size_signal(
        symbol="BTCUSDT",
        side="long",
        entry=Decimal("100"),
        stop=Decimal("90"),
        balance=Decimal("1000"),
        risk_pct=Decimal("2"),
        step_size=Decimal("0.001"),
    )
    assert sized is not None
    sid = insert_signal(
        conn, sized, status="sent", created_at_ms=sent_at, expires_at_ms=sent_at + 1, sent_at_ms=sent_at
    )
    _seed_calm_1h_bar(conn, symbol="BTCUSDT", bar_ts=bar_ts)
    # Available exactly at bar close: timestamp + 5h == close_ms.
    _seed_analysis(
        conn,
        symbol="BTCUSDT",
        action="flat",
        confidence=0.0,
        invalidation=None,
        ts=close_ms - 5 * 3_600_000,
    )
    resolved = resolve_open_tracks(conn, now_ms=close_ms)
    assert resolved["n"] == 1
    row = conn.execute("SELECT exit_reason FROM assist_track WHERE signal_id=?", (sid,)).fetchone()
    assert row is not None and row[0] == "flat"

    # One millisecond later: not yet available → no analysis exit.
    conn2 = open_db(tmp_path / "asof-flat-late.db")
    sid2 = insert_signal(
        conn2, sized, status="sent", created_at_ms=sent_at, expires_at_ms=sent_at + 1, sent_at_ms=sent_at
    )
    _seed_calm_1h_bar(conn2, symbol="BTCUSDT", bar_ts=bar_ts)
    _seed_analysis(
        conn2,
        symbol="BTCUSDT",
        action="flat",
        confidence=0.0,
        invalidation=None,
        ts=close_ms - 5 * 3_600_000 + 1,
    )
    assert resolve_open_tracks(conn2, now_ms=close_ms)["n"] == 0
    open_row = conn2.execute("SELECT exit_at_ms FROM assist_track WHERE signal_id=?", (sid2,)).fetchone()
    assert open_row is not None and open_row[0] is None
    conn.close()
    conn2.close()


def test_historical_flat_exit_when_newest_analysis_is_long(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "asof-hist.db")
    sent_at = _ms_bangkok(2026, 10, 27, 8, 0)
    bar_ts = sent_at + 3_600_000
    close_ms = bar_ts + 3_600_000
    sized = size_signal(
        symbol="BTCUSDT",
        side="long",
        entry=Decimal("100"),
        stop=Decimal("90"),
        balance=Decimal("1000"),
        risk_pct=Decimal("2"),
        step_size=Decimal("0.001"),
    )
    assert sized is not None
    sid = insert_signal(
        conn, sized, status="sent", created_at_ms=sent_at, expires_at_ms=sent_at + 1, sent_at_ms=sent_at
    )
    _seed_calm_1h_bar(conn, symbol="BTCUSDT", bar_ts=bar_ts)
    # Older flat is available at the bar close.
    _seed_analysis(
        conn,
        symbol="BTCUSDT",
        action="flat",
        confidence=0.0,
        invalidation=None,
        ts=close_ms - 5 * 3_600_000,
    )
    # Newer long is the overall newest but not yet available (lookahead trap).
    _seed_analysis(
        conn,
        symbol="BTCUSDT",
        action="long",
        confidence=0.8,
        invalidation=90.0,
        ts=close_ms - 5 * 3_600_000 + 60_000,
    )
    resolved = resolve_open_tracks(conn, now_ms=close_ms)
    assert resolved["n"] == 1
    row = conn.execute("SELECT exit_reason FROM assist_track WHERE signal_id=?", (sid,)).fetchone()
    assert row is not None and row[0] == "flat"
    conn.close()


def test_split_fill_counts_as_one_loss(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "split.db")
    now = _ms_bangkok(2026, 10, 27, 15, 0)
    # Three fill legs of one stop-out within 120s — one close event, no streak.
    income = [
        {"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "income": "-2", "time": now - 2000},
        {"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "income": "-2", "time": now - 1500},
        {"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "income": "-1", "time": now - 1000},
    ]
    fake = FakeFutures(balance=Decimal("1000"), income=income)
    sent, send = _send_capture()
    r = evaluate_pause(conn, futures=fake, now_ms=now, send=send)
    assert r.get("paused") is False
    assert sent == []
    conn.close()


def test_three_separate_losing_trades_pause(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "sep.db")
    now = _ms_bangkok(2026, 10, 27, 15, 0)
    # Same symbol, more than 120s apart → three close events.
    income = [
        {"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "income": "-5", "time": now - 400_000},
        {"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "income": "-4", "time": now - 250_000},
        {"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "income": "-3", "time": now - 100_000},
    ]
    fake = FakeFutures(balance=Decimal("1000"), income=income)
    sent, send = _send_capture()
    r = evaluate_pause(conn, futures=fake, now_ms=now, send=send)
    assert r["paused"] is True and r["reason"] == "streak"
    assert STREAK_PAUSE_TEXT in sent[0]["text"]
    conn.close()


def test_midweek_transfer_does_not_change_loss_pct(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "xfer.db")
    now = _ms_bangkok(2026, 10, 27, 18, 0)
    # Without transfer: bal 1000, trading -70 → week start 1070, loss ~6.54%.
    base_income = [{"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "income": "-70", "time": now - 2000}]
    with_xfer = [
        *base_income,
        {"symbol": "", "incomeType": "TRANSFER", "income": "50", "time": now - 1000},
    ]
    sent_a, send_a = _send_capture()
    r_a = evaluate_pause(
        conn, futures=FakeFutures(balance=Decimal("1000"), income=base_income), now_ms=now, send=send_a
    )
    assert r_a["paused"] is True and r_a["reason"] == "week_loss"
    pct_a = float(r_a["loss_pct"])
    write_settings(conn, paused_until_ms=None)
    write_edge(conn, week_pause_id=None, resume_alerted=False)
    sent_b, send_b = _send_capture()
    # bal 1050 includes the +50 transfer; week start must still be 1070.
    r_b = evaluate_pause(
        conn, futures=FakeFutures(balance=Decimal("1050"), income=with_xfer), now_ms=now, send=send_b
    )
    assert r_b["paused"] is True and r_b["reason"] == "week_loss"
    assert abs(float(r_b["loss_pct"]) - pct_a) < 1e-9
    conn.close()
