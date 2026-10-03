"""Deterministic paper analyzer (no orders).

Uses stored indicators only. Doctrine: structure first, oscillators as context,
stops >= 1.5x ATR, FLAT below confidence threshold, never full Kelly.
Optional HTF confirm (T2.3) via ``analyze_mtf`` / ``JARVISE_ANALYZE_MTF``.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any

CONFIDENCE_THRESHOLD = 0.55
ATR_STOP_MULT = 1.5
RANGE_EMA_PCT = 0.015
CHAOTIC_ATR_PCT = 0.05
MAX_SIZE_PCT = 2.0
BASE_SIZE_PCT = 1.5
DEFAULT_HTF = "1d"
_TRUE = frozenset({"1", "true", "yes", "on"})


def mtf_enabled() -> bool:
    """HTF confirm on by default once wired; set JARVISE_ANALYZE_MTF=false to disable."""
    raw = (os.environ.get("JARVISE_ANALYZE_MTF") or "true").strip().lower()
    return raw in _TRUE


def htf_timeframe() -> str:
    return (os.environ.get("JARVISE_ANALYZE_HTF") or DEFAULT_HTF).strip() or DEFAULT_HTF


def analyze_snapshot(
    candle: dict[str, Any],
    *,
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
) -> dict[str, Any]:
    """Classify one closed candle into analysis_output fields."""
    symbol = str(candle["symbol"]).upper()
    timeframe = str(candle.get("timeframe") or "")
    timestamp = int(candle["timestamp"])
    close = _num(candle.get("close"))
    atr = _num(candle.get("atr_14"))
    rsi = _num(candle.get("rsi_14"))
    ema20 = _num(candle.get("ema_20"))
    ema200 = _num(candle.get("ema_200"))

    if close is None or atr is None or ema20 is None or ema200 is None or close <= 0:
        return _result(
            symbol,
            timeframe,
            timestamp,
            regime="chaotic",
            action="flat",
            confidence=0.1,
            invalidation=None,
            size=0.0,
            thesis="Insufficient indicators (warm-up or missing); stay flat.",
        )

    atr_pct = atr / close
    ema_gap_pct = abs(ema20 - ema200) / close
    bullish_stack = ema20 > ema200
    bearish_stack = ema20 < ema200

    if atr_pct >= CHAOTIC_ATR_PCT and _rsi_conflicts(bullish_stack, bearish_stack, rsi):
        regime = "chaotic"
        lean = "flat"
        confidence = 0.25
        thesis = f"Chaotic: ATR {atr_pct:.1%} with momentum conflict vs EMA stack; flat."
    elif ema_gap_pct < RANGE_EMA_PCT:
        regime = "range"
        lean = "flat"
        confidence = 0.4
        thesis = f"Range: EMA gap {ema_gap_pct:.2%} of price; no directional stack."
    elif bullish_stack:
        regime = "trend_up"
        lean = "long"
        confidence = _trend_confidence(rsi, side="long")
        if close < ema20:
            confidence = max(0.05, confidence - 0.1)
        thesis = f"Trend up: EMA20>{ema200:.4g}; RSI={rsi if rsi is not None else 'n/a'}."
    elif bearish_stack:
        regime = "trend_down"
        lean = "short"
        confidence = _trend_confidence(rsi, side="short")
        if close > ema20:
            confidence = max(0.05, confidence - 0.1)
        thesis = f"Trend down: EMA20<{ema200:.4g}; RSI={rsi if rsi is not None else 'n/a'}."
    else:
        regime = "range"
        lean = "flat"
        confidence = 0.35
        thesis = "No clear EMA stack alignment; treat as range / flat."

    action = lean
    size = 0.0
    invalidation = None
    if lean in ("long", "short") and confidence >= confidence_threshold:
        invalidation = close - ATR_STOP_MULT * atr if lean == "long" else close + ATR_STOP_MULT * atr
        size = min(MAX_SIZE_PCT, round(BASE_SIZE_PCT * confidence, 4))
    else:
        action = "flat"
        size = 0.0
        if lean in ("long", "short") and confidence < confidence_threshold:
            thesis += f" Confidence {confidence:.2f} < {confidence_threshold:.2f}; FLAT."

    return _result(
        symbol,
        timeframe,
        timestamp,
        regime=regime,
        action=action,
        confidence=round(confidence, 4),
        invalidation=round(invalidation, 8) if invalidation is not None else None,
        size=size,
        thesis=thesis,
    )


def analyze_mtf(
    ltf_candle: dict[str, Any],
    htf_candle: dict[str, Any] | None,
    *,
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
) -> dict[str, Any]:
    """LTF signal only when HTF regime agrees; else force flat (fail-closed)."""
    ltf = analyze_snapshot(ltf_candle, confidence_threshold=confidence_threshold)
    if htf_candle is None:
        return _force_flat(
            ltf,
            reason="htf_missing",
            thesis_suffix=" HTF candle missing; stay flat.",
        )
    htf = analyze_snapshot(htf_candle, confidence_threshold=confidence_threshold)
    htf_regime = str(htf.get("regime_state") or "")
    ltf["htf_regime"] = htf_regime
    ltf["htf_timeframe"] = str(htf_candle.get("timeframe") or "")
    if ltf["action"] == "flat":
        ltf["thesis"] = f"{ltf['thesis']} HTF={htf_regime}."
        return ltf
    if htf_regime == "chaotic":
        return _force_flat(
            ltf,
            reason="htf_conflict",
            thesis_suffix=f" HTF chaotic; flatten LTF {ltf['action']}.",
        )
    if htf_regime == "range":
        return _force_flat(
            ltf,
            reason="htf_range",
            thesis_suffix=" HTF range; no directional LTF.",
        )
    if htf_regime == "trend_up" and ltf["action"] != "long":
        return _force_flat(
            ltf,
            reason="htf_conflict",
            thesis_suffix=f" HTF trend_up conflicts with LTF {ltf['action']}.",
        )
    if htf_regime == "trend_down" and ltf["action"] != "short":
        return _force_flat(
            ltf,
            reason="htf_conflict",
            thesis_suffix=f" HTF trend_down conflicts with LTF {ltf['action']}.",
        )
    ltf["thesis"] = f"{ltf['thesis']} HTF {htf_regime} confirms."
    ltf["mtf_reason"] = "htf_confirm"
    return ltf


def _force_flat(
    base: dict[str, Any],
    *,
    reason: str,
    thesis_suffix: str,
) -> dict[str, Any]:
    out = dict(base)
    out["action"] = "flat"
    out["size_pct_equity"] = 0.0
    out["invalidation_price"] = None
    out["thesis"] = f"{base.get('thesis') or ''}{thesis_suffix}".strip()
    out["mtf_reason"] = reason
    # Re-hash analysis_id so MTF flats do not collide with the unfiltered LTF id.
    material = (
        f"{out['symbol']}|{out['timeframe']}|{out['timestamp']}|{out['regime_state']}|"
        f"flat|{out['confidence_score']:.4f}|None|0.0000|{reason}"
    )
    out["analysis_id"] = hashlib.sha256(material.encode()).hexdigest()[:12]
    return out


def _num(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _rsi_conflicts(bullish: bool, bearish: bool, rsi: float | None) -> bool:
    if rsi is None:
        return False
    if bullish and rsi < 30:
        return True
    if bearish and rsi > 70:
        return True
    return False


def _trend_confidence(rsi: float | None, *, side: str) -> float:
    conf = 0.6
    if rsi is None:
        return conf
    if side == "long":
        if 45 <= rsi <= 70:
            conf += 0.15
        elif rsi < 30:
            conf -= 0.35
        elif rsi > 75:
            conf -= 0.1
    else:
        if 30 <= rsi <= 55:
            conf += 0.15
        elif rsi > 70:
            conf -= 0.35
        elif rsi < 25:
            conf -= 0.1
    return max(0.05, min(0.95, conf))


def _result(
    symbol: str,
    timeframe: str,
    timestamp: int,
    *,
    regime: str,
    action: str,
    confidence: float,
    invalidation: float | None,
    size: float,
    thesis: str,
) -> dict[str, Any]:
    material = (
        f"{symbol}|{timeframe}|{timestamp}|{regime}|{action}|{confidence:.4f}|{invalidation}|{size:.4f}"
    )
    analysis_id = hashlib.sha256(material.encode()).hexdigest()[:12]
    return {
        "analysis_id": analysis_id,
        "timestamp": timestamp,
        "symbol": symbol,
        "timeframe": timeframe,
        "regime_state": regime,
        "confidence_score": confidence,
        "action": action,
        "invalidation_price": invalidation,
        "size_pct_equity": size,
        "thesis": thesis,
    }
