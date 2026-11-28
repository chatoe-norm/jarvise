"""M1a-1: Quick menu stepper, stored cards, draft text (no orders)."""

from __future__ import annotations

import logging
import re
import secrets
from collections.abc import Callable
from datetime import timedelta, timezone
from decimal import Decimal, Overflow
from typing import Any

from jarvise_assist import ASSIST_SYMBOLS
from jarvise_assist.explore import normalize_explore_symbol
from jarvise_assist.messages import STOP_INSTRUCTION, _fmt_price_tick, fmt_price, fmt_qty
from jarvise_assist.pause import main_week_loss_pause_until
from jarvise_assist.quick import (
    REG_TTL_MS,
    _cb,
    _expire_window_phrase,
    _fmt_pct,
    create_quick_registration,
    quick_symbol_busy_toast,
    render_quick_menu,
)
from jarvise_assist.quick_card import (
    CARD_TTL_PREFIX,
    QUICK_CARD_TTL_MS,
    REFUSAL,
    _fail,
    _hhmm,
    _refusal_week_loss,
)
from jarvise_assist.quick_config import (
    AWAITING_QUICK_TTL_MS,
    canonical_string,
    effective_quick_values,
    get_quick_config,
    stop_far_limit,
)
from jarvise_assist.quick_cutoff import cutoff_block_text, in_entry_block
from jarvise_assist.quick_errors import (
    STOP_STEP_HINT,
    e_draft_stop_pct_range,
    e_draft_stop_token,
    e_reg,
    e_stop_far,
    fmt_pct_bounded,
)
from jarvise_assist.quick_regime import Trend, quick_side_risk, quick_trend_4h
from jarvise_assist.quick_tags import quick_symbols
from jarvise_assist.store import (
    clear_awaiting_quick_input,
    ensure_settings,
    list_explore_symbols,
    set_awaiting_quick_input,
    write_settings,
)

logger = logging.getLogger(__name__)

EditFn = Callable[..., Any]
SendFn = Callable[..., Any]

QUICK_DRAFT_TTL_MS = 300_000
QUICK_CARD_MAX_AGE_MS = 1_800_000
QUICK_ISSUED_FUTURE_S = 60
STOP_PRESETS = frozenset({"0.3", "0.5", "1"})
_BANGKOK = timezone(timedelta(hours=7), name="Asia/Bangkok")

_BTN_LONG = "🟢 LONG"
_BTN_SHORT = "🔴 SHORT"
_BTN_REFRESH = "🔄 อัปเดตราคา"
_BTN_REGISTER = "⚡ เริ่มไม้ quick"
_BTN_REGISTER_OFFER = "⚡ ยืนยันขนาดที่เสนอ"
_BTN_CANCEL = "✖️ ยกเลิก"
_TREND_TH: dict[Trend, str] = {"up": "ขาขึ้น", "down": "ขาลง", "unknown": "ไม่ทราบ"}


def _stop_preset_strings(conn: Any | None = None) -> list[str]:
    """Canonical stop-preset % strings (default byte-identical to STOP_PRESETS order)."""
    if conn is None:
        return ["0.3", "0.5", "1"]
    cfg = get_quick_config(conn)
    presets = cfg.values.get("quick_stop_presets") or (Decimal("0.3"), Decimal("0.5"), Decimal("1"))
    return canonical_string("quick_stop_presets", tuple(presets)).split(",")


MSG_DRAFT_EXPIRED = "หมดเวลา — เริ่มใหม่จากเมนู ⚡ Quick"
MSG_BAD_BUTTON = "ข้อมูลปุ่มไม่ถูกต้อง — เริ่มใหม่จากเมนู ⚡ Quick"
MSG_CARD_TOO_OLD = "การ์ดเก่าเกิน 30 นาที — เริ่มใหม่จากเมนู ⚡ Quick"
MSG_ALREADY_REGISTERED = "ลงทะเบียนไปแล้ว — ดูสถานะในเมนู ⚡ Quick"
MSG_CARD_QUOTE_EXPIRED = "การ์ดหมดอายุ — กด 🔄 อัปเดตราคา ก่อนลงทะเบียน"
MSG_REGISTER_FAILED = "ลงทะเบียนไม่สำเร็จ — ยังไม่ได้ลงทะเบียน · ดูสถานะในเมนู ⚡ Quick"
MSG_SIZE_CHANGED = "ขนาดเปลี่ยนจากการ์ด — ยังไม่ได้ลงทะเบียน · กด 🔄 อัปเดตราคา"


def msg_stop_far(conn: Any | None = None) -> str:
    """Stop-far draft hint from the live ``quick_stop_far_pct`` setting (default 20)."""
    from jarvise_assist.quick_config import stop_far_limit as _limit
    from jarvise_assist.quick_config import stop_far_pct_text as _pct

    return f"stop ห่างเกิน {_pct(_limit(conn))}% — ถ้าหมายถึงเปอร์เซ็นต์ให้ใส่ % (เช่น 0.5%)"


_DRAFT_NUMERIC_TOKEN = re.compile(r"[0-9][0-9.,]*%?")


def _new_card_id(conn: Any) -> str:
    for _ in range(8):
        cid = secrets.token_hex(4)
        exists = conn.execute("SELECT 1 FROM assist_quick_card WHERE id=?", (cid,)).fetchone()
        if exists is None:
            return cid
    raise RuntimeError("could not allocate quick card id")


def cancel_quick_drafts(conn: Any, *, now_ms: int) -> None:
    conn.execute(
        """
        UPDATE assist_quick_card
        SET status='cancelled', updated_at_ms=?
        WHERE status IN ('pick_symbol', 'pick_stop')
        """,
        (int(now_ms),),
    )
    conn.commit()


def _cancel_other_drafts(conn: Any, keep_id: str, *, now_ms: int, commit: bool) -> None:
    conn.execute(
        """
        UPDATE assist_quick_card
        SET status='cancelled', updated_at_ms=?
        WHERE id != ? AND status IN ('pick_symbol', 'pick_stop')
        """,
        (int(now_ms), keep_id),
    )
    if commit:
        conn.commit()


_QUICK_REG_REFUSAL_MAP = {
    "open_registration": "quick_open",
    "open_quick_trade": "quick_open",
    "open_assist_watch": "main_position",
    "live_position": "main_position",
}


def _registration_refusal_text(reason: str, conn: Any | None = None) -> str:
    if reason == "same_side_cap":
        return "ไม่ลงทะเบียน — ฝั่งนี้เปิดอยู่ครบเพดานแล้ว (เพดานฝั่งละ 2) · ปิดหรือให้ทะเบียนหมดอายุก่อน"
    mapped = _QUICK_REG_REFUSAL_MAP.get(str(reason), str(reason))
    if mapped == "stop_far" or str(reason) == "stop_far":
        from jarvise_assist.quick_card import stop_far_refusal_text

        return stop_far_refusal_text(conn)
    if mapped in REFUSAL:
        return REFUSAL[mapped]
    if str(reason) in REFUSAL:
        return REFUSAL[str(reason)]
    return e_reg(str(reason))


def create_quick_registration_from_card(
    conn: Any,
    *,
    symbol: str,
    side: str,
    card: dict[str, Any],
    now_ms: int,
    commit: bool = True,
) -> tuple[str | None, str | None]:
    from jarvise_assist.quick_cap import count_open_same_side, same_side_cap_reached

    if same_side_cap_reached(count_open_same_side(conn, side)):
        return None, "same_side_cap"
    return create_quick_registration(
        conn,
        symbol=symbol,
        side=side,
        planned_entry=float(card["entry"]),
        planned_stop=float(card["stop"]),
        qty=float(card["qty"]),
        now_ms=now_ms,
        live_positions=list(card.get("positions") or []),
        commit=commit,
    )


QUOTE_CLOCK_SKEW_MS = 60_000


def _quote_is_expired(quoted_at_ms: int, now_ms: int) -> bool:
    qa = int(quoted_at_ms or 0)
    if qa <= 0:
        return True
    age = int(now_ms) - qa
    if age < -QUOTE_CLOCK_SKEW_MS:
        return True
    return age > QUICK_CARD_TTL_MS


def _card_text_after_registration(card_text: str) -> str:
    lines = [ln for ln in card_text.split("\n") if not ln.startswith(CARD_TTL_PREFIX)]
    lines.append("✅ ลงทะเบียนแล้ว")
    return "\n".join(lines)


