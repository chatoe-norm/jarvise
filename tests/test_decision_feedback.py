"""T2.1 decision feedback: attribution, metrics by source, soft EV gate."""

from __future__ import annotations

from pathlib import Path

from jarvise_ingest.db import (
    SCHEMA_VERSION,
    ensure_paper_account,
    insert_paper_order,
    open_db,
    schema_version,
    upsert_market_technicals,
)
from jarvise_paper.approval import approve_approval, enqueue_approval
from jarvise_paper.auto_decide import AutoDecideConfig, run_auto_decide
from jarvise_paper.engine import apply_signal
from jarvise_paper.feedback import (
    auto_ev_gate_status,
    decision_source_from_reason,
    metrics_by_decision_source,
)
from jarvise_paper.metrics import compute_paper_metrics


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


def test_decision_source_from_reason() -> None:
    assert decision_source_from_reason("auto:claude:approve") == "auto_claude"
    assert decision_source_from_reason("auto:rule:same_side_hold") == "auto_rule"
    assert decision_source_from_reason("paper_fill") == "manual"
    assert decision_source_from_reason(None) == "manual"


def test_schema_version_seven_has_feedback_tables(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "fb.db")
    assert schema_version(conn) == SCHEMA_VERSION
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "paper_decision_outcomes" in tables
    assert "paper_auto_runs" in tables
    cols = {r[1] for r in conn.execute("PRAGMA table_info(paper_orders)").fetchall()}
    assert "approval_id" in cols and "decision_source" in cols
    conn.close()


