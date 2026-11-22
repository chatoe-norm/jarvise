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
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jarvise_assist import partner_fx as fx
from jarvise_assist import partner_kyc as pk
from jarvise_assist import partner_ledger as pl
from jarvise_assist import partner_rules
from jarvise_assist import partner_store as ps
from jarvise_assist import partner_thb as thb
from jarvise_assist.futures import build_futures_client
from jarvise_assist.partner_value import (
    AccountValue,
    AccountValueError,
    read_account_value,
)

if TYPE_CHECKING:
    from jarvise_assist.partner_audit import GuardResult

logger = logging.getLogger(__name__)

BANGKOK = timezone(timedelta(hours=7), name="Asia/Bangkok")

_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-\u0E00-\u0E7F]{1,24}$")
_AMOUNT_RE = re.compile(r"^(?:[0-9]{1,9}(?:\.[0-9]{1,6})?|[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]{1,6})?)$")
_CMD_RE = re.compile(r"^\s*p(\s|$)", re.IGNORECASE)
_REQ_ID_RE = re.compile(r"^pr_[0-9a-f]{16}$")
_GUARD_FORCE_RE = re.compile(r"^(pr_[0-9a-f]{16}):([0-9]{10})$")
_GUARD_OVERRIDE_S = 900
_GUARD_FUTURE_S = 60
_GUARD_MARKER = "\n\n⚠️ ตรวจยอดโอน"

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
    "p unbind <ชื่อ> — ยกเลิกการผูก Telegram\n"
    "p cv <ชื่อ> — สร้างคำขอทบกำไร\n"
    "p wd <ชื่อ> — สร้างคำขอถอนกำไร\n"
    "p close <ชื่อ> — สร้างคำขอปิดพอร์ต\n"
    "p closed — พาร์ทเนอร์ที่ปิดแล้ว\n"
    "p due — รายการค้างจ่ายและคำขอค้าง\n"
    "p paid <เป้าหมาย> sold= rate= trade= net= — บันทึกโอนเงินบาท (ยืนยันก่อน)\n"
    "p fix <#รหัส> — แก้ไขรายการเงินบาทที่บันทึกแล้ว (ยืนยันก่อน)\n"
    "p kyc — สถานะข้อมูลรับเงิน\n"
    "p kyc <ชื่อ> — ดูข้อมูลรับเงินเต็ม\n"
    "p kyc <ชื่อ> set <เหตุผล> — แก้ข้อมูลรับเงิน (บรรทัดถัดไปเป็นฟอร์ม)\n"
    "เงินเข้าหรือออกของเจ้าของบันทึกเป็น owner_deposit / owner_redeem "
    "(จัดประเภทใน L2) — ห้ามบันทึกเงินก้อนนั้นด้วยคำสั่งฝากหรือถอนของแอดมิน"
)

_HELP = HELP_TEXT

_GATE_MSG = "คำสั่งพอร์ตร่วมใช้ได้เฉพาะแชทส่วนตัวกับบอท"
_FAIL_MSG = "ทำไม่สำเร็จ — ยังไม่บันทึกอะไร"

_STORE_TH: dict[str, str] = {
    "not_found": "ไม่พบพาร์ทเนอร์",
    "closed": "พาร์ทเนอร์ปิดแล้ว",
    "duplicate_name": "มีชื่อนี้แล้ว — ใช้ชื่อใหม่",
    "bad_name": "ชื่อว่างไม่ได้",
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
    "admin_has_partners": "ปิดแอดมินไม่ได้ ขณะที่ยังมีพาร์ทเนอร์ถือหน่วย",
    "no_profit": "ยังไม่มีกำไร",
    "below_min": "กำไรน้อยเกินไป",
    "not_due": "รายการนี้ปิดไปแล้ว",
    "payout_due": "มียอดค้างจ่าย — โอน USDT ออกแล้วกด 📤 ก่อน (p due)",
    "kyc_required": "ยังไม่มีข้อมูลรับเงิน",
    "kyc_locked": "บันทึกข้อมูลรับเงินแล้ว แก้ไขเองไม่ได้",
    "kyc_draft_expired": "ฟอร์มหมดอายุ — ให้ส่งใหม่",
    "kyc_invalid": "ข้อมูลรับเงินไม่ครบหรือไม่ถูกต้อง",
    "kyc_key": "ยังไม่ได้ตั้งคีย์ข้อมูลรับเงิน",
    "admin_exempt": "แอดมินไม่ต้องกรอก",
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
        result = send(text, reply_markup=reply_markup, chat_id=str(tg), protect_content=True)
        return result is not None and result is not False
    except Exception as exc:  # noqa: BLE001
        logger.warning("partner send failed: %s", type(exc).__name__)
        return False


def _parse_kyc_command(parts: list[str], rest: str) -> dict[str, Any]:
    """First line only. The form is the remaining lines and is not split()."""
    if len(parts) == 2:
        return {"cmd": "kyc_list"}
    if len(parts) == 3 and _NAME_RE.match(parts[2]):
        return {"cmd": "kyc_show", "name": parts[2]}
    if len(parts) >= 5 and parts[3].lower() == "set" and _NAME_RE.match(parts[2]):
        return {"cmd": "kyc_set", "name": parts[2], "reason": " ".join(parts[4:]), "form": rest}
    return {"error": "usage"}


def parse_partner_command(text: str) -> dict[str, Any] | None:
    raw = text or ""
    if not _CMD_RE.match(raw):
        return None
    first, _, rest = raw.partition("\n")
    first_parts = first.strip().split()
    if len(first_parts) >= 2 and first_parts[1].lower() == "kyc":
        return _parse_kyc_command(first_parts, rest)
    if len(first_parts) >= 2 and first_parts[1].lower() in ("paid", "fix", "carry"):
        return {"cmd": first_parts[1].lower(), "raw": raw}
    parts = raw.strip().split()
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
    if head == "cv":
        if len(parts) != 3 or not _NAME_RE.match(parts[2]):
            return {"error": "usage"}
        return {"cmd": "cv", "name": parts[2]}
    if head == "wd":
        if len(parts) != 3 or not _NAME_RE.match(parts[2]):
            return {"error": "usage"}
        return {"cmd": "wd", "name": parts[2]}
    if head == "close":
        if len(parts) != 3 or not _NAME_RE.match(parts[2]):
            return {"error": "usage"}
        return {"cmd": "close", "name": parts[2]}
    if head == "closed" and len(parts) == 2:
        return {"cmd": "closed"}
    if head == "due" and len(parts) == 2:
        return {"cmd": "due"}
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
    key = ps.name_key(name)
    if not key:
        return None
    for row in ps.list_partners(conn):
        if ps.name_key(str(row["name"])) == key:
            return row
    return None


def _name_taken_text(name: str, *, closed: bool) -> str:
    if closed:
        return f"{name} ปิดแล้ว — ใช้ชื่อนี้ซ้ำไม่ได้\nเปิดบัญชีใหม่ด้วยชื่อใหม่ (p add <ชื่อใหม่>)"
    return f"มีชื่อ {name} อยู่แล้ว — ใช้ชื่อใหม่"


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
        SELECT nav, COALESCE(effective_at_ms, created_at_ms) AS t
        FROM partner_event
        ORDER BY t DESC, rowid DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    try:
        return Decimal(str(row["nav"])), int(row["t"])
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


def _request_buttons(request_id: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ ยืนยัน", "callback_data": f"pa:ry:{request_id}"},
                {"text": "✖️ ปฏิเสธ", "callback_data": f"pa:rn:{request_id}"},
            ]
        ]
    }


