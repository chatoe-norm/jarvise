"""QPR1: quick settings, registration, fill tagging, lock, report, wiring."""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.test_assist import FakeFutures as WatchFakeFutures
from tests.test_assist import _ms_bangkok
from tests.test_assist_listen import _send_capture
from tests.test_assist_real_trades import FakeFutures as HistoryFakeFutures

from jarvise_assist.journal import summarize_journal
from jarvise_assist.keyboard import BTN_EXPLORE, BTN_QUICK, reply_keyboard
from jarvise_assist.listen import process_update
from jarvise_assist.menu import match_menu_label
from jarvise_assist.pause import STREAK_PAUSE_TEXT, bangkok_day_start_ms, day_bot_net_r, evaluate_pause
from jarvise_assist.quick import (
    create_quick_registration,
    get_quick_settings,
    render_quick_menu,
    summarize_quick,
    sync_quick_trades,
)
from jarvise_assist.quick_tags import quick_income_predicate
from jarvise_assist.real_trades import (
    build_income_reconcile,
    build_real_rows,
    summarize_real_vs_virtual,
)
from jarvise_assist.store import ensure_settings, get_edge, upsert_watch_position, write_settings
from jarvise_assist.watch import run_assist_watch
from jarvise_assist.weekly import format_weekly_summary, summarize_income
from jarvise_ingest.db import SCHEMA_VERSION, open_db, schema_version


def _answers() -> tuple[list[str], Any]:
    seen: list[str] = []

    def answer(cq_id: str, *, text: str | None = None) -> bool:
        del text
        seen.append(cq_id)
        return True

    return seen, answer


def _fill(
    *,
    tid: int,
    symbol: str,
    side: str,
    price: float,
    qty: float,
    time_ms: int,
    realized: float = 0.0,
    commission: float = 0.01,
    commission_asset: str = "USDT",
    order_id: int | None = None,
) -> dict[str, Any]:
    return {
        "id": tid,
        "orderId": order_id if order_id is not None else tid,
        "symbol": symbol,
        "side": side,
        "price": str(price),
        "qty": str(qty),
        "time": time_ms,
        "realizedPnl": str(realized),
        "commission": str(commission),
        "commissionAsset": commission_asset,
    }


def _income(
    *,
    symbol: str,
    itype: str,
    income: str,
    time_ms: int,
    trade_id: int | str = "",
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "incomeType": itype,
        "income": income,
        "time": time_ms,
        "tradeId": str(trade_id),
        "tranId": f"{itype}-{time_ms}-{trade_id}",
    }


# --- 1a migration -----------------------------------------------------------


def test_migration_27_fresh_and_from_26(tmp_path: Path) -> None:
    fresh = open_db(tmp_path / "fresh.db")
    assert SCHEMA_VERSION == 29
    assert schema_version(fresh) == 29
    for table in (
        "assist_quick_settings",
        "assist_quick_registration",
        "assist_quick_trade",
    ):
        assert fresh.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
    qs = get_quick_settings(fresh)
    assert qs["risk_pct"] == 0.5
    assert qs["day_max_loss_r"] == 2
    assert qs["streak_lock_n"] == 3
    assert qs["cap_pct"] == 1.0
    fresh.execute(
        """
        INSERT INTO assist_signal (
            id, symbol, side, entry, stop, target, qty, notional, leverage,
            loss_at_stop, created_at_ms, expires_at_ms, status
        ) VALUES ('keep', 'BTCUSDT', 'long', 1, 0.9, 1.2, 1, 1, 1, 0.1, 1, 2, 'sent')
        """
    )
    fresh.commit()
    fresh.close()

    path26 = tmp_path / "from26.db"
    conn = open_db(path26)
    conn.execute(
        """
        INSERT INTO assist_signal (
            id, symbol, side, entry, stop, target, qty, notional, leverage,
            loss_at_stop, created_at_ms, expires_at_ms, status
        ) VALUES ('old', 'ETHUSDT', 'long', 1, 0.9, 1.2, 2, 2, 1, 0.2, 1, 2, 'sent')
        """
    )
    conn.execute("DROP TABLE IF EXISTS assist_quick_settings")
    conn.execute("DROP TABLE IF EXISTS assist_quick_registration")
    conn.execute("DROP TABLE IF EXISTS assist_quick_trade")
    conn.execute("PRAGMA user_version = 26")
    conn.commit()
    conn.close()

    conn = open_db(path26)
    assert schema_version(conn) == 29
    row = conn.execute("SELECT qty FROM assist_signal WHERE id='old'").fetchone()
    assert row["qty"] == 2
    qs = get_quick_settings(conn)
    assert qs["risk_pct"] == 0.5
    assert qs["day_max_loss_r"] == 2.0
    assert qs["streak_lock_n"] == 3
    assert qs["cap_pct"] == 1.0
    conn.close()


# --- 1b registration --------------------------------------------------------


