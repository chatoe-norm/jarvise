from pathlib import Path

from jarvise_ingest.db import (
    get_analysis_output,
    get_approval,
    get_latest_llm_review,
    insert_llm_review,
    open_db,
    set_approval_resolve_reason,
    upsert_analysis_output,
    upsert_pending_approval,
)


def _approval(conn, approval_id: str = "a1") -> dict:
    return upsert_pending_approval(
        conn,
        {
            "id": approval_id,
            "created_at_ms": 1_000,
            "expires_at_ms": 9_999_999_999_999,
            "symbol": "BTCUSDT",
            "timeframe": "4h",
            "analysis_id": "an-1",
            "action": "long",
            "regime_state": "trend_up",
            "confidence_score": 0.75,
            "invalidation_price": 97.0,
            "size_pct_equity": 1.125,
            "status": "pending",
        },
    )


def test_llm_review_roundtrip_latest_wins(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "r.db")
    _approval(conn)
    assert get_latest_llm_review(conn, "a1") is None
    insert_llm_review(
        conn,
        {
            "approval_id": "a1",
            "model": "anthropic/claude-sonnet-4.5",
            "decision": "defer",
            "reason": "first",
            "brief_hash": "h1",
            "created_at_ms": 2_000,
        },
    )
    latest = insert_llm_review(
        conn,
        {
            "approval_id": "a1",
            "model": "anthropic/claude-sonnet-4.5",
            "decision": "approve",
            "reason": "second",
            "brief_hash": "h2",
            "created_at_ms": 3_000,
        },
    )
    assert latest["decision"] == "approve"
    got = get_latest_llm_review(conn, "a1")
    assert got is not None
    assert got["reason"] == "second"
    assert got["created_at_ms"] == 3_000


def test_set_resolve_reason_only_touches_reason(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "r2.db")
    _approval(conn)
    row = set_approval_resolve_reason(conn, "a1", "auto:claude:approve")
    assert row is not None
    assert row["resolve_reason"] == "auto:claude:approve"
    assert row["status"] == "pending"
    assert get_approval(conn, "a1")["resolve_reason"] == "auto:claude:approve"
    assert set_approval_resolve_reason(conn, "missing", "x") is None


def test_get_analysis_output_by_id(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "r3.db")
    upsert_analysis_output(
        conn,
        {
            "analysis_id": "an-1",
            "timestamp": 1_700_000_000_000,
            "symbol": "BTCUSDT",
            "timeframe": "4h",
            "regime_state": "trend_up",
            "confidence_score": 0.75,
            "action": "long",
            "invalidation_price": 84159.85,
            "size_pct_equity": 1.125,
            "thesis": "trend_up: price above EMA20 and EMA200",
        },
    )
    row = get_analysis_output(conn, "an-1")
    assert row is not None
    assert row["invalidation_price"] == 84159.85
    assert get_analysis_output(conn, "nope") is None
