"""Tests for paper expectancy metrics."""

from pathlib import Path

from jarvise_ingest.db import ensure_paper_account, open_db
from jarvise_paper.engine import apply_signal
from jarvise_paper.metrics import (
    MIN_TRADES_FOR_RATIOS,
    compute_paper_metrics,
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
