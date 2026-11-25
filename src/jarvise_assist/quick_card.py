"""On-demand quick sizing card and registration (QPR2a).

Places no orders. Binance access is limited to existing read-only GETs.
Never writes assist_settings or assist_edge. Not imported by watch, signals,
execute, quick, quick_tags, or pause.
"""

from __future__ import annotations

import logging
import math
import os
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any

from jarvise_assist import quick_errors
from jarvise_assist.explore import contract_rejection, normalize_explore_symbol
from jarvise_assist.messages import (
    STOP_INSTRUCTION,
    _fmt_price_tick,
    account_leverage_warning_lines,
    fmt_price,
    fmt_qty,
    safe_symbol_configs,
)
from jarvise_assist.order_lines import format_closing_order_line
from jarvise_assist.pause import format_paused_until, main_week_loss_pause_until
from jarvise_assist.quick import (
    QUICK_USAGE,
    RISK_ALLOWED,
    get_quick_settings,
)
from jarvise_assist.quick_cutoff import cutoff_block_text, in_entry_block
from jarvise_assist.quick_minlot import (
    SizeOfferResult,
    fmt_pct,
    min_lot_stop_max,
    size_offer,
)
from jarvise_assist.quick_tags import quick_symbols
from jarvise_assist.signals import build_existing_ideas
from jarvise_assist.sizing import (
    TARGET_R,
    SignalCandidate,
    SizedSignal,
    assign_shared_leverage,
    below_min_notional,
    floor_to_step,
    portfolio_risk_cap_pct,
    round_level_to_tick,
    size_signal,
)
from jarvise_assist.store import ensure_settings, symbol_has_in_flight_exec

logger = logging.getLogger(__name__)

SendFn = Callable[..., Any]

QUICK_FEE_BPS_PER_SIDE = Decimal("6")
QUICK_CARD_TTL_MS = 120_000
VALID_SIZE_MODES = frozenset({"risk", "min_lot", "fit_margin"})
CARD_TTL_PREFIX = "การ์ดนี้ใช้ได้ถึง"
QUICK_REGISTER_PREFIX = "qr:"
BTN_QUICK_REGISTER = "⚡ เริ่มไม้ quick"

_BANGKOK = timezone(timedelta(hours=7), name="Asia/Bangkok")


def quick_mode_tag() -> str:
    raw = (os.environ.get("JARVISE_ASSIST_EXECUTE") or "off").strip().lower()
    if raw == "live":
        return "💰 เงินจริง"
    if raw == "testnet":
        return "🧪 เทสเน็ต · ราคา/ยอดอ่านจากบัญชีจริง"
    return "💰 เงินจริง · บอทไม่ส่งคำสั่ง"


def _refusal_week_loss(until: int, *, now_ms: int) -> str:
    return f"⏸ quick ลงทะเบียนไม่ได้ — ขาดทุนสัปดาห์นี้เกิน 4R · ระบบหลัก{format_paused_until(until, now_ms=now_ms)}"


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
    return "อ่านข้อมูลจาก Binance ไม่ได้ — ยังไม่ได้ลงทะเบียน"


def _refusal_stop_wrong_side() -> str:
    return "stop อยู่ผิดฝั่งของราคา"


def _refusal_below_min() -> str:
    return "ไม้เล็กกว่าขั้นต่ำของสัญญา"


def _refusal_size_failed() -> str:
    return "❌ คำนวณขนาดไม่ได้ — ตรวจยอดใน Futures"


def _refusal_stop_far() -> str:
    return "❌ stop ห่างเกิน 20% · ถ้าหมายถึงเปอร์เซ็นต์ให้ใส่ % (เช่น 0.5%)"


def _refusal_quick_cap() -> str:
    return "เกินเพดานความเสี่ยง quick ที่เปิดพร้อมกัน"


def _refusal_portfolio_cap(cap: Decimal | None = None) -> str:
    if cap is not None:
        return f"เกินเพดานความเสี่ยงรวม (หลัก + quick ≤ {fmt_price(cap)}%)"
    return "เกินเพดานความเสี่ยงรวม (หลัก + quick)"


def _refusal_margin_cap() -> str:
    return "margin ไม่พอ (available balance) — บอทไม่ย่อขนาดให้"


def _refusal_leverage_cap() -> str:
    return "stop แคบเกินไป — ขนาดตาม risk ต้องใช้ leverage เกิน 5x (บอทไม่ย่อขนาดให้)"


