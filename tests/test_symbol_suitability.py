"""Tests for symbol suitability evaluation and ranking."""

from jarvise_analyze.suitability import evaluate_symbol_suitability


def test_evaluate_symbol_suitability_high_trend_short():
    res = evaluate_symbol_suitability(
        "SOLUSDT",
        analysis={
            "action": "short",
            "regime_state": "trend_down",
            "confidence_score": 0.75,
            "thesis": "Trend down: EMA20<110.7",
        },
        candle={
            "close": 110.0,
            "ema_20": 110.7,
            "ema_200": 128.0,
            "rsi_14": 31.5,
            "atr_14": 4.2,
        },
        health={
            "rows": 12685,
            "ema200_ready": 12025,
            "newest_age_min": 10.0,
            "gaps": 0,
        },
    )
    assert res["symbol"] == "SOLUSDT"
    assert res["score"] >= 80.0
    assert res["grade"] == "high"
    assert "สัญญาณ SELL/SHORT ชัดเจน" in res["reasons"]
    assert res["action"] == "short"


def test_evaluate_symbol_suitability_flat_range():
    res = evaluate_symbol_suitability(
        "BTCUSDT",
        analysis={
            "action": "flat",
            "regime_state": "range",
            "confidence_score": 0.40,
            "thesis": "Range: EMA gap small",
        },
        candle={
            "close": 68000.0,
            "ema_20": 67900.0,
            "ema_200": 65000.0,
            "rsi_14": 52.0,
            "atr_14": 1200.0,
        },
        health={
            "rows": 12685,
            "ema200_ready": 12025,
            "newest_age_min": 5.0,
            "gaps": 0,
        },
    )
    assert res["symbol"] == "BTCUSDT"
    assert res["score"] < 60.0
    assert res["grade"] in ("moderate", "low")
    assert res["action"] == "flat"


def test_evaluate_symbol_suitability_handles_none():
    res = evaluate_symbol_suitability("XRPUSDT")
    assert res["symbol"] == "XRPUSDT"
    assert res["score"] >= 0.0
    assert res["grade"] == "low"
    assert res["action"] == "flat"
