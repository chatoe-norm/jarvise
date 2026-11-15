"""Admin-only Telegram commands for the partner unit/NAV ledger (P2/P3).

Owner private chat only. Decimal only. Partner notifications go through send_to_partner.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any

from jarvise_assist import partner_ledger as pl
from jarvise_assist import partner_store as ps
from jarvise_assist.futures import build_futures_client
from jarvise_assist.partner_value import (
    AccountValue,
    AccountValueError,
    read_account_value,
)

logger = logging.getLogger(__name__)

BANGKOK = timezone(timedelta(hours=7), name="Asia/Bangkok")

_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-\u0E00-\u0E7F]{1,24}$")
_AMOUNT_RE = re.compile(r"^(?:[0-9]{1,9}(?:\.[0-9]{1,6})?|[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]{1,6})?)$")
_CMD_RE = re.compile(r"^\s*p(\s|$)", re.IGNORECASE)
_REQ_ID_RE = re.compile(r"^pr_[0-9a-f]{16}$")

_CENT = Decimal("0.01")
_NAV_Q = Decimal("0.000001")
_UNIT_Q = Decimal("0.0001")
_ZERO = Decimal("0")
_ONE = Decimal("1")
_NAV_WARN = Decimal("0.10")
_MAX_PARTNERS_SHOWN = 40

HELP_TEXT = (
    "คำสั่งพอร์ตร่วม (แอดมินเท่านั้น)\n"
    "p — สรุปพอร์ต\n"
    "p help — รายการคำสั่ง\n"
    "p init — ตั้งต้นครั้งแรก\n"
    "p add <ชื่อ> — เพิ่มพาร์ทเนอร์ (ค่าธรรมเนียม 50%)\n"
    "p dep <ชื่อ> <จำนวน> — บันทึกฝาก (ยืนยันก่อน)\n"
    "p cost <ชื่อ> <ทุนใหม่> <เหตุผล> — ปรับทุน (เหตุผลอย่างน้อย 10 ตัวอักษร)\n"
    "p invite <ชื่อ> — ออกลิงก์เชิญผูก Telegram\n"
    "p unbind <ชื่อ> — ยกเลิกการผูก Telegram"
)

_GATE_MSG = "คำสั่งพอร์ตร่วมใช้ได้เฉพาะแชทส่วนตัวกับบอท"
_FAIL_MSG = "ทำไม่สำเร็จ — ยังไม่บันทึกอะไร"

_STORE_TH: dict[str, str] = {
    "not_found": "ไม่พบพาร์ทเนอร์",
    "closed": "พาร์ทเนอร์ปิดแล้ว",
    "nav_not_positive": "ยอดพอร์ตน้อยกว่าเงินฝาก — เงินเข้า futures wallet แล้วหรือยัง",
    "amount_not_positive": "จำนวนต้องมากกว่าศูนย์",
    "below_min_deposit": "ฝากขั้นต่ำ 50 USDT",
    "request_pending": "มีรายการค้างอยู่",
    "request_not_pending": "รายการนี้ปิดไปแล้ว",
    "reason_short": "เหตุผลสั้นเกินไป (อย่างน้อย 10 ตัวอักษร)",
    "cost_negative": "ทุนติดลบไม่ได้",
    "bad_number": "ตัวเลขไม่ถูกต้อง",
    "bad_args": "อาร์กิวเมนต์ไม่ครบ",
    "no_admin": "ยังไม่มีแอดมิน",
    "deposit_pending": "มีรายการฝากค้างอยู่",
    "first_deposit_must_equal_value": "ฝากครั้งแรกต้องเท่ากับยอดพอร์ต",
}

_VALUE_TH: dict[str, str] = {
    "no_client": "อ่านยอดไม่ได้ — ไม่มีคีย์อ่าน",
    "read_failed": "อ่านยอดไม่สำเร็จ",
    "bad_payload": "ข้อมูลจากตลาดไม่ถูกต้อง",
    "no_usdt_row": "ไม่พบยอด USDT ใน futures wallet",
    "non_usdt_asset": "มีสินทรัพย์อื่นในวอลเล็ต",
    "non_usdt_position": "มีโพซิชันที่ไม่ใช่ USDT",
    "wallet_moving": "ยอดวอลเล็ตเปลี่ยนระหว่างอ่าน — ลองใหม่",
    "value_not_positive": "ยอดพอร์ตต้องมากกว่าศูนย์",
}


def fmt_usdt(value: Decimal) -> str:
    q = Decimal(value).quantize(_CENT, rounding=ROUND_DOWN)
    if q == 0:
        return "0.00"
    sign = "-" if q < 0 else ""
    body = f"{abs(q):,.2f}"
    return f"{sign}{body}"


def fmt_signed_usdt(value: Decimal) -> str:
    q = Decimal(value).quantize(_CENT, rounding=ROUND_DOWN)
    if q == 0:
        return "0.00"
    if q > 0:
        return f"+{q:,.2f}"
    return f"{q:,.2f}"


def fmt_pct(numer: Decimal, denom: Decimal) -> str:
    if Decimal(denom) == 0:
        return "—"
    pct = (Decimal(numer) / Decimal(denom) * Decimal("100")).quantize(_CENT, rounding=ROUND_DOWN)
    if pct == 0:
        return "0.00%"
    if pct > 0:
        return f"{pct:.2f}%"
    return f"{pct:.2f}%"


def fmt_nav(value: Decimal) -> str:
    q = Decimal(value).quantize(_NAV_Q, rounding=ROUND_DOWN)
    return f"{q:.6f}"


def fmt_units(value: Decimal) -> str:
    q = Decimal(value).quantize(_UNIT_Q, rounding=ROUND_DOWN)
    return f"{q:,.4f}"


def thai_reason(code: str, *, detail: str = "") -> str:
    if code in _STORE_TH:
        return _STORE_TH[code]
    if code in _VALUE_TH:
        base = _VALUE_TH[code]
        if detail and code in ("non_usdt_asset", "non_usdt_position"):
            return f"{base} ({detail})"
        return base
    return f"ทำไม่ได้ ({code})"


def parse_amount(raw: str, *, allow_zero: bool) -> Decimal | None:
    if not _AMOUNT_RE.match(raw):
        return None
    cleaned = raw.replace(",", "")
    try:
        value = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite():
        return None
    if allow_zero:
        if value < 0:
            return None
    elif value <= 0:
        return None
    return value


def kind_th(kind: str) -> str:
    return {
        "deposit": "ฝาก",
        "convert": "ทบกำไร",
        "withdraw_profit": "ถอนกำไร",
        "close": "ปิดพอร์ต",
    }.get(kind, kind)


def send_to_partner(
    send: Any,
    row: sqlite3.Row | dict[str, Any],
    text: str,
    *,
    reply_markup: dict[str, Any] | None = None,
) -> bool:
    """Send to a bound non-admin partner. All callback_data must start with pp:."""
    try:
        tg = row["telegram_user_id"] if not isinstance(row, dict) else row.get("telegram_user_id")
        is_admin = int(row["is_admin"] if not isinstance(row, dict) else row.get("is_admin") or 0)
        if tg is None or is_admin == 1:
            return False
        owner = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
        if str(tg) == owner:
            return False
        if reply_markup is not None:
            for row_btns in reply_markup.get("inline_keyboard") or []:
                for btn in row_btns:
                    data = str(btn.get("callback_data") or "")
                    if not data.startswith("pp:"):
                        return False
        send(text, reply_markup=reply_markup, chat_id=str(tg))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("partner send failed: %s", type(exc).__name__)
        return False


def parse_partner_command(text: str) -> dict[str, Any] | None:
    if not _CMD_RE.match(text or ""):
        return None
    parts = (text or "").strip().split()
    if not parts:
        return None
    if len(parts) == 1:
        return {"cmd": "overview"}
    head = parts[1].lower()
    if head == "help" and len(parts) == 2:
        return {"cmd": "help"}
    if head == "init" and len(parts) == 2:
        return {"cmd": "init"}
    if head == "add":
        if len(parts) != 3 or not _NAME_RE.match(parts[2]):
            return {"error": "usage"}
        return {"cmd": "add", "name": parts[2]}
    if head == "dep":
        if len(parts) != 4 or not _NAME_RE.match(parts[2]):
            return {"error": "usage"}
        amount = parse_amount(parts[3], allow_zero=False)
        if amount is None:
            return {"error": "usage"}
        return {"cmd": "dep", "name": parts[2], "amount": amount}
    if head == "cost":
        if len(parts) < 5 or not _NAME_RE.match(parts[2]):
            return {"error": "usage"}
        new_cost = parse_amount(parts[3], allow_zero=True)
        if new_cost is None:
            return {"error": "usage"}
        reason = " ".join(parts[4:])
        return {"cmd": "cost", "name": parts[2], "new_cost": new_cost, "reason": reason}
    if head == "invite":
        if len(parts) != 3 or not _NAME_RE.match(parts[2]):
            return {"error": "usage"}
        return {"cmd": "invite", "name": parts[2]}
    if head == "unbind":
        if len(parts) != 3 or not _NAME_RE.match(parts[2]):
            return {"error": "usage"}
        return {"cmd": "unbind", "name": parts[2]}
    return {"error": "usage"}


def is_admin_private(chat: dict[str, Any] | None, from_user: dict[str, Any] | None) -> bool:
    expected = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not expected or not isinstance(chat, dict) or not isinstance(from_user, dict):
        return False
    return (
        str(chat.get("id")) == expected
        and str(chat.get("type") or "") == "private"
        and str(from_user.get("id")) == expected
    )


def _has_events(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT 1 FROM partner_event LIMIT 1").fetchone()
    return row is not None


def _find_partner_by_name(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM partner WHERE lower(name) = lower(?)",
        (name,),
    ).fetchone()


def _pending_request(conn: sqlite3.Connection, partner_id: str, *, now_ms: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM partner_request
        WHERE partner_id = ? AND status = 'pending' AND expires_at_ms > ?
        ORDER BY created_at_ms DESC LIMIT 1
        """,
        (partner_id, int(now_ms)),
    ).fetchone()


