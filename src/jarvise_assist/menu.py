"""Reply-keyboard handlers for the trade assistant."""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from jarvise_assist import ASSIST_SYMBOLS
from jarvise_assist.execute import assist_execute_mode, execution_buttons_enabled
from jarvise_assist.explore import render_explore_message
from jarvise_assist.futures import (
    BinanceFuturesClient,
    FuturesPosition,
    build_futures_client,
    find_protective_stop_info,
    find_take_profit_info,
    protective_stop_coverage,
    protective_stop_covered_qty,
    trigger_label,
)
from jarvise_assist.keyboard import (
    BTN_ANYTHING,
    BTN_POSITIONS,
    BTN_QUICK,
    BTN_RISK_PREFIX,
    BTN_TOGGLE,
    BTN_WEEKLY,
    LEGACY_BTN_ANYTHING,
    LEGACY_BTN_EXPLORE,
    LEGACY_BTN_RISK_PREFIX,
    LEGACY_BTN_TOGGLE,
    anything_now_keyboard,
    reply_keyboard,
    reply_keyboard_for,
    risk_choice_keyboard,
    risk_confirm_keyboard,
    toggle_confirm_keyboard,
)
from jarvise_assist.messages import (
    _fmt_price_tick,
    fmt_price,
    fmt_qty,
    format_prior_idea_line,
    format_signal_message,
    safe_symbol_configs,
)
from jarvise_assist.pause import (
    clear_pause_manual,
    format_paused_until,
    is_paused,
    pause_clear_warning,
    week_start_risk_pct,
)
from jarvise_assist.quick import render_quick_menu
from jarvise_assist.quick_tags import quick_symbols
from jarvise_assist.signals import scan_signal_candidates
from jarvise_assist.sizing import (
    below_min_notional,
    cap_requote_qty,
    chase_exceeded,
    next_assist_run_ms,
    portfolio_risk_cap_pct,
)
from jarvise_assist.store import (
    clamp_stored_risk_pct,
    ensure_settings,
    find_linkable_signal,
    get_watch_position,
    latest_seen_idea,
    write_settings,
)
from jarvise_assist.trail_advice import current_trail_advice
from jarvise_assist.trend_view import (
    build_trend_rows,
    fit_telegram,
    reason_label,
    render_trend_ranking,
)
from jarvise_assist.weekly import run_assist_weekly
from jarvise_ingest.db import load_candles_in_range
from jarvise_notify.telegram import send_message

SendFn = Callable[..., Any]

_NO_STOP_LINE = "⚠️ ยังไม่มี stop — ตั้ง Stop-Market (Reduce-Only, Mark) ทันที"
_MARGIN_LABEL = "margin"
RISK_MIN_PCT = 0.5
RISK_MAX_PCT = 3.0
TOGGLE_CONFIRM_TTL_S = 60
TOGGLE_CONFIRM_FUTURE_S = 5
_TOGGLE_CONFIRM_RE = re.compile(r"^at:(on|onp|off|no):([0-9]{10})$")
_RISK_YES_RE = re.compile(r"^risk_yes:([^:]+):([0-9]{10})$")


def _fmt_margin(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,.2f}"


def _margin_text(pos: FuturesPosition) -> str:
    unit = pos.margin_asset or "USDT"
    if pos.isolated_wallet is not None and pos.isolated_wallet > 0:
        return f"{_MARGIN_LABEL} {_fmt_margin(pos.isolated_wallet)} {unit}"
    if pos.position_initial_margin is not None and pos.position_initial_margin > 0:
        return f"{_MARGIN_LABEL} ≈ {_fmt_margin(pos.position_initial_margin)} {unit} (cross)"
    return f"{_MARGIN_LABEL} —"


def match_menu_label(text: str) -> str | None:
    raw = (text or "").strip()
    if raw == BTN_POSITIONS:
        return "positions"
    if raw == BTN_ANYTHING or raw == LEGACY_BTN_ANYTHING:
        return "anything"
    if raw == BTN_WEEKLY:
        return "weekly"
    if raw.startswith(BTN_RISK_PREFIX):
        return "risk"
    if raw.startswith(LEGACY_BTN_RISK_PREFIX):
        return "risk"
    if raw == BTN_TOGGLE or raw == LEGACY_BTN_TOGGLE:
        return "toggle"
    if raw == LEGACY_BTN_EXPLORE:
        return "explore"
    if raw == BTN_QUICK:
        return "quick"
    return None


def is_legacy_menu_label(text: str) -> bool:
    raw = (text or "").strip()
    if raw == LEGACY_BTN_ANYTHING:
        return True
    if raw.startswith(LEGACY_BTN_RISK_PREFIX):
        return True
    if raw == LEGACY_BTN_TOGGLE:
        return True
    if raw == LEGACY_BTN_EXPLORE:
        return True
    return False