def _refusal_card_expired() -> str:
    return "การ์ดหมดอายุแล้ว — พิมพ์คำสั่ง q ใหม่"


def _refusal_bad_data() -> str:
    return "ข้อมูลปุ่มไม่ถูกต้อง — พิมพ์คำสั่ง q ใหม่"


def _refusal_cutoff_block() -> str:
    return "❌ หลัง 23:30 ไม่ลงทะเบียน quick ใหม่ · 👉 เริ่มใหม่ได้หลัง 00:00"


REFUSAL: dict[str, str] = {
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
    "stop_far": _refusal_stop_far(),
    "cutoff_block": _refusal_cutoff_block(),
}


def parse_quick_command(text: str) -> dict[str, Any] | None:
    """Parse ``q <SYMBOL> <long|short> <stop>``. None when not a quick command."""
    if not re.match(r"^\s*q(?:uick)?\s+\S", text or "", re.I):
        return None
    parts = (text or "").strip().split()
    # q / quick + 3 tokens
    if len(parts) < 2:
        return {"error": "usage", "field": "count", "count": max(0, len(parts) - 1)}
    head = parts[0].lower()
    if head not in {"q", "quick"}:
        return None
    tokens = parts[1:]
    if len(tokens) != 3:
        return {"error": "usage", "field": "count", "count": len(tokens)}
    raw_sym, raw_side, raw_stop = tokens
    sym = raw_sym.upper().replace("/", "").replace("-", "").replace(",", "")
    if not sym.endswith("USDT"):
        sym = f"{sym}USDT"
    symbol = normalize_explore_symbol(sym)
    if symbol is None:
        return {"error": "usage", "field": "symbol", "token": raw_sym, "count": 3}
    side_l = raw_side.lower()
    if side_l in {"long", "l", "buy"}:
        side = "long"
    elif side_l in {"short", "s", "sell"}:
        side = "short"
    else:
        return {"error": "usage", "field": "side", "token": raw_side, "count": 3}
    # token keeps the original stop spelling, before the comma strip below.
    stop_raw = raw_stop.replace(",", "")
    stop_err = {"error": "usage", "field": "stop", "token": raw_stop, "count": 3}
    if stop_raw.endswith("%"):
        try:
            pct = Decimal(stop_raw[:-1])
        except Exception:  # noqa: BLE001
            return stop_err
        if not pct.is_finite() or pct <= 0 or pct >= 20:
            return stop_err
        return {"symbol": symbol, "side": side, "stop_pct": pct}
    try:
        price = Decimal(stop_raw)
    except Exception:  # noqa: BLE001
        return stop_err
    if not price.is_finite() or price <= 0 or price >= Decimal("1e9"):
        return stop_err
    return {"symbol": symbol, "side": side, "stop": price}


_fmt_usdt = quick_errors.fmt_usdt


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


_GENERIC_FAIL = "❌ ทำรายการไม่ได้ — ดูสถานะในเมนู ⚡ Quick"


def _fail(reason: str, text: str | None = None) -> dict[str, Any]:
    shown = text or REFUSAL.get(reason) or _GENERIC_FAIL
    return {"ok": False, "reason": reason, "text": shown}


def _sized_stub(
    *,
    sym: str,
    side_l: str,
    mark: Decimal,
    stop_level: Decimal,
    risk_pct: Decimal,
    step: Decimal,
    balance: Decimal,
) -> SizedSignal:
    r = abs(mark - stop_level)
    if side_l == "long":
        target = mark + TARGET_R * r
    else:
        target = mark - TARGET_R * r
    budget = balance * (risk_pct / Decimal("100"))
    return SizedSignal(
        symbol=sym,
        side=side_l,
        entry=mark,
        stop=stop_level,
        target=target,
        qty=Decimal("0"),
        notional=Decimal("0"),
        leverage=1,
        loss_at_stop=Decimal("0"),
        risk_pct=risk_pct,
        r_distance=r,
        leverage_capped=False,
        uncapped_leverage=1,
        analysis_id=None,
        step_size=step,
        balance_usdt=balance,
        budget_usdt=budget,
        reduce_reasons=(),
    )


