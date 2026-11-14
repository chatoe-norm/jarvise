"""Pause reason on assist_settings (migration 29).

(P) pass on 75cf23b and after. (N) fail on 75cf23b and pass after.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from tests.test_assist import FakeFutures, _ms_bangkok
from tests.test_assist_day_loss import _trade
from tests.test_assist_listen import _send_capture
from tests.test_assist_quick_card import _forbid_trade_client, _happy_fake, _seed_settings
from tests.test_assist_week_loss_4r import TUE, WED_10, WEEK_ID, _eval

from jarvise_assist.pause import (
    clear_pause_manual,
    evaluate_day_loss_pause,
    evaluate_pause,
    format_paused_until,
    maybe_auto_resume,
    next_0820_bangkok_ms,
)
from jarvise_assist.quick_card import build_quick_card
from jarvise_assist.store import ensure_settings, get_edge, write_edge, write_settings
from jarvise_ingest.db import SCHEMA_VERSION, open_db, schema_version

DAY_NOW = _ms_bangkok(2026, 11, 9, 15, 0)


def test_n_migration_29(tmp_path: Path) -> None:
    fresh = open_db(tmp_path / "fresh.db")
    assert SCHEMA_VERSION == 30
    assert schema_version(fresh) == 30
    cols = {row[1] for row in fresh.execute("PRAGMA table_info(assist_settings)").fetchall()}
    assert "pause_reason" in cols
    assert ensure_settings(fresh)["pause_reason"] is None
    fresh.close()

    path = tmp_path / "from28.db"
    conn = open_db(path)
    x = next_0820_bangkok_ms(TUE)
    write_settings(conn, paused_until_ms=x)
    write_edge(
        conn,
        week_pause_id=WEEK_ID,
        day_pause_id="2026-11-14",
        streak_anchor_ms=111,
        week_risk_id=WEEK_ID,
        week_risk_pct=1.0,
    )
    conn.execute("ALTER TABLE assist_settings DROP COLUMN pause_reason")
    conn.execute("PRAGMA user_version = 28")
    conn.commit()
    conn.close()

    conn = open_db(path)
    assert schema_version(conn) == 30
    settings = ensure_settings(conn)
    assert settings["paused_until_ms"] == x
    assert settings["pause_reason"] is None
    edge = get_edge(conn)
    assert edge["week_pause_id"] == WEEK_ID
    assert edge["day_pause_id"] == "2026-11-14"
    assert edge["streak_anchor_ms"] == 111
    assert edge["week_risk_id"] == WEEK_ID
    assert edge["week_risk_pct"] == pytest.approx(1.0)
    conn.close()

    conn = open_db(path)
    assert schema_version(conn) == 30
    settings = ensure_settings(conn)
    assert settings["paused_until_ms"] == x
    assert settings["pause_reason"] is None
    edge = get_edge(conn)
    assert edge["week_pause_id"] == WEEK_ID
    assert edge["day_pause_id"] == "2026-11-14"
    assert edge["streak_anchor_ms"] == 111
    assert edge["week_risk_id"] == WEEK_ID
    assert edge["week_risk_pct"] == pytest.approx(1.0)
    conn.close()


def test_n_reason_per_pause(tmp_path: Path) -> None:
    # Streak
    conn = open_db(tmp_path / "streak.db")
    now = _ms_bangkok(2026, 10, 27, 15, 0)
    income = [
        {"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "income": "-5", "time": now - 3_000},
        {"symbol": "ETHUSDT", "incomeType": "REALIZED_PNL", "income": "-4", "time": now - 2_000},
        {"symbol": "SOLUSDT", "incomeType": "REALIZED_PNL", "income": "-3", "time": now - 1_000},
    ]
    sent, send = _send_capture()
    streak = evaluate_pause(
        conn,
        futures=FakeFutures(balance=Decimal("1000"), income=income),
        now_ms=now,
        send=send,
    )
    until = next_0820_bangkok_ms(now)
    assert ensure_settings(conn)["pause_reason"] == "streak"
    assert ensure_settings(conn)["paused_until_ms"] == until
    assert set(streak.keys()) == {"paused", "reason", "until_ms", "anchor_ms"}
    assert streak == {
        "paused": True,
        "reason": "streak",
        "until_ms": until,
        "anchor_ms": now - 1_000,
    }
    conn.close()

    # Week
    conn = open_db(tmp_path / "week.db")
    week, _sent = _eval(conn, risk=1.0, net=-50, now_ms=TUE)
    until = next_0820_bangkok_ms(TUE)
    assert ensure_settings(conn)["pause_reason"] == "week_loss"
    assert ensure_settings(conn)["paused_until_ms"] == until
    assert set(week.keys()) == {
        "paused",
        "reason",
        "until_ms",
        "loss_pct",
        "week_id",
        "threshold_pct",
        "week_risk_pct",
    }
    assert week["paused"] is True
    assert week["reason"] == "week_loss"
    assert week["until_ms"] == until
    assert week["week_id"] == WEEK_ID
    conn.close()

    # Day + deeper
    conn = open_db(tmp_path / "day.db")
    _trade(conn, oid="t1", exit_ms=DAY_NOW - 120_000, net=-3.0)
    sent, send = _send_capture()
    day = evaluate_day_loss_pause(conn, now_ms=DAY_NOW, send=send)
    until = next_0820_bangkok_ms(DAY_NOW)
    assert ensure_settings(conn)["pause_reason"] == "day_loss"
    assert ensure_settings(conn)["paused_until_ms"] == until
    assert set(day.keys()) == {"paused", "reason", "until_ms", "net_r", "day_id"}
    assert day == {
        "paused": True,
        "reason": "day_loss",
        "until_ms": until,
        "net_r": -3.0,
        "day_id": "2026-11-09",
    }

    clear_pause_manual(conn, now_ms_value=DAY_NOW + 60_000)
    _trade(conn, oid="t2", exit_ms=DAY_NOW + 90_000, net=-1.0, symbol="ETHUSDT")
    later = DAY_NOW + 120_000
    deeper = evaluate_day_loss_pause(conn, now_ms=later, send=send)
    until2 = next_0820_bangkok_ms(later)
    assert ensure_settings(conn)["pause_reason"] == "day_loss_deeper"
    assert ensure_settings(conn)["paused_until_ms"] == until2
    assert set(deeper.keys()) == {"paused", "reason", "until_ms", "net_r", "day_id"}
    assert deeper["paused"] is True
    assert deeper["reason"] == "day_loss_deeper"
    assert deeper["until_ms"] == until2
    assert deeper["day_id"] == "2026-11-09"
    conn.close()


def test_n_reason_cleared(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "clear_week.db")
    _eval(conn, risk=1.0, net=-50, now_ms=TUE)
    clear_pause_manual(conn, now_ms_value=TUE + 60_000)
    settings = ensure_settings(conn)
    assert settings["paused_until_ms"] is None
    assert settings["pause_reason"] is None
    assert get_edge(conn)["week_pause_id"] == WEEK_ID
    result, _sent = _eval(conn, risk=1.0, net=-50, now_ms=TUE + 120_000)
    assert result.get("paused") is not True or result.get("reason") != "week_loss"
    assert ensure_settings(conn)["pause_reason"] is None
    assert ensure_settings(conn)["paused_until_ms"] is None
    conn.close()

    conn = open_db(tmp_path / "clear_day.db")
    _trade(conn, oid="t", exit_ms=DAY_NOW - 60_000, net=-3.0)
    sent, send = _send_capture()
    day = evaluate_day_loss_pause(conn, now_ms=DAY_NOW, send=send)
    until = day["until_ms"]
    assert maybe_auto_resume(conn, now_ms=until, send=send) is True
    settings = ensure_settings(conn)
    assert settings["paused_until_ms"] is None
    assert settings["pause_reason"] is None
    conn.close()


def test_n_write_settings_guard(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "guard.db")
    x = next_0820_bangkok_ms(TUE)
    y = next_0820_bangkok_ms(WED_10)
    write_settings(conn, paused_until_ms=x, pause_reason="week_loss")
    s = ensure_settings(conn)
    assert s["paused_until_ms"] == x
    assert s["pause_reason"] == "week_loss"

    write_settings(conn, risk_pct=2.0)
    s = ensure_settings(conn)
    assert s["risk_pct"] == pytest.approx(2.0)
    assert s["paused_until_ms"] == x
    assert s["pause_reason"] == "week_loss"

    write_settings(conn, paused_until_ms=y)
    s = ensure_settings(conn)
    assert s["paused_until_ms"] == y
    assert s["pause_reason"] is None

    with pytest.raises(ValueError):
        write_settings(conn, paused_until_ms=x, pause_reason="manual")
    with pytest.raises(ValueError):
        write_settings(conn, pause_reason="week_loss")
    with pytest.raises(ValueError):
        write_settings(conn, paused_until_ms=None, pause_reason="week_loss")
    conn.close()


def test_n_quick_allowed_streak_after_week_loss(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / "qs.db")
    _eval(conn, risk=1.0, net=-50, now_ms=TUE)
    clear_pause_manual(conn, now_ms_value=TUE + 60_000)
    income = [
        {
            "symbol": "BTCUSDT",
            "incomeType": "REALIZED_PNL",
            "income": "-5",
            "time": WED_10 - 3_000,
        },
        {
            "symbol": "ETHUSDT",
            "incomeType": "REALIZED_PNL",
            "income": "-4",
            "time": WED_10 - 2_000,
        },
        {
            "symbol": "SOLUSDT",
            "incomeType": "REALIZED_PNL",
            "income": "-3",
            "time": WED_10 - 1_000,
        },
    ]
    sent, send = _send_capture()
    streak = evaluate_pause(
        conn,
        futures=FakeFutures(balance=Decimal("1000"), income=income),
        now_ms=WED_10,
        send=send,
    )
    assert streak["reason"] == "streak"
    assert get_edge(conn)["week_pause_id"] == WEEK_ID
    assert ensure_settings(conn)["pause_reason"] == "streak"
    card = build_quick_card(
        conn, _happy_fake(), symbol="SUIUSDT", side="long", stop=Decimal("1.994"), now_ms=WED_10
    )
    assert card["ok"] is True
    conn.close()


def test_n_quick_allowed_day_loss_after_week_loss(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / "qd.db")
    _eval(conn, risk=1.0, net=-50, now_ms=TUE)
    clear_pause_manual(conn, now_ms_value=TUE + 60_000)
    _trade(conn, oid="d", exit_ms=WED_10 - 60_000, net=-3.0)
    sent, send = _send_capture()
    day = evaluate_pause(
        conn,
        futures=FakeFutures(balance=Decimal("1000"), income=[]),
        now_ms=WED_10,
        send=send,
    )
    assert day["reason"] == "day_loss"
    assert get_edge(conn)["week_pause_id"] == WEEK_ID
    assert ensure_settings(conn)["pause_reason"] == "day_loss"
    card = build_quick_card(
        conn, _happy_fake(), symbol="SUIUSDT", side="long", stop=Decimal("1.994"), now_ms=WED_10
    )
    assert card["ok"] is True
    conn.close()


def test_p_quick_blocks_week_loss_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / "block.db")
    result, _sent = _eval(conn, risk=1.0, net=-50, now_ms=TUE)
    until = result["until_ms"]
    card = build_quick_card(
        conn,
        _happy_fake(),
        symbol="SUIUSDT",
        side="long",
        stop=Decimal("1.994"),
        now_ms=TUE + 60_000,
    )
    assert card["reason"] == "week_loss_pause"
    assert "4R" in card["text"]
    assert format_paused_until(until) in card["text"]
    conn.close()

    conn = open_db(tmp_path / "sun.db")
    _seed_settings(conn)
    sun = _ms_bangkok(2026, 11, 22, 22, 0)
    until = next_0820_bangkok_ms(sun)
    write_settings(conn, paused_until_ms=until)
    write_edge(conn, week_pause_id=WEEK_ID)
    mon_am = _ms_bangkok(2026, 11, 23, 1, 0)
    card = build_quick_card(
        conn, _happy_fake(), symbol="SUIUSDT", side="long", stop=Decimal("1.994"), now_ms=mon_am
    )
    assert card["reason"] == "week_loss_pause"
    after = _ms_bangkok(2026, 11, 23, 8, 21)
    card2 = build_quick_card(
        conn, _happy_fake(), symbol="SUIUSDT", side="long", stop=Decimal("1.994"), now_ms=after
    )
    assert card2["ok"] is True
    conn.close()


def test_p_quick_legacy_null_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / "legacy.db")
    _seed_settings(conn)
    write_settings(conn, paused_until_ms=next_0820_bangkok_ms(TUE))
    write_edge(conn, week_pause_id=WEEK_ID)
    card = build_quick_card(
        conn, _happy_fake(), symbol="SUIUSDT", side="long", stop=Decimal("1.994"), now_ms=TUE
    )
    assert card["reason"] == "week_loss_pause"
    conn.close()

    conn = open_db(tmp_path / "legacy_ok.db")
    _seed_settings(conn)
    write_settings(conn, paused_until_ms=next_0820_bangkok_ms(TUE))
    # week_pause_id None
    card = build_quick_card(
        conn, _happy_fake(), symbol="SUIUSDT", side="long", stop=Decimal("1.994"), now_ms=TUE
    )
    assert card["ok"] is True
    conn.close()
