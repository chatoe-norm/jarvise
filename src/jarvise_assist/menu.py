"""Reply-keyboard handlers for the trade assistant."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from jarvise_assist import ASSIST_SYMBOLS
from jarvise_assist.futures import (
    BinanceFuturesClient,
    build_futures_client,
    find_protective_stop,
)
from jarvise_assist.keyboard import (
    BTN_ANYTHING,
    BTN_POSITIONS,
    BTN_RISK_PREFIX,
    BTN_TOGGLE,
    BTN_WEEKLY,
    reply_keyboard,
    risk_choice_keyboard,
    risk_confirm_keyboard,
)
from jarvise_assist.messages import format_signal_message
from jarvise_assist.pause import clear_pause_manual, format_paused_until, is_paused
from jarvise_assist.signals import scan_signal_candidates
from jarvise_assist.sizing import chase_exceeded, next_assist_run_ms
from jarvise_assist.store import (
    ensure_settings,
    find_linkable_signal,
    get_watch_position,
    latest_seen_idea,
    write_settings,
)
from jarvise_assist.weekly import run_assist_weekly
from jarvise_notify.telegram import send_message

SendFn = Callable[..., Any]

BANGKOK = timezone(timedelta(hours=7), name="Asia/Bangkok")

ON_DEMAND_REASON_TH: dict[str, str] = {
    "flat": "ไม่มีแนวโน้ม",
    "low_confidence": "ความมั่นใจต่ำ",
    "stale_analysis": "ข้อมูลยังไม่อัปเดต",
    "open_position": "ถืออยู่แล้ว",
    "risk_cap": "เต็มเพดานความเสี่ยง",
    "below_min_notional": "ไม้เล็กกว่าขั้นต่ำ",
    "margin_cap": "margin ไม่พอ",
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
    send_fn = send or (lambda t, **kw: send_message(t, reply_markup=kw.get("reply_markup")) is not None)
    if kind == "positions":
        body = render_open_positions(conn, futures=futures, now_ms=now_ms)
        send_fn(body)
        return {"handled": True, "kind": kind, "text": body}
    if kind == "anything":
        body = render_anything_now(conn, futures=futures, now_ms=now_ms)
        send_fn(body)
        return {"handled": True, "kind": kind, "text": body}
    if kind == "weekly":
        payload = run_assist_weekly(conn, futures=futures, now_ms_value=now_ms, send=send_fn)
        return {"handled": True, "kind": kind, "text": payload.get("text"), "payload": payload}
    if kind == "risk":
        markup = risk_choice_keyboard()
        msg = "ตั้งความเสี่ยงต่อไม้ — เลือก หรือ กด อื่นๆ แล้วพิมพ์ตัวเลข 0.5–15"
        send_fn(msg, reply_markup=markup)
        return {"handled": True, "kind": kind}
    if kind == "toggle":
        body = toggle_enabled(conn)
        settings = ensure_settings(conn)
        send_fn(body, reply_markup=reply_keyboard(settings["risk_pct"]))
        return {"handled": True, "kind": kind, "text": body}
    return {"handled": False}


def toggle_enabled(conn: Any) -> str:
    settings = ensure_settings(conn)
    if settings["enabled"]:
        write_settings(conn, enabled=False)
        return "⏸ ผู้ช่วยปิดแล้ว — จะไม่ส่ง signal ใหม่"
    write_settings(conn, enabled=True)
    clear_pause_manual(conn)
    return "▶️ ผู้ช่วยเปิดแล้ว"


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
        if sym not in ASSIST_SYMBOLS:
            lines.append(f"{sym} · no data, no advice")
            continue
        watch = get_watch_position(conn, sym, pos.position_side)
        linked = find_linkable_signal(conn, symbol=sym, side=pos.side, now_ms=now_ms)
        if watch and watch.get("initial_stop") is not None:
            suggested = float(watch["initial_stop"])
            src = "freeze"
        elif linked is not None:
            suggested = float(linked["stop"])
            src = "signal"
        else:
            suggested = None
            src = None
        protective = find_protective_stop(
            orders,
            symbol=sym,
            position_side=pos.position_side,
            trade_side=pos.side,
        )
        prot_s = f"{float(protective):g}" if protective is not None else "ไม่มี"
        side_l = "LONG" if pos.side == "long" else "SHORT"
        if suggested is None:
            lines.append(f"{sym} {side_l} · stop แนะนำไม่มี · ป้องกัน {prot_s}")
            continue
        risk = abs(float(pos.entry_price) - suggested)
        if risk > 0:
            if pos.side == "long":
                r_mult = (float(pos.mark_price) - float(pos.entry_price)) / risk
            else:
                r_mult = (float(pos.entry_price) - float(pos.mark_price)) / risk
            r_s = f"{r_mult:.1f}R"
        else:
            r_s = "R unknown"
        lines.append(f"{sym} {side_l} · stop แนะนำ {suggested:g} ({src}) · ป้องกัน {prot_s} · ≈ {r_s}")
    return "\n".join(lines)


def _reason_line(symbol: str, reason: str) -> str:
    label = ON_DEMAND_REASON_TH.get(reason)
    if label is None:
        return f"{symbol} {reason}"
    return f"{symbol} {label}"


def _format_sent_at_line(seen: dict[str, Any]) -> str:
    raw = seen.get("sent_at_ms")
    if raw is None:
        raw = seen.get("created_at_ms")
    when_ms = int(raw or 0)
    when = datetime.fromtimestamp(when_ms / 1000.0, tz=BANGKOK)
    return f"ส่งแล้วเมื่อ {when.strftime('%H:%M')}"


def _render_on_demand_idea(
    conn: Any,
    cand: Any,
    *,
    now_ms: int,
    expires_at_ms: int,
    balance: Decimal,
) -> str:
    sized = cand.sized
    seen = latest_seen_idea(conn, symbol=sized.symbol, side=sized.side, now_ms=now_ms)
    if seen is not None:
        entry = Decimal(str(seen["entry"]))
        stop = Decimal(str(seen["stop"]))
        if chase_exceeded(side=sized.side, entry=entry, stop=stop, mark=sized.entry):
            return f"{sized.symbol} ราคาวิ่งไปแล้ว — ไม่แนะนำไล่"
        body = format_signal_message(sized, expires_at_ms=expires_at_ms, balance=balance)
        return f"{body}\n{_format_sent_at_line(seen)}"
    return format_signal_message(sized, expires_at_ms=expires_at_ms, balance=balance)


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
) -> str:
    settings = ensure_settings(conn)
    if not settings["enabled"]:
        return "ผู้ช่วยปิดอยู่"
    if is_paused(settings, now_ms=now_ms):
        return format_paused_until(int(settings["paused_until_ms"]))
    client = futures if futures is not None else build_futures_client()
    if client is None:
        return "อ่าน futures ไม่ได้"
    try:
        result = scan_signal_candidates(
            conn,
            futures=client,
            now_ms=now_ms,
            persist=False,
            on_demand=True,
        )
    except Exception:  # noqa: BLE001
        return "อ่าน futures ไม่ได้"
    admitted = result.get("admitted") or []
    if not admitted:
        return render_empty_on_demand(list(result.get("skipped") or []))
    balance = Decimal(str(result.get("balance_usdt") or 0))
    expires = next_assist_run_ms(now_ms)
    blocks = [
        _render_on_demand_idea(
            conn,
            cand,
            now_ms=now_ms,
            expires_at_ms=expires,
            balance=balance,
        )
        for cand in admitted
    ]
    return "\n\n".join(blocks)


def handle_risk_callback(
    conn: Any,
    data: str,
    *,
    send: SendFn,
) -> dict[str, Any]:
    """Inline risk:1|2|3|5|other or risk_yes:X / risk_no."""
    if data == "risk:other":
        write_settings(conn, awaiting_risk="number", pending_risk_pct=None)
        send("พิมพ์ความเสี่ยงเป็นตัวเลข 0.5–15")
        return {"ok": True, "awaiting": "number"}
    if data.startswith("risk:"):
        try:
            pct = float(data.split(":", 1)[1])
        except ValueError:
            return {"ok": False}
        return _apply_or_confirm_risk(conn, pct, send=send)
    if data.startswith("risk_yes:"):
        try:
            pct = float(data.split(":", 1)[1])
        except ValueError:
            return {"ok": False}
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
    if pct < 0.5 or pct > 15:
        send("รับได้แค่ 0.5–15 — ลองใหม่หรือยกเลิกด้วยเมนู")
        return {"handled": True, "rejected": True}
    write_settings(conn, awaiting_risk=None)
    return {**_apply_or_confirm_risk(conn, pct, send=send), "handled": True}


def _apply_or_confirm_risk(conn: Any, pct: float, *, send: SendFn) -> dict[str, Any]:
    if pct > 5:
        write_settings(conn, awaiting_risk="confirm", pending_risk_pct=pct)
        send(f"ยืนยัน {pct:g}%?", reply_markup=risk_confirm_keyboard(pct))
        return {"ok": True, "confirm": True, "pending": pct}
    write_settings(conn, risk_pct=pct, awaiting_risk=None, pending_risk_pct=None)
    send(f"บันทึกความเสี่ยง {pct:g}% แล้ว", reply_markup=reply_keyboard(pct))
    return {"ok": True, "risk_pct": pct}
