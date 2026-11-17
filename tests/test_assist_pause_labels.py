"""Pause resume Thai labels + block_reason mapping (Task C)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from tests.test_assist import FakeFutures, _ms_bangkok, _seed_analysis
from tests.test_assist_listen import _send_capture, _three_losses

from jarvise_assist.journal import _not_in_streak_pause, collect_auto_checks
from jarvise_assist.menu import render_anything_now
from jarvise_assist.pause import (
    BOT_ENTRIES_ONLY_TH,
    bangkok_resume_label,
    day_loss_deeper_pause_text,
    day_loss_pause_text,
    evaluate_pause,
    format_paused_until,
    next_0820_bangkok_ms,
    streak_pause_text,
    week_loss_pause_text,
)
from jarvise_assist.quick_card import _refusal_week_loss, build_quick_card
from jarvise_assist.signals import run_assist_morning, run_assist_signals
from jarvise_assist.sizing import size_signal
from jarvise_assist.store import (
    PAUSE_REASONS,
    ensure_settings,
    pause_block_reason,
    record_blocked_signal,
    write_edge,
    write_settings,
)
from jarvise_assist.weekly import _format_blocked_line
from jarvise_ingest.db import open_db

BANGKOK = timezone(timedelta(hours=7), name="Asia/Bangkok")
# Monday 2026-11-09 Bangkok
MON = 2026, 11, 9


def _ms_bkk(
    year: int,
    month: int,
    day: int,
    hour: int,
    minute: int = 0,
    second: int = 0,
    microsecond: int = 0,
) -> int:
    dt = datetime(year, month, day, hour, minute, second, microsecond, tzinfo=BANGKOK)
    return int(dt.timestamp() * 1000)


def _day_trade(conn: Any, *, oid: str, exit_ms: int, net: float) -> None:
    conn.execute(
        """
        INSERT INTO assist_real_trade (
            symbol, side, entry_order_id, status, updated_at_ms, intent_id,
            entry_time_ms, loss_at_stop, exit_time_ms, exit_kind, net_r_real
        ) VALUES ('BTCUSDT', 'long', ?, 'closed', ?, 'intent-bot', ?, 10.0, ?, 'stop', ?)
        """,
        (oid, exit_ms, exit_ms - 3_600_000, exit_ms, net),
    )
    conn.commit()


def _week_futures(net: float, *, at_ms: int) -> FakeFutures:
    trading_net = Decimal(str(net))
    return FakeFutures(
        balance=Decimal("1000") + trading_net,
        income=[
            {
                "symbol": "BTCUSDT",
                "incomeType": "REALIZED_PNL",
                "income": str(net),
                "time": at_ms - 1000,
            }
        ],
    )


# ---------------------------------------------------------------------------
# 1a. bangkok_resume_label
# ---------------------------------------------------------------------------


def test_bangkok_resume_label_today_tomorrow_and_far() -> None:
    day = MON
    cases_today = (
        _ms_bkk(*day, 0, 0),
        _ms_bkk(*day, 2, 30),
        _ms_bkk(*day, 8, 19, 59, 999_000),
    )
    for now in cases_today:
        until = next_0820_bangkok_ms(now)
        assert until == _ms_bangkok(*day, 8, 20)
        assert bangkok_resume_label(until, now_ms=now) == "วันนี้ 08:20"

    cases_tomorrow = (
        _ms_bkk(*day, 8, 20, 0, 0),
        _ms_bkk(*day, 15, 0),
        _ms_bkk(*day, 23, 59, 59),
    )
    for now in cases_tomorrow:
        until = next_0820_bangkok_ms(now)
        assert until == _ms_bangkok(2026, 11, 10, 8, 20)
        assert bangkok_resume_label(until, now_ms=now) == "พรุ่งนี้ 08:20"

    now = _ms_bkk(*day, 15, 0)
    far = _ms_bangkok(2026, 11, 12, 8, 20)  # Wed, two+ Bangkok days ahead of Mon
    assert bangkok_resume_label(far, now_ms=now) == "12/11 08:20"


# ---------------------------------------------------------------------------
# 1b. evaluate_pause Thai lines + until timing
# ---------------------------------------------------------------------------


def test_streak_pause_text_today_vs_tomorrow(tmp_path: Path) -> None:
    for hour, expected in ((2, "วันนี้"), (15, "พรุ่งนี้")):
        conn = open_db(tmp_path / f"streak_{hour}.db")
        now = _ms_bangkok(*MON, hour, 0)
        fake = FakeFutures(balance=Decimal("1000"), income=_three_losses(now))
        sent, send = _send_capture()
        r = evaluate_pause(conn, futures=fake, now_ms=now, send=send)
        until = next_0820_bangkok_ms(now)
        assert r["paused"] is True and r["reason"] == "streak"
        assert r["until_ms"] == until
        assert ensure_settings(conn)["paused_until_ms"] == until
        assert sent[0]["text"] == f"⏸ หยุดส่ง signal ถึง{expected} 08:20 — แพ้ติดกัน 3 ไม้"
        assert sent[0]["text"] == streak_pause_text(until_ms=until, now_ms=now)
        conn.close()


def test_week_loss_pause_text_today_vs_tomorrow(tmp_path: Path) -> None:
    # Monday 01:00 vs Monday 15:00; -50 USDT on 1000 bal at 1% risk → > 4R.
    for hour, expected in ((1, "วันนี้"), (15, "พรุ่งนี้")):
        conn = open_db(tmp_path / f"week_{hour}.db")
        now = _ms_bangkok(*MON, hour, 0)
        write_settings(conn, risk_pct=1.0)
        sent, send = _send_capture()
        r = evaluate_pause(conn, futures=_week_futures(-50, at_ms=now), now_ms=now, send=send)
        until = next_0820_bangkok_ms(now)
        assert r["paused"] is True and r["reason"] == "week_loss"
        assert r["until_ms"] == until
        loss_pct = Decimal(str(r["loss_pct"]))
        assert sent[0]["text"] == week_loss_pause_text(loss_pct, until_ms=until, now_ms=now)
        assert f"ถึง{expected} 08:20" in sent[0]["text"]
        assert "ขาดทุนสัปดาห์นี้" in sent[0]["text"]
        conn.close()


def test_day_loss_pause_text_today_vs_tomorrow(tmp_path: Path) -> None:
    for hour, expected in ((3, "วันนี้"), (15, "พรุ่งนี้")):
        conn = open_db(tmp_path / f"day_{hour}.db")
        now = _ms_bangkok(*MON, hour, 0)
        # Closed between 00:00 and now so it counts in the Bangkok day sum.
        _day_trade(conn, oid=f"t{hour}", exit_ms=now - 60_000, net=-3.0)
        sent, send = _send_capture()
        r = evaluate_pause(
            conn, futures=FakeFutures(balance=Decimal("1000"), income=[]), now_ms=now, send=send
        )
        until = next_0820_bangkok_ms(now)
        assert r["paused"] is True and r["reason"] == "day_loss"
        assert r["until_ms"] == until
        assert sent[0]["text"] == (f"⏸ หยุดส่ง signal ถึง{expected} 08:20 — วันนี้ -3.0R ({BOT_ENTRIES_ONLY_TH})")
        assert sent[0]["text"] == day_loss_pause_text(Decimal("-3.0"), until_ms=until, now_ms=now)
        conn.close()


# ---------------------------------------------------------------------------
# 1c. format_paused_until + quick refusal + render_anything_now
# ---------------------------------------------------------------------------


def test_format_paused_until_tomorrow_then_today(tmp_path: Path) -> None:
    mon_15 = _ms_bangkok(*MON, 15, 0)
    until = next_0820_bangkok_ms(mon_15)  # Tue 08:20
    assert until == _ms_bangkok(2026, 11, 10, 8, 20)

    mon_20 = _ms_bangkok(*MON, 20, 0)
    tue_01 = _ms_bangkok(2026, 11, 10, 1, 0)
    assert format_paused_until(until, now_ms=mon_20) == "หยุดพักถึงพรุ่งนี้ 08:20"
    assert format_paused_until(until, now_ms=tue_01) == "หยุดพักถึงวันนี้ 08:20"

    assert "พรุ่งนี้ 08:20" in _refusal_week_loss(until, now_ms=mon_20)
    assert "วันนี้ 08:20" in _refusal_week_loss(until, now_ms=tue_01)

    conn = open_db(tmp_path / "menu_pause.db")
    write_settings(conn, paused_until_ms=until, pause_reason="week_loss")
    write_edge(conn, week_pause_id="2026-11-09")
    fake = FakeFutures(marks={"BTCUSDT": Decimal("100")})
    text_mon, _ = render_anything_now(conn, futures=fake, now_ms=mon_20)
    assert text_mon == "หยุดพักถึงพรุ่งนี้ 08:20"
    text_tue, _ = render_anything_now(conn, futures=fake, now_ms=tue_01)
    assert text_tue == "หยุดพักถึงวันนี้ 08:20"

    card = build_quick_card(conn, fake, symbol="SUIUSDT", side="long", stop=Decimal("1.994"), now_ms=mon_20)
    assert card["reason"] == "week_loss_pause"
    assert format_paused_until(until, now_ms=mon_20) in card["text"]
    card2 = build_quick_card(conn, fake, symbol="SUIUSDT", side="long", stop=Decimal("1.994"), now_ms=tue_01)
    assert card2["reason"] == "week_loss_pause"
    assert format_paused_until(until, now_ms=tue_01) in card2["text"]
    conn.close()


# ---------------------------------------------------------------------------
# 2a–2b. block_reason mapping + record_blocked_signal
# ---------------------------------------------------------------------------


def test_pause_block_reason_map() -> None:
    assert pause_block_reason(None) == "streak_pause"
    assert pause_block_reason("unknown") == "streak_pause"
    for reason in PAUSE_REASONS:
        assert pause_block_reason(reason) == f"{reason}_pause"


def test_run_assist_signals_block_reason_per_pause_reason(tmp_path: Path) -> None:
    now = _ms_bangkok(*MON, 12, 0)
    until = now + 3_600_000
    for reason in (*PAUSE_REASONS, None):
        label = reason or "null"
        conn = open_db(tmp_path / f"sig_{label}.db")
        if reason is None:
            write_settings(conn, paused_until_ms=until)
        else:
            write_settings(conn, paused_until_ms=until, pause_reason=reason)
        fake = FakeFutures(marks={"BTCUSDT": Decimal("100")})
        _seed_analysis(
            conn, symbol="BTCUSDT", action="long", confidence=0.7, invalidation=90.0, ts=now - 60_000
        )
        p = run_assist_signals(conn, futures=fake, now_ms_value=now, send=lambda *_a, **_k: True)
        assert p["skipped"] == "streak_pause"
        assert p.get("pause_reason") == reason
        blocked = conn.execute(
            "SELECT t.block_reason FROM assist_signal s JOIN assist_track t ON t.signal_id = s.id"
        ).fetchall()
        assert len(blocked) == 1
        expected = "streak_pause" if reason is None else f"{reason}_pause"
        assert blocked[0][0] == expected

        m = run_assist_morning(conn, futures=fake, now_ms_value=now, send=lambda *_a, **_k: True)
        assert m["skipped"] == "streak_pause"
        assert m.get("pause_reason") == reason
        conn.close()


def test_record_blocked_accepts_new_pause_reasons(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "blocked_new.db")
    ts = _ms_bangkok(*MON, 12, 0)
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
    for i, reason in enumerate(("week_loss_pause", "day_loss_pause", "day_loss_deeper_pause")):
        sym_sized = size_signal(
            symbol=("BTCUSDT", "ETHUSDT", "SOLUSDT")[i],
            side="long",
            entry=Decimal("100"),
            stop=Decimal("90"),
            balance=Decimal("1000"),
            risk_pct=Decimal("2"),
            step_size=Decimal("0.001"),
        )
        assert sym_sized is not None
        sid = record_blocked_signal(conn, sym_sized, reason=reason, created_at_ms=ts)
        assert sid is not None
    conn.close()


# ---------------------------------------------------------------------------
# 2c. weekly blocked line order
# ---------------------------------------------------------------------------


def test_format_blocked_line_order_includes_new_keys() -> None:
    blocked = {
        "day_loss_deeper_pause": {"n": 1, "mean_r": -0.1, "funding_missing": 0},
        "risk_cap": {"n": 2, "mean_r": 0.2, "funding_missing": 0},
        "week_loss_pause": {"n": 1, "mean_r": -0.5, "funding_missing": 0},
        "streak_pause": {"n": 1, "mean_r": -0.8, "funding_missing": 0},
        "margin_cap": {"n": 1, "mean_r": 0.0, "funding_missing": 0},
        "day_loss_pause": {"n": 1, "mean_r": -0.3, "funding_missing": 0},
    }
    line = _format_blocked_line(blocked)
    assert line == (
        "ถ้าไม่บล็อก: risk_cap n=2 · Expectancy +0.20R · margin_cap n=1 · Expectancy 0.00R · "
        "streak_pause n=1 · Expectancy -0.80R · week_loss_pause n=1 · Expectancy -0.50R · "
        "day_loss_pause n=1 · Expectancy -0.30R · day_loss_deeper_pause n=1 · Expectancy -0.10R"
    )


# ---------------------------------------------------------------------------
# 2d. journal not_in_streak_pause uses pause_reason
# ---------------------------------------------------------------------------


def test_not_in_streak_pause_respects_pause_reason(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "journal_pause.db")
    anchor = _ms_bangkok(*MON, 10, 0)
    until = _ms_bangkok(2026, 11, 10, 8, 20)
    entry = _ms_bangkok(*MON, 12, 0)

    write_settings(conn, paused_until_ms=until, pause_reason="streak")
    write_edge(conn, streak_anchor_ms=anchor)
    assert _not_in_streak_pause(conn, entry) is False

    write_settings(conn, paused_until_ms=until, pause_reason="day_loss")
    write_edge(conn, streak_anchor_ms=anchor)
    assert _not_in_streak_pause(conn, entry) is True

    write_settings(conn, paused_until_ms=until, pause_reason="week_loss")
    write_edge(conn, streak_anchor_ms=anchor)
    assert _not_in_streak_pause(conn, entry) is True

    # Legacy NULL reason: keep the anchor-window rule.
    write_settings(conn, paused_until_ms=until)
    write_edge(conn, streak_anchor_ms=anchor)
    assert ensure_settings(conn)["pause_reason"] is None
    assert _not_in_streak_pause(conn, entry) is False

    write_settings(conn, paused_until_ms=None, pause_reason=None)
    write_edge(conn, streak_anchor_ms=None)
    assert _not_in_streak_pause(conn, entry) is True

    # collect_auto_checks key stays not_in_streak_pause
    trade = {
        "symbol": "BTCUSDT",
        "side": "long",
        "entry_time_ms": entry,
        "entry_order_id": "x",
        "planned_stop": 90.0,
        "exit_kind": "stop",
    }
    write_settings(conn, paused_until_ms=until, pause_reason="day_loss")
    write_edge(conn, streak_anchor_ms=anchor)
    checks = collect_auto_checks(conn, trade)
    assert "not_in_streak_pause" in checks
    assert checks["not_in_streak_pause"] is True
    conn.close()


def test_day_loss_deeper_text_uses_resume_label() -> None:
    now = _ms_bangkok(*MON, 15, 0)
    until = next_0820_bangkok_ms(now)
    text = day_loss_deeper_pause_text(Decimal("-4.0"), until_ms=until, now_ms=now)
    assert text == (f"⏸ หยุดส่ง signal ถึงพรุ่งนี้ 08:20 — วันนี้ -4.0R ลึกกว่าตอนเปิดต่ออีก 1R ({BOT_ENTRIES_ONLY_TH})")


def test_pause_texts_space_before_numeric_date() -> None:
    now = _ms_bangkok(*MON, 15, 0)
    far = _ms_bangkok(2026, 11, 12, 8, 20)
    assert bangkok_resume_label(far, now_ms=now) == "12/11 08:20"
    assert streak_pause_text(until_ms=far, now_ms=now) == "⏸ หยุดส่ง signal ถึง 12/11 08:20 — แพ้ติดกัน 3 ไม้"
    assert (
        week_loss_pause_text(Decimal("4.2"), until_ms=far, now_ms=now)
        == "⏸ หยุดส่ง signal ถึง 12/11 08:20 — ขาดทุนสัปดาห์นี้ 4.2%"
    )
    assert (
        day_loss_pause_text(Decimal("-3.0"), until_ms=far, now_ms=now)
        == f"⏸ หยุดส่ง signal ถึง 12/11 08:20 — วันนี้ -3.0R ({BOT_ENTRIES_ONLY_TH})"
    )
    assert (
        day_loss_deeper_pause_text(Decimal("-4.0"), until_ms=far, now_ms=now)
        == f"⏸ หยุดส่ง signal ถึง 12/11 08:20 — วันนี้ -4.0R ลึกกว่าตอนเปิดต่ออีก 1R ({BOT_ENTRIES_ONLY_TH})"
    )
    assert format_paused_until(far, now_ms=now) == "หยุดพักถึง 12/11 08:20"
    tomorrow = next_0820_bangkok_ms(now)
    assert streak_pause_text(until_ms=tomorrow, now_ms=now) == "⏸ หยุดส่ง signal ถึงพรุ่งนี้ 08:20 — แพ้ติดกัน 3 ไม้"
    assert format_paused_until(tomorrow, now_ms=now) == "หยุดพักถึงพรุ่งนี้ 08:20"