def _apply_size_offer(
    sized: SizedSignal | None,
    *,
    stub: SizedSignal,
    offer: Any,
) -> SizedSignal:
    base = sized if sized is not None else stub
    return replace(
        base,
        qty=offer.qty,
        notional=offer.notional,
        leverage=offer.leverage,
        loss_at_stop=offer.risk_usdt,
    )


def _format_size_offer_refusal(
    result: SizeOfferResult,
    *,
    mark: Decimal,
    stop: Decimal,
    wallet_balance: Decimal,
    available: Decimal,
    base: str,
    tick: Decimal,
    cap_pct: Decimal,
) -> str:
    reason = result.reason or ""
    if reason == "leverage_over_5x" and result.candidate is None:
        return quick_errors.e_no_available()
    cand = result.candidate
    if cand is None:
        return REFUSAL["size_failed"]
    if reason == "risk_over_1pct" and cand.kind == "min_lot":
        _dist, stop_max_pct, stop_max_price = min_lot_stop_max(
            candidate=cand,
            mark=mark,
            stop=stop,
            wallet_balance=wallet_balance,
            tick=tick,
            max_real_risk_pct=cap_pct,
        )
        return quick_errors.e_minlot(
            qty=cand.qty,
            base=base,
            risk_usdt=cand.risk_usdt,
            fee_usdt=cand.fee_usdt,
            real_risk_pct=cand.real_risk_pct,
            cap_pct=cap_pct,
            wallet_balance=wallet_balance,
            mark=mark,
            stop=stop,
            stop_max_pct=stop_max_pct,
            stop_max_price=stop_max_price,
            tick=tick,
        )
    if reason == "risk_over_1pct" and cand.kind == "fit_margin":
        return quick_errors.e_fit_over(
            qty=cand.qty,
            base=base,
            risk_usdt=cand.risk_usdt,
            fee_usdt=cand.fee_usdt,
            real_risk_pct=cand.real_risk_pct,
            cap_pct=cap_pct,
            wallet_balance=wallet_balance,
        )
    if reason == "leverage_over_5x" and result.need_margin is not None:
        return quick_errors.e_margin_offer(
            need_margin=result.need_margin,
            available=available,
            q_fit=result.q_fit or Decimal("0"),
            q_min=result.q_min or Decimal("0"),
            base=base,
        )
    return REFUSAL["size_failed"]


def _try_quick_size_offer(
    *,
    allow_offer: bool,
    sym: str,
    side_l: str,
    mark: Decimal,
    stop_level: Decimal,
    balance: Decimal,
    available: Decimal,
    risk_pct: Decimal,
    step: Decimal,
    filters: Any,
    sized: SizedSignal | None,
    risk_qty: Decimal,
    cap_pct: Decimal,
) -> tuple[SizedSignal | None, str, dict[str, Any] | None, str | None]:
    """Return (sized, size_mode, fail_dict, offer_bound). size_mode stays 'risk' when unchanged."""
    if not allow_offer:
        return sized, "risk", None, None
    base = sym[:-4] if sym.endswith("USDT") else sym
    min_qty = Decimal(str(filters.min_qty))
    min_notional = Decimal(str(filters.min_notional))
    tick = Decimal(str(filters.tick_size))
    result = size_offer(
        mark=mark,
        stop=stop_level,
        wallet_balance=balance,
        available=available,
        min_qty=min_qty,
        min_notional=min_notional,
        step=step,
        risk_qty=risk_qty,
        max_real_risk_pct=cap_pct,
    )
    offer = result.offer
    reason = result.reason
    if offer is not None:
        stub = _sized_stub(
            sym=sym,
            side_l=side_l,
            mark=mark,
            stop_level=stop_level,
            risk_pct=risk_pct,
            step=step,
            balance=balance,
        )
        return _apply_size_offer(sized, stub=stub, offer=offer), offer.kind, None, offer.bound
    if reason is None:
        return sized, "risk", None, None
    if reason in {"risk_over_1pct", "leverage_over_5x"}:
        text = _format_size_offer_refusal(
            result,
            mark=mark,
            stop=stop_level,
            wallet_balance=balance,
            available=available,
            base=base,
            tick=tick,
            cap_pct=cap_pct,
        )
        return sized, "risk", _fail(reason, text), None
    return (
        sized,
        "risk",
        _fail(
            "size_failed",
            quick_errors.e_size_failed(quick_errors.fmt_usdt(balance), fmt_price(risk_pct)),
        ),
        None,
    )


