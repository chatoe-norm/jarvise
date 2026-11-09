"""Daily 3R loss pause. Streak and week-loss stay as they are."""

from __future__ import annotations

import inspect
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.test_assist import FakeFutures, _ms_bangkok, _seed_analysis

from jarvise_assist import execute as execute_mod
from jarvise_assist.futures import FuturesPosition
from jarvise_assist.keyboard import BTN_TOGGLE
from jarvise_assist.menu import handle_menu_text
from jarvise_assist.pause import (
    BOT_ENTRIES_ONLY_TH,
    bangkok_day_id,
    bangkok_day_start_ms,
    evaluate_pause,
    is_paused,
    maybe_auto_resume,
    next_0820_bangkok_ms,
    next_midnight_bangkok_ms,
)
from jarvise_assist.signals import run_assist_morning, run_assist_signals
from jarvise_assist.sizing import round_level_to_tick, size_signal
from jarvise_assist.store import (
    ensure_settings,
    get_edge,
    insert_signal,
    upsert_watch_position,
    write_edge,
    write_settings,
)
from jarvise_assist.watch import run_assist_watch
from jarvise_ingest.db import open_db, schema_version, upsert_market_technicals

NOW = _ms_bangkok(2026, 11, 9, 15, 0)


def _send() -> tuple[list[str], Any]:
    sent: list[str] = []

    def send(text: str, reply_markup: dict[str, Any] | None = None, **_kwargs: Any) -> int:
        del reply_markup
        sent.append(text)
        return len(sent)

    return sent, send


def _pause_lines(sent: list[str]) -> list[str]:
    return [text for text in sent if "หยุดส่ง signal ถึงเที่ยงคืน" in text]