def _latest_nav(conn: sqlite3.Connection) -> tuple[Decimal, int] | None:
    row = conn.execute(
        """
        SELECT nav, created_at_ms FROM partner_event
        ORDER BY created_at_ms DESC, rowid DESC LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    try:
        return Decimal(str(row["nav"])), int(row["created_at_ms"])
    except (InvalidOperation, ValueError, TypeError):
        return None


def _deposit_buttons(request_id: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ บันทึกฝาก", "callback_data": f"pa:dy:{request_id}"},
                {"text": "✖️ ยกเลิก", "callback_data": f"pa:dn:{request_id}"},
            ]
        ]
    }


def _init_buttons() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ ตั้งต้น", "callback_data": "pa:iy"},
                {"text": "✖️ ยกเลิก", "callback_data": "pa:in"},
            ]
        ]
    }


def _resolve_client(futures: Any) -> Any:
    if futures is not None:
        return futures
    return build_futures_client()


def _read_v(futures: Any, *, now_ms: int) -> AccountValue | AccountValueError:
    return read_account_value(_resolve_client(futures), now_ms=now_ms)


def _bangkok_stamp(now_ms: int) -> str:
    dt = datetime.fromtimestamp(now_ms / 1000, tz=BANGKOK)
    return dt.strftime("%d/%m %H:%M")


def _state_emoji(state: str) -> str:
    if state == "up":
        return "🟢"
    if state == "down":
        return "🔴"
    return "⚪"


def _fee_if_closed(view: dict[str, Any]) -> Decimal:
    if not view.get("ok"):
        return _ZERO
    profit = Decimal(view.get("profit") or 0)
    if profit <= 0:
        return _ZERO
    return Decimal(view.get("fee") or 0)


def _overview_text(conn: sqlite3.Connection, av: AccountValue | AccountValueError, *, now_ms: int) -> str:
    lines: list[str] = [f"พอร์ตร่วม · {_bangkok_stamp(now_ms)}"]
    if not _has_events(conn):
        lines.append("ยังไม่ได้ตั้งต้น")
        if isinstance(av, AccountValue):
            lines.append(f"V {fmt_usdt(av.value)} USDT")
        elif isinstance(av, AccountValueError):
            lines.append(thai_reason(av.reason, detail=av.detail))
        lines.append("พิมพ์ p init")
        return "\n".join(lines)

    v_ok = isinstance(av, AccountValue)
    if isinstance(av, AccountValueError):
        lines.append(thai_reason(av.reason, detail=av.detail))
    else:
        lines.append(
            f"V {fmt_usdt(av.value)} · wallet {fmt_usdt(av.wallet)} · ยังไม่ปิด {fmt_signed_usdt(av.unrealized)}"
        )
        lines.append(f"โพซิชันเปิด {av.open_positions}")
        due = ps.payouts_due_total(conn)
        lines.append(f"ค้างจ่าย {fmt_usdt(due)}")
        u_total = ps.total_units(conn)
        pool = av.value - due
        n = pl.nav(pool, u_total)
        if isinstance(n, pl.Refusal):
            lines.append(thai_reason(n.reason))
        else:
            lines.append(f"NAV {fmt_nav(n)} · หน่วยรวม {fmt_units(u_total)}")

    partners = [p for p in ps.list_partners(conn) if str(p["status"]) == "active"]
    closed_n = sum(1 for p in ps.list_partners(conn) if str(p["status"]) == "closed")
    shown = partners[:_MAX_PARTNERS_SHOWN]
    total_fee = _ZERO
    u_total = ps.total_units(conn)
    account_for_view = av.value if isinstance(av, AccountValue) else _ZERO

    for p in shown:
        pid = str(p["id"])
        name = str(p["name"])
        units = ps.units_from_e8(int(p["units_e8"]))
        cost = ps.usdt_from_micro(int(p["cost_micro"]))
        share = fmt_pct(units, u_total) if u_total > 0 else "—"
        name_line = f"· {name}"
        if p["telegram_user_id"] is not None:
            name_line += " · TG ✅"
        block = [name_line, f"  หน่วย {fmt_units(units)} · ส่วนแบ่ง {share} · ทุน {fmt_usdt(cost)}"]
        if v_ok:
            view = ps.view_partner(conn, pid, account_for_view)
            if view.get("ok"):
                fee = _fee_if_closed(view)
                total_fee += fee
                profit = Decimal(view["profit"])
                if profit > 0:
                    block.append(
                        f"  กำไรก่อนหัก {fmt_usdt(view['gross'])} / ค่าธรรมเนียม {fmt_usdt(view['fee'])} / สุทธิ {fmt_usdt(view['net'])}"
                    )
                else:
                    block.append(f"  มูลค่า {fmt_usdt(view['gross'])}")
                pnl = Decimal(view["net"]) - cost
                pct = fmt_pct(pnl, cost)
                if pct != "—" and pnl > 0 and not pct.startswith("+"):
                    pct = f"+{pct}"
                block.append(f"  {_state_emoji(str(view['state']))} {fmt_signed_usdt(pnl)} ({pct})")
        lines.extend(block)

    if len(partners) > _MAX_PARTNERS_SHOWN:
        lines.append(f"แสดง {_MAX_PARTNERS_SHOWN} จาก {len(partners)}")
    if v_ok:
        lines.append(f"ปิดแล้ว {closed_n} · ค่าธรรมเนียมถ้าปิดทั้งหมด {fmt_usdt(total_fee)}")
    else:
        lines.append(f"ปิดแล้ว {closed_n}")
    pending_n = int(
        conn.execute(
            """
            SELECT COUNT(*) AS n FROM partner_request
            WHERE status = 'pending' AND expires_at_ms > ?
            """,
            (int(now_ms),),
        ).fetchone()["n"]
    )
    lines.append(f"รายการค้าง {pending_n}")
    verify = ps.verify_ledger(conn)
    mism = len(verify.get("mismatches") or [])
    if verify.get("ok"):
        lines.append("✅ ตรงกัน")
    else:
        lines.append(f"⚠️ ไม่ตรง {mism} รายการ")
    return "\n".join(lines)


def _handle_overview(conn: sqlite3.Connection, *, send: Any, futures: Any, now_ms: int) -> dict[str, Any]:
    av = _read_v(futures, now_ms=now_ms)
    send(_overview_text(conn, av, now_ms=now_ms))
    return {"ok": True, "cmd": "overview"}


def _handle_help(*, send: Any) -> dict[str, Any]:
    send(HELP_TEXT)
    return {"ok": True, "cmd": "help"}


def _handle_init(conn: sqlite3.Connection, *, send: Any, futures: Any, now_ms: int) -> dict[str, Any]:
    if _has_events(conn):
        send("ตั้งต้นไปแล้ว")
        return {"ok": False, "reason": "already_init"}
    av = _read_v(futures, now_ms=now_ms)
    if isinstance(av, AccountValueError):
        send(thai_reason(av.reason, detail=av.detail))
        return {"ok": False, "reason": av.reason}
    text = (
        f"ตั้งต้นพอร์ตร่วม\n"
        f"V {fmt_usdt(av.value)} · wallet {fmt_usdt(av.wallet)} · "
        f"ยังไม่ปิด {fmt_signed_usdt(av.unrealized)}\n"
        f"แอดมินถือ 100% · NAV 1.000000\n"
        f"จากนี้ทุกการโอนเข้าหรือออกจาก futures wallet ต้องบันทึก"
    )
    send(text, reply_markup=_init_buttons())
    return {"ok": True, "cmd": "init", "preview": True}


def _handle_add(conn: sqlite3.Connection, name: str, *, send: Any, now_ms: int) -> dict[str, Any]:
    if not _has_events(conn):
        send("ยังไม่ได้ตั้งต้น\nพิมพ์ p init")
        return {"ok": False, "reason": "not_init"}
    if _find_partner_by_name(conn, name) is not None:
        send(f"มีชื่อ {name} อยู่แล้ว")
        return {"ok": False, "reason": "duplicate"}
    try:
        ps.create_partner(conn, name=name, fee_rate_bps=5000, now_ms=now_ms)
    except sqlite3.IntegrityError:
        send(f"มีชื่อ {name} อยู่แล้ว")
        return {"ok": False, "reason": "duplicate"}
    send(f"เพิ่ม {name} แล้ว · ค่าธรรมเนียม 50% · ยังไม่มีเงิน")
    return {"ok": True, "cmd": "add", "name": name}


def _handle_dep(
    conn: sqlite3.Connection,
    name: str,
    amount: Decimal,
    *,
    send: Any,
    futures: Any,
    now_ms: int,
) -> dict[str, Any]:
    if not _has_events(conn):
        send("ยังไม่ได้ตั้งต้น\nพิมพ์ p init")
        return {"ok": False, "reason": "not_init"}
    row = _find_partner_by_name(conn, name)
    if row is None:
        send(thai_reason("not_found"))
        return {"ok": False, "reason": "not_found"}
    if str(row["status"]) != "active":
        send(thai_reason("closed"))
        return {"ok": False, "reason": "closed"}
    partner_id = str(row["id"])
    pending = _pending_request(conn, partner_id, now_ms=now_ms)
    if pending is not None:
        pend_kind = str(pending["kind"])
        if pend_kind != "deposit":
            send(f"{name} มีคำขอ{kind_th(pend_kind)}ค้างอยู่ — ตัดสินอันนั้นก่อน")
            return {"ok": False, "reason": "request_pending"}
        amt = ps.usdt_from_micro(int(pending["quote_gross_micro"] or 0))
        send(
            f"มีรายการฝากค้างอยู่ {fmt_usdt(amt)} — ยืนยันหรือยกเลิกอันเดิมก่อน",
            reply_markup=_deposit_buttons(str(pending["id"])),
        )
        return {"ok": False, "reason": "request_pending"}

    av = _read_v(futures, now_ms=now_ms)
    if isinstance(av, AccountValueError):
        send(thai_reason(av.reason, detail=av.detail))
        return {"ok": False, "reason": av.reason}

    due = ps.payouts_due_total(conn)
    u_total = ps.total_units(conn)
    cost = ps.usdt_from_micro(int(row["cost_micro"]))
    units = ps.units_from_e8(int(row["units_e8"]))
    plan = pl.plan_deposit(
        amount=amount,
        account_value_incl=av.value - due,
        total_units=u_total,
        cost=cost,
        units=units,
    )
    if isinstance(plan, pl.Refusal):
        send(thai_reason(plan.reason))
        return {"ok": False, "reason": plan.reason}

    created = ps.create_request(conn, partner_id, "deposit", now_ms, amount=amount)
    if not created.get("ok"):
        reason = str(created.get("reason") or "")
        if reason == "request_pending":
            pending2 = _pending_request(conn, partner_id, now_ms=now_ms)
            if pending2 is not None:
                amt = ps.usdt_from_micro(int(pending2["quote_gross_micro"] or 0))
                send(
                    f"มีรายการฝากค้างอยู่ {fmt_usdt(amt)} — ยืนยันหรือยกเลิกอันเดิมก่อน",
                    reply_markup=_deposit_buttons(str(pending2["id"])),
                )
                return {"ok": False, "reason": "request_pending"}
        send(thai_reason(reason))
        return {"ok": False, "reason": reason}

    rid = str(created["request_id"])
    lines = [
        f"ฝาก {fmt_usdt(amount)} · ต้องเข้า futures wallet แล้ว",
        f"V {fmt_usdt(av.value)} · NAV ก่อนฝาก {fmt_nav(plan.nav_pre)}",
        f"หน่วยโดยประมาณ {fmt_units(plan.units_issued)} · ทุนหลัง {fmt_usdt(plan.cost_after)}",
        "ตัวเลขจริงคิดใหม่ตอนกดยืนยัน · หมดอายุใน 24 ชม.",
    ]
    last = _latest_nav(conn)
    if last is not None and plan.nav_pre > 0 and last[0] > 0:
        ratio = abs(plan.nav_pre / last[0] - _ONE)
        if ratio > _NAV_WARN:
            lines.append(f"⚠️ NAV ก่อนฝากห่างจากครั้งล่าสุด {fmt_nav(last[0])} ({_bangkok_stamp(last[1])})")
    send("\n".join(lines), reply_markup=_deposit_buttons(rid))
    return {"ok": True, "cmd": "dep", "request_id": rid}


def _handle_cost(
    conn: sqlite3.Connection,
    name: str,
    new_cost: Decimal,
    reason: str,
    *,
    send: Any,
    futures: Any,
    now_ms: int,
) -> dict[str, Any]:
    if not _has_events(conn):
        send("ยังไม่ได้ตั้งต้น\nพิมพ์ p init")
        return {"ok": False, "reason": "not_init"}
    if len(reason.strip()) < 10:
        send(thai_reason("reason_short"))
        return {"ok": False, "reason": "reason_short"}
    row = _find_partner_by_name(conn, name)
    if row is None:
        send(thai_reason("not_found"))
        return {"ok": False, "reason": "not_found"}
    if str(row["status"]) != "active":
        send(thai_reason("closed"))
        return {"ok": False, "reason": "closed"}
    old_cost = ps.usdt_from_micro(int(row["cost_micro"]))
    if old_cost == new_cost:
        send("ทุนเท่าเดิม ไม่บันทึก")
        return {"ok": False, "reason": "unchanged"}

    av = _read_v(futures, now_ms=now_ms)
    if isinstance(av, AccountValueError):
        send(thai_reason(av.reason, detail=av.detail))
        return {"ok": False, "reason": av.reason}

    fee_before = _fee_if_closed(ps.view_partner(conn, str(row["id"]), av.value))
    result = ps.apply_adjust_cost(conn, str(row["id"]), new_cost, reason.strip(), av.value, now_ms)
    if not result.get("ok"):
        send(thai_reason(str(result.get("reason") or "")))
        return {"ok": False, "reason": result.get("reason")}
    view_after = ps.view_partner(conn, str(row["id"]), av.value)
    fee_after = _fee_if_closed(view_after)
    new_c = Decimal(result["cost_after"])
    send(
        f"ปรับทุน {name}\n"
        f"ทุน {fmt_usdt(old_cost)} → {fmt_usdt(new_c)}\n"
        f"ค่าธรรมเนียมถ้าปิดตอนนี้ {fmt_usdt(fee_before)} → {fmt_usdt(fee_after)}"
    )
    refreshed = ps.get_partner(conn, str(row["id"]))
    if refreshed is not None and refreshed["telegram_user_id"] is not None:
        send_to_partner(
            send,
            refreshed,
            f"แอดมินปรับทุนของคุณ {fmt_usdt(old_cost)} → {fmt_usdt(new_c)} USDT\nเหตุผล: {reason.strip()}",
        )
    return {"ok": True, "cmd": "cost"}


def _handle_invite(conn: sqlite3.Connection, name: str, *, send: Any, now_ms: int) -> dict[str, Any]:
    if not _has_events(conn):
        send("ยังไม่ได้ตั้งต้น\nพิมพ์ p init")
        return {"ok": False, "reason": "not_init"}
    row = _find_partner_by_name(conn, name)
    if row is None:
        send(thai_reason("not_found"))
        return {"ok": False, "reason": "not_found"}
    if str(row["status"]) == "closed":
        send(thai_reason("closed"))
        return {"ok": False, "reason": "closed"}
    if int(row["is_admin"]) == 1:
        send("แอดมินไม่ใช้ลิงก์เชิญ")
        return {"ok": False, "reason": "admin_no_invite"}
    if row["telegram_user_id"] is not None:
        send(f"{name} ผูกอยู่แล้ว — พิมพ์ p unbind {name} ก่อน")
        return {"ok": False, "reason": "already_bound"}
    out = ps.create_invite(conn, str(row["id"]), now_ms=now_ms)
    if not out.get("ok"):
        send(thai_reason(str(out.get("reason") or "")))
        return {"ok": False, "reason": out.get("reason")}
    expires_stamp = _bangkok_stamp(int(out["expires_at_ms"]))
    send(
        f"เชิญ {name}\n"
        f"ให้ {name} เปิดแชทส่วนตัวกับบอทนี้ แล้วพิมพ์\n"
        f"/start {out['code']}\n"
        f"ใช้ได้ครั้งเดียว · หมดอายุ {expires_stamp}\n"
        f"อย่าส่งต่อให้คนอื่น"
    )
    return {"ok": True, "cmd": "invite"}


def _handle_unbind(conn: sqlite3.Connection, name: str, *, send: Any, now_ms: int) -> dict[str, Any]:
    row = _find_partner_by_name(conn, name)
    if row is None:
        send(thai_reason("not_found"))
        return {"ok": False, "reason": "not_found"}
    ps.unbind_partner(conn, str(row["id"]), now_ms=now_ms)
    send(f"ยกเลิกการผูก {name} แล้ว")
    return {"ok": True, "cmd": "unbind"}


def handle_partner_text(
    conn: sqlite3.Connection,
    text: str,
    *,
    message: dict[str, Any],
    send: Any,
    futures: Any = None,
    now_ms: int,
) -> dict[str, Any]:
    try:
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        from_user = message.get("from") if isinstance(message.get("from"), dict) else {}
        if not is_admin_private(chat, from_user):
            send(_GATE_MSG)
            return {"ok": False, "refused": "not_private"}
        parsed = parse_partner_command(text)
        if parsed is None:
            return {"ok": False, "reason": "not_partner"}
        if parsed.get("error") == "usage":
            send(HELP_TEXT)
            return {"ok": False, "reason": "usage"}
        cmd = str(parsed.get("cmd") or "")
        if cmd == "overview":
            return _handle_overview(conn, send=send, futures=futures, now_ms=now_ms)
        if cmd == "help":
            return _handle_help(send=send)
        if cmd == "init":
            return _handle_init(conn, send=send, futures=futures, now_ms=now_ms)
        if cmd == "add":
            return _handle_add(conn, str(parsed["name"]), send=send, now_ms=now_ms)
        if cmd == "dep":
            return _handle_dep(
                conn,
                str(parsed["name"]),
                Decimal(parsed["amount"]),
                send=send,
                futures=futures,
                now_ms=now_ms,
            )
        if cmd == "cost":
            return _handle_cost(
                conn,
                str(parsed["name"]),
                Decimal(parsed["new_cost"]),
                str(parsed["reason"]),
                send=send,
                futures=futures,
                now_ms=now_ms,
            )
        if cmd == "invite":
            return _handle_invite(conn, str(parsed["name"]), send=send, now_ms=now_ms)
        if cmd == "unbind":
            return _handle_unbind(conn, str(parsed["name"]), send=send, now_ms=now_ms)
        send(HELP_TEXT)
        return {"ok": False, "reason": "usage"}
    except Exception:  # noqa: BLE001
        logger.exception("partner_admin text failed")
        try:
            send(_FAIL_MSG)
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "reason": "exception"}


def handle_partner_callback(
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
    answered = False

    def _answer(text: str | None = None) -> None:
        nonlocal answered
        if not answered:
            answer(str(callback.get("id") or ""), text=text)
            answered = True

    try:
        raw_msg = callback.get("message")
        msg: dict[str, Any] = raw_msg if isinstance(raw_msg, dict) else {}
        raw_chat = msg.get("chat")
        chat: dict[str, Any] = raw_chat if isinstance(raw_chat, dict) else {}
        raw_from = callback.get("from")
        from_user: dict[str, Any] = raw_from if isinstance(raw_from, dict) else {}
        if not is_admin_private(chat, from_user):
            _answer(_GATE_MSG)
            return {"ok": False, "refused": "not_private"}

        mid = msg.get("message_id")
        if data == "pa:in":
            _answer()
            if mid is not None:
                edit(int(mid), "ยกเลิกแล้ว", reply_markup={"inline_keyboard": []})
            return {"ok": True, "cmd": "init_cancel"}

        if data == "pa:iy":
            if _has_events(conn):
                _answer("ตั้งต้นไปแล้ว")
                if mid is not None:
                    edit(int(mid), str(msg.get("text") or "ตั้งต้นไปแล้ว"), reply_markup={"inline_keyboard": []})
                return {"ok": False, "reason": "already_init"}
            av = _read_v(futures, now_ms=now_ms)
            if isinstance(av, AccountValueError):
                _answer(thai_reason(av.reason, detail=av.detail))
                return {"ok": False, "reason": av.reason}
            admin = ps.admin_partner(conn)
            if admin is not None and ps.units_from_e8(int(admin["units_e8"])) == 0:
                admin_id = str(admin["id"])
            else:
                created = ps.create_partner(conn, name="admin", is_admin=True, now_ms=now_ms)
                admin_id = str(created["partner_id"])
            result = ps.apply_deposit(conn, admin_id, av.value, av.value, now_ms)
            if not result.get("ok"):
                _answer(thai_reason(str(result.get("reason") or "")))
                return {"ok": False, "reason": result.get("reason")}
            _answer()
            body = (
                f"✅ ตั้งต้นแล้ว\n"
                f"หน่วย {fmt_units(Decimal(result.get('units_issued') or av.value))} · "
                f"ทุน {fmt_usdt(Decimal(result.get('cost_after') or av.value))}\n"
                f"NAV 1.000000"
            )
            if mid is not None:
                edit(int(mid), body, reply_markup={"inline_keyboard": []})
            return {"ok": True, "cmd": "init"}

        if data.startswith("pa:dn:"):
            rid = data[len("pa:dn:") :]
            if not _REQ_ID_RE.match(rid):
                _answer("รายการนี้ปิดไปแล้ว")
                return {"ok": False, "reason": "bad_id"}
            req = conn.execute("SELECT * FROM partner_request WHERE id = ?", (rid,)).fetchone()
            if req is None:
                _answer("รายการนี้ปิดไปแล้ว")
                return {"ok": False, "reason": "request_not_pending"}
            status = str(req["status"])
            expired = status == "expired" or (status == "pending" and int(req["expires_at_ms"]) <= now_ms)
            if expired:
                _answer("รายการหมดอายุแล้ว — ส่ง p dep ใหม่")
                if mid is not None:
                    edit(int(mid), str(msg.get("text") or ""), reply_markup={"inline_keyboard": []})
                return {"ok": False, "reason": "expired"}
            if status != "pending":
                _answer("รายการนี้ปิดไปแล้ว")
                if mid is not None:
                    edit(int(mid), str(msg.get("text") or ""), reply_markup={"inline_keyboard": []})
                return {"ok": False, "reason": "request_not_pending"}
            if str(req["kind"]) != "deposit":
                _answer("ไม่ใช่รายการฝาก")
                return {"ok": False, "reason": "not_deposit"}
            rejected = ps.reject_request(conn, rid, now_ms)
            if rejected.get("ok"):
                _answer()
                if mid is not None:
                    edit(int(mid), "ยกเลิกแล้ว", reply_markup={"inline_keyboard": []})
                return {"ok": True, "cmd": "dep_cancel"}
            _answer("รายการนี้ปิดไปแล้ว")
            if mid is not None:
                edit(int(mid), str(msg.get("text") or ""), reply_markup={"inline_keyboard": []})
            return {"ok": False, "reason": "request_not_pending"}

        if data.startswith("pa:dy:"):
            rid = data[len("pa:dy:") :]
            if not _REQ_ID_RE.match(rid):
                _answer("รายการนี้ปิดไปแล้ว")
                return {"ok": False, "reason": "bad_id"}
            req = conn.execute("SELECT * FROM partner_request WHERE id = ?", (rid,)).fetchone()
            if req is None:
                _answer("รายการนี้ปิดไปแล้ว")
                return {"ok": False, "reason": "request_not_pending"}
            status = str(req["status"])
            expired = status == "expired" or (status == "pending" and int(req["expires_at_ms"]) <= now_ms)
            if expired:
                _answer("รายการหมดอายุแล้ว — ส่ง p dep ใหม่")
                if mid is not None:
                    edit(int(mid), str(msg.get("text") or ""), reply_markup={"inline_keyboard": []})
                return {"ok": False, "reason": "expired"}
            if status != "pending":
                _answer("รายการนี้ปิดไปแล้ว")
                if mid is not None:
                    edit(int(mid), str(msg.get("text") or ""), reply_markup={"inline_keyboard": []})
                return {"ok": False, "reason": "request_not_pending"}
            if str(req["kind"]) != "deposit":
                _answer("ไม่ใช่รายการฝาก")
                return {"ok": False, "reason": "not_deposit"}
            av = _read_v(futures, now_ms=now_ms)
            if isinstance(av, AccountValueError):
                _answer(thai_reason(av.reason, detail=av.detail))
                return {"ok": False, "reason": av.reason}
            amount = ps.usdt_from_micro(int(req["quote_gross_micro"] or 0))
            result = ps.confirm_request(conn, rid, av.value, now_ms, amount_received=amount)
            if result.get("ok"):
                _answer()
                body = (
                    f"✅ บันทึกฝากแล้ว\n"
                    f"จำนวน {fmt_usdt(amount)} · หน่วย {fmt_units(Decimal(result.get('units_issued') or 0))}\n"
                    f"NAV ก่อนฝาก {fmt_nav(Decimal(result.get('nav_pre') or 0))} · "
                    f"ทุนหลัง {fmt_usdt(Decimal(result.get('cost_after') or 0))}"
                )
                if mid is not None:
                    edit(int(mid), body, reply_markup={"inline_keyboard": []})
                return {"ok": True, "cmd": "dep_confirm"}
            reason = str(result.get("reason") or "")
            if reason == "request_not_pending":
                _answer("รายการนี้ปิดไปแล้ว")
                return {"ok": False, "reason": reason}
            _answer(thai_reason(reason))
            return {"ok": False, "reason": reason}

        _answer()
        return {"ok": False, "reason": "unknown"}
    except Exception:  # noqa: BLE001
        logger.exception("partner_admin callback failed")
        try:
            _answer(_FAIL_MSG)
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "reason": "exception"}
