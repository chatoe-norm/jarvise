"""Additive exit telemetry: cards, taps, stop history, exit source. No order path."""

from __future__ import annotations

import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.test_assist import FakeFutures, _ms_bangkok
from tests.test_assist_execute import (
    RecordingTransport,
    _base_entry_handlers,
    _client,
    _entry_intent,
    _open_then_flat,
)

from jarvise_assist.execute import handle_exec_callback, run_entry_sequence
from jarvise_assist.exit_log import (
    exit_source_for_close,
    log_card,
    lookup_atr_at_entry,
    note_atr_at_entry,
    note_early_exit_ignores,
    note_exchange_stop,
    record_intent_action,
    resolve_exit_source,
    sweep_expired_move_ignores,
)
from jarvise_assist.futures import FuturesPosition
from jarvise_assist.journal import EXIT_SOURCE_LABELS
from jarvise_assist.real_trades import _INSERT_COLS, SPEC_START_MS, sync_real_trades
from jarvise_assist.store import (
    get_watch_position,
    insert_exec_intent,
    insert_exec_log,
    insert_watch_advice,
    upsert_watch_position,
)
from jarvise_assist.watch import _link_or_freeze_stop, flush_held_watch_advice, run_assist_watch
from jarvise_ingest.db import SCHEMA_VERSION, open_db, schema_version, upsert_market_technicals
from jarvise_ingest.db import migrate as run_migrate


