"""Pydantic contracts for paper signals, orders, and LLM decisions."""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _finite(value: float, name: str) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


class SignalIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    symbol: str
    action: Literal["long", "short", "flat"]
    confidence_score: float = 0.0
    size_pct_equity: float = 0.0
    invalidation_price: float | None = None
    analysis_id: str | None = None
    regime_state: str | None = None
    thesis: str | None = None
    timeframe: str | None = None
    timestamp: int | None = None

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, value: str) -> str:
        return str(value).upper()

    @field_validator("confidence_score")
    @classmethod
    def _conf(cls, value: float) -> float:
        n = _finite(float(value), "confidence_score")
        if n < 0 or n > 1:
            raise ValueError("confidence_score must be in [0, 1]")
        return n

    @field_validator("size_pct_equity")
    @classmethod
    def _size(cls, value: float) -> float:
        n = _finite(float(value), "size_pct_equity")
        if n < 0 or n > 100:
            raise ValueError("size_pct_equity out of range")
        return n

    @field_validator("invalidation_price")
    @classmethod
    def _inv(cls, value: float | None) -> float | None:
        if value is None:
            return None
        n = _finite(float(value), "invalidation_price")
        if n <= 0:
            raise ValueError("invalidation_price must be > 0")
        return n


class OrderIntent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    symbol: str
    side: Literal["buy", "sell"]
    qty: float = Field(gt=0)
    price: float = Field(gt=0)

    @field_validator("qty", "price")
    @classmethod
    def _finite_pos(cls, value: float) -> float:
        return _finite(float(value), "qty/price")


class LlmDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject", "defer"]
    reason: str = Field(min_length=1, max_length=500)


def parse_signal(data: dict[str, Any]) -> SignalIn:
    payload = dict(data)
    payload["action"] = str(payload.get("action") or "flat").lower()
    return SignalIn.model_validate(payload)
