"""Read-only partner profile. Mask columns only. No ledger writes."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from decimal import Decimal
from typing import Any

from jarvise_assist import partner_kyc as pk
from jarvise_assist import partner_rules
from jarvise_assist import partner_store as ps
from jarvise_assist.partner_admin import fmt_signed_usdt, fmt_usdt

logger = logging.getLogger(__name__)

# Same wording as the active card's reminder. Kept here so this module does not import the portal.
_KYC_LEGACY = "⚠️ ยังไม่ได้กรอกข้อมูลรับเงิน — ส่งฟอร์มเมื่อสะดวก"


def _fee_line(bps: int) -> str:
    text = format(Decimal(int(bps)) / Decimal(100), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"ค่าธรรมเนียม {text}%"


def _stamp(now_ms: int) -> str:
    dt = datetime.fromtimestamp(int(now_ms) / 1000, tz=partner_rules.BANGKOK)
    return dt.strftime("%d/%m %H:%M")


def profile_data(
    conn: sqlite3.Connection,
    partner_id: str,
    now_ms: int,
    *,
    account_value: Decimal | None = None,
) -> dict[str, Any]:
    """Pure data for one partner. ``account_value`` is the live wallet the card uses.

    Omit it and the capital line still comes from the stored cost; net and profit stay unset.
    """
    row = ps.get_partner(conn, partner_id)
    if row is None:
        logger.warning("partner profile missing partner_id=%s reason=not_found", partner_id)
        return {"ok": False, "reason": "not_found"}
    pid = str(row["id"])
    joined = partner_rules.joined_at_ms(conn, pid)
    cost = ps.usdt_from_micro(int(row["cost_micro"]))
    net: Decimal | None = None
    pnl: Decimal | None = None
    if account_value is not None:
        view = ps.view_partner(conn, pid, account_value)
        if view.get("ok"):
            net = Decimal(view["net"])
            pnl = net - cost
    dues = [
        ps.usdt_from_micro(int(due["payout_micro"] or 0))
        for due in ps.list_due_payouts(conn)
        if str(due["partner_id"]) == pid
    ]
    kyc = pk.masked_profile(conn, pid)
    status = str(row["status"])
    return {
        "ok": True,
        "name": str(row["name"]),
        "status": status,
        "updated_ms": int(now_ms),
        "joined_at_ms": joined,
        "fee_rate_bps": int(row["fee_rate_bps"]),
        "cost": cost,
        "net": net,
        "pnl": pnl,
        "dues": dues,
        "kyc": kyc,
        "kyc_legacy": kyc is None and status == "active" and joined is not None,
    }


def render_profile(data: dict[str, Any]) -> str:
    """Pure text. Does not touch the database or log the body."""
    if not data.get("ok"):
        return "โปรไฟล์พาร์ทเนอร์\nอ่านโปรไฟล์ไม่ได้ตอนนี้"
    lines = ["โปรไฟล์พาร์ทเนอร์"]
    status = str(data.get("status") or "")
    lines.append("สถานะ: เปิดอยู่" if status == "active" else "สถานะ: ปิดแล้ว")
    lines.append(f"{data['name']} · อัปเดต {_stamp(int(data['updated_ms']))}")
    joined = data.get("joined_at_ms")
    if joined is None:
        lines.append("เข้าร่วม: ยังไม่ฝาก")
    else:
        lines.append(f"เข้าร่วม {partner_rules.fmt_date(int(joined))}")
    lines.append(_fee_line(int(data["fee_rate_bps"])))
    lines.append(f"ทุน {fmt_usdt(Decimal(data['cost']))} USDT")
    if data.get("net") is not None and data.get("pnl") is not None:
        lines.append(f"ยอดสุทธิ {fmt_usdt(Decimal(data['net']))} USDT")
        lines.append(f"กำไร {fmt_signed_usdt(Decimal(data['pnl']))} USDT")
    for cash in data.get("dues") or []:
        lines.append(f"รอโอน {fmt_usdt(Decimal(cash))} USDT")
    kyc = data.get("kyc")
    if not kyc:
        lines.append("ข้อมูลรับเงิน: ยังไม่กรอก")
        if data.get("kyc_legacy"):
            lines.append(_KYC_LEGACY)
    else:
        account = pk.mask_account(str(kyc["bank_code"]), str(kyc["account_last4"]))
        lines.append(f"ข้อมูลรับเงิน: {kyc['real_name_mask']} · {account}")
        lines.append(str(kyc["holder_name_mask"]))
        lines.append(pk.mask_phone(str(kyc["phone_last4"])))
    lines.append("แก้ไขข้อมูล: ติดต่อแอดมิน")
    return "\n".join(lines)
