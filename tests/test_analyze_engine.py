"""Paper analyzer: regime + confidence + invalidation + size. No orders."""

from jarvise_analyze.engine import analyze_snapshot


def _snap(**overrides):
    base = {
        "symbol": "BTCUSDT",
        "timeframe": "4h",
        "timestamp": 1_700_000_000_000,
        "close": 100.0,
        "atr_14": 2.0,
        "rsi_14": 55.0,
        "ema_20": 101.0,
        "ema_200": 95.0,
    }
    base.update(overrides)
    return base


def test_bullish_ema_stack_is_trend_up_long():
    out = analyze_snapshot(_snap())
    assert out["regime_state"] == "trend_up"
    assert out["action"] == "long"
    assert out["invalidation_price"] == 97.0  # close - 1.5 * atr
    assert 0.0 < out["confidence_score"] <= 1.0
    assert out["size_pct_equity"] > 0


def test_bearish_ema_stack_is_trend_down_short():
    out = analyze_snapshot(
        _snap(close=90.0, ema_20=89.0, ema_200=100.0, rsi_14=40.0)
    )
    assert out["regime_state"] == "trend_down"
    assert out["action"] == "short"
    assert out["invalidation_price"] == 93.0  # 90 + 1.5 * 2


def test_flat_emas_are_range_and_action_flat():
    out = analyze_snapshot(
        _snap(close=100.0, ema_20=100.1, ema_200=99.9, atr_14=1.0, rsi_14=50.0)
    )
    assert out["regime_state"] == "range"
    assert out["action"] == "flat"
    assert out["size_pct_equity"] == 0.0


def test_high_atr_with_conflict_is_chaotic_flat():
    out = analyze_snapshot(
        _snap(
            close=100.0,
            ema_20=105.0,
            ema_200=95.0,
            atr_14=8.0,  # 8% ATR
            rsi_14=25.0,  # conflicts with bullish stack
        )
    )
    assert out["regime_state"] == "chaotic"
    assert out["action"] == "flat"


def test_missing_indicators_force_flat_low_confidence():
    out = analyze_snapshot(
        _snap(atr_14=None, ema_20=None, ema_200=None, rsi_14=None)
    )
    assert out["action"] == "flat"
    assert out["confidence_score"] < 0.3
    assert out["size_pct_equity"] == 0.0


def test_confidence_below_threshold_forces_flat_even_in_trend():
    # RSI deeply against the trend → confidence collapses under threshold
    out = analyze_snapshot(
        _snap(close=100.0, ema_20=101.0, ema_200=95.0, rsi_14=15.0, atr_14=2.0),
        confidence_threshold=0.55,
    )
    assert out["regime_state"] == "trend_up"
    assert out["action"] == "flat"
    assert out["size_pct_equity"] == 0.0


def test_analysis_id_is_stable_for_same_inputs():
    a = analyze_snapshot(_snap())
    b = analyze_snapshot(_snap())
    assert a["analysis_id"] == b["analysis_id"]
    assert len(a["analysis_id"]) == 12