def handle_menu_text(
    conn: Any,
    text: str,
    *,
    futures: BinanceFuturesClient | None = None,
    now_ms: int,
    send: SendFn | None = None,
) -> dict[str, Any]:
    kind = match_menu_label(text)
    if kind is None:
        return {"handled": False}
    from jarvise_assist.quick_m1a1 import cancel_quick_drafts

    cancel_quick_drafts(conn, now_ms=now_ms)
    if kind != "explore" and ensure_settings(conn).get("awaiting_explore"):
        write_settings(conn, awaiting_explore=False)
    send_fn = send or (lambda t, **kw: send_message(t, reply_markup=kw.get("reply_markup")) is not None)
    clamp_stored_risk_pct(
        conn,
        send=send_fn,
        reply_keyboard_fn=lambda pct: reply_keyboard(pct, enabled=bool(ensure_settings(conn)["enabled"])),
    )
    result: dict[str, Any]
    if kind == "positions":
        body, markup = render_open_positions_message(conn, futures=futures, now_ms=now_ms)
        send_fn(body, reply_markup=markup)
        result = {"handled": True, "kind": kind, "text": body, "reply_markup": markup}
    elif kind == "anything":
        messages = render_coin_trend(conn, futures=futures, now_ms=now_ms)
        texts: list[str] = []
        for body, markup in messages:
            # reply_markup only on the idea message (last when ranking precedes it).
            send_fn(body, reply_markup=markup)
            texts.append(body)
        result = {
            "handled": True,
            "kind": kind,
            "text": "\n\n".join(texts),
            "messages": len(messages),
        }
    elif kind == "weekly":
        payload = run_assist_weekly(conn, futures=futures, now_ms_value=now_ms, send=send_fn)
        result = {"handled": True, "kind": kind, "text": payload.get("text"), "payload": payload}
    elif kind == "risk":
        balance: Decimal | None = None
        client = futures if futures is not None else build_futures_client()
        if client is not None:
            try:
                balance = client.usdt_wallet_balance()
            except Exception:  # noqa: BLE001
                balance = None
        markup = risk_choice_keyboard(balance=balance)
        msg = "ตั้งความเสี่ยงต่อไม้ — เลือก หรือ กด อื่นๆ แล้วพิมพ์ตัวเลข 0.5–3"
        send_fn(msg, reply_markup=markup)
        result = {"handled": True, "kind": kind}
    elif kind == "toggle":
        body, markup = render_toggle_confirm(conn, now_ms=now_ms)
        send_fn(body, reply_markup=markup)
        result = {"handled": True, "kind": kind, "text": body, "confirm": True}
    elif kind == "explore":
        write_settings(conn, awaiting_explore=True, awaiting_risk=None, pending_risk_pct=None)
        explore_body, explore_markup = render_explore_message(conn)
        send_fn(explore_body, reply_markup=explore_markup)
        result = {"handled": True, "kind": kind, "text": explore_body}
    elif kind == "quick":
        body, markup = render_quick_menu(conn, now_ms=now_ms)
        send_fn(body, reply_markup=markup)
        result = {"handled": True, "kind": kind, "text": body}
    else:
        return {"handled": False}
    if is_legacy_menu_label(text) and kind != "toggle":
        send_fn("เมนูอัปเดตแล้ว", reply_markup=reply_keyboard_for(ensure_settings(conn)))
        result = {**result, "menu_refreshed": True}
    return result


def toggle_enabled(conn: Any, *, now_ms_value: int | None = None) -> str:
    settings = ensure_settings(conn)
    if settings["enabled"]:
        write_settings(conn, enabled=False)
        return "⏸ ผู้ช่วยปิดแล้ว — หยุดส่ง signal ใหม่ · ยังเฝ้าไม้ที่เปิดอยู่ (เตือน stop / เลื่อน stop / ปิดไม้)"
    write_settings(conn, enabled=True)
    note = clear_pause_manual(conn, now_ms_value=now_ms_value)
    if note:
        return f"▶️ ผู้ช่วยเปิดแล้ว — {note}"
    return "▶️ ผู้ช่วยเปิดแล้ว"


def render_toggle_confirm(conn: Any, *, now_ms: int) -> tuple[str, dict[str, Any]]:
    """Read-only confirm prompt + inline keyboard for the ผู้ช่วย button."""
    settings = ensure_settings(conn)
    ts_s = int(now_ms) // 1000
    ttl = TOGGLE_CONFIRM_TTL_S
    if settings["enabled"]:
        body = (
            "ยืนยันปิดผู้ช่วย?\n"
            "• หยุดส่ง signal ใหม่\n"
            "• ยังเฝ้าไม้ที่เปิดอยู่ (เตือน stop / เลื่อน stop / ปิดไม้)\n"
            f"กดยืนยันภายใน {ttl} วิ"
        )
        action = "off"
    else:
        warnings = pause_clear_warning(conn, now_ms=now_ms)
        lines = ["ยืนยันเปิดผู้ช่วย?", "• กลับมาส่ง signal ใหม่", *warnings, f"กดยืนยันภายใน {ttl} วิ"]
        body = "\n".join(lines)
        action = "onp" if warnings else "on"
    return body, toggle_confirm_keyboard(action, ts_s)