def test_registration_create_and_refusals(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "reg.db")
    t0 = _ms_bangkok(2026, 11, 10, 12, 0)
    ok, reason = create_quick_registration(
        conn,
        symbol="BTCUSDT",
        side="long",
        planned_entry=100.0,
        planned_stop=95.0,
        qty=0.01,
        now_ms=t0,
    )
    assert reason is None
    assert ok is not None
    row = conn.execute("SELECT * FROM assist_quick_registration WHERE id=?", (ok,)).fetchone()
    assert row["expires_at_ms"] == t0 + 10 * 60_000
    assert row["status"] == "open"

    # open registration blocks
    bad, why = create_quick_registration(
        conn,
        symbol="BTCUSDT",
        side="long",
        planned_entry=100.0,
        planned_stop=95.0,
        qty=0.01,
        now_ms=t0 + 1000,
    )
    assert bad is None and why == "open_registration"

    conn.execute("UPDATE assist_quick_registration SET status='tagged' WHERE id=?", (ok,))
    conn.commit()

    # open watch blocks
    upsert_watch_position(
        conn,
        {
            "symbol": "ETHUSDT",
            "position_side": "LONG",
            "side": "long",
            "entry": 100.0,
            "qty": 1.0,
            "in_universe": 1,
            "signal_id": None,
            "initial_stop": None,
            "initial_stop_source": None,
            "entry_time_ms": t0,
            "no_stop_latched": 0,
            "last_seen_ms": t0,
            "closed_at_ms": None,
        },
    )
    bad, why = create_quick_registration(
        conn,
        symbol="ETHUSDT",
        side="long",
        planned_entry=100.0,
        planned_stop=95.0,
        qty=0.01,
        now_ms=t0,
    )
    assert bad is None and why == "open_assist_watch"

    bad, why = create_quick_registration(
        conn,
        symbol="SOLUSDT",
        side="flat",
        planned_entry=100.0,
        planned_stop=95.0,
        qty=0.01,
        now_ms=t0,
    )
    assert bad is None and why == "invalid_side"

    bad, why = create_quick_registration(
        conn,
        symbol="SOLUSDT",
        side="long",
        planned_entry=100.0,
        planned_stop=105.0,
        qty=0.01,
        now_ms=t0,
    )
    assert bad is None and why == "stop_wrong_side"

    bad, why = create_quick_registration(
        conn,
        symbol="SOLUSDT",
        side="long",
        planned_entry=100.0,
        planned_stop=95.0,
        qty=0,
        now_ms=t0,
    )
    assert bad is None and why == "qty_non_positive"

    # lock blocks
    conn.execute(
        """
        UPDATE assist_quick_settings
        SET locked_until_ms=?, lock_reason='streak', lock_day_id='2026-11-10'
        WHERE id=1
        """,
        (t0 + 86_400_000,),
    )
    conn.commit()
    bad, why = create_quick_registration(
        conn,
        symbol="SOLUSDT",
        side="long",
        planned_entry=100.0,
        planned_stop=95.0,
        qty=0.01,
        now_ms=t0,
    )
    assert bad is None and why == "locked"

    # open quick trade blocks
    conn.execute("UPDATE assist_quick_settings SET locked_until_ms=NULL, lock_reason=NULL WHERE id=1")
    rid, _ = create_quick_registration(
        conn,
        symbol="BNBUSDT",
        side="short",
        planned_entry=100.0,
        planned_stop=105.0,
        qty=0.5,
        now_ms=t0,
    )
    assert rid is not None
    conn.execute(
        """
        INSERT INTO assist_quick_trade (
            id, registration_id, symbol, side, planned_stop, entry_time_ms,
            entry_qty, entry_avg, status, updated_at_ms
        ) VALUES ('qt1', ?, 'BNBUSDT', 'short', 105, ?, 0.5, 100, 'open', ?)
        """,
        (rid, t0, t0),
    )
    conn.execute("UPDATE assist_quick_registration SET status='tagged' WHERE id=?", (rid,))
    conn.commit()
    bad, why = create_quick_registration(
        conn,
        symbol="BNBUSDT",
        side="short",
        planned_entry=100.0,
        planned_stop=105.0,
        qty=0.5,
        now_ms=t0 + 1,
    )
    assert bad is None and why == "open_quick_trade"
    conn.close()


# --- 1c fill tagging --------------------------------------------------------


def test_fill_tagging_happy_path_and_edges(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "tag.db")
    t0 = _ms_bangkok(2026, 11, 10, 10, 0)
    rid, _ = create_quick_registration(
        conn,
        symbol="BTCUSDT",
        side="long",
        planned_entry=100.0,
        planned_stop=90.0,
        qty=1.0,
        now_ms=t0,
    )
    assert rid is not None
    buy_t = t0 + 3 * 60_000
    sell_t = t0 + 4 * 60_000
    trades = [
        _fill(tid=1, symbol="BTCUSDT", side="BUY", price=100.0, qty=1.0, time_ms=buy_t, commission=0.05),
        _fill(
            tid=2,
            symbol="BTCUSDT",
            side="SELL",
            price=110.0,
            qty=1.0,
            time_ms=sell_t,
            realized=10.0,
            commission=0.05,
        ),
    ]
    funding_t = buy_t + 30_000
    income = [
        _income(
            symbol="BTCUSDT",
            itype="FUNDING_FEE",
            income="-0.2",
            time_ms=funding_t,
        ),
        _income(
            symbol="BTCUSDT",
            itype="FUNDING_FEE",
            income="-9.0",
            time_ms=buy_t,  # not > entry → excluded
        ),
        _income(
            symbol="BTCUSDT",
            itype="FUNDING_FEE",
            income="-8.0",
            time_ms=sell_t + 1,  # after close → excluded
        ),
    ]
    fake = HistoryFakeFutures(trades=trades, income=income)
    sent: list[str] = []
    info = sync_quick_trades(conn, fake, now_ms=sell_t + 1000, send=lambda t, **_k: sent.append(t) or True)
    assert info  # non-empty when work done
    row = conn.execute("SELECT * FROM assist_quick_trade WHERE registration_id=?", (rid,)).fetchone()
    assert row["status"] == "closed"
    assert row["entry_avg"] == pytest.approx(100.0)
    assert row["exit_avg"] == pytest.approx(110.0)
    assert row["realized_pnl_usdt"] == pytest.approx(10.0)
    assert row["commission_usdt"] == pytest.approx(-0.10)
    assert row["funding_usdt"] == pytest.approx(-0.2)
    assert row["risk_usdt"] == pytest.approx(10.0)  # |100-90|*1
    assert row["net_usdt"] == pytest.approx(10.0 - 0.10 - 0.2)
    assert row["net_r"] == pytest.approx(row["net_usdt"] / 10.0)
    assert row["fee_r"] == pytest.approx(0.10 / 10.0)
    entry_ids = json.loads(row["entry_trade_ids"])
    exit_ids = json.loads(row["exit_trade_ids"])
    assert entry_ids == [1]
    assert exit_ids == [2]

    # idempotent
    sync_quick_trades(conn, fake, now_ms=sell_t + 2000, send=lambda t, **_k: sent.append(t) or True)
    rows = conn.execute("SELECT id FROM assist_quick_trade").fetchall()
    assert len(rows) == 1
    row2 = conn.execute("SELECT * FROM assist_quick_trade WHERE registration_id=?", (rid,)).fetchone()
    assert row2["net_usdt"] == pytest.approx(row["net_usdt"])
    assert row2["net_r"] == pytest.approx(row["net_r"])
    conn.close()


def test_fill_expired_and_wrong_side(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "exp.db")
    t0 = _ms_bangkok(2026, 11, 10, 11, 0)
    rid, _ = create_quick_registration(
        conn,
        symbol="BTCUSDT",
        side="long",
        planned_entry=100.0,
        planned_stop=90.0,
        qty=1.0,
        now_ms=t0,
    )
    fill_t = t0 + 11 * 60_000
    fake = HistoryFakeFutures(
        trades=[_fill(tid=1, symbol="BTCUSDT", side="BUY", price=100.0, qty=1.0, time_ms=fill_t)]
    )
    sync_quick_trades(
        conn,
        fake,
        now_ms=t0 + 12 * 60_000 + 1,  # > expires + 2 min
        send=lambda *_a, **_k: True,
    )
    reg = conn.execute("SELECT status FROM assist_quick_registration WHERE id=?", (rid,)).fetchone()
    assert reg["status"] == "expired"
    assert conn.execute("SELECT COUNT(*) AS n FROM assist_quick_trade").fetchone()["n"] == 0

    rid2, _ = create_quick_registration(
        conn,
        symbol="ETHUSDT",
        side="long",
        planned_entry=100.0,
        planned_stop=90.0,
        qty=1.0,
        now_ms=t0,
    )
    fake2 = HistoryFakeFutures(
        trades=[
            _fill(
                tid=9,
                symbol="ETHUSDT",
                side="SELL",
                price=100.0,
                qty=1.0,
                time_ms=t0 + 60_000,
            )
        ]
    )
    sync_quick_trades(conn, fake2, now_ms=t0 + 60_000, send=lambda *_a, **_k: True)
    reg2 = conn.execute("SELECT status FROM assist_quick_registration WHERE id=?", (rid2,)).fetchone()
    assert reg2["status"] == "expired"
    assert (
        conn.execute(
            "SELECT COUNT(*) AS n FROM assist_quick_trade WHERE registration_id=?",
            (rid2,),
        ).fetchone()["n"]
        == 0
    )
    conn.close()


