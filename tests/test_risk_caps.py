"""Risk caps unit + approval integration tests."""

from pathlib import Path
from unittest.mock import patch

from jarvise_ingest.db import (
    ensure_paper_account,
    get_approval,
    get_paper_position,
    list_approvals,
    list_paper_orders,
    open_db,
    set_paper_account_value,
    upsert_market_technicals,
)
from jarvise_paper.approval import approve_approval, enqueue_approval, expire_approvals
from jarvise_paper.engine import apply_signal
from jarvise_risk.caps import RiskCaps, check_caps, load_risk_caps


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


def test_check_caps_notional_and_drawdown() -> None:
    caps = RiskCaps(
        max_notional_per_order=500.0,
        max_daily_loss_usd=100.0,
        drawdown_lock_pct=5.0,
    )
    assert (
        check_caps(
            caps,
            equity=10_000.0,
            starting_equity=10_000.0,
            size_pct_equity=10.0,
            action="long",
        )
        is not None
    )
    assert (
        check_caps(
            caps,
            equity=9_400.0,
            starting_equity=10_000.0,
            action="flat",
        )
        is not None
    )  # 6% DD
    assert (
        check_caps(
            caps,
            equity=10_000.0,
            starting_equity=10_000.0,
            size_pct_equity=2.0,
            action="long",
        )
        is None
    )


def test_load_risk_caps_from_env(monkeypatch) -> None:
    monkeypatch.setenv("JARVISE_MAX_NOTIONAL_PER_ORDER", "123")
    monkeypatch.setenv("JARVISE_MAX_DAILY_LOSS_USD", "45")
    monkeypatch.setenv("JARVISE_DRAWDOWN_LOCK_PCT", "3.5")
    caps = load_risk_caps()
    assert caps.max_notional_per_order == 123.0
    assert caps.max_daily_loss_usd == 45.0
    assert caps.drawdown_lock_pct == 3.5