def handle_toggle_confirm_callback(
    conn: Any,
    data: str,
    *,
    send: SendFn,
    answer: Callable[..., Any],
    edit: Callable[..., Any],
    message: dict[str, Any],
    now_ms: int,
) -> dict[str, Any]:
    """Apply or cancel an assist toggle confirm stamped into callback_data."""
    matched = _TOGGLE_CONFIRM_RE.match(data or "")
    mid = message.get("message_id") if isinstance(message, dict) else None

    def _edit(text: str) -> None:
        if mid is not None:
            edit(int(mid), text, reply_markup={"inline_keyboard": []})

    if matched is None:
        answer("ปุ่มนี้ใช้ไม่ได้แล้ว")
        return {"ok": False, "reason": "bad_data"}

    action = matched.group(1)
    ts_s = int(matched.group(2))
    now_s = int(now_ms) // 1000
    settings = ensure_settings(conn)

    if action == "no":
        answer()
        _edit("✖️ ยกเลิกแล้ว — ไม่ได้เปลี่ยนอะไร")
        status = "ผู้ช่วยยังเปิดอยู่" if settings["enabled"] else "ผู้ช่วยยังปิดอยู่"
        send(status, reply_markup=reply_keyboard_for(settings))
        return {"ok": True, "cancelled": True}

    if now_s - ts_s > TOGGLE_CONFIRM_TTL_S or ts_s > now_s + TOGGLE_CONFIRM_FUTURE_S:
        answer("หมดเวลาแล้ว — กด ผู้ช่วย ใหม่")
        _edit("⌛ หมดเวลา — ไม่ได้เปลี่ยนอะไร · กด ผู้ช่วย ใหม่")
        return {"ok": False, "reason": "expired"}

    if action == "off" and not settings["enabled"]:
        msg = "ผู้ช่วยปิดอยู่แล้ว — ไม่ได้เปลี่ยนอะไร"
        answer(msg)
        _edit(msg)
        return {"ok": False, "reason": "stale"}
    if action in ("on", "onp") and settings["enabled"]:
        msg = "ผู้ช่วยเปิดอยู่แล้ว — ไม่ได้เปลี่ยนอะไร"
        answer(msg)
        _edit(msg)
        return {"ok": False, "reason": "stale"}

    if action == "on" and is_paused(settings, now_ms=now_ms):
        msg = "มีการหยุดพักที่ยังไม่ได้แจ้ง — ไม่ได้เปลี่ยนอะไร · กด ผู้ช่วย ใหม่"
        answer(msg)
        _edit(msg)
        return {"ok": False, "reason": "stale_pause"}

    body = toggle_enabled(conn, now_ms_value=now_ms)
    enabled = bool(ensure_settings(conn)["enabled"])
    answer()
    _edit("เปิดผู้ช่วย — ✅ ยืนยันแล้ว" if enabled else "ปิดผู้ช่วย — ✅ ยืนยันแล้ว")
    send(body, reply_markup=reply_keyboard_for(ensure_settings(conn)))
    return {"ok": True, "applied": True, "text": body, "enabled": enabled}


def _signed_usdt(value: float) -> str:
    rounded = round(value, 2)
    if rounded == 0:
        return "0.00"
    if rounded > 0:
        return f"+{rounded:.2f}"
    return f"{rounded:.2f}"


def _signed_r_one(value: float) -> str:
    rounded = round(value, 1)
    if rounded == 0:
        return "0.0R"
    if rounded > 0:
        return f"+{rounded:.1f}R"
    return f"{rounded:.1f}R"


def _signed_r_two(value: float) -> str:
    rounded = round(value, 2)
    if rounded == 0:
        return "0.00R"
    if rounded > 0:
        return f"+{rounded:.2f}R"
    return f"{rounded:.2f}R"


def _price_text(value: Decimal | float, tick: Decimal | None) -> str:
    dec = value if isinstance(value, Decimal) else Decimal(str(value))
    if tick is not None and tick > 0:
        return _fmt_price_tick(dec, tick)
    return fmt_price(dec)


def _step_and_tick(client: Any, symbol: str) -> tuple[Decimal, Decimal | None]:
    try:
        filters = client.symbol_filters(symbol)
    except Exception:  # noqa: BLE001
        return Decimal("0"), None
    return filters.step_size, filters.tick_size


def _partial_stop_line(covered: Decimal, qty: Decimal) -> str:
    return f"⚠️ stop ครอบแค่ {fmt_qty(covered)}/{fmt_qty(qty)}"


def _append_position_block(lines: list[str], number: int, symbol: str, details: list[str]) -> None:
    lines.append("")
    lines.append(f"{number}. {symbol}")
    for detail in details:
        lines.append(f"• {detail}")


