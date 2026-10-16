"""Trading suitability analysis & ranking for crypto symbols.

Ranks symbols based on actionable directional signals, model confidence,
trend regime clarity, EMA alignment, RSI momentum health, and data freshness.
Does NOT loosen confidence floors or trade rules.
"""

from __future__ import annotations

from typing import Any


def evaluate_symbol_suitability(
    symbol: str,
    *,
    analysis: dict[str, Any] | None = None,
    candle: dict[str, Any] | None = None,
    health: dict[str, Any] | None = None,
    position: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute a trading suitability score (0-100) and ranking metadata.

    Score components:
    1. Actionable Directional Signal (0 - 35 pts)
    2. Confidence Score from Engine (0 - 30 pts)
    3. Market Regime Clarity (0 - 15 pts)
    4. Trend Alignment & Moving Averages (0 - 10 pts)
    5. Data Health & Warm-up (0 - 10 pts)
    """
    sym = symbol.strip().upper()
    score = 0.0
    reasons: list[str] = []

    action = str((analysis or {}).get("action") or "flat").lower()
    regime = str((analysis or {}).get("regime_state") or "unknown").lower()
    conf = float((analysis or {}).get("confidence_score") or 0.0)

    close_val = (candle or {}).get("close")
    close = float(close_val) if close_val is not None else None
    ema20_val = (candle or {}).get("ema_20")
    ema20 = float(ema20_val) if ema20_val is not None else None
    ema200_val = (candle or {}).get("ema_200")
    ema200 = float(ema200_val) if ema200_val is not None else None
    rsi_val = (candle or {}).get("rsi_14")
    rsi = float(rsi_val) if rsi_val is not None else None
    atr_val = (candle or {}).get("atr_14")
    atr = float(atr_val) if atr_val is not None else None

    rows = int((health or {}).get("rows") or 0)
    ready = int((health or {}).get("ema200_ready") or 0)
    gaps = int((health or {}).get("gaps") or 0)
    age_min = (health or {}).get("newest_age_min")

    # 1. Actionable Signal (0 - 35 pts)
    if action in ("buy", "long"):
        score += 35.0
        reasons.append("สัญญาณ BUY/LONG ชัดเจน")
    elif action in ("sell", "short"):
        score += 35.0
        reasons.append("สัญญาณ SELL/SHORT ชัดเจน")
    elif action == "flat":
        score += 8.0
        reasons.append("สถานะ FLAT (รอสัญญาณสะสมพลัง)")
    else:
        score += 5.0
        reasons.append(f"สถานะ {action}")

    # 2. Confidence Score (0 - 30 pts)
    if conf > 0.0:
        # 0.70+ gives high points; 0.55 gives moderate; 0.40 gives baseline
        conf_pts = round(min(30.0, max(0.0, (conf / 0.85) * 30.0)), 1)
        score += conf_pts
        reasons.append(f"ความมั่นใจโมเดล {conf:.2f} ({int(conf * 100)}%)")
    else:
        reasons.append("ยังไม่มีคะแนนความมั่นใจ")

    # 3. Regime Clarity (0 - 15 pts)
    if regime in ("trend_up", "trend_down"):
        score += 15.0
        reasons.append(f"สภาวะตลาดเป็นเทรนด์ชัดเจน ({regime})")
    elif regime == "breakout":
        score += 13.0
        reasons.append("สภาวะตลาด Breakout กรอบราคา")
    elif regime == "range":
        score += 8.0
        reasons.append("สภาวะตลาด Sideway ในกรอบ (Range)")
    elif regime == "chaotic":
        score += 2.0
        reasons.append("สภาวะตลาดผันผวนสูง (Chaotic / Missing EMA200)")
    else:
        reasons.append(f"สภาวะตลาด {regime}")

    # 4. Trend Alignment & Technical Confirmation (0 - 10 pts)
    if close is not None and ema20 is not None and ema200 is not None:
        if action in ("buy", "long") and close > ema20 > ema200:
            score += 10.0
            reasons.append("ราคาเรียงตัว Bullish Stack (Close > EMA20 > EMA200)")
        elif action in ("sell", "short") and close < ema20 < ema200:
            score += 10.0
            reasons.append("ราคาเรียงตัว Bearish Stack (Close < EMA20 < EMA200)")
        elif (regime == "trend_up" and close > ema20) or (regime == "trend_down" and close < ema20):
            score += 7.0
            reasons.append("โมเมนตัมราคาสอดคล้องกับ EMA20")
        else:
            score += 4.0
    elif close is not None and ema20 is not None:
        score += 4.0

    # 5. Data Health & Freshness (0 - 10 pts)
    if ready > 0 and gaps == 0:
        score += 10.0
        reasons.append("ข้อมูลแท่งเทียนสมบูรณ์ (EMA200 พร้อม, ไร้ Gap)")
    elif ready > 0:
        score += 6.0
        reasons.append(f"EMA200 พร้อม ({gaps} gaps)")
    elif rows > 0:
        score += 3.0
        reasons.append("ประวัติแท่งเทียนยังไม่ครบเกณฑ์ EMA200 Warmup")
    else:
        reasons.append("ยังไม่มีข้อมูลแท่งเทียน")

    final_score = round(min(100.0, max(0.0, score)), 1)

    # Classification
    if final_score >= 80.0:
        grade = "high"
        label = "โดดเด่นมาก (High Suitability)"
        color = "#22c55e"  # green
    elif final_score >= 65.0:
        grade = "medium_high"
        label = "โดดเด่น (Promising)"
        color = "#10b981"  # emerald
    elif final_score >= 50.0:
        grade = "moderate"
        label = "ปานกลาง / รอจังหวะ (Moderate)"
        color = "#eab308"  # yellow
    else:
        grade = "low"
        label = "ไซด์เวย์ / พักตัว (Low)"
        color = "#94a3b8"  # slate

    return {
        "symbol": sym,
        "score": final_score,
        "grade": grade,
        "label": label,
        "color": color,
        "reasons": reasons,
        "action": action,
        "regime_state": regime,
        "confidence_score": conf,
        "thesis": (analysis or {}).get("thesis"),
        "analysis_timestamp": (analysis or {}).get("timestamp"),
        "candle": {
            "close": close,
            "ema_20": ema20,
            "ema_200": ema200,
            "rsi_14": rsi,
            "atr_14": atr,
        },
        "health": {
            "rows": rows,
            "ema200_ready": ready,
            "newest_age_min": age_min,
            "gaps": gaps,
        },
        "has_open_position": position is not None,
    }