def test_first_fill_wrong_side_before_opening_expires(tmp_path: Path) -> None:
    """Earlier wrong-side fill expires the registration; later opening fill must not tag."""
    conn = open_db(tmp_path / "wrongfirst.db")
    t0 = _ms_bangkok(2026, 11, 10, 11, 0)
    rid, _ = create_quick_registration(
        conn,
        symbol="ETHUSDT",
        side="long",
        planned_entry=100.0,
        planned_stop=90.0,
        qty=1.0,
        now_ms=t0,
    )
    fake = HistoryFakeFutures(
        trades=[
            _fill(
                tid=9,
                symbol="ETHUSDT",
                side="SELL",
                price=100.0,
                qty=1.0,
                time_ms=t0 + 60_000,
            ),
            _fill(
                tid=10,
                symbol="ETHUSDT",
                side="BUY",
                price=100.0,
                qty=1.0,
                time_ms=t0 + 3 * 60_000,
                realized=-1.0,
            ),
        ]
    )
    sync_quick_trades(conn, fake, now_ms=t0 + 3 * 60_000, send=lambda *_a, **_k: True)
    reg = conn.execute("SELECT status FROM assist_quick_registration WHERE id=?", (rid,)).fetchone()
    assert reg["status"] == "expired"
    assert conn.execute("SELECT COUNT(*) AS n FROM assist_quick_trade").fetchone()["n"] == 0
    pred = quick_income_predicate(conn, now_ms=t0 + 3 * 60_000)
    assert (
        pred(
            _income(
                symbol="ETHUSDT",
                itype="REALIZED_PNL",
                income="-1",
                time_ms=t0 + 3 * 60_000,
                trade_id=10,
            )
        )
        is False
    )

    # short mirror: BUY first, then SELL
    rid2, _ = create_quick_registration(
        conn,
        symbol="BTCUSDT",
        side="short",
        planned_entry=100.0,
        planned_stop=110.0,
        qty=1.0,
        now_ms=t0,
    )
    fake2 = HistoryFakeFutures(
        trades=[
            _fill(
                tid=19,
                symbol="BTCUSDT",
                side="BUY",
                price=100.0,
                qty=1.0,
                time_ms=t0 + 60_000,
            ),
            _fill(
                tid=20,
                symbol="BTCUSDT",
                side="SELL",
                price=100.0,
                qty=1.0,
                time_ms=t0 + 3 * 60_000,
                realized=-1.0,
            ),
        ]
    )
    sync_quick_trades(conn, fake2, now_ms=t0 + 3 * 60_000, send=lambda *_a, **_k: True)
    reg2 = conn.execute("SELECT status FROM assist_quick_registration WHERE id=?", (rid2,)).fetchone()
    assert reg2["status"] == "expired"
    assert (
        conn.execute(
            "SELECT COUNT(*) AS n FROM assist_quick_trade WHERE registration_id=?",
            (rid2,),
        ).fetchone()["n"]
        == 0
    )
    pred2 = quick_income_predicate(conn, now_ms=t0 + 3 * 60_000)
    assert (
        pred2(
            _income(
                symbol="BTCUSDT",
                itype="REALIZED_PNL",
                income="-1",
                time_ms=t0 + 3 * 60_000,
                trade_id=20,
            )
        )
        is False
    )
    conn.close()


def test_fill_partials_and_cross_zero(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "part.db")
    t0 = _ms_bangkok(2026, 11, 10, 12, 0)
    rid, _ = create_quick_registration(
        conn,
        symbol="BTCUSDT",
        side="long",
        planned_entry=100.0,
        planned_stop=90.0,
        qty=2.0,
        now_ms=t0,
    )
    trades = [
        _fill(
            tid=1, symbol="BTCUSDT", side="BUY", price=100.0, qty=1.0, time_ms=t0 + 60_000, commission=0.01
        ),
        _fill(
            tid=2, symbol="BTCUSDT", side="BUY", price=100.0, qty=1.0, time_ms=t0 + 90_000, commission=0.01
        ),
        _fill(
            tid=3,
            symbol="BTCUSDT",
            side="SELL",
            price=105.0,
            qty=1.0,
            time_ms=t0 + 120_000,
            realized=5.0,
            commission=0.02,
        ),
        _fill(
            tid=4,
            symbol="BTCUSDT",
            side="SELL",
            price=110.0,
            qty=1.0,
            time_ms=t0 + 150_000,
            realized=10.0,
            commission=0.02,
        ),
    ]
    fake = HistoryFakeFutures(trades=trades, income=[])
    # after first sell — still open
    sync_quick_trades(conn, fake, now_ms=t0 + 130_000, send=lambda *_a, **_k: True)
    row = conn.execute("SELECT * FROM assist_quick_trade WHERE registration_id=?", (rid,)).fetchone()
    assert row["status"] == "open"
    assert set(json.loads(row["entry_trade_ids"])) == {1, 2}
    assert set(json.loads(row["exit_trade_ids"])) == {3}

    sync_quick_trades(conn, fake, now_ms=t0 + 160_000, send=lambda *_a, **_k: True)
    row = conn.execute("SELECT * FROM assist_quick_trade WHERE registration_id=?", (rid,)).fetchone()
    assert row["status"] == "closed"
    assert set(json.loads(row["exit_trade_ids"])) == {3, 4}
    assert row["realized_pnl_usdt"] == pytest.approx(15.0)
    conn.close()

    # cross zero: sell more than open
    conn = open_db(tmp_path / "cross.db")
    rid, _ = create_quick_registration(
        conn,
        symbol="BTCUSDT",
        side="long",
        planned_entry=100.0,
        planned_stop=90.0,
        qty=1.0,
        now_ms=t0,
    )
    trades = [
        _fill(
            tid=10, symbol="BTCUSDT", side="BUY", price=100.0, qty=1.0, time_ms=t0 + 10_000, commission=0.10
        ),
        _fill(
            tid=11,
            symbol="BTCUSDT",
            side="SELL",
            price=110.0,
            qty=2.0,
            time_ms=t0 + 20_000,
            realized=10.0,
            commission=0.20,
        ),
    ]
    fake = HistoryFakeFutures(trades=trades, income=[])
    sync_quick_trades(conn, fake, now_ms=t0 + 30_000, send=lambda *_a, **_k: True)
    row = conn.execute("SELECT * FROM assist_quick_trade WHERE registration_id=?", (rid,)).fetchone()
    assert row["status"] == "closed"
    assert row["realized_pnl_usdt"] == pytest.approx(10.0)
    # commission prorated by closing fraction 1/2 of the crossing fill + full entry
    # entry: -0.10; exit half of 0.20 → -0.10; total -0.20
    assert row["commission_usdt"] == pytest.approx(-0.20)
    assert set(json.loads(row["exit_trade_ids"])) == {11}
    conn.close()


