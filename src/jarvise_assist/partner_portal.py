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

from jarvise_assist import partner_fx as fx
from jarvise_assist import partner_kyc as pk
from jarvise_assist import partner_ledger as pl
from jarvise_assist import partner_profile as profile
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
_GUARD_CACHE_TTL_MS = 10 * 60 * 1000
_KYC_TOKEN_RE = re.compile(r"^pp:kyc:(ok|re):([0-9a-f]{8})$")
_UNRECORDED_CARD = "กำลังตรวจยอด"
_UNRECORDED_LOCK = Block(
    code="unrecorded_transfer",
    toast="มีเงินโอนที่ยังไม่บันทึก — รอแอดมิน",
    label="🔒 กำลังตรวจยอด",
)
_KYC_LOCKED = "บันทึกข้อมูลรับเงินแล้ว แก้ไขเองไม่ได้ — ติดต่อแอดมิน"
_KYC_SAVED = "บันทึกแล้ว แก้ไขเองไม่ได้"
_KYC_BEFORE_DEPOSIT = "กรอกข้อมูลรับเงินก่อนฝากครั้งแรก\nส่งข้อความที่ขึ้นต้นด้วย ข้อมูลรับเงิน\nอย่าโอนเงินก่อน"
_KYC_TEMPLATE = "ข้อมูลรับเงิน\nชื่อจริง: \nเบอร์: \nธนาคาร: \nเลขบัญชี: \nชื่อบัญชี: "
_KYC_HOLDER_WARN = "⚠️ ชื่อบัญชีไม่ตรงกับชื่อจริง — บัญชีต้องเป็นชื่อของคุณเอง"
_KYC_LENGTH_WARN = "⚠️ จำนวนหลักบัญชีไม่ตรงกับธนาคารนี้"
_KYC_LEGACY = pk._KYC_LEGACY
_START_RE = re.compile(r"^/start(@[A-Za-z0-9_]+)?\s+([A-Za-z0-9_-]{20,64})$")
_DEP_TEXT_RE = re.compile(r"^ฝาก\s+(\S+)$")
_KIND_CODE = {"cv": "convert", "wd": "withdraw_profit", "cl": "close"}
_BAD_DEP_TEXT = f"จำนวนไม่ถูกต้อง (ขั้นต่ำ {ps.MIN_DEPOSIT_USDT} USDT)"

# Portal-only V cache: (AccountValue, read_at_ms, due_total, total_units) or None
_value_cache: tuple[AccountValue, int, Decimal, Decimal] | None = None
# Display cache: (ledger_key, status, checked_at_ms). Never caches "unknown".
_guard_cache: tuple[tuple[str, int, int, int], str, int] | None = None


def reset_value_cache() -> None:
    """Test hook: clear the portal AccountValue and unrecorded-guard caches."""
    global _value_cache, _guard_cache
    _value_cache = None
    _guard_cache = None


def reset_guard_cache() -> None:
    """Test hook: clear the portal unrecorded-transfer guard cache."""
    global _guard_cache
    _guard_cache = None


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


def _unrecorded_status_for_card(conn: sqlite3.Connection, futures: Any, *, now_ms: int) -> str:
    """Tri-state for card display. Uses a 10-min ledger-keyed cache; never caches unknown."""
    from jarvise_assist import partner_unrecorded as pu

    global _guard_cache
    key = pu.ledger_cache_key(conn)
    if _guard_cache is not None:
        cached_key, status, at = _guard_cache
        if cached_key == key and int(now_ms) - at < _GUARD_CACHE_TTL_MS and status != "unknown":
            return status
    client = _resolve_client(futures)
    status = pu.resolve_unrecorded_status(conn, client, now_ms=int(now_ms))
    if status != "unknown":
        _guard_cache = (key, status, int(now_ms))
    else:
        _guard_cache = None
    return status


def _money_request_blocked(conn: sqlite3.Connection, futures: Any, *, now_ms: int) -> bool:
    """Fresh guard (no cache) before create_request. Fail closed unless clear."""
    from jarvise_assist import partner_unrecorded as pu

    client = _resolve_client(futures)
    return pu.guard_unrecorded_status(conn, client, now_ms=int(now_ms)) != "clear"


