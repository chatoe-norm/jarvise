"""Explainable confidence ladder — mirrors ``engine.analyze_snapshot`` without changing scores.

Pure function: same inputs → same ``total`` as ``confidence_score``. Thai step labels for
the owner-facing SPA. Do not import LLM / network here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jarvise_analyze.engine import (
    CHAOTIC_ATR_PCT,
    CONFIDENCE_THRESHOLD,
    RANGE_EMA_PCT,
)

# Matches auto-decide / recommendation card "approve without doctrine" gate.
DOCTRINE_FREE_APPROVE = 0.70


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _rsi_conflicts(bullish: bool, bearish: bool, rsi: float | None) -> bool:
    if rsi is None:
        return False
    if bullish and rsi < 30:
        return True
    if bearish and rsi > 70:
        return True
    return False


def _trend_steps(rsi: float | None, *, side: str) -> tuple[float, list[dict[str, Any]]]:
    """Mirror ``engine._trend_confidence`` with labelled steps. Returns (base_after_rsi, steps)."""
    base = 0.6
    steps: list[dict[str, Any]] = [
        {
            "label": f"ฐาน {'trend_up' if side == 'long' else 'trend_down'}",
            "delta": base,
        }
    ]
    conf = base
    if rsi is None:
        steps.append({"label": "RSI ไม่พร้อม — ไม่ปรับ", "delta": 0.0})
        return conf, steps
    if side == "long":
        if 45 <= rsi <= 70:
            conf += 0.15
            steps.append(
                {
                    "label": f"RSI {rsi:.0f} อยู่ในโซน 45–70 (+0.15)",
                    "delta": 0.15,
                }
            )
        elif rsi < 30:
            conf -= 0.35
            steps.append(
                {
                    "label": f"RSI {rsi:.0f} เย็นจัด (<30) (−0.35)",
                    "delta": -0.35,
                }
            )
        elif rsi > 75:
            conf -= 0.1
            steps.append(
                {
                    "label": f"RSI {rsi:.0f} ร้อนแรง (>75) (−0.10)",
                    "delta": -0.1,
                }
            )
        else:
            steps.append(
                {
                    "label": f"RSI {rsi:.0f} นอกโซนปรับ — ไม่ปรับ",
                    "delta": 0.0,
                }
            )
    else:
        if 30 <= rsi <= 55:
            conf += 0.15
            steps.append(
                {
                    "label": f"RSI {rsi:.0f} อยู่ในโซน 30–55 (+0.15)",
                    "delta": 0.15,
                }
            )
        elif rsi > 70:
            conf -= 0.35
            steps.append(
                {
                    "label": f"RSI {rsi:.0f} ร้อนแรง (>70) (−0.35)",
                    "delta": -0.35,
                }
            )
        elif rsi < 25:
            conf -= 0.1
            steps.append(
                {
                    "label": f"RSI {rsi:.0f} เย็นจัด (<25) (−0.10)",
                    "delta": -0.1,
                }
            )
        else:
            steps.append(
                {
                    "label": f"RSI {rsi:.0f} นอกโซนปรับ — ไม่ปรับ",
                    "delta": 0.0,
                }
            )
    return max(0.05, min(0.95, conf)), steps


def _fixed(
    *,
    regime: str,
    side: str,
    total: float,
    label: str,
    insufficient: bool = False,
) -> dict[str, Any]:
    return {
        "regime": regime,
        "side": side,
        "base": total,
        "steps": [{"label": label, "delta": total}],
        "total": round(total, 4),
        "gates": {
            "flat_below": CONFIDENCE_THRESHOLD,
            "doctrine_free_approve": DOCTRINE_FREE_APPROVE,
        },
        "insufficient": insufficient,
    }


def confidence_breakdown(candle: Mapping[str, Any]) -> dict[str, Any]:
    """Return a step ladder whose ``total`` matches ``analyze_snapshot`` confidence_score."""
    close = _num(candle.get("close"))
    atr = _num(candle.get("atr_14"))
    rsi = _num(candle.get("rsi_14"))
    ema20 = _num(candle.get("ema_20"))
    ema200 = _num(candle.get("ema_200"))

    if close is None or atr is None or ema20 is None or ema200 is None or close <= 0:
        return _fixed(
            regime="chaotic",
            side="flat",
            total=0.1,
            label="ข้อมูลไม่ครบ (warm-up / missing) — stay flat",
            insufficient=True,
        )

    atr_pct = atr / close
    ema_gap_pct = abs(ema20 - ema200) / close
    bullish_stack = ema20 > ema200
    bearish_stack = ema20 < ema200

    if atr_pct >= CHAOTIC_ATR_PCT and _rsi_conflicts(bullish_stack, bearish_stack, rsi):
        return _fixed(
            regime="chaotic",
            side="flat",
            total=0.25,
            label=f"Chaotic: ATR {atr_pct:.1%} + momentum conflict vs EMA stack",
        )

    if ema_gap_pct < RANGE_EMA_PCT:
        return _fixed(
            regime="range",
            side="flat",
            total=0.4,
            label=f"Range: EMA gap {ema_gap_pct:.2%} ของราคา — ไม่มีทิศชัด",
        )

    if bullish_stack:
        conf, steps = _trend_steps(rsi, side="long")
        if close < ema20:
            conf = max(0.05, conf - 0.1)
            steps.append(
                {
                    "label": "ราคาปิดต่ำกว่า EMA20 (−0.10)",
                    "delta": -0.1,
                }
            )
        else:
            steps.append(
                {
                    "label": "ราคาปิดอยู่เหนือหรือเท่า EMA20 — ไม่หัก",
                    "delta": 0.0,
                }
            )
        return {
            "regime": "trend_up",
            "side": "long",
            "base": 0.6,
            "steps": steps,
            "total": round(conf, 4),
            "gates": {
                "flat_below": CONFIDENCE_THRESHOLD,
                "doctrine_free_approve": DOCTRINE_FREE_APPROVE,
            },
            "insufficient": False,
        }

    if bearish_stack:
        conf, steps = _trend_steps(rsi, side="short")
        if close > ema20:
            conf = max(0.05, conf - 0.1)
            steps.append(
                {
                    "label": "ราคาปิดสูงกว่า EMA20 (−0.10)",
                    "delta": -0.1,
                }
            )
        else:
            steps.append(
                {
                    "label": "ราคาปิดอยู่ใต้หรือเท่า EMA20 — ไม่หัก",
                    "delta": 0.0,
                }
            )
        return {
            "regime": "trend_down",
            "side": "short",
            "base": 0.6,
            "steps": steps,
            "total": round(conf, 4),
            "gates": {
                "flat_below": CONFIDENCE_THRESHOLD,
                "doctrine_free_approve": DOCTRINE_FREE_APPROVE,
            },
            "insufficient": False,
        }

    return _fixed(
        regime="range",
        side="flat",
        total=0.35,
        label="ไม่มี EMA stack ชัด — treat as range / flat",
    )