def test_approve_stamps_approval_id_on_fills(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("JARVISE_LIVE_TRADING", raising=False)
    conn = open_db(tmp_path / "ap.db")
    ensure_paper_account(conn)
    _seed_candle(conn, "BTCUSDT", "4h", 100.0)
    row = enqueue_approval(
        conn,
        analysis={
            "analysis_id": "a1",
            "symbol": "BTCUSDT",
            "action": "long",
            "invalidation_price": 97.0,
            "size_pct_equity": 1.5,
            "regime_state": "trend_up",
            "confidence_score": 0.8,
        },
        timeframe="4h",
        now_ms=1_000,
    )
    result = approve_approval(
        conn,
        row["id"],
        kill_switch=False,
        now_ms=2_000,
        decision_source="auto_claude",
        resolve_reason="auto:claude:approve",
    )
    assert result["ok"] is True, result
    fills = result["fills"]
    assert fills
    assert all(f.get("approval_id") == row["id"] for f in fills)
    assert all(f.get("decision_source") == "auto_claude" for f in fills)
    approval = result["approval"]
    assert approval["resolve_reason"] == "auto:claude:approve"
    conn.close()


def test_metrics_split_by_decision_source(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "m.db")
    ensure_paper_account(conn)
    # Manual round-trip win
    apply_signal(
        conn,
        analysis={
            "analysis_id": "o1",
            "symbol": "BTCUSDT",
            "action": "long",
            "invalidation_price": 97.0,
            "size_pct_equity": 10.0,
            "regime_state": "trend_up",
            "confidence_score": 0.7,
        },
        mid_price=100.0,
        timeframe="4h",
        now_ms=1_000,
        approval_id="m1",
        decision_source="manual",
    )
    apply_signal(
        conn,
        analysis={
            "analysis_id": "c1",
            "symbol": "BTCUSDT",
            "action": "flat",
            "invalidation_price": 97.0,
            "size_pct_equity": 0.0,
            "regime_state": "range",
            "confidence_score": 0.4,
        },
        mid_price=110.0,
        timeframe="4h",
        now_ms=2_000,
        approval_id="m1c",
        decision_source="manual",
    )
    # Auto round-trip loss
    apply_signal(
        conn,
        analysis={
            "analysis_id": "o2",
            "symbol": "ETHUSDT",
            "action": "long",
            "invalidation_price": 97.0,
            "size_pct_equity": 10.0,
            "regime_state": "trend_up",
            "confidence_score": 0.7,
        },
        mid_price=50.0,
        timeframe="4h",
        now_ms=3_000,
        approval_id="a1",
        decision_source="auto_claude",
    )
    apply_signal(
        conn,
        analysis={
            "analysis_id": "c2",
            "symbol": "ETHUSDT",
            "action": "flat",
            "invalidation_price": 97.0,
            "size_pct_equity": 0.0,
            "regime_state": "range",
            "confidence_score": 0.4,
        },
        mid_price=40.0,
        timeframe="4h",
        now_ms=4_000,
        approval_id="a1c",
        decision_source="auto_claude",
    )
    report = compute_paper_metrics(conn, now_ms=5_000)
    assert report["closed_trades"] == 2
    by_src = report["by_decision_source"]
    assert "manual" in by_src and "auto_claude" in by_src
    assert by_src["manual"]["closed_trades"] == 1
    assert by_src["auto_claude"]["closed_trades"] == 1
    assert by_src["manual"]["expected_value_ev"] is not None
    assert by_src["auto_claude"]["expected_value_ev"] is not None
    assert by_src["manual"]["expected_value_ev"] > 0
    assert by_src["auto_claude"]["expected_value_ev"] < 0
    assert "auto" in by_src
    conn.close()


def test_auto_ev_gate_blocks_when_negative(tmp_path: Path, monkeypatch) -> None:
    conn = open_db(tmp_path / "g.db")
    ensure_paper_account(conn)
    now = 10_000_000
    # Seed enough losing auto outcomes.
    for i in range(10):
        open_id = f"o{i}"
        close_id = f"c{i}"
        insert_paper_order(
            conn,
            {
                "order_id": open_id,
                "ts": now - 1_000 + i,
                "symbol": "BTCUSDT",
                "timeframe": "4h",
                "side": "buy",
                "qty": 1.0,
                "price": 100.0,
                "fee_usd": 0.0,
                "fee_bps": 0.0,
                "slip_bps": 0.0,
                "analysis_id": f"a{i}",
                "reason": "open_long",
                "approval_id": f"ap{i}",
                "decision_source": "auto_claude",
            },
        )
        insert_paper_order(
            conn,
            {
                "order_id": close_id,
                "ts": now - 500 + i,
                "symbol": "BTCUSDT",
                "timeframe": "4h",
                "side": "sell",
                "qty": 1.0,
                "price": 90.0,
                "fee_usd": 0.0,
                "fee_bps": 0.0,
                "slip_bps": 0.0,
                "analysis_id": f"b{i}",
                "reason": "close_long",
                "approval_id": f"ap{i}",
                "decision_source": "auto_claude",
            },
        )
    conn.commit()
    compute_paper_metrics(conn, now_ms=now)
    monkeypatch.setenv("JARVISE_AUTO_DECIDE_MIN_EV", "0")
    monkeypatch.setenv("JARVISE_AUTO_DECIDE_MIN_EV_N", "5")
    gate = auto_ev_gate_status(conn, now_ms=now)
    assert gate["enabled"] is True
    assert gate["blocked"] is True
    assert gate["n"] >= 5
    assert gate["ev"] is not None and gate["ev"] < 0

    monkeypatch.setenv("JARVISE_PAPER_AUTO_DECIDE", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    cfg = AutoDecideConfig(
        enabled=True,
        model="test",
        min_conf=0.55,
        max_per_run=4,
        timeout_s=5.0,
        api_key="test-key",
    )
    out = run_auto_decide(
        conn, now_ms=now, config=cfg, chat=lambda *a, **k: {"decision": "approve", "reason": "x"}
    )
    assert out.get("skipped") is True
    assert out.get("reason") == "auto_ev_gate"
    runs = conn.execute("SELECT COUNT(*) FROM paper_auto_runs").fetchone()[0]
    assert runs >= 1
    conn.close()


def test_metrics_by_source_empty(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "e.db")
    assert metrics_by_decision_source(conn) == {}
    conn.close()
