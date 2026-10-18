"""Owner-facing recommendation card for a paper approval.

Deterministic Thai copy built from stored rows only — no LLM, no network. Paper only;
nothing here places orders. JSON keys stay English for the SPA.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jarvise_analyze.engine import ATR_STOP_MULT
from jarvise_analyze.explain import confidence_breakdown
from jarvise_paper.engine import load_paper_fee_bps

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


def template_recommendation(
    action: str | None,
    conf: float | None,
    *,
    position_side: str | None = None,
) -> str:
    act = (action or "flat").lower()
    c = float(conf or 0.0)
    if act == "flat":
        if position_side:
            return "approve"
        return "reject"
    if c < CAUTION_CONF:
        return "reject"
    if c >= APPROVE_CONF:
        return "approve"
    return "approve_with_caution"


def doctrine_query(row: Mapping[str, Any]) -> str:
    """Embed query for card / auto-decide doctrine lookup.

    Includes Jarvise doctrine anchors so retrieval prefers owner protocol notes over
    generic Notebook scrapes (raw regime/action queries alone surface Investopedia/TV).
    """
    regime = row.get("regime_state") or "range"
    action = row.get("action") or "flat"
    return (
        f"jarvise doctrine {regime} {action} entry pullback EMA20 "
        "risk stop capital preservation kill-switch FLAT"
    )


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _headline(rec: str, action: str, conf: float, *, position_side: str | None = None) -> str:
    if action == "flat" and position_side:
        return "แนะนำ: APPROVE — ปิดไม้ตามสัญญาณ FLAT"
    if action == "flat":
        return "แนะนำ: REJECT — สัญญาณ flat (approve ไม่เปิดตำแหน่งใหม่; ถ้ามีตำแหน่งเปิดอยู่จะถูกปิด)"
    if rec == "approve":
        return f"ผ่านเกณฑ์สัญญาณ (ความมั่นใจ {conf:.2f})"
    if rec == "approve_with_caution":
        return f"ผ่านเกณฑ์แบบระวัง (ความมั่นใจ {conf:.2f} — ขนาดเล็ก, ดู stop ให้ดี)"
    return f"แนะนำ: REJECT (ความมั่นใจ {conf:.2f} ต่ำกว่าเกณฑ์ {CAUTION_CONF:.2f})"


_REGIME_LINE = {
    "trend_up": "แท่งสัญญาณอ่านแนวโน้มขาขึ้น",
    "trend_down": "แท่งสัญญาณอ่านแนวโน้มขาลง",
    "range": "แท่งสัญญาณอ่านว่าออกข้าง",
    "chaotic": "แท่งสัญญาณอ่านแนวโน้มไม่ได้",
}


def _what_happened(
    candle: Mapping[str, Any] | None,
    safety: Mapping[str, Any] | None,
    *,
    analysis: Mapping[str, Any] | None = None,
    signal_candle: Mapping[str, Any] | None = None,
) -> list[str]:
    lines: list[str] = []
    regime = str((analysis or {}).get("regime_state") or "").lower()
    regime_line = _REGIME_LINE.get(regime)
    if regime_line is not None:
        signal_close = _f((signal_candle or {}).get("close"))
        if signal_close is not None:
            lines.append(f"{regime_line} (ปิด {signal_close:g})")
        else:
            lines.append(regime_line)
    c = candle or {}
    close, ema20, ema200 = _f(c.get("close")), _f(c.get("ema_20")), _f(c.get("ema_200"))
    if close is None or ema20 is None or ema200 is None:
        lines.append("ตัวชี้วัดระยะยาว (EMA200) ยังไม่พร้อม — ระบบจะไม่เสนอเทรดจนกว่าข้อมูลครบ")

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

    vol = _f(c.get("volume"))
    if vol is not None and vol > 0:
        lines.append(f"ปริมาณการซื้อขายแท่งล่าสุด: {vol:,.2f}")

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
        price_loss = notional * abs(close - invalidation) / close
        fee = notional * 2.0 * load_paper_fee_bps() / 10_000.0
        risk["est_loss_usd"] = round(price_loss + fee, 2)
    return risk


def _checklist(
    approval: Mapping[str, Any],
    position: Mapping[str, Any] | None,
    *,
    has_news: bool = False,
) -> list[str]:
    symbol = str(approval.get("symbol") or "")
    timeframe = str(approval.get("timeframe") or "")
    if has_news:
        lines = ["ดูบริบทข่าวด้านล่าง (ไม่ใช่สัญญาณ)"]
    else:
        lines = [f"มีข่าวใหญ่ใน {timeframe} ข้างหน้าหรือไม่ (ระบบไม่เห็นข่าว)"]
    action = str(approval.get("action") or "flat").lower()
    if position:
        held = str(position.get("side") or "").lower()
        held_line = f"พอร์ตถือ {symbol} อยู่แล้ว ({position.get('side')} {position.get('qty')})"
        if action == "flat":
            lines.append(f"{held_line} — approve ปิดไม้")
        elif held == action:
            lines.append(f"{held_line} — approve ไม่เติมไม้")
        else:
            lines.append(f"{held_line} — approve ปิดไม้เดิมแล้วเปิดฝั่งใหม่")
    else:
        lines.append("พอร์ตยังไม่ถือตำแหน่งเดียวกันซ้ำ")
    lines.append("คำสั่งนี้เป็น paper เท่านั้น — ไม่มีเงินจริงถูกส่งไปตลาด")
    return lines


def news_context_block(report: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Card copy for a precomputed study. Missing report leaves the checklist unchanged."""
    if not report or report.get("not_a_trade_signal") is not True:
        return None
    lines = [str(line) for line in (report.get("lines") or []) if str(line).strip()]
    if not lines:
        return None
    return {"not_a_trade_signal": True, "lines": lines}