def test_import_watch_before_execute() -> None:
    """watch imports execute at module load. That import must succeed on its own."""
    proc = subprocess.run(
        [sys.executable, "-c", "import jarvise_assist.watch; import jarvise_assist.execute"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


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
        == "manual_stop"
    )
    assert EXIT_SOURCE_LABELS["manual_stop"] == "stop ที่ตั้งเอง"
    assert EXIT_SOURCE_LABELS["manual"] == "มือ"


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

    intent_row = insert_exec_intent(
        conn,
        kind="exit",
        symbol="BNBUSDT",
        side="short",
        preview={"source": "progress", "qty": "1", "entry": "600", "early_close": True},
        created_at_ms=shown,
        expires_at_ms=shown + 60_000,
    )
    conn.execute(
        "UPDATE assist_exec_intent SET id=?, status='done' WHERE id=?",
        ("intent-early", intent_row),
    )
    conn.execute(
        """
        INSERT INTO assist_exec_log (
            intent_id, step, endpoint, client_order_id, order_or_algo_id, created_at_ms
        ) VALUES ('intent-early', 'exit_market', '/fapi/v1/order', 'jv-early-ex', '900', ?)
        """,
        (shown + 4_000,),
    )
    conn.commit()
    intent = {
        "id": "intent-early",
        "kind": "exit",
        "symbol": "BNBUSDT",
        "side": "short",
        "created_at_ms": shown,
        "preview": {"source": "progress", "qty": "1", "entry": "600", "early_close": True},
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
    assert (
        exit_source_for_close(
            conn,
            exit_kind="owner_exit",
            stop_detail=None,
            symbol="BNBUSDT",
            side="short",
            entry_time_ms=0,
            exit_time_ms=shown + 4_000,
            orders_by_id={"900": {"clientOrderId": "jv-early-ex"}},
            exit_order_ids=["900"],
            move_stop_by_cid={},
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

    move_id = insert_exec_intent(
        conn,
        kind="move_stop",
        symbol="BNBUSDT",
        side="short",
        preview={"stop": "605", "source": "breakeven"},
        created_at_ms=10,
        expires_at_ms=20,
    )
    log_card(
        conn,
        symbol="BNBUSDT",
        position_side="BOTH",
        side="short",
        kind="move_stop",
        shown_at_ms=10,
        intent_id=move_id,
        delivered=True,
        entry_bar=False,
    )
    sweep_expired_move_ignores(conn, now_ms=100)
    ignored = conn.execute("SELECT action FROM assist_exit_response WHERE kind='move_stop'").fetchone()
    assert ignored["action"] == "ignore"
    # Sweep does not change the intent, so a later tap can still see preview.
    status = conn.execute(
        "SELECT status FROM assist_exec_intent WHERE id=?",
        (move_id,),
    ).fetchone()["status"]
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
        "SELECT kind, source, wick, close_1h, suggested_stop, approx_r_after_fees, atr_at_entry, "
        "gap_r, entry, initial_stop, qty "
        "FROM assist_exit_card WHERE kind='move_stop'"
    ).fetchall()
    assert len(cards) == 1
    card = cards[0]
    assert card["source"] == "breakeven"
    assert card["wick"] == pytest.approx(0.16355)
    assert card["close_1h"] == pytest.approx(0.164)
    assert card["approx_r_after_fees"] is not None
    assert card["atr_at_entry"] == pytest.approx(0.004)
    assert card["gap_r"] is not None
    assert card["entry"] == pytest.approx(0.1673)
    assert card["initial_stop"] == pytest.approx(0.1748)
    assert card["qty"] == pytest.approx(100)
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
    assert SCHEMA_VERSION == 39
    assert schema_version(conn) == 39
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


def _seed_atr(conn: Any, symbol: str, rows: list[tuple[int, float]]) -> None:
    upsert_market_technicals(
        conn,
        [
            {
                "symbol": symbol,
                "timestamp": ts,
                "timeframe": "4h",
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume": 1.0,
            }
            for ts, _atr in rows
        ],
    )
    for ts, atr in rows:
        conn.execute(
            """
            UPDATE market_technicals SET atr_14=?
            WHERE symbol=? AND timeframe='4h' AND timestamp=?
            """,
            (atr, symbol, ts),
        )
    conn.commit()


def test_reopened_bot_entry_clears_and_resnapshots_atr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / "reopen.db")
    upsert_watch_position(
        conn,
        {
            "symbol": "DOGEUSDT",
            "position_side": "BOTH",
            "side": "short",
            "entry": 0.09,
            "qty": 50.0,
            "in_universe": 1,
            "signal_id": None,
            "initial_stop": 0.1,
            "initial_stop_source": "atr",
            "entry_time_ms": 1,
            "no_stop_latched": 0,
            "last_seen_ms": 1,
            "closed_at_ms": 2,
        },
    )
    note_atr_at_entry(conn, symbol="DOGEUSDT", position_side="BOTH", atr=Decimal("1.5"))
    intent_id = _entry_intent(conn)
    trade = _client(RecordingTransport(_base_entry_handlers(intent_id=intent_id)))
    assert run_entry_sequence(conn, intent_id, trade=trade).startswith("✅")
    cleared = conn.execute(
        "SELECT atr_at_entry, closed_at_ms, entry FROM assist_watch_position WHERE symbol='DOGEUSDT'"
    ).fetchone()
    assert cleared["atr_at_entry"] is None
    assert cleared["closed_at_ms"] is None
    assert cleared["entry"] == pytest.approx(0.08)

    stored = get_watch_position(conn, "DOGEUSDT", "BOTH")
    assert stored is not None
    now = int(stored["entry_time_ms"]) + 1_000
    _seed_atr(conn, "DOGEUSDT", [(now - 4 * 3_600_000, 0.004)])
    pos = FuturesPosition(
        symbol="DOGEUSDT",
        position_side="BOTH",
        position_amt=Decimal("-100"),
        entry_price=Decimal("0.08"),
        mark_price=Decimal("0.08"),
        update_time_ms=now,
    )
    refreshed = _link_or_freeze_stop(conn, pos, stored, now_ms=now)
    assert refreshed["atr_at_entry"] == pytest.approx(0.004)
    conn.close()


def test_sync_keeps_each_trades_own_atr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = open_db(tmp_path / "sync.db")
    hour = 3_600_000
    t_pre = SPEC_START_MS + 1_000
    t1 = SPEC_START_MS + 30 * 24 * hour
    t2 = t1 + 40 * 24 * hour
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
            "entry_time_ms": t2,
            "no_stop_latched": 0,
            "last_seen_ms": t2,
            "closed_at_ms": None,
        },
    )
    note_atr_at_entry(conn, symbol="BNBUSDT", position_side="BOTH", atr=Decimal("9"))
    conn.execute(
        """
        INSERT INTO assist_real_trade (
            symbol, side, entry_order_id, entry_time_ms, status, updated_at_ms, atr_at_entry
        ) VALUES
            ('BNBUSDT', 'short', 'pre', ?, 'closed', ?, NULL),
            ('BNBUSDT', 'short', 't1', ?, 'closed', ?, 2.5),
            ('BNBUSDT', 'short', 't2', ?, 'closed', ?, NULL)
        """,
        (t_pre, t2, t1, t2, t2, t2),
    )
    conn.commit()

    def fake_build(
        db: Any,
        _client: Any,
        *,
        since_ms: int,
        until_ms: int | None = None,
        asset_prices: dict[str, float] | None = None,
    ) -> list[dict[str, Any]]:
        del since_ms, until_ms, asset_prices
        rows: list[dict[str, Any]] = []
        for oid, entry_ms in (("pre", t_pre), ("t1", t1), ("t2", t2)):
            row = dict.fromkeys(_INSERT_COLS)
            row.update(
                {
                    "symbol": "BNBUSDT",
                    "side": "short",
                    "entry_order_id": oid,
                    "entry_time_ms": entry_ms,
                    "atr_at_entry": lookup_atr_at_entry(
                        db, symbol="BNBUSDT", side="short", entry_time_ms=entry_ms
                    ),
                    "status": "closed",
                    "updated_at_ms": t2,
                }
            )
            rows.append(row)
        return rows

    monkeypatch.setattr("jarvise_assist.real_trades.build_real_rows", fake_build)
    sync_real_trades(conn, object(), since_ms=SPEC_START_MS, until_ms=t2)
    sync_real_trades(conn, object(), since_ms=SPEC_START_MS, until_ms=t2)
    got = {
        row["entry_order_id"]: row["atr_at_entry"]
        for row in conn.execute("SELECT entry_order_id, atr_at_entry FROM assist_real_trade")
    }
    assert got["pre"] is None
    assert got["t1"] == pytest.approx(2.5)
    assert got["t2"] == pytest.approx(9.0)
    assert lookup_atr_at_entry(conn, symbol="BNBUSDT", side="short", entry_time_ms=None) is None
    conn.close()


