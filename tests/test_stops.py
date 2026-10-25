"""Stop evaluation and monitor (doctrine: FLAT when invalidation is hit)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jarvise_analyze.replay import apply_replay_stop
from jarvise_ingest.db import (
    ensure_paper_account,
    get_paper_position,
    list_paper_orders,
    open_db,
    upsert_analysis_output,
    upsert_market_technicals,
    upsert_paper_position,
    write_indicators,
)
from jarvise_paper.engine import apply_signal
from jarvise_paper.stops import backfill_stops, evaluate_stop, resting_stop_fill, run_stop_monitor


def _long_pos(**extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "symbol": "BTCUSDT",
        "side": "long",
        "qty": 1.0,
        "entry_price": 100.0,
        "entry_ts": 1_000,
        "unrealized_pnl": 0.0,
        "realized_pnl": 0.0,
        "stop_price": 97.0,
        "stop_source": "analysis",
        "initial_stop_price": 97.0,
        "stop_moved_ts": None,
    }
    row.update(extra)
    return row


def _short_pos(**extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "symbol": "ETHUSDT",
        "side": "short",
        "qty": 1.0,
        "entry_price": 100.0,
        "entry_ts": 1_000,
        "unrealized_pnl": 0.0,
        "realized_pnl": 0.0,
        "stop_price": 103.0,
        "stop_source": "analysis",
        "initial_stop_price": 103.0,
        "stop_moved_ts": None,
    }
    row.update(extra)
    return row


def _ohlc(
    symbol: str,
    ts: int,
    *,
    open_: float,
    high: float,
    low: float,
    close: float,
    timeframe: str = "4h",
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "timestamp": ts,
        "timeframe": timeframe,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": 1.0,
    }


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
    open_orders = [o for o in list_paper_orders(conn) if str(o.get("reason") or "").startswith("open_")]
    assert len(open_orders) == 1
    assert open_orders[0]["initial_stop_price"] == pos["stop_price"] == 97.0

    apply_signal(
        conn,
        analysis={
            "analysis_id": "a2",
            "symbol": "BTCUSDT",
            "action": "flat",
            "size_pct_equity": 0.0,
            "regime_state": "range",
            "confidence_score": 0.4,
        },
        mid_price=99.0,
        timeframe="4h",
        now_ms=2_000,
    )
    close_orders = [o for o in list_paper_orders(conn) if str(o.get("reason") or "").startswith("close_")]
    assert len(close_orders) == 1
    assert close_orders[0]["initial_stop_price"] is None


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


def test_stop_monitor_ignores_extremes_before_stop_move(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JARVISE_MARKET_SAFETY", "0")
    conn = open_db(tmp_path / "trail-hit.db")
    ensure_paper_account(conn)
    upsert_market_technicals(
        conn,
        [
            {
                "symbol": "ETHUSDT",
                "timestamp": 1_000,
                "timeframe": "4h",
                "open": 2500.0,
                "high": 2537.69,
                "low": 2480.0,
                "close": 2510.0,
                "volume": 1.0,
            },
            {
                "symbol": "ETHUSDT",
                "timestamp": 6_000,
                "timeframe": "4h",
                "open": 2420.0,
                "high": 2430.0,
                "low": 2410.0,
                "close": 2421.0,
                "volume": 1.0,
            },
        ],
    )
    upsert_paper_position(
        conn,
        {
            "symbol": "ETHUSDT",
            "side": "short",
            "qty": 0.04,
            "entry_price": 2507.49,
            "entry_ts": 1_000,
            "unrealized_pnl": 0.0,
            "realized_pnl": 0.0,
            "stop_price": 2458.66,
            "stop_source": "trail",
            "initial_stop_price": 2562.46,
            "stop_moved_ts": 5_000,
        },
    )
    conn.commit()
    report = run_stop_monitor(conn, now_ms=7_000)
    assert report["closed"] == 0
    assert get_paper_position(conn, "ETHUSDT") is not None


def test_resting_stop_fill_long_wick(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "wick.db")
    ensure_paper_account(conn)
    upsert_paper_position(conn, _long_pos())
    upsert_market_technicals(
        conn,
        [_ohlc("BTCUSDT", 2_000, open_=100.0, high=101.0, low=96.0, close=99.0)],
    )
    conn.commit()
    hit = resting_stop_fill(conn, _long_pos(), mid=99.0, until_ms=2_000, timeframe="4h")
    assert hit is not None
    assert hit.fill_mid == 97.0
    assert hit.trigger == "bar_low"


def test_resting_stop_fill_long_gap(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "gap.db")
    ensure_paper_account(conn)
    upsert_paper_position(conn, _long_pos())
    upsert_market_technicals(
        conn,
        [_ohlc("BTCUSDT", 2_000, open_=95.0, high=96.0, low=94.0, close=95.5)],
    )
    conn.commit()
    hit = resting_stop_fill(conn, _long_pos(), mid=95.5, until_ms=2_000, timeframe="4h")
    assert hit is not None
    assert hit.fill_mid == 95.0
    assert hit.trigger == "bar_low"


def test_resting_stop_fill_short_wick_and_gap(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "short.db")
    ensure_paper_account(conn)
    upsert_paper_position(conn, _short_pos())
    upsert_market_technicals(
        conn,
        [_ohlc("ETHUSDT", 2_000, open_=100.0, high=104.0, low=99.0, close=102.0)],
    )
    conn.commit()
    wick = resting_stop_fill(conn, _short_pos(), mid=102.0, until_ms=2_000, timeframe="4h")
    assert wick is not None
    assert wick.fill_mid == 103.0
    assert wick.trigger == "bar_high"

    conn.execute("DELETE FROM market_technicals")
    upsert_market_technicals(
        conn,
        [_ohlc("ETHUSDT", 2_000, open_=105.0, high=106.0, low=104.0, close=105.5)],
    )
    conn.commit()
    gap = resting_stop_fill(conn, _short_pos(), mid=105.5, until_ms=2_000, timeframe="4h")
    assert gap is not None
    assert gap.fill_mid == 105.0


def test_resting_stop_fill_first_touch_not_later_extreme(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "late.db")
    ensure_paper_account(conn)
    upsert_paper_position(conn, _long_pos())
    upsert_market_technicals(
        conn,
        [
            _ohlc("BTCUSDT", 2_000, open_=100.0, high=101.0, low=96.0, close=99.0),
            _ohlc("BTCUSDT", 3_000, open_=99.0, high=99.5, low=90.0, close=91.0),
        ],
    )
    conn.commit()
    hit = resting_stop_fill(conn, _long_pos(), mid=91.0, until_ms=3_000, timeframe="4h")
    assert hit is not None
    assert hit.fill_mid == 97.0


def test_resting_stop_paper_and_replay_same_fill(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JARVISE_MARKET_SAFETY", "0")
    bars = [
        _ohlc("BTCUSDT", 2_000, open_=100.0, high=101.0, low=96.0, close=99.0),
        _ohlc("BTCUSDT", 3_000, open_=99.0, high=99.5, low=90.0, close=91.0),
    ]

    probe = open_db(tmp_path / "probe.db")
    ensure_paper_account(probe)
    upsert_paper_position(probe, _long_pos())
    upsert_market_technicals(probe, bars)
    probe.commit()
    shared = resting_stop_fill(probe, _long_pos(), mid=91.0, until_ms=3_000, timeframe="4h")
    assert shared is not None
    assert shared.fill_mid == 97.0

    paper = open_db(tmp_path / "paper.db")
    ensure_paper_account(paper)
    upsert_paper_position(paper, _long_pos())
    upsert_market_technicals(paper, bars)
    paper.commit()
    monitor = run_stop_monitor(paper, now_ms=3_000)
    assert monitor["closed"] == 1
    assert monitor["hits"][0]["fill_mid"] == 97.0
    assert get_paper_position(paper, "BTCUSDT") is None

    replay = open_db(tmp_path / "replay.db")
    ensure_paper_account(replay)
    upsert_paper_position(replay, _long_pos())
    upsert_market_technicals(replay, bars)
    replay.commit()
    assert (
        apply_replay_stop(
            replay,
            {"symbol": "BTCUSDT", "timestamp": 2_000, "timeframe": "4h", "close": 99.0},
            mode="fixed",
        )
        == 1
    )
    assert get_paper_position(replay, "BTCUSDT") is None
    replay_shared = resting_stop_fill(
        probe,
        _long_pos(),
        mid=99.0,
        until_ms=2_000,
        timeframe="4h",
    )
    assert replay_shared is not None
    assert replay_shared.fill_mid == monitor["hits"][0]["fill_mid"] == 97.0


def test_resting_stop_mid_fallback_long_fills_at_close(tmp_path: Path) -> None:
    """Stop moved on this bar: no later bar; close below the new stop fills at close."""
    conn = open_db(tmp_path / "mid-long.db")
    ensure_paper_account(conn)
    pos = _long_pos(stop_price=102.0, stop_moved_ts=2_000, stop_source="trail")
    upsert_paper_position(conn, pos)
    upsert_market_technicals(
        conn,
        [_ohlc("BTCUSDT", 2_000, open_=100.0, high=105.0, low=99.0, close=101.0)],
    )
    conn.commit()
    hit = resting_stop_fill(conn, pos, mid=101.0, until_ms=2_000, timeframe="4h")
    assert hit is not None
    assert hit.fill_mid == 101.0
    assert hit.trigger == "mid"


def test_resting_stop_mid_fallback_short_fills_at_close(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "mid-short.db")
    ensure_paper_account(conn)
    pos = _short_pos(stop_price=98.0, stop_moved_ts=2_000, stop_source="trail")
    upsert_paper_position(conn, pos)
    upsert_market_technicals(
        conn,
        [_ohlc("ETHUSDT", 2_000, open_=100.0, high=101.0, low=95.0, close=99.0)],
    )
    conn.commit()
    hit = resting_stop_fill(conn, pos, mid=99.0, until_ms=2_000, timeframe="4h")
    assert hit is not None
    assert hit.fill_mid == 99.0
    assert hit.trigger == "mid"


def test_resting_stop_mid_fallback_at_stop_fills_at_stop(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "mid-eq.db")
    ensure_paper_account(conn)
    pos = _long_pos(stop_price=97.0, stop_moved_ts=2_000, stop_source="trail")
    upsert_paper_position(conn, pos)
    upsert_market_technicals(
        conn,
        [_ohlc("BTCUSDT", 2_000, open_=100.0, high=101.0, low=98.0, close=97.0)],
    )
    conn.commit()
    hit = resting_stop_fill(conn, pos, mid=97.0, until_ms=2_000, timeframe="4h")
    assert hit is not None
    assert hit.fill_mid == 97.0
    assert hit.trigger == "mid"
