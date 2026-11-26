"""Read-only partner profile. Mask columns only. No ledger writes."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from decimal import Decimal
from typing import Any

from jarvise_assist import partner_fx as fx
from jarvise_assist import partner_kyc as pk
from jarvise_assist import partner_rules
from jarvise_assist import partner_store as ps
from jarvise_assist.partner_admin import fmt_signed_usdt, fmt_usdt

logger = logging.getLogger(__name__)

_KYC_LEGACY = pk._KYC_LEGACY


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
    valuation_pending: bool = False,
) -> dict[str, Any]:
    """Pure data for one partner. ``account_value`` is the live wallet the card uses.

    A closed partner does not use that wallet. A missing or refused value is reported
    as unreadable instead of inventing a net figure. ``valuation_pending`` hides
    net/profit while the portal unrecorded gate is not clear (cost and dues stay).
    """
    row = ps.get_partner(conn, partner_id)
    if row is None:
        logger.warning("partner profile missing partner_id=%s reason=not_found", partner_id)
        return {"ok": False, "reason": "not_found"}
    pid = str(row["id"])
    status = str(row["status"])
    joined = partner_rules.joined_at_ms(conn, pid)
    dues = [
        ps.usdt_from_micro(int(due["payout_micro"] or 0))
        for due in ps.list_due_payouts(conn)
        if str(due["partner_id"]) == pid
    ]
    kyc = pk.masked_profile(conn, pid)
    awaiting = [ps.usdt_from_micro(int(row["payout_micro"] or 0)) for row in fx.awaiting_thb(conn, pid)]
    data: dict[str, Any] = {
        "ok": True,
        "name": str(row["name"]),
        "status": status,
        "updated_ms": int(now_ms),
        "joined_at_ms": joined,
        "fee_rate_bps": int(row["fee_rate_bps"]),
        "dues": dues,
        "awaiting_thb": awaiting,
        "kyc": kyc,
        "kyc_legacy": kyc is None and status == "active" and joined is not None,
        "close_cash": None,
        "empty": False,
        "value_unreadable": False,
        "valuation_pending": False,
        "cost": None,
        "net": None,
        "pnl": None,
    }
    if status == "closed":
        summary = ps.close_summary(conn, pid)
        if summary.get("ok"):
            close_cash = summary.get("close_cash")
            data["close_cash"] = close_cash if isinstance(close_cash, Decimal) else Decimal("0")
        return data
    cost = ps.usdt_from_micro(int(row["cost_micro"]))
    units = ps.units_from_e8(int(row["units_e8"]))
    data["cost"] = cost
    if valuation_pending:
        data["valuation_pending"] = True
        return data
    if account_value is None:
        data["value_unreadable"] = True
        return data
    view = ps.view_partner(conn, pid, account_value)
    if not view.get("ok"):
        data["value_unreadable"] = True
        return data
    if units == 0 and cost == 0:
        data["empty"] = True
        return data
    net = Decimal(view["net"])
    data["net"] = net
    data["pnl"] = net - cost
    return data


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
    if status == "closed":
        if data.get("close_cash") is not None:
            lines.append(f"ยอดปิด {fmt_usdt(Decimal(data['close_cash']))} USDT")
    elif data.get("valuation_pending"):
        lines.append("กำลังตรวจยอด")
        lines.append(f"ทุน {fmt_usdt(Decimal(data['cost']))} USDT")
    elif data.get("empty"):
        lines.append("ยังไม่มีเงินในพอร์ต")
    elif data.get("value_unreadable"):
        lines.append("อ่านยอดไม่ได้ตอนนี้ ลองใหม่อีกครั้ง")
        lines.append(f"ทุน {fmt_usdt(Decimal(data['cost']))} USDT")
    else:
        lines.append(f"ทุน {fmt_usdt(Decimal(data['cost']))} USDT")
        lines.append(f"ยอดสุทธิ {fmt_usdt(Decimal(data['net']))} USDT")
        lines.append(f"กำไร {fmt_signed_usdt(Decimal(data['pnl']))} USDT")
    for cash in data.get("dues") or []:
        lines.append(f"รอโอน {fmt_usdt(Decimal(cash))} USDT")
    for cash in data.get("awaiting_thb") or []:
        lines.append(f"กำลังแปลงเป็นเงินบาท {fmt_usdt(Decimal(cash))} USDT")
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