def _money_callback(code: str, lock: Block | None) -> str:
    """Unrecorded lock → ``pp:lk`` (toast only). Other locks keep ``pp:rq:*`` for their toast."""
    if lock is not None and lock.code == "unrecorded_transfer":
        return "pp:lk"
    return f"pp:rq:{code}"


def _card_buttons(locks: dict[str, Block] | None = None, *, kyc_saved: bool = False) -> dict[str, Any]:
    """Active-card keyboard. Unrecorded locks use ``pp:lk`` so taps cannot open fee previews."""
    locks = locks or {}
    cv = locks.get("cv")
    wd = locks.get("wd")
    cl = locks.get("cl")
    if kyc_saved:
        last = [{"text": "👤 โปรไฟล์", "callback_data": "pp:pf"}]
    else:
        last = [{"text": "ข้อมูลรับเงิน", "callback_data": "pp:kyc"}]
    return {
        "inline_keyboard": [
            [
                {"text": "🔄 ดูยอด", "callback_data": "pp:v"},
                {"text": "💰 แจ้งฝาก", "callback_data": "pp:dep"},
            ],
            [
                {
                    "text": cv.label if cv is not None else "🔁 ทบกำไร",
                    "callback_data": _money_callback("cv", cv),
                },
                {
                    "text": wd.label if wd is not None else "💸 ถอนกำไร",
                    "callback_data": _money_callback("wd", wd),
                },
            ],
            [
                {
                    "text": cl.label if cl is not None else "🚪 ปิดพอร์ต",
                    "callback_data": _money_callback("cl", cl),
                }
            ],
            [{"text": "📊 ไม้ที่เปิดอยู่", "callback_data": "pp:pos"}],
            last,
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
    fee_rate = ps.fee_rate_from_bps(int(row["fee_rate_bps"]))
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
    futures: Any = None,
) -> tuple[str, dict[str, Any] | None]:
    name = str(row["name"])
    stamp = _bangkok_stamp(now_ms)
    cost = ps.usdt_from_micro(int(row["cost_micro"]))
    units = ps.units_from_e8(int(row["units_e8"]))
    pid = str(row["id"])
    joined = partner_rules.joined_at_ms(conn, pid)
    kyc_saved = pk.kyc_state(conn, pid) == "saved"
    unrecorded = True
    try:
        status = _unrecorded_status_for_card(conn, futures, now_ms=now_ms)
        unrecorded = status != "clear"
    except Exception:  # noqa: BLE001
        logger.warning("portal unrecorded check failed")
        unrecorded = True

    def _with_kyc(lines: list[str], *, closed: bool = False) -> list[str]:
        lines.append(pk.card_kyc_line(conn, pid))
        if closed:
            if not kyc_saved:
                lines.append("แก้ไขข้อมูลรับเงิน: ติดต่อแอดมิน")
        elif not kyc_saved and joined is not None:
            lines.append(_KYC_LEGACY)
        return lines

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
        for waiting in fx.awaiting_thb(conn, pid):
            x = ps.usdt_from_micro(int(waiting["payout_micro"] or 0))
            lines.append(f"กำลังแปลงเป็นเงินบาท {fmt_usdt(x)} USDT")

    if str(row["status"]) == "closed":
        lines = [f"พอร์ตของ {name} · {stamp}", "ปิดพอร์ตแล้ว"]
        summary = ps.close_summary(conn, pid)
        if summary.get("ok"):
            joined_ms = summary.get("joined_at_ms")
            closed_ms = summary.get("closed_at_ms")
            lines.append(
                "เข้าร่วม " + (partner_rules.fmt_date(int(joined_ms)) if joined_ms is not None else "—")
            )
            lines.append(
                "ปิดเมื่อ " + (partner_rules.fmt_date(int(closed_ms)) if closed_ms is not None else "—")
            )
            close_cash = summary.get("close_cash")
            if not isinstance(close_cash, Decimal):
                close_cash = Decimal("0")
            lines.append(f"ยอดปิด {fmt_usdt(close_cash)} USDT")
            for due in summary.get("dues") or []:
                lines.append(f"รอโอน {fmt_usdt(Decimal(due['cash']))} USDT")
        for waiting in fx.awaiting_thb(conn, pid):
            x = ps.usdt_from_micro(int(waiting["payout_micro"] or 0))
            lines.append(f"กำลังแปลงเป็นเงินบาท {fmt_usdt(x)} USDT")
        dues = summary.get("dues") if summary.get("ok") else None
        markup = None
        if dues:
            markup = {"inline_keyboard": [[{"text": "👤 โปรไฟล์", "callback_data": "pp:pf"}]]}
        return "\n".join(_with_kyc(lines, closed=True)), markup

    nav: Decimal | None = None
    if isinstance(av, AccountValue):
        nav = _nav_for_rules(conn, av)
    locks = _locks_for_row(conn, row, nav=nav, now_ms=now_ms)
    if unrecorded:
        locks = {
            "cv": _UNRECORDED_LOCK,
            "wd": _UNRECORDED_LOCK,
            "cl": _UNRECORDED_LOCK,
        }
    markup = _card_buttons(locks, kyc_saved=kyc_saved)

    if unrecorded:
        lines = _header()
        lines.extend([_UNRECORDED_CARD, f"ทุน {fmt_usdt(cost)} USDT"])
        _pending_and_due(lines)
        return "\n".join(_with_kyc(lines)), markup

    if isinstance(av, AccountValueError):
        lines = _header()
        lines.extend(
            [
                "อ่านยอดไม่ได้ตอนนี้ ลองใหม่อีกครั้ง",
                f"ทุน {fmt_usdt(cost)} USDT",
            ]
        )
        _pending_and_due(lines)
        return "\n".join(_with_kyc(lines)), markup

    if units == 0 and cost == 0:
        lines = _header()
        lines.append("ยังไม่มีเงินในพอร์ต")
        _pending_and_due(lines)
        return "\n".join(_with_kyc(lines)), markup

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
        return "\n".join(_with_kyc(lines)), markup

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
    return "\n".join(_with_kyc(lines)), markup


def _send_card(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    send: Any,
    futures: Any,
    now_ms: int,
) -> dict[str, Any]:
    av = _read_v_cached(conn, futures, now_ms=now_ms)
    text, markup = _build_card(conn, row, av, now_ms=now_ms, futures=futures)
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
    if ps.deposit_kyc_required(conn, row):
        send_to_partner(send, row, _KYC_BEFORE_DEPOSIT)
        return {"ok": False, "reason": "kyc_required"}
    amount = parse_amount(amount_raw, allow_zero=False)
    if amount is None or amount < ps.MIN_DEPOSIT_USDT:
        send_to_partner(send, row, _BAD_DEP_TEXT)
        return {"ok": False, "reason": "bad_amount"}
    created = ps.create_request(conn, str(row["id"]), "deposit", now_ms, amount=amount, source="portal")
    if not created.get("ok"):
        reason = str(created.get("reason") or "")
        if reason == "request_pending":
            send_to_partner(send, row, "มีคำขอค้างอยู่ รอแอดมิน")
            return {"ok": False, "reason": "request_pending"}
        send_to_partner(send, row, _BAD_DEP_TEXT)
        return {"ok": False, "reason": reason}
    rid = str(created["request_id"])
    send_to_partner(send, row, f"แจ้งฝาก {fmt_usdt(amount)} USDT แล้ว รอแอดมินยืนยัน")
    try:
        send(
            f"📥 {row['name']} แจ้งฝาก {fmt_usdt(amount)} USDT\n"
            f"{pk.deposit_kyc_line(conn, str(row['id']))}\n"
            f"กดบันทึกเมื่อเงินเข้า futures wallet แล้วเท่านั้น\n"
            f"ถ้ายอดเข้าจริงไม่เท่านี้ ให้ยกเลิก แล้วใช้ p dep {row['name']} <ยอดจริง>",
            reply_markup=_admin_deposit_buttons(rid),
        )
    except Exception:  # noqa: BLE001
        logger.warning("portal admin deposit notice failed: %s", "Exception")
    return {"ok": True, "cmd": "deposit_notify", "request_id": rid}


def _is_kyc_form(text: str) -> bool:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped == "ข้อมูลรับเงิน"
    return False


def _kyc_confirm_text(staged: dict[str, Any]) -> str:
    lines = [
        "ตรวจข้อมูลก่อนบันทึก",
        f"ชื่อจริง: {staged['real_name']}",
        f"เบอร์: {staged['phone']}",
        f"ธนาคาร: {staged['bank_code']}",
        f"เลขบัญชี: {staged['account_no']}",
        f"ชื่อบัญชี: {staged['holder_name']}",
        pk.KYC_WARNING,
    ]
    if not staged.get("holder_matches"):
        lines.append(_KYC_HOLDER_WARN)
    if staged.get("account_length_warn"):
        lines.append(_KYC_LENGTH_WARN)
    return "\n".join(lines)


def _kyc_saved_text(result: dict[str, Any]) -> str:
    account = pk.mask_account(str(result["bank_code"]), str(result["account_last4"]))
    phone = pk.mask_phone(str(result["phone_last4"]))
    return (
        f"{_KYC_SAVED}\n"
        f"ข้อมูลรับเงิน: {result['real_name_mask']}\n"
        f"{phone}\n"
        f"โอนเข้า: {account} ({result['holder_name_mask']})"
    )


def _kyc_buttons(token: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ ยืนยันบันทึก", "callback_data": f"pp:kyc:ok:{token}"},
                {"text": "✏️ กรอกใหม่", "callback_data": f"pp:kyc:re:{token}"},
            ]
        ]
    }