def quick_planned_stop(conn: Any, *, symbol: str, side: str) -> float | None:
    """1R anchor for an open Quick: tagged trade stop, else open registration stop."""
    sym = symbol.upper()
    side_l = side.lower()
    trade = conn.execute(
        """
        SELECT planned_stop FROM assist_quick_trade
        WHERE status='open' AND UPPER(symbol)=? AND LOWER(side)=?
        ORDER BY entry_time_ms DESC, id DESC LIMIT 1
        """,
        (sym, side_l),
    ).fetchone()
    if trade is not None and trade["planned_stop"] is not None:
        return float(trade["planned_stop"])
    reg = conn.execute(
        """
        SELECT planned_stop FROM assist_quick_registration
        WHERE status='open' AND UPPER(symbol)=? AND LOWER(side)=?
        ORDER BY created_at_ms DESC, id DESC LIMIT 1
        """,
        (sym, side_l),
    ).fetchone()
    if reg is not None and reg["planned_stop"] is not None:
        return float(reg["planned_stop"])
    return None


def quick_registered_stop_line(*, stop: float, tick: Decimal | None) -> str:
    return f"⚡ ไม้ quick · stop ที่ลงทะเบียน {_price_text(stop, tick)} (ฐาน 1R)"


def compute_position_unrealized(
    conn: Any,
    *,
    pos: FuturesPosition,
    orders: list[dict[str, Any]],
    now_ms: int,
    tick: Decimal | None,
) -> tuple[str, float | None, float]:
    """Return display line, R multiple (None if unknown), and risk distance used for R."""
    sym = pos.symbol
    is_quick = sym.upper() in quick_symbols(conn)
    quick_stop = quick_planned_stop(conn, symbol=sym, side=pos.side) if is_quick else None
    # Quick never borrows a main watch row or main signal for R.
    if is_quick:
        watch = None
        linked = None
    else:
        watch = get_watch_position(conn, sym, pos.position_side)
        linked = find_linkable_signal(conn, symbol=sym, side=pos.side, now_ms=now_ms)
    initial_stop: float | None
    if watch and watch.get("initial_stop") is not None:
        initial_stop = float(watch["initial_stop"])
    elif linked is not None:
        initial_stop = float(linked["stop"])
    else:
        initial_stop = None
    stop_info = find_protective_stop_info(
        orders,
        symbol=sym,
        position_side=pos.position_side,
        trade_side=pos.side,
    )
    entry = float(pos.entry_price)
    mark = float(pos.mark_price)
    qty = float(pos.qty)
    if pos.side == "long":
        unreal_usdt = (mark - entry) * qty
    else:
        unreal_usdt = (entry - mark) * qty
    r_note = ""
    risk = 0.0
    if is_quick:
        # Order: Quick planned stop (loss-side) → placed stop → R unknown.
        if quick_stop is not None:
            loss_side = (pos.side == "long" and quick_stop < entry) or (
                pos.side == "short" and quick_stop > entry
            )
            if loss_side:
                risk = abs(entry - quick_stop)
                r_note = " (จาก stop ที่ลงทะเบียน)"
        if risk <= 0 and stop_info is not None:
            placed = float(stop_info.price)
            loss_side = (pos.side == "long" and placed < entry) or (pos.side == "short" and placed > entry)
            if loss_side:
                risk = abs(entry - placed)
                r_note = " (จาก stop จริง)"
    elif initial_stop is not None:
        risk = abs(entry - initial_stop)
    elif stop_info is not None:
        placed = float(stop_info.price)
        loss_side = (pos.side == "long" and placed < entry) or (pos.side == "short" and placed > entry)
        if loss_side:
            risk = abs(entry - placed)
            r_note = " (จาก stop จริง)"
        else:
            risk = 0.0
    else:
        risk = 0.0
    if risk > 0:
        if pos.side == "long":
            r_mult = (mark - entry) / risk
        else:
            r_mult = (entry - mark) / risk
        r_s = _signed_r_one(r_mult) + r_note
    else:
        r_s = "R unknown"
        r_mult = None
    line = f"ยังไม่ปิด: {_signed_usdt(unreal_usdt)} USDT · ≈ {r_s} (ก่อนค่าธรรมเนียม)"
    return line, r_mult if risk > 0 else None, risk


def render_open_positions(
    conn: Any,
    *,
    futures: BinanceFuturesClient | None = None,
    now_ms: int,
) -> str:
    body, _ = render_open_positions_message(conn, futures=futures, now_ms=now_ms)
    return body


