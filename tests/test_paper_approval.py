from pathlib import Path

from jarvise_ingest.db import (
    ensure_paper_account,
    get_paper_position,
    list_approvals,
    list_paper_orders,
    open_db,
    upsert_market_technicals,
)
from jarvise_paper.approval import (
    approve_approval,
    enqueue_approval,
    expire_approvals,
    reject_approval,
)


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


def test_enqueue_approve_fills_paper(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "p.db")
    ensure_paper_account(conn)
    _seed_candle(conn, "BTCUSDT", "4h", 100.0)
    row = enqueue_approval(
        conn,
        analysis={
            "analysis_id": "a1",
            "symbol": "BTCUSDT",
            "action": "long",
            "size_pct_equity": 10.0,
            "regime_state": "trend_up",
            "confidence_score": 0.7,
        },
        timeframe="4h",
        now_ms=1_000,
    )
    assert row["status"] == "pending"
    assert get_paper_position(conn, "BTCUSDT") is None

    result = approve_approval(conn, row["id"], kill_switch=False, now_ms=2_000)
    assert result["ok"] is True
    assert result["approval"]["status"] == "approved"
    assert get_paper_position(conn, "BTCUSDT") is not None
    assert list_paper_orders(conn)


def test_reject_and_expire_do_not_fill(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "p2.db")
    ensure_paper_account(conn)
    _seed_candle(conn, "ETHUSDT", "4h", 50.0)
    row = enqueue_approval(
        conn,
        analysis={
            "analysis_id": "e1",
            "symbol": "ETHUSDT",
            "action": "long",
            "size_pct_equity": 5.0,
            "regime_state": "trend_up",
            "confidence_score": 0.6,
        },
        timeframe="4h",
        now_ms=1_000,
    )
    reject_approval(conn, row["id"], reason="nope", now_ms=1_100)
    assert get_paper_position(conn, "ETHUSDT") is None

    row2 = enqueue_approval(
        conn,
        analysis={
            "analysis_id": "e2",
            "symbol": "ETHUSDT",
            "action": "short",
            "size_pct_equity": 5.0,
            "regime_state": "trend_down",
            "confidence_score": 0.6,
        },
        timeframe="4h",
        now_ms=1_000,
    )
    conn.execute("UPDATE approval_queue SET expires_at_ms = 500 WHERE id = ?", (row2["id"],))
    conn.commit()
    out = expire_approvals(conn, now_ms=2_000)
    assert out["expired"] == 1
    assert get_paper_position(conn, "ETHUSDT") is None
    assert list_approvals(conn, status="timed_out")


def test_kill_switch_blocks_approve(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "p3.db")
    ensure_paper_account(conn)
    _seed_candle(conn, "BTCUSDT", "4h", 100.0)
    row = enqueue_approval(
        conn,
        analysis={
            "analysis_id": "a1",
            "symbol": "BTCUSDT",
            "action": "long",
            "size_pct_equity": 10.0,
            "regime_state": "trend_up",
            "confidence_score": 0.7,
        },
        timeframe="4h",
        now_ms=1_000,
    )
    result = approve_approval(conn, row["id"], kill_switch=True, now_ms=2_000)
    assert result["ok"] is False
    assert "kill" in (result["error"] or "").lower()
    assert row["id"] == list_approvals(conn, status="pending")[0]["id"]