def _guard_deposit_buttons(rid: str, ts_s: int) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "⚠️ ยืนยันทั้งที่มีเตือน", "callback_data": f"pa:df:{rid}:{ts_s}"}],
            [{"text": "✖️ ยกเลิก", "callback_data": f"pa:dn:{rid}"}],
        ]
    }


def _guard_request_buttons(rid: str, ts_s: int) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "⚠️ ยืนยันทั้งที่มีเตือน", "callback_data": f"pa:rf:{rid}:{ts_s}"}],
            [{"text": "✖️ ปฏิเสธ", "callback_data": f"pa:rn:{rid}"}],
        ]
    }


def _paid_button(request_id: str) -> dict[str, Any]:
    return {"inline_keyboard": [[{"text": "📤 USDT ออกจากพอร์ตแล้ว", "callback_data": f"pa:pd:{request_id}"}]]}


def _transfer_guard(
    conn: sqlite3.Connection,
    client: Any,
    *,
    now_ms: int,
    req: sqlite3.Row | dict[str, Any],
) -> tuple[GuardResult, str]:
    """Run confirm-time transfer guard. Lazy-imports partner_audit (never at module load)."""
    from jarvise_assist.partner_audit import GuardResult as _GuardResult
    from jarvise_assist.partner_audit import format_guard_th, guard_check, load_state

    try:
        deposit_request_id: str | None = None
        deposit_amount: Decimal | None = None
        deposit_partner: str | None = None
        if str(req["kind"]) == "deposit":
            deposit_request_id = str(req["id"])
            deposit_amount = ps.usdt_from_micro(int(req["quote_gross_micro"] or 0))
            partner = ps.get_partner(conn, str(req["partner_id"]))
            deposit_partner = str(partner["name"]) if partner is not None else "?"
        result = guard_check(
            conn,
            client,
            now_ms=int(now_ms),
            deposit_request_id=deposit_request_id,
            deposit_amount=deposit_amount,
            deposit_partner=deposit_partner,
        )
        last_ok_ms: int | None = None
        if result.status != "ok":
            row = conn.execute("PRAGMA database_list").fetchone()
            db_file = ""
            if row is not None:
                db_file = str(row["file"] if hasattr(row, "keys") else row[2] or "")
            if db_file:
                state = load_state(Path(db_file).with_name("partner_audit_state.json"))
                lok = state.get("last_ok_ms")
                last_ok_ms = int(lok) if lok is not None else None
        return result, format_guard_th(result, last_ok_ms=last_ok_ms)
    except Exception as exc:  # noqa: BLE001
        logger.warning("partner transfer guard error type=%s", type(exc).__name__)
        err = _GuardResult(
            status="error",
            checked_at_ms=int(now_ms),
            findings=[],
            warn=True,
            error=type(exc).__name__,
        )
        return err, format_guard_th(err, last_ok_ms=None)


def _should_run_guard(*, force: bool, ts_s: int | None, now_ms: int) -> bool:
    if not force:
        return True
    if ts_s is None:
        return True
    now_s = int(now_ms) // 1000
    if int(ts_s) > now_s + _GUARD_FUTURE_S:
        return True
    if now_s - int(ts_s) > _GUARD_OVERRIDE_S:
        return True
    return False


def _merge_guard_text(orig: str, guard_text: str) -> str:
    base = orig
    idx = base.find(_GUARD_MARKER)
    if idx >= 0:
        base = base[:idx]
    return base + "\n\n" + guard_text


def _guard_ok_suffix(result: GuardResult) -> str:
    if result.status == "not_init":
        return ""
    info_n = sum(1 for f in result.findings if f.code in ("near_amount", "non_usdt"))
    line = "\n✅ ตรวจยอดโอนแล้ว ไม่พบรายการค้าง"
    if info_n:
        line += f" (มีหมายเหตุ {info_n} — ดู partner-audit)"
    return line


def _settled_view(conn: sqlite3.Connection, rid: str) -> tuple[str, dict[str, Any]]:
    """Render admin message text from the DB row (never from callback message.text)."""
    empty: dict[str, Any] = {"inline_keyboard": []}
    req = ps.get_request(conn, rid)
    if req is None:
        return "รายการนี้ปิดไปแล้ว", empty
    partner = ps.get_partner(conn, str(req["partner_id"]))
    pname = str(partner["name"]) if partner is not None else "?"
    status = str(req["status"])
    kind = str(req["kind"])
    payout_state = str(req["payout_state"] or "none")

    if status == "rejected":
        if kind == "deposit":
            return "ยกเลิกแล้ว", empty
        return "ปฏิเสธแล้ว", empty
    if status == "cancelled":
        return "ยกเลิกอัตโนมัติ — ไม่มีกำไรแล้ว ณ ตอนยืนยัน", empty
    if status == "confirmed":
        if payout_state == "due":
            x = ps.usdt_from_micro(int(req["payout_micro"] or 0))
            body = (
                f"✅ ยืนยันแล้ว · ค้างจ่าย {fmt_usdt(x)} USDT ให้ {pname}\nโอน USDT ออกจาก futures wallet แล้วกด 📤"
            )
            body += "\n" + pk.payout_account_line(
                conn,
                partner_id=str(req["partner_id"]),
                payout_kyc_id=req["payout_kyc_id"],
                partner_name=pname,
            )
            return body, _paid_button(rid)
        if payout_state == "paid":
            x = ps.usdt_from_micro(int(req["payout_micro"] or 0))
            is_admin = partner is not None and int(partner["is_admin"] or 0) == 1
            recorded = fx.current_fx(conn, rid)
            paid_at = req["paid_at_ms"]
            cash_out = str(kind) in ("withdraw_profit", "close") and int(req["payout_micro"] or 0) > 0
            if not is_admin and recorded is not None:
                return (
                    f"💸 บันทึกโอนเงินบาทแล้ว · {pname} · "
                    f"{thb.fmt_thb(int(recorded['net_satang']))} บาท (#{thb.short_id(rid)})"
                ), empty
            if not is_admin and fx.unsellable(conn, req):
                return f"💸 โอนแล้ว {fmt_usdt(x)} USDT ให้ {pname}", empty
            if (
                not is_admin
                and cash_out
                and paid_at is not None
                and int(paid_at) >= int(fx.THB_FLOW_SINCE_MS)
            ):
                return (
                    f"📤 USDT ออกจากพอร์ตแล้ว · {pname} · {fmt_usdt(x)} USDT · "
                    f"รอบันทึกเงินบาท (p paid #{thb.short_id(rid)} …)"
                ), empty
            return f"💸 โอนแล้ว {fmt_usdt(x)} USDT ให้ {pname}", empty
        if kind == "deposit":
            x = ps.usdt_from_micro(int(req["quote_gross_micro"] or 0))
            return f"✅ บันทึกฝากแล้ว · {pname} {fmt_usdt(x)} USDT", empty
        if kind == "convert":
            return f"✅ ทบกำไร {pname} แล้ว", empty
        return f"✅ ยืนยันแล้ว · ปิด {pname} แล้ว (ไม่มีเงินออก)", empty
    return "รายการนี้ปิดไปแล้ว", empty


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
        if due > 0:
            lines.append(f"⚠️ ค้างจ่าย {fmt_usdt(due)} — ถ้าโอน USDT ออกแล้วให้กด 📤 (p due)")
        u_total = ps.total_units(conn)
        pool = av.value - due
        n = pl.nav(pool, u_total)
        if isinstance(n, pl.Refusal):
            lines.append(thai_reason(n.reason))
        else:
            lines.append(f"NAV {fmt_nav(n)} · หน่วยรวม {fmt_units(u_total)}")

    waiting = fx.awaiting_thb(conn)
    if waiting:
        lines.append(f"รอบันทึกเงินบาท {len(waiting)} รายการ (p due)")

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
        joined = partner_rules.joined_at_ms(conn, pid)
        if joined is not None:
            name_line += f" · เข้าร่วม {partner_rules.fmt_date(joined)}"
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
    existing = _find_partner_by_name(conn, name)
    if existing is not None:
        send(_name_taken_text(str(existing["name"]), closed=str(existing["status"]) == "closed"))
        return {"ok": False, "reason": "duplicate_name"}
    try:
        created = ps.create_partner(conn, name=name, fee_rate_bps=5000, now_ms=now_ms)
    except sqlite3.IntegrityError:
        again = _find_partner_by_name(conn, name)
        label = str(again["name"]) if again is not None else name
        closed = again is not None and str(again["status"]) == "closed"
        send(_name_taken_text(label, closed=closed))
        return {"ok": False, "reason": "duplicate_name"}
    if not created.get("ok"):
        again = _find_partner_by_name(conn, name)
        closed = again is not None and str(again["status"]) == "closed"
        reason = str(created.get("reason") or "duplicate_name")
        label = str(again["name"]) if again is not None else name
        send(_name_taken_text(label, closed=closed) if reason == "duplicate_name" else thai_reason(reason))
        return {"ok": False, "reason": reason}
    send(f"เพิ่ม {name} แล้ว · ค่าธรรมเนียม 50% · ยังไม่มีเงิน")
    return {"ok": True, "cmd": "add", "name": name}


