"""1R profit lock: tighten only, and replay stops close on the bar extreme."""

from __future__ import annotations

from pathlib import Path

from jarvise_analyze.replay import apply_replay_stop
from jarvise_ingest.db import (
    ensure_paper_account,
    get_paper_position,
    open_db,
    upsert_market_technicals,
    upsert_paper_position,
)
from jarvise_paper.profit_lock import run_profit_lock, trail_stop
from jarvise_paper.stops import run_stop_monitor


def _short(**over: object) -> dict:
    pos = {
        "symbol": "ETHUSDT",
        "side": "short",
        "entry_price": 2507.49,
        "stop_price": 2562.46,
        "initial_stop_price": 2562.46,
    }
    pos.update(over)
    return pos


def test_trail_stop_stays_put_below_breakeven() -> None:
    # 2507.49 - 2490.0 = 17.49 < 0.5 * 54.97 (27.485) -> stays put
    assert trail_stop(_short(), best_low=2490.0, best_high=2530.0) is None


def test_trail_stop_locks_breakeven_at_half_r() -> None:
    # 2507.49 - 2470.0 = 37.49 >= 0.5 * 54.97 (27.485) and < 1R -> breakeven (+20 bps fee buffer)
    entry = 2507.49
    res = trail_stop(_short(), best_low=2470.0, best_high=2530.0)
    expected_be = round(entry * (1.0 - 0.002), 8)
    assert res == expected_be


def test_trail_stop_stays_put_below_one_r_when_breakeven_disabled() -> None:
    assert trail_stop(_short(), best_low=2460.0, best_high=2530.0, breakeven_r=None) is None


def test_trail_stop_reaches_entry_at_one_r() -> None:
    entry = 2507.49
    risk = 2562.46 - entry
    new_stop = trail_stop(_short(), best_low=entry - risk, best_high=2530.0)
    assert new_stop == entry


def test_trail_stop_follows_and_does_not_loosen() -> None:
    armed = _short(stop_price=2458.66)
    tighter = trail_stop(armed, best_low=2400.0, best_high=2500.0)
    assert tighter is not None and tighter < 2458.66
    assert trail_stop(armed, best_low=2450.0, best_high=2500.0) is None


def test_trail_stop_long_mirrors_short() -> None:
    pos = {
        "side": "long",
        "entry_price": 100.0,
        "stop_price": 90.0,
        "initial_stop_price": 90.0,
    }
    # risk = 10.0, 0.5R = 5.0
    assert trail_stop(pos, best_low=95.0, best_high=104.0) is None
    # 106.0 reaches >= 0.5R (5.0) -> breakeven 100.0 * 1.002 = 100.2
    assert trail_stop(pos, best_low=95.0, best_high=106.0) == 100.2
    # 110.0 reaches 1.0R -> 110.0 - 10.0 = 100.0
    assert trail_stop(pos, best_low=95.0, best_high=110.0, breakeven_r=None) == 100.0
    locked = {**pos, "stop_price": 105.0}
    assert trail_stop(locked, best_low=95.0, best_high=116.0) == 106.0
    assert trail_stop(locked, best_low=95.0, best_high=114.0) is None


