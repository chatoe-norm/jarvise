"""1% risk-at-stop ceiling (env may lower, never raise)."""

from __future__ import annotations

from pathlib import Path

from jarvise_ingest.db import ensure_paper_account, open_db, upsert_market_technicals
from jarvise_paper.approval import enqueue_approval
from jarvise_risk.caps import (
    HARD_MAX_RISK_PER_TRADE_PCT,
    RiskCaps,
    check_caps,
    max_risk_per_trade_pct,
    risk_at_stop_pct,
)


def test_risk_at_stop_pct_formula() -> None:
    # 2% notional, 3% stop distance → 0.06% of equity (plus 0 costs)
    assert (
        abs(risk_at_stop_pct(size_pct_equity=2.0, entry=100.0, stop=97.0, round_trip_cost_bps=0.0) - 0.06)
        < 1e-9
    )


def test_env_cannot_raise_ceiling(monkeypatch) -> None:
    monkeypatch.setenv("JARVISE_MAX_RISK_PER_TRADE_PCT", "5")
    assert max_risk_per_trade_pct() == HARD_MAX_RISK_PER_TRADE_PCT
    monkeypatch.setenv("JARVISE_MAX_RISK_PER_TRADE_PCT", "0.25")
    assert max_risk_per_trade_pct() == 0.25


def test_check_caps_missing_invalidation_no_kill() -> None:
    caps = RiskCaps(max_notional_per_order=5000.0, max_daily_loss_usd=100.0, drawdown_lock_pct=5.0)
    reason = check_caps(
        caps,
        equity=10_000.0,
        starting_equity=10_000.0,
        size_pct_equity=1.5,
        action="long",
        stop_price=None,
        entry_price=100.0,
    )
    assert reason == "missing_invalidation"


def test_check_caps_blocks_over_one_percent_at_stop() -> None:
    caps = RiskCaps(max_notional_per_order=50_000.0, max_daily_loss_usd=100.0, drawdown_lock_pct=5.0)
    # 10% size, 20% stop distance → 2% equity risk
    reason = check_caps(
        caps,
        equity=10_000.0,
        starting_equity=10_000.0,
        size_pct_equity=10.0,
        action="long",
        entry_price=100.0,
        stop_price=80.0,
    )
    assert reason is not None
    assert "risk_at_stop" in reason


def test_enqueue_blocks_missing_stop_without_kill(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JARVISE_MARKET_SAFETY", "0")
    conn = open_db(tmp_path / "p.db")
    ensure_paper_account(conn)
    upsert_market_technicals(
        conn,
        [
            {
                "symbol": "BTCUSDT",
                "timestamp": 1_700_000_000_000,
                "timeframe": "4h",
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
                "volume": 1.0,
            }
        ],
    )
    row = enqueue_approval(
        conn,
        analysis={
            "analysis_id": "a1",
            "symbol": "BTCUSDT",
            "action": "long",
            "size_pct_equity": 1.5,
            "regime_state": "trend_up",
            "confidence_score": 0.7,
        },
        timeframe="4h",
        now_ms=1_000,
    )
    assert row.get("ok") is False
    assert "missing_invalidation" in str(row.get("error"))
    assert row.get("kill_switch_engaged") is False