def _kyc_set_hint(name: str) -> str:
    return f"ให้พาร์ทเนอร์ส่งข้อมูลรับเงิน หรือ p kyc {name} set <เหตุผล>\nแล้วขึ้นบรรทัดใหม่ใส่ฟอร์ม"


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
    if ps.deposit_kyc_required(conn, row):
        stored = str(row["name"])
        send("ยังไม่มีข้อมูลรับเงิน\n" + _kyc_set_hint(stored))
        return {"ok": False, "reason": "kyc_required"}
    pending = _pending_request(conn, partner_id, now_ms=now_ms)
    if pending is not None:
        pend_kind = str(pending["kind"])
        if pend_kind != "deposit":
            send(
                f"{name} มีคำขอ{kind_th(pend_kind)}ค้างอยู่ — ตัดสินอันนั้นก่อน",
                reply_markup=_request_buttons(str(pending["id"])),
            )
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

    created = ps.create_request(conn, partner_id, "deposit", now_ms, amount=amount, source="admin")
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
        pk.deposit_kyc_line(conn, partner_id),
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
        # Never reopen. A new account is p add with a new name; a due payout stays due.
        stored = str(row["name"])
        open_rows = _due_for(conn, str(row["id"]))
        if open_rows:
            if any(item.get("stage") == "due" for item in open_rows):
                send(
                    f"{stored} ปิดแล้วและยังรอโอน — ออกลิงก์ให้บัญชีเดิมไม่ได้\n"
                    f"โอนให้ครบก่อน แล้วเปิดบัญชีใหม่ด้วยชื่อใหม่ (p add <ชื่อใหม่>)"
                )
            else:
                send(
                    f"{stored} ปิดแล้วและยังรอบันทึกเงินบาท — ออกลิงก์ให้บัญชีเดิมไม่ได้\n"
                    f"บันทึกให้ครบก่อน แล้วเปิดบัญชีใหม่ด้วยชื่อใหม่ (p add <ชื่อใหม่>)"
                )
            return {"ok": False, "reason": "payout_due"}
        send(f"{stored} ปิดแล้ว — ออกลิงก์ให้บัญชีเดิมไม่ได้\nเปิดบัญชีใหม่ด้วยชื่อใหม่ (p add <ชื่อใหม่>)")
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


def _due_for(conn: sqlite3.Connection, partner_id: str) -> list[dict[str, Any]]:
    """Due USDT, awaiting THB, and leftover carry. Read-only."""
    return fx.open_settlements(conn, str(partner_id))


def _warn_send_fail(name: str) -> str:
    return f"⚠️ แจ้งพาร์ทเนอร์ไม่สำเร็จ — ยังไม่ตัดการผูก (p unbind {name})"


def _warn_send_fail_due() -> str:
    return "⚠️ แจ้งพาร์ทเนอร์ไม่สำเร็จ — ยังไม่ตัดการผูก"


def _warn_still_due() -> str:
    return "⚠️ ยังมียอดค้างจ่าย — ยังไม่ตัดการผูก (โอนให้ครบก่อน)"


def _close_fact_lines(conn: sqlite3.Connection, partner_id: str) -> list[str]:
    """Join date, close date, and close amount. Empty when the partner is missing."""
    summary = ps.close_summary(conn, partner_id)
    if not summary.get("ok"):
        return []
    joined = summary.get("joined_at_ms")
    closed = summary.get("closed_at_ms")
    cash = summary.get("close_cash")
    if not isinstance(cash, Decimal):
        cash = _ZERO
    return [
        "เข้าร่วม " + (partner_rules.fmt_date(int(joined)) if joined is not None else "—"),
        "ปิดเมื่อ " + (partner_rules.fmt_date(int(closed)) if closed is not None else "—"),
        f"ยอดปิด {fmt_usdt(cash)} USDT",
    ]


def _closing_note(conn: sqlite3.Connection, partner_id: str, *, cash: Decimal, fee: Decimal) -> str:
    """Partner-facing close summary. Does not read the futures wallet."""
    lines = ["✅ แอดมินยืนยันปิดพอร์ต", *_close_fact_lines(conn, partner_id)]
    if fee > 0:
        lines.append(f"ค่าธรรมเนียม {fmt_usdt(fee)} USDT")
    if cash <= 0:
        lines.append("ไม่มีเงินออก")
    summary = ps.close_summary(conn, partner_id)
    if summary.get("ok"):
        for due in summary.get("dues") or []:
            lines.append(f"รอโอน {fmt_usdt(Decimal(due['cash']))} USDT")
    snap = conn.execute(
        """
        SELECT payout_kyc_id FROM partner_request
        WHERE partner_id = ? AND kind = 'close' AND status = 'confirmed'
        ORDER BY decided_at_ms DESC, id DESC
        LIMIT 1
        """,
        (partner_id,),
    ).fetchone()
    kyc_id = None if snap is None else snap["payout_kyc_id"]
    account = pk.partner_payout_line(conn, payout_kyc_id=kyc_id, cash=cash)
    if account:
        lines.append(account)
    return "\n".join(lines)


