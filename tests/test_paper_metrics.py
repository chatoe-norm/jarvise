"""Tests for paper expectancy metrics."""

from pathlib import Path

from jarvise_ingest.db import ensure_paper_account, open_db
from jarvise_paper.engine import apply_signal
from jarvise_paper.metrics import (
    MIN_TRADES_FOR_RATIOS,
    compute_paper_metrics,
    loss_lesson,
    persist_metrics_snapshot,
    reconstruct_closed_trades,
)


def _long_flat(conn, *, sym: str, open_mid: float, close_mid: float, t0: int, size: float = 10.0):
    apply_signal(
        conn,
        analysis={
            "analysis_id": f"o{t0}",
            "symbol": sym,
            "action": "long",
            "invalidation_price": 97.0,
            "size_pct_equity": size,
            "regime_state": "trend_up",
            "confidence_score": 0.7,
        },
        mid_price=open_mid,
        timeframe="4h",
        now_ms=t0,
    )
    apply_signal(
        conn,
        analysis={
            "analysis_id": f"c{t0}",
            "symbol": sym,
            "action": "flat",
            "invalidation_price": 97.0,
            "size_pct_equity": 0.0,
            "regime_state": "range",
            "confidence_score": 0.4,
        },
        mid_price=close_mid,
        timeframe="4h",
        now_ms=t0 + 1,
    )


def test_empty_ledger_metrics(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "e.db")
    ensure_paper_account(conn)
    report = compute_paper_metrics(conn, now_ms=1)
    assert report["ok"] is True
    assert report["closed_trades"] == 0
    assert report["expected_value_ev"] is None
    assert report["sharpe_ratio"] is None
    assert report["ratios_ready"] is False
    conn.close()


def test_win_and_loss_expectancy(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "p.db")
    ensure_paper_account(conn)
    _long_flat(conn, sym="BTCUSDT", open_mid=100.0, close_mid=110.0, t0=1_000)
    _long_flat(conn, sym="ETHUSDT", open_mid=50.0, close_mid=40.0, t0=2_000)
    report = compute_paper_metrics(conn, now_ms=3_000)
    assert report["closed_trades"] == 2
    assert report["wins"] == 1
    assert report["losses"] == 1
    assert report["win_rate"] == 0.5
    assert report["expected_value_ev"] is not None
    assert report["sharpe_ratio"] is None  # < 30
    assert report["max_drawdown_pct"] is not None
    conn.close()


def test_scratch_excluded_from_win_rate() -> None:
    # Round-trips below: +10 (win), 0 (scratch), -5 (loss) via the reconstruct path.
    orders = [
        {
            "order_id": "o1",
            "ts": 1,
            "symbol": "BTCUSDT",
            "side": "buy",
            "qty": 1.0,
            "price": 100.0,
            "fee_usd": 0.0,
            "reason": "open_long",
        },
        {
            "order_id": "c1",
            "ts": 2,
            "symbol": "BTCUSDT",
            "side": "sell",
            "qty": 1.0,
            "price": 100.0,
            "fee_usd": 0.0,
            "reason": "close_long",
        },
        {
            "order_id": "o2",
            "ts": 3,
            "symbol": "ETHUSDT",
            "side": "buy",
            "qty": 1.0,
            "price": 50.0,
            "fee_usd": 0.0,
            "reason": "open_long",
        },
        {
            "order_id": "c2",
            "ts": 4,
            "symbol": "ETHUSDT",
            "side": "sell",
            "qty": 1.0,
            "price": 60.0,
            "fee_usd": 0.0,
            "reason": "close_long",
        },
    ]
    closed, unmatched, open_rem = reconstruct_closed_trades(orders)
    assert unmatched == 0
    assert open_rem == 0
    assert len(closed) == 2
    pnls = [t["pnl_usd"] for t in closed]
    assert 0.0 in pnls
    assert 10.0 in pnls


def test_stop_hit_counts_as_closed_round_trip() -> None:
    orders = [
        {
            "order_id": "o1",
            "ts": 1,
            "symbol": "BTCUSDT",
            "side": "buy",
            "qty": 0.01,
            "price": 100.0,
            "fee_usd": 0.1,
            "reason": "open_long",
        },
        {
            "order_id": "s1",
            "ts": 2,
            "symbol": "BTCUSDT",
            "side": "sell",
            "qty": 0.01,
            "price": 90.0,
            "fee_usd": 0.1,
            "reason": "stop_hit",
        },
        {
            "order_id": "o2",
            "ts": 3,
            "symbol": "ETHUSDT",
            "side": "sell",
            "qty": 1.0,
            "price": 50.0,
            "fee_usd": 0.05,
            "reason": "open_short",
        },
        {
            "order_id": "s2",
            "ts": 4,
            "symbol": "ETHUSDT",
            "side": "buy",
            "qty": 1.0,
            "price": 55.0,
            "fee_usd": 0.05,
            "reason": "stop_hit",
        },
    ]
    trades, unmatched, open_rem = reconstruct_closed_trades(orders)
    assert unmatched == 0
    assert open_rem == 0
    assert len(trades) == 2
    by_symbol = {t["symbol"]: t for t in trades}
    assert by_symbol["BTCUSDT"]["side"] == "long"
    assert by_symbol["BTCUSDT"]["pnl_usd"] < 0
    assert by_symbol["ETHUSDT"]["side"] == "short"
    assert by_symbol["ETHUSDT"]["pnl_usd"] < 0