def _kyc_template_text() -> str:
    return pk.KYC_WARNING + "\n" + _KYC_TEMPLATE


def _delete_incoming_form(message: dict[str, Any], delete: Any, partner_id: str) -> None:
    """Best-effort delete of the partner's typed form. Never logs the text."""
    raw_chat = message.get("chat")
    chat: dict[str, Any] = raw_chat if isinstance(raw_chat, dict) else {}
    if delete is None or message.get("message_id") is None:
        return
    try:
        removed = delete(str(chat.get("id")), int(message["message_id"]))
    except Exception:  # noqa: BLE001
        logger.warning("partner kyc delete failed partner_id=%s", partner_id)
        return
    if removed is False:
        logger.warning("partner kyc delete failed partner_id=%s", partner_id)


def _retire_kyc_confirm(
    *,
    mid: Any,
    partner_chat: str,
    text: str,
    edit: Any,
    delete: Any,
    partner_id: str,
) -> bool:
    """Replace the confirm message. On a failed edit, delete it and report False.

    Callers send ``text`` only when this returns False, so the full form is not left behind
    and is never copied into a new message.
    """
    if mid is None:
        return False
    try:
        if edit(int(mid), text, reply_markup={"inline_keyboard": []}, chat_id=partner_chat):
            return True
    except Exception:  # noqa: BLE001
        pass
    if delete is not None:
        try:
            removed = delete(partner_chat, int(mid))
        except Exception as exc:  # noqa: BLE001
            logger.warning("partner kyc confirm delete failed err=%s", type(exc).__name__)
        else:
            if removed is False:
                logger.warning("partner kyc confirm delete failed partner_id=%s", partner_id)
    return False