def _release_closed_binding(
    conn: sqlite3.Connection,
    partner: sqlite3.Row,
    *,
    send: Any,
    note: str,
    now_ms: int,
) -> list[str]:
    """Send ``note`` first. Unbind only when that send returned True and nothing is due.

    A partner with no Telegram id, and the admin row, are left as they are: no send
    and no warning. Active partners are notified and left bound.
    """
    tg = partner["telegram_user_id"]
    if tg is None or int(partner["is_admin"] or 0) == 1:
        return []
    sent_ok = send_to_partner(send, partner, note)
    if str(partner["status"]) != "closed":
        return []
    name = str(partner["name"])
    dues = _due_for(conn, str(partner["id"]))
    if sent_ok and not dues:
        ps.unbind_partner(conn, str(partner["id"]), now_ms=now_ms)
        return []
    warnings: list[str] = []
    if not sent_ok:
        warnings.append(_warn_send_fail_due() if dues else _warn_send_fail(name))
    if dues:
        warnings.append(_warn_still_due())
    return warnings


def _append_warnings(body: str, warnings: list[str]) -> str:
    if not warnings:
        return body
    return body + "\n" + "\n".join(warnings)


def _handle_unbind(conn: sqlite3.Connection, name: str, *, send: Any, now_ms: int) -> dict[str, Any]:
    row = _find_partner_by_name(conn, name)
    if row is None:
        send(thai_reason("not_found"))
        return {"ok": False, "reason": "not_found"}
    if str(row["status"]) == "closed":
        open_rows = fx.open_settlements(conn, str(row["id"]))
        if any(item.get("stage") == "due" for item in open_rows):
            send(f"{name} ปิดแล้วและยังมียอดรอโอน — ยกเลิกการผูกไม่ได้")
            return {"ok": False, "reason": "payout_due"}
        if any(item.get("stage") == "awaiting_thb" for item in open_rows):
            send(f"{name} ปิดแล้วและยังรอบันทึกเงินบาท — ยกเลิกการผูกไม่ได้")
            return {"ok": False, "reason": "payout_due"}
        if any(item.get("stage") == "carry_closed" for item in open_rows):
            send(f"{name} ปิดแล้วและยังมีเศษค้าง — ใช้ p carry ก่อน")
            return {"ok": False, "reason": "payout_due"}
    ps.unbind_partner(conn, str(row["id"]), now_ms=now_ms)
    send(f"ยกเลิกการผูก {name} แล้ว")
    return {"ok": True, "cmd": "unbind"}


def _admin_request_message(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    kind: str,
    *,
    send: Any,
    futures: Any,
    now_ms: int,
) -> dict[str, Any]:
    av = _read_v(futures, now_ms=now_ms)
    if isinstance(av, AccountValueError):
        send(thai_reason(av.reason, detail=av.detail))
        return {"ok": False, "reason": av.reason}
    created = ps.create_request(conn, str(row["id"]), kind, now_ms, source="admin")
    if not created.get("ok"):
        send(thai_reason(str(created.get("reason") or "")))
        return {"ok": False, "reason": created.get("reason")}
    rid = str(created["request_id"])
    due = ps.payouts_due_total(conn)
    u_total = ps.total_units(conn)
    n = pl.nav(av.value - due, u_total)
    cash = _ZERO
    fee = _ZERO
    if not isinstance(n, pl.Refusal):
        units = ps.units_from_e8(int(row["units_e8"]))
        cost = ps.usdt_from_micro(int(row["cost_micro"]))
        fee_rate = ps.fee_rate_from_bps(int(row["fee_rate_bps"]))
        if kind == "convert":
            cv = pl.plan_convert(units=units, cost=cost, fee_rate=fee_rate, nav=n)
            if not isinstance(cv, pl.Refusal):
                fee = cv.fee
        elif kind == "withdraw_profit":
            wd = pl.plan_withdraw_profit(units=units, cost=cost, fee_rate=fee_rate, nav=n)
            if not isinstance(wd, pl.Refusal):
                cash, fee = wd.cash, wd.fee
        elif kind == "close":
            cl = pl.plan_close(units=units, cost=cost, fee_rate=fee_rate, nav=n)
            if not isinstance(cl, pl.Refusal):
                cash, fee = cl.cash, cl.fee
    notice = (
        f"📥 คำขอจาก {row['name']}: {kind_th(kind)}\n"
        f"ประมาณการตอนนี้: เงินออก {fmt_usdt(cash)} · ค่าธรรมเนียม {fmt_usdt(fee)}"
    )
    if kind in ("withdraw_profit", "close"):
        current = pk.current_kyc(conn, str(row["id"]))
        notice += "\n" + pk.payout_account_line(
            conn,
            partner_id=str(row["id"]),
            payout_kyc_id=None if current is None else str(current["id"]),
            partner_name=str(row["name"]),
        )
    notice += "\nกดยืนยัน = ล็อกตัวเลขตามยอดพอร์ตตอนกด และตั้งเป็นยอดค้างจ่าย\nหมดอายุใน 24 ชม."
    send(notice, reply_markup=_request_buttons(rid))
    return {"ok": True, "cmd": kind, "request_id": rid}