def _base_asset(sym: str) -> str:
    return sym[:-4] if sym.endswith("USDT") else sym


def _quick_open_text(conn: Any, sym: str, *, now_ms: int) -> str:
    reg = conn.execute(
        """
        SELECT expires_at_ms FROM assist_quick_registration
        WHERE status='open' AND UPPER(symbol)=?
        """,
        (sym,),
    ).fetchone()
    if reg is not None:
        raw_exp = reg[0]
        if raw_exp is None or int(now_ms) >= int(raw_exp):
            return quick_errors.e_quick_open_reg_grace(sym)
        return quick_errors.e_quick_open_reg(sym, _hhmm(int(raw_exp)))
    trade = conn.execute(
        """
        SELECT entry_avg FROM assist_quick_trade
        WHERE status='open' AND UPPER(symbol)=?
        """,
        (sym,),
    ).fetchone()
    if trade is not None:
        return quick_errors.e_quick_open_trade(sym, fmt_price(Decimal(str(trade[0] or 0))))
    return REFUSAL["quick_open"]


def _stop_tight_text(
    *,
    side: str,
    mark: Decimal,
    stop: Decimal,
    tick: Decimal,
    risk_pct: Decimal,
) -> str:
    dist = abs(stop - mark)
    stop_pct = (dist / mark * Decimal("100")) if mark > 0 else Decimal("0")
    fee_r = (Decimal("0.0012") * mark / dist) if dist > 0 else Decimal("0")
    min_stop_pct = risk_pct / Decimal("5")
    price_s = quick_errors.min_stop_price_s(mark=mark, tick=tick, side=side, min_stop_pct=min_stop_pct)
    return quick_errors.e_stop_tight(stop_pct, risk_pct, fee_r, min_stop_pct, price_s, side)


def _margin_legacy_text(*, sized: SizedSignal, available: Decimal, base: str) -> str:
    need = sized.notional / Decimal("5")
    return quick_errors.e_margin_legacy(
        fmt_qty(sized.qty),
        base,
        quick_errors.fmt_usdt(sized.notional),
        quick_errors.fmt_usdt(need),
        quick_errors.fmt_usdt(available),
    )


def _below_min_legacy_text(
    *,
    sized: SizedSignal,
    mark: Decimal,
    filters: Any,
    step: Decimal,
    base: str,
) -> str:
    min_qty = Decimal(str(filters.min_qty))
    min_notional = Decimal(str(filters.min_notional))
    qmin_s = quick_errors.format_qmin(mark=mark, min_qty=min_qty, min_notional=min_notional, step=step)
    return quick_errors.e_below_min_legacy(
        fmt_qty(sized.qty),
        base,
        quick_errors.fmt_usdt(sized.notional),
        qmin_s,
        quick_errors.fmt_usdt(min_notional),
    )


def _numbered_size_failed(
    *,
    allow_offer: bool,
    balance: Decimal,
    risk_pct: Decimal,
    mark: Decimal,
    stop_level: Decimal,
    step: Decimal,
    tick: Decimal,
    base: str,
) -> str:
    generic = quick_errors.e_size_failed(quick_errors.fmt_usdt(balance), fmt_price(risk_pct))
    if allow_offer or balance <= 0 or risk_pct <= 0 or mark <= 0 or step <= 0:
        return generic
    dist = abs(mark - stop_level)
    if dist <= 0:
        return generic
    budget = balance * (risk_pct / Decimal("100"))
    raw_qty = budget / dist
    if floor_to_step(raw_qty, step) > 0:
        return generic
    raw_5 = raw_qty.quantize(Decimal("0.00001"), rounding=ROUND_DOWN)
    dist_s = _fmt_price_tick(dist, tick) if tick > 0 else fmt_price(dist)
    return quick_errors.e_size_zero_legacy(
        quick_errors.fmt_usdt(budget),
        dist_s,
        fmt_pct(dist / mark * Decimal("100")),
        fmt_qty(raw_5),
        fmt_qty(step),
        base,
    )


