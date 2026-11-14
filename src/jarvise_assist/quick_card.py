"""On-demand quick sizing card and registration (QPR2a).

Places no orders. Binance access is limited to existing read-only GETs.
Never writes assist_settings or assist_edge. Not imported by watch, signals,
execute, quick, quick_tags, or pause.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from jarvise_assist.explore import contract_rejection, normalize_explore_symbol
from jarvise_assist.futures import FuturesPosition
from jarvise_assist.messages import (
    STOP_INSTRUCTION,
    _fmt_price_tick,
    account_leverage_warning_lines,
    fmt_price,
    fmt_qty,
    safe_symbol_configs,
)
from jarvise_assist.pause import bangkok_week_id, format_paused_until
from jarvise_assist.quick import (
    QUICK_USAGE,
    REG_TTL_MS,
    create_quick_registration,
    get_quick_settings,
    is_quick_locked,
)
from jarvise_assist.quick_tags import quick_symbols
from jarvise_assist.signals import build_existing_ideas
from jarvise_assist.sizing import (
    MAX_PORTFOLIO_RISK_PCT,
    SignalCandidate,
    assign_shared_leverage,
    below_min_notional,
    round_level_to_tick,
    size_signal,
)
from jarvise_assist.store import ensure_settings, symbol_has_in_flight_exec

logger = logging.getLogger(__name__)

SendFn = Callable[..., Any]

QUICK_FEE_BPS_PER_SIDE = Decimal("6")
QUICK_CARD_TTL_MS = 120_000
QUICK_REGISTER_PREFIX = "qr:"
BTN_QUICK_REGISTER = "⚡ เริ่มไม้ quick"

_BANGKOK = timezone(timedelta(hours=7), name="Asia/Bangkok")


def _refusal_locked() -> str:
    return "🔒 quick ล็อกอยู่ — รอถึง 00:00 หรือปลดล็อกจากเมนู Quick"


def _refusal_week_loss(until: int) -> str:
    return f"⏸ quick ลงทะเบียนไม่ได้ — ขาดทุนสัปดาห์นี้เกิน 4R · ระบบหลัก{format_paused_until(until)}"


def _refusal_quick_open() -> str:
    return "มีไม้ quick หรือการลงทะเบียนเปิดอยู่บนสัญลักษณ์นี้แล้ว"


def _refusal_main_position() -> str:
    return "ถือ position อยู่แล้วบนสัญลักษณ์นี้ — ปิดก่อนถึงเปิดไม้ quick ได้"


def _refusal_in_flight() -> str:
    return "มีคำสั่งของบอทค้างบนสัญลักษณ์นี้ — รอให้จบก่อน"


def _refusal_pending_order() -> str:
    return "มีออเดอร์เปิดที่ไม่ใช่ reduce-only บนสัญลักษณ์นี้ — ยกเลิกก่อน"


def _refusal_contract() -> str:
    return "สัญลักษณ์นี้ใช้ไม่ได้ (ต้องเป็น USDⓈ-M PERPETUAL สถานะ TRADING คู่ USDT)"


def _refusal_read_failed() -> str:
    return "อ่านข้อมูลจาก Binance ไม่ได้ — ลองใหม่"


def _refusal_stop_wrong_side() -> str:
    return "stop อยู่ผิดฝั่งของราคา"


def _refusal_below_min() -> str:
    return "ไม้เล็กกว่าขั้นต่ำของสัญญา"


def _refusal_size_failed() -> str:
    return "คำนวณขนาดไม้ไม่ได้"


def _refusal_quick_cap() -> str:
    return "เกินเพดานความเสี่ยง quick ที่เปิดพร้อมกัน"


def _refusal_portfolio_cap() -> str:
    return "เกินเพดานความเสี่ยงรวม (หลัก + quick ≤ 4%)"


def _refusal_margin_cap() -> str:
    return "margin ไม่พอ (available balance) — บอทไม่ย่อขนาดให้"


def _refusal_leverage_cap() -> str:
    return "stop แคบเกินไป — ขนาดตาม risk ต้องใช้ leverage เกิน 5x (บอทไม่ย่อขนาดให้)"


def _refusal_card_expired() -> str:
    return "การ์ดหมดอายุแล้ว — พิมพ์คำสั่ง q ใหม่"


def _refusal_bad_data() -> str:
    return "ข้อมูลปุ่มไม่ถูกต้อง — พิมพ์คำสั่ง q ใหม่"


REFUSAL: dict[str, str] = {
    "locked": _refusal_locked(),
    "quick_open": _refusal_quick_open(),
    "main_position": _refusal_main_position(),
    "in_flight": _refusal_in_flight(),
    "pending_order": _refusal_pending_order(),
    "contract": _refusal_contract(),
    "read_failed": _refusal_read_failed(),
    "stop_wrong_side": _refusal_stop_wrong_side(),
    "below_min_notional": _refusal_below_min(),
    "size_failed": _refusal_size_failed(),
    "quick_cap": _refusal_quick_cap(),
    "portfolio_cap": _refusal_portfolio_cap(),
    "margin_cap": _refusal_margin_cap(),
    "leverage_cap": _refusal_leverage_cap(),
    "card_expired": _refusal_card_expired(),
    "bad_data": _refusal_bad_data(),
}


def main_week_loss_pause_until(conn: Any, *, now_ms: int) -> int | None:
    """Return paused_until_ms when the main weekly 4R pause is active, else None.

    Rule: active pause with pause_reason == 'week_loss' returns until.
    Any other non-NULL reason (streak / day_loss / day_loss_deeper) → None.
    NULL reason (pause set before migration 29) falls back to the legacy
    week_pause_id window rule: paused_until_ms > now AND week_pause_id ∈
    {bangkok_week_id(until − 86_400_000), bangkok_week_id(now)}.

    Read-only: plain SELECTs. Does not call ensure_settings or get_edge
    (both INSERT a default row when missing).
    """
    row_s = conn.execute("SELECT paused_until_ms, pause_reason FROM assist_settings WHERE id=1").fetchone()
    if row_s is None or row_s[0] is None:
        return None
    until = int(row_s[0])
    if until <= int(now_ms):
        return None
    reason = row_s[1] if len(row_s) > 1 else None
    if reason == "week_loss":
        return until
    if reason is not None:
        return None
    row_e = conn.execute("SELECT week_pause_id FROM assist_edge WHERE id=1").fetchone()
    if row_e is None or row_e[0] is None:
        return None
    week_id = str(row_e[0])
    allowed = {
        bangkok_week_id(until - 86_400_000),
        bangkok_week_id(int(now_ms)),
    }
    if week_id not in allowed:
        return None
    return until


def parse_quick_command(text: str) -> dict[str, Any] | None:
    """Parse ``q <SYMBOL> <long|short> <stop>``. None when not a quick command."""
    if not re.match(r"^\s*q(?:uick)?\s+\S", text or "", re.I):
        return None
    parts = (text or "").strip().split()
    # q / quick + 3 tokens
    if len(parts) < 2:
        return {"error": "usage"}
    head = parts[0].lower()
    if head not in {"q", "quick"}:
        return None
    tokens = parts[1:]
    if len(tokens) != 3:
        return {"error": "usage"}
    raw_sym, raw_side, raw_stop = tokens
    sym = raw_sym.upper().replace("/", "").replace("-", "").replace(",", "")
    if not sym.endswith("USDT"):
        sym = f"{sym}USDT"
    symbol = normalize_explore_symbol(sym)
    if symbol is None:
        return {"error": "usage"}
    side_l = raw_side.lower()
    if side_l in {"long", "l", "buy"}:
        side = "long"
    elif side_l in {"short", "s", "sell"}:
        side = "short"
    else:
        return {"error": "usage"}
    stop_raw = raw_stop.replace(",", "")
    if stop_raw.endswith("%"):
        try:
            pct = Decimal(stop_raw[:-1])
        except Exception:  # noqa: BLE001
            return {"error": "usage"}
        if not pct.is_finite():
            return {"error": "usage"}
        if pct <= 0 or pct >= 20:
            return {"error": "usage"}
        return {"symbol": symbol, "side": side, "stop_pct": pct}
    try:
        price = Decimal(stop_raw)
    except Exception:  # noqa: BLE001
        return {"error": "usage"}
    if not price.is_finite():
        return {"error": "usage"}
    if price <= 0:
        return {"error": "usage"}
    if price >= Decimal("1e9"):
        return {"error": "usage"}
    return {"symbol": symbol, "side": side, "stop": price}


def _fmt_usdt(value: Decimal) -> str:
    """USDT display: half-up to 0.01, then fmt_price (strip trailing zeros)."""
    return fmt_price(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _fmt_r_abs(value: Decimal) -> str:
    """Absolute R to 2 decimals (no sign)."""
    q = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{q:.2f}"


def _fmt_r_signed(value: Decimal) -> str:
    """Signed R: + when >0, − (U+2212) when <0, bare 0.00R when 0."""
    q = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if q > 0:
        return f"+{q:.2f}R"
    if q < 0:
        return f"−{(-q):.2f}R"
    return f"{abs(q):.2f}R"


def fee_breakdown(entry: Decimal, stop: Decimal) -> dict[str, Any]:
    """Net-of-fee R math. f = (2 × 6 / 10000) × entry / r."""
    r = abs(entry - stop)
    if r <= 0:
        raise ValueError("stop distance must be positive")
    f = (Decimal("2") * QUICK_FEE_BPS_PER_SIDE / Decimal("10000")) * entry / r
    net_1 = Decimal("1") - f
    net_15 = Decimal("15") / Decimal("10") - f
    loss = -(Decimal("1") + f)

    def _be(k: Decimal) -> int:
        return int(math.ceil(float(Decimal("100") * (Decimal("1") + f) / (k + Decimal("1")))))

    return {
        "f": f,
        "net_1r": net_1,
        "net_1_5r": net_15,
        "loss_r": loss,
        "be_1r_pct": _be(Decimal("1")),
        "be_1_5r_pct": _be(Decimal("15") / Decimal("10")),
        "warn": (Decimal("3") * f) > Decimal("1"),
    }


def _is_reduce_or_close(order: dict[str, Any]) -> bool:
    raw_r = order.get("reduceOnly")
    if raw_r is True or str(raw_r or "").lower() in {"true", "1"}:
        return True
    raw_c = order.get("closePosition")
    if raw_c is True or str(raw_c or "").lower() in {"true", "1"}:
        return True
    return False


def _hhmmss(ms: int) -> str:
    local = datetime.fromtimestamp(ms / 1000.0, tz=_BANGKOK)
    return local.strftime("%H:%M:%S")


def _hhmm(ms: int) -> str:
    local = datetime.fromtimestamp(ms / 1000.0, tz=_BANGKOK)
    return local.strftime("%H:%M")


def _fail(reason: str, text: str | None = None) -> dict[str, Any]:
    return {"ok": False, "reason": reason, "text": text or REFUSAL.get(reason, reason)}


def build_quick_card(
    conn: Any,
    futures: Any,
    *,
    symbol: str,
    side: str,
    stop: Decimal | None = None,
    stop_pct: Decimal | None = None,
    now_ms: int,
) -> dict[str, Any]:
    """Build the on-demand sizing card. Read-only except lock clear."""
    sym = str(symbol).upper()
    side_l = str(side).lower()

    if is_quick_locked(conn, now_ms=now_ms):
        return _fail("locked")

    week_until = main_week_loss_pause_until(conn, now_ms=now_ms)
    if week_until is not None:
        return _fail("week_loss_pause", _refusal_week_loss(week_until))

    if sym in quick_symbols(conn):
        return _fail("quick_open")

    watch = conn.execute(
        """
        SELECT 1 FROM assist_watch_position
        WHERE symbol=? AND closed_at_ms IS NULL
        """,
        (sym,),
    ).fetchone()
    if watch is not None:
        return _fail("main_position")

    if symbol_has_in_flight_exec(conn, sym):
        return _fail("in_flight")

    if futures is None:
        return _fail("read_failed")

    leftover_n = 0
    try:
        row = futures.exchange_symbol(sym)
        if contract_rejection(row) is not None:
            return _fail("contract")
        positions = list(futures.position_risk())
        for pos in positions:
            if str(pos.symbol).upper() == sym:
                return _fail("main_position")
        orders = list(futures.all_open_orders())
        pending = False
        for order in orders:
            if str(order.get("symbol") or "").upper() != sym:
                continue
            if _is_reduce_or_close(order):
                leftover_n += 1
            else:
                pending = True
        if pending:
            return _fail("pending_order")
        mark = Decimal(str(futures.mark_price(sym)))
        filters = futures.symbol_filters(sym)
        balance, available = futures.usdt_wallet_and_available()
        balance = Decimal(str(balance))
        available = Decimal(str(available))
    except Exception as exc:  # noqa: BLE001
        logger.warning("quick card read failed: %s", type(exc).__name__)
        return _fail("read_failed")

    tick = Decimal(str(filters.tick_size))
    step = Decimal(str(filters.step_size))
    if stop_pct is not None:
        pct = Decimal(str(stop_pct))
        if side_l == "long":
            raw_stop = mark * (Decimal("1") - pct / Decimal("100"))
        else:
            raw_stop = mark * (Decimal("1") + pct / Decimal("100"))
    elif stop is not None:
        raw_stop = Decimal(str(stop))
    else:
        return _fail("stop_wrong_side")

    stop_level = round_level_to_tick(raw_stop, tick, side=side_l, kind="stop")
    if stop_level <= 0:
        return _fail("stop_wrong_side")
    if side_l == "long" and stop_level >= mark:
        return _fail("stop_wrong_side")
    if side_l == "short" and stop_level <= mark:
        return _fail("stop_wrong_side")

    qs = get_quick_settings(conn)
    risk_pct = Decimal(str(qs["risk_pct"]))
    cap_pct = Decimal(str(qs["cap_pct"]))

    sized = size_signal(
        symbol=sym,
        side=side_l,
        entry=mark,
        stop=stop_level,
        balance=balance,
        risk_pct=risk_pct,
        step_size=step,
    )
    if sized is None:
        return _fail("size_failed")
    if "leverage_cap" in sized.reduce_reasons:
        return _fail("leverage_cap")
    if below_min_notional(
        qty=sized.qty,
        notional=sized.notional,
        min_qty=Decimal(str(filters.min_qty)),
        min_notional=Decimal(str(filters.min_notional)),
    ):
        return _fail("below_min_notional")

    survivors, _dropped = assign_shared_leverage(
        [SignalCandidate(sized=sized, confidence=0.0, symbol_rank=0)],
        available_balance=available,
    )
    if not survivors or "margin_cap" in survivors[0].sized.reduce_reasons:
        return _fail("margin_cap")
    sized = survivors[0].sized
    if below_min_notional(
        qty=sized.qty,
        notional=sized.notional,
        min_qty=Decimal(str(filters.min_qty)),
        min_notional=Decimal(str(filters.min_notional)),
    ):
        return _fail("below_min_notional")

    main_risk_pct = Decimal(str(ensure_settings(conn)["risk_pct"]))
    main_ideas = build_existing_ideas(
        conn,
        positions=positions,
        orders=orders,
        balance=balance,
        risk_pct=main_risk_pct,
        now_ms=now_ms,
        include_sent_held=False,
    )
    main_open = sum((i.risk_usdt for i in main_ideas), Decimal("0"))

    quick_open = Decimal("0")
    for row in conn.execute("SELECT risk_usdt FROM assist_quick_trade WHERE status='open'").fetchall():
        quick_open += Decimal(str(row[0] or 0))
    for row in conn.execute(
        """
        SELECT planned_entry, planned_stop, qty FROM assist_quick_registration
        WHERE status='open'
        """
    ).fetchall():
        pe, ps, q = Decimal(str(row[0])), Decimal(str(row[1])), Decimal(str(row[2]))
        quick_open += abs(pe - ps) * q

    new_risk = sized.loss_at_stop
    if quick_open + new_risk > balance * (cap_pct / Decimal("100")):
        return _fail("quick_cap")
    if main_open + quick_open + new_risk > balance * (MAX_PORTFOLIO_RISK_PCT / Decimal("100")):
        return _fail("portfolio_cap")

    fees = fee_breakdown(mark, stop_level)
    f = fees["f"]
    r_dist = abs(mark - stop_level)
    if side_l == "long":
        t1 = mark + r_dist
        t15 = mark + (Decimal("15") / Decimal("10")) * r_dist
    else:
        t1 = mark - r_dist
        t15 = mark - (Decimal("15") / Decimal("10")) * r_dist
    t1 = round_level_to_tick(t1, tick, side=side_l, kind="target")
    t15 = round_level_to_tick(t15, tick, side=side_l, kind="target")

    stop_pct_shown = (stop_level - mark) / mark * Decimal("100")
    lev = max(1, int(sized.leverage))
    margin = (sized.notional / Decimal(lev)).quantize(Decimal("0.01"))
    base = sym[:-4] if sym.endswith("USDT") else sym
    notional_s = fmt_price(sized.notional.quantize(Decimal("0.01")))
    fee_usdt = (Decimal("2") * QUICK_FEE_BPS_PER_SIDE / Decimal("10000")) * sized.notional
    loss_total = sized.loss_at_stop + fee_usdt
    loss_total_s = _fmt_usdt(loss_total)
    loss_price_s = _fmt_usdt(sized.loss_at_stop)
    fee_usdt_s = _fmt_usdt(fee_usdt)
    f_r_s = _fmt_r_abs(f)
    loss_r_s = _fmt_r_abs(Decimal("1") + f)
    entry_s = _fmt_price_tick(mark, tick)
    stop_s = _fmt_price_tick(stop_level, tick)
    t1_s = _fmt_price_tick(t1, tick)
    t15_s = _fmt_price_tick(t15, tick)

    bal_s = fmt_price(balance)
    budget = sized.budget_usdt if sized.budget_usdt is not None else balance * (risk_pct / Decimal("100"))
    budget_s = fmt_price(budget)

    def _pct_of_bal(usdt: Decimal) -> str:
        if balance <= 0:
            return "0"
        return fmt_price((usdt / balance * Decimal("100")).quantize(Decimal("0.01")))

    def _target_line(label: str, target_s: str, net: Decimal, be_pct: int) -> str:
        if net <= 0:
            tail = "ชนะทุกไม้ก็ไม่มีกำไร"
        else:
            tail = f"ต้องชนะ ≥ {be_pct}% ถึงคุ้มทุน"
        return f"เป้า {label}: {target_s} → สุทธิ {_fmt_r_signed(net)} · {tail}"

    card_until = int(now_ms) + QUICK_CARD_TTL_MS
    side_label = "LONG" if side_l == "long" else "SHORT"
    lines = [
        f"⚡ Quick · {sym} · {side_label}",
        f"เข้า (mark ตอนนี้): {entry_s}",
        f"Stop: {stop_s} ({float(stop_pct_shown):+.2f}%)",
        f"ขนาด: {fmt_qty(sized.qty)} {base} ≈ {notional_s} USDT",
        f"ฐาน: Futures {bal_s} USDT × {fmt_price(risk_pct)}% = งบ {budget_s} USDT",
        f"Leverage ที่ต้องใช้: {lev}x · margin ≈ {fmt_price(margin)} USDT",
        (
            f"ถ้าชน stop: −{loss_total_s} USDT ≈ −{loss_r_s}R รวมค่าธรรมเนียม "
            f"(ราคา −{loss_price_s} USDT = 1R · ค่าธรรมเนียม ≈ {fee_usdt_s} USDT)"
        ),
        (f"ค่าธรรมเนียมไป-กลับ ≈ {f_r_s}R ≈ {fee_usdt_s} USDT (โมเดล 6 bps/ข้าง — ยังไม่อ่านค่าจริงจากบัญชี)"),
        _target_line("1R", t1_s, fees["net_1r"], int(fees["be_1r_pct"])),
        _target_line("1.5R", t15_s, fees["net_1_5r"], int(fees["be_1_5r_pct"])),
    ]
    if fees["warn"]:
        lines.append("⚠ ค่าธรรมเนียมสูงเทียบระยะ stop — reward < 3× ค่าไป-กลับ")
    if leftover_n > 0:
        lines.append(f"⚠ มีคำสั่งป้องกันค้างบนสัญลักษณ์นี้ {leftover_n} อัน — ตรวจก่อนเข้า")
    cfg = safe_symbol_configs(futures).get(sym)
    for warn in account_leverage_warning_lines(sized, cfg):
        lines.append(warn)
    lines.append(
        f"ความเสี่ยงเปิดอยู่: หลัก {_pct_of_bal(main_open)}% · "
        f"quick {_pct_of_bal(quick_open)}% + ไม้นี้ {_pct_of_bal(new_risk)}% "
        f"(เพดาน quick {fmt_price(cap_pct)}% · รวม {fmt_price(MAX_PORTFOLIO_RISK_PCT)}%)"
    )
    lines.append(
        f"การ์ดนี้ใช้ได้ถึง {_hhmmss(card_until)} · บอทไม่ส่งคำสั่ง — กดลงทะเบียน แล้วเปิดเองใน Binance ภายใน 10 นาที"
    )
    lines.append(STOP_INSTRUCTION)

    side_code = "l" if side_l == "long" else "s"
    cb = f"{QUICK_REGISTER_PREFIX}{sym}:{side_code}:{stop_s}:{int(now_ms) // 1000}"
    reply_markup: dict[str, Any] | None
    if len(cb.encode("utf-8")) > 64:
        reply_markup = None
        lines.append("ปุ่มลงทะเบียนยาวเกิน 64 ไบต์ — พิมพ์คำสั่งใหม่")
    else:
        reply_markup = {"inline_keyboard": [[{"text": BTN_QUICK_REGISTER, "callback_data": cb}]]}

    return {
        "ok": True,
        "text": "\n".join(lines),
        "reply_markup": reply_markup,
        "entry": mark,
        "stop": stop_level,
        "qty": sized.qty,
        "leverage": lev,
        "tick": tick,
        "symbol": sym,
        "side": side_l,
        "positions": positions,
        "fees": fees,
        "callback_data": cb if reply_markup is not None else None,
    }


def handle_quick_text(
    conn: Any,
    text: str,
    *,
    futures: Any,
    send: SendFn,
    now_ms: int,
) -> dict[str, Any] | None:
    parsed = parse_quick_command(text)
    if parsed is None:
        return None
    if parsed.get("error") == "usage":
        send(QUICK_USAGE)
        return {"handled": True, "kind": "quick_card", "ok": False, "reason": "usage"}
    card = build_quick_card(
        conn,
        futures,
        symbol=str(parsed["symbol"]),
        side=str(parsed["side"]),
        stop=parsed.get("stop"),
        stop_pct=parsed.get("stop_pct"),
        now_ms=now_ms,
    )
    send(card["text"], reply_markup=card.get("reply_markup"))
    return {
        "handled": True,
        "kind": "quick_card",
        "ok": bool(card.get("ok")),
        "reason": card.get("reason"),
    }


def handle_quick_register_callback(
    conn: Any,
    data: str,
    *,
    futures: Any,
    send: SendFn,
    now_ms: int,
) -> dict[str, Any]:
    """Handle qr:* tap. Never calls answer (listen does that). Exactly one send."""
    parts = data.split(":")
    if len(parts) != 5 or parts[0] != "qr":
        send(REFUSAL["bad_data"])
        return {"ok": False, "reason": "bad_data"}
    _prefix, sym, side_code, stop_s, created_s = parts
    sym = sym.upper()
    if side_code not in {"l", "s"}:
        send(REFUSAL["bad_data"])
        return {"ok": False, "reason": "bad_data"}
    try:
        created_sec = int(created_s)
        stop_level = Decimal(stop_s)
    except Exception:  # noqa: BLE001
        send(REFUSAL["bad_data"])
        return {"ok": False, "reason": "bad_data"}
    if not stop_level.is_finite() or stop_level <= 0 or stop_level >= Decimal("1e9"):
        send(REFUSAL["bad_data"])
        return {"ok": False, "reason": "bad_data"}

    created_ms = created_sec * 1000
    age = int(now_ms) - created_ms
    if age > QUICK_CARD_TTL_MS or age < -60_000:
        send(REFUSAL["card_expired"])
        return {"ok": False, "reason": "card_expired"}

    side = "long" if side_code == "l" else "short"
    card = build_quick_card(
        conn,
        futures,
        symbol=sym,
        side=side,
        stop=stop_level,
        now_ms=now_ms,
    )
    if not card.get("ok"):
        send(card["text"])
        return {"ok": False, "reason": card.get("reason")}

    week_until = main_week_loss_pause_until(conn, now_ms=now_ms)
    if week_until is not None:
        send(_refusal_week_loss(week_until))
        return {"ok": False, "reason": "week_loss_pause"}

    positions: list[FuturesPosition] = list(card.get("positions") or [])
    rid, reason = create_quick_registration(
        conn,
        symbol=sym,
        side=side,
        planned_entry=float(card["entry"]),
        planned_stop=float(card["stop"]),
        qty=float(card["qty"]),
        now_ms=now_ms,
        live_positions=positions,
    )
    if rid is None:
        mapped = {
            "open_registration": "quick_open",
            "open_quick_trade": "quick_open",
            "open_assist_watch": "main_position",
            "live_position": "main_position",
            "locked": "locked",
        }.get(str(reason), str(reason))
        text = REFUSAL.get(mapped, REFUSAL.get(str(reason), str(reason)))
        send(text)
        return {"ok": False, "reason": mapped}

    expires = int(now_ms) + REG_TTL_MS
    side_label = "LONG" if side == "long" else "SHORT"
    tick = Decimal(str(card.get("tick") or "0.0001"))
    entry_s = _fmt_price_tick(Decimal(str(card["entry"])), tick)
    stop_fmt = _fmt_price_tick(Decimal(str(card["stop"])), tick)
    qty_s = fmt_qty(Decimal(str(card["qty"])))
    body = "\n".join(
        [
            (
                f"✅ ลงทะเบียนไม้ quick {sym} {side_label} แล้ว — "
                f"เปิดเองใน Binance ภายใน 10 นาที (ถึง {_hhmm(expires)} น.)"
            ),
            f"เข้า {entry_s} · stop {stop_fmt} · qty {qty_s}",
            STOP_INSTRUCTION,
            (f"บอทไม่ส่งคำสั่ง · ไม้แรกฝั่ง {side_label} บน {sym} ในช่วงนี้จะนับเป็น quick"),
        ]
    )
    send(body)
    return {"ok": True, "registration_id": rid}
