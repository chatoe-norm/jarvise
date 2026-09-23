"""Unit tests for paper fill engine."""

from pathlib import Path

from jarvise_ingest.db import (
    ensure_paper_account,
    get_paper_account,
    get_paper_position,
    list_paper_orders,
    open_db,
)
from jarvise_paper.engine import apply_signal, fee_usd, fill_price


def test_fill_price_adverse():
    assert fill_price(100.0, side="buy") > 100.0
    assert fill_price(100.0, side="sell") < 100.0


def test_fee_usd():
    assert abs(fee_usd(10_000.0) - 5.0) < 1e-9


def test_long_then_flat_realizes_pnl(tmp_path: Path):
    db = tmp_path / "p.db"
    conn = open_db(db)
    ensure_paper_account(conn)

    open_res = apply_signal(
        conn,
        analysis={
            "analysis_id": "a1",
            "symbol": "BTCUSDT",
            "action": "long",
            "size_pct_equity": 10.0,
            "regime_state": "trend_up",
            "confidence_score": 0.7,
        },
        mid_price=100.0,
        timeframe="4h",
        now_ms=1_000,
    )
    assert open_res["fills"]
    assert open_res["fills"][0]["side"] == "buy"
    pos = get_paper_position(conn, "BTCUSDT")
    assert pos is not None
    assert pos["side"] == "long"

    close_res = apply_signal(
        conn,
        analysis={
            "analysis_id": "a2",
            "symbol": "BTCUSDT",
            "action": "flat",
            "size_pct_equity": 0.0,
            "regime_state": "range",
            "confidence_score": 0.4,
        },
        mid_price=110.0,
        timeframe="4h",
        now_ms=2_000,
    )
    assert close_res["realized_delta"] > 0
    assert get_paper_position(conn, "BTCUSDT") is None
    acct = get_paper_account(conn)
    assert acct["equity"] > 9_900  # fees take a little, but price up
    assert len(list_paper_orders(conn)) >= 2
    conn.close()


def test_dry_run_writes_nothing(tmp_path: Path):
    db = tmp_path / "p.db"
    conn = open_db(db)
    ensure_paper_account(conn)
    apply_signal(
        conn,
        analysis={
            "analysis_id": "a1",
            "symbol": "ETHUSDT",
            "action": "long",
            "size_pct_equity": 5.0,
            "regime_state": "trend_up",
            "confidence_score": 0.6,
        },
        mid_price=50.0,
        timeframe="1h",
        dry_run=True,
        now_ms=1_000,
    )
    assert get_paper_position(conn, "ETHUSDT") is None
    assert list_paper_orders(conn) == []
    acct = get_paper_account(conn)
    assert acct["cash"] == 10_000.0
    conn.close()
