"""Pydantic contracts reject NaN, inf, unknown actions, and missing stops."""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from jarvise_paper.schemas import LlmDecision, OrderIntent, parse_signal


def test_parse_signal_rejects_nan() -> None:
    with pytest.raises(ValidationError):
        parse_signal(
            {"symbol": "BTCUSDT", "action": "long", "confidence_score": math.nan, "size_pct_equity": 1}
        )


def test_parse_signal_rejects_inf() -> None:
    with pytest.raises(ValidationError):
        parse_signal(
            {"symbol": "BTCUSDT", "action": "long", "confidence_score": math.inf, "size_pct_equity": 1}
        )


def test_parse_signal_rejects_unknown_action() -> None:
    with pytest.raises(ValidationError):
        parse_signal({"symbol": "BTCUSDT", "action": "hold", "size_pct_equity": 1})


def test_parse_signal_requires_stop_for_open() -> None:
    sig = parse_signal({"symbol": "BTCUSDT", "action": "long", "size_pct_equity": 1.0})
    assert sig.invalidation_price is None


def test_parse_signal_accepts_long_with_stop() -> None:
    sig = parse_signal(
        {
            "symbol": "btcusdt",
            "action": "long",
            "confidence_score": 0.7,
            "size_pct_equity": 1.5,
            "invalidation_price": 97.0,
        }
    )
    assert sig.symbol == "BTCUSDT"
    assert sig.invalidation_price == 97.0


def test_order_intent_rejects_negative() -> None:
    with pytest.raises(ValidationError):
        OrderIntent(symbol="BTCUSDT", side="buy", qty=-1, price=1)


def test_llm_decision_forbids_extra() -> None:
    with pytest.raises(ValidationError):
        LlmDecision.model_validate({"decision": "approve", "reason": "ok", "extra": 1})