def _kyc_ok_failure_text(reason: str) -> str:
    if reason == "kyc_draft_expired":
        return "ฟอร์มหมดอายุ ส่งข้อมูลรับเงินอีกครั้ง"
    if reason == "kyc_locked":
        return _KYC_LOCKED
    if reason == "closed":
        return "ปิดพอร์ตแล้ว — ติดต่อแอดมิน"
    return "บันทึกข้อมูลรับเงินไม่ได้ตอนนี้"


def _notify_admin_kyc(conn: sqlite3.Connection, row: sqlite3.Row, result: dict[str, Any], send: Any) -> None:
    account = pk.mask_account(str(result["bank_code"]), str(result["account_last4"]))
    lines = [
        f"📝 {row['name']} บันทึกข้อมูลรับเงิน",
        str(result["real_name_mask"]),
        pk.mask_phone(str(result["phone_last4"])),
        f"โอนเข้า: {account} ({result['holder_name_mask']})",
    ]
    if not result.get("holder_matches"):
        lines.append("⚠️ ชื่อบัญชีไม่ตรงกับชื่อจริง")
    if result.get("account_length_warn"):
        lines.append(_KYC_LENGTH_WARN)
    for other_id in result.get("duplicate_partner_ids") or []:
        other = ps.get_partner(conn, str(other_id))
        handle = str(other["name"]) if other is not None else str(other_id)
        lines.append(f"⚠️ ซ้ำกับ {handle}")
    lines.append(f"ดูเต็ม: p kyc {row['name']}")
    try:
        send("\n".join(lines))
    except Exception:  # noqa: BLE001
        logger.warning("partner kyc admin notice failed partner_id=%s", row["id"])


