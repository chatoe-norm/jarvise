from jarvise_ingest.providers.alternative_fng import parse_fear_greed
from jarvise_paper.engine import estimate_impact_bps
from jarvise_paper.schemas import LlmDecision, parse_signal
from pydantic import ValidationError


def test_parse_fear_greed() -> None:
    assert parse_fear_greed({"data": [{"value": "42"}]}) == 42
    assert parse_fear_greed({"data": []}) is None
    assert parse_fear_greed({"data": [{"value": "nope"}]}) is None


def test_estimate_impact_bps() -> None:
    assert estimate_impact_bps(250.0, 25_000.0) == 1.0
    assert estimate_impact_bps(100.0, 0.0) == 0.0


def test_signal_schema_rejects_nan_and_unknown_action() -> None:
    try:
        parse_signal({"symbol": "BTCUSDT", "action": "long", "size_pct_equity": 1.0, "invalidation_price": float("nan")})
        raise AssertionError("expected error")
    except ValidationError:
        pass
    try:
        parse_signal({"symbol": "BTCUSDT", "action": "yolo", "size_pct_equity": 0.0})
        raise AssertionError("expected error")
    except ValidationError:
        pass


def test_llm_decision_strict() -> None:
    LlmDecision.model_validate({"decision": "defer", "reason": "wait"})
    try:
        LlmDecision.model_validate({"decision": "approve", "reason": "x", "extra": 1})
        raise AssertionError("expected extra forbid")
    except ValidationError:
        pass