def _load_card(conn: Any, card_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM assist_quick_card WHERE id=?", (card_id,)).fetchone()
    return dict(row) if row is not None else None


def _recent_quick_symbols(conn: Any, limit: int = 6) -> list[str]:
    rows = conn.execute(
        """
        SELECT symbol FROM assist_quick_registration
        GROUP BY symbol
        ORDER BY MAX(created_at_ms) DESC
        """
    ).fetchall()
    out: list[str] = []
    for row in rows:
        sym = str(row[0]).upper()
        if sym and sym not in out:
            out.append(sym)
        if len(out) >= limit:
            break
    for fallback in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        if fallback not in out:
            out.append(fallback)
        if len(out) >= limit:
            break
    return out[:limit]


def _side_label(side: str) -> str:
    return "LONG" if str(side).lower() == "long" else "SHORT"


def _symbol_buttons(card_id: str, symbols: list[str]) -> list[list[dict[str, str]]]:
    rows: list[list[dict[str, str]]] = []
    row: list[dict[str, str]] = []
    for sym in symbols:
        cb = f"qk:sy:{card_id}:{sym}"
        if len(cb.encode("utf-8")) > 64:
            continue
        base = sym[:-4] if sym.endswith("USDT") else sym
        row.append({"text": base, "callback_data": cb})
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": _BTN_CANCEL, "callback_data": f"qk:x:{card_id}"}])
    return rows


def _stop_step_markup(card_id: str, presets: list[str] | None = None) -> dict[str, Any]:
    pcts = list(presets) if presets is not None else ["0.3", "0.5", "1"]
    buttons = [
        {"text": f"{p}%", "callback_data": _cb(f"qk:sl:{card_id}:{p}")}
        for p in pcts
        if len(f"qk:sl:{card_id}:{p}".encode()) <= 64
    ]
    return {
        "inline_keyboard": [
            buttons,
            [{"text": _BTN_CANCEL, "callback_data": _cb(f"qk:x:{card_id}")}],
        ]
    }


def _stop_step_markup_for(conn: Any, card_id: str) -> dict[str, Any]:
    return _stop_step_markup(card_id, _stop_preset_strings(conn))


def _refresh_only_markup(card_id: str) -> dict[str, Any]:
    rf = f"qk:rf:{card_id}"
    xb = f"qk:x:{card_id}"
    return {
        "inline_keyboard": [
            [{"text": _BTN_REFRESH, "callback_data": rf}, {"text": _BTN_CANCEL, "callback_data": xb}]
        ]
    }


def _quoted_markup(card_id: str) -> dict[str, Any]:
    return _quoted_markup_for(card_id, "risk")


def _quoted_markup_for(
    card_id: str,
    size_mode: str,
    *,
    show_bot: bool = False,
) -> dict[str, Any]:
    rg = f"qk:rg:{card_id}"
    rf = f"qk:rf:{card_id}"
    xb = f"qk:x:{card_id}"
    ex = f"qk:ex:{card_id}"
    for cb in (rg, rf, xb, ex):
        if len(cb.encode("utf-8")) > 64:
            raise ValueError(f"callback_data exceeds 64 bytes: {cb!r}")
    mode = str(size_mode or "risk")
    reg_label = _BTN_REGISTER_OFFER if mode in {"min_lot", "fit_margin"} else _BTN_REGISTER
    rows: list[list[dict[str, str]]] = []
    if show_bot and mode == "risk":
        from jarvise_assist.quick_execute import BTN_QUICK_BOT_ENTER

        rows.append([{"text": BTN_QUICK_BOT_ENTER, "callback_data": ex}])
    rows.append([{"text": reg_label, "callback_data": rg}])
    rows.append([{"text": _BTN_REFRESH, "callback_data": rf}, {"text": _BTN_CANCEL, "callback_data": xb}])
    return {"inline_keyboard": rows}


def _quoted_markup_for_card(conn: Any, card_id: str, card: dict[str, Any]) -> dict[str, Any]:
    """Build quoted markup; bot button only when flag on + eligible (D4/universe)."""
    from jarvise_assist.quick_execute import quick_execute_enabled

    size_mode = str(card.get("size_mode") or "risk")
    show_bot = bool(quick_execute_enabled() and size_mode == "risk" and card.get("bot_eligible") is True)
    return _quoted_markup_for(card_id, size_mode, show_bot=show_bot)


def _mode_tag() -> str:
    from jarvise_assist.quick_card import quick_mode_tag

    return quick_mode_tag()


def _decorate_card_text(body: str) -> str:
    tag = _mode_tag()
    lines = body.split("\n")
    if not lines:
        return tag
    if len(lines) > 1 and lines[1].strip() == tag:
        return body
    return "\n".join([lines[0], tag, *lines[1:]])


def _persist_quote(
    conn: Any,
    card_id: str,
    card: dict[str, Any],
    *,
    stop_kind: str,
    stop_value: str,
    now_ms: int,
) -> None:
    conn.execute(
        """
        UPDATE assist_quick_card SET
            status='quoted',
            stop_kind=?,
            stop_value=?,
            quoted_at_ms=?,
            quote_entry=?,
            quote_stop=?,
            quote_qty=?,
            size_mode=?,
            updated_at_ms=?
        WHERE id=? AND status IN ('pick_stop', 'quoted')
        """,
        (
            stop_kind,
            stop_value,
            int(now_ms),
            float(card["entry"]),
            float(card["stop"]),
            float(card["qty"]),
            str(card.get("size_mode") or "risk"),
            int(now_ms),
            card_id,
        ),
    )
    conn.commit()


def build_card_for_row(
    conn: Any,
    futures: Any,
    row: dict[str, Any],
    *,
    now_ms: int,
    enforce_stop_far: bool = False,
) -> dict[str, Any]:
    from jarvise_assist.quick_card import build_quick_card

    sym = str(row.get("symbol") or "")
    side = str(row["side"])
    stop_kind = row.get("stop_kind")
    stop_value = row.get("stop_value")
    kw: dict[str, Any] = {"symbol": sym, "side": side, "now_ms": now_ms}
    if stop_kind == "pct" and stop_value:
        kw["stop_pct"] = Decimal(str(stop_value))
    elif stop_kind == "price" and stop_value:
        kw["stop"] = Decimal(str(stop_value))
    else:
        return _fail("bad_data")
    kw["allow_offer"] = True
    kw["enforce_stop_far"] = enforce_stop_far
    return build_quick_card(conn, futures, **kw)


def build_card_at_quoted_stop(
    conn: Any,
    futures: Any,
    row: dict[str, Any],
    *,
    now_ms: int,
) -> dict[str, Any]:
    from jarvise_assist.quick_card import build_quick_card

    sym = str(row.get("symbol") or "")
    side = str(row["side"])
    qs = row.get("quote_stop")
    if qs is not None:
        return build_quick_card(
            conn,
            futures,
            symbol=sym,
            side=side,
            stop=Decimal(str(qs)),
            now_ms=now_ms,
            allow_offer=True,
        )
    return build_card_for_row(conn, futures, row, now_ms=now_ms)


def _terminal_status_answer(status: str) -> str:
    if status == "registered":
        return MSG_ALREADY_REGISTERED
    return MSG_BAD_BUTTON


def _expire_draft_row_if_needed(conn: Any, row: dict[str, Any], *, now_ms: int) -> dict[str, Any] | None:
    if row["status"] not in {"pick_symbol", "pick_stop"}:
        return row
    if int(now_ms) - int(row["updated_at_ms"]) <= QUICK_DRAFT_TTL_MS:
        return row
    conn.execute(
        "UPDATE assist_quick_card SET status='expired', updated_at_ms=? WHERE id=? AND status=?",
        (int(now_ms), row["id"], row["status"]),
    )
    conn.commit()
    return None


def has_active_quick_draft(conn: Any, *, now_ms: int) -> bool:
    return _active_draft(conn, now_ms=now_ms) is not None


def registration_success_message(
    *,
    sym: str,
    side: str,
    entry: Decimal,
    stop: Decimal,
    qty: Decimal,
    tick: Decimal,
    now_ms: int,
) -> str:
    expires = int(now_ms) + REG_TTL_MS
    window = _expire_window_phrase(REG_TTL_MS)
    side_label = _side_label(side)
    entry_s = _fmt_price_tick(entry, tick)
    stop_s = _fmt_price_tick(stop, tick)
    qty_s = fmt_qty(qty)
    return "\n".join(
        [
            (
                f"✅ ลงทะเบียนไม้ quick {sym} {side_label} แล้ว — "
                f"เปิดเองใน Binance ภายใน {window} (ถึง {_hhmm(expires)} น.)"
            ),
            _mode_tag(),
            f"เข้า {entry_s} · stop {stop_s} · qty {qty_s}",
            f"⏳ เหลือ {window} (ถึง {_hhmm(expires)} น.) · ดูสถานะใน ⚡ Quick",
            STOP_INSTRUCTION,
            f"บอทไม่ส่งคำสั่ง · ไม้แรกฝั่ง {side_label} บน {sym} ในช่วงนี้จะนับเป็น quick",
        ]
    )


def _registered_idempotent_card_text(conn: Any, row: dict[str, Any]) -> str:
    """Replay body from the stored registration row — never the live mark."""
    rid = row.get("registration_id")
    if not rid:
        return _decorate_card_text("✅ ลงทะเบียนแล้ว")
    reg = conn.execute("SELECT * FROM assist_quick_registration WHERE id=?", (rid,)).fetchone()
    if reg is None:
        return _decorate_card_text("✅ ลงทะเบียนแล้ว")
    sym = str(reg["symbol"]).upper()
    side = str(reg["side"])
    entry = Decimal(str(reg["planned_entry"]))
    stop = Decimal(str(reg["planned_stop"]))
    qty = Decimal(str(reg["qty"]))
    lines = [
        f"⚡ Quick · {sym} · {_side_label(side)}",
        f"เข้า {fmt_price(entry)} · stop {fmt_price(stop)} · qty {fmt_qty(qty)}",
        "✅ ลงทะเบียนแล้ว",
    ]
    return _decorate_card_text("\n".join(lines))


def _render_registered_card_edit(
    conn: Any,
    row: dict[str, Any],
    *,
    edit: Callable[[str, dict[str, Any] | None], bool],
) -> None:
    body = _registered_idempotent_card_text(conn, row)
    try:
        edit(body, {"inline_keyboard": []})
    except Exception:  # noqa: BLE001
        logger.exception("qk:rg idempotent edit failed card_id=%s", row.get("id"))


def _handle_qk_ex(
    conn: Any,
    card_id: str,
    row: dict[str, Any],
    st: str,
    *,
    futures: Any,
    send: SendFn,
    answer: Callable[[str | None], None],
    edit: Callable[[str, dict[str, Any] | None], bool],
    now_ms: int,
) -> dict[str, Any]:
    """qk:ex — preview bot Quick entry. Zero trade-client writes."""
    from jarvise_assist.execute import assist_execute_mode
    from jarvise_assist.quick_execute import (
        any_non_jv_open_order,
        build_bot_plan,
        check_quick_and_portfolio_caps,
        format_quick_confirm_text,
        in_quick_bot_night,
        pause_blocks_bot,
        quick_execute_enabled,
        quick_stop_buffer_ok,
        same_side_blocks,
        symbol_busy_for_bot,
        symbol_in_quick_exec_universe,
        unfinished_entry_for_card,
        universe_line_for_mode,
    )
    from jarvise_assist.store import insert_exec_intent

    # Flag / execute mode off → refuse with zero trade writes.
    if not quick_execute_enabled() or assist_execute_mode() == "off":
        answer("ปิดอยู่")
        send("บอทเข้าไม้ quick ปิดอยู่")
        return {"ok": False, "reason": "off"}

    if st != "quoted":
        answer(MSG_BAD_BUTTON)
        return {"ok": False}

    quoted_at = int(row.get("quoted_at_ms") or 0)
    if _quote_is_expired(quoted_at, now_ms):
        body = _decorate_card_text("\n".join([MSG_CARD_QUOTE_EXPIRED, _mode_tag()]))
        edit(body, _refresh_only_markup(card_id))
        answer(MSG_CARD_QUOTE_EXPIRED)
        return {"ok": False, "quote_expired": True}

    if str(row.get("size_mode") or "risk") != "risk":
        answer(None)
        send("บอทเข้าไม้ได้เฉพาะขนาดตาม risk — ข้อเสนอ min-lot/fit-margin บันทึกอย่างเดียว")
        return {"ok": False, "reason": "size_mode"}

    if unfinished_entry_for_card(conn, card_id, now_ms=now_ms):
        answer(None)
        send("มีคำยืนยันเข้าไม้ของการ์ดนี้อยู่แล้ว — รอให้หมดอายุหรือกดยกเลิกก่อน")
        return {"ok": False, "reason": "double_tap"}

    sym = str(row["symbol"]).upper()
    side = str(row["side"])
    cfg = get_quick_config(conn)
    values = cfg.values
    universe = str(values.get("quick_exec_universe") or "core_explore")
    if not symbol_in_quick_exec_universe(conn, sym, universe=universe):
        answer(None)
        send(universe_line_for_mode(universe))
        return {"ok": False, "reason": "universe"}

    if pause_blocks_bot(conn, now_ms=now_ms):
        answer(None)
        send("⏸ ระบบหลักพักอยู่ — บอทเข้าไม้ quick ไม่ได้")
        return {"ok": False, "reason": "paused"}

    night_start = str(values["quick_night_start"])
    night_end = str(values["quick_night_end"])
    if in_quick_bot_night(now_ms, start=night_start, end=night_end):
        answer(None)
        send(f"🌙 ช่วงคืนของบอท ({night_start}–{night_end}) — เข้าไม้ไม่ได้")
        return {"ok": False, "reason": "night"}

    if in_entry_block(now_ms):
        send(cutoff_block_text(now_ms))
        answer(None)
        return {"ok": False, "reason": "cutoff_block"}

    busy = symbol_busy_for_bot(conn, sym)
    if busy:
        answer(None)
        send(REFUSAL.get(busy, REFUSAL.get("read_failed", "ไม่พร้อม")))
        return {"ok": False, "reason": busy}

    side_cap = int(values["quick_same_side_cap"])
    if same_side_blocks(conn, side, cap=side_cap):
        from jarvise_assist.quick_cap import count_open_same_side
        from jarvise_assist.quick_execute import quick_same_side_cap_text

        answer(None)
        send(quick_same_side_cap_text(side, count_open_same_side(conn, side), side_cap))
        return {"ok": False, "reason": "same_side_cap"}

    try:
        card = build_card_at_quoted_stop(conn, futures, row, now_ms=now_ms)
    except Exception as exc:  # noqa: BLE001
        logger.warning("qk:ex build card failed type=%s", type(exc).__name__)
        from jarvise_assist.execute import _reason_th

        text = _reason_th("fapi_read_failed:card")
        send(text)
        edit(_decorate_card_text(text), {"inline_keyboard": []})
        answer(None)
        return {"ok": False, "reason": "fapi_read_failed:card"}

    card_text = str(card.get("text") or REFUSAL.get("read_failed", ""))
    quoted_ok = bool(card.get("ok"))
    # Record-only quick_cap uses full risk. The bot plan is counter-trend sized,
    # so re-check that cap on the plan instead of refusing the card text.
    if not quoted_ok and card.get("reason") != "quick_cap":
        send(card_text)
        answer(None)
        return {"ok": False, "reason": card.get("reason")}

    if quoted_ok:
        orders = list(card.get("orders") or [])
        if not orders and futures is not None:
            try:
                orders = list(futures.all_open_orders())
            except Exception:  # noqa: BLE001
                orders = []
        positions = list(card.get("positions") or [])
        entry = Decimal(str(card["entry"]))
        stop = Decimal(str(card["stop"]))
        tick = Decimal(str(card.get("tick") or "0.0001"))
    else:
        try:
            entry = Decimal(str(row["quote_entry"]))
            stop = Decimal(str(row["quote_stop"]))
            orders = list(futures.all_open_orders())
            positions = list(futures.position_risk())
            tick = Decimal(str(futures.symbol_filters(sym).tick_size))
        except Exception:  # noqa: BLE001
            logger.exception("qk:ex quoted levels unreadable card_id=%s", card_id)
            send(card_text)
            answer(None)
            return {"ok": False, "reason": card.get("reason")}

    if any_non_jv_open_order(orders, sym):
        answer(None)
        send("มีออเดอร์ที่ไม่ใช่ jv- บนสัญลักษณ์นี้ (รวม reduce-only) — ยกเลิกก่อน")
        return {"ok": False, "reason": "non_jv_order"}

    for pos in positions:
        if str(getattr(pos, "symbol", "") or "").upper() == sym:
            answer(None)
            send(REFUSAL.get("main_position", "ถือ position อยู่แล้ว"))
            return {"ok": False, "reason": "open_position"}

    testnet_levels = False
    sizing_client = futures
    # Testnet: freeze demo mark into the preview so confirm chase is demo-to-demo
    # (card futures is mainnet read; trade client is demo). Size from that same book.
    if assist_execute_mode() == "testnet":
        from jarvise_assist.execute import _apply_pct_levels, build_trade_client

        trade_ro, _reason = build_trade_client()
        if trade_ro is None:
            answer(None)
            send("อ่านราคา demo ไม่ได้ — กด 🔄 อัปเดตราคา")
            return {"ok": False, "reason": "read_failed"}
        try:
            demo_mark = Decimal(str(trade_ro.mark_price(sym)))
        except Exception as exc:  # noqa: BLE001
            logger.warning("qk:ex demo mark failed type=%s", type(exc).__name__)
            from jarvise_assist.execute import _reason_th

            text = _reason_th("fapi_read_failed:mark")
            send(text)
            edit(_decorate_card_text(text), {"inline_keyboard": []})
            answer(None)
            return {"ok": False, "reason": "fapi_read_failed:mark"}
        if demo_mark <= 0:
            answer(None)
            send("อ่านราคา demo ไม่ได้ — กด 🔄 อัปเดตราคา")
            return {"ok": False, "reason": "read_failed"}
        # Placeholder target for % remap; build_bot_plan recomputes t1 from remapped levels.
        t1_ph = entry + (entry - stop) if side.lower() == "long" else entry - (stop - entry)
        stop, _t1_mapped = _apply_pct_levels(
            mainnet_entry=entry,
            mainnet_stop=stop,
            mainnet_target=t1_ph,
            testnet_mark=demo_mark,
            side=side,
        )
        entry = demo_mark
        testnet_levels = True
        sizing_client = trade_ro
    try:
        balance, available = sizing_client.usdt_wallet_and_available()
        balance = Decimal(str(balance))
        available = Decimal(str(available))
    except Exception as exc:  # noqa: BLE001
        logger.warning("qk:ex balance read failed type=%s", type(exc).__name__)
        from jarvise_assist.execute import _reason_th

        balance_text = _reason_th("fapi_read_failed:balance")
        send(balance_text)
        edit(_decorate_card_text(balance_text), {"inline_keyboard": []})
        answer(None)
        return {"ok": False, "reason": "fapi_read_failed:balance"}
    try:
        filters = sizing_client.symbol_filters(sym)
        step = Decimal(str(filters.step_size))
        min_qty = Decimal(str(filters.min_qty))
        min_notional = Decimal(str(filters.min_notional))
    except Exception as exc:  # noqa: BLE001
        logger.warning("qk:ex filters read failed type=%s", type(exc).__name__)
        from jarvise_assist.execute import _reason_th

        text = _reason_th("fapi_read_failed:filters")
        send(text)
        edit(_decorate_card_text(text), {"inline_keyboard": []})
        answer(None)
        return {"ok": False, "reason": "fapi_read_failed:filters"}
    if testnet_levels:
        from jarvise_assist.sizing import round_level_to_tick

        tick = Decimal(str(filters.tick_size))
        stop = round_level_to_tick(stop, tick, side=side, kind="stop")

    far = stop_far_limit(conn)
    if entry > 0 and abs(stop - entry) / entry > far / Decimal("100"):
        dist_pct = abs(stop - entry) / entry * Decimal("100")
        answer(None)
        send(e_stop_far(dist_pct, limit=far))
        return {"ok": False, "reason": "stop_far"}

    plan = build_bot_plan(
        conn,
        symbol=sym,
        side=side,
        entry=entry,
        stop=stop,
        balance=balance,
        available=available,
        step=step,
        tick=tick,
        min_qty=min_qty,
        min_notional=min_notional,
        now_ms=now_ms,
        positions=positions,
        orders=orders,
    )
    if plan is None:
        answer(None)
        send("คำนวณขนาดบอทไม่ได้ — กด 🔄 อัปเดตราคา")
        return {"ok": False, "reason": "size_failed"}

    from jarvise_assist.execute import _reason_th
    from jarvise_assist.sizing import round_trip_fee_rate

    plan_stop = Decimal(str(plan["stop"]))
    buf_pct = Decimal(str(values["quick_stop_buffer_pct"]))
    buf_ticks = int(values["quick_stop_buffer_ticks"])
    if not quick_stop_buffer_ok(
        side=side,
        mark=entry,
        stop=plan_stop,
        tick=tick,
        buffer_pct=buf_pct,
        buffer_ticks=buf_ticks,
    ):
        answer(None)
        send(_reason_th("stop_buffer"))
        return {"ok": False, "reason": "stop_buffer"}
    fee_bps = Decimal(str(values["quick_fee_bps_per_side"]))
    max_fee_r = Decimal(str(values["quick_max_fee_r"]))
    r_dist = abs(entry - plan_stop)
    if r_dist <= 0:
        answer(None)
        send(_reason_th("stop_wrong_side"))
        return {"ok": False, "reason": "stop_wrong_side"}
    fee_r = (round_trip_fee_rate(fee_bps) * entry) / r_dist
    if fee_r > max_fee_r:
        answer(None)
        send(_reason_th("fee_r"))
        return {"ok": False, "reason": "fee_r"}
    cap_hit = check_quick_and_portfolio_caps(
        conn,
        balance=balance,
        new_risk=Decimal(str(plan["loss_at_stop"])),
        positions=positions,
        orders=orders,
        now_ms=now_ms,
        cap_pct=Decimal(str(values["quick_risk_cap_pct"])),
    )
    if cap_hit:
        answer(None)
        send(_reason_th(cap_hit))
        return {"ok": False, "reason": cap_hit}

    ttl_s = int(values["quick_confirm_ttl_s"])
    preview = {
        "origin": "quick",
        "cohort": "quick",
        "card_id": card_id,
        "symbol": sym,
        "side": side,
        "qty": str(plan["qty"]),
        "entry": str(plan["entry"]),
        "stop": str(plan["stop"]),
        "t1": str(plan["t1"]),
        "target": str(plan["t1"]),  # run_entry_sequence reads target; Quick uses t1
        "tp_half_qty": str(plan["tp_half_qty"]) if plan["tp_half_qty"] is not None else None,
        "tp_refuse": plan.get("tp_refuse"),
        "leverage": int(plan["leverage"]),
        "loss_at_stop": str(plan["loss_at_stop"]),
        "loss_at_stop_incl_fees": str(plan["loss_at_stop_incl_fees"]),
        "risk_pct_used": str(plan["risk_pct_used"]),
        "counter_trend": bool(plan["counter_trend"]),
        "trend": plan["trend"],
        "combined_warn": bool(plan["combined_warn"]),
        "tick": str(tick),
        "cfg": plan["cfg"],
        "testnet_levels": testnet_levels,
    }
    intent_id = insert_exec_intent(
        conn,
        kind="entry",
        symbol=sym,
        side=side,
        preview=preview,
        created_at_ms=int(now_ms),
        expires_at_ms=int(now_ms) + int(ttl_s) * 1000,
    )
    confirm = format_quick_confirm_text(plan, symbol=sym, side=side, ttl_s=ttl_s, tick=tick)
    ey_cb = f"ey:{intent_id}"
    en_cb = f"en:{intent_id}"
    for cb in (ey_cb, en_cb):
        if len(cb.encode("utf-8")) > 64:
            answer(None)
            send("ปุ่มยืนยันยาวเกิน 64 ไบต์")
            return {"ok": False, "reason": "callback_too_long"}
    markup = {
        "inline_keyboard": [
            [{"text": "✅ ยืนยันให้บอทเข้า", "callback_data": ey_cb}],
            [{"text": "✖️ ยกเลิก", "callback_data": en_cb}],
        ]
    }
    send(confirm, reply_markup=markup)
    answer(None)
    return {"ok": True, "intent_id": intent_id, "preview": True}


def _handle_qk_register(
    conn: Any,
    card_id: str,
    row: dict[str, Any],
    st: str,
    *,
    futures: Any,
    send: SendFn,
    answer: Callable[[str | None], None],
    edit: Callable[[str, dict[str, Any] | None], bool],
    now_ms: int,
) -> dict[str, Any]:
    if st == "registered":
        rid_existing = row.get("registration_id")
        if rid_existing:
            _render_registered_card_edit(conn, row, edit=edit)
            answer(MSG_ALREADY_REGISTERED)
            return {"ok": True, "idempotent": True}
        repaired = conn.execute(
            """
            UPDATE assist_quick_card
            SET status='quoted', updated_at_ms=?
            WHERE id=? AND status='registered' AND registration_id IS NULL
            """,
            (int(now_ms), card_id),
        ).rowcount
        conn.commit()
        row = _load_card(conn, card_id) or row
        st = str(row["status"])
        if repaired == 0 and row.get("registration_id"):
            _render_registered_card_edit(conn, row, edit=edit)
            answer(MSG_ALREADY_REGISTERED)
            return {"ok": True, "idempotent": True}

    if st != "quoted":
        answer(MSG_BAD_BUTTON)
        return {"ok": False}

    quoted_at = int(row.get("quoted_at_ms") or 0)
    if _quote_is_expired(quoted_at, now_ms):
        body = _decorate_card_text("\n".join([MSG_CARD_QUOTE_EXPIRED, _mode_tag()]))
        edit(body, _refresh_only_markup(card_id))
        answer(MSG_CARD_QUOTE_EXPIRED)
        return {"ok": False, "quote_expired": True}

    if in_entry_block(now_ms):
        send(cutoff_block_text(now_ms))
        answer(None)
        return {"ok": False, "reason": "cutoff_block"}

    sym = str(row["symbol"]).upper()
    side = str(row["side"])

    try:
        card = build_card_at_quoted_stop(conn, futures, row, now_ms=now_ms)
    except Exception:  # noqa: BLE001
        logger.exception("qk:rg build card failed card_id=%s", card_id)
        answer(MSG_REGISTER_FAILED)
        return {"ok": False, "reason": "read_failed"}

    if not card.get("ok"):
        send(str(card.get("text") or REFUSAL.get("read_failed", "")))
        try:
            answer(None)
        except Exception:  # noqa: BLE001
            logger.exception("qk:rg refusal answer failed card_id=%s", card_id)
        return {"ok": False, "reason": card.get("reason")}

    stored_mode = str(row.get("size_mode") or "risk")
    rebuilt_mode = str(card.get("size_mode") or "risk")
    quote_qty = Decimal(str(row.get("quote_qty") or 0))
    rebuilt_qty = Decimal(str(card.get("qty") or 0))
    mode_mismatch = stored_mode != rebuilt_mode
    qty_mismatch = stored_mode in {"min_lot", "fit_margin"} and rebuilt_qty != quote_qty
    if mode_mismatch or qty_mismatch:
        send(MSG_SIZE_CHANGED)
        try:
            answer(None)
        except Exception:  # noqa: BLE001
            logger.exception("qk:rg size-changed answer failed card_id=%s", card_id)
        return {"ok": False, "reason": "size_changed"}

    rid: str | None = None
    try:
        claim = conn.execute(
            """
            UPDATE assist_quick_card SET status='registered', updated_at_ms=?
            WHERE id=? AND status='quoted'
            """,
            (int(now_ms), card_id),
        )
        if claim.rowcount == 0:
            conn.rollback()
            answer(MSG_ALREADY_REGISTERED)
            return {"ok": True, "idempotent": True}
        if not conn.in_transaction:
            raise RuntimeError("qk:rg expected an open transaction after claim")

        week_until = main_week_loss_pause_until(conn, now_ms=now_ms)
        if week_until is not None:
            conn.rollback()
            send(_refusal_week_loss(week_until, now_ms=now_ms))
            try:
                answer(None)
            except Exception:  # noqa: BLE001
                logger.exception("qk:rg week-loss answer failed card_id=%s", card_id)
            return {"ok": False, "reason": "week_loss_pause"}

        rid, reason = create_quick_registration_from_card(
            conn,
            symbol=sym,
            side=side,
            card=card,
            now_ms=now_ms,
            commit=False,
        )
        if rid is None:
            from jarvise_assist.quick_cap import count_open_same_side, same_side_cap_text

            side_cap_count = count_open_same_side(conn, side) if reason == "same_side_cap" else 0
            conn.rollback()
            if reason == "same_side_cap":
                send(same_side_cap_text(side, side_cap_count))
            else:
                send(_registration_refusal_text(str(reason), conn))
            try:
                answer(None)
            except Exception:  # noqa: BLE001
                logger.exception("qk:rg registration refusal answer failed card_id=%s", card_id)
            return {"ok": False, "reason": reason}

        link = conn.execute(
            """
            UPDATE assist_quick_card SET registration_id=?, updated_at_ms=?
            WHERE id=? AND status='registered' AND registration_id IS NULL
            """,
            (rid, int(now_ms), card_id),
        )
        if link.rowcount != 1:
            conn.rollback()
            answer(MSG_REGISTER_FAILED)
            return {"ok": False, "reason": "read_failed"}
        conn.commit()
    except Exception:  # noqa: BLE001
        logger.exception("qk:rg registration failed card_id=%s", card_id)
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            logger.exception("qk:rg rollback failed card_id=%s", card_id)
        try:
            answer(MSG_REGISTER_FAILED)
        except Exception:  # noqa: BLE001
            logger.exception("qk:rg answer failed card_id=%s", card_id)
        return {"ok": False, "reason": "read_failed"}
    except BaseException:
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            logger.exception("qk:rg base rollback failed card_id=%s", card_id)
        raise

    tick = Decimal(str(card.get("tick") or "0.0001"))
    try:
        edited = _card_text_after_registration(str(card["text"]))
        edit(_decorate_card_text(edited), {"inline_keyboard": []})
    except Exception:  # noqa: BLE001
        logger.exception("qk:rg post-commit edit failed card_id=%s", card_id)
    try:
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
    except Exception:  # noqa: BLE001
        logger.exception("qk:rg post-commit send failed card_id=%s", card_id)
    try:
        answer(None)
    except Exception:  # noqa: BLE001
        logger.exception("qk:rg post-commit answer failed card_id=%s", card_id)
    return {"ok": True, "registration_id": rid}


def _universe_line_and_button(
    conn: Any,
    symbol: str,
    *,
    counter_used: Decimal,
) -> tuple[str | None, dict[str, str] | None]:
    """A0b labels only — record-only Quick stays open to any valid USDT-M perp."""
    sym = str(symbol).upper()
    cfg = get_quick_config(conn)
    universe = str(cfg.values.get("quick_exec_universe") or "core_explore")
    if sym in ASSIST_SYMBOLS:
        return None, None
    explore = set(list_explore_symbols(conn))
    if sym in explore:
        return "🧪 เหรียญทดลอง", None
    counter_s = _fmt_pct(float(counter_used))
    if universe == "core":
        return "บอทเข้าไม้ (A1) ได้เฉพาะ 7 เหรียญหลัก · การ์ดนี้บันทึกอย่างเดียว", None
    if universe == "any":
        return f"🔴 ไม่ทราบเทรนด์ — บอท (A1) จะใช้ risk สวนเทรนด์ {counter_s}%", None
    # core_explore (default)
    line = "บอทเข้าไม้ (A1) ได้เฉพาะ 7 เหรียญหลัก + เหรียญทดลอง · การ์ดนี้บันทึกอย่างเดียว"
    btn = {
        "text": "🧪 เพิ่มเข้าเหรียญทดลอง",
        "callback_data": _cb(f"qk:ea:{sym}"),
    }
    return line, btn


def render_side_picker(
    conn: Any,
    symbol: str,
    *,
    now_ms: int,
    futures: Any = None,
    explore_conn: Any | None = None,
) -> tuple[str, dict[str, Any]]:
    """Build the coin→side picker message + markup."""
    sym = str(symbol).upper()
    eff = effective_quick_values(get_quick_config(conn))
    trend = quick_trend_4h(conn, sym, now_ms=now_ms, explore_conn=explore_conn)
    mark_s = "—"
    if futures is not None:
        try:
            mark_s = fmt_price(Decimal(str(futures.mark_price(sym))))
        except Exception:  # noqa: BLE001
            mark_s = "—"
    lines = [
        f"⚡ {sym} · 4h: {_TREND_TH[trend]} · mark {mark_s}",
        _mode_tag(),
    ]
    uni_line, uni_btn = _universe_line_and_button(conn, sym, counter_used=Decimal(str(eff["counter_used"])))
    if uni_line:
        lines.append(uni_line)

    long_counter, _ = quick_side_risk(trend, "long", eff)
    short_counter, _ = quick_side_risk(trend, "short", eff)
    if long_counter or short_counter:
        risk_s = _fmt_pct(float(eff["risk_used"]))
        lines.append(f"การ์ดบันทึกอย่างเดียวคิดขนาดที่ risk {risk_s}% · risk สวนเทรนด์ใช้เมื่อบอทเข้าไม้ (A1)")

    issued_s = int(now_ms) // 1000
    counter_s = _fmt_pct(float(eff["counter_used"]))

    def _side_btn(code: str, label: str, is_counter: bool) -> dict[str, str]:
        text = label
        if is_counter:
            text = f"{label} (สวนเทรนด์ · risk {counter_s}%)"
        return {"text": text, "callback_data": _cb(f"qk:sd:{code}:{sym}:{issued_s}")}

    rows: list[list[dict[str, str]]] = [
        [
            _side_btn("l", _BTN_LONG, long_counter),
            _side_btn("s", _BTN_SHORT, short_counter),
        ]
    ]
    if uni_btn is not None:
        rows.append([uni_btn])
    rows.append(
        [
            {"text": "↩️ เลือกเหรียญใหม่", "callback_data": _cb("qk:bk")},
            {"text": "✖️ ยกเลิก", "callback_data": _cb("qk:cx")},
        ]
    )
    return "\n".join(lines), {"inline_keyboard": rows}


def _edit_callback_message(
    *,
    message: dict[str, Any],
    text: str,
    markup: dict[str, Any] | None,
    send: SendFn,
    edit: EditFn,
    fallback_send: bool = True,
) -> None:
    mid = message.get("message_id")
    chat_id = (message.get("chat") or {}).get("id")
    if mid is None or chat_id is None:
        if fallback_send:
            send(text, reply_markup=markup)
        return
    try:
        if edit(
            int(mid),
            text,
            reply_markup=markup or {"inline_keyboard": []},
            chat_id=str(chat_id),
        ):
            return
    except Exception:  # noqa: BLE001
        pass
    if fallback_send:
        send(text, reply_markup=markup)


def _clear_symbol_await_only(conn: Any) -> None:
    """Clear typed-symbol await; leave cfg:<key> settings prompts alone."""
    if ensure_settings(conn).get("awaiting_quick_input") == "symbol":
        clear_awaiting_quick_input(conn)


def _clear_other_awaitings(conn: Any) -> None:
    write_settings(
        conn,
        awaiting_explore=False,
        awaiting_risk=None,
        pending_risk_pct=None,
        awaiting_quick_input=None,
        awaiting_quick_until_ms=None,
    )


def handle_quick_trade_callback(
    conn: Any,
    data: str,
    *,
    futures: Any,
    send: SendFn,
    answer: Callable[[str | None], None],
    edit: EditFn,
    message: dict[str, Any],
    now_ms: int,
    explore_lookup: Callable[[str], dict[str, Any] | None] | None = None,
    explore_fetch: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    parts = data.split(":")
    if len(parts) < 2 or parts[0] != "qk":
        answer(MSG_BAD_BUTTON)
        return {"ok": False}

    op = parts[1]

    # --- Menu-level ops (no card id) -----------------------------------------
    if op == "bz":
        if len(parts) < 3:
            answer(MSG_BAD_BUTTON)
            return {"ok": False}
        sym = normalize_explore_symbol(":".join(parts[2:]).upper())
        if sym is None:
            answer(MSG_BAD_BUTTON)
            return {"ok": False}
        answer(quick_symbol_busy_toast(sym))
        return {"ok": True, "busy": True}

    if op == "mx":
        # Close drops the keyboard in place; never re-send the whole Quick menu.
        _edit_callback_message(
            message=message,
            text=str(message.get("text") or "⚡ Quick"),
            markup={"inline_keyboard": []},
            send=send,
            edit=edit,
            fallback_send=False,
        )
        answer(None)
        return {"ok": True, "closed": True}

    if op == "cx":
        _clear_symbol_await_only(conn)
        _edit_callback_message(
            message=message,
            text="ยกเลิกแล้ว",
            markup={"inline_keyboard": []},
            send=send,
            edit=edit,
        )
        answer(None)
        return {"ok": True, "cancelled": True}

    if op == "bk":
        _clear_symbol_await_only(conn)
        text, kb = render_quick_menu(conn, now_ms=now_ms)
        # Coin grid only (no status prose re-send): keep full menu text for context.
        _edit_callback_message(message=message, text=text, markup=kb, send=send, edit=edit)
        answer(None)
        return {"ok": True, "step": "coin_menu"}

    if op == "ot":
        cancel_quick_drafts(conn, now_ms=now_ms)
        set_awaiting_quick_input(conn, "symbol", until_ms=int(now_ms) + AWAITING_QUICK_TTL_MS)
        prompt = "พิมพ์รหัสเหรียญ เช่น PEPE, WIF, 1000PEPEUSDT"
        markup = {"inline_keyboard": [[{"text": "✖️ ยกเลิก", "callback_data": _cb("qk:cx")}]]}
        _edit_callback_message(message=message, text=prompt, markup=markup, send=send, edit=edit)
        answer(None)
        return {"ok": True, "awaiting": "symbol"}

    if op == "cs":
        if len(parts) < 3:
            answer(MSG_BAD_BUTTON)
            return {"ok": False}
        sym = normalize_explore_symbol(":".join(parts[2:]).upper())
        if sym is None:
            answer(MSG_BAD_BUTTON)
            return {"ok": False}
        if sym in quick_symbols(conn):
            answer(quick_symbol_busy_toast(sym))
            return {"ok": True, "busy": True}
        _clear_symbol_await_only(conn)
        text, markup = render_side_picker(conn, sym, now_ms=now_ms, futures=futures)
        _edit_callback_message(message=message, text=text, markup=markup, send=send, edit=edit)
        answer(None)
        return {"ok": True, "step": "pick_side", "symbol": sym}

    if op == "ea":
        if len(parts) < 3:
            answer(MSG_BAD_BUTTON)
            return {"ok": False}
        sym = normalize_explore_symbol(":".join(parts[2:]).upper())
        if sym is None:
            answer(MSG_BAD_BUTTON)
            return {"ok": False}
        from jarvise_assist.explore import submit_explore_symbol
        from jarvise_ingest.providers.binance_klines import fetch_klines

        lookup = explore_lookup
        fetch = explore_fetch or fetch_klines
        if lookup is None:
            answer(MSG_BAD_BUTTON)
            return {"ok": False}
        result = submit_explore_symbol(conn, sym, lookup=lookup, fetch=fetch, now_ms_value=now_ms)
        send(str(result.get("text") or ""), reply_markup=result.get("reply_markup"))
        text, markup = render_side_picker(conn, sym, now_ms=now_ms, futures=futures)
        _edit_callback_message(message=message, text=text, markup=markup, send=send, edit=edit)
        answer(None)
        return {"ok": bool(result.get("ok")), "explore": result, "step": "pick_side", "symbol": sym}

    if op == "sd":
        side_code = parts[2] if len(parts) > 2 else ""
        if side_code not in {"l", "s"}:
            answer(MSG_BAD_BUTTON)
            return {"ok": False}
        side = "long" if side_code == "l" else "short"

        # Extended form: qk:sd:<l|s>:<SYMBOL>:<issued_s> (exactly 5 parts)
        if len(parts) == 5:
            sym = normalize_explore_symbol(parts[3].upper())
            if sym is None:
                answer(MSG_BAD_BUTTON)
                return {"ok": False}
            try:
                issued_s = int(parts[4])
            except (TypeError, ValueError):
                answer(MSG_BAD_BUTTON)
                return {"ok": False}
            now_s = int(now_ms) // 1000
            if issued_s > now_s + QUICK_ISSUED_FUTURE_S:
                answer(MSG_BAD_BUTTON)
                return {"ok": False}
            if (now_s - issued_s) * 1000 > QUICK_DRAFT_TTL_MS:
                answer(MSG_DRAFT_EXPIRED)
                return {"ok": False, "expired": True}
            if sym in quick_symbols(conn):
                answer(quick_symbol_busy_toast(sym))
                return {"ok": True, "busy": True}
            cancel_quick_drafts(conn, now_ms=now_ms)
            _clear_other_awaitings(conn)
            card_id = _new_card_id(conn)
            conn.execute(
                """
                INSERT INTO assist_quick_card (
                    id, side, symbol, status, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, 'pick_stop', ?, ?)
                """,
                (card_id, side, sym, int(now_ms), int(now_ms)),
            )
            conn.commit()
            body = "\n".join(
                [
                    f"⚡ Quick · {sym} · {_side_label(side)} — {STOP_STEP_HINT}",
                    _mode_tag(),
                ]
            )
            markup = _stop_step_markup_for(conn, card_id)
            mid = message.get("message_id")
            chat_id = (message.get("chat") or {}).get("id")

            def _edit_sd(text: str, mk: dict[str, Any] | None = None) -> bool:
                if mid is None or chat_id is None:
                    new_mid = send(text, reply_markup=mk)
                    if new_mid is not None:
                        conn.execute(
                            "UPDATE assist_quick_card SET message_id=? WHERE id=?",
                            (int(new_mid), card_id),
                        )
                        conn.commit()
                    return new_mid is not None
                try:
                    ok = edit(
                        int(mid),
                        text,
                        reply_markup=mk or {"inline_keyboard": []},
                        chat_id=str(chat_id),
                    )
                    if ok:
                        conn.execute(
                            "UPDATE assist_quick_card SET message_id=? WHERE id=?",
                            (int(mid), card_id),
                        )
                        conn.commit()
                        return True
                except Exception:  # noqa: BLE001
                    pass
                new_mid = send(text, reply_markup=mk)
                if new_mid is not None:
                    conn.execute(
                        "UPDATE assist_quick_card SET message_id=? WHERE id=?",
                        (int(new_mid), card_id),
                    )
                    conn.commit()
                return new_mid is not None

            _edit_sd(body, markup)
            answer(None)
            return {"ok": True, "step": "pick_stop", "card_id": card_id}

        if len(parts) != 3:
            answer(MSG_BAD_BUTTON)
            return {"ok": False}

        # Legacy 3-part: qk:sd:l|s → pick_symbol
        cancel_quick_drafts(conn, now_ms=now_ms)
        _clear_other_awaitings(conn)
        card_id = _new_card_id(conn)
        conn.execute(
            """
            INSERT INTO assist_quick_card (
                id, side, status, created_at_ms, updated_at_ms
            ) VALUES (?, ?, 'pick_symbol', ?, ?)
            """,
            (card_id, side, int(now_ms), int(now_ms)),
        )
        conn.commit()
        mid = send(
            "\n".join(
                [
                    f"⚡ Quick · {_side_label(side)} — เลือกเหรียญ หรือพิมพ์ชื่อ (เช่น BTC)",
                    _mode_tag(),
                ]
            ),
            reply_markup={"inline_keyboard": _symbol_buttons(card_id, _recent_quick_symbols(conn))},
        )
        if mid is not None:
            conn.execute(
                "UPDATE assist_quick_card SET message_id=? WHERE id=?",
                (int(mid), card_id),
            )
            conn.commit()
        answer(None)
        return {"ok": True, "step": "pick_symbol", "card_id": card_id}

    if len(parts) < 3:
        answer(MSG_BAD_BUTTON)
        return {"ok": False}

    card_id = parts[2] if len(parts) > 2 else ""
    row = _load_card(conn, card_id)
    if row is None:
        answer(MSG_BAD_BUTTON)
        return {"ok": False}
    fresh = _expire_draft_row_if_needed(conn, row, now_ms=now_ms)
    if fresh is None:
        answer(MSG_DRAFT_EXPIRED)
        return {"ok": False, "expired": True}
    row = fresh

    mid = message.get("message_id")
    chat_id = (message.get("chat") or {}).get("id")

    def _edit(text: str, markup: dict[str, Any] | None = None) -> bool:
        if mid is None or chat_id is None:
            new_mid = send(text, reply_markup=markup)
            if new_mid is not None:
                conn.execute(
                    "UPDATE assist_quick_card SET message_id=? WHERE id=?",
                    (int(new_mid), card_id),
                )
                conn.commit()
            return new_mid is not None
        try:
            ok = edit(
                int(mid),
                text,
                reply_markup=markup or {"inline_keyboard": []},
                chat_id=str(chat_id),
            )
            if ok:
                return True
        except Exception:  # noqa: BLE001
            pass
        new_mid = send(text, reply_markup=markup)
        if new_mid is not None:
            conn.execute(
                "UPDATE assist_quick_card SET message_id=? WHERE id=?",
                (int(new_mid), card_id),
            )
            conn.commit()
        return new_mid is not None

    st = str(row["status"])

    if op == "x":
        if st == "registered":
            answer(MSG_ALREADY_REGISTERED)
            return {"ok": True, "idempotent": True}
        if st in {"cancelled", "expired"}:
            answer(MSG_BAD_BUTTON)
            return {"ok": False}
        cur = conn.execute(
            """
            UPDATE assist_quick_card
            SET status='cancelled', updated_at_ms=?
            WHERE id=? AND status IN ('pick_symbol', 'pick_stop', 'quoted')
            """,
            (int(now_ms), card_id),
        )
        if cur.rowcount == 0:
            conn.rollback()
            answer(MSG_BAD_BUTTON)
            return {"ok": False}
        conn.commit()
        _edit("ยกเลิกแล้ว", {"inline_keyboard": []})
        answer(None)
        return {"ok": True, "cancelled": True}

    if op == "ex":
        return _handle_qk_ex(
            conn,
            card_id,
            row,
            st,
            futures=futures,
            send=send,
            answer=answer,
            edit=_edit,
            now_ms=now_ms,
        )

    if op == "rg":
        return _handle_qk_register(
            conn,
            card_id,
            row,
            st,
            futures=futures,
            send=send,
            answer=answer,
            edit=_edit,
            now_ms=now_ms,
        )

    if st in {"registered", "cancelled", "expired"}:
        answer(_terminal_status_answer(st))
        return {"ok": st == "registered", "idempotent": st == "registered"}

    if op == "sy" and len(parts) >= 4:
        if st != "pick_symbol":
            answer(MSG_BAD_BUTTON)
            return {"ok": False}
        sym = ":".join(parts[3:]).upper()
        symbol = normalize_explore_symbol(sym)
        if symbol is None:
            answer(MSG_BAD_BUTTON)
            return {"ok": False}
        cur = conn.execute(
            """
            UPDATE assist_quick_card
            SET symbol=?, status='pick_stop', updated_at_ms=?
            WHERE id=? AND status='pick_symbol'
            """,
            (symbol, int(now_ms), card_id),
        )
        if cur.rowcount == 0:
            conn.rollback()
            answer(MSG_BAD_BUTTON)
            return {"ok": False}
        conn.commit()
        side = str(row["side"])
        text = "\n".join(
            [
                f"⚡ Quick · {symbol} · {_side_label(side)} — {STOP_STEP_HINT}",
                _mode_tag(),
            ]
        )
        _edit(text, _stop_step_markup_for(conn, card_id))
        answer(None)
        return {"ok": True, "step": "pick_stop"}

    if op == "sl" and len(parts) >= 4:
        if st != "pick_stop":
            answer(MSG_BAD_BUTTON)
            return {"ok": False}
        pct_s = parts[3]
        allowed = set(_stop_preset_strings(conn))
        if pct_s not in allowed:
            answer(MSG_BAD_BUTTON)
            return {"ok": False, "stale": True}
        if in_entry_block(now_ms):
            answer(None)
            send(cutoff_block_text(now_ms), reply_markup=_stop_step_markup_for(conn, card_id))
            return {"ok": False, "reason": "cutoff_block"}
        cur = conn.execute(
            """
            UPDATE assist_quick_card
            SET stop_kind='pct', stop_value=?, updated_at_ms=?
            WHERE id=? AND status='pick_stop'
            """,
            (pct_s, int(now_ms), card_id),
        )
        if cur.rowcount == 0:
            conn.rollback()
            answer(MSG_BAD_BUTTON)
            return {"ok": False}
        conn.commit()
        row = _load_card(conn, card_id) or row
        return _finalize_quoted_card(
            conn, futures, card_id, row, send=send, edit=_edit, answer=answer, now_ms=now_ms
        )

    if op == "rf":
        if int(now_ms) - int(row["created_at_ms"]) > QUICK_CARD_MAX_AGE_MS:
            answer(MSG_CARD_TOO_OLD)
            return {"ok": False}
        if st not in {"quoted", "pick_stop"}:
            answer(MSG_BAD_BUTTON)
            return {"ok": False}
        rebuilt = build_card_for_row(conn, futures, row, now_ms=now_ms)
        if not rebuilt.get("ok") and rebuilt.get("reason") == "stop_wrong_side":
            sym = str(row.get("symbol") or "")
            mark_s = "?"
            if futures is not None and sym:
                try:
                    mark_s = fmt_price(Decimal(str(futures.mark_price(sym))))
                except Exception:  # noqa: BLE001
                    pass
            stop_v = row.get("stop_value") or "?"
            cur = conn.execute(
                """
                UPDATE assist_quick_card SET status='pick_stop', updated_at_ms=?
                WHERE id=? AND status IN ('quoted', 'pick_stop')
                """,
                (int(now_ms), card_id),
            )
            if cur.rowcount != 1:
                conn.rollback()
                answer(MSG_BAD_BUTTON)
                return {"ok": False}
            _cancel_other_drafts(conn, card_id, now_ms=now_ms, commit=False)
            conn.commit()
            text = f"stop {stop_v} อยู่ผิดฝั่งของราคาแล้ว (ตอนนี้ {mark_s}) — เลือก stop ใหม่"
            _edit("\n".join([text, _mode_tag()]), _stop_step_markup_for(conn, card_id))
            answer(None)
            return {"ok": False, "stop_wrong_side": True}
        if not rebuilt.get("ok"):
            reason = rebuilt.get("reason")
            if reason == "cutoff_block":
                send(
                    str(rebuilt.get("text") or cutoff_block_text(now_ms)),
                    reply_markup=_refresh_only_markup(card_id),
                )
                answer(None)
                return {"ok": False, "reason": "cutoff_block"}
            body = _decorate_card_text(str(rebuilt.get("text") or REFUSAL.get("read_failed", "")))
            _edit(body, _refresh_only_markup(card_id))
            answer(None)
            return {"ok": False, "reason": reason}
        _persist_quote(
            conn,
            card_id,
            rebuilt,
            stop_kind=str(row["stop_kind"] or "pct"),
            stop_value=str(row["stop_value"] or ""),
            now_ms=now_ms,
        )
        body = _decorate_card_text(str(rebuilt["text"]))
        _edit(body, _quoted_markup_for_card(conn, card_id, rebuilt))
        answer(None)
        return {"ok": True, "refreshed": True}

    answer(MSG_BAD_BUTTON)
    return {"ok": False}


def _finalize_quoted_card(
    conn: Any,
    futures: Any,
    card_id: str,
    row: dict[str, Any],
    *,
    send: SendFn,
    edit: Callable[[str, dict[str, Any] | None], bool],
    answer: Callable[[str | None], None],
    now_ms: int,
) -> dict[str, Any]:
    card = build_card_for_row(conn, futures, row, now_ms=now_ms)
    if not card.get("ok"):
        # sl/qk:sl presets only produce pct cards; price wrong-side uses qk:rf or typed draft.
        edit(_decorate_card_text(str(card.get("text") or "")), _stop_step_markup_for(conn, card_id))
        answer(None)
        return {"ok": False, "reason": card.get("reason")}
    stop_kind = str(row.get("stop_kind") or "pct")
    stop_value = str(row.get("stop_value") or "")
    _persist_quote(conn, card_id, card, stop_kind=stop_kind, stop_value=stop_value, now_ms=now_ms)
    edit(
        _decorate_card_text(str(card["text"])),
        _quoted_markup_for_card(conn, card_id, card),
    )
    answer(None)
    return {"ok": True, "quoted": True}


def _active_draft(conn: Any, *, now_ms: int) -> dict[str, Any] | None:
    rows = conn.execute(
        """
        SELECT * FROM assist_quick_card
        WHERE status IN ('pick_symbol', 'pick_stop')
        ORDER BY updated_at_ms DESC
        """
    ).fetchall()
    alive = [dict(r) for r in rows if int(now_ms) - int(r["updated_at_ms"]) <= QUICK_DRAFT_TTL_MS]
    if len(alive) != 1:
        return None
    return alive[0]


def _edit_draft_message(
    conn: Any,
    *,
    card_id: str,
    message_id: int | None,
    text: str,
    markup: dict[str, Any] | None,
    send: SendFn,
    edit: EditFn,
) -> None:
    if message_id is not None:
        try:
            if edit(int(message_id), text, reply_markup=markup):
                return
        except Exception:  # noqa: BLE001
            pass
    new_mid = send(text, reply_markup=markup)
    if new_mid is not None:
        conn.execute(
            "UPDATE assist_quick_card SET message_id=? WHERE id=?",
            (int(new_mid), card_id),
        )
        conn.commit()


def handle_quick_draft_text(
    conn: Any,
    text: str,
    *,
    futures: Any,
    send: SendFn,
    edit: EditFn,
    now_ms: int,
) -> dict[str, Any] | None:
    draft = _active_draft(conn, now_ms=now_ms)
    if draft is None:
        return None
    card_id = str(draft["id"])
    token = text.strip()
    if draft["status"] == "pick_symbol":
        if not re.fullmatch(r"(?=.*[A-Za-z])[A-Za-z0-9]{1,20}", token):
            return None
        sym = token.upper()
        if not sym.endswith("USDT"):
            sym = f"{sym}USDT"
        symbol = normalize_explore_symbol(sym)
        if symbol is None:
            return None
        cur = conn.execute(
            """
            UPDATE assist_quick_card
            SET symbol=?, status='pick_stop', updated_at_ms=?
            WHERE id=? AND status='pick_symbol'
            """,
            (symbol, int(now_ms), card_id),
        )
        if cur.rowcount == 0:
            conn.rollback()
            return None
        conn.commit()
        side = str(draft["side"])
        body = "\n".join(
            [
                f"⚡ Quick · {symbol} · {_side_label(side)} — {STOP_STEP_HINT}",
                _mode_tag(),
            ]
        )
        mid = draft.get("message_id")
        _edit_draft_message(
            conn,
            card_id=card_id,
            message_id=int(mid) if mid is not None else None,
            text=body,
            markup=_stop_step_markup_for(conn, card_id),
            send=send,
            edit=edit,
        )
        return {"handled": True, "step": "pick_stop"}

    if draft["status"] == "pick_stop":
        sym = str(draft["symbol"])
        pct_m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*%", token, re.I)
        if pct_m:
            try:
                pct = Decimal(pct_m.group(1))
            except Exception:  # noqa: BLE001
                return None
            far_limit = stop_far_limit(conn)
            if pct <= 0 or pct >= far_limit:
                side = str(draft["side"])
                sym0 = str(draft.get("symbol") or sym)
                hint = "\n".join(
                    [
                        e_draft_stop_pct_range(fmt_pct_bounded(pct), limit=far_limit),
                        f"⚡ Quick · {sym0} · {_side_label(side)} — {STOP_STEP_HINT}",
                        _mode_tag(),
                    ]
                )
                _edit_draft_message(
                    conn,
                    card_id=card_id,
                    message_id=int(draft["message_id"]) if draft.get("message_id") else None,
                    text=hint,
                    markup=_stop_step_markup_for(conn, card_id),
                    send=send,
                    edit=edit,
                )
                return {"handled": True, "rejected": True}
            if in_entry_block(now_ms):
                send(cutoff_block_text(now_ms), reply_markup=_stop_step_markup_for(conn, card_id))
                return {"handled": True, "rejected": True}
            cur = conn.execute(
                """
                UPDATE assist_quick_card
                SET stop_kind='pct', stop_value=?, updated_at_ms=?
                WHERE id=? AND status='pick_stop'
                """,
                (str(pct), int(now_ms), card_id),
            )
            if cur.rowcount == 0:
                conn.rollback()
                return None
            conn.commit()
            row = _load_card(conn, card_id) or draft
            card = build_card_for_row(conn, futures, row, now_ms=now_ms)
            if not card.get("ok"):
                _edit_draft_message(
                    conn,
                    card_id=card_id,
                    message_id=int(draft["message_id"]) if draft.get("message_id") else None,
                    text=_decorate_card_text(str(card.get("text") or "")),
                    markup=_stop_step_markup_for(conn, card_id),
                    send=send,
                    edit=edit,
                )
                return {"handled": True, "ok": False}
            _persist_quote(conn, card_id, card, stop_kind="pct", stop_value=str(pct), now_ms=now_ms)
            _edit_draft_message(
                conn,
                card_id=card_id,
                message_id=int(draft["message_id"]) if draft.get("message_id") else None,
                text=_decorate_card_text(str(card["text"])),
                markup=_quoted_markup_for_card(conn, card_id, card),
                send=send,
                edit=edit,
            )
            return {"handled": True, "quoted": True}
        price: Decimal | None
        try:
            price = Decimal(token.replace(",", ""))
        except Exception:  # noqa: BLE001
            price = None
        numeric_token = _DRAFT_NUMERIC_TOKEN.fullmatch(token) is not None
        if price is None or not price.is_finite() or price <= 0:
            if numeric_token:
                send(e_draft_stop_token(token))
                return {"handled": True, "rejected": True}
            return None
        if in_entry_block(now_ms):
            send(cutoff_block_text(now_ms), reply_markup=_stop_step_markup_for(conn, card_id))
            return {"handled": True, "rejected": True}
        far_pct: Decimal | None = None
        far_limit = stop_far_limit(conn)
        if futures is not None:
            try:
                mark = Decimal(str(futures.mark_price(sym)))
                if mark > 0:
                    try:
                        ratio = abs(price - mark) / mark
                        dist_pct = ratio * Decimal("100")
                    except Overflow:
                        ratio = dist_pct = Decimal("Infinity")
                    if ratio > far_limit / Decimal("100") and not token.endswith("%"):
                        far_pct = dist_pct
            except Exception:  # noqa: BLE001
                pass
        if far_pct is not None:
            send(e_stop_far(far_pct, limit=far_limit))
            return {"handled": True, "rejected": True}
        cur = conn.execute(
            """
            UPDATE assist_quick_card
            SET stop_kind='price', stop_value=?, updated_at_ms=?
            WHERE id=? AND status='pick_stop'
            """,
            (str(price), int(now_ms), card_id),
        )
        if cur.rowcount == 0:
            conn.rollback()
            return None
        conn.commit()
        row = _load_card(conn, card_id) or draft
        card = build_card_for_row(conn, futures, row, now_ms=now_ms)
        if not card.get("ok"):
            _edit_draft_message(
                conn,
                card_id=card_id,
                message_id=int(draft["message_id"]) if draft.get("message_id") else None,
                text=_decorate_card_text(str(card.get("text") or "")),
                markup=_stop_step_markup_for(conn, card_id),
                send=send,
                edit=edit,
            )
            return {"handled": True, "ok": False}
        _persist_quote(conn, card_id, card, stop_kind="price", stop_value=str(price), now_ms=now_ms)
        _edit_draft_message(
            conn,
            card_id=card_id,
            message_id=int(draft["message_id"]) if draft.get("message_id") else None,
            text=_decorate_card_text(str(card["text"])),
            markup=_quoted_markup_for_card(conn, card_id, card),
            send=send,
            edit=edit,
        )
        return {"handled": True, "quoted": True}
    return None


def create_card_from_typed_command(
    conn: Any,
    futures: Any,
    parsed: dict[str, Any],
    *,
    now_ms: int,
) -> tuple[str | None, dict[str, Any]]:
    """Store assist_quick_card row for typed q command; returns (card_id, card)."""
    side = str(parsed["side"])
    sym = str(parsed["symbol"]).upper()
    if parsed.get("stop_pct") is not None:
        stop_kind, stop_value = "pct", str(parsed["stop_pct"])
    else:
        stop_kind, stop_value = "price", str(parsed.get("stop"))
    row_stub = {
        "symbol": sym,
        "side": side,
        "stop_kind": stop_kind,
        "stop_value": stop_value,
    }
    card = build_card_for_row(conn, futures, row_stub, now_ms=now_ms, enforce_stop_far=True)
    if not card.get("ok"):
        return None, card
    card_id = _new_card_id(conn)
    conn.execute(
        """
        INSERT INTO assist_quick_card (
            id, side, symbol, stop_kind, stop_value, status,
            quoted_at_ms, quote_entry, quote_stop, quote_qty, size_mode,
            created_at_ms, updated_at_ms
        ) VALUES (?, ?, ?, ?, ?, 'quoted', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            card_id,
            side,
            sym,
            stop_kind,
            stop_value,
            int(now_ms),
            float(card["entry"]),
            float(card["stop"]),
            float(card["qty"]),
            str(card.get("size_mode") or "risk"),
            int(now_ms),
            int(now_ms),
        ),
    )
    conn.commit()
    return card_id, card