def render_open_positions_message(
    conn: Any,
    *,
    futures: BinanceFuturesClient | None = None,
    now_ms: int,
) -> tuple[str, dict[str, Any] | None]:
    client = futures if futures is not None else build_futures_client()
    if client is None:
        return "อ่าน futures ไม่ได้", None
    try:
        positions = client.position_risk()
        orders = client.all_open_orders()
    except Exception:  # noqa: BLE001
        return "อ่าน futures ไม่ได้", None
    if not positions:
        return "ไม่มีไม้ที่ถืออยู่", None
    quick = quick_symbols(conn)
    lines = ["📋 ไม้ที่ถืออยู่"]
    for number, pos in enumerate(positions, start=1):
        sym = pos.symbol
        step, tick = _step_and_tick(client, sym)
        coverage = protective_stop_coverage(
            orders,
            symbol=sym,
            position_side=pos.position_side,
            trade_side=pos.side,
            position_qty=pos.qty,
            step_size=step,
        )
        covered = protective_stop_covered_qty(
            orders,
            symbol=sym,
            position_side=pos.position_side,
            trade_side=pos.side,
        )
        side_l = "LONG" if pos.side == "long" else "SHORT"
        is_quick = sym.upper() in quick
        quick_stop = quick_planned_stop(conn, symbol=sym, side=pos.side) if is_quick else None
        if sym not in ASSIST_SYMBOLS and not is_quick:
            outside = [f"{side_l} · qty {fmt_qty(pos.qty)} · {_margin_text(pos)} · no data, no advice"]
            if coverage == "none":
                outside.append(_NO_STOP_LINE)
            elif coverage == "partial":
                outside.append(_partial_stop_line(covered, pos.qty))
            _append_position_block(lines, number, sym, outside)
            continue
        if sym not in ASSIST_SYMBOLS and is_quick:
            mark_s = _price_text(pos.mark_price, tick)
            outside = [
                f"{side_l} · qty {fmt_qty(pos.qty)} · {_margin_text(pos)}",
                f"เข้า {_price_text(pos.entry_price, tick)} · mark {mark_s}",
            ]
            if quick_stop is not None:
                outside.append(quick_registered_stop_line(stop=quick_stop, tick=tick))
            if coverage == "none":
                outside.append(_NO_STOP_LINE)
            elif coverage == "partial":
                outside.append(_partial_stop_line(covered, pos.qty))
            unrealized, _r_mult, _risk = compute_position_unrealized(
                conn, pos=pos, orders=orders, now_ms=now_ms, tick=tick
            )
            outside.append(unrealized)
            liq_s = (
                _price_text(pos.liquidation_price, tick)
                if pos.liquidation_price is not None and pos.liquidation_price > 0
                else None
            )
            if liq_s is not None:
                outside.append(f"liquidation {liq_s}")
            _append_position_block(lines, number, sym, outside)
            continue
        watch = get_watch_position(conn, sym, pos.position_side)
        linked = find_linkable_signal(conn, symbol=sym, side=pos.side, now_ms=now_ms)
        # Quick: keep ⚡ / R / placed stop / TP / liq; hide main 1R anchor + trail advice.
        if is_quick:
            watch = None
            linked = None
        initial_stop: float | None
        if watch and watch.get("initial_stop") is not None:
            initial_stop = float(watch["initial_stop"])
        elif linked is not None:
            initial_stop = float(linked["stop"])
        else:
            initial_stop = None
        stop_info = find_protective_stop_info(
            orders,
            symbol=sym,
            position_side=pos.position_side,
            trade_side=pos.side,
        )
        tp_info = find_take_profit_info(
            orders,
            symbol=sym,
            position_side=pos.position_side,
            trade_side=pos.side,
        )
        covered_qty = float(min(covered, pos.qty))
        mark_s = _price_text(pos.mark_price, tick)
        liq_s = (
            _price_text(pos.liquidation_price, tick)
            if pos.liquidation_price is not None and pos.liquidation_price > 0
            else None
        )
        details = [
            f"{side_l} · qty {fmt_qty(pos.qty)} · {_margin_text(pos)}",
            f"เข้า {_price_text(pos.entry_price, tick)} · mark {mark_s}",
        ]
        if quick_stop is not None:
            details.append(quick_registered_stop_line(stop=quick_stop, tick=tick))
        unrealized, _r_mult, risk = compute_position_unrealized(
            conn, pos=pos, orders=orders, now_ms=now_ms, tick=tick
        )
        entry = float(pos.entry_price)
        qty = float(pos.qty)

        if coverage == "none":
            details.append(_NO_STOP_LINE)
        elif stop_info is None:
            details.append("stop แบบ trailing")
            if coverage == "partial":
                details.append(_partial_stop_line(covered, pos.qty))
        else:
            stop_trig = trigger_label(stop_info.working_type)
            stop_px = _price_text(stop_info.price, tick)
            if stop_trig is not None:
                details.append(f"stop ที่ตั้งไว้ {stop_px} · trigger {stop_trig}")
            else:
                details.append(f"stop ที่ตั้งไว้ {stop_px}")
        details.append(unrealized)
        if stop_info is not None and coverage != "none":
            prot = float(stop_info.price)
            lock_qty = covered_qty if coverage == "partial" else qty
            if pos.side == "long":
                lock_usdt = (prot - entry) * lock_qty
            else:
                lock_usdt = (entry - prot) * lock_qty
            if risk > 0 and lock_qty > 0:
                lock_r = lock_usdt / (lock_qty * risk)
                lock_r_s = _signed_r_two(lock_r)
            else:
                lock_r_s = "R unknown"
            lock_line = f"ถ้าโดน stop: ล็อก ≈ {lock_r_s} ({_signed_usdt(lock_usdt)} USDT) (ก่อนค่าธรรมเนียม)"
            if coverage == "partial":
                uncovered = pos.qty - min(covered, pos.qty)
                lock_line = f"{lock_line} · อีก {fmt_qty(uncovered)} ไม่มี stop"
            details.append(lock_line)
            if coverage == "partial":
                details.append(_partial_stop_line(covered, pos.qty))

        if initial_stop is not None:
            details.append(f"stop ตั้งต้น {_price_text(initial_stop, tick)} (ฐาน 1R)")
            entry_time = int(watch["entry_time_ms"]) if watch and watch.get("entry_time_ms") else now_ms
            candles_1h = load_candles_in_range(conn, sym, "1h", since_ms=entry_time, until_ms=int(now_ms))
            tick_d = tick if tick is not None and tick > 0 else Decimal("0.01")
            advice = current_trail_advice(
                side=pos.side,
                entry=pos.entry_price,
                initial_stop=Decimal(str(initial_stop)),
                candles_1h=candles_1h,
                entry_time_ms=entry_time,
                now_ms=now_ms,
                tick=tick_d,
                symbol=sym,
            )
            if advice is not None:
                advice_level, advice_src = advice
                placed_stop = Decimal(str(stop_info.price)) if stop_info is not None else None
                if placed_stop is None:
                    tighter = True
                elif pos.side == "long":
                    tighter = advice_level > placed_stop
                else:
                    tighter = advice_level < placed_stop
                if tighter:
                    details.append(f"stop แนะนำตอนนี้ {_price_text(advice_level, tick)} ({advice_src})")

        if tp_info is not None:
            tp_trig = trigger_label(tp_info.working_type) or "?"
            details.append(f"TP {_price_text(tp_info.price, tick)} · trigger {tp_trig}")
            if (
                stop_info is not None
                and stop_info.working_type is not None
                and tp_info.working_type is not None
                and stop_info.working_type != tp_info.working_type
            ):
                stop_trig = trigger_label(stop_info.working_type) or stop_info.working_type
                details.append(f"⚠️ stop กับ TP คนละ trigger ({stop_trig} / {tp_trig})")

        if liq_s is not None:
            details.append(f"liquidation {liq_s}")
        _append_position_block(lines, number, sym, details)
    markup: dict[str, Any] | None = None
    if assist_execute_mode() == "live":
        rows: list[list[dict[str, str]]] = []
        for pos in positions:
            side_l = "LONG" if pos.side == "long" else "SHORT"
            side_code = "l" if pos.side == "long" else "s"
            rows.append(
                [
                    {
                        "text": f"ปิดไม้ {pos.symbol.upper()} {side_l}",
                        "callback_data": f"xc:{pos.symbol.upper()}:{side_code}",
                    }
                ]
            )
        if rows:
            markup = {"inline_keyboard": rows}
    return "\n".join(lines), markup


