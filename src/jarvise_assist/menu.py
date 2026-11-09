"""Reply-keyboard handlers for the trade assistant."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

from jarvise_assist import ASSIST_SYMBOLS
from jarvise_assist.execute import execution_buttons_enabled
from jarvise_assist.explore import render_explore_message
from jarvise_assist.futures import (
    BinanceFuturesClient,
    build_futures_client,
    find_protective_stop_info,
    find_take_profit_info,
    protective_stop_coverage,
    protective_stop_covered_qty,
    trigger_label,
)
from jarvise_assist.keyboard import (
    BTN_ANYTHING,
    BTN_EXPLORE,
    BTN_POSITIONS,
    BTN_RISK_PREFIX,
    BTN_TOGGLE,
    BTN_WEEKLY,
    anything_now_keyboard,
    reply_keyboard,
    risk_choice_keyboard,
    risk_confirm_keyboard,
)
from jarvise_assist.messages import (
    _fmt_price_tick,
    fmt_price,
    fmt_qty,
    format_prior_idea_line,
    format_signal_message,
    safe_symbol_configs,
)
from jarvise_assist.pause import clear_pause_manual, format_paused_until, is_paused
from jarvise_assist.signals import scan_signal_candidates
from jarvise_assist.sizing import below_min_notional, cap_requote_qty, chase_exceeded, next_assist_run_ms
from jarvise_assist.store import (
    clamp_stored_risk_pct,
    ensure_settings,
    find_linkable_signal,
    get_watch_position,
    latest_seen_idea,
    write_settings,
)
from jarvise_assist.trail_advice import current_trail_advice
from jarvise_assist.weekly import run_assist_weekly
from jarvise_ingest.db import load_candles_in_range
from jarvise_notify.telegram import send_message

SendFn = Callable[..., Any]

_NO_STOP_LINE = "⚠️ ยังไม่มี stop — ตั้ง Stop-Market (Reduce-Only, Mark) ทันที"
RISK_MIN_PCT = 0.5
RISK_MAX_PCT = 3.0
RISK_CONFIRM_ABOVE = 2.0

ON_DEMAND_REASON_TH: dict[str, str] = {
    "flat": "ไม่มีแนวโน้ม",
    "low_confidence": "ความมั่นใจต่ำ",
    "stale_analysis": "ข้อมูลยังไม่อัปเดต",
    "open_position": "ถืออยู่แล้ว",
    "risk_cap": "เต็มเพดานความเสี่ยง",
    "below_min_notional": "ไม้เล็กกว่าขั้นต่ำ",
    "margin_cap": "margin ไม่พอ",
    "size_failed": "คำนวณขนาดไม้ไม่ได้",
    "same_idea": "ส่งไปแล้วใน 24 ชม.",
    "requote_cap_zero": "จำกัดตามไอเดียแรกแล้วขนาดเหลือ 0 — ไม่ส่ง",
    "requote_no_baseline": "ไม่มีขนาดอ้างอิงจากไอเดียแรก",
}


def match_menu_label(text: str) -> str | None:
    raw = (text or "").strip()
    if raw == BTN_POSITIONS:
        return "positions"
    if raw == BTN_ANYTHING:
        return "anything"
    if raw == BTN_WEEKLY:
        return "weekly"
    if raw.startswith(BTN_RISK_PREFIX):
        return "risk"
    if raw == BTN_TOGGLE:
        return "toggle"
    if raw == BTN_EXPLORE:
        return "explore"
    return None


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
    if kind != "explore" and ensure_settings(conn).get("awaiting_explore"):
        write_settings(conn, awaiting_explore=False)
    send_fn = send or (lambda t, **kw: send_message(t, reply_markup=kw.get("reply_markup")) is not None)
    clamp_stored_risk_pct(conn, send=send_fn, reply_keyboard_fn=reply_keyboard)
    if kind == "positions":
        body = render_open_positions(conn, futures=futures, now_ms=now_ms)
        send_fn(body)
        return {"handled": True, "kind": kind, "text": body}
    if kind == "anything":
        body, markup = render_anything_now(conn, futures=futures, now_ms=now_ms)
        send_fn(body, reply_markup=markup)
        return {"handled": True, "kind": kind, "text": body}
    if kind == "weekly":
        payload = run_assist_weekly(conn, futures=futures, now_ms_value=now_ms, send=send_fn)
        return {"handled": True, "kind": kind, "text": payload.get("text"), "payload": payload}
    if kind == "risk":
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
        return {"handled": True, "kind": kind}
    if kind == "toggle":
        body = toggle_enabled(conn)
        settings = ensure_settings(conn)
        send_fn(body, reply_markup=reply_keyboard(settings["risk_pct"]))
        return {"handled": True, "kind": kind, "text": body}
    if kind == "explore":
        write_settings(conn, awaiting_explore=True, awaiting_risk=None, pending_risk_pct=None)
        explore_body, explore_markup = render_explore_message(conn)
        send_fn(explore_body, reply_markup=explore_markup)
        return {"handled": True, "kind": kind, "text": explore_body}
    return {"handled": False}


def toggle_enabled(conn: Any) -> str:
    settings = ensure_settings(conn)
    if settings["enabled"]:
        write_settings(conn, enabled=False)
        return "⏸ ผู้ช่วยปิดแล้ว — จะไม่ส่ง signal ใหม่"
    write_settings(conn, enabled=True)
    clear_pause_manual(conn)
    return "▶️ ผู้ช่วยเปิดแล้ว"


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


def render_open_positions(
    conn: Any,
    *,
    futures: BinanceFuturesClient | None = None,
    now_ms: int,
) -> str:
    client = futures if futures is not None else build_futures_client()
    if client is None:
        return "อ่าน futures ไม่ได้"
    try:
        positions = client.position_risk()
        orders = client.all_open_orders()
    except Exception:  # noqa: BLE001
        return "อ่าน futures ไม่ได้"
    if not positions:
        return "ไม่มีไม้ที่ถืออยู่"
    lines = ["📋 ไม้ที่ถืออยู่"]
    for pos in positions:
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
        if sym not in ASSIST_SYMBOLS:
            lines.append(f"{sym} {side_l} · qty {fmt_qty(pos.qty)} · no data, no advice")
            if coverage == "none":
                lines.append(_NO_STOP_LINE)
            elif coverage == "partial":
                lines.append(_partial_stop_line(covered, pos.qty))
            continue
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
        tp_info = find_take_profit_info(
            orders,
            symbol=sym,
            position_side=pos.position_side,
            trade_side=pos.side,
        )
        entry = float(pos.entry_price)
        mark = float(pos.mark_price)
        qty = float(pos.qty)
        covered_qty = float(min(covered, pos.qty))
        mark_s = _price_text(pos.mark_price, tick)
        liq_s = (
            _price_text(pos.liquidation_price, tick)
            if pos.liquidation_price is not None and pos.liquidation_price > 0
            else None
        )
        lines.append(f"{sym} {side_l}")
        lines.append(f"qty {fmt_qty(pos.qty)} · เข้า {_price_text(pos.entry_price, tick)} · mark {mark_s}")
        if pos.side == "long":
            unreal_usdt = (mark - entry) * qty
        else:
            unreal_usdt = (entry - mark) * qty
        r_note = ""
        if initial_stop is not None:
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
        lines.append(f"ยังไม่ปิด: {_signed_usdt(unreal_usdt)} USDT · ≈ {r_s} (ก่อนค่าธรรมเนียม)")

        if coverage == "none":
            lines.append(_NO_STOP_LINE)
        elif stop_info is None:
            lines.append("stop แบบ trailing")
            if coverage == "partial":
                lines.append(_partial_stop_line(covered, pos.qty))
        else:
            prot = float(stop_info.price)
            stop_trig = trigger_label(stop_info.working_type)
            stop_px = _price_text(stop_info.price, tick)
            if stop_trig is not None:
                lines.append(f"stop ที่ตั้งไว้ {stop_px} · trigger {stop_trig}")
            else:
                lines.append(f"stop ที่ตั้งไว้ {stop_px}")
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
            lines.append(lock_line)
            if coverage == "partial":
                lines.append(_partial_stop_line(covered, pos.qty))

        if initial_stop is not None:
            lines.append(f"stop ตั้งต้น {_price_text(initial_stop, tick)} (ฐาน 1R)")
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
                    lines.append(f"stop แนะนำตอนนี้ {_price_text(advice_level, tick)} ({advice_src})")

        if tp_info is not None:
            tp_trig = trigger_label(tp_info.working_type) or "?"
            lines.append(f"TP {_price_text(tp_info.price, tick)} · trigger {tp_trig}")
            if (
                stop_info is not None
                and stop_info.working_type is not None
                and tp_info.working_type is not None
                and stop_info.working_type != tp_info.working_type
            ):
                stop_trig = trigger_label(stop_info.working_type) or stop_info.working_type
                lines.append(f"⚠️ stop กับ TP คนละ trigger ({stop_trig} / {tp_trig})")

        if liq_s is not None:
            lines.append(f"liquidation {liq_s}")
    return "\n".join(lines)


def _reason_line(symbol: str, reason: str) -> str:
    label = ON_DEMAND_REASON_TH.get(reason)
    if label is None and reason.startswith("mark:"):
        label = "อ่านราคาไม่ได้"
    if label is None:
        return f"{symbol} {reason}"
    return f"{symbol} {label}"


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
) -> tuple[str, bool]:
    """Return (text, offer_entry). offer_entry is False for reason lines."""
    sized = cand.sized
    seen = latest_seen_idea(conn, symbol=sized.symbol, side=sized.side, now_ms=now_ms)
    if seen is not None:
        entry = Decimal(str(seen["entry"]))
        stop = Decimal(str(seen["stop"]))
        if chase_exceeded(side=sized.side, entry=entry, stop=stop, mark=sized.entry):
            return f"{sized.symbol} ราคาวิ่งไปแล้ว — ไม่แนะนำไล่", False
        first_qty = Decimal(str(seen["qty"])) if seen.get("qty") is not None else None
        first_loss = Decimal(str(seen["loss_at_stop"])) if seen.get("loss_at_stop") is not None else None
        sized, cap_reason = cap_requote_qty(
            sized,
            first_qty=first_qty,
            first_loss=first_loss,
            balance=balance,
        )
        if sized is None:
            return _reason_line(cand.sized.symbol, cap_reason or "requote_no_baseline"), False
        if filters is not None and below_min_notional(
            qty=sized.qty,
            notional=sized.notional,
            min_qty=filters.min_qty,
            min_notional=filters.min_notional,
        ):
            return _reason_line(sized.symbol, "below_min_notional"), False
        body = format_signal_message(
            sized,
            expires_at_ms=expires_at_ms,
            balance=balance,
            tick_size=tick_size,
            account_config=account_config,
        )
        return f"{body}\n{_format_first_send_line(seen, tick_size=tick_size)}", True
    return (
        format_signal_message(
            sized,
            expires_at_ms=expires_at_ms,
            balance=balance,
            tick_size=tick_size,
            account_config=account_config,
        ),
        True,
    )


def render_empty_on_demand(skipped: list[dict[str, Any]]) -> str:
    """One Thai reason line per universe symbol when nothing is admitted."""
    by_symbol = {str(row.get("symbol") or "").upper(): str(row.get("reason") or "") for row in skipped}
    lines = [_reason_line(sym, by_symbol.get(sym) or "flat") for sym in ASSIST_SYMBOLS]
    return "\n".join(lines)


def render_anything_now(
    conn: Any,
    *,
    futures: BinanceFuturesClient | None = None,
    now_ms: int,
) -> tuple[str, dict[str, Any] | None]:
    settings = ensure_settings(conn)
    if not settings["enabled"]:
        return "ผู้ช่วยปิดอยู่", None
    if is_paused(settings, now_ms=now_ms):
        return format_paused_until(int(settings["paused_until_ms"])), None
    client = futures if futures is not None else build_futures_client()
    if client is None:
        return "อ่าน futures ไม่ได้", None
    try:
        result = scan_signal_candidates(
            conn,
            futures=client,
            now_ms=now_ms,
            persist=False,
            on_demand=True,
        )
    except Exception:  # noqa: BLE001
        return "อ่าน futures ไม่ได้", None
    admitted = result.get("admitted") or []
    if not admitted:
        return render_empty_on_demand(list(result.get("skipped") or [])), None
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
    for cand in admitted:
        text, offer_entry = _render_on_demand_idea(
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
        if offer_entry:
            offered.append((cand.sized.symbol, cand.sized.side))
    markup = None
    if execution_buttons_enabled(conn) and offered:
        markup = anything_now_keyboard(offered)
    return "\n\n".join(blocks), markup


def handle_risk_callback(
    conn: Any,
    data: str,
    *,
    send: SendFn,
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
        if pct < RISK_MIN_PCT or pct > RISK_MAX_PCT:
            send("รับได้แค่ 0.5–3 — ลองใหม่หรือยกเลิกด้วยเมนู")
            return {"ok": False, "rejected": True}
        return _apply_or_confirm_risk(conn, pct, send=send)
    if data.startswith("risk_yes:"):
        try:
            pct = float(data.split(":", 1)[1])
        except ValueError:
            return {"ok": False}
        if pct < RISK_MIN_PCT or pct > RISK_MAX_PCT:
            send("รับได้แค่ 0.5–3 — ลองใหม่หรือยกเลิกด้วยเมนู")
            write_settings(conn, awaiting_risk=None, pending_risk_pct=None)
            return {"ok": False, "rejected": True}
        write_settings(conn, risk_pct=pct, awaiting_risk=None, pending_risk_pct=None)
        send(f"บันทึกความเสี่ยง {pct:g}% แล้ว", reply_markup=reply_keyboard(pct))
        return {"ok": True, "risk_pct": pct}
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
    if pct < RISK_MIN_PCT or pct > RISK_MAX_PCT:
        send("รับได้แค่ 0.5–3 — ลองใหม่หรือยกเลิกด้วยเมนู")
        return {"handled": True, "rejected": True}
    write_settings(conn, awaiting_risk=None)
    return {**_apply_or_confirm_risk(conn, pct, send=send), "handled": True}


def _apply_or_confirm_risk(conn: Any, pct: float, *, send: SendFn) -> dict[str, Any]:
    if pct > RISK_CONFIRM_ABOVE:
        write_settings(conn, awaiting_risk="confirm", pending_risk_pct=pct)
        send(f"ยืนยัน {pct:g}%?", reply_markup=risk_confirm_keyboard(pct))
        return {"ok": True, "confirm": True, "pending": pct}
    write_settings(conn, risk_pct=pct, awaiting_risk=None, pending_risk_pct=None)
    send(f"บันทึกความเสี่ยง {pct:g}% แล้ว", reply_markup=reply_keyboard(pct))
    return {"ok": True, "risk_pct": pct}