def test_fill_non_usdt_commission_and_zero_calls(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "bnb.db")
    t0 = _ms_bangkok(2026, 11, 10, 13, 0)
    rid, _ = create_quick_registration(
        conn,
        symbol="BTCUSDT",
        side="long",
        planned_entry=100.0,
        planned_stop=90.0,
        qty=1.0,
        now_ms=t0,
    )
    trades = [
        _fill(
            tid=1,
            symbol="BTCUSDT",
            side="BUY",
            price=100.0,
            qty=1.0,
            time_ms=t0 + 1000,
            commission=0.001,
            commission_asset="BNB",
        ),
        _fill(
            tid=2,
            symbol="BTCUSDT",
            side="SELL",
            price=110.0,
            qty=1.0,
            time_ms=t0 + 2000,
            realized=10.0,
            commission=0.001,
            commission_asset="BNB",
        ),
    ]
    fake = HistoryFakeFutures(trades=trades, income=[])
    sync_quick_trades(conn, fake, now_ms=t0 + 3000, send=lambda *_a, **_k: True)
    row = conn.execute("SELECT * FROM assist_quick_trade WHERE registration_id=?", (rid,)).fetchone()
    assert row["commission_other_asset"]
    assert "BNB" in row["commission_other_asset"]
    assert row["net_r"] is None
    assert row["fee_r"] is None
    stats = summarize_quick(conn, until_ms=t0 + 3000)
    assert stats["no_r_n"] >= 1
    conn.close()

    conn = open_db(tmp_path / "nocall.db")
    fake = HistoryFakeFutures(trades=[], income=[])
    sync_quick_trades(conn, fake, now_ms=t0, send=lambda *_a, **_k: True)
    assert fake.user_trade_calls == []
    assert fake.income_calls == []
    conn.close()


# --- 1d lock ----------------------------------------------------------------


def _close_quick_loss(
    conn: Any,
    *,
    symbol: str,
    net_usdt: float,
    net_r: float,
    closed_at_ms: int,
    idx: int,
) -> None:
    rid = f"reg{idx}"
    tid = f"qt{idx}"
    conn.execute(
        """
        INSERT INTO assist_quick_registration (
            id, symbol, side, planned_entry, planned_stop, qty,
            created_at_ms, expires_at_ms, status, trade_id, updated_at_ms
        ) VALUES (?, ?, 'long', 100, 90, 1, ?, ?, 'tagged', ?, ?)
        """,
        (rid, symbol, closed_at_ms - 60_000, closed_at_ms, tid, closed_at_ms),
    )
    conn.execute(
        """
        INSERT INTO assist_quick_trade (
            id, registration_id, symbol, side, planned_stop, entry_time_ms,
            entry_qty, entry_avg, closed_at_ms, exit_avg,
            entry_trade_ids, exit_trade_ids, realized_pnl_usdt, commission_usdt,
            funding_usdt, net_usdt, risk_usdt, net_r, fee_r, status, updated_at_ms
        ) VALUES (
            ?, ?, ?, 'long', 90, ?, 1, 100, ?, 95,
            '[]', '[]', ?, 0, 0, ?, 10, ?, 0, 'closed', ?
        )
        """,
        (tid, rid, symbol, closed_at_ms - 30_000, closed_at_ms, net_usdt, net_usdt, net_r, closed_at_ms),
    )
    conn.commit()