def test_profit_lock_then_old_high_does_not_stop(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JARVISE_MARKET_SAFETY", "0")
    conn = open_db(tmp_path / "lock.db")
    ensure_paper_account(conn)
    upsert_market_technicals(
        conn,
        [
            {
                "symbol": "ETHUSDT",
                "timestamp": 1_000,
                "timeframe": "4h",
                "open": 2507.0,
                "high": 2537.69,
                "low": 2403.69,
                "close": 2421.6,
                "volume": 1.0,
            }
        ],
    )
    upsert_paper_position(
        conn,
        {
            "symbol": "ETHUSDT",
            "side": "short",
            "qty": 0.044754263154700505,
            "entry_price": 2507.49,
            "entry_ts": 1_000,
            "unrealized_pnl": 0.0,
            "realized_pnl": 0.0,
            "stop_price": 2562.46,
            "stop_source": "analysis",
            "initial_stop_price": 2562.46,
            "stop_moved_ts": None,
        },
    )
    conn.commit()
    moved = run_profit_lock(conn, now_ms=5_000)
    assert moved["moved"][0]["symbol"] == "ETHUSDT"
    pos = get_paper_position(conn, "ETHUSDT")
    assert pos is not None
    assert abs(pos["stop_price"] - (2403.69 + (2562.46 - 2507.49))) < 1e-6
    assert pos["stop_source"] == "trail"
    assert pos["stop_moved_ts"] == 5_000
    report = run_stop_monitor(conn, now_ms=5_001)
    assert report["closed"] == 0
    assert get_paper_position(conn, "ETHUSDT") is not None


def test_replay_trail_closes_when_later_bar_hits(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JARVISE_MARKET_SAFETY", "0")
    conn = open_db(tmp_path / "replay-stop.db")
    ensure_paper_account(conn)
    upsert_paper_position(
        conn,
        {
            "symbol": "SOLUSDT",
            "side": "short",
            "qty": 1.0,
            "entry_price": 100.0,
            "entry_ts": 1_000,
            "unrealized_pnl": 0.0,
            "realized_pnl": 0.0,
            "stop_price": 110.0,
            "stop_source": "analysis",
            "initial_stop_price": 110.0,
            "stop_moved_ts": None,
        },
    )
    upsert_market_technicals(
        conn,
        [
            {
                "symbol": "SOLUSDT",
                "timestamp": 2_000,
                "timeframe": "4h",
                "open": 100.0,
                "high": 95.0,
                "low": 89.0,
                "close": 90.0,
                "volume": 1.0,
            },
            {
                "symbol": "SOLUSDT",
                "timestamp": 3_000,
                "timeframe": "4h",
                "open": 90.0,
                "high": 100.0,
                "low": 89.5,
                "close": 99.0,
                "volume": 1.0,
            },
        ],
    )
    conn.commit()
    assert (
        apply_replay_stop(
            conn,
            {"symbol": "SOLUSDT", "timestamp": 2_000, "timeframe": "4h", "close": 90.0},
            mode="trail",
        )
        == 0
    )
    locked = get_paper_position(conn, "SOLUSDT")
    assert locked is not None and locked["stop_price"] == 99.0
    fills = apply_replay_stop(
        conn,
        {"symbol": "SOLUSDT", "timestamp": 3_000, "timeframe": "4h", "close": 99.0},
        mode="trail",
    )
    assert fills == 1
    assert get_paper_position(conn, "SOLUSDT") is None


def _replay_long(conn, *, entry_ts: int) -> None:
    upsert_paper_position(
        conn,
        {
            "symbol": "SOLUSDT",
            "side": "long",
            "qty": 1.0,
            "entry_price": 100.0,
            "entry_ts": entry_ts,
            "unrealized_pnl": 0.0,
            "realized_pnl": 0.0,
            "stop_price": 90.0,
            "stop_source": "analysis",
            "initial_stop_price": 90.0,
            "stop_moved_ts": None,
        },
    )


def _bar(ts: int, tf: str, *, high: float, low: float, close: float) -> dict:
    return {
        "symbol": "SOLUSDT",
        "timestamp": ts,
        "timeframe": tf,
        "open": close,
        "high": high,
        "low": low,
        "close": close,
        "volume": 1.0,
    }


def test_replay_stop_ignores_entry_bar_extremes(tmp_path: Path, monkeypatch) -> None:
    """The fill is at the entry bar's close; its earlier low and high are not after entry."""
    monkeypatch.setenv("JARVISE_MARKET_SAFETY", "0")
    conn = open_db(tmp_path / "entry-bar.db")
    ensure_paper_account(conn)
    _replay_long(conn, entry_ts=1_000)
    upsert_market_technicals(conn, [_bar(1_000, "4h", high=115.0, low=85.0, close=100.0)])
    conn.commit()
    for mode in ("fixed", "trail"):
        bar = {"symbol": "SOLUSDT", "timestamp": 1_000, "timeframe": "4h", "close": 100.0}
        assert apply_replay_stop(conn, bar, mode=mode) == 0
    pos = get_paper_position(conn, "SOLUSDT")
    assert pos is not None and pos["stop_price"] == 90.0


def test_replay_stop_ignores_other_timeframes(tmp_path: Path, monkeypatch) -> None:
    """A 1d bar opening after entry spans past the 4h bar being replayed."""
    monkeypatch.setenv("JARVISE_MARKET_SAFETY", "0")
    conn = open_db(tmp_path / "other-tf.db")
    ensure_paper_account(conn)
    _replay_long(conn, entry_ts=1_000)
    upsert_market_technicals(
        conn,
        [
            _bar(2_000, "4h", high=103.0, low=97.0, close=101.0),
            _bar(1_500, "1d", high=130.0, low=60.0, close=101.0),
            _bar(1_800, "1h", high=130.0, low=60.0, close=101.0),
        ],
    )
    conn.commit()
    bar = {"symbol": "SOLUSDT", "timestamp": 2_000, "timeframe": "4h", "close": 101.0}
    assert apply_replay_stop(conn, bar, mode="trail") == 0
    pos = get_paper_position(conn, "SOLUSDT")
    assert pos is not None and pos["stop_price"] == 90.0


def test_replay_old_stop_checked_before_trail_moves(tmp_path: Path, monkeypatch) -> None:
    """One bar reaching 1R and dipping back cannot both raise the stop and hit it."""
    monkeypatch.setenv("JARVISE_MARKET_SAFETY", "0")
    conn = open_db(tmp_path / "order.db")
    ensure_paper_account(conn)
    _replay_long(conn, entry_ts=1_000)
    upsert_market_technicals(conn, [_bar(2_000, "4h", high=112.0, low=99.0, close=105.0)])
    conn.commit()
    bar = {"symbol": "SOLUSDT", "timestamp": 2_000, "timeframe": "4h", "close": 105.0}
    assert apply_replay_stop(conn, bar, mode="trail") == 0
    pos = get_paper_position(conn, "SOLUSDT")
    assert pos is not None and pos["stop_price"] == 102.0 and pos["stop_source"] == "trail"