def test_zero_atr_does_not_freeze_a_stop(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "zero.db")
    now = _ms_bangkok(2026, 11, 14, 12, 0)
    upsert_watch_position(
        conn,
        {
            "symbol": "DOGEUSDT",
            "position_side": "BOTH",
            "side": "long",
            "entry": 0.08,
            "qty": 100.0,
            "in_universe": 1,
            "signal_id": None,
            "initial_stop": None,
            "initial_stop_source": None,
            "entry_time_ms": now - 3_600_000,
            "no_stop_latched": 0,
            "last_seen_ms": now,
            "closed_at_ms": None,
        },
    )
    _seed_atr(
        conn,
        "DOGEUSDT",
        [
            (now - 8 * 3_600_000, 0.002),
            (now - 4 * 3_600_000, 0.0),
        ],
    )
    stored = get_watch_position(conn, "DOGEUSDT", "BOTH")
    assert stored is not None
    pos = FuturesPosition(
        symbol="DOGEUSDT",
        position_side="BOTH",
        position_amt=Decimal("100"),
        entry_price=Decimal("0.08"),
        mark_price=Decimal("0.081"),
        update_time_ms=now,
    )
    refreshed = _link_or_freeze_stop(conn, pos, stored, now_ms=now)
    assert refreshed.get("initial_stop") is None
    assert refreshed.get("atr_at_entry") is None
    conn.close()


def test_failing_exit_log_does_not_block_the_exit_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "boom.db")
    ts = _ms_bangkok(2026, 11, 14, 12, 0)
    intent_id = insert_exec_intent(
        conn,
        kind="exit",
        symbol="ADAUSDT",
        side="short",
        preview={"qty": "100", "entry": "0.1673", "source": "progress", "level": "0.5"},
        created_at_ms=ts,
        expires_at_ms=ts + 120_000,
    )
    conn.execute("DROP TABLE assist_exit_card")
    conn.commit()
    live = {
        "symbol": "ADAUSDT",
        "positionSide": "BOTH",
        "positionAmt": "-100",
        "entryPrice": "0.1673",
        "markPrice": "0.16355",
    }
    transport = RecordingTransport(
        {
            ("GET", "/fapi/v1/positionSide/dual"): (200, {"dualSidePosition": False}),
            ("GET", "/fapi/v1/multiAssetsMargin"): (200, {"multiAssetsMargin": False}),
            ("GET", "/fapi/v3/positionRisk"): _open_then_flat([live]),
            ("POST", "/fapi/v1/order"): (
                200,
                {"orderId": 9, "avgPrice": "0.1636", "executedQty": "100", "status": "FILLED"},
            ),
            ("GET", "/fapi/v1/userTrades"): (200, [{"realizedPnl": "0.37"}]),
            ("GET", "/fapi/v1/openOrders"): (200, []),
            ("GET", "/fapi/v1/openAlgoOrders"): (200, []),
        }
    )
    result = handle_exec_callback(
        conn,
        f"xx:{intent_id}",
        send=lambda text, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: True,
        trade=_client(transport),
        now_ms_value=ts,
        message={"message_id": 4, "text": "progress"},
    )
    posts = [call for call in transport.calls if call[0] == "POST" and call[1] == "/fapi/v1/order"]
    assert len(posts) == 1
    assert result.get("ok") is True
    conn.close()