def _trade(
    conn: Any,
    *,
    oid: str,
    exit_ms: int,
    net: float,
    loss_at_stop: float | None = 10.0,
    status: str = "closed",
    side: str = "long",
    intent_id: str | None = "intent-bot",
    exit_kind: str = "stop",
    updated_at_ms: int | None = None,
    symbol: str = "BTCUSDT",
) -> None:
    conn.execute(
        """
        INSERT INTO assist_real_trade (
            symbol, side, entry_order_id, status, updated_at_ms, intent_id,
            entry_time_ms, loss_at_stop, exit_time_ms, exit_kind, net_r_real
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            symbol,
            side,
            oid,
            status,
            int(updated_at_ms if updated_at_ms is not None else exit_ms),
            intent_id,
            exit_ms - 3_600_000,
            loss_at_stop,
            exit_ms,
            exit_kind,
            net,
        ),
    )
    conn.commit()


def _futures() -> FakeFutures:
    return FakeFutures(balance=Decimal("1000"), income=[])


def test_day_sum_minus_2_9_does_not_pause(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "m29.db")
    yesterday = _ms_bangkok(2026, 11, 8, 22, 0)
    _trade(conn, oid="y", exit_ms=yesterday, net=-10.0)
    _trade(conn, oid="t", exit_ms=NOW - 60_000, net=-2.9)
    sent, send = _send()
    result = evaluate_pause(conn, futures=_futures(), now_ms=NOW, send=send)
    assert result["paused"] is False
    assert result["day_id"] == "2026-11-09"
    assert sent == []
    assert ensure_settings(conn)["paused_until_ms"] is None
    conn.close()


def test_day_sum_minus_3_pauses_until_next_midnight_once(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "m3.db")
    sent, send = _send()
    # exit_time_ms == now is outside [day start, now).
    _trade(conn, oid="edge", exit_ms=NOW, net=-3.0)
    early = evaluate_pause(conn, futures=_futures(), now_ms=NOW, send=send)
    assert early["paused"] is False
    assert sent == []

    start = bangkok_day_start_ms(NOW)
    conn.execute("UPDATE assist_real_trade SET exit_time_ms=? WHERE entry_order_id='edge'", (start,))
    conn.commit()
    first = evaluate_pause(conn, futures=_futures(), now_ms=NOW, send=send)
    midnight = next_midnight_bangkok_ms(NOW)
    assert first["paused"] is True
    assert first["reason"] == "day_loss"
    assert first["until_ms"] == midnight
    assert ensure_settings(conn)["paused_until_ms"] == midnight
    assert _pause_lines(sent) == [f"⏸ หยุดส่ง signal ถึงเที่ยงคืน 00:00 — วันนี้ -3.0R ({BOT_ENTRIES_ONLY_TH})"]
    assert get_edge(conn)["day_pause_id"] == "2026-11-09"

    again = evaluate_pause(conn, futures=_futures(), now_ms=NOW + 1_000, send=send)
    assert again["reason"] == "already"
    assert len(_pause_lines(sent)) == 1

    # Same -3R must not fire again after the pause clock is cleared.
    write_settings(conn, paused_until_ms=None)
    third = evaluate_pause(conn, futures=_futures(), now_ms=NOW + 2_000, send=send)
    assert third["paused"] is False
    assert len(_pause_lines(sent)) == 1
    conn.close()


def test_next_bangkok_day_resumes_and_counts_from_zero(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "next.db")
    sent, send = _send()
    _trade(conn, oid="d1", exit_ms=NOW - 60_000, net=-3.0)
    first = evaluate_pause(conn, futures=_futures(), now_ms=NOW, send=send)
    assert first["reason"] == "day_loss"
    next_day = _ms_bangkok(2026, 11, 10, 0, 0)
    assert first["until_ms"] == next_day
    assert is_paused(ensure_settings(conn), now_ms=next_day - 1) is True
    assert is_paused(ensure_settings(conn), now_ms=next_day) is False
    assert maybe_auto_resume(conn, now_ms=next_day, send=send) is True
    resumed = evaluate_pause(conn, futures=_futures(), now_ms=next_day + 1_000, send=send)
    assert resumed["paused"] is False
    assert resumed["day_id"] == "2026-11-10"
    assert float(resumed["net_r"]) == 0.0
    _trade(conn, oid="d2", exit_ms=next_day + 60_000, net=-2.9)
    later = evaluate_pause(conn, futures=_futures(), now_ms=next_day + 120_000, send=send)
    assert later["paused"] is False
    assert len(_pause_lines(sent)) == 1
    conn.close()


def test_win_offsets_losses(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "win.db")
    sent, send = _send()
    _trade(conn, oid="loss", exit_ms=NOW - 120_000, net=-4.0, side="long")
    _trade(conn, oid="win", exit_ms=NOW - 60_000, net=1.5, side="short", symbol="ETHUSDT")
    held = evaluate_pause(conn, futures=_futures(), now_ms=NOW, send=send)
    assert held["paused"] is False
    assert sent == []
    _trade(conn, oid="more", exit_ms=NOW - 30_000, net=-0.5, symbol="SOLUSDT")
    paused = evaluate_pause(conn, futures=_futures(), now_ms=NOW, send=send)
    assert paused["reason"] == "day_loss"
    assert "-3.0R" in sent[0]
    conn.close()


def test_unmatched_manual_and_missing_loss_are_ignored(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "skip.db")
    sent, send = _send()
    exit_ms = NOW - 60_000
    _trade(conn, oid="unmatched", exit_ms=exit_ms, net=-10.0, status="unmatched", side="buy", intent_id=None)
    _trade(
        conn,
        oid="hand",
        exit_ms=exit_ms,
        net=-10.0,
        side="long",
        intent_id=None,
        exit_kind="manual",
    )
    _trade(conn, oid="noloss", exit_ms=exit_ms, net=-10.0, loss_at_stop=None, intent_id="intent-bot")
    _trade(conn, oid="zeroloss", exit_ms=exit_ms, net=-10.0, loss_at_stop=0.0, intent_id="intent-bot")
    # Bot entry closed by hand still counts. Two -1.5 rows reach -3; the ignored rows do not.
    _trade(conn, oid="bot-hand-exit", exit_ms=exit_ms, net=-1.5, exit_kind="manual", intent_id="intent-a")
    quiet = evaluate_pause(conn, futures=_futures(), now_ms=NOW, send=send)
    assert quiet["paused"] is False
    _trade(conn, oid="bot-stop", exit_ms=exit_ms + 1_000, net=-1.5, intent_id="intent-b", symbol="ETHUSDT")
    paused = evaluate_pause(conn, futures=_futures(), now_ms=NOW, send=send)
    assert paused["reason"] == "day_loss"
    assert "-3.0R" in _pause_lines(sent)[0]
    conn.close()


def test_exit_time_not_sync_time_assigns_the_bangkok_day(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "sync.db")
    sent, send = _send()
    exit_ms = _ms_bangkok(2026, 11, 9, 23, 59)
    synced = _ms_bangkok(2026, 11, 10, 0, 2)
    _trade(conn, oid="late", exit_ms=exit_ms, net=-3.5, updated_at_ms=synced)
    new_day = evaluate_pause(conn, futures=_futures(), now_ms=synced, send=send)
    assert new_day["paused"] is False
    assert new_day["day_id"] == "2026-11-10"
    assert sent == []
    previous = evaluate_pause(conn, futures=_futures(), now_ms=exit_ms + 30_000, send=send)
    assert previous["reason"] == "day_loss"
    assert previous["day_id"] == "2026-11-09"
    assert get_edge(conn)["day_pause_id"] == "2026-11-09"
    conn.close()


def test_pause_at_2358_ends_at_midnight(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "mid.db")
    sent, send = _send()
    at = _ms_bangkok(2026, 11, 9, 23, 58)
    _trade(conn, oid="late", exit_ms=at - 60_000, net=-3.0)
    result = evaluate_pause(conn, futures=_futures(), now_ms=at, send=send)
    midnight = _ms_bangkok(2026, 11, 10, 0, 0)
    assert result["until_ms"] == midnight
    assert next_midnight_bangkok_ms(at) == midnight
    assert is_paused(ensure_settings(conn), now_ms=at) is True
    assert is_paused(ensure_settings(conn), now_ms=midnight) is False
    assert maybe_auto_resume(conn, now_ms=midnight, send=send) is True
    nxt = evaluate_pause(conn, futures=_futures(), now_ms=midnight + 1_000, send=send)
    assert nxt["paused"] is False
    assert nxt["day_id"] == "2026-11-10"
    assert bangkok_day_id(at) == "2026-11-09"
    conn.close()


def test_manual_resume_same_day_warns_and_does_not_repause(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "resume.db")
    sent, send = _send()
    _trade(conn, oid="t", exit_ms=NOW - 60_000, net=-3.2)
    first = evaluate_pause(conn, futures=_futures(), now_ms=NOW, send=send)
    assert first["reason"] == "day_loss"
    handle_menu_text(conn, BTN_TOGGLE, now_ms=NOW, send=send)
    opened = handle_menu_text(conn, BTN_TOGGLE, now_ms=NOW, send=send)
    assert "วันนี้ติดลบแล้ว -3.2R" in str(opened["text"])
    assert BOT_ENTRIES_ONLY_TH in str(opened["text"])
    assert ensure_settings(conn)["paused_until_ms"] is None
    assert ensure_settings(conn)["enabled"] is True
    edge = get_edge(conn)
    assert edge["day_pause_id"] == "2026-11-09"
    assert edge["day_resume_id"] == "2026-11-09"
    assert edge["day_resume_net_r"] == pytest.approx(-3.2)
    again = evaluate_pause(conn, futures=_futures(), now_ms=NOW + 1_000, send=send)
    assert again["paused"] is False
    assert len(_pause_lines(sent)) == 1
    conn.close()


def test_further_1r_after_manual_resume_repauses_once(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "deeper.db")
    sent, send = _send()
    _trade(conn, oid="base", exit_ms=NOW - 180_000, net=-3.2)
    assert evaluate_pause(conn, futures=_futures(), now_ms=NOW, send=send)["reason"] == "day_loss"
    handle_menu_text(conn, BTN_TOGGLE, now_ms=NOW, send=send)
    handle_menu_text(conn, BTN_TOGGLE, now_ms=NOW, send=send)
    _trade(conn, oid="not-yet", exit_ms=NOW - 120_000, net=-0.9, symbol="ETHUSDT")
    short = evaluate_pause(conn, futures=_futures(), now_ms=NOW, send=send)
    assert short["paused"] is False
    _trade(conn, oid="deeper", exit_ms=NOW - 60_000, net=-0.1, symbol="SOLUSDT")
    deeper = evaluate_pause(conn, futures=_futures(), now_ms=NOW, send=send)
    assert deeper["reason"] == "day_loss_deeper"
    assert deeper["until_ms"] == next_midnight_bangkok_ms(NOW)
    assert "ลึกกว่าตอนเปิดต่ออีก 1R" in _pause_lines(sent)[-1]
    assert "-4.2R" in _pause_lines(sent)[-1]
    assert get_edge(conn)["day_deeper_pause_id"] == "2026-11-09"
    repeat = evaluate_pause(conn, futures=_futures(), now_ms=NOW + 1_000, send=send)
    assert repeat["reason"] == "already"
    assert len([line for line in _pause_lines(sent) if "ลึกกว่า" in line]) == 1

    handle_menu_text(conn, BTN_TOGGLE, now_ms=NOW + 2_000, send=send)
    second = handle_menu_text(conn, BTN_TOGGLE, now_ms=NOW + 2_000, send=send)
    assert "วันนี้ติดลบแล้ว -4.2R" in str(second["text"])
    assert get_edge(conn)["day_resume_net_r"] == pytest.approx(-3.2)
    _trade(conn, oid="third", exit_ms=NOW - 10_000, net=-2.0, symbol="BNBUSDT")
    third = evaluate_pause(conn, futures=_futures(), now_ms=NOW + 3_000, send=send)
    assert third["paused"] is False
    assert len([line for line in _pause_lines(sent) if "ลึกกว่า" in line]) == 1
    conn.close()


def test_streak_and_week_loss_still_take_priority(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "prio.db")
    sent, send = _send()
    now = _ms_bangkok(2026, 10, 27, 15, 0)
    _trade(conn, oid="day", exit_ms=now - 60_000, net=-3.0)
    income = [
        {"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "income": "-5", "time": now - 3_000},
        {"symbol": "ETHUSDT", "incomeType": "REALIZED_PNL", "income": "-4", "time": now - 2_000},
        {"symbol": "SOLUSDT", "incomeType": "REALIZED_PNL", "income": "-3", "time": now - 1_000},
    ]
    streak = evaluate_pause(
        conn,
        futures=FakeFutures(balance=Decimal("1000"), income=income),
        now_ms=now,
        send=send,
    )
    assert streak["reason"] == "streak"
    assert streak["until_ms"] == next_0820_bangkok_ms(now)
    assert _pause_lines(sent) == []
    assert "แพ้ติดกัน 3 ไม้" in sent[0]

    write_settings(conn, paused_until_ms=None)
    day = evaluate_pause(
        conn,
        futures=FakeFutures(balance=Decimal("1000"), income=income),
        now_ms=now + 1_000,
        send=send,
    )
    assert day["reason"] == "day_loss"
    conn.close()

    conn = open_db(tmp_path / "week.db")
    sent, send = _send()
    _trade(conn, oid="day", exit_ms=now - 60_000, net=-3.0)
    week_income = [{"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "income": "-70", "time": now - 1_000}]
    week = evaluate_pause(
        conn,
        futures=FakeFutures(balance=Decimal("1000"), income=week_income),
        now_ms=now,
        send=send,
    )
    assert week["reason"] == "week_loss"
    assert week["until_ms"] == next_0820_bangkok_ms(now)
    assert "ขาดทุนสัปดาห์นี้" in sent[0]
    assert _pause_lines(sent) == []
    conn.close()


def test_day_loss_blocks_new_signals_not_exits(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "sig.db")
    now = _ms_bangkok(2026, 11, 9, 12, 0)
    _trade(conn, oid="t", exit_ms=now - 60_000, net=-3.0)
    _seed_analysis(
        conn,
        symbol="BTCUSDT",
        action="long",
        confidence=0.7,
        invalidation=90.0,
        ts=now - 60_000,
    )
    sent, send = _send()
    fake = FakeFutures(marks={"BTCUSDT": Decimal("100")}, income=[])
    payload = run_assist_signals(conn, futures=fake, now_ms_value=now, send=send)
    assert payload["skipped"] == "streak_pause"
    assert payload["pause"]["reason"] == "day_loss"
    assert _pause_lines(sent) == [f"⏸ หยุดส่ง signal ถึงเที่ยงคืน 00:00 — วันนี้ -3.0R ({BOT_ENTRIES_ONLY_TH})"]
    assert conn.execute("SELECT COUNT(*) FROM assist_signal WHERE status='sent'").fetchone()[0] == 0
    sized = size_signal(
        symbol="BTCUSDT",
        side="long",
        entry=Decimal("100"),
        stop=Decimal("90"),
        balance=Decimal("1000"),
        risk_pct=Decimal("1"),
        step_size=Decimal("0.001"),
    )
    assert sized is not None
    insert_signal(conn, sized, status="held", created_at_ms=now, expires_at_ms=now + 10_000_000)
    morning = run_assist_morning(conn, futures=fake, now_ms_value=now, send=send)
    assert morning["skipped"] == "streak_pause"
    assert "is_paused" in inspect.getsource(execute_mod.size_for_execution)
    assert "is_paused" in inspect.getsource(execute_mod._confirm_guards_entry)
    assert "is_paused" not in inspect.getsource(execute_mod.run_exit_sequence)
    assert "is_paused" not in inspect.getsource(execute_mod.run_move_stop_sequence)
    conn.close()


def test_open_position_stays_open_and_watch_advice_sends_while_day_paused(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "open.db")
    day = NOW
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
    _trade(conn, oid="loss", exit_ms=day - 60_000, net=-3.0, symbol="ETHUSDT")
    pos = FuturesPosition(
        symbol="BTCUSDT",
        position_side="BOTH",
        position_amt=Decimal("1"),
        entry_price=Decimal("100"),
        mark_price=Decimal("100"),
        update_time_ms=day,
    )
    fake = FakeFutures(positions=[pos], orders=[], income=[])
    sent, send = _send()
    payload = run_assist_watch(conn, futures=fake, now_ms_value=day, send=send)
    assert payload["pause"]["reason"] == "day_loss"
    assert payload["closed"] == []
    assert fake.positions == [pos]
    assert any(action.get("action") == "no_stop" for action in payload["actions"])
    assert any("ไม่มี stop" in text for text in sent)
    conn.close()


def test_watch_pauses_on_day_loss_in_the_same_poll_without_binance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = open_db(tmp_path / "poll.db")
    day = NOW
    upsert_watch_position(
        conn,
        {
            "symbol": "BTCUSDT",
            "position_side": "BOTH",
            "side": "long",
            "entry": 100.0,
            "qty": 1.0,
            "in_universe": 1,
            "signal_id": None,
            "initial_stop": 90.0,
            "initial_stop_source": "signal",
            "entry_time_ms": day - 3_600_000,
            "no_stop_latched": 0,
            "last_seen_ms": day - 3_600_000,
            "closed_at_ms": None,
        },
    )
    fake = FakeFutures(positions=[], orders=[], income=[])
    calls = {"n": 0, "at_sync": -1}

    def balance() -> Decimal:
        calls["n"] += 1
        return Decimal("1000")

    def income(**_kwargs: Any) -> list[dict[str, Any]]:
        calls["n"] += 1
        return []

    fake.usdt_wallet_balance = balance  # type: ignore[method-assign]
    fake.income_history = income  # type: ignore[method-assign]

    def sync(db: Any, _client: Any, *, since_ms: int, until_ms: int) -> None:
        del since_ms, until_ms
        calls["at_sync"] = calls["n"]
        _trade(db, oid="closed-now", exit_ms=day - 1_000, net=-3.0)

    monkeypatch.setattr("jarvise_assist.journal.sync_real_trades", sync)
    sent, send = _send()
    payload = run_assist_watch(conn, futures=fake, now_ms_value=day, send=send)
    assert calls["at_sync"] >= 0
    assert calls["n"] == calls["at_sync"]
    assert payload["pause"]["reason"] == "day_loss"
    assert payload["pause"]["until_ms"] == next_midnight_bangkok_ms(day)
    assert len(_pause_lines(sent)) == 1
    assert any(row["symbol"] == "BTCUSDT" for row in payload["closed"])
    conn.close()


def test_day_loss_migration_is_additive(tmp_path: Path) -> None:
    path = tmp_path / "mig.db"
    conn = open_db(path)
    write_edge(conn, streak_anchor_ms=111, week_pause_id="2026-10-26", resume_alerted=True)
    for col in ("day_deeper_pause_id", "day_resume_id", "day_resume_net_r", "day_pause_id"):
        conn.execute(f"ALTER TABLE assist_edge DROP COLUMN {col}")
    conn.execute("PRAGMA user_version = 22")
    conn.commit()
    conn.close()

    conn = open_db(path)
    assert schema_version(conn) == 23
    row = conn.execute(
        """
        SELECT streak_anchor_ms, week_pause_id, resume_alerted,
               day_pause_id, day_resume_net_r, day_resume_id, day_deeper_pause_id
        FROM assist_edge WHERE id=1
        """
    ).fetchone()
    assert row["streak_anchor_ms"] == 111
    assert row["week_pause_id"] == "2026-10-26"
    assert int(row["resume_alerted"]) == 1
    assert row["day_pause_id"] is None
    assert row["day_resume_net_r"] is None
    assert row["day_resume_id"] is None
    assert row["day_deeper_pause_id"] is None
    conn.close()


def test_sized_loss_stays_within_risk_after_stop_tick_round() -> None:
    balance = Decimal("2500")
    ticks = (Decimal("0.01"), Decimal("0.1"), Decimal("0.0001"), Decimal("0.5"), Decimal("1"))
    steps = (Decimal("0.001"), Decimal("0.01"), Decimal("0.1"), Decimal("1"))
    risks = (Decimal("0.5"), Decimal("1"), Decimal("2"))
    sized_count = {"long": 0, "short": 0}
    for side in ("long", "short"):
        raw = Decimal("91.037") if side == "long" else Decimal("109.037")
        for tick in ticks:
            stop = round_level_to_tick(raw, tick, side=side, kind="stop")
            for step in steps:
                for risk_pct in risks:
                    sized = size_signal(
                        symbol="BTCUSDT",
                        side=side,
                        entry=Decimal("100"),
                        stop=stop,
                        balance=balance,
                        risk_pct=risk_pct,
                        step_size=step,
                    )
                    if sized is None:
                        continue
                    sized_count[side] += 1
                    cap = balance * risk_pct / Decimal("100")
                    assert sized.loss_at_stop <= cap, (side, tick, step, risk_pct, sized.loss_at_stop, cap)
    assert sized_count["long"] > 0
    assert sized_count["short"] > 0
