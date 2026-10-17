"""Replay-only strategies. These tests do not run the daily research windows."""

import shutil
from pathlib import Path

from jarvise_analyze.cli import run
from jarvise_analyze.engine import BASE_SIZE_PCT, analyze_snapshot
from jarvise_analyze.replay import replay_range
from jarvise_analyze.strategies import DONCHIAN_CONFIDENCE, decide
from jarvise_ingest.db import (
    get_paper_position,
    list_paper_orders,
    open_db,
    upsert_market_technicals,
    write_indicators,
)
from jarvise_ingest.series import recompute_indicators
from jarvise_ingest.timeframes import INTERVAL_MS

DAY_MS = INTERVAL_MS["1d"]
START = 1_700_000_000_000


def _bar(index: int, *, high: float, low: float, close: float, atr: float = 2.0) -> dict:
    return {
        "symbol": "BTCUSDT",
        "timestamp": START + index * DAY_MS,
        "timeframe": "1d",
        "open": close,
        "high": high,
        "low": low,
        "close": close,
        "volume": 10.0,
        "atr_14": atr,
        "rsi_14": 55.0,
        "ema_20": close,
        "ema_200": close - 1.0,
    }


def _seed_trend(db: Path, *, n: int = 40) -> None:
    conn = open_db(db)
    rows = []
    for i in range(n):
        close = 100.0 + i * 0.5
        rows.append(
            {
                "symbol": "BTCUSDT",
                "timestamp": START + i * 14_400_000,
                "timeframe": "4h",
                "open": close - 0.2,
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "volume": 10.0,
            }
        )
    upsert_market_technicals(conn, rows)
    recompute_indicators(conn, "BTCUSDT", "4h")
    conn.close()


def _store(db: Path, bars: list[dict]) -> None:
    conn = open_db(db)
    upsert_market_technicals(conn, bars)
    write_indicators(
        conn,
        [
            {
                "symbol": bar["symbol"],
                "timeframe": bar["timeframe"],
                "timestamp": bar["timestamp"],
                "atr_14": bar["atr_14"],
                "rsi_14": bar["rsi_14"],
                "ema_20": bar["ema_20"],
                "ema_200": bar["ema_200"],
            }
            for bar in bars
        ],
    )
    conn.close()