def test_decline_and_expired_move_log_one_ignore(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "ign.db")
    ts = _ms_bangkok(2026, 11, 14, 12, 0)

    def _tap(data: str) -> None:
        handle_exec_callback(
            conn,
            data,
            send=lambda text, **_k: True,
            answer=lambda *_a, **_k: None,
            edit=lambda *_a, **_k: None,
            now_ms_value=ts,
            message={"message_id": 1, "text": "advice"},
        )

    declined = insert_exec_intent(
        conn,
        kind="move_stop",
        symbol="BNBUSDT",
        side="short",
        preview={"stop": "605", "source": "breakeven", "advice_text": "move"},
        created_at_ms=ts - 1_000,
        expires_at_ms=ts + 60_000,
    )
    _tap(f"mn:{declined}")
    _tap(f"mn:{declined}")
    expired = insert_exec_intent(
        conn,
        kind="move_stop",
        symbol="BNBUSDT",
        side="short",
        preview={"stop": "605", "source": "breakeven", "advice_text": "move"},
        created_at_ms=ts - 2_000,
        expires_at_ms=ts - 1,
    )
    _tap(f"ms:{expired}")
    _tap(f"ms:{expired}")
    counts = {
        row["intent_id"]: row["n"]
        for row in conn.execute(
            """
            SELECT intent_id, COUNT(*) AS n FROM assist_exit_response
            WHERE action='ignore' GROUP BY intent_id
            """
        )
    }
    assert counts == {declined: 1, expired: 1}
    conn.close()