def _reason_line(symbol: str, reason: str) -> str:
    return f"{symbol} {reason_label(reason)}"


def _format_first_send_line(seen: dict[str, Any], *, tick_size: Decimal | None = None) -> str:
    entry = Decimal(str(seen.get("entry") or "0"))
    stop = Decimal(str(seen.get("stop") or "0"))
    qty = Decimal(str(seen.get("qty") or "0"))
    raw = seen.get("sent_at_ms")
    if not raw:
        raw = seen.get("created_at_ms")
    return format_prior_idea_line(
        label="ไอเดียที่ส่งล่าสุด",
        entry=entry,
        stop=stop,
        qty=qty,
        sent_at_ms=int(raw) if raw else None,
        tick_size=tick_size,
        with_time=True,
    )


def _render_on_demand_idea(
    conn: Any,
    cand: Any,
    *,
    now_ms: int,
    expires_at_ms: int,
    balance: Decimal,
    tick_size: Decimal | None = None,
    account_config: Any = None,
    filters: Any = None,
) -> tuple[str, bool, Any | None]:
    """Return (text, offer_entry, final_sized). final_sized is None on reason lines."""
    sized = cand.sized
    seen = latest_seen_idea(conn, symbol=sized.symbol, side=sized.side, now_ms=now_ms)
    if seen is not None:
        entry = Decimal(str(seen["entry"]))
        stop = Decimal(str(seen["stop"]))
        if chase_exceeded(side=sized.side, entry=entry, stop=stop, mark=sized.entry):
            return f"{sized.symbol} ราคาวิ่งไปแล้ว — ไม่แนะนำไล่", False, None
        first_qty = Decimal(str(seen["qty"])) if seen.get("qty") is not None else None
        first_loss = Decimal(str(seen["loss_at_stop"])) if seen.get("loss_at_stop") is not None else None
        anchor_ms = seen.get("sent_at_ms") or seen.get("created_at_ms")
        sized, cap_reason = cap_requote_qty(
            sized,
            first_qty=first_qty,
            first_loss=first_loss,
            balance=balance,
            anchor_id=str(seen["id"]) if seen.get("id") else None,
            anchor_sent_at_ms=int(anchor_ms) if anchor_ms else None,
        )
        if sized is None:
            return _reason_line(cand.sized.symbol, cap_reason or "requote_no_baseline"), False, None
        if filters is not None and below_min_notional(
            qty=sized.qty,
            notional=sized.notional,
            min_qty=filters.min_qty,
            min_notional=filters.min_notional,
        ):
            return _reason_line(sized.symbol, "below_min_notional"), False, None
        body = format_signal_message(
            sized,
            expires_at_ms=expires_at_ms,
            balance=balance,
            tick_size=tick_size,
            account_config=account_config,
        )
        return f"{body}\n{_format_first_send_line(seen, tick_size=tick_size)}", True, sized
    return (
        format_signal_message(
            sized,
            expires_at_ms=expires_at_ms,
            balance=balance,
            tick_size=tick_size,
            account_config=account_config,
        ),
        True,
        sized,
    )


