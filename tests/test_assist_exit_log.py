"""Additive exit telemetry: cards, taps, stop history, exit source. No order path."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from tests.test_assist import FakeFutures, _ms_bangkok

from jarvise_assist.exit_log import (
    exit_source_for_close,
    log_card,
    note_atr_at_entry,
    note_exchange_stop,
    record_intent_action,
    resolve_exit_source,
    sweep_expired_move_ignores,
)
from jarvise_assist.futures import FuturesPosition
from jarvise_assist.store import insert_exec_intent, upsert_watch_position
from jarvise_assist.watch import flush_held_watch_advice, run_assist_watch
from jarvise_ingest.db import SCHEMA_VERSION, open_db, schema_version, upsert_market_technicals
from jarvise_ingest.db import migrate as run_migrate


def test_resolve_exit_source_labels() -> None:
    assert resolve_exit_source(
        exit_kind="stop", stop_detail="initial", move_source=None, early_exit=False
    ) == ("initial_stop")
    assert (
        resolve_exit_source(exit_kind="stop", stop_detail="moved", move_source="breakeven", early_exit=False)
        == "be_stop"
    )
    assert resolve_exit_source(
        exit_kind="stop", stop_detail="moved", move_source="trail", early_exit=False
    ) == ("trail_stop")
    assert resolve_exit_source(exit_kind="tp", stop_detail=None, move_source=None, early_exit=False) == "tp"
    assert (
        resolve_exit_source(exit_kind="manual", stop_detail=None, move_source=None, early_exit=False)
        == "manual"
    )
    assert resolve_exit_source(
        exit_kind="owner_exit", stop_detail=None, move_source=None, early_exit=True
    ) == ("early_exit")
    assert (
        resolve_exit_source(exit_kind="stop", stop_detail=None, move_source=None, early_exit=False)
        == "manual"
    )


def test_card_tap_stop_history_and_atr(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "log.db")
    upsert_watch_position(
        conn,
        {
            "symbol": "BNBUSDT",
            "position_side": "BOTH",
            "side": "short",
            "entry": 600.0,
            "qty": 1.0,
            "in_universe": 1,
            "signal_id": None,
            "initial_stop": 610.0,
            "initial_stop_source": "atr",
            "entry_time_ms": 1_000,
            "no_stop_latched": 0,
            "last_seen_ms": 1_000,
            "closed_at_ms": None,
        },
    )
    note_atr_at_entry(conn, symbol="BNBUSDT", position_side="BOTH", atr=Decimal("4.5"))
    note_atr_at_entry(conn, symbol="BNBUSDT", position_side="BOTH", atr=Decimal("9"))
    stored = conn.execute("SELECT atr_at_entry FROM assist_watch_position WHERE symbol='BNBUSDT'").fetchone()
    assert stored["atr_at_entry"] == 4.5

    shown = 3_600_000
    card_id = log_card(
        conn,
        symbol="BNBUSDT",
        position_side="BOTH",
        side="short",
        kind="early_exit_offer",
        source="progress",
        shown_at_ms=shown,
        mark=Decimal("590"),
        entry=Decimal("600"),
        initial_stop=Decimal("610"),
        qty=Decimal("1"),
        r_now=Decimal("1"),
        suggested_stop=None,
        exchange_stop=Decimal("610"),
        had_button=True,
        intent_id="intent-early",
        entry_time_ms=0,
        candles_1h=[
            {"timestamp": 0, "high": 602.0, "low": 580.0, "close": 595.0},
        ],
        entry_bar=False,
    )
    assert card_id is not None
    row = conn.execute("SELECT * FROM assist_exit_card WHERE id=?", (card_id,)).fetchone()
    assert row["kind"] == "early_exit_offer"
    assert row["wick"] == pytest.approx(580.0)
    assert row["close_1h"] == pytest.approx(595.0)
    assert row["wick"] != row["close_1h"]
    assert row["atr_at_entry"] == 4.5
    assert row["had_button"] == 1

    intent = {
        "id": "intent-early",
        "kind": "exit",
        "symbol": "BNBUSDT",
        "side": "short",
        "created_at_ms": shown,
        "preview": {"source": "progress", "qty": "1", "entry": "600"},
    }
    record_intent_action(conn, intent, action="tap", action_at_ms=shown + 4_000)
    tap = conn.execute(
        "SELECT kind, action, latency_ms FROM assist_exit_response WHERE intent_id='intent-early'"
    ).fetchone()
    assert tap["kind"] == "early_exit"
    assert tap["action"] == "tap"
    assert tap["latency_ms"] == 4_000
    # A second tap does not create a second row.
    record_intent_action(conn, intent, action="tap", action_at_ms=shown + 9_000)
    n = conn.execute("SELECT COUNT(*) AS n FROM assist_exit_response").fetchone()["n"]
    assert n == 1

    assert (
        exit_source_for_close(
            conn,
            exit_kind="owner_exit",
            stop_detail=None,
            symbol="BNBUSDT",
            side="short",
            entry_time_ms=1_000,
            exit_time_ms=shown + 4_000,
        )
        == "early_exit"
    )

    note_exchange_stop(
        conn,
        symbol="BNBUSDT",
        position_side="BOTH",
        observed_at_ms=1,
        stop_price=Decimal("610"),
        covered_qty=Decimal("1"),
        position_qty=Decimal("1"),
    )
    note_exchange_stop(
        conn,
        symbol="BNBUSDT",
        position_side="BOTH",
        observed_at_ms=2,
        stop_price=Decimal("610"),
        covered_qty=Decimal("1"),
        position_qty=Decimal("1"),
    )
    note_exchange_stop(
        conn,
        symbol="BNBUSDT",
        position_side="BOTH",
        observed_at_ms=3,
        stop_price=Decimal("605"),
        covered_qty=Decimal("1"),
        position_qty=Decimal("1"),
    )
    stops = conn.execute("SELECT stop_price FROM assist_exchange_stop ORDER BY observed_at_ms").fetchall()
    assert [row["stop_price"] for row in stops] == [610.0, 605.0]

    insert_exec_intent(
        conn,
        kind="move_stop",
        symbol="BNBUSDT",
        side="short",
        preview={"stop": "605", "source": "breakeven"},
        created_at_ms=10,
        expires_at_ms=20,
    )
    sweep_expired_move_ignores(conn, now_ms=100)
    ignored = conn.execute("SELECT action FROM assist_exit_response WHERE kind='move_stop'").fetchone()
    assert ignored["action"] == "ignore"
    # Sweep does not change the intent, so a later tap can still see preview.
    status = conn.execute("SELECT status FROM assist_exec_intent").fetchone()["status"]
    assert status == "preview"
    conn.close()


def test_watch_logs_the_spot_wick_that_tripped_breakeven(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "watch.db")
    day = _ms_bangkok(2026, 11, 14, 14, 0)
    entry_time = day - 2 * 3_600_000
    upsert_watch_position(
        conn,
        {
            "symbol": "DOGEUSDT",
            "position_side": "BOTH",
            "side": "short",
            "entry": 0.1673,
            "qty": 100.0,
            "in_universe": 1,
            "signal_id": None,
            "initial_stop": 0.1748,
            "initial_stop_source": "signal",
            "entry_time_ms": entry_time,
            "no_stop_latched": 0,
            "last_seen_ms": entry_time,
            "closed_at_ms": None,
        },
    )
    upsert_market_technicals(
        conn,
        [
            {
                "symbol": "DOGEUSDT",
                "timestamp": entry_time,
                "timeframe": "1h",
                "open": 0.1673,
                "high": 0.1680,
                "low": 0.16355,
                "close": 0.1640,
                "volume": 1.0,
            },
            {
                "symbol": "DOGEUSDT",
                "timestamp": entry_time,
                "timeframe": "4h",
                "open": 0.1673,
                "high": 0.17,
                "low": 0.16,
                "close": 0.165,
                "volume": 1.0,
            },
        ],
    )
    conn.execute(
        """
        UPDATE market_technicals SET atr_14=?
        WHERE symbol='DOGEUSDT' AND timeframe='4h'
        """,
        (0.004,),
    )
    conn.commit()
    pos = FuturesPosition(
        symbol="DOGEUSDT",
        position_side="BOTH",
        position_amt=Decimal("-100"),
        entry_price=Decimal("0.1673"),
        mark_price=Decimal("0.164"),
        update_time_ms=entry_time,
    )
    fake = FakeFutures(
        positions=[pos],
        orders=[
            {
                "symbol": "DOGEUSDT",
                "side": "BUY",
                "type": "STOP_MARKET",
                "reduceOnly": True,
                "stopPrice": "0.1748",
                "origQty": "100",
            }
        ],
        ticks={"DOGEUSDT": Decimal("0.0001")},
        steps={"DOGEUSDT": Decimal("1")},
        marks={"DOGEUSDT": Decimal("0.164")},
    )
    sent: list[str] = []
    run_assist_watch(conn, futures=fake, now_ms_value=day, send=lambda text, **_k: sent.append(text) or True)
    cards = conn.execute(
        "SELECT kind, source, wick, close_1h, suggested_stop, approx_r_after_fees, atr_at_entry "
        "FROM assist_exit_card WHERE kind='move_stop'"
    ).fetchall()
    assert len(cards) == 1
    card = cards[0]
    assert card["source"] == "breakeven"
    assert card["wick"] == pytest.approx(0.16355)
    assert card["close_1h"] == pytest.approx(0.164)
    assert card["approx_r_after_fees"] is not None
    assert card["atr_at_entry"] == pytest.approx(0.004)
    move_text = next(text for text in sent if "ปุ่มหมดอายุ" in text)
    assert "ไส้แท่ง 1h สปอต" in move_text
    assert "ปิดเกิน" not in move_text
    assert "หลังค่าธรรมเนียม" in move_text
    assert "ห่าง mark" in move_text
    conn.close()


def test_morning_flush_does_not_mention_a_button(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "flush.db")
    now = _ms_bangkok(2026, 11, 14, 7, 30)
    upsert_watch_position(
        conn,
        {
            "symbol": "BTCUSDT",
            "position_side": "BOTH",
            "side": "long",
            "entry": 100.0,
            "qty": 0.01,
            "in_universe": 1,
            "signal_id": None,
            "initial_stop": 90.0,
            "initial_stop_source": "signal",
            "entry_time_ms": now - 3_600_000,
            "no_stop_latched": 0,
            "last_seen_ms": now,
            "closed_at_ms": None,
        },
    )
    from jarvise_assist.store import insert_watch_advice

    insert_watch_advice(
        conn,
        symbol="BTCUSDT",
        position_side="BOTH",
        kind="move_stop",
        reason="breakeven",
        level=95.0,
        approx_r=0.5,
        status="held",
        created_at_ms=now - 60_000,
    )
    pos = FuturesPosition(
        symbol="BTCUSDT",
        position_side="BOTH",
        position_amt=Decimal("0.01"),
        entry_price=Decimal("100"),
        mark_price=Decimal("100"),
        update_time_ms=now - 3_600_000,
    )
    fake = FakeFutures(
        positions=[pos],
        orders=[
            {
                "symbol": "BTCUSDT",
                "side": "SELL",
                "type": "STOP_MARKET",
                "reduceOnly": True,
                "stopPrice": "90",
                "origQty": "0.01",
            }
        ],
        marks={"BTCUSDT": Decimal("100")},
        ticks={"BTCUSDT": Decimal("0.1")},
    )
    sent: list[str] = []
    out = flush_held_watch_advice(
        conn, futures=fake, now_ms_value=now, send=lambda text, **_k: sent.append(text) or True
    )
    assert len(out["sent"]) == 1
    assert len(sent) == 1
    text = sent[0]
    assert "ปุ่ม" not in text
    assert "หมดอายุ" not in text
    assert "ยังไม่ได้เลื่อน stop" in text
    assert "เลื่อนเองในแอป" in text
    assert "🛡" not in text
    resp = conn.execute("SELECT action, kind FROM assist_exit_response").fetchone()
    assert resp["action"] == "manual"
    assert resp["kind"] == "move_stop"
    card = conn.execute("SELECT kind, had_button FROM assist_exit_card").fetchone()
    assert card["kind"] == "morning_move"
    assert card["had_button"] == 0
    conn.close()


def test_migration_32_keeps_existing_fees(tmp_path: Path) -> None:
    path = tmp_path / "from31.db"
    conn = open_db(path)
    conn.execute(
        """
        INSERT INTO assist_real_trade (
            symbol, side, entry_order_id, status, updated_at_ms, commission_usdt, funding_usdt
        ) VALUES ('BNBUSDT', 'short', 'old-bnb', 'closed', 1, -0.4, 0.1)
        """
    )
    conn.executescript(
        """
        DROP TABLE IF EXISTS assist_exit_card;
        DROP TABLE IF EXISTS assist_exit_response;
        DROP TABLE IF EXISTS assist_exchange_stop;
        """
    )
    conn.execute("ALTER TABLE assist_real_trade DROP COLUMN exit_source")
    conn.execute("ALTER TABLE assist_real_trade DROP COLUMN atr_at_entry")
    conn.execute("ALTER TABLE assist_watch_position DROP COLUMN atr_at_entry")
    conn.execute("PRAGMA user_version = 31")
    conn.commit()
    conn.close()

    conn = open_db(path)
    assert SCHEMA_VERSION == 32
    assert schema_version(conn) == 32
    row = conn.execute(
        "SELECT commission_usdt, funding_usdt, exit_source, atr_at_entry FROM assist_real_trade"
    ).fetchone()
    assert row["commission_usdt"] == -0.4
    assert row["funding_usdt"] == 0.1
    assert row["exit_source"] is None
    assert row["atr_at_entry"] is None
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='assist_exit_card'"
    ).fetchone()
    assert run_migrate(conn) == []
    conn.close()


def test_move_source_becomes_be_stop(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "src.db")
    preview = json.dumps({"stop": "100", "source": "breakeven"})
    conn.execute(
        """
        INSERT INTO assist_exec_intent (
            id, kind, symbol, side, preview_json, created_at_ms, expires_at_ms, status
        ) VALUES ('mv1', 'move_stop', 'BNBUSDT', 'short', ?, 1, 2, 'done')
        """,
        (preview,),
    )
    conn.commit()
    source = exit_source_for_close(
        conn,
        exit_kind="stop",
        stop_detail="moved",
        symbol="BNBUSDT",
        side="short",
        entry_time_ms=1,
        exit_time_ms=9,
        orders_by_id={"9": {"clientOrderId": "jv-x-ms"}},
        exit_order_ids=["9"],
        move_stop_by_cid={"jv-x-ms": "mv1"},
    )
    assert source == "be_stop"
    conn.close()