def test_progress_alert_logs_the_early_exit_offer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setattr("jarvise_assist.watch.build_trade_client", lambda: (None, "no_key"))
    conn = open_db(tmp_path / "prog.db")
    day = _ms_bangkok(2026, 11, 14, 12, 0)
    entry_time = day - 3_600_000
    upsert_watch_position(
        conn,
        {
            "symbol": "ADAUSDT",
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
    pos = FuturesPosition(
        symbol="ADAUSDT",
        position_side="BOTH",
        position_amt=Decimal("-100"),
        entry_price=Decimal("0.1673"),
        mark_price=Decimal("0.16355"),
        update_time_ms=entry_time,
    )
    fake = FakeFutures(
        positions=[pos],
        orders=[
            {
                "symbol": "ADAUSDT",
                "side": "BUY",
                "type": "STOP_MARKET",
                "reduceOnly": True,
                "stopPrice": "0.1748",
                "origQty": "100",
            }
        ],
        ticks={"ADAUSDT": Decimal("0.0001")},
        steps={"ADAUSDT": Decimal("1")},
        marks={"ADAUSDT": Decimal("0.16355")},
    )
    run_assist_watch(conn, futures=fake, now_ms_value=day, send=lambda text, **_k: True)
    kinds = {row["kind"] for row in conn.execute("SELECT kind FROM assist_exit_card").fetchall()}
    assert "progress" in kinds
    assert "early_exit_offer" in kinds
    preview = json.loads(
        conn.execute("SELECT preview_json FROM assist_exec_intent WHERE kind='exit'").fetchone()[
            "preview_json"
        ]
    )
    assert preview["early_close"] is True
    conn.close()


def test_note_early_exit_ignores_an_untapped_offer(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "ei.db")
    shown = 5_000
    offered = log_card(
        conn,
        symbol="BNBUSDT",
        position_side="BOTH",
        side="short",
        kind="early_exit_offer",
        shown_at_ms=shown,
        intent_id="offer-1",
        entry_bar=False,
    )
    tapped = log_card(
        conn,
        symbol="ETHUSDT",
        position_side="BOTH",
        side="long",
        kind="early_exit_offer",
        shown_at_ms=shown,
        intent_id="offer-2",
        entry_bar=False,
    )
    record_intent_action(
        conn,
        {
            "id": "offer-2",
            "kind": "exit",
            "symbol": "ETHUSDT",
            "side": "long",
            "created_at_ms": shown,
            "preview": {"source": "progress", "early_close": True},
        },
        action="tap",
        action_at_ms=shown + 10,
    )
    note_early_exit_ignores(conn, [("BNBUSDT", "BOTH"), ("ETHUSDT", "BOTH")], now_ms=shown + 100)
    ignored = conn.execute("SELECT card_id FROM assist_exit_response WHERE action='ignore'").fetchall()
    assert [row["card_id"] for row in ignored] == [offered]
    assert tapped not in {row["card_id"] for row in ignored}
    conn.close()


def test_morning_flush_send_has_no_reply_markup(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "markup.db")
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
    captured: list[tuple[str, object]] = []

    def send(text: str, reply_markup: object = None, **_k: object) -> bool:
        captured.append((text, reply_markup))
        return True

    flush_held_watch_advice(conn, futures=fake, now_ms_value=now, send=send)
    assert len(captured) == 1
    text, markup = captured[0]
    assert markup is None
    assert "🛡" not in text
    assert "ปุ่ม" not in text
    assert "หมดอายุ" not in text
    assert "ถ้าเลื่อน ล็อก ≈" in text
    card = conn.execute("SELECT advice_id FROM assist_exit_card WHERE kind='morning_move'").fetchone()
    advice_id = conn.execute("SELECT id FROM assist_watch_advice").fetchone()["id"]
    assert card["advice_id"] == advice_id
    latency = conn.execute("SELECT latency_ms FROM assist_exit_response WHERE action='manual'").fetchone()
    assert latency["latency_ms"] is None
    conn.close()


def test_later_planned_close_is_not_early_exit(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "later.db")
    entry_ms = 1_000
    warn_ms = 5_000
    close_ms = 9_000
    warn_id = insert_exec_intent(
        conn,
        kind="exit",
        symbol="BNBUSDT",
        side="short",
        preview={"source": "progress", "qty": "1", "entry": "600", "early_close": True, "level": "0.5"},
        created_at_ms=warn_ms,
        expires_at_ms=warn_ms + 60_000,
    )
    ok_id = insert_exec_intent(
        conn,
        kind="exit",
        symbol="BNBUSDT",
        side="short",
        preview={"source": "progress", "qty": "1", "entry": "600", "early_close": False, "level": "1.5"},
        created_at_ms=close_ms - 100,
        expires_at_ms=close_ms + 60_000,
    )
    conn.execute("UPDATE assist_exec_intent SET status='done'")
    conn.commit()
    insert_exec_log(
        conn,
        intent_id=warn_id,
        step="exit_market",
        endpoint="/fapi/v1/order",
        client_order_id="jv-warn-ex",
        order_or_algo_id="1",
        created_at_ms=warn_ms,
    )
    insert_exec_log(
        conn,
        intent_id=ok_id,
        step="exit_market",
        endpoint="/fapi/v1/order",
        client_order_id="jv-ok-ex",
        order_or_algo_id="2",
        created_at_ms=close_ms,
    )
    record_intent_action(
        conn,
        {
            "id": warn_id,
            "kind": "exit",
            "symbol": "BNBUSDT",
            "side": "short",
            "created_at_ms": warn_ms,
            "preview": {"source": "progress", "early_close": True},
        },
        action="tap",
        action_at_ms=warn_ms + 10,
    )
    planned = exit_source_for_close(
        conn,
        exit_kind="owner_exit",
        stop_detail=None,
        symbol="BNBUSDT",
        side="short",
        entry_time_ms=entry_ms,
        exit_time_ms=close_ms,
        orders_by_id={"2": {"clientOrderId": "jv-ok-ex"}},
        exit_order_ids=["2"],
        move_stop_by_cid={},
    )
    assert planned == "owner_exit"
    warned = exit_source_for_close(
        conn,
        exit_kind="owner_exit",
        stop_detail=None,
        symbol="BNBUSDT",
        side="short",
        entry_time_ms=entry_ms,
        exit_time_ms=warn_ms,
        orders_by_id={"1": {"clientOrderId": "jv-warn-ex"}},
        exit_order_ids=["1"],
        move_stop_by_cid={},
    )
    assert warned == "early_exit"
    conn.close()
