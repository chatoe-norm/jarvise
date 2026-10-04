# tests/test_recommendation.py
from jarvise_paper.recommendation import (
    build_recommendation,
    confidence_label,
    doctrine_query,
    template_recommendation,
)

APPROVAL = {
    "id": "261b5544d1bfe916",
    "symbol": "BTCUSDT",
    "timeframe": "4h",
    "action": "long",
    "regime_state": "trend_up",
    "confidence_score": 0.75,
    "size_pct_equity": 1.125,
    "expires_at_ms": 9_999_999_999_999,
    "status": "pending",
}
CANDLE = {
    "close": 85000.0,
    "ema_20": 84000.0,
    "ema_200": 80000.0,
    "rsi_14": 65.0,
    "atr_14": 1000.0,
}
ANALYSIS = {"invalidation_price": 83500.0, "thesis": "trend_up"}
ACCOUNT = {"equity": 10000.0, "cash": 10000.0, "starting_equity": 10000.0}
SAFETY_OK = {"ok": True, "force_flat": False, "critical": False, "reasons": []}


def test_template_thresholds() -> None:
    assert template_recommendation("long", 0.75) == "approve"
    assert template_recommendation("long", 0.60) == "approve_with_caution"
    assert template_recommendation("long", 0.40) == "reject"
    assert template_recommendation("flat", 0.90) == "reject"
    assert template_recommendation(None, None) == "reject"


def test_confidence_labels() -> None:
    assert confidence_label(0.75) == "ปานกลาง-สูง"
    assert confidence_label(0.60) == "ปานกลาง"
    assert confidence_label(0.40) == "ต่ำ"
    assert confidence_label(None) == "ต่ำ"


def test_doctrine_query_uses_regime_and_action() -> None:
    assert doctrine_query(APPROVAL) == (
        "jarvise doctrine trend_up long entry pullback EMA20 risk stop capital preservation kill-switch FLAT"
    )
    assert doctrine_query({}) == (
        "jarvise doctrine range flat entry pullback EMA20 risk stop capital preservation kill-switch FLAT"
    )


def test_card_approve_with_risk_math() -> None:
    card = build_recommendation(
        approval=APPROVAL,
        candle=CANDLE,
        analysis=ANALYSIS,
        account=ACCOUNT,
        position=None,
        safety=SAFETY_OK,
        doctrine=["structure first, oscillators as context"],
        claude=None,
    )
    assert card["ok"] is True
    assert card["approval_id"] == "261b5544d1bfe916"
    assert card["recommendation"] == "approve"
    assert card["recommendation_source"] == "template"
    assert card["confidence_label"] == "ปานกลาง-สูง"
    assert card["headline"] == "แนะนำ: APPROVE (ความมั่นใจ 0.75)"
    assert card["risk"]["notional_usd"] == 112.5
    assert card["risk"]["invalidation_price"] == 83500.0
    # 112.5 * |85000-83500| / 85000 = 1.985...
    assert round(card["risk"]["est_loss_usd"], 2) == 1.99
    assert card["risk"]["stop_atr_multiple"] == 1.5
    assert any("ขาขึ้น" in line for line in card["what_happened"])
    assert any("RSI 65" in line for line in card["what_happened"])
    assert any("ไม่พบสัญญาณอันตราย" in line for line in card["what_happened"])
    assert card["doctrine"] == ["structure first, oscillators as context"]
    assert any("ยังไม่ถือตำแหน่งเดียวกันซ้ำ" in line for line in card["checklist"])
    assert card["claude"] is None
    assert card["confidence_breakdown"]["total"] == 0.75
    assert card["gates"] == {"flat": 0.55, "approve": 0.70}
    assert not any("auto-decide จะ defer" in line for line in card["what_happened"])


def test_card_omits_est_loss_without_invalidation() -> None:
    card = build_recommendation(
        approval=APPROVAL,
        candle=CANDLE,
        analysis=None,
        account=ACCOUNT,
        position=None,
        safety=SAFETY_OK,
        doctrine=[],
        claude=None,
    )
    assert "est_loss_usd" not in card["risk"]
    assert card["risk"]["invalidation_price"] is None
    assert card["doctrine"] == []
    # conf 0.75 >= 0.70 → no doctrine-gate defer note even when doctrine empty
    assert not any("auto-decide จะ defer" in line for line in card["what_happened"])


def test_card_gate_note_when_conf_below_approve_and_no_doctrine() -> None:
    mid = {**APPROVAL, "confidence_score": 0.65, "size_pct_equity": 0.975}
    pullback = {
        **CANDLE,
        "close": 2687.0,
        "ema_20": 2700.0,
        "ema_200": 2560.0,
        "rsi_14": 49.0,
        "atr_14": 40.0,
    }
    card = build_recommendation(
        approval=mid,
        candle=pullback,
        analysis=ANALYSIS,
        account=ACCOUNT,
        position=None,
        safety=SAFETY_OK,
        doctrine=[],
        claude=None,
        signal_candle=pullback,
    )
    assert card["recommendation"] == "approve_with_caution"
    assert card["confidence_breakdown"]["total"] == 0.65
    assert any("auto-decide จะ defer" in line for line in card["what_happened"])
    # With doctrine present, note should not appear
    with_doc = build_recommendation(
        approval=mid,
        candle=pullback,
        analysis=ANALYSIS,
        account=ACCOUNT,
        position=None,
        safety=SAFETY_OK,
        doctrine=["structure first"],
        claude=None,
        signal_candle=pullback,
    )
    assert not any("auto-decide จะ defer" in line for line in with_doc["what_happened"])


def test_card_flat_explains_reject_and_missing_ema200() -> None:
    flat = {**APPROVAL, "action": "flat", "confidence_score": 0.1, "size_pct_equity": 0.0}
    card = build_recommendation(
        approval=flat,
        candle={**CANDLE, "ema_200": None},
        analysis=None,
        account=ACCOUNT,
        position=None,
        safety=SAFETY_OK,
        doctrine=[],
        claude=None,
    )
    assert card["recommendation"] == "reject"
    assert "flat" in card["headline"]
    assert any("EMA200" in line for line in card["what_happened"])


def test_card_claude_source_and_open_position_and_safety_reasons() -> None:
    card = build_recommendation(
        approval=APPROVAL,
        candle=CANDLE,
        analysis=ANALYSIS,
        account=ACCOUNT,
        position={"symbol": "BTCUSDT", "side": "long", "qty": 0.001},
        safety={"ok": False, "force_flat": False, "critical": False, "reasons": ["book_stale_ms=1"]},
        doctrine=[],
        claude={
            "decision": "defer",
            "reason": "มีตำแหน่งเดิมอยู่",
            "model": "anthropic/claude-sonnet-4.5",
            "at_ms": 5,
        },
    )
    assert card["recommendation_source"] == "claude"
    assert card["claude"]["decision"] == "defer"
    assert any("ถือ BTCUSDT อยู่แล้ว" in line for line in card["checklist"])
    assert any("book_stale_ms=1" in line for line in card["what_happened"])