def _handle_kyc_text(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    text: str,
    *,
    message: dict[str, Any],
    send: Any,
    delete: Any,
    now_ms: int,
) -> dict[str, Any]:
    pid = str(row["id"])
    try:
        state = pk.kyc_state(conn, pid)
    except Exception:  # noqa: BLE001
        _delete_incoming_form(message, delete, pid)
        raise
    if state == "saved":
        send_to_partner(send, row, _KYC_LOCKED)
        _delete_incoming_form(message, delete, pid)
        return {"ok": False, "reason": "kyc_locked"}
    try:
        staged = pk.stage_draft(conn, pid, text, now_ms)
    except pk.KycKeyError:
        logger.warning("partner kyc key unavailable partner_id=%s", pid)
        send_to_partner(send, row, "บันทึกข้อมูลรับเงินไม่ได้ตอนนี้")
        _delete_incoming_form(message, delete, pid)
        return {"ok": False, "reason": "kyc_key"}
    except Exception:  # noqa: BLE001
        _delete_incoming_form(message, delete, pid)
        raise
    if not staged.get("ok"):
        reason = str(staged.get("reason") or "")
        if reason == "kyc_invalid":
            send_to_partner(send, row, "\n".join(str(e) for e in (staged.get("errors") or [])))
        elif reason == "kyc_locked":
            send_to_partner(send, row, _KYC_LOCKED)
        elif reason == "closed":
            send_to_partner(send, row, "ปิดพอร์ตแล้ว")
        elif reason == "admin_exempt":
            send_to_partner(send, row, "แอดมินไม่ต้องกรอก")
        else:
            send_to_partner(send, row, "บันทึกข้อมูลรับเงินไม่ได้ตอนนี้")
        _delete_incoming_form(message, delete, pid)
        return {"ok": False, "reason": reason}
    _delete_incoming_form(message, delete, pid)
    send_to_partner(send, row, _kyc_confirm_text(staged), reply_markup=_kyc_buttons(str(staged["token"])))
    return {"ok": True, "cmd": "kyc_confirm", "token": staged["token"]}