def build_quick_card(
    conn: Any,
    futures: Any,
    *,
    symbol: str,
    side: str,
    stop: Decimal | None = None,
    stop_pct: Decimal | None = None,
    now_ms: int,
    allow_offer: bool = False,
    enforce_stop_far: bool = False,
) -> dict[str, Any]:
    """Build the on-demand sizing card. Read-only."""
    sym = str(symbol).upper()
    side_l = str(side).lower()

    week_until = main_week_loss_pause_until(conn, now_ms=now_ms)
    if week_until is not None:
        return _fail("week_loss_pause", _refusal_week_loss(week_until, now_ms=now_ms))

    if in_entry_block(now_ms):
        return _fail("cutoff_block", cutoff_block_text(now_ms))

    if sym in quick_symbols(conn):
        return _fail("quick_open", _quick_open_text(conn, sym, now_ms=now_ms))

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

    leftover_orders: list[dict[str, Any]] = []
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
                leftover_orders.append(order)
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
    if (
        stop_level <= 0
        or (side_l == "long" and stop_level >= mark)
        or (side_l == "short" and stop_level <= mark)
    ):
        return _fail("stop_wrong_side", quick_errors.e_stop_side(side_l, stop_level, mark, tick))
    if enforce_stop_far and stop_pct is None and stop is not None and mark > 0:
        raw_price = Decimal(str(stop))
        if abs(raw_price - mark) / mark > Decimal("0.20"):
            dist_pct = abs(raw_price - mark) / mark * Decimal("100")
            return _fail("stop_far", quick_errors.e_stop_far(dist_pct))

    qs = get_quick_settings(conn)
    risk_pct = Decimal(str(qs["risk_pct"]))
    cap_pct = Decimal(str(qs["cap_pct"]))

    size_mode = "risk"
    offer_bound: str | None = None
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
        sized, size_mode, offer_fail, offer_bound = _try_quick_size_offer(
            allow_offer=allow_offer,
            sym=sym,
            side_l=side_l,
            mark=mark,
            stop_level=stop_level,
            balance=balance,
            available=available,
            risk_pct=risk_pct,
            step=step,
            filters=filters,
            sized=None,
            risk_qty=Decimal("0"),
            cap_pct=cap_pct,
        )
        if offer_fail is not None:
            return offer_fail
        if sized is None:
            return _fail(
                "size_failed",
                _numbered_size_failed(
                    allow_offer=allow_offer,
                    balance=balance,
                    risk_pct=risk_pct,
                    mark=mark,
                    stop_level=stop_level,
                    step=step,
                    tick=tick,
                    base=_base_asset(sym),
                ),
            )
    elif "leverage_cap" in sized.reduce_reasons:
        return _fail(
            "leverage_cap",
            _stop_tight_text(side=side_l, mark=mark, stop=stop_level, tick=tick, risk_pct=risk_pct),
        )
    else:
        if below_min_notional(
            qty=sized.qty,
            notional=sized.notional,
            min_qty=Decimal(str(filters.min_qty)),
            min_notional=Decimal(str(filters.min_notional)),
        ):
            if not allow_offer:
                return _fail(
                    "below_min_notional",
                    _below_min_legacy_text(
                        sized=sized,
                        mark=mark,
                        filters=filters,
                        step=step,
                        base=_base_asset(sym),
                    ),
                )
            sized, size_mode, offer_fail, offer_bound = _try_quick_size_offer(
                allow_offer=True,
                sym=sym,
                side_l=side_l,
                mark=mark,
                stop_level=stop_level,
                balance=balance,
                available=available,
                risk_pct=risk_pct,
                step=step,
                filters=filters,
                sized=sized,
                risk_qty=sized.qty,
                cap_pct=cap_pct,
            )
            if offer_fail is not None:
                return offer_fail
            if sized is None:
                return _fail(
                    "size_failed",
                    _numbered_size_failed(
                        allow_offer=allow_offer,
                        balance=balance,
                        risk_pct=risk_pct,
                        mark=mark,
                        stop_level=stop_level,
                        step=step,
                        tick=tick,
                        base=_base_asset(sym),
                    ),
                )

        if size_mode == "risk":
            assert sized is not None
            risk_qty_pre = sized.qty
            survivors, _dropped = assign_shared_leverage(
                [SignalCandidate(sized=sized, confidence=0.0, symbol_rank=0)],
                available_balance=available,
            )
            if not survivors or "margin_cap" in survivors[0].sized.reduce_reasons:
                if not allow_offer:
                    return _fail(
                        "margin_cap",
                        _margin_legacy_text(sized=sized, available=available, base=_base_asset(sym)),
                    )
                sized, size_mode, offer_fail, offer_bound = _try_quick_size_offer(
                    allow_offer=True,
                    sym=sym,
                    side_l=side_l,
                    mark=mark,
                    stop_level=stop_level,
                    balance=balance,
                    available=available,
                    risk_pct=risk_pct,
                    step=step,
                    filters=filters,
                    sized=sized,
                    risk_qty=risk_qty_pre,
                    cap_pct=cap_pct,
                )
                if offer_fail is not None:
                    return offer_fail
            else:
                sized = survivors[0].sized
                fee_rt = Decimal("2") * QUICK_FEE_BPS_PER_SIDE / Decimal("10000")
                margin_need = (sized.notional / Decimal(sized.leverage) + fee_rt * sized.notional) / Decimal(
                    "0.98"
                )
                if allow_offer and margin_need > available:
                    sized, size_mode, offer_fail, offer_bound = _try_quick_size_offer(
                        allow_offer=True,
                        sym=sym,
                        side_l=side_l,
                        mark=mark,
                        stop_level=stop_level,
                        balance=balance,
                        available=available,
                        risk_pct=risk_pct,
                        step=step,
                        filters=filters,
                        sized=sized,
                        risk_qty=risk_qty_pre,
                        cap_pct=cap_pct,
                    )
                    if offer_fail is not None:
                        return offer_fail
                elif below_min_notional(
                    qty=sized.qty,
                    notional=sized.notional,
                    min_qty=Decimal(str(filters.min_qty)),
                    min_notional=Decimal(str(filters.min_notional)),
                ):
                    if not allow_offer:
                        return _fail(
                            "below_min_notional",
                            _below_min_legacy_text(
                                sized=sized,
                                mark=mark,
                                filters=filters,
                                step=step,
                                base=_base_asset(sym),
                            ),
                        )
                    sized, size_mode, offer_fail, offer_bound = _try_quick_size_offer(
                        allow_offer=True,
                        sym=sym,
                        side_l=side_l,
                        mark=mark,
                        stop_level=stop_level,
                        balance=balance,
                        available=available,
                        risk_pct=risk_pct,
                        step=step,
                        filters=filters,
                        sized=sized,
                        risk_qty=risk_qty_pre,
                        cap_pct=cap_pct,
                    )
                    if offer_fail is not None:
                        return offer_fail

    if sized is None:
        return _fail(
            "size_failed",
            _numbered_size_failed(
                allow_offer=allow_offer,
                balance=balance,
                risk_pct=risk_pct,
                mark=mark,
                stop_level=stop_level,
                step=step,
                tick=tick,
                base=_base_asset(sym),
            ),
        )

    main_risk_pct = Decimal(str(ensure_settings(conn)["risk_pct"]))
    portfolio_cap_pct = portfolio_risk_cap_pct(main_risk_pct)
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
    cap_usdt = balance * (cap_pct / Decimal("100"))
    if quick_open + new_risk > cap_usdt and allow_offer and size_mode == "risk":
        # Fee-aware risk qty can fit margin yet still exceed cap_pct; shrink via
        # size_offer (max_real_risk_pct=cap) before refusing — same path the
        # old fee-blind margin buffer used to trigger coincidentally.
        offered, size_mode_o, offer_fail, offer_bound_o = _try_quick_size_offer(
            allow_offer=True,
            sym=sym,
            side_l=side_l,
            mark=mark,
            stop_level=stop_level,
            balance=balance,
            available=available,
            risk_pct=risk_pct,
            step=step,
            filters=filters,
            sized=sized,
            risk_qty=sized.qty,
            cap_pct=cap_pct,
        )
        if offer_fail is not None:
            return offer_fail
        if offered is not None:
            sized = offered
            size_mode = size_mode_o
            offer_bound = offer_bound_o
            new_risk = sized.loss_at_stop
    if quick_open + new_risk > cap_usdt:
        preset = quick_errors.suggest_quick_preset(
            balance=balance,
            risk_pct=risk_pct,
            mark=mark,
            stop=stop_level,
            step=step,
            min_qty=Decimal(str(filters.min_qty)),
            min_notional=Decimal(str(filters.min_notional)),
            remaining=cap_usdt - quick_open,
            main_open=main_open,
            quick_open=quick_open,
            portfolio_cap_pct=portfolio_cap_pct,
            risk_allowed=RISK_ALLOWED,
        )
        return _fail(
            "quick_cap",
            quick_errors.e_quick_cap(
                cap_pct=cap_pct,
                balance=balance,
                open_usdt=quick_open,
                new_usdt=new_risk,
                preset=preset,
            ),
        )
    if main_open + quick_open + new_risk > balance * (portfolio_cap_pct / Decimal("100")):
        return _fail(
            "portfolio_cap",
            quick_errors.e_portfolio_cap(portfolio_cap_pct, balance, main_open, quick_open, new_risk),
        )

    if size_mode not in VALID_SIZE_MODES:
        size_mode = "risk"

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
    real_risk_pct = (loss_total / balance * Decimal("100")) if balance > 0 else Decimal("0")
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
    if size_mode == "min_lot":
        size_head = f"⚠️ ขนาดตาม risk {fmt_price(risk_pct)}% ต่ำกว่าขั้นต่ำ"
        size_detail = f"ขนาดที่เสนอ: {fmt_qty(sized.qty)} {base} ≈ {notional_s} USDT (ขั้นต่ำของสัญญา)"
    elif size_mode == "fit_margin":
        bound = offer_bound or "margin"
        if bound == "risk":
            size_head = "ขนาดเต็มตาม risk · ใช้ margin กันไว้ 2% (ระบบหลักกัน 5%)"
            size_detail = f"ขนาดที่เสนอ: {fmt_qty(sized.qty)} {base} ≈ {notional_s} USDT (ขนาดเต็มตาม risk)"
        else:
            size_head = "⚠️ margin ไม่พอสำหรับขนาดเต็ม"
            if bound == "risk_1pct":
                size_detail = (
                    f"ขนาดที่เสนอ: {fmt_qty(sized.qty)} {base} ≈ {notional_s} USDT "
                    f"(ใหญ่สุดที่ risk จริงรวมค่าธรรมเนียม ≤ {fmt_price(cap_pct)}%)"
                )
            else:
                size_detail = (
                    f"ขนาดที่เสนอ: {fmt_qty(sized.qty)} {base} ≈ {notional_s} USDT (ใหญ่สุดที่ margin พอที่ 5x)"
                )
    else:
        size_head = f"ขนาด: {fmt_qty(sized.qty)} {base} ≈ {notional_s} USDT"
        size_detail = f"ฐาน: Futures {bal_s} USDT × {fmt_price(risk_pct)}% = งบ {budget_s} USDT"

    lev_line = (
        f"Leverage ที่ต้องใช้: {lev}x isolated · margin ≈ {fmt_price(margin)} USDT "
        f"— ตั้งใน Binance เองก่อนเปิด · บอทไม่เปลี่ยนให้"
        if size_mode in {"min_lot", "fit_margin"}
        else f"Leverage ที่ต้องใช้: {lev}x · margin ≈ {fmt_price(margin)} USDT"
    )

    lines = [
        f"⚡ Quick · {sym} · {side_label}",
        quick_mode_tag(),
        f"เข้า (mark ตอนนี้): {entry_s}",
        f"Stop: {stop_s} ({float(stop_pct_shown):+.2f}%)",
    ]
    if size_mode in {"min_lot", "fit_margin"}:
        lines.extend(
            [
                size_head,
                size_detail,
                (
                    f"risk จริง: −{loss_total_s} USDT = {fmt_pct(real_risk_pct)}% "
                    f"ของ Futures {bal_s} USDT (เป้า {fmt_price(risk_pct)}% · เพดาน {fmt_price(cap_pct)}%)"
                ),
                lev_line,
            ]
        )
    else:
        lines.extend(
            [
                size_head,
                size_detail,
                (
                    f"risk จริง: −{loss_total_s} USDT = {fmt_pct(real_risk_pct)}% "
                    f"ของ Futures {bal_s} USDT (เป้า {fmt_price(risk_pct)}%)"
                ),
                lev_line,
            ]
        )
    lines.extend(
        [
            (
                f"ถ้าชน stop: −{loss_total_s} USDT ≈ −{loss_r_s}R รวมค่าธรรมเนียม "
                f"(ราคา −{loss_price_s} USDT = 1R · ค่าธรรมเนียม ≈ {fee_usdt_s} USDT)"
            ),
            (f"ค่าธรรมเนียมไป-กลับ ≈ {f_r_s}R ≈ {fee_usdt_s} USDT (โมเดล 6 bps/ข้าง — ยังไม่อ่านค่าจริงจากบัญชี)"),
            _target_line("1R", t1_s, fees["net_1r"], int(fees["be_1r_pct"])),
            _target_line("1.5R", t15_s, fees["net_1_5r"], int(fees["be_1_5r_pct"])),
        ]
    )
    if fees["warn"]:
        lines.append("⚠ ค่าธรรมเนียมสูงเทียบระยะ stop — reward < 3× ค่าไป-กลับ")
    if leftover_orders:
        lines.append(f"⚠ มีคำสั่งป้องกันค้างบนสัญลักษณ์นี้ {len(leftover_orders)} อัน — ตรวจก่อนเข้า:")
        for order in leftover_orders:
            lines.append(format_closing_order_line(order, tick=tick))
    cfg = safe_symbol_configs(futures).get(sym)
    for warn in account_leverage_warning_lines(sized, cfg):
        lines.append(warn)
    lines.append(
        f"ความเสี่ยงเปิดอยู่: หลัก {_pct_of_bal(main_open)}% · "
        f"quick {_pct_of_bal(quick_open)}% + ไม้นี้ {_pct_of_bal(new_risk)}% (ไม่รวมค่าธรรมเนียม) "
        f"(เพดาน quick {fmt_price(cap_pct)}% · รวม {fmt_price(portfolio_cap_pct)}%)"
    )
    lines.append(
        f"{CARD_TTL_PREFIX} {_hhmmss(card_until)} (หมดแล้วกด 🔄 อัปเดตราคา) · "
        "บอทไม่ส่งคำสั่ง — กดลงทะเบียน แล้วเปิดเองใน Binance ภายใน 10 นาที"
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
        "size_mode": size_mode,
    }