def test_orphan_close_counted_unmatched() -> None:
    orders = [
        {
            "order_id": "c1",
            "ts": 1,
            "symbol": "BTCUSDT",
            "side": "sell",
            "qty": 1.0,
            "price": 100.0,
            "fee_usd": 0.0,
            "reason": "close_long",
        }
    ]
    trades, unmatched, open_rem = reconstruct_closed_trades(orders)
    assert trades == []
    assert unmatched == 1
    assert open_rem == 0


def test_sharpe_null_until_threshold(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "s.db")
    ensure_paper_account(conn)
    for i in range(5):
        mid = 100.0 + (i % 2) * 10
        close = mid + 5
        _long_flat(
            conn,
            sym="BTCUSDT",
            open_mid=mid,
            close_mid=close,
            t0=10_000 + i * 10,
            size=2.0,
        )
    report = compute_paper_metrics(conn)
    assert report["closed_trades"] == 5
    assert report["closed_trades"] < MIN_TRADES_FOR_RATIOS
    assert report["sharpe_ratio"] is None
    assert report["sortino_ratio"] is None
    conn.close()


def test_persist_snapshot(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "m.db")
    ensure_paper_account(conn)
    _long_flat(conn, sym="BTCUSDT", open_mid=100.0, close_mid=105.0, t0=1_000)
    report = compute_paper_metrics(conn, now_ms=9_999)
    persist_metrics_snapshot(conn, report)
    row = conn.execute(
        "SELECT expected_value_ev, max_drawdown_pct FROM performance_risk_metrics "
        "WHERE strategy_id='paper' AND timestamp=9999"
    ).fetchone()
    assert row is not None
    assert row[0] is not None
    conn.close()


def _leg(
    order_id: str,
    ts: int,
    symbol: str,
    reason: str,
    price: float,
    *,
    source: str | None = None,
) -> dict:
    row = {
        "order_id": order_id,
        "ts": ts,
        "symbol": symbol,
        "side": "buy" if reason.startswith("open_") else "sell",
        "qty": 1.0,
        "price": price,
        "fee_usd": 0.0,
        "reason": reason,
    }
    if source is not None:
        row["decision_source"] = source
    return row


def test_loss_lesson_exit_mix_symbol_and_floors() -> None:
    orders = [
        _leg("o1", 1, "BTCUSDT", "open_long", 100.0, source="auto_claude"),
        _leg("c1", 2, "BTCUSDT", "stop_hit", 90.0),
        _leg("o2", 3, "ETHUSDT", "open_long", 50.0, source="auto_claude"),
        _leg("c2", 4, "ETHUSDT", "close_long", 40.0),
        _leg("o3", 5, "BTCUSDT", "open_long", 100.0, source="auto_rule"),
        _leg("c3", 6, "BTCUSDT", "close_long", 110.0),
    ]
    trades, unmatched, open_rem = reconstruct_closed_trades(orders)
    assert unmatched == 0 and open_rem == 0
    assert [t["exit_reason"] for t in trades] == ["stop_hit", "close_long", "close_long"]
    for trade, source in zip(trades, ("auto_claude", "auto_claude", "auto_rule"), strict=True):
        trade["decision_source"] = source
    lesson = loss_lesson(trades)
    assert lesson["closed_trades"] == 3
    assert lesson["wins"] == 1
    assert lesson["losses"] == 2
    assert lesson["by_exit"]["stop_hit"] == {"n": 1, "pnl_usd": -10.0}
    assert lesson["by_exit"]["flat"] == {"n": 1, "pnl_usd": -10.0}
    assert lesson["by_symbol"]["ETHUSDT"] == {"n": 1, "pnl_usd": -10.0}
    assert lesson["by_symbol"]["BTCUSDT"]["n"] == 2
    assert lesson["by_symbol"]["BTCUSDT"]["pnl_usd"] == 0.0
    assert lesson["auto_closed"] == 3
    assert lesson["auto_ev"] is not None and lesson["auto_ev"] < 0
    assert lesson["floors_unchanged"] is True
    assert lesson["by_exit"]["manual"] == {"n": 0, "pnl_usd": 0.0}


def test_loss_lesson_manual_close_is_not_a_flat_exit() -> None:
    orders = [
        _leg("o1", 1, "SOLUSDT", "open_long", 100.0),
        _leg("c1", 2, "SOLUSDT", "close_manual", 95.0),
        _leg("o2", 3, "SOLUSDT", "open_long", 100.0),
        _leg("c2", 4, "SOLUSDT", "close_long", 98.0),
    ]
    trades, _, _ = reconstruct_closed_trades(orders)
    lesson = loss_lesson(trades)
    assert lesson["by_exit"]["manual"] == {"n": 1, "pnl_usd": -5.0}
    assert lesson["by_exit"]["flat"] == {"n": 1, "pnl_usd": -2.0}

    losing_at_gate = [
        {
            "symbol": "BTCUSDT",
            "pnl_usd": -1.0,
            "exit_reason": "stop_hit",
            "decision_source": "auto_claude",
        }
        for _ in range(8)
    ]
    held = loss_lesson(losing_at_gate)
    assert held["auto_closed"] == 8
    assert held["auto_ev"] is not None and held["auto_ev"] < 0
    assert held["floors_unchanged"] is True

    positive = [
        {
            "symbol": "BTCUSDT",
            "pnl_usd": 1.0,
            "exit_reason": "close_long",
            "decision_source": "auto_claude",
        }
        for _ in range(8)
    ]
    cleared = loss_lesson(positive)
    assert cleared["auto_closed"] == 8
    assert cleared["auto_ev"] == 1.0
    assert cleared["floors_unchanged"] is False
    short = positive[:7]
    assert loss_lesson(short)["floors_unchanged"] is True
    assert loss_lesson([])["floors_unchanged"] is True