def _handle_cv_wd_close(
    conn: sqlite3.Connection,
    name: str,
    kind: str,
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
    if kind == "close" and int(row["is_admin"]) == 1:
        other = conn.execute(
            """
            SELECT 1 FROM partner
            WHERE id != ? AND status = 'active' AND units_e8 > 0
            LIMIT 1
            """,
            (str(row["id"]),),
        ).fetchone()
        if other is not None:
            send("ปิดแอดมินไม่ได้ ขณะที่ยังมีพาร์ทเนอร์ถือหน่วย")
            return {"ok": False, "reason": "admin_has_partners"}
    return _admin_request_message(conn, row, kind, send=send, futures=futures, now_ms=now_ms)


def _handle_due(conn: sqlite3.Connection, *, send: Any, now_ms: int) -> dict[str, Any]:
    msgs = 0
    remaining = 0
    settlements = fx.open_settlements(conn)
    due_rows = [row for row in settlements if row.get("stage") == "due"]
    awaiting = [row for row in settlements if row.get("stage") == "awaiting_thb"]
    closed_carry = [row for row in settlements if row.get("stage") == "carry_closed"]
    pending_rows = ps.list_pending_requests(conn, now_ms=now_ms)
    units: list[tuple[str, Any]] = [("due", row) for row in due_rows]
    if awaiting:
        units.append(("awaiting", awaiting))
    if closed_carry:
        units.append(("carry", closed_carry))
    units.extend(("pending", row) for row in pending_rows)
    if not units:
        send("ไม่มีรายการค้าง")
        return {"ok": True, "cmd": "due", "n": 0}
    shown = units[:10]
    remaining = len(units) - len(shown)
    for kind, item in shown:
        if kind == "due":
            send(thb.due_line(conn, item, now_ms), reply_markup=_paid_button(str(item["id"])))
        elif kind == "awaiting":
            send(_awaiting_due_text(conn, item, now_ms))
        elif kind == "carry":
            lines = [
                f"เศษค้าง (ปิดแล้ว): {row['partner_name']} {fmt_usdt(ps.usdt_from_micro(int(row['carry_micro'])))} USDT"
                for row in item
            ]
            send("\n".join(lines))
        else:
            _send_pending(conn, item, send=send)
        msgs += 1
    if remaining > 0:
        send(f"อีก {remaining} รายการ")
    return {"ok": True, "cmd": "due", "n": msgs}


def _awaiting_due_text(conn: sqlite3.Connection, rows: list[dict[str, Any]], now_ms: int) -> str:
    header = f"รอบันทึกเงินบาท ({len(rows)})"
    footer = "บันทึก: p paid #รหัส sold= rate= trade= net="
    bits: list[str] = []
    seen: set[str] = set()
    for row in rows:
        bit = (
            f"· #{thb.short_id(str(row['id']))} · {row.get('partner_name') or '?'} · "
            f"{kind_th(str(row['kind']))} {fmt_usdt(ps.usdt_from_micro(int(row['payout_micro'])))} USDT · "
            f"USDT ออก {_bangkok_stamp(int(row.get('paid_at_ms') or now_ms))}"
        )
        pid = str(row["partner_id"])
        if pid not in seen:
            carry = fx.carry_balance(conn, pid)
            if carry > 0:
                bit += f" · เศษยกมา {fmt_usdt(ps.usdt_from_micro(carry))}"
            seen.add(pid)
        bits.append(bit)

    def pack(shown: list[str], omitted: int) -> str:
        lines = [header, *shown]
        if omitted:
            lines.append(f"… อีก {omitted} รายการ")
        lines.append(footer)
        return "\n".join(lines)

    if len(pack(bits, 0)) <= 3500:
        return pack(bits, 0)
    shown: list[str] = []
    for index, bit in enumerate(bits):
        trial = pack(shown + [bit], len(bits) - index - 1)
        if len(trial) > 3500:
            break
        shown.append(bit)
    return pack(shown, len(bits) - len(shown))


def _send_pending(conn: sqlite3.Connection, req: sqlite3.Row, *, send: Any) -> None:
    partner = ps.get_partner(conn, str(req["partner_id"]))
    pname = str(partner["name"]) if partner is not None else "?"
    kind = str(req["kind"])
    if kind == "deposit":
        amt = ps.usdt_from_micro(int(req["quote_gross_micro"] or 0))
        send(
            f"{pname} · ฝาก · {fmt_usdt(amt)} USDT · ค้างยืนยัน",
            reply_markup=_deposit_buttons(str(req["id"])),
        )
        return
    send(
        f"{pname} · {kind_th(kind)} · ค้างยืนยัน",
        reply_markup=_request_buttons(str(req["id"])),
    )


def _closed_sort_key(summary: dict[str, Any]) -> tuple[int, str]:
    raw = summary.get("closed_at_ms")
    stamp = int(raw) if raw is not None else -1
    return (-stamp, str(summary.get("name") or ""))


def _fmt_bkk_date(ms: Any) -> str:
    if ms is None:
        return "—"
    return partner_rules.fmt_date(int(ms))


_CLOSED_TEXT_MAX = 4096


def _closed_status(summary: dict[str, Any]) -> str:
    """Label for the close payout only. A paid profit withdrawal does not count."""
    state = summary.get("close_payout_state")
    cash = summary.get("close_cash")
    if not isinstance(cash, Decimal):
        cash = _ZERO
    if state == "paid":
        if summary.get("close_awaiting_thb"):
            return "USDT ออกแล้ว · รอบันทึกเงินบาท"
        return "โอนแล้ว"
    if state == "due":
        return f"รอโอน {fmt_usdt(cash)} USDT"
    return "ไม่มีเงินออก"


def _closed_partner_line(summary: dict[str, Any]) -> str:
    name = str(summary.get("name") or "")
    cash = summary.get("close_cash")
    if not isinstance(cash, Decimal):
        cash = _ZERO
    closed = _fmt_bkk_date(summary.get("closed_at_ms"))
    return f"· {name} · ปิด {closed} · {fmt_usdt(cash)} USDT · {_closed_status(summary)}"


def _render_closed(summaries: list[dict[str, Any]]) -> tuple[str, int]:
    """Newest first, at most 40 rows, and never longer than a Telegram message."""
    total = len(summaries)
    header = f"พาร์ทเนอร์ที่ปิดแล้ว ({total})"
    lines = [_closed_partner_line(summary) for summary in summaries]
    shown = min(_MAX_PARTNERS_SHOWN, len(lines))
    while True:
        extra = total - shown
        parts = [header, *lines[:shown]]
        if extra > 0:
            parts.append(f"อีก {extra} คน")
        text = "\n".join(parts)
        if len(text) <= _CLOSED_TEXT_MAX or shown == 0:
            return text, shown
        shown -= 1


def _handle_closed(conn: sqlite3.Connection, *, send: Any) -> dict[str, Any]:
    summaries: list[dict[str, Any]] = []
    for row in ps.list_partners(conn):
        if str(row["status"]) != "closed":
            continue
        summary = ps.close_summary(conn, str(row["id"]))
        if summary.get("ok"):
            summary["close_awaiting_thb"] = _close_awaiting_thb(conn, str(row["id"]))
            summaries.append(summary)
    summaries.sort(key=_closed_sort_key)
    if not summaries:
        send("ยังไม่มีพาร์ทเนอร์ที่ปิด")
        return {"ok": True, "cmd": "closed", "n": 0}
    text, shown = _render_closed(summaries)
    send(text)
    return {"ok": True, "cmd": "closed", "n": shown}


def _close_awaiting_thb(conn: sqlite3.Connection, partner_id: str) -> bool:
    row = conn.execute(
        """
        SELECT id, paid_at_ms, payout_micro FROM partner_request
        WHERE partner_id = ? AND kind = 'close' AND status = 'confirmed'
        ORDER BY decided_at_ms DESC, id DESC
        LIMIT 1
        """,
        (str(partner_id),),
    ).fetchone()
    if row is None or int(row["payout_micro"] or 0) <= 0:
        return False
    paid_at = row["paid_at_ms"]
    if paid_at is None or int(paid_at) < int(fx.THB_FLOW_SINCE_MS):
        return False
    return fx.current_fx(conn, str(row["id"])) is None


def _show_admin(
    body: str,
    warnings: list[str],
    *,
    mid: Any,
    edit: Any,
    send: Any,
) -> None:
    """Edit the admin card. If that fails and there are warnings, send them as a new message."""
    edited = False
    if mid is not None:
        edited = bool(edit(int(mid), body, reply_markup={"inline_keyboard": []}))
    if warnings and not edited:
        send(body)


def _masked_kyc_bits(result: dict[str, Any]) -> str:
    account = pk.mask_account(str(result["bank_code"]), str(result["account_last4"]))
    return f"{result['real_name_mask']} · {account} ({result['holder_name_mask']})"


def _handle_kyc_list(conn: sqlite3.Connection, *, send: Any, now_ms: int) -> dict[str, Any]:
    pk.purge_expired_drafts(conn, now_ms)
    rows = pk.list_kyc_status(conn)
    if not rows:
        send("ยังไม่มีพาร์ทเนอร์")
        return {"ok": True, "cmd": "kyc_list", "n": 0}
    lines = ["ข้อมูลรับเงิน"]
    for item in rows:
        name = str(item["name"])
        if item["is_admin"]:
            lines.append(f"{name} · แอดมินไม่ต้องกรอก")
        elif item["state"] == "none":
            lines.append(f"{name} · ยังไม่กรอก")
        else:
            flag = "" if item.get("holder_matches") else " ⚠️ ชื่อบัญชีไม่ตรง"
            lines.append(f"{name} · บันทึกแล้ว · {item['bank_code']} ••••{item['account_last4']}{flag}")
    send("\n".join(lines))
    return {"ok": True, "cmd": "kyc_list", "n": len(rows)}


def _handle_kyc_show(conn: sqlite3.Connection, name: str, *, send: Any) -> dict[str, Any]:
    row = _find_partner_by_name(conn, name)
    if row is None:
        send(thai_reason("not_found"))
        return {"ok": False, "reason": "not_found"}
    if int(row["is_admin"] or 0) == 1:
        send("แอดมินไม่ต้องกรอก")
        return {"ok": False, "reason": "admin_exempt"}
    try:
        revealed = pk.reveal_current(conn, str(row["id"]))
    except pk.KycKeyError:
        logger.warning("partner kyc key unavailable partner_id=%s", row["id"])
        send(thai_reason("kyc_key"))
        return {"ok": False, "reason": "kyc_key"}
    except pk.KycCryptoError:
        logger.warning("partner kyc decrypt failed partner_id=%s", row["id"])
        send("อ่านข้อมูลรับเงินไม่ได้ตอนนี้")
        return {"ok": False, "reason": "kyc_crypto"}
    if not revealed.get("ok"):
        send(f"{row['name']} · ยังไม่กรอก")
        return {"ok": True, "cmd": "kyc_show", "state": "none"}
    lines = [f"ข้อมูลรับเงินของ {row['name']}"]
    for ver in revealed["versions"]:
        lines.append(
            f"v{ver['version']} · {ver['source']} · {partner_rules.fmt_date(int(ver['created_at_ms']))}"
        )
        lines.append(f"ชื่อจริง: {ver['real_name']}")
        lines.append(f"เบอร์: {ver['phone']}")
        lines.append(f"ธนาคาร: {ver['bank_code']}")
        lines.append(f"เลขบัญชี: {ver['account_no']}")
        lines.append(f"ชื่อบัญชี: {ver['holder_name']}")
        if not ver["holder_matches"]:
            lines.append("⚠️ ชื่อบัญชีไม่ตรงกับชื่อจริง")
        if ver["account_length_warn"]:
            lines.append("⚠️ จำนวนหลักบัญชีไม่ตรงกับธนาคารนี้")
        if ver["change_reason"]:
            lines.append(f"เหตุผล: {ver['change_reason']}")
    send("\n".join(lines))
    return {"ok": True, "cmd": "kyc_show", "state": "saved"}


def _handle_kyc_set(
    conn: sqlite3.Connection,
    name: str,
    reason: str,
    form: str,
    *,
    send: Any,
    now_ms: int,
) -> dict[str, Any]:
    row = _find_partner_by_name(conn, name)
    if row is None:
        send(thai_reason("not_found"))
        return {"ok": False, "reason": "not_found"}
    if int(row["is_admin"] or 0) == 1:
        send("แอดมินไม่ต้องกรอก")
        return {"ok": False, "reason": "admin_exempt"}
    try:
        result = pk.admin_set_kyc(conn, str(row["id"]), form, reason, now_ms)
    except pk.KycKeyError:
        logger.warning("partner kyc key unavailable partner_id=%s", row["id"])
        send(thai_reason("kyc_key"))
        return {"ok": False, "reason": "kyc_key"}
    if not result.get("ok"):
        code = str(result.get("reason") or "")
        if code == "kyc_invalid":
            send("\n".join(str(err) for err in (result.get("errors") or [])))
        else:
            send(thai_reason(code))
        return {"ok": False, "reason": code}
    bits = _masked_kyc_bits(result)
    lines = [f"บันทึกข้อมูลรับเงิน {row['name']} เวอร์ชัน {result['version']} แล้ว", bits]
    if not result.get("holder_matches"):
        lines.append("⚠️ ชื่อบัญชีไม่ตรงกับชื่อจริง")
    if result.get("account_length_warn"):
        lines.append("⚠️ จำนวนหลักบัญชีไม่ตรงกับธนาคารนี้")
    send("\n".join(lines))
    send_to_partner(
        send,
        row,
        f"แอดมินแก้ไขข้อมูลรับเงินของคุณแล้ว: {bits} — ถ้าไม่ได้ขอ ติดต่อแอดมินทันที",
    )
    return {"ok": True, "cmd": "kyc_set", "kyc_id": result.get("kyc_id"), "version": result.get("version")}


def handle_partner_text(
    conn: sqlite3.Connection,
    text: str,
    *,
    message: dict[str, Any],
    send: Any,
    futures: Any = None,
    now_ms: int,
    edit: Any = None,
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
        if cmd == "cv":
            return _handle_cv_wd_close(
                conn, str(parsed["name"]), "convert", send=send, futures=futures, now_ms=now_ms
            )
        if cmd == "wd":
            return _handle_cv_wd_close(
                conn,
                str(parsed["name"]),
                "withdraw_profit",
                send=send,
                futures=futures,
                now_ms=now_ms,
            )
        if cmd == "close":
            return _handle_cv_wd_close(
                conn, str(parsed["name"]), "close", send=send, futures=futures, now_ms=now_ms
            )
        if cmd == "closed":
            return _handle_closed(conn, send=send)
        if cmd == "due":
            return _handle_due(conn, send=send, now_ms=now_ms)
        if cmd == "paid":
            return thb.handle_paid_text(
                conn,
                str(parsed.get("raw") or ""),
                send=send,
                edit=edit or (lambda *_a, **_k: False),
                entered_by="admin",
                now_ms=now_ms,
            )
        if cmd == "fix":
            return thb.handle_fix_text(
                conn,
                str(parsed.get("raw") or ""),
                send=send,
                edit=edit or (lambda *_a, **_k: False),
                entered_by="admin",
                now_ms=now_ms,
            )
        if cmd == "carry":
            return thb.handle_carry_text(
                conn,
                str(parsed.get("raw") or ""),
                send=send,
                edit=edit or (lambda *_a, **_k: False),
                entered_by="admin",
                now_ms=now_ms,
            )
        if cmd == "kyc_list":
            return _handle_kyc_list(conn, send=send, now_ms=now_ms)
        if cmd == "kyc_show":
            return _handle_kyc_show(conn, str(parsed["name"]), send=send)
        if cmd == "kyc_set":
            return _handle_kyc_set(
                conn,
                str(parsed["name"]),
                str(parsed["reason"]),
                str(parsed.get("form") or ""),
                send=send,
                now_ms=now_ms,
            )
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
                if not created.get("ok"):
                    reason = str(created.get("reason") or "")
                    _answer(thai_reason(reason))
                    return {"ok": False, "reason": reason}
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
                    body, markup = _settled_view(conn, rid)
                    edit(int(mid), body, reply_markup=markup)
                return {"ok": False, "reason": "request_not_pending"}
            if str(req["kind"]) != "deposit":
                _answer("ไม่ใช่รายการฝาก")
                return {"ok": False, "reason": "not_deposit"}
            amount = ps.usdt_from_micro(int(req["quote_gross_micro"] or 0))
            partner = ps.get_partner(conn, str(req["partner_id"]))
            rejected = ps.reject_request(conn, rid, now_ms)
            if rejected.get("ok"):
                _answer()
                if mid is not None:
                    edit(int(mid), "ยกเลิกแล้ว", reply_markup={"inline_keyboard": []})
                if partner is not None:
                    send_to_partner(
                        send,
                        partner,
                        f"✖️ แอดมินยกเลิกรายการฝาก {fmt_usdt(amount)} USDT",
                    )
                return {"ok": True, "cmd": "dep_cancel"}
            _answer("รายการนี้ปิดไปแล้ว")
            if mid is not None:
                body, markup = _settled_view(conn, rid)
                edit(int(mid), body, reply_markup=markup)
            return {"ok": False, "reason": "request_not_pending"}

        if data.startswith("pa:dy:") or data.startswith("pa:df:"):
            force = data.startswith("pa:df:")
            ts_s: int | None = None
            if force:
                m = _GUARD_FORCE_RE.match(data[len("pa:df:") :])
                if m is None:
                    _answer("รายการนี้ปิดไปแล้ว")
                    return {"ok": False, "reason": "bad_id"}
                rid = m.group(1)
                ts_s = int(m.group(2))
            else:
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
                    body, markup = _settled_view(conn, rid)
                    edit(int(mid), body, reply_markup=markup)
                return {"ok": False, "reason": "request_not_pending"}
            if str(req["kind"]) != "deposit":
                _answer("ไม่ใช่รายการฝาก")
                return {"ok": False, "reason": "not_deposit"}
            dep_partner = ps.get_partner(conn, str(req["partner_id"]))
            if dep_partner is not None and ps.deposit_kyc_required(conn, dep_partner):
                _answer("ยังไม่มีข้อมูลรับเงิน")
                send(_kyc_set_hint(str(dep_partner["name"])))
                return {"ok": False, "reason": "kyc_required"}
            due_total = ps.payouts_due_total(conn)
            if due_total > 0:
                _answer(f"มียอดค้างจ่าย {fmt_usdt(due_total)} — โอน USDT ออกแล้วกด 📤 ก่อน (p due)")
                return {"ok": False, "reason": "payout_due"}

            client = _resolve_client(futures)
            guard_suffix = ""
            if _should_run_guard(force=force, ts_s=ts_s, now_ms=now_ms):
                guard_result, guard_text = _transfer_guard(conn, client, now_ms=now_ms, req=req)
                if guard_result.warn:
                    _answer("มีเรื่องยอดโอน — อ่านบนการ์ดก่อนยืนยัน")
                    text = _merge_guard_text(str(msg.get("text") or ""), guard_text)
                    buttons = _guard_deposit_buttons(rid, int(now_ms) // 1000)
                    edited_ok = False
                    if mid is not None:
                        edited_ok = bool(edit(int(mid), text, reply_markup=buttons))
                    if mid is None or not edited_ok:
                        send(text, reply_markup=buttons)
                    return {"ok": False, "reason": "guard_warn"}
                guard_suffix = _guard_ok_suffix(guard_result)
            else:
                guard_suffix = "\n⚠️ ยืนยันทั้งที่มีคำเตือนยอดโอน"
                logger.info("partner guard force-confirm id=%s kind=deposit", rid)

            av = _read_v(client, now_ms=now_ms)
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
                body += guard_suffix
                if mid is not None:
                    edit(int(mid), body, reply_markup={"inline_keyboard": []})
                partner = ps.get_partner(conn, str(req["partner_id"]))
                if partner is not None:
                    send_to_partner(
                        send,
                        partner,
                        f"✅ บันทึกเงินฝาก {fmt_usdt(amount)} USDT แล้ว · "
                        f"ทุนรวม {fmt_usdt(Decimal(result.get('cost_after') or 0))} USDT",
                    )
                return {"ok": True, "cmd": "dep_confirm"}
            reason = str(result.get("reason") or "")
            if reason == "request_not_pending":
                _answer("รายการนี้ปิดไปแล้ว")
                if mid is not None:
                    body, markup = _settled_view(conn, rid)
                    edit(int(mid), body, reply_markup=markup)
                return {"ok": False, "reason": reason}
            _answer(thai_reason(reason))
            return {"ok": False, "reason": reason}

        if data.startswith("pa:ry:") or data.startswith("pa:rf:"):
            force = data.startswith("pa:rf:")
            ts_s = None
            if force:
                m = _GUARD_FORCE_RE.match(data[len("pa:rf:") :])
                if m is None:
                    _answer("รายการนี้ปิดไปแล้ว")
                    return {"ok": False, "reason": "bad_id"}
                rid = m.group(1)
                ts_s = int(m.group(2))
            else:
                rid = data[len("pa:ry:") :]
                if not _REQ_ID_RE.match(rid):
                    _answer("รายการนี้ปิดไปแล้ว")
                    return {"ok": False, "reason": "bad_id"}
            req = conn.execute("SELECT * FROM partner_request WHERE id = ?", (rid,)).fetchone()
            if req is None:
                _answer("รายการนี้ปิดไปแล้ว")
                return {"ok": False, "reason": "request_not_pending"}
            kind = str(req["kind"])
            if kind not in ("convert", "withdraw_profit", "close"):
                _answer("ไม่ใช่คำขอนี้")
                return {"ok": False, "reason": "not_this_kind"}
            status = str(req["status"])
            expired = status == "expired" or (status == "pending" and int(req["expires_at_ms"]) <= now_ms)
            if expired:
                _answer("รายการหมดอายุแล้ว")
                if mid is not None:
                    edit(int(mid), str(msg.get("text") or ""), reply_markup={"inline_keyboard": []})
                return {"ok": False, "reason": "expired"}
            if status != "pending":
                _answer("รายการนี้ปิดไปแล้ว")
                if mid is not None:
                    body, markup = _settled_view(conn, rid)
                    edit(int(mid), body, reply_markup=markup)
                return {"ok": False, "reason": "request_not_pending"}
            due_total = ps.payouts_due_total(conn)
            if due_total > 0:
                _answer(f"มียอดค้างจ่าย {fmt_usdt(due_total)} — โอน USDT ออกแล้วกด 📤 ก่อน (p due)")
                return {"ok": False, "reason": "payout_due"}

            client = _resolve_client(futures)
            guard_suffix = ""
            if _should_run_guard(force=force, ts_s=ts_s, now_ms=now_ms):
                guard_result, guard_text = _transfer_guard(conn, client, now_ms=now_ms, req=req)
                if guard_result.warn:
                    _answer("มีเรื่องยอดโอน — อ่านบนการ์ดก่อนยืนยัน")
                    text = _merge_guard_text(str(msg.get("text") or ""), guard_text)
                    buttons = _guard_request_buttons(rid, int(now_ms) // 1000)
                    edited_ok = False
                    if mid is not None:
                        edited_ok = bool(edit(int(mid), text, reply_markup=buttons))
                    if mid is None or not edited_ok:
                        send(text, reply_markup=buttons)
                    return {"ok": False, "reason": "guard_warn"}
                guard_suffix = _guard_ok_suffix(guard_result)
            else:
                guard_suffix = "\n⚠️ ยืนยันทั้งที่มีคำเตือนยอดโอน"
                logger.info("partner guard force-confirm id=%s kind=%s", rid, kind)

            av = _read_v(client, now_ms=now_ms)
            if isinstance(av, AccountValueError):
                _answer(thai_reason(av.reason, detail=av.detail))
                return {"ok": False, "reason": av.reason}
            result = ps.confirm_request(conn, rid, av.value, now_ms)
            partner = ps.get_partner(conn, str(req["partner_id"]))
            pname = str(partner["name"]) if partner is not None else "?"
            if result.get("ok"):
                fee = Decimal(result.get("fee") or 0)
                cash = Decimal(result.get("cash") or 0)
                cost_after = Decimal(result.get("cost_after") or 0)
                _answer()
                if kind == "convert":
                    body = (
                        f"✅ ทบกำไร {pname} แล้ว · ค่าธรรมเนียม {fmt_usdt(fee)} · ทุนใหม่ {fmt_usdt(cost_after)}"
                    )
                    body += guard_suffix
                    if mid is not None:
                        edit(int(mid), body, reply_markup={"inline_keyboard": []})
                    if partner is not None:
                        send_to_partner(
                            send,
                            partner,
                            f"✅ ทบกำไรแล้ว · ค่าธรรมเนียม {fmt_usdt(fee)} · ทุนใหม่ {fmt_usdt(cost_after)} USDT",
                        )
                elif cash > 0:
                    saved = conn.execute(
                        "SELECT payout_kyc_id FROM partner_request WHERE id = ?",
                        (rid,),
                    ).fetchone()
                    kyc_id = None if saved is None else saved["payout_kyc_id"]
                    account = pk.payout_account_line(
                        conn,
                        partner_id=str(req["partner_id"]),
                        payout_kyc_id=kyc_id,
                        partner_name=pname,
                    )
                    body = (
                        f"✅ ยืนยันแล้ว · ค้างจ่าย {fmt_usdt(cash)} USDT ให้ {pname}\n"
                        f"โอน USDT ออกจาก futures wallet แล้วกด 📤\n"
                        f"{account}"
                    )
                    body += guard_suffix
                    if mid is not None:
                        edit(int(mid), body, reply_markup=_paid_button(rid))
                    if partner is not None:
                        if kind == "withdraw_profit":
                            send_to_partner(
                                send,
                                partner,
                                f"✅ แอดมินยืนยันถอนกำไร {fmt_usdt(cash)} USDT "
                                f"(ค่าธรรมเนียม {fmt_usdt(fee)}) · รอโอน",
                            )
                        else:
                            send_to_partner(
                                send,
                                partner,
                                _closing_note(conn, str(partner["id"]), cash=cash, fee=fee),
                            )
                else:
                    # close with cash 0: tell the partner first, then unbind only
                    # when that send succeeded and this partner has nothing due.
                    body = f"✅ ยืนยันแล้ว · ปิด {pname} แล้ว (ไม่มีเงินออก)"
                    body += guard_suffix
                    warnings = []
                    if partner is not None:
                        note = _closing_note(conn, str(partner["id"]), cash=cash, fee=fee)
                        if not _due_for(conn, str(partner["id"])):
                            note += "\nปิดพอร์ตเรียบร้อย บอทจะไม่แสดงยอดอีก"
                        warnings = _release_closed_binding(conn, partner, send=send, note=note, now_ms=now_ms)
                        body = _append_warnings(body, warnings)
                    _show_admin(body, warnings, mid=mid, edit=edit, send=send)
                return {"ok": True, "cmd": "confirm", "kind": kind}
            reason = str(result.get("reason") or "")
            if reason in ("no_profit", "below_min"):
                _answer()
                if mid is not None:
                    edit(
                        int(mid),
                        "ยกเลิกอัตโนมัติ — ไม่มีกำไรแล้ว ณ ตอนยืนยัน",
                        reply_markup={"inline_keyboard": []},
                    )
                if partner is not None:
                    send_to_partner(
                        send,
                        partner,
                        f"คำขอ{kind_th(kind)}ถูกยกเลิก — ไม่มีกำไร ณ ตอนแอดมินยืนยัน",
                    )
                return {"ok": False, "reason": reason}
            if reason == "deposit_pending":
                _answer("มีรายการฝากค้างอยู่ — บันทึกหรือยกเลิกก่อน")
                return {"ok": False, "reason": reason}
            if reason == "request_not_pending":
                _answer("รายการนี้ปิดไปแล้ว")
                if mid is not None:
                    body, markup = _settled_view(conn, rid)
                    edit(int(mid), body, reply_markup=markup)
                return {"ok": False, "reason": reason}
            _answer(thai_reason(reason))
            return {"ok": False, "reason": reason}

        if data.startswith("pa:rn:"):
            rid = data[len("pa:rn:") :]
            if not _REQ_ID_RE.match(rid):
                _answer("รายการนี้ปิดไปแล้ว")
                return {"ok": False, "reason": "bad_id"}
            req = conn.execute("SELECT * FROM partner_request WHERE id = ?", (rid,)).fetchone()
            if req is None:
                _answer("รายการนี้ปิดไปแล้ว")
                return {"ok": False, "reason": "request_not_pending"}
            kind = str(req["kind"])
            if kind not in ("convert", "withdraw_profit", "close"):
                _answer("ไม่ใช่คำขอนี้")
                return {"ok": False, "reason": "not_this_kind"}
            partner = ps.get_partner(conn, str(req["partner_id"]))
            rejected = ps.reject_request(conn, rid, now_ms)
            if rejected.get("ok"):
                _answer()
                if mid is not None:
                    edit(int(mid), "ปฏิเสธแล้ว", reply_markup={"inline_keyboard": []})
                if partner is not None:
                    send_to_partner(send, partner, f"✖️ แอดมินปฏิเสธคำขอ{kind_th(kind)}")
                return {"ok": True, "cmd": "reject"}
            _answer("รายการนี้ปิดไปแล้ว")
            if mid is not None:
                body, markup = _settled_view(conn, rid)
                edit(int(mid), body, reply_markup=markup)
            return {"ok": False, "reason": "request_not_pending"}

        if data.startswith("pa:pd:"):
            rid = data[len("pa:pd:") :]
            if not _REQ_ID_RE.match(rid):
                _answer("รายการนี้ปิดไปแล้ว")
                return {"ok": False, "reason": "bad_id"}
            req = conn.execute("SELECT * FROM partner_request WHERE id = ?", (rid,)).fetchone()
            paid = ps.mark_paid(conn, rid, now_ms)
            if paid.get("ok"):
                partner = ps.get_partner(conn, str(req["partner_id"])) if req is not None else None
                _answer()
                note = thb.step1_partner(conn, rid)
                card_warnings: list[str] = []
                if partner is not None and thb.step1_releases(conn, rid):
                    facts = _close_fact_lines(conn, str(partner["id"]))
                    if facts:
                        note += "\n" + "\n".join(facts)
                    note += "\nปิดพอร์ตเรียบร้อย บอทจะไม่แสดงยอดอีก"
                    release_warnings = _release_closed_binding(
                        conn, partner, send=send, note=note, now_ms=now_ms
                    )
                    card_warnings.extend(release_warnings)
                elif partner is not None:
                    sent_ok = send_to_partner(send, partner, note)
                    if not sent_ok and partner["telegram_user_id"] is not None:
                        card_warnings.append("⚠️ แจ้งพาร์ทเนอร์ไม่สำเร็จ")
                body = thb.step1_admin(conn, rid)
                if card_warnings:
                    body += "\n" + "\n".join(card_warnings)
                _show_admin(body, card_warnings, mid=mid, edit=edit, send=send)
                return {"ok": True, "cmd": "paid"}
            _answer("รายการนี้ปิดไปแล้ว")
            if mid is not None:
                body, markup = _settled_view(conn, rid)
                edit(int(mid), body, reply_markup=markup)
            return {"ok": False, "reason": "not_due"}

        if data.startswith(("pa:fy:", "pa:fn:", "pa:fs:", "pa:fc:")):
            return thb.confirm_callback(
                conn,
                data,
                now_ms=now_ms,
                entered_by="admin",
                answer=_answer,
                edit=edit,
                send=send,
                notify=lambda partner, text: send_to_partner(send, partner, text),
                release=_release_closed_binding,
                close_facts=_close_fact_lines,
                mid=mid,
            )

        _answer()
        return {"ok": False, "reason": "unknown"}
    except Exception:  # noqa: BLE001
        logger.exception("partner_admin callback failed")
        try:
            _answer(_FAIL_MSG)
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "reason": "exception"}
