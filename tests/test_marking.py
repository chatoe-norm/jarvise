"""Mark-to-market equity from latest closes (not entry prices)."""

from __future__ import annotations

from pathlib import Path

from jarvise_ingest.db import (
    ensure_paper_account,
    get_paper_account,
    open_db,
    upsert_market_technicals,
)
from jarvise_paper.engine import apply_signal
from jarvise_paper.marking import mark_to_market


def test_mtm_marks_second_symbol_from_stored_close(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "p.db")
    ensure_paper_account(conn)
    apply_signal(
        conn,
        analysis={
            "analysis_id": "a1",
            "symbol": "BTCUSDT",
            "action": "long",
            "size_pct_equity": 1.5,
            "invalidation_price": 97.0,
            "regime_state": "trend_up",
            "confidence_score": 0.7,
        },
        mid_price=100.0,
        timeframe="4h",
        now_ms=1_000,
    )
    apply_signal(
        conn,
        analysis={
            "analysis_id": "a2",
            "symbol": "ETHUSDT",
            "action": "long",
            "size_pct_equity": 1.5,
            "invalidation_price": 48.0,
            "regime_state": "trend_up",
            "confidence_score": 0.7,
        },
        mid_price=50.0,
        timeframe="4h",
        now_ms=2_000,
    )
    upsert_market_technicals(
        conn,
        [
            {
                "symbol": "BTCUSDT",
                "timestamp": 3_000,
                "timeframe": "4h",
                "open": 120.0,
                "high": 120.0,
                "low": 120.0,
                "close": 120.0,
                "volume": 1.0,
            }
        ],
    )
    out = mark_to_market(conn, now_ms=3_000, persist=True)
    acct = get_paper_account(conn)
    assert out["equity"] == acct["equity"]
    # BTC marked at 120 not leftover 100 entry
    assert acct["equity"] > 10_000.0