def render_empty_on_demand(skipped: list[dict[str, Any]]) -> str:
    """One Thai reason line per universe symbol when nothing is admitted."""
    by_symbol = {str(row.get("symbol") or "").upper(): str(row.get("reason") or "") for row in skipped}
    lines = [_reason_line(sym, by_symbol.get(sym) or "flat") for sym in ASSIST_SYMBOLS]
    return "\n".join(lines)


def _idea_blocks_from_scan(
    conn: Any,
    result: dict[str, Any],
    *,
    client: BinanceFuturesClient,
    now_ms: int,
) -> tuple[str, dict[str, Any] | None, dict[str, str], dict[str, Decimal]]:
    """Build today's idea message + markup + idea_status + idea_loss from one scan."""
    admitted = list(result.get("admitted") or [])
    balance = Decimal(str(result.get("balance_usdt") or 0))
    expires = next_assist_run_ms(now_ms)
    configs = safe_symbol_configs(client)
    filters_by_symbol: dict[str, Any] = {}
    ticks: dict[str, Decimal] = {}
    for cand in admitted:
        sym = cand.sized.symbol
        try:
            flt = client.symbol_filters(sym)
            filters_by_symbol[sym] = flt
            ticks[sym] = flt.tick_size
        except Exception:  # noqa: BLE001
            ticks[sym] = Decimal("0")
    blocks: list[str] = []
    offered: list[tuple[str, str]] = []
    idea_status: dict[str, str] = {}
    idea_loss: dict[str, Decimal] = {}
    for cand in admitted:
        text, offer_entry, final_sized = _render_on_demand_idea(
            conn,
            cand,
            now_ms=now_ms,
            expires_at_ms=expires,
            balance=balance,
            tick_size=ticks.get(cand.sized.symbol) or None,
            account_config=configs.get(cand.sized.symbol),
            filters=filters_by_symbol.get(cand.sized.symbol),
        )
        blocks.append(text)
        sym = cand.sized.symbol
        if offer_entry:
            offered.append((sym, cand.sized.side))
            idea_status[sym] = "✅ เปิดได้ ↓"
            if final_sized is not None:
                idea_loss[sym] = final_sized.loss_at_stop
        elif text.startswith(sym):
            idea_status[sym] = text[len(sym) :].lstrip(" ").lstrip("·").strip()
        else:
            idea_status[sym] = text
    markup = None
    if execution_buttons_enabled(conn) and offered:
        markup = anything_now_keyboard(offered)
    return "\n\n".join(blocks), markup, idea_status, idea_loss


def render_coin_trend(
    conn: Any,
    *,
    futures: BinanceFuturesClient | None = None,
    now_ms: int,
) -> list[tuple[str, dict[str, Any] | None]]:
    """Ranking message plus today's idea message (display-only; one scan)."""
    settings = ensure_settings(conn)
    if not settings["enabled"]:
        return [("ผู้ช่วยปิดอยู่ · ยังเฝ้าไม้ที่เปิดอยู่", None)]
    if is_paused(settings, now_ms=now_ms):
        return [(format_paused_until(int(settings["paused_until_ms"]), now_ms=now_ms), None)]
    client = futures if futures is not None else build_futures_client()
    if client is None:
        return [("อ่าน futures ไม่ได้", None)]
    try:
        result = scan_signal_candidates(
            conn,
            futures=client,
            now_ms=now_ms,
            persist=False,
            on_demand=True,
        )
    except Exception:  # noqa: BLE001
        return [("อ่าน futures ไม่ได้", None)]

    admitted = list(result.get("admitted") or [])
    idea_text = ""
    idea_markup: dict[str, Any] | None = None
    idea_status: dict[str, str] = {}
    idea_loss: dict[str, Decimal] = {}
    if admitted:
        idea_text, idea_markup, idea_status, idea_loss = _idea_blocks_from_scan(
            conn, result, client=client, now_ms=now_ms
        )

    rows, footer = build_trend_rows(conn, result, idea_status, now_ms, idea_loss=idea_loss)
    if not rows:
        # No passing coin → today's empty on-demand output unchanged.
        return [(render_empty_on_demand(list(result.get("skipped") or [])), None)]

    ranking = fit_telegram(render_trend_ranking(rows, footer))
    if not admitted:
        return [(ranking, None)]
    return [(ranking, None), (idea_text, idea_markup)]


def render_anything_now(
    conn: Any,
    *,
    futures: BinanceFuturesClient | None = None,
    now_ms: int,
) -> tuple[str, dict[str, Any] | None]:
    """Compatibility wrapper: returns the last coin-trend message (ideas or empty)."""
    messages = render_coin_trend(conn, futures=futures, now_ms=now_ms)
    return messages[-1]


