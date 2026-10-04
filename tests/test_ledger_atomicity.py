"""Approve fill must be one SQLite transaction (claim + ledger + order ids)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from jarvise_ingest.db import (
    ensure_paper_account,
    get_approval,
    get_paper_account,
    list_paper_orders,
    open_db,
    transaction,
    upsert_market_technicals,
)
from jarvise_paper.approval import approve_approval, enqueue_approval


def _seed_candle(conn, symbol: str, timeframe: str, close: float, ts: int = 1_700_000_000_000) -> None:
    upsert_market_technicals(
        conn,
        [
            {
                "symbol": symbol,
                "timestamp": ts,
                "timeframe": timeframe,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 1.0,
            }
        ],
    )


def test_transaction_rolls_back_on_error(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "tx.db")
    ensure_paper_account(conn)
    conn.execute("CREATE TABLE IF NOT EXISTS tx_probe (id INTEGER PRIMARY KEY, n INTEGER)")
    conn.commit()
    try:
        with transaction(conn):
            conn.execute("INSERT INTO tx_probe (n) VALUES (1)")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    rows = conn.execute("SELECT COUNT(*) FROM tx_probe").fetchone()
    assert int(rows[0]) == 0


def test_approve_rolls_back_fill_when_position_upsert_fails(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "p.db")
    ensure_paper_account(conn)
    _seed_candle(conn, "BTCUSDT", "4h", 100.0)
    row = enqueue_approval(
        conn,
        analysis={
            "analysis_id": "a1",
            "symbol": "BTCUSDT",
            "action": "long",
            "size_pct_equity": 2.0,
            "regime_state": "trend_up",
            "confidence_score": 0.7,
            "invalidation_price": 97.0,
        },
        timeframe="4h",
        now_ms=1_000,
    )
    cash_before = get_paper_account(conn)["cash"]

    def boom(*_a, **_k):
        raise RuntimeError("injected upsert failure")

    with patch("jarvise_paper.engine.upsert_paper_position", side_effect=boom):
        result = approve_approval(conn, row["id"], kill_switch=False, now_ms=2_000)

    assert result["ok"] is False
    assert list_paper_orders(conn) == []
    assert get_paper_account(conn)["cash"] == cash_before
    assert get_approval(conn, row["id"])["status"] == "failed"