def evidence_line(gate: Mapping[str, Any] | None) -> str | None:
    """Owner copy when the stored auto EV gate is latched or the sample EV is negative.

    Does not read or write the latch. Callers pass the status they already computed.
    """
    if not gate:
        return None
    ev = _f(gate.get("ev"))
    latched = bool(gate.get("latched"))
    if not latched and (ev is None or ev >= 0):
        return None
    if ev is not None and ev >= 0:
        return None
    n = gate.get("n")
    n_txt = "—" if n is None else str(int(n))
    ev_txt = "—" if ev is None else f"{ev:.2f}"
    return f"หลักฐานย้อนหลัง: EV ของระบบยังติดลบ (n={n_txt}, EV {ev_txt}) — การ Approve เป็นการตัดสินใจของคุณ"


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
    signal_candle: Mapping[str, Any] | None = None,
    news_report: Mapping[str, Any] | None = None,
    ev_gate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    action = str(approval.get("action") or "flat").lower()
    conf = float(approval.get("confidence_score") or 0.0)
    position_side = str(position.get("side")) if position and position.get("side") else None
    rec = template_recommendation(action, conf, position_side=position_side)
    explain_src = signal_candle if signal_candle is not None else candle
    breakdown = confidence_breakdown(explain_src or {})
    news = news_context_block(news_report)
    what = _what_happened(candle, safety, analysis=analysis, signal_candle=signal_candle)
    if conf < APPROVE_CONF and not doctrine:
        what.append(
            f"ความมั่นใจ {conf:.2f} < {APPROVE_CONF:.2f} และไม่พบกฎการเทรดตรงสัญญาณ "
            "→ auto-decide จะ defer ให้เจ้าของตัดสินใจเอง"
        )
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
        "headline": _headline(rec, action, conf, position_side=position_side),
        "evidence": evidence_line(ev_gate),
        "what_happened": what,
        "risk": _risk(approval, candle, analysis, account),
        "doctrine": list(doctrine),
        "checklist": _checklist(approval, position, has_news=news is not None),
        "news_context": news,
        "thesis": (analysis or {}).get("thesis"),
        "claude": dict(claude) if claude else None,
        "confidence_breakdown": breakdown,
        "gates": {"flat": CAUTION_CONF, "approve": APPROVE_CONF},
        "paper_only": True,
    }