def handle_risk_callback(
    conn: Any,
    data: str,
    *,
    send: SendFn,
    now_ms: int,
) -> dict[str, Any]:
    """Inline risk:0.5|1|2|3|other or risk_yes:X / risk_no."""
    if data == "risk:other":
        write_settings(conn, awaiting_risk="number", pending_risk_pct=None)
        send("พิมพ์ความเสี่ยงเป็นตัวเลข 0.5–3")
        return {"ok": True, "awaiting": "number"}
    if data.startswith("risk:"):
        try:
            pct = float(data.split(":", 1)[1])
        except ValueError:
            return {"ok": False}
        if not math.isfinite(pct) or pct < RISK_MIN_PCT or pct > RISK_MAX_PCT:
            send("รับได้แค่ 0.5–3 — ลองใหม่หรือยกเลิกด้วยเมนู")
            return {"ok": False, "rejected": True, "reason": "range"}
        return _apply_or_confirm_risk(conn, pct, send=send, now_ms=now_ms)
    if data.startswith("risk_yes:"):
        settings = ensure_settings(conn)
        cur = float(settings["risk_pct"])

        def _reject_risk_yes(reason: str) -> dict[str, Any]:
            write_settings(conn, awaiting_risk=None, pending_risk_pct=None)
            if reason == "expired":
                send(f"⌛ หมดเวลา — ไม่ได้เปลี่ยนอะไร · ความเสี่ยงยังเป็น {cur:g}%")
            else:
                send(f"ปุ่มนี้ใช้ไม่ได้แล้ว — ความเสี่ยงยังเป็น {cur:g}%")
            return {"ok": False, "rejected": True, "reason": reason}

        matched = _RISK_YES_RE.match(data or "")
        if matched is None:
            return _reject_risk_yes("legacy")
        pct_token = matched.group(1)
        ts_s = int(matched.group(2))
        now_s = int(now_ms) // 1000
        if now_s - ts_s > TOGGLE_CONFIRM_TTL_S or ts_s > now_s + TOGGLE_CONFIRM_FUTURE_S:
            return _reject_risk_yes("expired")
        if settings.get("awaiting_risk") != "confirm":
            return _reject_risk_yes("not_pending")
        pending = settings.get("pending_risk_pct")
        if pending is None:
            return _reject_risk_yes("not_pending")
        if f"{float(pending):g}" != pct_token:
            return _reject_risk_yes("mismatch")
        return _save_risk_pct(conn, float(pending), send=send)
    if data == "risk_no":
        write_settings(conn, awaiting_risk=None, pending_risk_pct=None)
        settings = ensure_settings(conn)
        send(f"ยกเลิก — ความเสี่ยงยังเป็น {settings['risk_pct']:g}%")
        return {"ok": True, "cancelled": True}
    return {"ok": False}


def handle_risk_number(
    conn: Any,
    text: str,
    *,
    send: SendFn,
    now_ms: int,
) -> dict[str, Any]:
    """Consume a bare number while awaiting_risk='number'."""
    settings = ensure_settings(conn)
    if settings.get("awaiting_risk") != "number":
        return {"handled": False}
    raw = (text or "").strip().replace("%", "")
    try:
        pct = float(raw)
    except ValueError:
        return {"handled": False}
    if not math.isfinite(pct) or pct < RISK_MIN_PCT or pct > RISK_MAX_PCT:
        send("รับได้แค่ 0.5–3 — ลองใหม่หรือยกเลิกด้วยเมนู")
        return {"handled": True, "rejected": True}
    write_settings(conn, awaiting_risk=None)
    return {**_apply_or_confirm_risk(conn, pct, send=send, now_ms=now_ms), "handled": True}


def _save_risk_pct(conn: Any, pct: float, *, send: SendFn) -> dict[str, Any]:
    week_start_risk_pct(conn)
    write_settings(conn, risk_pct=pct, awaiting_risk=None, pending_risk_pct=None)
    cap = float(portfolio_risk_cap_pct(Decimal(str(pct))))
    send(
        f"บันทึกความเสี่ยง {pct:g}% แล้ว · เพดานความเสี่ยงรวม {cap:g}%",
        reply_markup=reply_keyboard_for(ensure_settings(conn)),
    )
    return {"ok": True, "risk_pct": pct}


def _apply_or_confirm_risk(conn: Any, pct: float, *, send: SendFn, now_ms: int) -> dict[str, Any]:
    current = float(ensure_settings(conn)["risk_pct"])
    if Decimal(str(pct)) > Decimal(str(current)):
        write_settings(conn, awaiting_risk="confirm", pending_risk_pct=pct)
        ts_s = int(now_ms) // 1000
        send(
            f"ยืนยัน {pct:g}%?\nกดยืนยันภายใน 60 วิ",
            reply_markup=risk_confirm_keyboard(pct, ts_s),
        )
        return {"ok": True, "confirm": True, "pending": pct}
    return _save_risk_pct(conn, pct, send=send)