def handle_portal_text(
    conn: sqlite3.Connection,
    message: dict[str, Any],
    *,
    send: Any,
    futures: Any = None,
    now_ms: int,
    delete: Any = None,
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
    start = _START_RE.match(text.strip())
    # A closed partner who is still bound may /start a new account. Any other
    # text, including /start while the account is still active, keeps the card.
    if row is not None and not (start is not None and str(row["status"]) == "closed"):
        dep = _DEP_TEXT_RE.match(text.strip())
        if dep is not None:
            return _handle_deposit_text(conn, row, dep.group(1), send=send, now_ms=now_ms)
        if _is_kyc_form(text):
            return _handle_kyc_text(conn, row, text, message=message, send=send, delete=delete, now_ms=now_ms)
        return _send_card(conn, row, send=send, futures=futures, now_ms=now_ms)

    if start is None:
        return {"ignored": "foreign_chat"}

    code = start.group(2)
    admin_chat = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    bound = ps.bind_invite(conn, code, tg, admin_chat_id=admin_chat, now_ms=now_ms)
    if not bound.get("ok"):
        reason = str(bound.get("reason") or "")
        if reason == "closed_payout_due":
            refusal = "บัญชีเดิมยังมียอดรอโอน — ผูกบัญชีใหม่ไม่ได้จนกว่าแอดมินจะโอนครบ"
            try:
                send(refusal, chat_id=str(chat.get("id")), protect_content=True)
            except Exception:  # noqa: BLE001
                logger.warning("portal closed-rebind refusal failed: %s", "Exception")
            return {"ok": False, "reason": "closed_payout_due"}
        # Still bound: a spent or unknown code is not a new account.
        if row is not None:
            return _send_card(conn, row, send=send, futures=futures, now_ms=now_ms)
        try:
            send(
                "ลิงก์เชิญไม่ถูกต้องหรือหมดอายุ — ขอลิงก์ใหม่จากแอดมิน",
                chat_id=str(chat.get("id")),
                protect_content=True,
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
    # Non-money rules first (kyc / closed / pending) — same order as submit.
    early = partner_rules.block(conn, row, kind, nav=None, now_ms=now_ms)
    if early is not None:
        answer(early.toast)
        return {"ok": False, "reason": early.code}
    try:
        status = _unrecorded_status_for_card(conn, futures, now_ms=now_ms)
        if status != "clear":
            answer(_UNRECORDED_LOCK.toast)
            return {"ok": False, "reason": "unrecorded_transfer"}
    except Exception:  # noqa: BLE001
        logger.warning("portal preview unrecorded check failed")
        answer(_UNRECORDED_LOCK.toast)
        return {"ok": False, "reason": "unrecorded_transfer"}
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
    if kind in ("withdraw_profit", "close"):
        current = pk.current_kyc(conn, str(row["id"]))
        if current is not None:
            account = pk.mask_account(str(current["bank_code"]), str(current["account_last4"]))
            body += f"\nโอนเข้า: {account} ({current['holder_name_mask']})"
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


def _request_kyc_line(conn: sqlite3.Connection, row: sqlite3.Row, kind: str) -> str:
    if kind not in ("withdraw_profit", "close"):
        return ""
    current = pk.current_kyc(conn, str(row["id"]))
    kyc_id = None if current is None else str(current["id"])
    return pk.payout_account_line(
        conn,
        partner_id=str(row["id"]),
        payout_kyc_id=kyc_id,
        partner_name=str(row["name"]),
    )


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
    # Non-money rules first (closed / pending / close clock) so a
    # closed partner still gets "ปิดพอร์ตแล้ว" even when V is unreadable.
    early = partner_rules.block(conn, row, kind, nav=None, now_ms=now_ms)
    if early is not None:
        answer(early.toast)
        return {"ok": False, "reason": early.code}
    if kind in ("convert", "withdraw_profit", "close"):
        try:
            if _money_request_blocked(conn, futures, now_ms=now_ms):
                answer(_UNRECORDED_LOCK.toast)
                return {"ok": False, "reason": "unrecorded_transfer"}
        except Exception:  # noqa: BLE001
            logger.warning("portal unrecorded gate failed")
            answer(_UNRECORDED_LOCK.toast)
            return {"ok": False, "reason": "unrecorded_transfer"}

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
        notice = (
            f"📥 คำขอจาก {row['name']}: {kind_th(kind)}\n"
            f"ประมาณการตอนนี้: เงินออก {fmt_usdt(cash)} · ค่าธรรมเนียม {fmt_usdt(fee)}"
        )
        kyc_line = _request_kyc_line(conn, row, kind)
        if kyc_line:
            notice += "\n" + kyc_line
        notice += "\nกดยืนยัน = ล็อกตัวเลขตามยอดพอร์ตตอนกด และตั้งเป็นยอดค้างจ่าย\nหมดอายุใน 24 ชม."
        send(notice, reply_markup=_admin_request_buttons(rid))
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
    delete: Any = None,
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
        text, markup = _build_card(conn, row, av, now_ms=now_ms, futures=futures)
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
        if ps.deposit_kyc_required(conn, row):
            _answer()
            send_to_partner(send, row, _KYC_BEFORE_DEPOSIT)
            return {"ok": False, "reason": "kyc_required"}
        _answer()
        tip = (
            "โอนเงินให้แอดมินตามที่ตกลงกัน แล้วพิมพ์ ฝาก <จำนวน> เช่น ฝาก 500\n"
            f"ขั้นต่ำ {ps.MIN_DEPOSIT_USDT} USDT · แอดมินจะบันทึกหลังเงินเข้าพอร์ตแล้ว"
        )
        send_to_partner(send, row, tip)
        return {"ok": True, "cmd": "dep_help"}

    if data == "pp:x":
        _answer()
        av = _read_v_cached(conn, futures, now_ms=now_ms)
        text, markup = _build_card(conn, row, av, now_ms=now_ms, futures=futures)
        if mid is not None:
            try:
                if edit(int(mid), text, reply_markup=markup, chat_id=partner_chat):
                    return {"ok": True, "cmd": "cancel_preview"}
            except Exception:  # noqa: BLE001
                pass
        send_to_partner(send, row, text, reply_markup=markup)
        return {"ok": True, "cmd": "cancel_preview"}

    if data == "pp:lk":
        _answer(_UNRECORDED_LOCK.toast)
        return {"ok": False, "reason": "unrecorded_transfer"}

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

    if data == "pp:pf":
        _answer()
        account_value = None
        if str(row["status"]) == "active":
            av = _read_v_cached(conn, futures, now_ms=now_ms)
            account_value = av.value if isinstance(av, AccountValue) else None
        text = profile.render_profile(
            profile.profile_data(conn, str(row["id"]), now_ms, account_value=account_value)
        )
        markup = {"inline_keyboard": [[{"text": "⬅️ กลับ", "callback_data": "pp:v"}]]}
        return _edit_or_unchanged(
            msg=msg,
            text=text,
            markup=markup,
            mid=mid,
            partner_chat=partner_chat,
            edit=edit,
            send=send,
            row=row,
            cmd="profile",
        )

    if data == "pp:kyc":
        _answer()
        if str(row["status"]) != "active":
            send_to_partner(send, row, "ปิดพอร์ตแล้ว — ติดต่อแอดมิน")
            return {"ok": False, "reason": "closed"}
        if pk.kyc_state(conn, str(row["id"])) == "saved":
            send_to_partner(send, row, "แก้ไขข้อมูล: ติดต่อแอดมิน")
            return {"ok": True, "cmd": "kyc_locked"}
        send_to_partner(send, row, _kyc_template_text())
        return {"ok": True, "cmd": "kyc_template"}

    kyc_cb = _KYC_TOKEN_RE.match(data)
    if kyc_cb is not None:
        action, token = kyc_cb.group(1), kyc_cb.group(2)
        _answer()
        pid = str(row["id"])
        if action == "re":
            pk.discard_draft(conn, pid, token)
            body = _kyc_template_text()
            if not _retire_kyc_confirm(
                mid=mid, partner_chat=partner_chat, text=body, edit=edit, delete=delete, partner_id=pid
            ):
                send_to_partner(send, row, body)
            return {"ok": True, "cmd": "kyc_redo"}
        try:
            result = pk.commit_draft(conn, pid, token, now_ms)
        except pk.KycKeyError:
            logger.warning("partner kyc key unavailable partner_id=%s", pid)
            fail = _kyc_ok_failure_text("kyc_key")
            if not _retire_kyc_confirm(
                mid=mid, partner_chat=partner_chat, text=fail, edit=edit, delete=delete, partner_id=pid
            ):
                send_to_partner(send, row, fail)
            return {"ok": False, "reason": "kyc_key"}
        except pk.KycCryptoError:
            logger.warning("partner kyc decrypt failed partner_id=%s", pid)
            fail = _kyc_ok_failure_text("kyc_crypto")
            if not _retire_kyc_confirm(
                mid=mid, partner_chat=partner_chat, text=fail, edit=edit, delete=delete, partner_id=pid
            ):
                send_to_partner(send, row, fail)
            return {"ok": False, "reason": "kyc_crypto"}
        if not result.get("ok"):
            reason = str(result.get("reason") or "")
            fail = _kyc_ok_failure_text(reason)
            if not _retire_kyc_confirm(
                mid=mid, partner_chat=partner_chat, text=fail, edit=edit, delete=delete, partner_id=pid
            ):
                send_to_partner(send, row, fail)
            return {"ok": False, "reason": reason}
        saved = _kyc_saved_text(result)
        if not _retire_kyc_confirm(
            mid=mid, partner_chat=partner_chat, text=saved, edit=edit, delete=delete, partner_id=pid
        ):
            send_to_partner(send, row, saved)
        _notify_admin_kyc(conn, row, result, send)
        return {"ok": True, "cmd": "kyc_saved", "kyc_id": result.get("kyc_id")}

    _answer("ปุ่มนี้ใช้ไม่ได้แล้ว")
    return {"ok": False, "reason": "unknown"}
