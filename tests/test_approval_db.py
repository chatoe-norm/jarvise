from pathlib import Path

from jarvise_ingest.db import (
    expire_pending_approvals,
    get_approval,
    list_approvals,
    open_db,
    resolve_approval,
    upsert_pending_approval,
)


def test_upsert_pending_dedupes_symbol_timeframe(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "a.db")
    first = upsert_pending_approval(
        conn,
        {
            "id": "appr1",
            "created_at_ms": 1_000,
            "expires_at_ms": 3_600_000,
            "symbol": "BTCUSDT",
            "timeframe": "4h",
            "analysis_id": "a1",
            "action": "long",
            "regime_state": "trend_up",
            "confidence_score": 0.7,
            "size_pct_equity": 5.0,
            "status": "pending",
        },
    )
    second = upsert_pending_approval(
        conn,
        {
            "id": "appr_new_ignored",
            "created_at_ms": 2_000,
            "expires_at_ms": 4_000_000,
            "symbol": "BTCUSDT",
            "timeframe": "4h",
            "analysis_id": "a2",
            "action": "flat",
            "regime_state": "range",
            "confidence_score": 0.4,
            "size_pct_equity": 0.0,
            "status": "pending",
        },
    )
    assert second["id"] == first["id"]
    assert second["analysis_id"] == "a2"
    assert second["action"] == "flat"
    assert second["created_at_ms"] == 2_000
    assert len(list_approvals(conn, status="pending")) == 1


def test_resolve_and_expire(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "b.db")
    row = upsert_pending_approval(
        conn,
        {
            "id": "x1",
            "created_at_ms": 1_000,
            "expires_at_ms": 1_500,
            "symbol": "ETHUSDT",
            "timeframe": "1h",
            "analysis_id": "e1",
            "action": "long",
            "regime_state": "trend_up",
            "confidence_score": 0.8,
            "size_pct_equity": 2.0,
            "status": "pending",
        },
    )
    assert resolve_approval(conn, row["id"], status="rejected", resolve_reason="nope")["status"] == "rejected"
    assert resolve_approval(conn, row["id"], status="approved") is None  # not pending

    upsert_pending_approval(
        conn,
        {
            "id": "x2",
            "created_at_ms": 1_000,
            "expires_at_ms": 1_500,
            "symbol": "SOLUSDT",
            "timeframe": "1h",
            "analysis_id": "s1",
            "action": "short",
            "regime_state": "trend_down",
            "confidence_score": 0.6,
            "size_pct_equity": 1.0,
            "status": "pending",
        },
    )
    assert expire_pending_approvals(conn, now_ms=2_000) == 1
    assert get_approval(conn, "x2")["status"] == "timed_out"