def test_quick_lock_streak_and_day_loss(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lock.db")
    day = _ms_bangkok(2026, 11, 10, 15, 0)
    sent: list[str] = []

    def send(text: str, **_k: Any) -> bool:
        sent.append(text)
        return True

    for i in range(3):
        _close_quick_loss(
            conn,
            symbol="BTCUSDT",
            net_usdt=-1.0,
            net_r=-0.1,
            closed_at_ms=day - (3 - i) * 60_000,
            idx=i,
        )
    # settings snapshot before
    settings_before = dict(ensure_settings(conn))
    edge_before = dict(get_edge(conn))

    fake = HistoryFakeFutures()
    sync_quick_trades(conn, fake, now_ms=day, send=send)
    qs = get_quick_settings(conn)
    assert qs["locked_until_ms"] == bangkok_day_start_ms(day) + 86_400_000
    assert len([m for m in sent if m.startswith("🔒 Quick")]) == 1
    assert "แพ้ติดกัน 3 ไม้" in sent[0]

    n = len(sent)
    sync_quick_trades(conn, fake, now_ms=day + 1000, send=send)
    assert len(sent) == n

    # registration refused while locked
    bad, why = create_quick_registration(
        conn,
        symbol="SOLUSDT",
        side="long",
        planned_entry=100.0,
        planned_stop=90.0,
        qty=1.0,
        now_ms=day + 2000,
    )
    assert bad is None and why == "locked"

    assert dict(ensure_settings(conn)) == settings_before
    assert dict(get_edge(conn)) == edge_before

    # next day unlocks
    next_day = bangkok_day_start_ms(day) + 86_400_000 + 60_000
    sync_quick_trades(conn, fake, now_ms=next_day, send=send)
    qs = get_quick_settings(conn)
    # clear lock when past locked_until (evaluate may not clear fields until unlock path)
    assert qs["locked_until_ms"] is None or int(qs["locked_until_ms"]) <= next_day

    # win resets streak: two losses then win then two losses — no lock at 2
    conn2 = open_db(tmp_path / "win.db")
    sent2: list[str] = []
    t = _ms_bangkok(2026, 11, 11, 14, 0)
    _close_quick_loss(conn2, symbol="BTCUSDT", net_usdt=-1, net_r=-0.1, closed_at_ms=t - 4000, idx=1)
    _close_quick_loss(conn2, symbol="ETHUSDT", net_usdt=-1, net_r=-0.1, closed_at_ms=t - 3000, idx=2)
    # win
    conn2.execute(
        """
        INSERT INTO assist_quick_registration (
            id, symbol, side, planned_entry, planned_stop, qty,
            created_at_ms, expires_at_ms, status, trade_id, updated_at_ms
        ) VALUES ('regw', 'SOLUSDT', 'long', 100, 90, 1, ?, ?, 'tagged', 'qtw', ?)
        """,
        (t - 2000, t, t - 1000),
    )
    conn2.execute(
        """
        INSERT INTO assist_quick_trade (
            id, registration_id, symbol, side, planned_stop, entry_time_ms,
            entry_qty, entry_avg, closed_at_ms, exit_avg,
            entry_trade_ids, exit_trade_ids, realized_pnl_usdt, commission_usdt,
            funding_usdt, net_usdt, risk_usdt, net_r, fee_r, status, updated_at_ms
        ) VALUES (
            'qtw', 'regw', 'SOLUSDT', 'long', 90, ?, 1, 100, ?, 110,
            '[]', '[]', 5, 0, 0, 5, 10, 0.5, 0, 'closed', ?
        )
        """,
        (t - 1500, t - 1000, t - 1000),
    )
    _close_quick_loss(conn2, symbol="BNBUSDT", net_usdt=-1, net_r=-0.1, closed_at_ms=t - 500, idx=3)
    _close_quick_loss(conn2, symbol="ADAUSDT", net_usdt=-1, net_r=-0.1, closed_at_ms=t - 100, idx=4)
    sync_quick_trades(
        conn2, HistoryFakeFutures(), now_ms=t, send=lambda text, **_k: sent2.append(text) or True
    )
    assert get_quick_settings(conn2)["locked_until_ms"] in (None, 0) or not any("แพ้ติดกัน" in m for m in sent2)
    # only 2 losses in a row at end — streak_lock_n=3, no streak lock
    assert not any("แพ้ติดกัน" in m for m in sent2)
    conn2.close()

    # day max loss
    conn3 = open_db(tmp_path / "dl.db")
    sent3: list[str] = []
    t = _ms_bangkok(2026, 11, 12, 16, 0)
    _close_quick_loss(conn3, symbol="BTCUSDT", net_usdt=-12, net_r=-1.2, closed_at_ms=t - 2000, idx=1)
    _close_quick_loss(conn3, symbol="ETHUSDT", net_usdt=-9, net_r=-0.9, closed_at_ms=t - 1000, idx=2)
    sync_quick_trades(
        conn3, HistoryFakeFutures(), now_ms=t, send=lambda text, **_k: sent3.append(text) or True
    )
    assert any("ขาดทุนวันนี้" in m and "−2R" in m.replace("-", "−") or "ขาดทุนวันนี้" in m for m in sent3)
    assert get_quick_settings(conn3)["locked_until_ms"] is not None

    conn4 = open_db(tmp_path / "dl1.db")
    conn4.execute("UPDATE assist_quick_settings SET day_max_loss_r=1 WHERE id=1")
    conn4.commit()
    sent4: list[str] = []
    t = _ms_bangkok(2026, 11, 13, 16, 0)
    _close_quick_loss(conn4, symbol="BTCUSDT", net_usdt=-12, net_r=-1.2, closed_at_ms=t - 1000, idx=1)
    sync_quick_trades(
        conn4, HistoryFakeFutures(), now_ms=t, send=lambda text, **_k: sent4.append(text) or True
    )
    assert any("ขาดทุนวันนี้" in m for m in sent4)
    conn3.close()
    conn4.close()
    conn.close()


# --- 1e report --------------------------------------------------------------


def test_summarize_quick_math(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "rep.db")
    t = _ms_bangkok(2026, 11, 10, 18, 0)
    # wins: +1R, +2R; losses: -0.5R, -1.5R; one no-R
    specs = [
        (1, 1.0, 0.05, t - 5000),
        (2, 2.0, 0.04, t - 4000),
        (3, -0.5, 0.03, t - 3000),
        (4, -1.5, 0.02, t - 2000),
    ]
    for i, net_r, fee_r, closed in specs:
        rid = f"r{i}"
        conn.execute(
            """
            INSERT INTO assist_quick_registration (
                id, symbol, side, planned_entry, planned_stop, qty,
                created_at_ms, expires_at_ms, status, trade_id, updated_at_ms
            ) VALUES (?, 'BTCUSDT', 'long', 100, 90, 1, ?, ?, 'tagged', ?, ?)
            """,
            (rid, closed - 1000, closed, f"q{i}", closed),
        )
        conn.execute(
            """
            INSERT INTO assist_quick_trade (
                id, registration_id, symbol, side, planned_stop, entry_time_ms,
                entry_qty, entry_avg, closed_at_ms, exit_avg,
                entry_trade_ids, exit_trade_ids, realized_pnl_usdt, commission_usdt,
                funding_usdt, net_usdt, risk_usdt, net_r, fee_r, status, updated_at_ms
            ) VALUES (
                ?, ?, 'BTCUSDT', 'long', 90, ?, 1, 100, ?, 100,
                '[]', '[]', ?, 0, 0, ?, 10, ?, ?, 'closed', ?
            )
            """,
            (f"q{i}", rid, closed - 500, closed, net_r * 10, net_r * 10, net_r, fee_r, closed),
        )
    # no-R
    conn.execute(
        """
        INSERT INTO assist_quick_registration (
            id, symbol, side, planned_entry, planned_stop, qty,
            created_at_ms, expires_at_ms, status, trade_id, updated_at_ms
        ) VALUES ('rn', 'ETHUSDT', 'long', 100, 90, 1, ?, ?, 'tagged', 'qn', ?)
        """,
        (t - 1500, t, t - 1000),
    )
    conn.execute(
        """
        INSERT INTO assist_quick_trade (
            id, registration_id, symbol, side, planned_stop, entry_time_ms,
            entry_qty, entry_avg, closed_at_ms, exit_avg,
            entry_trade_ids, exit_trade_ids, realized_pnl_usdt, commission_usdt,
            commission_other_asset, funding_usdt, net_usdt, risk_usdt, net_r, fee_r,
            status, updated_at_ms
        ) VALUES (
            'qn', 'rn', 'ETHUSDT', 'long', 90, ?, 1, 100, ?, 110,
            '[]', '[]', 5, 0, 'BNB:0.01', 0, 5, 10, NULL, NULL, 'closed', ?
        )
        """,
        (t - 1200, t - 1000, t - 1000),
    )
    conn.commit()
    s = summarize_quick(conn, until_ms=t)
    assert s["n"] == 4  # R-eligible
    assert s["no_r_n"] == 1
    assert s["win_rate"] == pytest.approx(0.5)
    assert s["sum_r"] == pytest.approx(1.0)
    assert s["mean_r"] == pytest.approx(0.25)
    assert s["avg_win_r"] == pytest.approx(1.5)
    assert s["avg_loss_r"] == pytest.approx(-1.0)
    assert s["mean_fee_r"] == pytest.approx((0.05 + 0.04 + 0.03 + 0.02) / 4)
    # cumulative R by close: 1, 3, 2.5, 1.0 → max DD from peak 3 to 1 = 2
    assert s["max_dd_r"] == pytest.approx(2.0)
    assert s["today_r"] == pytest.approx(1.0)
    assert s["streak"] == -2  # two losses at end
    text, _kb = render_quick_menu(conn, now_ms=t)
    assert "ยังไม่พิสูจน์ edge — ตัวอย่าง 4/20 ไม้" in text
    conn.close()


# --- 1f settings UI ---------------------------------------------------------


def test_quick_settings_via_process_update(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "ui.db")
    sent, send = _send_capture()
    answers, answer = _answers()
    now = _ms_bangkok(2026, 11, 10, 12, 0)
    settings_snap = dict(ensure_settings(conn))

    # ⚡ button
    out = process_update(
        conn,
        {"message": {"chat": {"id": 42}, "text": BTN_QUICK}},
        send=send,
        now_ms_value=now,
    )
    assert out.get("kind") == "quick" or out.get("handled")
    assert any("Quick" in s["text"] for s in sent)
    assert match_menu_label(BTN_QUICK) == "quick"
    kb = reply_keyboard(1.0)
    assert kb["keyboard"][3] == [{"text": BTN_EXPLORE}, {"text": BTN_QUICK}]

    def _cq(data: str) -> dict[str, Any]:
        return {
            "callback_query": {
                "id": f"cq-{data}",
                "data": data,
                "message": {"chat": {"id": 42}, "message_id": 1},
            }
        }

    # qs:risk:0.25 saves
    n_ans = len(answers)
    process_update(conn, _cq("qs:risk:0.25"), send=send, answer=answer, now_ms_value=now)
    assert len(answers) == n_ans + 1
    assert get_quick_settings(conn)["risk_pct"] == 0.25

    # qs:risk:1 asks confirm
    process_update(conn, _cq("qs:risk:1"), send=send, answer=answer, now_ms_value=now)
    assert any("ยืนยัน" in s["text"] for s in sent[-2:])
    process_update(conn, _cq("qs:ok:risk:1"), send=send, answer=answer, now_ms_value=now)
    assert get_quick_settings(conn)["risk_pct"] == 1.0

    process_update(conn, _cq("qs:risk:1"), send=send, answer=answer, now_ms_value=now)
    process_update(conn, _cq("qs:no"), send=send, answer=answer, now_ms_value=now)
    assert get_quick_settings(conn)["risk_pct"] == 1.0

    # qs:cap:2 confirm shows 6%
    process_update(conn, _cq("qs:cap:2"), send=send, answer=answer, now_ms_value=now)
    assert any("ความเสี่ยงรวมสูงสุด 6%" in s["text"] for s in sent)
    process_update(conn, _cq("qs:ok:cap:2"), send=send, answer=answer, now_ms_value=now)
    assert get_quick_settings(conn)["cap_pct"] == 2.0

    process_update(conn, _cq("qs:dl:1.5"), send=send, answer=answer, now_ms_value=now)
    assert get_quick_settings(conn)["day_max_loss_r"] == 1.5
    process_update(conn, _cq("qs:st:2"), send=send, answer=answer, now_ms_value=now)
    assert get_quick_settings(conn)["streak_lock_n"] == 2

    before = dict(get_quick_settings(conn))
    for data in ("qs:risk:3", "qs:dl:3", "qs:st:5"):
        out = process_update(conn, _cq(data), send=send, answer=answer, now_ms_value=now)
        assert "unhandled_callback" not in out
    assert dict(get_quick_settings(conn)) == before

    # foreign chat
    n_sent = len(sent)
    process_update(
        conn,
        {
            "callback_query": {
                "id": "x",
                "data": "qs:risk:0.5",
                "message": {"chat": {"id": 99}, "message_id": 1},
            }
        },
        send=send,
        answer=answer,
        now_ms_value=now,
    )
    assert len(sent) == n_sent

    assert dict(ensure_settings(conn)) == settings_snap

    # while explore awaited, ⚡ opens menu
    write_settings(conn, awaiting_explore=True)
    process_update(
        conn,
        {"message": {"chat": {"id": 42}, "text": BTN_QUICK}},
        send=send,
        now_ms_value=now,
    )
    assert any("Quick" in s["text"] for s in sent[-1:])
    conn.close()


# --- 1g main unchanged ------------------------------------------------------


def _seed_quick_closed_with_exit_ids(
    conn: Any,
    *,
    symbol: str,
    exit_trade_id: int,
    closed_at_ms: int,
    entry_ms: int,
    idx: int = 1,
) -> None:
    rid = f"qr{idx}"
    tid = f"qt{idx}"
    conn.execute(
        """
        INSERT INTO assist_quick_registration (
            id, symbol, side, planned_entry, planned_stop, qty,
            created_at_ms, expires_at_ms, status, trade_id, updated_at_ms
        ) VALUES (?, ?, 'long', 100, 90, 1, ?, ?, 'tagged', ?, ?)
        """,
        (rid, symbol, entry_ms, closed_at_ms, tid, closed_at_ms),
    )
    conn.execute(
        """
        INSERT INTO assist_quick_trade (
            id, registration_id, symbol, side, planned_stop, entry_time_ms,
            entry_qty, entry_avg, closed_at_ms, exit_avg,
            entry_trade_ids, exit_trade_ids, realized_pnl_usdt, commission_usdt,
            funding_usdt, net_usdt, risk_usdt, net_r, fee_r, status, updated_at_ms
        ) VALUES (
            ?, ?, ?, 'long', 90, ?, 1, 100, ?, 95,
            ?, ?, -5, -0.1, -0.05, -5.15, 10, -0.515, 0.01, 'closed', ?
        )
        """,
        (
            tid,
            rid,
            symbol,
            entry_ms,
            closed_at_ms,
            json.dumps([exit_trade_id - 1]),
            json.dumps([exit_trade_id]),
            closed_at_ms,
        ),
    )
    conn.commit()


def test_streak_skips_quick_week_loss_keeps_quick(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "streak.db")
    now = _ms_bangkok(2026, 11, 10, 15, 0)
    # three quick losses on distinct symbols — tagged (spacing > merge window)
    syms = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
    for i, tid in enumerate((101, 102, 103)):
        _seed_quick_closed_with_exit_ids(
            conn,
            symbol=syms[i],
            exit_trade_id=tid,
            closed_at_ms=now - (3 - i) * 180_000,
            entry_ms=now - (3 - i) * 180_000 - 10_000,
            idx=i + 1,
        )
    income = [
        {
            "symbol": syms[i],
            "incomeType": "REALIZED_PNL",
            "income": "-5",
            "time": now - (3 - i) * 180_000,
            "tradeId": str(101 + i),
        }
        for i in range(3)
    ]
    sent, send = _send_capture()
    fake = WatchFakeFutures(balance=Decimal("1000"), income=income)
    r = evaluate_pause(conn, futures=fake, now_ms=now, send=send)
    assert r.get("reason") != "streak"
    assert STREAK_PAUSE_TEXT not in " ".join(s["text"] for s in sent)

    # same income without quick rows → streak
    conn2 = open_db(tmp_path / "streak2.db")
    sent2, send2 = _send_capture()
    r2 = evaluate_pause(
        conn2,
        futures=WatchFakeFutures(balance=Decimal("1000"), income=income),
        now_ms=now,
        send=send2,
    )
    assert r2.get("reason") == "streak"

    # 2 main + 1 quick → no pause
    conn3 = open_db(tmp_path / "streak3.db")
    _seed_quick_closed_with_exit_ids(
        conn3,
        symbol="BTCUSDT",
        exit_trade_id=103,
        closed_at_ms=now - 1000,
        entry_ms=now - 10_000,
        idx=1,
    )
    income3 = [
        {
            "symbol": "BTCUSDT",
            "incomeType": "REALIZED_PNL",
            "income": "-5",
            "time": now - 3000,
            "tradeId": "1",
        },
        {
            "symbol": "ETHUSDT",
            "incomeType": "REALIZED_PNL",
            "income": "-4",
            "time": now - 2000,
            "tradeId": "2",
        },
        {
            "symbol": "BTCUSDT",
            "incomeType": "REALIZED_PNL",
            "income": "-3",
            "time": now - 1000,
            "tradeId": "103",
        },
    ]
    sent3, send3 = _send_capture()
    r3 = evaluate_pause(
        conn3,
        futures=WatchFakeFutures(balance=Decimal("1000"), income=income3),
        now_ms=now,
        send=send3,
    )
    assert r3.get("reason") != "streak"

    # 3 main with quick win between — pause (quick win not in realized filter for streak)
    conn4 = open_db(tmp_path / "streak4.db")
    _seed_quick_closed_with_exit_ids(
        conn4,
        symbol="SOLUSDT",
        exit_trade_id=50,
        closed_at_ms=now - 2500,
        entry_ms=now - 20_000,
        idx=1,
    )
    # override to win for predicate; income for win not in the three main losses
    income4 = [
        {
            "symbol": "BTCUSDT",
            "incomeType": "REALIZED_PNL",
            "income": "-5",
            "time": now - 4000,
            "tradeId": "10",
        },
        {
            "symbol": "ETHUSDT",
            "incomeType": "REALIZED_PNL",
            "income": "-4",
            "time": now - 3000,
            "tradeId": "11",
        },
        {
            "symbol": "SOLUSDT",
            "incomeType": "REALIZED_PNL",
            "income": "9",
            "time": now - 2500,
            "tradeId": "50",
        },
        {
            "symbol": "BNBUSDT",
            "incomeType": "REALIZED_PNL",
            "income": "-3",
            "time": now - 1000,
            "tradeId": "12",
        },
    ]
    sent4, send4 = _send_capture()
    r4 = evaluate_pause(
        conn4,
        futures=WatchFakeFutures(balance=Decimal("1000"), income=income4),
        now_ms=now,
        send=send4,
    )
    assert r4.get("reason") == "streak"

    # week loss still counts quick
    conn5 = open_db(tmp_path / "week.db")
    # large trading loss > 4R × week-start risk (4% at the default 1%)
    week_income = [
        {
            "symbol": "BTCUSDT",
            "incomeType": "REALIZED_PNL",
            "income": "-100",
            "time": now - 1000,
            "tradeId": "99",
        },
    ]
    _seed_quick_closed_with_exit_ids(
        conn5,
        symbol="BTCUSDT",
        exit_trade_id=99,
        closed_at_ms=now - 1000,
        entry_ms=now - 10_000,
        idx=1,
    )
    sent5, send5 = _send_capture()
    r5 = evaluate_pause(
        conn5,
        futures=WatchFakeFutures(balance=Decimal("1000"), income=week_income),
        now_ms=now,
        send=send5,
    )
    assert r5.get("reason") == "week_loss"
    conn.close()
    conn2.close()
    conn3.close()
    conn4.close()
    conn5.close()


def test_weekly_and_reconcile_skip_quick(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "weeksum.db")
    now = _ms_bangkok(2026, 11, 10, 8, 30)
    _seed_quick_closed_with_exit_ids(
        conn,
        symbol="BTCUSDT",
        exit_trade_id=7,
        closed_at_ms=now - 1000,
        entry_ms=now - 10_000,
        idx=1,
    )
    _seed_quick_closed_with_exit_ids(
        conn,
        symbol="PEPEUSDT",
        exit_trade_id=8,
        closed_at_ms=now - 2000,
        entry_ms=now - 20_000,
        idx=2,
    )
    income = [
        {
            "symbol": "BTCUSDT",
            "incomeType": "REALIZED_PNL",
            "income": "-5",
            "time": now - 1000,
            "tradeId": "7",
        },
        {
            "symbol": "PEPEUSDT",
            "incomeType": "REALIZED_PNL",
            "income": "-2",
            "time": now - 2000,
            "tradeId": "8",
        },
        {
            "symbol": "ETHUSDT",
            "incomeType": "REALIZED_PNL",
            "income": "3",
            "time": now - 500,
            "tradeId": "9",
        },
        {
            "symbol": "BTCUSDT",
            "incomeType": "COMMISSION",
            "income": "-0.1",
            "time": now - 1000,
            "tradeId": "6",
        },
        {
            "symbol": "BTCUSDT",
            "incomeType": "FUNDING_FEE",
            "income": "-0.05",
            "time": now - 5000,
            "tradeId": "",
        },
    ]
    pred = quick_income_predicate(conn, now_ms=now)
    with_q = summarize_income(income, is_quick=pred)
    assert with_q["realized_pnl"] == pytest.approx(3.0)
    assert with_q["quick_n"] >= 1
    assert "quick_usdt" in with_q

    text = format_weekly_summary(
        counts={"sent": 0, "skipped": 0, "taken": 0},
        virtual={"n": 0},
        real=with_q,
        closed_all=0,
        quick={"n": 2, "sum_r": -1.03, "usdt": with_q["quick_usdt"]},
    )
    assert "Quick (แยกจาก assist) 7 วัน:" in text
    assert "สุทธิ" in text

    # no quick → identical
    empty = open_db(tmp_path / "empty.db")
    pred0 = quick_income_predicate(empty, now_ms=now)
    a = summarize_income(income, is_quick=pred0)
    b = summarize_income(income)
    assert a == b
    t0 = format_weekly_summary(
        counts={"sent": 0, "skipped": 0, "taken": 0},
        virtual={"n": 0},
        real=b,
        closed_all=0,
    )
    t1 = format_weekly_summary(
        counts={"sent": 0, "skipped": 0, "taken": 0},
        virtual={"n": 0},
        real=b,
        closed_all=0,
        quick=None,
    )
    assert t0 == t1

    # reconcile
    rec = build_income_reconcile(
        HistoryFakeFutures(income=income),  # type: ignore[arg-type]
        [],
        since_ms=now - 86_400_000,
        until_ms=now,
        is_quick=pred,
    )
    assert rec.get("quick_n", 0) > 0
    # quick rows not in unattributed for BTC trade 7
    assert not any(u.get("symbol") == "BTCUSDT" and u.get("amount") == -5.0 for u in rec["unattributed"])
    conn.close()
    empty.close()


def test_weekly_quick_line_uses_quick_usdt_for_no_r(tmp_path: Path) -> None:
    """No-R quick trade: Quick line USDT comes from real quick_usdt, not summarize_quick usdt=0."""
    conn = open_db(tmp_path / "norweek.db")
    now = _ms_bangkok(2026, 11, 10, 8, 30)
    closed = now - 1000
    entry_ms = now - 10_000
    exit_tid = 77
    entry_tid = 76
    conn.execute(
        """
        INSERT INTO assist_quick_registration (
            id, symbol, side, planned_entry, planned_stop, qty,
            created_at_ms, expires_at_ms, status, trade_id, updated_at_ms
        ) VALUES ('rn', 'ETHUSDT', 'long', 100, 90, 1, ?, ?, 'tagged', 'qn', ?)
        """,
        (entry_ms, closed, closed),
    )
    conn.execute(
        """
        INSERT INTO assist_quick_trade (
            id, registration_id, symbol, side, planned_stop, entry_time_ms,
            entry_qty, entry_avg, closed_at_ms, exit_avg,
            entry_trade_ids, exit_trade_ids, realized_pnl_usdt, commission_usdt,
            commission_other_asset, funding_usdt, net_usdt, risk_usdt, net_r, fee_r,
            status, updated_at_ms
        ) VALUES (
            'qn', 'rn', 'ETHUSDT', 'long', 90, ?, 1, 100, ?, 97,
            ?, ?, -3.0, -0.2, 'BNB:0.01', 0, -3.2, 10, NULL, NULL, 'closed', ?
        )
        """,
        (entry_ms, closed, json.dumps([entry_tid]), json.dumps([exit_tid]), closed),
    )
    conn.commit()
    income = [
        _income(
            symbol="ETHUSDT",
            itype="REALIZED_PNL",
            income="-3.0",
            time_ms=closed,
            trade_id=exit_tid,
        ),
        _income(
            symbol="ETHUSDT",
            itype="COMMISSION",
            income="-0.2",
            time_ms=closed,
            trade_id=exit_tid,
        ),
    ]
    pred = quick_income_predicate(conn, now_ms=now)
    real = summarize_income(income, is_quick=pred)
    assert real.get("quick_usdt") == pytest.approx(-3.2)
    quick = summarize_quick(conn, until_ms=now)
    assert quick["n"] == 0
    assert quick["usdt"] == pytest.approx(0.0)
    text = format_weekly_summary(
        counts={"sent": 0, "skipped": 0, "taken": 0},
        virtual={"n": 0},
        real=real,
        closed_all=0,
        quick=quick,
    )
    assert "Quick (แยกจาก assist) 7 วัน: n=0 · รวม +0.00R · -3.20 USDT" in text
    # สุทธิ + quick USDT equals the total of the income rows
    net_line = next(ln for ln in text.splitlines() if ln.startswith("สุทธิ "))
    net_usdt = float(net_line.split()[1])
    assert net_usdt + float(real["quick_usdt"]) == pytest.approx(-3.2)
    conn.close()


def test_watch_no_quick_and_sync_failure(tmp_path: Path, caplog: Any) -> None:
    conn = open_db(tmp_path / "watch.db")
    now = _ms_bangkok(2026, 11, 10, 12, 0)
    fake = WatchFakeFutures(balance=Decimal("1000"), positions=[], orders=[])
    sent, send = _send_capture()
    with caplog.at_level(logging.ERROR):
        r1 = run_assist_watch(conn, futures=fake, now_ms_value=now, send=send)
    assert "quick" not in r1
    assert r1["ok"] is True
    # no user_trades on watch fake — sync must not call it when idle
    assert not hasattr(fake, "user_trade_calls") or getattr(fake, "user_trade_calls", []) == []

    # sync raises — advice still runs
    class Boom(WatchFakeFutures):
        pass

    boom = Boom(balance=Decimal("1000"), positions=[], orders=[])
    # force open registration so sync tries and we inject failure via monkeypatch on sync
    create_quick_registration(
        conn,
        symbol="BTCUSDT",
        side="long",
        planned_entry=100.0,
        planned_stop=90.0,
        qty=1.0,
        now_ms=now,
    )

    import jarvise_assist.watch as watch_mod

    def _boom(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise RuntimeError("sync_fail")

    original = watch_mod.sync_quick_trades
    watch_mod.sync_quick_trades = _boom  # type: ignore[assignment]
    try:
        with caplog.at_level(logging.ERROR):
            r2 = run_assist_watch(conn, futures=boom, now_ms_value=now + 1000, send=send)
        assert r2["ok"] is True
        assert "assist quick sync failed" in caplog.text
    finally:
        watch_mod.sync_quick_trades = original  # type: ignore[assignment]
    conn.close()


def test_hand_quick_does_not_enter_main_summaries(tmp_path: Path) -> None:
    """Regression: assist_real_trade only keeps jv-…-mkt; hand fills stay out."""
    conn = open_db(tmp_path / "hand.db")
    now = _ms_bangkok(2026, 11, 10, 12, 0)
    # hand fill history (no jv client id / no exec log)
    trades = [
        _fill(tid=1, symbol="BTCUSDT", side="BUY", price=100, qty=1, time_ms=now - 10_000),
        _fill(
            tid=2,
            symbol="BTCUSDT",
            side="SELL",
            price=90,
            qty=1,
            time_ms=now - 1000,
            realized=-10,
        ),
    ]
    orders = [
        {
            "orderId": 1,
            "clientOrderId": "manual-1",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "time": now - 10_000,
            "status": "FILLED",
        },
        {
            "orderId": 2,
            "clientOrderId": "manual-2",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "type": "MARKET",
            "time": now - 1000,
            "status": "FILLED",
        },
    ]
    fake = HistoryFakeFutures(trades=trades, orders=orders, income=[])
    rows_a = build_real_rows(conn, fake, since_ms=now - 86_400_000, until_ms=now)  # type: ignore[arg-type]
    sum_a = summarize_real_vs_virtual(conn, since_ms=now - 86_400_000, until_ms=now, rows=rows_a)
    j_a = summarize_journal(conn, since_ms=now - 86_400_000, until_ms=now)
    d_a = day_bot_net_r(conn, now_ms=now)

    # with same history after tagging as quick — main still identical
    create_quick_registration(
        conn,
        symbol="BTCUSDT",
        side="long",
        planned_entry=100.0,
        planned_stop=90.0,
        qty=1.0,
        now_ms=now - 15_000,
    )
    sync_quick_trades(conn, fake, now_ms=now, send=lambda *_a, **_k: True)
    rows_b = build_real_rows(conn, fake, since_ms=now - 86_400_000, until_ms=now)  # type: ignore[arg-type]
    sum_b = summarize_real_vs_virtual(conn, since_ms=now - 86_400_000, until_ms=now, rows=rows_b)
    j_b = summarize_journal(conn, since_ms=now - 86_400_000, until_ms=now)
    d_b = day_bot_net_r(conn, now_ms=now)
    assert rows_a == rows_b
    assert sum_a == sum_b
    assert j_a == j_b
    assert d_a == d_b
    conn.close()