def handle_quick_text(
    conn: Any,
    text: str,
    *,
    futures: Any,
    send: SendFn,
    now_ms: int,
) -> dict[str, Any] | None:
    from jarvise_assist.quick_m1a1 import (
        _quoted_markup_for,
        create_card_from_typed_command,
    )

    parsed = parse_quick_command(text)
    if parsed is None:
        return None
    if parsed.get("error") == "usage":
        field = str(parsed.get("field") or "count")
        token = str(parsed.get("token") or "")
        count = int(parsed.get("count") or 0)
        if field == "side":
            detail = quick_errors.e_parse_side(token)
        elif field == "symbol":
            detail = quick_errors.e_parse_symbol(token)
        elif field == "stop":
            detail = quick_errors.e_parse_stop(token)
        else:
            detail = quick_errors.e_parse_count(count)
        send(f"{detail}\n{QUICK_USAGE}")
        return {"handled": True, "kind": "quick_card", "ok": False, "reason": "usage"}
    card_id, card = create_card_from_typed_command(conn, futures, parsed, now_ms=now_ms)
    markup = card.get("reply_markup")
    if card.get("ok") and card_id:
        markup = _quoted_markup_for(card_id, str(card.get("size_mode") or "risk"))
    elif not card.get("ok"):
        markup = None
    send(card["text"], reply_markup=markup)
    return {
        "handled": True,
        "kind": "quick_card",
        "ok": bool(card.get("ok")),
        "reason": card.get("reason"),
        "card_id": card_id,
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
        send(_refusal_week_loss(week_until, now_ms=now_ms))
        return {"ok": False, "reason": "week_loss_pause"}

    from jarvise_assist.quick_m1a1 import (
        _registration_refusal_text,
        create_quick_registration_from_card,
        registration_success_message,
    )

    rid, reason = create_quick_registration_from_card(
        conn,
        symbol=sym,
        side=side,
        card=card,
        now_ms=now_ms,
    )
    if rid is None:
        if reason == "same_side_cap":
            from jarvise_assist.quick_cap import count_open_same_side, same_side_cap_text

            send(same_side_cap_text(side, count_open_same_side(conn, side)))
        else:
            send(_registration_refusal_text(str(reason)))
        return {"ok": False, "reason": reason}

    tick = Decimal(str(card.get("tick") or "0.0001"))
    send(
        registration_success_message(
            sym=sym,
            side=side,
            entry=Decimal(str(card["entry"])),
            stop=Decimal(str(card["stop"])),
            qty=Decimal(str(card["qty"])),
            tick=tick,
            now_ms=now_ms,
        )
    )
    return {"ok": True, "registration_id": rid}