def test_approve_breach_fails_and_engages_kill(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JARVISE_MAX_NOTIONAL_PER_ORDER", "50")
    # Room for 10% size during enqueue; approve then hits per-order notional.
    monkeypatch.setenv("JARVISE_MAX_SYMBOL_NOTIONAL_PCT", "50")
    monkeypatch.setenv("JARVISE_MAX_GROSS_NOTIONAL_PCT", "50")
    monkeypatch.setenv("JARVISE_MAX_CORRELATED_BUCKET_PCT", "50")
    monkeypatch.delenv("REDIS_URL", raising=False)
    engaged: list[str] = []

    def fake_engage(*, reason: str = "") -> bool:
        engaged.append(reason)
        return True

    conn = open_db(tmp_path / "r.db")
    ensure_paper_account(conn)
    _seed_candle(conn, "BTCUSDT", "4h", 100.0)
    # Enqueue with high notional would also block — raise cap for enqueue only
    monkeypatch.setenv("JARVISE_MAX_NOTIONAL_PER_ORDER", "5000")
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
    monkeypatch.setenv("JARVISE_MAX_NOTIONAL_PER_ORDER", "50")
    with patch("jarvise_paper.approval.engage_kill_switch", fake_engage):
        result = approve_approval(conn, row["id"], kill_switch=False, now_ms=2_000)
    assert result["ok"] is False
    assert "max_notional" in (result["error"] or "")
    assert engaged
    assert get_approval(conn, row["id"])["status"] == "failed"
    assert get_paper_position(conn, "BTCUSDT") is None
    conn.close()


def test_enqueue_breach_skips(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JARVISE_MAX_NOTIONAL_PER_ORDER", "10")
    monkeypatch.delenv("REDIS_URL", raising=False)
    conn = open_db(tmp_path / "e.db")
    ensure_paper_account(conn)
    with patch("jarvise_paper.approval.engage_kill_switch", lambda **k: True):
        out = enqueue_approval(
            conn,
            analysis={
                "analysis_id": "x",
                "symbol": "BTCUSDT",
                "action": "long",
                "size_pct_equity": 50.0,
                "regime_state": "trend_up",
                "confidence_score": 0.9,
            },
            timeframe="4h",
            now_ms=1_000,
        )
    assert out.get("skipped") is True
    assert list_approvals(conn, status="pending") == []
    conn.close()


def test_timeout_same_side_holds(tmp_path: Path) -> None:
    """Same-side pending timeout must not FLAT an open paper position (auto-decide churn)."""
    conn = open_db(tmp_path / "f.db")
    ensure_paper_account(conn)
    _seed_candle(conn, "BTCUSDT", "4h", 100.0)
    apply_signal(
        conn,
        analysis={
            "analysis_id": "open1",
            "symbol": "BTCUSDT",
            "action": "long",
            "size_pct_equity": 5.0,
            "regime_state": "trend_up",
            "confidence_score": 0.7,
        },
        mid_price=100.0,
        timeframe="4h",
        now_ms=1_000,
    )
    assert get_paper_position(conn, "BTCUSDT") is not None
    row = enqueue_approval(
        conn,
        analysis={
            "analysis_id": "pend",
            "symbol": "BTCUSDT",
            "action": "long",
            "size_pct_equity": 5.0,
            "regime_state": "trend_up",
            "confidence_score": 0.7,
        },
        timeframe="4h",
        now_ms=2_000,
    )
    conn.execute(
        "UPDATE approval_queue SET expires_at_ms = 1500 WHERE id = ?", (row["id"],)
    )
    conn.commit()
    _seed_candle(conn, "BTCUSDT", "4h", 105.0, ts=1_700_000_100_000)
    out = expire_approvals(conn, now_ms=3_000)
    assert out["expired"] == 1
    assert out["held"] == 1
    assert out["flat_fills"] == 0
    assert get_paper_position(conn, "BTCUSDT") is not None
    assert get_approval(conn, row["id"])["status"] == "timed_out"
    assert get_approval(conn, row["id"])["resolve_reason"] == "timeout_hold"
    conn.close()


def test_timeout_opposite_side_applies_flat(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "f2.db")
    ensure_paper_account(conn)
    _seed_candle(conn, "BTCUSDT", "4h", 100.0)
    apply_signal(
        conn,
        analysis={
            "analysis_id": "open1",
            "symbol": "BTCUSDT",
            "action": "long",
            "size_pct_equity": 5.0,
            "regime_state": "trend_up",
            "confidence_score": 0.7,
        },
        mid_price=100.0,
        timeframe="4h",
        now_ms=1_000,
    )
    assert get_paper_position(conn, "BTCUSDT") is not None
    row = enqueue_approval(
        conn,
        analysis={
            "analysis_id": "pend",
            "symbol": "BTCUSDT",
            "action": "short",
            "size_pct_equity": 5.0,
            "regime_state": "trend_down",
            "confidence_score": 0.7,
        },
        timeframe="4h",
        now_ms=2_000,
    )
    conn.execute(
        "UPDATE approval_queue SET expires_at_ms = 1500 WHERE id = ?", (row["id"],)
    )
    conn.commit()
    _seed_candle(conn, "BTCUSDT", "4h", 105.0, ts=1_700_000_100_000)
    out = expire_approvals(conn, now_ms=3_000)
    assert out["expired"] == 1
    assert out["flat_fills"] >= 1
    assert out.get("held", 0) == 0
    assert get_paper_position(conn, "BTCUSDT") is None
    assert get_approval(conn, row["id"])["status"] == "timed_out"
    assert get_approval(conn, row["id"])["resolve_reason"] == "timeout_flat"
    assert list_paper_orders(conn)
    conn.close()


def test_portfolio_blocks_third_symbol_without_kill_switch(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("JARVISE_MAX_OPEN_POSITIONS", "2")
    monkeypatch.setenv("JARVISE_MAX_SYMBOL_NOTIONAL_PCT", "50")
    monkeypatch.setenv("JARVISE_MAX_GROSS_NOTIONAL_PCT", "50")
    monkeypatch.setenv("JARVISE_MAX_CORRELATED_BUCKET_PCT", "50")
    monkeypatch.setenv("JARVISE_MAX_NOTIONAL_PER_ORDER", "5000")
    monkeypatch.delenv("REDIS_URL", raising=False)
    engaged: list[str] = []
    conn = open_db(tmp_path / "port.db")
    ensure_paper_account(conn)
    for i, sym in enumerate(("BTCUSDT", "ETHUSDT")):
        _seed_candle(conn, sym, "4h", 100.0, ts=1_700_000_000_000 + i)
        apply_signal(
            conn,
            analysis={
                "analysis_id": f"o{i}",
                "symbol": sym,
                "action": "long",
                "size_pct_equity": 2.0,
                "regime_state": "trend_up",
                "confidence_score": 0.7,
            },
            mid_price=100.0,
            timeframe="4h",
            now_ms=1_000 + i,
        )
    _seed_candle(conn, "SOLUSDT", "4h", 50.0, ts=1_700_000_000_100)
    with patch("jarvise_paper.approval.engage_kill_switch", lambda **k: engaged.append("x") or True):
        out = enqueue_approval(
            conn,
            analysis={
                "analysis_id": "sol1",
                "symbol": "SOLUSDT",
                "action": "long",
                "size_pct_equity": 2.0,
                "regime_state": "trend_up",
                "confidence_score": 0.7,
            },
            timeframe="4h",
            now_ms=3_000,
        )
    assert out.get("skipped") is True
    assert "max_open_positions" in (out.get("error") or "")
    assert out.get("kill_switch_engaged") is False
    assert engaged == []
    conn.close()


def test_portfolio_bucket_cap(tmp_path: Path, monkeypatch) -> None:
    from jarvise_risk.caps import RiskCaps, check_portfolio_caps

    caps = RiskCaps(
        max_notional_per_order=5000.0,
        max_daily_loss_usd=100.0,
        drawdown_lock_pct=5.0,
        max_open_positions=5,
        max_gross_notional_pct=50.0,
        max_symbol_notional_pct=50.0,
        max_correlated_bucket_pct=10.0,
    )
    positions = [
        {
            "symbol": "BTCUSDT",
            "side": "long",
            "qty": 1.0,
            "entry_price": 600.0,  # 6% of 10k
            "entry_ts": 1,
            "unrealized_pnl": 0.0,
            "realized_pnl": 0.0,
        }
    ]
    # ETH candidate 5% → bucket 11% > 10%
    breach = check_portfolio_caps(
        caps,
        equity=10_000.0,
        positions=positions,
        symbol="ETHUSDT",
        action="long",
        size_pct_equity=5.0,
    )
    assert breach is not None
    assert "crypto_majors" in breach


def test_drawdown_blocks_approve(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JARVISE_DRAWDOWN_LOCK_PCT", "5")
    monkeypatch.setenv("JARVISE_MAX_NOTIONAL_PER_ORDER", "5000")
    monkeypatch.delenv("REDIS_URL", raising=False)
    conn = open_db(tmp_path / "dd.db")
    ensure_paper_account(conn)
    set_paper_account_value(conn, "equity", 9_400.0)  # 6% DD
    set_paper_account_value(conn, "cash", 9_400.0)
    conn.commit()
    _seed_candle(conn, "BTCUSDT", "4h", 100.0)
    row = enqueue_approval(
        conn,
        analysis={
            "analysis_id": "dd1",
            "symbol": "BTCUSDT",
            "action": "flat",
            "size_pct_equity": 0.0,
            "regime_state": "range",
            "confidence_score": 0.4,
        },
        timeframe="4h",
        now_ms=1_000,
    )
    # flat enqueue: drawdown still checked
    if row.get("skipped"):
        assert "drawdown" in (row.get("error") or "")
        conn.close()
        return
    with patch("jarvise_paper.approval.engage_kill_switch", lambda **k: True):
        result = approve_approval(conn, row["id"], kill_switch=False, now_ms=2_000)
    assert result["ok"] is False
    assert "drawdown" in (result["error"] or "")
    conn.close()
