"""Thai refusal catalogue for Quick cards. Pure: no I/O, no DB, no orders."""

from __future__ import annotations

import logging
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_UP, Decimal

from jarvise_assist.messages import _fmt_price_tick, fmt_price, fmt_qty
from jarvise_assist.quick_minlot import fmt_pct, minimum_contract_qty
from jarvise_assist.sizing import floor_to_step, round_level_to_tick

logger = logging.getLogger(__name__)

STOP_STEP_HINT = "เลือก stop หรือพิมพ์ราคา/เปอร์เซ็นต์ (เช่น 67000 หรือ 0.4%)"

_GENERIC_REG = "❌ ลงทะเบียนไม่ได้ — ยังไม่ได้ลงทะเบียน · 👉 ดูสถานะในเมนู ⚡ Quick"


def fmt_usdt(value: Decimal) -> str:
    """USDT display: half-up to 0.01, then fmt_price (strip trailing zeros)."""
    return fmt_price(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def fmt_usdt_floor(value: Decimal) -> str:
    """USDT display for remaining headroom: round down to 0.01, then fmt_price."""
    return fmt_price(value.quantize(Decimal("0.01"), rounding=ROUND_FLOOR))


def fmt_pct_bounded(value: Decimal, *, limit: Decimal | None = None) -> str:
    """Format a percent for display; huge values avoid InvalidOperation in fmt_pct."""
    if value >= Decimal("1000000"):
        return "> 999999.99"
    if limit is not None:
        return fmt_pct(value, mode="failed_limit", limit=limit)
    return fmt_pct(value)


def _fmt_total_display(total: Decimal, cap_usdt: Decimal) -> str:
    """Show totals above cap even when half-up USDT rounds equal to the cap line."""
    cap_s = fmt_usdt(cap_usdt)
    total_s = fmt_usdt(total)
    if total > cap_usdt and Decimal(total_s) <= Decimal(cap_s):
        return fmt_usdt(cap_usdt + Decimal("0.01"))
    return total_s


def fmt_pct_ceiling(value: Decimal) -> str:
    """Minimum stop percent: ceiling to 2 d.p. so the hint is reachable."""
    shown = value.quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    return f"{shown:.2f}"


def fmt_pct_remaining_down(value: Decimal) -> str:
    """Remaining cap percent: round down so the line never overstates room."""
    shown = value.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    return f"{shown:.2f}"


def format_current_stop_pct(stop_pct: Decimal, min_stop_pct_s: str) -> str:
    """Too-tight stop percent. Floor when half-up would print at least the minimum."""
    half = stop_pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if half >= Decimal(min_stop_pct_s):
        shown = stop_pct.quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
    else:
        shown = half
    return f"{shown:.2f}"


def fmt_fee_r(value: Decimal) -> str:
    """Fee in R: fixed 2 decimals, half-up, keep trailing zeros."""
    shown = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{shown:.2f}"


def _truncate_token(tok: str) -> str:
    if len(tok) > 32:
        return tok[:32] + "…"
    return tok


def e_minlot(
    *,
    qty: Decimal,
    base: str,
    risk_usdt: Decimal,
    fee_usdt: Decimal,
    real_risk_pct: Decimal,
    cap_pct: Decimal,
    wallet_balance: Decimal,
    mark: Decimal,
    stop: Decimal,
    stop_max_pct: Decimal | None,
    stop_max_price: Decimal | None,
    tick: Decimal,
) -> str:
    cap_usdt = wallet_balance * cap_pct / Decimal("100")
    cap_s = fmt_usdt(cap_usdt)
    wallet_s = fmt_price(wallet_balance)
    risk_total = fmt_usdt(risk_usdt + fee_usdt)
    risk_price = fmt_usdt(risk_usdt)
    fee_s = fmt_usdt(fee_usdt)
    qty_s = fmt_qty(qty)
    pct = fmt_pct(real_risk_pct, mode="failed_limit", limit=cap_pct)
    stop_pct = fmt_pct(abs(stop - mark) / mark * Decimal("100"))
    if stop_max_price is not None and stop_max_pct is not None:
        tail = (
            f"👉 ไม้ขั้นต่ำรับ stop ได้ไกลสุด ≈ {fmt_pct(stop_max_pct, mode='floor')}% "
            f"({_fmt_price_tick(stop_max_price, tick)})"
        )
    else:
        tail = "👉 ขั้นต่ำของสัญญาใหญ่เกินสำหรับ wallet นี้"
    return (
        f"❌ ไม่เปิด — ขั้นต่ำ {qty_s} {base} เสี่ยง {risk_total} USDT ({pct}%) "
        f"(ราคา {risk_price} + ค่าธรรมเนียม {fee_s}) เกินเพดาน {cap_s} USDT "
        f"({fmt_price(cap_pct)}% ของ wallet {wallet_s}) "
        f"· stop ห่าง {stop_pct}% กว้างเกินไปสำหรับ Quick · {tail}"
    )


def e_fit_over(
    *,
    qty: Decimal,
    base: str,
    risk_usdt: Decimal,
    fee_usdt: Decimal,
    real_risk_pct: Decimal,
    cap_pct: Decimal,
    wallet_balance: Decimal,
) -> str:
    cap_usdt = wallet_balance * cap_pct / Decimal("100")
    cap_s = fmt_usdt(cap_usdt)
    wallet_s = fmt_price(wallet_balance)
    risk_total = fmt_usdt(risk_usdt + fee_usdt)
    risk_price = fmt_usdt(risk_usdt)
    fee_s = fmt_usdt(fee_usdt)
    qty_s = fmt_qty(qty)
    pct = fmt_pct(real_risk_pct, mode="failed_limit", limit=cap_pct)
    return (
        f"❌ ไม่เปิด — margin ไม่พอสำหรับขนาดตาม risk · ขนาดที่ margin พอ {qty_s} {base} "
        f"เสี่ยง {risk_total} USDT ({pct}%) (ราคา {risk_price} + ค่าธรรมเนียม {fee_s}) "
        f"เกินเพดาน {cap_s} USDT ({fmt_price(cap_pct)}% ของ wallet {wallet_s}) · "
        f"👉 ลดความเสี่ยงในเมนู ⚡ Quick หรือโอน USDT เข้า Futures"
    )


def e_stop_tight(
    stop_pct: Decimal,
    risk_pct: Decimal,
    fee_r: Decimal,
    min_stop_pct: Decimal,
    min_stop_price_s: str,
    side: str,
) -> str:
    min_s = fmt_pct_ceiling(min_stop_pct)
    cur_s = format_current_stop_pct(stop_pct, min_s)
    risk_s = fmt_price(risk_pct)
    fee_s = fmt_fee_r(fee_r)
    price_op = "≥" if str(side).lower() == "short" else "≤"
    return (
        f"❌ stop ห่าง {cur_s}% แคบกว่าขั้นต่ำ ≈ {min_s}% (risk {risk_s}% ÷ 5x) · "
        f"ค่าธรรมเนียมไป-กลับ ≈ {fee_s}R · "
        f"👉 ขยาย stop ≥ {min_s}% ({price_op} {min_stop_price_s}) หรือลดความเสี่ยงในเมนู ⚡ Quick"
    )


def e_margin_legacy(qty_s: str, base: str, notional_s: str, need_s: str, available_s: str) -> str:
    return (
        f"❌ margin ไม่พอ — ขนาดตาม risk {qty_s} {base} ≈ {notional_s} USDT "
        f"ต้องใช้ ≈ {need_s} USDT ที่ 5x มี {available_s} USDT · "
        f"👉 พิมพ์ q ใหม่ บอทจะเสนอขนาดที่ margin พอ"
    )


def e_no_available() -> str:
    return "❌ margin ไม่พอ — มีให้ใช้ 0 USDT ใน Futures · 👉 โอน USDT เข้า Futures หรือปิดไม้อื่น"


def e_margin_offer(
    *,
    need_margin: Decimal,
    available: Decimal,
    q_fit: Decimal,
    q_min: Decimal,
    base: str,
) -> str:
    need_s = fmt_usdt(need_margin)
    avail_s = fmt_usdt(available)
    q_fit_s = fmt_qty(q_fit)
    q_min_s = fmt_qty(q_min)
    return (
        f"❌ margin ไม่พอแม้ใช้ 5x — ต้องใช้ ≈ {need_s} USDT (รวมค่าธรรมเนียม + กันไว้ 2%) "
        f"มี {avail_s} USDT · "
        f"ขนาดใหญ่สุดที่พอ {q_fit_s} < ขั้นต่ำ {q_min_s} {base} · 👉 โอนเข้า Futures หรือปิดไม้อื่น"
    )


def e_below_min_legacy(qty_s: str, base: str, notional_s: str, qmin_s: str, min_notional_s: str) -> str:
    return (
        f"❌ ไม้เล็กกว่าขั้นต่ำ — ขนาดตาม risk {qty_s} {base} ≈ {notional_s} USDT "
        f"ต่ำกว่าขั้นต่ำ {qmin_s} {base} / {min_notional_s} USDT · "
        f"👉 พิมพ์ q ใหม่ บอทจะเสนอขนาดขั้นต่ำถ้าความเสี่ยงไม่เกินเพดาน"
    )


def format_qmin(
    *,
    mark: Decimal,
    min_qty: Decimal,
    min_notional: Decimal,
    step: Decimal,
) -> str:
    """Contract minimum quantity, formatted. Uses ``minimum_contract_qty``."""
    return fmt_qty(
        minimum_contract_qty(
            mark=mark,
            min_qty=min_qty,
            min_notional=min_notional,
            step=step,
        )
    )


def e_size_zero_legacy(
    budget_s: str,
    dist_s: str,
    stop_pct: str,
    raw_qty_s: str,
    step_s: str,
    base: str,
) -> str:
    return (
        f"❌ ขนาดตาม risk ปัดเป็น 0 — งบ {budget_s} USDT ÷ ระยะ stop {dist_s} ({stop_pct}%) "
        f"= {raw_qty_s} {base} ต่ำกว่า 1 step {step_s} {base} · "
        f"👉 พิมพ์ q ใหม่เพื่อดูขนาดขั้นต่ำและความเสี่ยงจริง"
    )


def e_size_failed(balance_s: str, risk_pct_s: str) -> str:
    return f"❌ คำนวณขนาดไม่ได้ — wallet {balance_s} USDT · risk {risk_pct_s}% · 👉 ตรวจยอดใน Futures"


def suggest_quick_preset(
    *,
    balance: Decimal,
    risk_pct: Decimal,
    mark: Decimal,
    stop: Decimal,
    step: Decimal,
    min_qty: Decimal,
    min_notional: Decimal,
    remaining: Decimal,
    main_open: Decimal,
    quick_open: Decimal,
    portfolio_cap_pct: Decimal,
    risk_allowed: tuple[float, ...] | list[float] | tuple[Decimal, ...] | list[Decimal],
) -> tuple[Decimal, Decimal] | None:
    """First preset strictly below the stored risk whose floored qty fits both caps.

    ``remaining`` is unclamped (cap minus open quick risk). Returns ``(p, p_risk)`` or None.
    """
    dist = abs(mark - stop)
    if dist <= 0 or balance <= 0 or step <= 0 or mark <= 0:
        return None
    portfolio_cap_usdt = balance * portfolio_cap_pct / Decimal("100")
    presets = sorted((Decimal(str(p)) for p in risk_allowed), reverse=True)
    for p in presets:
        if p >= risk_pct:
            continue
        qty_p = floor_to_step(balance * p / Decimal("100") / dist, step)
        if qty_p <= 0:
            continue
        if qty_p < min_qty or qty_p * mark < min_notional:
            continue
        p_risk = qty_p * dist
        if p_risk > remaining:
            continue
        if main_open + quick_open + p_risk > portfolio_cap_usdt:
            continue
        return p, p_risk
    return None


def e_quick_cap(
    *,
    cap_pct: Decimal,
    balance: Decimal,
    open_usdt: Decimal,
    new_usdt: Decimal,
    preset: tuple[Decimal, Decimal] | None,
) -> str:
    cap_usdt = balance * cap_pct / Decimal("100")
    total = open_usdt + new_usdt
    remaining_raw = cap_usdt - open_usdt
    remaining = remaining_raw if remaining_raw > 0 else Decimal("0")
    if balance > 0:
        remaining_pct = remaining / balance * Decimal("100")
    else:
        remaining_pct = Decimal("0")
    head = (
        f"❌ เกินเพดาน Quick รวม {fmt_price(cap_pct)}% = {fmt_usdt(cap_usdt)} USDT · "
        f"เปิดอยู่ {fmt_usdt(open_usdt)} + ไม้นี้ {fmt_usdt(new_usdt)} = "
        f"{_fmt_total_display(total, cap_usdt)} · "
        f"เหลือ {fmt_usdt_floor(remaining)} USDT ({fmt_pct_remaining_down(remaining_pct)}%)"
    )
    if preset is None:
        return f"{head} · 👉 ปิดไม้หรือรอทะเบียนหมดอายุ"
    p, p_risk = preset
    return (
        f"{head} · 👉 ตั้งความเสี่ยง {fmt_price(p)}% ในเมนู ⚡ Quick "
        f"(ไม้นี้จะเสี่ยง {fmt_usdt(p_risk)} USDT) หรือปิดไม้/รอทะเบียนหมดอายุ"
    )


def e_portfolio_cap(
    cap_pct: Decimal,
    balance: Decimal,
    main: Decimal,
    quick: Decimal,
    new: Decimal,
) -> str:
    cap_usdt = balance * cap_pct / Decimal("100")
    total = main + quick + new
    return (
        f"❌ เกินเพดานความเสี่ยงรวม (หลัก + quick ≤ {fmt_price(cap_pct)}%) = {fmt_usdt(cap_usdt)} USDT · "
        f"หลัก {fmt_usdt(main)} + quick {fmt_usdt(quick)} + ไม้นี้ {fmt_usdt(new)} = "
        f"{_fmt_total_display(total, cap_usdt)} · "
        f"👉 ลดความเสี่ยงในเมนู ⚡ Quick หรือปิดไม้"
    )


def e_stop_side(side: str, stop: Decimal, mark: Decimal, tick: Decimal) -> str:
    if stop <= 0:
        return "❌ stop ต้องมากกว่า 0"
    stop_s = _fmt_price_tick(stop, tick)
    mark_tick_s = _fmt_price_tick(mark, tick)
    if stop != mark and stop_s == mark_tick_s:
        mark_s = fmt_price(mark)
    else:
        mark_s = mark_tick_s
    if str(side).lower() == "short":
        rel = "ต่ำกว่า" if stop < mark else "เท่ากับ"
        return (
            f"❌ SHORT ต้องตั้ง stop เหนือราคา — stop {stop_s} {rel} mark {mark_s} · "
            f"👉 ใส่ราคา > {mark_s} หรือเป็น % เช่น 0.4%"
        )
    rel = "สูงกว่า" if stop > mark else "เท่ากับ"
    return (
        f"❌ LONG ต้องตั้ง stop ใต้ราคา — stop {stop_s} {rel} mark {mark_s} · "
        f"👉 ใส่ราคา < {mark_s} หรือเป็น % เช่น 0.4%"
    )


def e_stop_far(dist_pct: Decimal) -> str:
    shown = fmt_pct_bounded(dist_pct, limit=Decimal("20"))
    return f"❌ stop ห่าง {shown}% เกิน 20% · ถ้าหมายถึงเปอร์เซ็นต์ให้ใส่ % (เช่น 0.5%)"


def e_quick_open_reg(sym: str, until_hhmm: str) -> str:
    return f"❌ {sym} มีทะเบียน quick ค้างอยู่ (หมดอายุ {until_hhmm}) · 👉 รอหมดอายุหรือเปิดไม้นั้นใน Binance"


def e_quick_open_trade(sym: str, entry_s: str) -> str:
    return f"❌ {sym} มีไม้ quick เปิดอยู่ (เข้า {entry_s}) · 👉 ปิดไม้นั้นก่อนเปิดไม้ใหม่บนเหรียญนี้"


def e_quick_open_reg_grace(sym: str) -> str:
    return f"❌ {sym} มีทะเบียน quick ที่หมดเวลาแล้ว รอระบบปิด · 👉 ดูสถานะในเมนู ⚡ Quick"


def e_parse_count(n: int) -> str:
    text = f"❌ ต้องมี 3 ค่า: เหรียญ ฝั่ง stop (ได้ {n})"
    if n > 3:
        text += " · ค่าเพิ่มเติม เช่น r= tp= จะใช้ได้ในรอบถัดไป"
    return text


def e_parse_side(tok: str) -> str:
    shown = _truncate_token(tok)
    return f'❌ ไม่รู้จักฝั่ง "{shown}" — รับ long / l / buy / short / s / sell'


def e_parse_symbol(tok: str) -> str:
    shown = _truncate_token(tok)
    return f'❌ ไม่รู้จักเหรียญ "{shown}"'


def e_parse_stop(tok: str) -> str:
    shown = _truncate_token(tok)
    return f'❌ stop "{shown}" ไม่ถูกต้อง — ใส่ราคา (เช่น 62000) หรือ % ที่มากกว่า 0 และน้อยกว่า 20 (เช่น 0.4%)'


def e_draft_stop_token(tok: str) -> str:
    shown = _truncate_token(tok)
    return f'❌ "{shown}" ไม่ใช่ราคาหรือ % — {STOP_STEP_HINT}'


def e_draft_stop_pct_range(pct_s: str) -> str:
    return f"❌ stop {pct_s}% ต้องมากกว่า 0 และน้อยกว่า 20%"


def e_cutoff_block(*, minutes: int) -> str:
    return f"❌ หลัง 23:30 ไม่ลงทะเบียน quick ใหม่ · cutoff 00:00 อีก {int(minutes)} นาที · 👉 เริ่มใหม่ได้หลัง 00:00"


def e_reg(code: str) -> str:
    if code == "qty_non_positive":
        return "❌ ขนาดไม้เป็น 0 — ยังไม่ได้ลงทะเบียน · 👉 กด 🔄 อัปเดตราคา"
    if code == "invalid_side":
        return "❌ ฝั่งไม่ถูกต้อง — ยังไม่ได้ลงทะเบียน · 👉 เริ่มใหม่จากเมนู ⚡ Quick"
    logger.warning("quick registration refusal unknown code=%s", code)
    return _GENERIC_REG


def e_setting_risk_over_cap(risk_s: str, cap_s: str) -> str:
    return f"⚠️ ความเสี่ยงต่อไม้ {risk_s}% มากกว่าเพดาน {cap_s}% — ไม้ quick ใหม่จะถูกปฏิเสธเพราะเกินเพดานรวม"


def e_setting_reject(tok: str | None = None) -> str:
    tail = "ใส่ตัวเลขมากกว่า 0 และไม่เกิน 100 (เช่น 0.6 หรือ 1.2)"
    if not tok:
        return f"❌ ค่าใช้ไม่ได้ — {tail}"
    shown = _truncate_token(tok)
    return f'❌ ค่า "{shown}" ใช้ไม่ได้ — {tail}'


def e_confirm_stale_ttl() -> str:
    return "ปุ่มนี้หมดอายุแล้ว (เกิน 60 วิ) — ไม่ได้เปลี่ยนอะไร"


def e_confirm_stale() -> str:
    return "ปุ่มนี้หมดอายุแล้ว — ไม่ได้เปลี่ยนอะไร"


def min_stop_price_s(
    *,
    mark: Decimal,
    tick: Decimal,
    side: str,
    min_stop_pct: Decimal,
) -> str:
    """Price next to the ceiling-formatted minimum stop percent."""
    displayed = Decimal(fmt_pct_ceiling(min_stop_pct))
    if str(side).lower() == "long":
        raw = mark * (Decimal("1") - displayed / Decimal("100"))
    else:
        raw = mark * (Decimal("1") + displayed / Decimal("100"))
    level = round_level_to_tick(raw, tick, side=side, kind="stop")
    return _fmt_price_tick(level, tick)
