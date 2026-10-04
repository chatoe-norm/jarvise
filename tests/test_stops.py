"""Stop evaluation and monitor (doctrine: FLAT when invalidation is hit)."""

from __future__ import annotations

from pathlib import Path

from jarvise_ingest.db import (
    ensure_paper_account,
    get_paper_position,
    list_paper_orders,
    open_db,
    upsert_analysis_output,
    upsert_market_technicals,
    write_indicators,
)
from jarvise_paper.engine import apply_signal
from jarvise_paper.stops import backfill_stops, evaluate_stop, run_stop_monitor


def test_evaluate_stop_long_gap_and_bar_low() -> None:
    pos = {
        "symbol": "BTCUSDT",
        "side": "long",
        "qty": 1.0,
        "entry_price": 100.0,
        "stop_price": 97.0,
    }
    assert evaluate_stop(pos, mid=98.0, low_since_entry=98.5, high_since_entry=101.0) is None
    hit_low = evaluate_stop(pos, mid=98.0, low_since_entry=96.5, high_since_entry=101.0)
    assert hit_low is not None
    assert hit_low.fill_mid == 96.5
    hit_gap = evaluate_stop(pos, mid=95.0, low_since_entry=95.0, high_since_entry=99.0)
    assert hit_gap is not None
    assert hit_gap.fill_mid == 95.0


def test_evaluate_stop_short() -> None:
    pos = {
        "symbol": "ETHUSDT",
        "side": "short",
        "qty": 1.0,
        "entry_price": 50.0,
        "stop_price": 52.0,
    }
    assert evaluate_stop(pos, mid=51.0, low_since_entry=49.0, high_since_entry=51.5) is None
    hit = evaluate_stop(pos, mid=53.0, low_since_entry=50.0, high_since_entry=53.0)
    assert hit is not None
    assert hit.fill_mid == 53.0


def test_apply_signal_refuses_open_without_stop(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "p.db")
    ensure_paper_account(conn)
    try:
        apply_signal(
            conn,
            analysis={
                "analysis_id": "a1",
                "symbol": "BTCUSDT",
                "action": "long",
                "size_pct_equity": 1.5,
                "regime_state": "trend_up",
                "confidence_score": 0.7,
            },
            mid_price=100.0,
            timeframe="4h",
            now_ms=1_000,
        )
        raise AssertionError("expected missing_invalidation")
    except ValueError as exc:
        assert "missing_invalidation" in str(exc)
    assert get_paper_position(conn, "BTCUSDT") is None


def test_apply_signal_stores_stop_price(tmp_path: Path) -> None:
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
    pos = get_paper_position(conn, "BTCUSDT")
    assert pos is not None
    assert pos["stop_price"] == 97.0
    assert pos["stop_source"] == "analysis"


def test_stop_monitor_flats_even_when_kill_switch_on(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JARVISE_MARKET_SAFETY", "0")
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
    upsert_market_technicals(
        conn,
        [
            {
                "symbol": "BTCUSDT",
                "timestamp": 1_700_000_100_000,
                "timeframe": "4h",
                "open": 96.0,
                "high": 96.5,
                "low": 95.0,
                "close": 96.0,
                "volume": 1.0,
            }
        ],
    )
    out = run_stop_monitor(conn, now_ms=1_700_000_100_000)
    assert out["closed"] == 1
    assert get_paper_position(conn, "BTCUSDT") is None
    reasons = [o.get("reason") for o in list_paper_orders(conn)]
    assert "stop_hit" in reasons


def test_backfill_stops_dry_run_then_apply(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "p.db")
    ensure_paper_account(conn)
    apply_signal(
        conn,
        analysis={
            "analysis_id": "a1",
            "symbol": "ETHUSDT",
            "action": "long",
            "size_pct_equity": 1.5,
            "invalidation_price": 48.0,
            "regime_state": "trend_up",
            "confidence_score": 0.7,
        },
        mid_price=50.0,
        timeframe="4h",
        now_ms=1_000,
    )
    upsert_market_technicals(
        conn,
        [
            {
                "symbol": "ETHUSDT",
                "timestamp": 1_700_000_000_000,
                "timeframe": "4h",
                "open": 50.0,
                "high": 51.0,
                "low": 49.0,
                "close": 50.0,
                "volume": 1.0,
            }
        ],
    )
    write_indicators(
        conn,
        [
            {
                "symbol": "ETHUSDT",
                "timeframe": "4h",
                "timestamp": 1_700_000_000_000,
                "atr_14": 1.0,
            }
        ],
    )
    upsert_analysis_output(
        conn,
        {
            "analysis_id": "a1",
            "timestamp": 1_000,
            "symbol": "ETHUSDT",
            "timeframe": "4h",
            "regime_state": "trend_up",
            "confidence_score": 0.7,
            "action": "long",
            "invalidation_price": 48.0,
            "size_pct_equity": 1.5,
            "thesis": "seed",
        },
    )
    conn.execute("UPDATE paper_positions SET stop_price = NULL, stop_source = NULL")
    conn.commit()
    dry = backfill_stops(conn, dry_run=True)
    assert dry["would_update"] == 1
    assert get_paper_position(conn, "ETHUSDT")["stop_price"] is None
    applied = backfill_stops(conn, dry_run=False)
    assert applied["updated"] == 1
    pos = get_paper_position(conn, "ETHUSDT")
    assert pos["stop_price"] is not None
    assert pos["stop_source"] == "backfill"
