"""Partner-facing Telegram portal (P3). Bound private chats only.

Shows only the partner's own balance and request previews. Decimal only.
Never confirms, rejects, or marks paid — those stay in partner_admin.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from jarvise_assist import partner_ledger as pl
from jarvise_assist import partner_rules
from jarvise_assist import partner_store as ps
from jarvise_assist.futures import build_futures_client
from jarvise_assist.partner_admin import (
    fmt_pct,
    fmt_signed_usdt,
    fmt_usdt,
    kind_th,
    parse_amount,
    send_to_partner,
)
from jarvise_assist.partner_positions import render_partner_positions
from jarvise_assist.partner_rules import Block
from jarvise_assist.partner_value import (
    AccountValue,
    AccountValueError,
    read_account_value,
)

logger = logging.getLogger(__name__)

BANGKOK = timezone(timedelta(hours=7), name="Asia/Bangkok")
_CACHE_TTL_MS = 30_000
_START_RE = re.compile(r"^/start(@[A-Za-z0-9_]+)?\s+([A-Za-z0-9_-]{20,64})$")
_DEP_TEXT_RE = re.compile(r"^ฝาก\s+(\S+)$")
_KIND_CODE = {"cv": "convert", "wd": "withdraw_profit", "cl": "close"}
_MIN_DEP = Decimal("50")

# Portal-only V cache: (AccountValue, read_at_ms, due_total, total_units) or None
_value_cache: tuple[AccountValue, int, Decimal, Decimal] | None = None


def reset_value_cache() -> None:
    """Test hook: clear the portal AccountValue cache."""
    global _value_cache
    _value_cache = None


def _bangkok_stamp(now_ms: int) -> str:
    dt = datetime.fromtimestamp(now_ms / 1000, tz=BANGKOK)
    return dt.strftime("%d/%m %H:%M")


def _state_emoji(state: str) -> str:
    if state == "up":
        return "🟢"
    if state == "down":
        return "🔴"
    return "⚪"


def _gate(message_or_cb_msg: dict[str, Any], from_user: dict[str, Any] | None) -> bool:
    raw_chat = message_or_cb_msg.get("chat")
    chat: dict[str, Any] = raw_chat if isinstance(raw_chat, dict) else {}
    if not isinstance(from_user, dict):
        return False
    return str(chat.get("type") or "") == "private" and str(chat.get("id")) == str(from_user.get("id"))


def _resolve_client(futures: Any) -> Any:
    if futures is not None:
        return futures
    try:
        return build_futures_client()
    except Exception as exc:  # noqa: BLE001
        logger.warning("partner portal futures client unavailable: %s", type(exc).__name__)
        return None


def _edit_or_unchanged(
    *,
    msg: dict[str, Any],
    text: str,
    markup: dict[str, Any] | None,
    mid: Any,
    partner_chat: str,
    edit: Any,
    send: Any,
    row: Any,
    cmd: str,
) -> dict[str, Any]:
    """Edit when text changed; skip edit/send when Telegram would say unchanged."""
    if msg.get("text") == text:
        return {"ok": True, "cmd": cmd, "unchanged": True}
    ok = False
    if mid is not None:
        try:
            ok = bool(edit(int(mid), text, reply_markup=markup, chat_id=partner_chat))
        except Exception:  # noqa: BLE001
            ok = False
    if not ok:
        send_to_partner(send, row, text, reply_markup=markup)
    return {"ok": True, "cmd": cmd}


def _read_v_cached(
    conn: sqlite3.Connection, futures: Any, *, now_ms: int
) -> AccountValue | AccountValueError:
    """Read V with a 30 s TTL; miss when due total or unit sum drifted since cache."""
    global _value_cache
    if _value_cache is not None:
        cached, at, due_cached, units_cached = _value_cache
        if int(now_ms) - at < _CACHE_TTL_MS:
            if ps.payouts_due_total(conn) == due_cached and ps.total_units(conn) == units_cached:
                return cached
    av = read_account_value(_resolve_client(futures), now_ms=now_ms)
    if isinstance(av, AccountValue):
        _value_cache = (
            av,
            int(now_ms),
            ps.payouts_due_total(conn),
            ps.total_units(conn),
        )
    return av


def _card_buttons(locks: dict[str, Block] | None = None) -> dict[str, Any]:
    """Active-card keyboard. Locked buttons change only ``text``; callbacks stay ``pp:rq:*``."""
    locks = locks or {}
    cv = locks.get("cv")
    wd = locks.get("wd")
    cl = locks.get("cl")
    return {
        "inline_keyboard": [
            [
                {"text": "🔄 ดูยอด", "callback_data": "pp:v"},
                {"text": "💰 แจ้งฝาก", "callback_data": "pp:dep"},
            ],
            [
                {"text": cv.label if cv is not None else "🔁 ทบกำไร", "callback_data": "pp:rq:cv"},
                {"text": wd.label if wd is not None else "💸 ถอนกำไร", "callback_data": "pp:rq:wd"},
            ],
            [
                {
                    "text": cl.label if cl is not None else "🚪 ปิดพอร์ต",
                    "callback_data": "pp:rq:cl",
                }
            ],
            [{"text": "📊 ไม้ที่เปิดอยู่", "callback_data": "pp:pos"}],
        ]
    }


def _nav_for_rules(conn: sqlite3.Connection, av: AccountValue) -> Decimal | None:
    due = ps.payouts_due_total(conn)
    u_total = ps.total_units(conn)
    n = pl.nav(av.value - due, u_total)
    if isinstance(n, pl.Refusal):
        return None
    return n


def _locks_for_row(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    nav: Decimal | None,
    now_ms: int,
) -> dict[str, Block]:
    out: dict[str, Block] = {}
    for code, kind in (("cv", "convert"), ("wd", "withdraw_profit"), ("cl", "close")):
        blocked = partner_rules.block(conn, row, kind, nav=nav, now_ms=now_ms)
        if blocked is not None:
            out[code] = blocked
    return out


def _go_buttons(code: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ ส่งคำขอ", "callback_data": f"pp:go:{code}"},
                {"text": "✖️ ไม่ทำ", "callback_data": "pp:x"},
            ]
        ]
    }


def _admin_deposit_buttons(request_id: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ บันทึกฝาก", "callback_data": f"pa:dy:{request_id}"},
                {"text": "✖️ ยกเลิก", "callback_data": f"pa:dn:{request_id}"},
            ]
        ]
    }


def _admin_request_buttons(request_id: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ ยืนยัน", "callback_data": f"pa:ry:{request_id}"},
                {"text": "✖️ ปฏิเสธ", "callback_data": f"pa:rn:{request_id}"},
            ]
        ]
    }


def _estimate(
    conn: sqlite3.Connection, row: sqlite3.Row, kind: str, av: AccountValue
) -> tuple[Decimal, Decimal, Decimal] | str:
    """Return (cash_or_zero, fee, cost_after) or refusal reason."""
    due = ps.payouts_due_total(conn)
    u_total = ps.total_units(conn)
    n = pl.nav(av.value - due, u_total)
    if isinstance(n, pl.Refusal):
        return n.reason
    units = ps.units_from_e8(int(row["units_e8"]))
    cost = ps.usdt_from_micro(int(row["cost_micro"]))
    fee_rate = ps._fee_rate_from_bps(int(row["fee_rate_bps"]))
    if kind == "convert":
        cv = pl.plan_convert(units=units, cost=cost, fee_rate=fee_rate, nav=n)
        if isinstance(cv, pl.Refusal):
            return cv.reason
        return Decimal("0"), cv.fee, cv.cost_after
    if kind == "withdraw_profit":
        wd = pl.plan_withdraw_profit(units=units, cost=cost, fee_rate=fee_rate, nav=n)
        if isinstance(wd, pl.Refusal):
            return wd.reason
        return wd.cash, wd.fee, cost
    if kind == "close":
        cl = pl.plan_close(units=units, cost=cost, fee_rate=fee_rate, nav=n)
        if isinstance(cl, pl.Refusal):
            return cl.reason
        return cl.cash, cl.fee, Decimal("0")
    return "bad_kind"


def _build_card(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    av: AccountValue | AccountValueError,
    *,
    now_ms: int,
) -> tuple[str, dict[str, Any] | None]:
    name = str(row["name"])
    stamp = _bangkok_stamp(now_ms)
    cost = ps.usdt_from_micro(int(row["cost_micro"]))
    units = ps.units_from_e8(int(row["units_e8"]))
    pid = str(row["id"])
    joined = partner_rules.joined_at_ms(conn, pid)

    def _header() -> list[str]:
        lines = [f"พอร์ตของ {name} · {stamp}"]
        if joined is not None:
            lines.append(f"เข้าร่วม {partner_rules.fmt_date(joined)}")
        return lines

    def _pending_and_due(lines: list[str]) -> None:
        pending = conn.execute(
            """
            SELECT * FROM partner_request
            WHERE partner_id = ? AND status = 'pending' AND expires_at_ms > ?
            ORDER BY created_at_ms DESC LIMIT 1
            """,
            (pid, int(now_ms)),
        ).fetchone()
        if pending is not None:
            lines.append(f"คำขอค้าง: {kind_th(str(pending['kind']))} · รอแอดมิน")
        for d in ps.list_due_payouts(conn):
            if str(d["partner_id"]) == pid:
                x = ps.usdt_from_micro(int(d["payout_micro"] or 0))
                lines.append(f"รอโอน {fmt_usdt(x)} USDT")

    if str(row["status"]) == "closed":
        lines = [f"พอร์ตของ {name} · {stamp}", "ปิดพอร์ตแล้ว"]
        for d in ps.list_due_payouts(conn):
            if str(d["partner_id"]) == pid:
                x = ps.usdt_from_micro(int(d["payout_micro"] or 0))
                lines.append(f"รอโอน {fmt_usdt(x)} USDT")
        return "\n".join(lines), None

    nav: Decimal | None = None
    if isinstance(av, AccountValue):
        nav = _nav_for_rules(conn, av)
    locks = _locks_for_row(conn, row, nav=nav, now_ms=now_ms)
    markup = _card_buttons(locks)

    if isinstance(av, AccountValueError):
        lines = _header()
        lines.extend(
            [
                "อ่านยอดไม่ได้ตอนนี้ ลองใหม่อีกครั้ง",
                f"ทุน {fmt_usdt(cost)} USDT",
            ]
        )
        _pending_and_due(lines)
        return "\n".join(lines), markup

    if units == 0 and cost == 0:
        lines = _header()
        lines.append("ยังไม่มีเงินในพอร์ต")
        _pending_and_due(lines)
        return "\n".join(lines), markup

    view = ps.view_partner(conn, pid, av.value)
    if not view.get("ok"):
        lines = _header()
        lines.extend(
            [
                "อ่านยอดไม่ได้ตอนนี้ ลองใหม่อีกครั้ง",
                f"ทุน {fmt_usdt(cost)} USDT",
            ]
        )
        _pending_and_due(lines)
        return "\n".join(lines), markup

    net = Decimal(view["net"])
    pnl = net - cost
    pct = fmt_pct(pnl, cost)
    if pct != "—" and pnl > 0 and not pct.startswith("+"):
        pct = f"+{pct}"
    lines = _header()
    lines.extend(
        [
            f"ทุน {fmt_usdt(cost)} USDT",
            f"ยอดสุทธิ {fmt_usdt(net)} USDT (หลังหักค่าธรรมเนียม)",
            f"{_state_emoji(str(view['state']))} {fmt_signed_usdt(pnl)} ({pct})",
        ]
    )
    _pending_and_due(lines)
    return "\n".join(lines), markup


def _send_card(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    send: Any,
    futures: Any,
    now_ms: int,
) -> dict[str, Any]:
    av = _read_v_cached(conn, futures, now_ms=now_ms)
    text, markup = _build_card(conn, row, av, now_ms=now_ms)
    send_to_partner(send, row, text, reply_markup=markup)
    return {"ok": True, "cmd": "card"}


def _handle_deposit_text(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    amount_raw: str,
    *,
    send: Any,
    now_ms: int,
) -> dict[str, Any]:
    if str(row["status"]) != "active":
        send_to_partner(send, row, "ปิดพอร์ตแล้ว")
        return {"ok": False, "reason": "closed"}
    amount = parse_amount(amount_raw, allow_zero=False)
    if amount is None or amount < _MIN_DEP:
        send_to_partner(send, row, "จำนวนไม่ถูกต้อง (ขั้นต่ำ 50 USDT)")
        return {"ok": False, "reason": "bad_amount"}
    created = ps.create_request(conn, str(row["id"]), "deposit", now_ms, amount=amount, source="portal")
    if not created.get("ok"):
        reason = str(created.get("reason") or "")
        if reason == "request_pending":
            send_to_partner(send, row, "มีคำขอค้างอยู่ รอแอดมิน")
            return {"ok": False, "reason": "request_pending"}
        send_to_partner(send, row, "จำนวนไม่ถูกต้อง (ขั้นต่ำ 50 USDT)")
        return {"ok": False, "reason": reason}
    rid = str(created["request_id"])
    send_to_partner(send, row, f"แจ้งฝาก {fmt_usdt(amount)} USDT แล้ว รอแอดมินยืนยัน")
    try:
        send(
            f"📥 {row['name']} แจ้งฝาก {fmt_usdt(amount)} USDT\n"
            f"กดบันทึกเมื่อเงินเข้า futures wallet แล้วเท่านั้น\n"
            f"ถ้ายอดเข้าจริงไม่เท่านี้ ให้ยกเลิก แล้วใช้ p dep {row['name']} <ยอดจริง>",
            reply_markup=_admin_deposit_buttons(rid),
        )
    except Exception:  # noqa: BLE001
        logger.warning("portal admin deposit notice failed: %s", "Exception")
    return {"ok": True, "cmd": "deposit_notify", "request_id": rid}


def handle_portal_text(
    conn: sqlite3.Connection,
    message: dict[str, Any],
    *,
    send: Any,
    futures: Any = None,
    now_ms: int,
) -> dict[str, Any]:
    raw_chat = message.get("chat")
    chat: dict[str, Any] = raw_chat if isinstance(raw_chat, dict) else {}
    raw_from = message.get("from")
    from_user: dict[str, Any] = raw_from if isinstance(raw_from, dict) else {}
    if not _gate(message, from_user):
        return {"ignored": "foreign_chat"}

    tg = str(from_user.get("id"))
    row = ps.partner_by_telegram_id(conn, tg)
    text = str(message.get("text") or "")
    if row is not None:
        dep = _DEP_TEXT_RE.match(text.strip())
        if dep is not None:
            return _handle_deposit_text(conn, row, dep.group(1), send=send, now_ms=now_ms)
        return _send_card(conn, row, send=send, futures=futures, now_ms=now_ms)

    m = _START_RE.match(text.strip())
    if m is None:
        return {"ignored": "foreign_chat"}

    code = m.group(2)
    admin_chat = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    bound = ps.bind_invite(conn, code, tg, admin_chat_id=admin_chat, now_ms=now_ms)
    if not bound.get("ok"):
        try:
            send(
                "ลิงก์เชิญไม่ถูกต้องหรือหมดอายุ — ขอลิงก์ใหม่จากแอดมิน",
                chat_id=str(chat.get("id")),
            )
        except Exception:  # noqa: BLE001
            logger.warning("portal invalid-invite send failed: %s", "Exception")
        return {"ok": False, "reason": "invite_invalid"}

    bound_row = ps.partner_by_telegram_id(conn, tg)
    assert bound_row is not None
    send_to_partner(send, bound_row, "ผูกบัญชีแล้ว · บอทนี้แสดงเฉพาะยอดของคุณ")
    _send_card(conn, bound_row, send=send, futures=futures, now_ms=now_ms)

    display = str(from_user.get("first_name") or "")
    if not display.strip():
        uname = str(from_user.get("username") or "").strip()
        display = f"@{uname}" if uname else "ผู้ใช้"
    display = display.replace("\n", " ").replace("\r", " ").strip()[:32]
    try:
        send(f"🔗 {bound['name']} ผูก Telegram แล้ว · {display}")
    except Exception:  # noqa: BLE001
        logger.warning("portal admin bind notice failed: %s", "Exception")
    return {"ok": True, "cmd": "bind", "partner_id": bound["partner_id"]}


def _preview_request(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    code: str,
    *,
    answer: Any,
    edit: Any,
    send: Any,
    msg: dict[str, Any],
    futures: Any,
    now_ms: int,
) -> dict[str, Any]:
    kind = _KIND_CODE[code]
    av = _read_v_cached(conn, futures, now_ms=now_ms)
    nav: Decimal | None = None
    if isinstance(av, AccountValue):
        nav = _nav_for_rules(conn, av)
    blocked = partner_rules.block(conn, row, kind, nav=nav, now_ms=now_ms)
    if blocked is not None:
        answer(blocked.toast)
        return {"ok": False, "reason": blocked.code}
    answer()

    mid = msg.get("message_id")
    partner_chat = str(row["telegram_user_id"])
    if isinstance(av, AccountValueError):
        text = "อ่านยอดไม่ได้ตอนนี้ ลองใหม่อีกครั้ง"
        if mid is not None:
            try:
                edit(int(mid), text, reply_markup={"inline_keyboard": []}, chat_id=partner_chat)
            except Exception:  # noqa: BLE001
                send_to_partner(send, row, text)
        else:
            send_to_partner(send, row, text)
        return {"ok": False, "reason": av.reason}
    est = _estimate(conn, row, kind, av)
    if isinstance(est, str):
        if est in ("no_profit", "below_min"):
            text = "ยังไม่มีกำไร ทำรายการนี้ไม่ได้"
        else:
            text = "ทำรายการนี้ไม่ได้"
        if mid is not None:
            try:
                edit(int(mid), text, reply_markup={"inline_keyboard": []}, chat_id=partner_chat)
            except Exception:  # noqa: BLE001
                send_to_partner(send, row, text)
        else:
            send_to_partner(send, row, text)
        return {"ok": False, "reason": est}
    cash, fee, cost_after = est
    if kind == "convert":
        body = f"ทบกำไร: ค่าธรรมเนียม {fmt_usdt(fee)} · ทุนใหม่ ≈ {fmt_usdt(cost_after)} USDT"
    elif kind == "withdraw_profit":
        body = f"ถอนกำไร ≈ {fmt_usdt(cash)} USDT · ค่าธรรมเนียม {fmt_usdt(fee)} · ทุนเท่าเดิม"
    else:
        body = f"ปิดพอร์ต · จะได้รับ ≈ {fmt_usdt(cash)} USDT · ค่าธรรมเนียม {fmt_usdt(fee)}"
    body = body + "\nตัวเลขจริงคิดตอนแอดมินยืนยัน"
    markup = _go_buttons(code)
    if mid is not None:
        try:
            if edit(int(mid), body, reply_markup=markup, chat_id=partner_chat):
                return {"ok": True, "cmd": "preview", "kind": kind}
        except Exception:  # noqa: BLE001
            pass
    send_to_partner(send, row, body, reply_markup=markup)
    return {"ok": True, "cmd": "preview", "kind": kind}


def _submit_request(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    code: str,
    *,
    answer: Any,
    edit: Any,
    send: Any,
    msg: dict[str, Any],
    futures: Any,
    now_ms: int,
) -> dict[str, Any]:
    kind = _KIND_CODE[code]
    # Non-money rules first (closed / pending / close clock / convert month) so a
    # closed partner still gets "ปิดพอร์ตแล้ว" even when V is unreadable.
    early = partner_rules.block(conn, row, kind, nav=None, now_ms=now_ms)
    if early is not None:
        answer(early.toast)
        return {"ok": False, "reason": early.code}

    av = _read_v_cached(conn, futures, now_ms=now_ms)
    if kind in ("convert", "withdraw_profit"):
        if isinstance(av, AccountValueError):
            answer("อ่านยอดไม่ได้ตอนนี้ ลองใหม่อีกครั้ง")
            return {"ok": False, "reason": av.reason}
        nav = _nav_for_rules(conn, av)
        if nav is None:
            # block() skips the 20 USDT mins on nav=None; fail closed instead.
            answer("อ่านยอดไม่ได้ตอนนี้ ลองใหม่อีกครั้ง")
            return {"ok": False, "reason": "nav_not_positive"}
        blocked = partner_rules.block(conn, row, kind, nav=nav, now_ms=now_ms)
        if blocked is not None:
            answer(blocked.toast)
            return {"ok": False, "reason": blocked.code}
    # close: money mins do not apply; unreadable V keeps today's create_request path

    created = ps.create_request(conn, str(row["id"]), kind, now_ms, source="portal")
    if not created.get("ok"):
        reason = str(created.get("reason") or "")
        if reason == "request_pending":
            answer("มีคำขอค้างอยู่ รอแอดมิน")
            return {"ok": False, "reason": "request_pending"}
        answer("ปุ่มนี้ใช้ไม่ได้แล้ว")
        return {"ok": False, "reason": reason}
    rid = str(created["request_id"])
    cash = Decimal("0")
    fee = Decimal("0")
    if isinstance(av, AccountValue):
        est = _estimate(conn, row, kind, av)
        if not isinstance(est, str):
            cash, fee, _ = est
    mid = msg.get("message_id")
    partner_chat = str(row["telegram_user_id"])
    done = "ส่งคำขอแล้ว รอแอดมินยืนยัน · หมดอายุใน 24 ชม."
    if mid is not None:
        try:
            edit(int(mid), done, reply_markup={"inline_keyboard": []}, chat_id=partner_chat)
        except Exception:  # noqa: BLE001
            send_to_partner(send, row, done)
    else:
        send_to_partner(send, row, done)
    try:
        send(
            f"📥 คำขอจาก {row['name']}: {kind_th(kind)}\n"
            f"ประมาณการตอนนี้: เงินออก {fmt_usdt(cash)} · ค่าธรรมเนียม {fmt_usdt(fee)}\n"
            f"กดยืนยัน = ล็อกตัวเลขตามยอดพอร์ตตอนกด และตั้งเป็นยอดค้างจ่าย\n"
            f"หมดอายุใน 24 ชม.",
            reply_markup=_admin_request_buttons(rid),
        )
    except Exception:  # noqa: BLE001
        logger.warning("portal admin request notice failed: %s", "Exception")
    return {"ok": True, "cmd": "request", "request_id": rid, "kind": kind}


def handle_portal_callback(
    conn: sqlite3.Connection,
    data: str,
    *,
    callback: dict[str, Any],
    send: Any,
    answer: Any,
    edit: Any,
    futures: Any = None,
    now_ms: int,
) -> dict[str, Any]:
    raw_msg = callback.get("message")
    msg: dict[str, Any] = raw_msg if isinstance(raw_msg, dict) else {}
    raw_from = callback.get("from")
    from_user: dict[str, Any] = raw_from if isinstance(raw_from, dict) else {}
    if not _gate(msg, from_user):
        return {"ignored": "foreign_chat"}

    tg = str(from_user.get("id"))
    row = ps.partner_by_telegram_id(conn, tg)
    if row is None:
        return {"ignored": "foreign_chat"}

    cq_id = str(callback.get("id") or "")
    answered = False

    def _answer(text: str | None = None) -> None:
        nonlocal answered
        if not answered:
            answer(cq_id, text=text)
            answered = True

    mid = msg.get("message_id")
    partner_chat = str(row["telegram_user_id"])

    if data == "pp:v":
        _answer()
        av = _read_v_cached(conn, futures, now_ms=now_ms)
        text, markup = _build_card(conn, row, av, now_ms=now_ms)
        return _edit_or_unchanged(
            msg=msg,
            text=text,
            markup=markup,
            mid=mid,
            partner_chat=partner_chat,
            edit=edit,
            send=send,
            row=row,
            cmd="refresh",
        )

    if data == "pp:dep":
        if str(row["status"]) != "active":
            _answer("ปิดพอร์ตแล้ว")
            return {"ok": False, "reason": "closed"}
        _answer()
        tip = (
            "โอนเงินให้แอดมินตามที่ตกลงกัน แล้วพิมพ์ ฝาก <จำนวน> เช่น ฝาก 500\n"
            "ขั้นต่ำ 50 USDT · แอดมินจะบันทึกหลังเงินเข้าพอร์ตแล้ว"
        )
        send_to_partner(send, row, tip)
        return {"ok": True, "cmd": "dep_help"}

    if data == "pp:x":
        _answer()
        av = _read_v_cached(conn, futures, now_ms=now_ms)
        text, markup = _build_card(conn, row, av, now_ms=now_ms)
        if mid is not None:
            try:
                if edit(int(mid), text, reply_markup=markup, chat_id=partner_chat):
                    return {"ok": True, "cmd": "cancel_preview"}
            except Exception:  # noqa: BLE001
                pass
        send_to_partner(send, row, text, reply_markup=markup)
        return {"ok": True, "cmd": "cancel_preview"}

    if data.startswith("pp:rq:"):
        code = data[len("pp:rq:") :]
        if code not in _KIND_CODE:
            _answer("ปุ่มนี้ใช้ไม่ได้แล้ว")
            return {"ok": False, "reason": "unknown"}
        return _preview_request(
            conn,
            row,
            code,
            answer=_answer,
            edit=edit,
            send=send,
            msg=msg,
            futures=futures,
            now_ms=now_ms,
        )

    if data.startswith("pp:go:"):
        code = data[len("pp:go:") :]
        if code not in _KIND_CODE:
            _answer("ปุ่มนี้ใช้ไม่ได้แล้ว")
            return {"ok": False, "reason": "unknown"}
        out = _submit_request(
            conn,
            row,
            code,
            answer=_answer,
            edit=edit,
            send=send,
            msg=msg,
            futures=futures,
            now_ms=now_ms,
        )
        if not answered:
            _answer()
        return out

    if data == "pp:pos":
        _answer()
        if str(row["status"]) != "active":
            text = "ปิดพอร์ตแล้ว"
            ok = False
            if mid is not None:
                try:
                    ok = bool(edit(int(mid), text, reply_markup=None, chat_id=partner_chat))
                except Exception:  # noqa: BLE001
                    ok = False
            if not ok:
                send_to_partner(send, row, text, reply_markup=None)
            return {"ok": True, "cmd": "positions"}
        text = render_partner_positions(conn, _resolve_client(futures), now_ms=now_ms)
        markup = {
            "inline_keyboard": [
                [
                    {"text": "🔄 รีเฟรช", "callback_data": "pp:pos"},
                    {"text": "⬅️ กลับ", "callback_data": "pp:v"},
                ]
            ]
        }
        return _edit_or_unchanged(
            msg=msg,
            text=text,
            markup=markup,
            mid=mid,
            partner_chat=partner_chat,
            edit=edit,
            send=send,
            row=row,
            cmd="positions",
        )

    _answer("ปุ่มนี้ใช้ไม่ได้แล้ว")
    return {"ok": False, "reason": "unknown"}
