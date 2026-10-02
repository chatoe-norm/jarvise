"""Owner-facing recommendation card for a paper approval.

Deterministic Thai copy built from stored rows only — no LLM, no network. Paper only;
nothing here places orders. JSON keys stay English for the SPA.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jarvise_analyze.engine import ATR_STOP_MULT

APPROVE_CONF = 0.70
CAUTION_CONF = 0.55
HIGH_VOL_PCT = 3.0


def confidence_label(conf: float | None) -> str:
    c = float(conf or 0.0)
    if c >= APPROVE_CONF:
        return "ปานกลาง-สูง"
    if c >= CAUTION_CONF:
        return "ปานกลาง"
    return "ต่ำ"


def template_recommendation(action: str | None, conf: float | None) -> str:
    act = (action or "flat").lower()
    c = float(conf or 0.0)
    if act == "flat" or c < CAUTION_CONF:
        return "reject"
    if c >= APPROVE_CONF:
        return "approve"
    return "approve_with_caution"


def doctrine_query(row: Mapping[str, Any]) -> str:
    regime = row.get("regime_state") or "range"
    action = row.get("action") or "flat"
    return f"{regime} {action} entry risk stop"


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _headline(rec: str, action: str, conf: float) -> str:
    if action == "flat":
        return (
            "แนะนำ: REJECT — สัญญาณ flat "
            "(approve ไม่เปิดตำแหน่งใหม่; ถ้ามีตำแหน่งเปิดอยู่จะถูกปิด)"
        )
    if rec == "approve":
        return f"แนะนำ: APPROVE (ความมั่นใจ {conf:.2f})"
    if rec == "approve_with_caution":
        return f"แนะนำ: APPROVE แบบระวัง (ความมั่นใจ {conf:.2f} — ขนาดเล็ก, ดู stop ให้ดี)"
    return f"แนะนำ: REJECT (ความมั่นใจ {conf:.2f} ต่ำกว่าเกณฑ์ {CAUTION_CONF:.2f})"


def _what_happened(candle: Mapping[str, Any] | None, safety: Mapping[str, Any] | None) -> list[str]:
    lines: list[str] = []
    c = candle or {}
    close, ema20, ema200 = _f(c.get("close")), _f(c.get("ema_20")), _f(c.get("ema_200"))
    if close is None or ema20 is None or ema200 is None:
        lines.append(
            "ตัวชี้วัดระยะยาว (EMA200) ยังไม่พร้อม — ระบบจะไม่เสนอเทรดจนกว่าข้อมูลครบ"
        )
    elif close > ema20 > ema200:
        lines.append("ราคาอยู่เหนือเส้นค่าเฉลี่ยทั้งระยะสั้นและระยะยาว = แนวโน้มขาขึ้น")
    elif close < ema20 < ema200:
        lines.append("ราคาอยู่ต่ำกว่าเส้นค่าเฉลี่ยทั้งระยะสั้นและระยะยาว = แนวโน้มขาลง")
    else:
        lines.append("ราคาอยู่ระหว่างเส้นค่าเฉลี่ย = ยังไม่มีแนวโน้มชัด (ออกข้าง)")

    rsi = _f(c.get("rsi_14"))
    if rsi is not None:
        if rsi >= 70:
            lines.append(f"โมเมนตัม RSI {rsi:.0f} ร้อนแรง (ซื้อมากเกิน) — ระวังการย่อตัว")
        elif rsi <= 30:
            lines.append(f"โมเมนตัม RSI {rsi:.0f} เย็นจัด (ขายมากเกิน) — อาจเด้งกลับ")
        else:
            lines.append(f"โมเมนตัม RSI {rsi:.0f} ยังไม่ร้อนเกินไป")

    atr = _f(c.get("atr_14"))
    if atr is not None and close:
        pct = atr / close * 100.0
        if pct > HIGH_VOL_PCT:
            lines.append(f"ความผันผวนสูง ({pct:.1f}% ต่อแท่ง) — ขนาดตำแหน่งถูกลดลงอัตโนมัติ")
        else:
            lines.append(f"ความผันผวนปกติ ({pct:.1f}% ต่อแท่ง)")

    reasons = list((safety or {}).get("reasons") or [])
    if reasons:
        lines.append("market safety พบ: " + "; ".join(str(r) for r in reasons))
    else:
        lines.append("market safety ไม่พบสัญญาณอันตราย")
    return lines


def _risk(
    approval: Mapping[str, Any],
    candle: Mapping[str, Any] | None,
    analysis: Mapping[str, Any] | None,
    account: Mapping[str, Any],
) -> dict[str, Any]:
    equity = float(account.get("equity") or 0.0)
    size = _f(approval.get("size_pct_equity"))
    notional = round(equity * (size or 0.0) / 100.0, 2)
    invalidation = _f((analysis or {}).get("invalidation_price"))
    close = _f((candle or {}).get("close"))
    risk: dict[str, Any] = {
        "size_pct_equity": size,
        "notional_usd": notional,
        "equity_usd": equity,
        "invalidation_price": invalidation,
        "stop_atr_multiple": ATR_STOP_MULT,
    }
    if invalidation is not None and close:
        risk["est_loss_usd"] = round(notional * abs(close - invalidation) / close, 2)
    return risk


def _checklist(
    approval: Mapping[str, Any], position: Mapping[str, Any] | None
) -> list[str]:
    symbol = str(approval.get("symbol") or "")
    timeframe = str(approval.get("timeframe") or "")
    lines = [f"มีข่าวใหญ่ใน {timeframe} ข้างหน้าหรือไม่ (ระบบไม่เห็นข่าว)"]
    if position:
        lines.append(
            f"พอร์ตถือ {symbol} อยู่แล้ว ({position.get('side')} {position.get('qty')}) "
            "— approve จะเพิ่มหรือกลับทิศตำแหน่งเดิม"
        )
    else:
        lines.append("พอร์ตยังไม่ถือตำแหน่งเดียวกันซ้ำ")
    lines.append("คำสั่งนี้เป็น paper เท่านั้น — ไม่มีเงินจริงถูกส่งไปตลาด")
    return lines


def build_recommendation(
    *,
    approval: Mapping[str, Any],
    candle: Mapping[str, Any] | None,
    analysis: Mapping[str, Any] | None,
    account: Mapping[str, Any],
    position: Mapping[str, Any] | None,
    safety: Mapping[str, Any] | None,
    doctrine: list[str],
    claude: Mapping[str, Any] | None,
) -> dict[str, Any]:
    action = str(approval.get("action") or "flat").lower()
    conf = float(approval.get("confidence_score") or 0.0)
    rec = template_recommendation(action, conf)
    return {
        "ok": True,
        "approval_id": approval.get("id"),
        "symbol": approval.get("symbol"),
        "timeframe": approval.get("timeframe"),
        "action": action,
        "status": approval.get("status"),
        "recommendation": rec,
        "recommendation_source": "claude" if claude else "template",
        "confidence_label": confidence_label(conf),
        "headline": _headline(rec, action, conf),
        "what_happened": _what_happened(candle, safety),
        "risk": _risk(approval, candle, analysis, account),
        "doctrine": list(doctrine),
        "checklist": _checklist(approval, position),
        "thesis": (analysis or {}).get("thesis"),
        "claude": dict(claude) if claude else None,
        "paper_only": True,
    }
