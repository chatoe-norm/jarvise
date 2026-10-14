"""Parity: confidence_breakdown total == analyze_snapshot confidence_score."""

from __future__ import annotations

from jarvise_analyze.engine import analyze_snapshot
from jarvise_analyze.explain import DOCTRINE_FREE_APPROVE, confidence_breakdown


def _candle(**overrides: object) -> dict:
    base = {
        "symbol": "ETHUSDT",
        "timeframe": "4h",
        "timestamp": 1_700_000_000_000,
        "close": 2687.0,
        "atr_14": 40.0,
        "rsi_14": 49.0,
        "ema_20": 2700.0,
        "ema_200": 2560.0,
    }
    base.update(overrides)
    return base


def test_eth_pullback_ladder_matches_engine() -> None:
    """Classic ETH case: trend_up + RSI mid − close < EMA20 → 0.65."""
    c = _candle()
    snap = analyze_snapshot(c)
    bd = confidence_breakdown(c)
    assert snap["confidence_score"] == 0.65
    assert bd["total"] == snap["confidence_score"]
    assert bd["regime"] == "trend_up"
    assert bd["side"] == "long"
    assert bd["gates"]["flat_below"] == 0.55
    assert bd["gates"]["doctrine_free_approve"] == DOCTRINE_FREE_APPROVE
    labels = " ".join(s["label"] for s in bd["steps"])
    assert "EMA20" in labels
    assert any(s["delta"] == 0.15 for s in bd["steps"])
    assert any(s["delta"] == -0.1 for s in bd["steps"])


def test_btc_above_ema20_no_penalty() -> None:
    c = _candle(close=85000.0, ema_20=84000.0, ema_200=80000.0, rsi_14=53.0, atr_14=1000.0)
    snap = analyze_snapshot(c)
    bd = confidence_breakdown(c)
    assert bd["total"] == snap["confidence_score"]
    assert snap["confidence_score"] == 0.75


def test_parity_grid() -> None:
    cases = [
        _candle(),  # ETH pullback 0.65
        _candle(close=2800.0, ema_20=2700.0),  # above EMA20 → 0.75
        _candle(rsi_14=25.0),  # oversold long penalty
        _candle(rsi_14=80.0),  # overbought long mild penalty
        _candle(ema_20=2565.0, ema_200=2560.0),  # range gap
        _candle(close=None),  # warm-up
        _candle(ema_200=None),  # insufficient
        _candle(
            close=100.0,
            atr_14=6.0,  # 6% ATR
            ema_20=110.0,
            ema_200=90.0,
            rsi_14=20.0,  # conflict with bullish stack
        ),  # chaotic
        _candle(
            close=100.0,
            ema_20=90.0,
            ema_200=110.0,
            rsi_14=40.0,
        ),  # trend_down short
        _candle(
            close=95.0,
            ema_20=90.0,
            ema_200=110.0,
            rsi_14=40.0,
        ),  # short with close > ema20 penalty
    ]
    for c in cases:
        snap = analyze_snapshot(c)
        bd = confidence_breakdown(c)
        assert bd["total"] == snap["confidence_score"], (c, snap, bd)
        assert bd["regime"] == snap["regime_state"]


def test_insufficient_flag() -> None:
    bd = confidence_breakdown(_candle(close=None))
    assert bd["insufficient"] is True
    assert bd["total"] == 0.1