def test_default_strategy_matches_snapshot_replay(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    _seed_trend(src)
    since_ms = START
    until_ms = START + 39 * 14_400_000
    shutil.copy(src, tmp_path / "left.db")
    shutil.copy(src, tmp_path / "right.db")
    left = open_db(tmp_path / "left.db")
    right = open_db(tmp_path / "right.db")
    kwargs = {
        "symbols": ["BTCUSDT"],
        "timeframe": "4h",
        "since_ms": since_ms,
        "until_ms": until_ms,
        "apply_paper": True,
        "paper_policy": "rules",
    }
    default = replay_range(left, **kwargs)
    explicit = replay_range(right, strategy="current", **kwargs)
    left.close()
    right.close()
    assert default["strategy"] == "current"
    assert default["bars"] == explicit["bars"]
    assert default["fills"] == explicit["fills"]
    assert default["action_counts"] == explicit["action_counts"]
    assert default["policy_counts"] == explicit["policy_counts"]


def test_trend_long_never_opens_short() -> None:
    candle = {
        "symbol": "BTCUSDT",
        "timestamp": START,
        "timeframe": "1d",
        "open": 95.0,
        "high": 96.0,
        "low": 94.0,
        "close": 95.0,
        "atr_14": 1.0,
        "rsi_14": 40.0,
        "ema_20": 90.0,
        "ema_200": 100.0,
    }
    snapshot = analyze_snapshot(candle)
    assert snapshot["action"] == "short"
    result = decide("trend_long", [candle], None)
    assert result["action"] == "flat"
    assert result["size_pct_equity"] == 0.0
    assert result["invalidation_price"] is None
    assert "long-only" in result["thesis"].lower()
    assert decide("trend_long", [candle], "short")["action"] != "short"


def test_donchian_channel_excludes_current_bar() -> None:
    history = [_bar(i, high=100.0, low=90.0, close=90.0) for i in range(20)]
    history.append(_bar(20, high=110.0, low=80.0, close=105.0))
    result = decide("donchian_long", history, None)
    assert result["action"] == "long"
    assert result["confidence_score"] == DONCHIAN_CONFIDENCE
    assert result["size_pct_equity"] == min(2.0, round(BASE_SIZE_PCT * DONCHIAN_CONFIDENCE, 4))
    assert result["invalidation_price"] == round(105.0 - 1.5 * 2.0, 8)
    assert "not a score" in result["thesis"]
    # Including this bar's own high (110) would reject close 105.
    assert history[-1]["close"] > max(bar["high"] for bar in history[:-1])
    assert not history[-1]["close"] > max(bar["high"] for bar in history)


def test_donchian_exits_on_prior_10_bar_low() -> None:
    history = [_bar(i, high=120.0, low=50.0, close=110.0) for i in range(10)]
    history.extend(_bar(i, high=120.0, low=100.0, close=110.0) for i in range(10, 20))
    history.append(_bar(20, high=120.0, low=70.0, close=99.0))
    assert decide("donchian_long", history, "long")["action"] == "flat"
    held = history[:-1] + [_bar(20, high=120.0, low=70.0, close=100.0)]
    assert decide("donchian_long", held, "long")["action"] == "long"


def test_donchian_does_not_see_bars_after_current() -> None:
    history = [_bar(i, high=100.0, low=90.0, close=90.0) for i in range(20)]
    history.append(_bar(20, high=106.0, low=100.0, close=105.0))
    future = _bar(21, high=10_000.0, low=1.0, close=1.0)
    assert decide("donchian_long", history, None)["action"] == "long"
    # The future bar is the current bar only when it is the last element passed in.
    shifted = [*history[1:], future]
    assert decide("donchian_long", shifted, None)["action"] == "flat"


def test_warmup_bars_are_not_filled(tmp_path: Path) -> None:
    bars = [_bar(i, high=100.0, low=90.0, close=90.0) for i in range(20)]
    bars.append(_bar(20, high=130.0, low=110.0, close=120.0))
    bars.extend(_bar(i, high=100.0, low=90.0, close=90.0) for i in range(21, 25))
    db = tmp_path / "warm.db"
    _store(db, bars)
    conn = open_db(db)
    report = replay_range(
        conn,
        symbols=["BTCUSDT"],
        timeframe="1d",
        since_ms=bars[21]["timestamp"],
        until_ms=bars[24]["timestamp"],
        apply_paper=True,
        paper_policy="rules",
        strategy="donchian_long",
    )
    assert report["bars"] == 4
    assert report["fills"] == 0
    assert report["action_counts"]["short"] == 0
    assert list_paper_orders(conn) == []
    assert get_paper_position(conn, "BTCUSDT") is None
    conn.close()


def test_strategy_flag_requires_replay_rules_and_no_mtf(capsys) -> None:
    assert run(["--symbol", "BTCUSDT", "--strategy", "trend_long"]) == 2
    assert "--strategy" in capsys.readouterr().err
    assert (
        run(
            [
                "--symbol",
                "BTCUSDT",
                "--replay",
                "--since",
                "2024-01-01",
                "--strategy",
                "donchian_long",
                "--mtf",
                "--db",
                "nope.db",
            ]
        )
        == 2
    )
    assert "--mtf" in capsys.readouterr().err
    assert (
        run(
            [
                "--symbol",
                "BTCUSDT",
                "--replay",
                "--since",
                "2024-01-01",
                "--apply-paper",
                "--strategy",
                "trend_long",
                "--db",
                "nope.db",
            ]
        )
        == 2
    )
    assert "--paper-policy" in capsys.readouterr().err
