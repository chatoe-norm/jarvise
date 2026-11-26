"""Telegram UI for Quick config (q settings / qc: callbacks). No order paths."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from jarvise_assist.quick import (
    MSG_QUICK_CONFIRM_STALE,
    QUICK_CONFIRM_FUTURE_S,
    QUICK_CONFIRM_TTL_S,
    _cb,
    _fmt_fixed,
    get_quick_settings,
    write_quick_settings,
)
from jarvise_assist.quick_config import (
    _BY_KEY,
    AWAITING_QUICK_TTL_MS,
    GROUP_LABELS,
    QUICK_CONFIG,
    Advice,
    ConfigEntry,
    _bounds_th,
    canonical_string,
    get_confirm_cancel_ms,
    get_quick_config,
    mark_confirm_cancelled,
    needs_confirm,
    parse_value,
    quick_config_active_warnings,
    quick_config_advice,
    quick_config_history,
    reset_quick_config,
    resolve_entry,
    set_quick_config,
)
from jarvise_assist.quick_errors import e_confirm_stale_ttl, e_setting_reject
from jarvise_assist.sizing import portfolio_risk_cap_pct
from jarvise_assist.store import (
    clear_awaiting_quick_input,
    ensure_settings,
    set_awaiting_quick_input,
)

SendFn = Callable[..., Any]
_BANGKOK = timezone(timedelta(hours=7), name="Asia/Bangkok")

_CFG_CMD_RE = re.compile(
    r"^\s*q(?:uick)?\s+(settings?|config|set|reset|history)\b",
    re.I,
)
_RESERVED_CARD_WORDS = frozenset({"set", "settings", "setting", "config", "reset", "history", "risk", "cap"})


def _fmt_one(v: Any) -> str:
    d = Decimal(str(v))
    if d == d.to_integral_value():
        return str(int(d))
    return f"{d:.10f}".rstrip("0").rstrip(".")


def _fmt_display(entry: ConfigEntry, value: Any) -> str:
    if entry.type == "list_pct":
        return ",".join(_fmt_one(x) for x in value)
    if entry.unit == "%":
        return f"{_fmt_one(value)}%"
    if entry.unit == "R":
        return f"{_fmt_one(value)}R"
    if entry.unit == "s":
        return f"{int(value)}s"
    if entry.unit == "x":
        return f"{int(value)}x"
    if entry.unit == "bps":
        return f"{_fmt_one(value)}bps"
    if entry.unit == "ticks":
        return f"{int(value)}ticks"
    return str(value) if entry.type in {"hhmm", "autobe", "enum"} else _fmt_one(value)


def _encode_val(entry: ConfigEntry, value: Any) -> str:
    if entry.type == "enum":
        return str(value)
    if entry.type == "list_pct":
        return "_".join(_fmt_one(x) for x in value)
    if entry.type == "hhmm":
        return str(value).replace(":", "")
    if entry.type == "autobe":
        if value == "night":
            return "night"
        return str(value).replace(":", "")
    return _fmt_one(value)


def _decode_val(entry: ConfigEntry, raw: str) -> tuple[Any | None, str | None]:
    if entry.type == "list_pct":
        return parse_value(entry.key, raw.replace("_", ","))
    if entry.type in {"hhmm", "autobe"} and raw.isdigit() and len(raw) == 4:
        return parse_value(entry.key, f"{raw[:2]}:{raw[2:]}")
    return parse_value(entry.key, raw)


def parse_quick_config_command(text: str) -> dict[str, Any] | None:
    """Parse q settings/set/reset/history. None → other handlers."""
    if not _CFG_CMD_RE.match(text or ""):
        return None
    parts = (text or "").strip().split()
    if len(parts) < 2:
        return None
    if parts[0].lower() not in {"q", "quick"}:
        return None
    verb = parts[1].lower()
    if verb in {"settings", "setting", "config"}:
        return {"action": "settings"}
    if verb == "history":
        return {"action": "history"}
    if verb == "set":
        if len(parts) < 4:
            return {"action": "set_usage"}
        return {"action": "set", "key": parts[2], "raw": " ".join(parts[3:])}
    if verb == "reset":
        if len(parts) < 3:
            return {"action": "reset_usage"}
        return {"action": "reset", "key": parts[2]}
    return None


def is_quick_config_reserved_symbol(token: str) -> bool:
    return token.strip().lower() in _RESERVED_CARD_WORDS


def handle_quick_config_text(
    conn: Any,
    parsed: dict[str, Any],
    *,
    send: SendFn,
    now_ms: int,
) -> dict[str, Any]:
    action = parsed.get("action")
    if action == "settings":
        text, kb = render_settings_home(conn, now_ms=now_ms)
        send(text, reply_markup=kb)
        return {"ok": True, "settings": True}
    if action == "history":
        send(render_history(conn))
        return {"ok": True, "history": True}
    if action == "set_usage":
        send("พิมพ์: q set <alias> <ค่า> เช่น q set counter 0.3")
        return {"ok": True, "hint": True}
    if action == "reset_usage":
        send("พิมพ์: q reset <alias|all>")
        return {"ok": True, "hint": True}
    if action == "set":
        return apply_quick_config_change(
            conn,
            str(parsed["key"]),
            str(parsed["raw"]),
            send=send,
            now_ms=now_ms,
            confirmed=False,
            source="text:set",
        )
    if action == "reset":
        return handle_reset(conn, str(parsed["key"]), send=send, now_ms=now_ms, confirmed=False)
    send("คำสั่งไม่รู้จัก")
    return {"ok": False}


def render_history(conn: Any) -> str:
    rows = quick_config_history(conn, limit=10)
    if not rows:
        return "ยังไม่มีประวัติการตั้งค่า quick"
    lines = ["ประวัติตั้งค่า quick (10 ล่าสุด)"]
    for r in rows:
        at = datetime.fromtimestamp(int(r["at_ms"]) / 1000.0, tz=_BANGKOK).strftime("%m-%d %H:%M")
        key = str(r["key"])
        entry = _BY_KEY.get(key)
        alias = entry.alias if entry else key
        old = r["old_value"] if r["old_value"] is not None else "—"
        new = r["new_value"] if r["new_value"] is not None else "—"
        lines.append(f"{at} · {alias} {old} → {new} · {r['source']}")
    return "\n".join(lines)


def render_settings_home(conn: Any, *, now_ms: int) -> tuple[str, dict[str, Any]]:
    view = get_quick_config(conn)
    main = Decimal(str(ensure_settings(conn)["risk_pct"]))
    warns = quick_config_active_warnings(view.values, main_risk_pct=main)
    lines = ["⚙️ ตั้งค่า Quick", "เลือกกลุ่ม:"]
    for _g, label in GROUP_LABELS.items():
        lines.append(f"· {label}")
    if warns:
        lines.append("")
        lines.append("คำเตือนที่เปิดอยู่:")
        for a in warns:
            mark = "⚠️" if a.level == "warn" else "💡"
            lines.append(f"{mark} {a.text_th}")
    rows = [[{"text": label, "callback_data": _cb(f"qc:g:{g}")}] for g, label in GROUP_LABELS.items()]
    rows.append([{"text": "↩️ ค่าเริ่มต้นทั้งหมด", "callback_data": _cb("qc:ra")}])
    return "\n".join(lines), {"inline_keyboard": rows}


def _phase_suffix(entry: ConfigEntry) -> str:
    if entry.phase in {"now", "A0b"}:
        return " · ใช้แล้ว"
    if entry.phase == "A1":
        return " (ใช้ตั้งแต่ A1)"
    return " (A2)"


def _parse_example(entry: ConfigEntry) -> str:
    """Example value that round-trips through ``parse_value`` (not display-with-unit)."""
    return canonical_string(entry.key, entry.default)


def render_group(conn: Any, group: str, *, now_ms: int) -> tuple[str, dict[str, Any]]:
    view = get_quick_config(conn)
    label = GROUP_LABELS.get(group, group)
    lines = [f"⚙️ {label}"]
    rows: list[list[dict[str, str]]] = []
    for entry in QUICK_CONFIG:
        if entry.group != group:
            continue
        val = view.values[entry.key]
        shown = _fmt_display(entry, val)
        mark = " ✓" if canonical_string(entry.key, val) == canonical_string(entry.key, entry.default) else ""
        btn = f"{entry.alias} {shown}{mark}"
        lines.append(f"· {entry.alias} {shown}{_phase_suffix(entry)}")
        rows.append([{"text": btn, "callback_data": _cb(f"qc:k:{entry.alias}")}])
    rows.append([{"text": "⬅️ กลับ", "callback_data": _cb("qc:h")}])
    return "\n".join(lines), {"inline_keyboard": rows}


def _step_value(entry: ConfigEntry, value: Any, direction: int) -> Any | None:
    if entry.type == "list_pct":
        return None
    if entry.type == "enum":
        opts = list(entry.enum_values or ())
        i = opts.index(str(value)) if str(value) in opts else 0
        return opts[(i + direction) % len(opts)]
    if entry.type == "hhmm":
        mins = int(value[:2]) * 60 + int(value[3:])
        mins = (mins + direction * 30) % (24 * 60)
        return f"{mins // 60:02d}:{mins % 60:02d}"
    if entry.type == "autobe":
        if value == "night":
            return "23:30"
        return "night"
    if entry.type == "int":
        iv = int(value) + direction
        lo, hi = int(entry.hard_min), int(entry.hard_max)  # type: ignore[arg-type]
        return max(lo, min(hi, iv))
    d = Decimal(str(value))
    if entry.key == "quick_tp1_close_pct":
        step = Decimal("5")
    elif entry.type == "r":
        step = Decimal("0.1")
    elif d < 1:
        step = Decimal("0.05")
    elif d < 5:
        step = Decimal("0.25")
    else:
        step = Decimal("0.5")
    nxt = d + step * direction
    lo_d = Decimal(str(entry.hard_min))
    hi_d = Decimal(str(entry.hard_max))
    return max(lo_d, min(hi_d, nxt))


def render_key(conn: Any, alias: str, *, now_ms: int) -> tuple[str, dict[str, Any]]:
    entry = resolve_entry(alias)
    if entry is None:
        return "ไม่รู้จักคีย์", {"inline_keyboard": [[{"text": "⬅️ กลับ", "callback_data": _cb("qc:h")}]]}
    view = get_quick_config(conn)
    val = view.values[entry.key]
    lines = [
        f"⚙️ {entry.alias} — {entry.label_th}",
        f"ค่า: {_fmt_display(entry, val)}",
        f"เริ่มต้น: {_fmt_display(entry, entry.default)}",
        f"ขอบเขต: {_bounds_th(entry)}",
        entry.meaning_th + _phase_suffix(entry),
    ]
    note = view.stored_notes.get(entry.key)
    if note:
        lines.append(note)
    rows: list[list[dict[str, str]]] = []
    presets = _preset_buttons(entry)
    if presets:
        row: list[dict[str, str]] = []
        for p in presets:
            enc = _encode_val(entry, p)
            row.append(
                {
                    "text": _fmt_display(entry, p),
                    "callback_data": _cb(f"qc:v:{entry.alias}:{enc}"),
                }
            )
            if len(row) == 4:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
    if entry.type != "list_pct":
        minus = _step_value(entry, val, -1)
        plus = _step_value(entry, val, 1)
        step_row: list[dict[str, str]] = []
        if minus is not None:
            step_row.append(
                {
                    "text": "−",
                    "callback_data": _cb(f"qc:v:{entry.alias}:{_encode_val(entry, minus)}"),
                }
            )
        if plus is not None:
            step_row.append(
                {
                    "text": "+",
                    "callback_data": _cb(f"qc:v:{entry.alias}:{_encode_val(entry, plus)}"),
                }
            )
        if step_row:
            rows.append(step_row)
    rows.append(
        [
            {"text": "✏️ พิมพ์ค่า", "callback_data": _cb(f"qc:e:{entry.alias}")},
            {"text": "↩️ ค่าเริ่มต้น", "callback_data": _cb(f"qc:d:{entry.alias}")},
        ]
    )
    rows.append([{"text": "⬅️ กลับ", "callback_data": _cb(f"qc:g:{entry.group}")}])
    return "\n".join(lines), {"inline_keyboard": rows}


def _preset_buttons(entry: ConfigEntry) -> list[Any]:
    if entry.type == "enum":
        return list(entry.enum_values or ())
    if entry.key == "quick_risk_pct":
        return [Decimal("0.5"), Decimal("0.75"), Decimal("1.0")]
    if entry.key == "quick_risk_cap_pct":
        return [Decimal("1.0"), Decimal("1.5"), Decimal("2.0")]
    if entry.key == "quick_counter_risk_pct":
        return [Decimal("0.25"), Decimal("0.5"), Decimal("1.0")]
    if entry.key == "quick_tp1_close_pct":
        return [Decimal("0"), Decimal("50"), Decimal("100")]
    if entry.key == "quick_tp1_r":
        return [Decimal("0.5"), Decimal("1.0"), Decimal("2.0")]
    if entry.key == "quick_confirm_ttl_s":
        return [30, 60, 90]
    if entry.key == "quick_max_leverage":
        return [3, 4, 5]
    if entry.type == "autobe":
        return ["night", "23:30"]
    if entry.type == "list_pct":
        return [entry.default]
    return []


def _main_risk(conn: Any) -> Decimal:
    return Decimal(str(ensure_settings(conn)["risk_pct"]))


def _advice_markup(advice: list[Advice]) -> list[list[dict[str, str]]]:
    rows: list[list[dict[str, str]]] = []
    for a in advice:
        if a.fix is None:
            continue
        key, val = a.fix
        entry = _BY_KEY[key]
        enc = _encode_val(entry, val)
        rows.append(
            [
                {
                    "text": f"ปรับตาม: {entry.alias} → {_fmt_display(entry, val)}",
                    "callback_data": _cb(f"qc:fx:{entry.alias}:{enc}"),
                }
            ]
        )
    return rows


def _advice_lines(advice: list[Advice]) -> list[str]:
    lines: list[str] = []
    for a in advice:
        mark = "⚠️" if a.level == "warn" else "💡"
        lines.append(f"{mark} {a.text_th}")
    return lines


def apply_quick_config_change(
    conn: Any,
    alias_or_key: str,
    raw: str,
    *,
    send: SendFn,
    now_ms: int,
    confirmed: bool,
    source: str,
    confirm_ts: int | None = None,
) -> dict[str, Any]:
    entry = resolve_entry(alias_or_key)
    if entry is None:
        send("ไม่รู้จักคีย์นี้")
        return {"ok": False}
    value, err = parse_value(entry.key, raw)
    if err is not None or value is None:
        if entry.alias in {"risk", "cap"} and err and "เกินขอบเขต" not in err and "ขอบเขต" not in err:
            send(e_setting_reject(raw))
        else:
            send(err or e_setting_reject(raw))
        return {"ok": False, "rejected": True}

    view = get_quick_config(conn)
    old = view.values[entry.key]
    if needs_confirm(entry.key, old, value) and not confirmed:
        return _send_confirm(
            conn,
            entry=entry,
            old=old,
            new=value,
            send=send,
            now_ms=now_ms,
            source=source,
        )

    if confirmed and confirm_ts is not None:
        stale = _confirm_stale(conn, entry=entry, confirm_ts=confirm_ts, now_ms=now_ms)
        if stale is not None:
            send(stale)
            return {"ok": False, "rejected": True, "reason": "stale"}

    before = dict(view.values)
    try:
        old_v, new_v = set_quick_config(conn, entry.key, value, source=source, now_ms=now_ms)
    except ValueError as exc:
        send(str(exc))
        return {"ok": False, "rejected": True}

    after_view = get_quick_config(conn)
    advice = quick_config_advice(before, after_view.values, main_risk_pct=_main_risk(conn))
    head = f"{entry.alias} {_fmt_display(entry, old_v)} → {_fmt_display(entry, new_v)} ✅"
    body = "\n".join([head, *_advice_lines(advice)])
    kb_rows = _advice_markup(advice)
    if kb_rows:
        send(body, reply_markup={"inline_keyboard": kb_rows})
    else:
        send(body)
    return {"ok": True, "key": entry.key, "old": old_v, "new": new_v}


def _send_confirm(
    conn: Any,
    *,
    entry: ConfigEntry,
    old: Any,
    new: Any,
    send: SendFn,
    now_ms: int,
    source: str,
) -> dict[str, Any]:
    before = get_quick_config(conn).values
    after = dict(before)
    after[entry.key] = new
    advice = quick_config_advice(before, after, main_risk_pct=_main_risk(conn))
    # Legacy qs:ok keeps second granularity; new qc:ok cards use ms (13 digits).
    ts_s = int(now_ms) // 1000
    ts_ms = int(now_ms)
    enc = _encode_val(entry, new)
    if entry.alias == "risk" and source.startswith("qs"):
        fmt = _fmt_fixed(float(Decimal(str(new))))
        text = f"ยืนยันความเสี่ยงต่อไม้ {fmt}%?\nกดยืนยันภายใน 60 วิ"
        kb = {
            "inline_keyboard": [
                [
                    {"text": "✅ ยืนยัน", "callback_data": f"qs:ok:risk:{fmt}:{ts_s}"},
                    {"text": "↩️ ไม่เปลี่ยน", "callback_data": "qs:no"},
                ]
            ]
        }
        send(text, reply_markup=kb)
        return {"ok": True, "confirm": True}
    if entry.alias == "cap" and source.startswith("qs"):
        main_cap = float(portfolio_risk_cap_pct(_main_risk(conn)))
        total = main_cap + float(Decimal(str(new)))
        fmt = _fmt_fixed(float(Decimal(str(new))))
        text = (
            f"ยืนยันเพดาน {fmt}%? "
            f"ความเสี่ยงรวมสูงสุด {_fmt_fixed(total)}% "
            f"(หลัก {_fmt_fixed(main_cap)}% + quick {fmt}%)\n"
            "กดยืนยันภายใน 60 วิ"
        )
        kb = {
            "inline_keyboard": [
                [
                    {"text": "✅ ยืนยัน", "callback_data": f"qs:ok:cap:{fmt}:{ts_s}"},
                    {"text": "↩️ ไม่เปลี่ยน", "callback_data": "qs:no"},
                ]
            ]
        }
        send(text, reply_markup=kb)
        return {"ok": True, "confirm": True}

    lines = [
        f"ยืนยัน {entry.alias} {_fmt_display(entry, old)} → {_fmt_display(entry, new)}?",
        *_advice_lines(advice),
        "กดยืนยันภายใน 60 วิ",
    ]
    kb = {
        "inline_keyboard": [
            [
                {
                    "text": "✅ ยืนยัน",
                    "callback_data": _cb(f"qc:ok:{entry.alias}:{enc}:{ts_ms}"),
                },
                {"text": "↩️ ไม่เปลี่ยน", "callback_data": _cb("qc:no")},
            ]
        ]
    }
    send("\n".join(lines), reply_markup=kb)
    return {"ok": True, "confirm": True}


def _confirm_stale(
    conn: Any,
    *,
    entry: ConfigEntry | None,
    confirm_ts: int,
    now_ms: int,
    any_change: bool = False,
) -> str | None:
    """TTL / future / cancel-epoch / post-card changes in ms. ``entry=None`` → reset-all.

    ``confirm_ts`` is the card issuance time in milliseconds (13-digit qc:ok).
    """
    card_ms = int(confirm_ts)
    now = int(now_ms)
    if now - card_ms > QUICK_CONFIRM_TTL_S * 1000:
        return e_confirm_stale_ttl()
    if card_ms > now + QUICK_CONFIRM_FUTURE_S * 1000:
        return MSG_QUICK_CONFIRM_STALE

    cancel_ms = get_confirm_cancel_ms(conn)
    # Stale only when cancel is strictly after issuance (fresh card after cancel applies).
    if cancel_ms is not None and int(cancel_ms) > card_ms:
        return MSG_QUICK_CONFIRM_STALE

    check_settings = any_change or (
        entry is not None and entry.key in {"quick_risk_pct", "quick_risk_cap_pct"}
    )
    if check_settings:
        updated_at_ms = get_quick_settings(conn).get("updated_at_ms")
        if updated_at_ms is not None and int(updated_at_ms) > card_ms:
            return MSG_QUICK_CONFIRM_STALE

    if any_change or entry is None:
        row = conn.execute("SELECT MAX(at_ms) AS m FROM assist_quick_config_log").fetchone()
    else:
        row = conn.execute(
            "SELECT MAX(at_ms) AS m FROM assist_quick_config_log WHERE key=?",
            (entry.key,),
        ).fetchone()
    if row is not None:
        m = row["m"] if hasattr(row, "keys") else row[0]
        if m is not None and int(m) > card_ms:
            return MSG_QUICK_CONFIRM_STALE
    return None


def handle_reset(
    conn: Any,
    key_or_all: str,
    *,
    send: SendFn,
    now_ms: int,
    confirmed: bool,
    confirm_ts: int | None = None,
) -> dict[str, Any]:
    if key_or_all == "all":
        view = get_quick_config(conn)
        changes = []
        for entry in QUICK_CONFIG:
            old = view.values[entry.key]
            new = entry.default
            if canonical_string(entry.key, old) != canonical_string(entry.key, new):
                changes.append((entry.key, old, new))
        if not changes:
            send("ทุกค่าเป็นค่าเริ่มต้นอยู่แล้ว")
            return {"ok": True}
        if any(needs_confirm(k, o, n) for k, o, n in changes) and not confirmed:
            ts_ms = int(now_ms)
            send(
                "ยืนยันรีเซ็ตค่าทั้งหมดเป็นค่าเริ่มต้น?\nกดยืนยันภายใน 60 วิ",
                reply_markup={
                    "inline_keyboard": [
                        [
                            {"text": "✅ ยืนยัน", "callback_data": _cb(f"qc:ok:all:1:{ts_ms}")},
                            {"text": "↩️ ไม่เปลี่ยน", "callback_data": _cb("qc:no")},
                        ]
                    ]
                },
            )
            return {"ok": True, "confirm": True}
        if confirmed and confirm_ts is not None:
            stale = _confirm_stale(
                conn,
                entry=None,
                confirm_ts=confirm_ts,
                now_ms=now_ms,
                any_change=True,
            )
            if stale is not None:
                send(stale)
                return {"ok": False, "rejected": True, "reason": "stale"}
        before = dict(view.values)
        try:
            reset_quick_config(conn, "all", source="text:reset_all", now_ms=now_ms)
        except ValueError as exc:
            send(str(exc))
            return {"ok": False, "rejected": True}
        after = get_quick_config(conn).values
        advice = quick_config_advice(before, after, main_risk_pct=_main_risk(conn))
        lines = ["รีเซ็ตทั้งหมด ✅", *_advice_lines(advice)]
        kb = _advice_markup(advice)
        if kb:
            send("\n".join(lines), reply_markup={"inline_keyboard": kb})
        else:
            send("\n".join(lines))
        return {"ok": True, "reset": "all"}

    target = resolve_entry(key_or_all)
    if target is None:
        send("ไม่รู้จักคีย์นี้")
        return {"ok": False}
    return apply_quick_config_change(
        conn,
        target.alias,
        canonical_string(target.key, target.default),
        send=send,
        now_ms=now_ms,
        confirmed=confirmed,
        source="text:reset",
        confirm_ts=confirm_ts,
    )


def handle_quick_config_callback(
    conn: Any,
    data: str,
    *,
    send: SendFn,
    now_ms: int,
) -> dict[str, Any]:
    if data == "qc:no":
        mark_confirm_cancelled(conn, now_ms=now_ms)
        write_quick_settings(conn, updated_at_ms=now_ms)
        send("ยกเลิก")
        return {"ok": True, "cancelled": True}
    if data == "qc:h":
        text, kb = render_settings_home(conn, now_ms=now_ms)
        send(text, reply_markup=kb)
        return {"ok": True}
    if data == "qc:ra":
        return handle_reset(conn, "all", send=send, now_ms=now_ms, confirmed=False)

    if data.startswith("qc:g:"):
        group = data.split(":", 2)[2]
        text, kb = render_group(conn, group, now_ms=now_ms)
        send(text, reply_markup=kb)
        return {"ok": True, "group": group}

    if data.startswith("qc:k:"):
        alias = data.split(":", 2)[2]
        text, kb = render_key(conn, alias, now_ms=now_ms)
        send(text, reply_markup=kb)
        return {"ok": True, "key": alias}

    if data.startswith("qc:e:"):
        alias = data.split(":", 2)[2]
        entry = resolve_entry(alias)
        if entry is None:
            send("ไม่รู้จักคีย์นี้")
            return {"ok": False}
        set_awaiting_quick_input(conn, f"cfg:{entry.key}", until_ms=int(now_ms) + AWAITING_QUICK_TTL_MS)
        send(f"พิมพ์ค่าสำหรับ {entry.alias} ภายใน 5 นาที (เช่น {_parse_example(entry)})")
        return {"ok": True, "awaiting": entry.key}

    if data.startswith("qc:d:"):
        alias = data.split(":", 2)[2]
        entry = resolve_entry(alias)
        if entry is None:
            send("ไม่รู้จักคีย์นี้")
            return {"ok": False}
        return apply_quick_config_change(
            conn,
            entry.alias,
            canonical_string(entry.key, entry.default),
            send=send,
            now_ms=now_ms,
            confirmed=False,
            source="qc:default",
        )

    m_ok = re.match(r"^qc:ok:([^:]+):([^:]+):([0-9]{13})$", data or "")
    if m_ok:
        alias, enc, card_ms = m_ok.group(1), m_ok.group(2), int(m_ok.group(3))
        if alias == "all":
            return handle_reset(
                conn,
                "all",
                send=send,
                now_ms=now_ms,
                confirmed=True,
                confirm_ts=card_ms,
            )
        entry = resolve_entry(alias)
        if entry is None:
            send(MSG_QUICK_CONFIRM_STALE)
            return {"ok": False, "rejected": True}
        val, err = _decode_val(entry, enc)
        if err or val is None:
            send(err or e_setting_reject(enc))
            return {"ok": False, "rejected": True}
        return apply_quick_config_change(
            conn,
            entry.alias,
            canonical_string(entry.key, val),
            send=send,
            now_ms=now_ms,
            confirmed=True,
            source="qc:ok",
            confirm_ts=card_ms,
        )

    # 10-digit (or any other) qc:ok → stale; only exact 13-digit ms cards apply.
    if data.startswith("qc:ok:"):
        send(MSG_QUICK_CONFIRM_STALE)
        return {"ok": False, "rejected": True, "reason": "stale"}

    for prefix, source in (("qc:v:", "qc:v"), ("qc:fx:", "qc:fx")):
        if data.startswith(prefix):
            rest = data[len(prefix) :]
            parts = rest.split(":", 1)
            if len(parts) != 2:
                send("คำสั่งไม่รู้จัก")
                return {"ok": False}
            alias, enc = parts
            entry = resolve_entry(alias)
            if entry is None:
                send("ไม่รู้จักคีย์นี้")
                return {"ok": False}
            val, err = _decode_val(entry, enc)
            if err or val is None:
                send(err or e_setting_reject(enc))
                return {"ok": False, "rejected": True}
            return apply_quick_config_change(
                conn,
                entry.alias,
                canonical_string(entry.key, val),
                send=send,
                now_ms=now_ms,
                confirmed=False,
                source=source,
            )

    send("คำสั่งไม่รู้จัก")
    return {"ok": False}


def handle_awaiting_quick_input(
    conn: Any,
    text: str,
    *,
    send: SendFn,
    now_ms: int,
    lookup: Callable[[str], dict[str, Any] | None] | None = None,
    futures: Any = None,
) -> dict[str, Any] | None:
    """Consume typed value while awaiting_quick_input is set. None → fall through."""
    settings = ensure_settings(conn)
    pending = settings.get("awaiting_quick_input")
    until = settings.get("awaiting_quick_until_ms")
    if not pending:
        return None
    if until is not None and int(now_ms) > int(until):
        clear_awaiting_quick_input(conn)
        return None
    pending_s = str(pending)
    if pending_s == "symbol":
        return _handle_awaiting_symbol(conn, text, send=send, now_ms=now_ms, lookup=lookup, futures=futures)
    if not pending_s.startswith("cfg:"):
        clear_awaiting_quick_input(conn)
        return None
    key = pending_s[4:]
    clear_awaiting_quick_input(conn)
    return apply_quick_config_change(
        conn,
        key,
        text.strip(),
        send=send,
        now_ms=now_ms,
        confirmed=False,
        source="text:await",
    )


def _refresh_symbol_await(conn: Any, *, now_ms: int) -> None:
    set_awaiting_quick_input(conn, "symbol", until_ms=int(now_ms) + AWAITING_QUICK_TTL_MS)


def _handle_awaiting_symbol(
    conn: Any,
    text: str,
    *,
    send: SendFn,
    now_ms: int,
    lookup: Callable[[str], dict[str, Any] | None] | None,
    futures: Any,
) -> dict[str, Any]:
    from jarvise_assist.quick_m1a1 import render_side_picker
    from jarvise_assist.quick_symbol import validate_quick_symbol

    if lookup is None:
        _refresh_symbol_await(conn, now_ms=now_ms)
        send("อ่าน Binance ไม่ได้ — ลองพิมพ์ใหม่")
        return {"ok": False, "awaiting": "symbol", "reason": "no_lookup"}

    result = validate_quick_symbol(text, lookup=lookup)
    if result.outcome == "ok" and result.symbol:
        clear_awaiting_quick_input(conn)
        body, markup = render_side_picker(conn, result.symbol, now_ms=now_ms, futures=futures)
        send(body, reply_markup=markup)
        return {"ok": True, "step": "pick_side", "symbol": result.symbol}

    if result.outcome == "hint_1000" and result.hint_symbol:
        _refresh_symbol_await(conn, now_ms=now_ms)
        hint = result.hint_symbol
        markup = {
            "inline_keyboard": [
                [
                    {
                        "text": f"หมายถึง {hint} ?",
                        "callback_data": _cb(f"qk:cs:{hint}"),
                    }
                ],
                [{"text": "✖️ ยกเลิก", "callback_data": _cb("qk:cx")}],
            ]
        }
        send(str(result.message or ""), reply_markup=markup)
        return {"ok": False, "awaiting": "symbol", "hint": hint}

    _refresh_symbol_await(conn, now_ms=now_ms)
    cancel_row = {"inline_keyboard": [[{"text": "✖️ ยกเลิก", "callback_data": _cb("qk:cx")}]]}
    send(str(result.message or "พิมพ์ใหม่"), reply_markup=cancel_row)
    return {"ok": False, "awaiting": "symbol", "outcome": result.outcome}
