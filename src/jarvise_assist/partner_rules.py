"""Partner portal request rules (PR-A). SELECT-only; no writes; no Binance.

Binds the partner portal only. Admin ``p cv/wd/close`` and ``pa:*`` stay free.
A portal convert has no per-month limit; only the profit min and the
one-pending-request rule apply. Must not import partner_audit or partner_portal.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from jarvise_assist import partner_ledger as pl
from jarvise_assist import partner_store as ps
from jarvise_assist.partner_kyc import kyc_state
from jarvise_assist.pause import BANGKOK

MIN_CONVERT_PROFIT = Decimal("20")
MIN_WITHDRAW_CASH = Decimal("20")
CLOSE_MIN_DAYS = 30


@dataclass(frozen=True)
class Block:
    code: str
    toast: str
    label: str


def joined_at_ms(conn: sqlite3.Connection, partner_id: str) -> int | None:
    """First confirmed deposit event time, or None when the partner has never deposited."""
    row = conn.execute(
        """
        SELECT MIN(created_at_ms) AS m
        FROM partner_event
        WHERE partner_id = ? AND kind = 'deposit'
        """,
        (str(partner_id),),
    ).fetchone()
    if row is None or row["m"] is None:
        return None
    return int(row["m"])


def fmt_date(ms: int) -> str:
    return datetime.fromtimestamp(int(ms) / 1000.0, tz=BANGKOK).strftime("%d/%m/%Y")


def close_open_date(joined_ms: int) -> date:
    """Bangkok calendar date when ปิดพอร์ต becomes available (join date + CLOSE_MIN_DAYS)."""
    joined = datetime.fromtimestamp(int(joined_ms) / 1000.0, tz=BANGKOK).date()
    return joined + timedelta(days=int(CLOSE_MIN_DAYS))


def _pending(conn: sqlite3.Connection, partner_id: str, now_ms: int) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM partner_request
        WHERE partner_id = ? AND status = 'pending' AND expires_at_ms > ?
        LIMIT 1
        """,
        (str(partner_id), int(now_ms)),
    ).fetchone()
    return row is not None


def block(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    kind: str,
    *,
    nav: Decimal | None,
    now_ms: int,
) -> Block | None:
    """First matching portal lock for ``kind`` in convert / withdraw_profit / close.

    ``nav`` is the pool NAV (same as portal ``_estimate``); pass ``None`` when V
    could not be read so money rules are skipped.
    """
    if kind not in ("convert", "withdraw_profit", "close"):
        return None

    if str(row["status"]) != "active":
        if kind == "convert":
            label = "🔒 ทบกำไร"
        elif kind == "withdraw_profit":
            label = "🔒 ถอนกำไร"
        else:
            label = "🔒 ปิดพอร์ต"
        return Block(code="closed", toast="ปิดพอร์ตแล้ว", label=label)

    pid = str(row["id"])

    if _pending(conn, pid, now_ms):
        if kind == "convert":
            label = "🔒 ทบกำไร"
        elif kind == "withdraw_profit":
            label = "🔒 ถอนกำไร"
        else:
            label = "🔒 ปิดพอร์ต"
        return Block(code="pending", toast="มีคำขอค้างอยู่ รอแอดมิน", label=label)

    if kind in ("withdraw_profit", "close") and kyc_state(conn, pid) == "none":
        if kind == "withdraw_profit":
            return Block(
                code="kyc_missing",
                toast="กรอกข้อมูลรับเงินก่อนถอน",
                label="🔒 ถอนกำไร",
            )
        return Block(
            code="kyc_missing",
            toast="กรอกข้อมูลรับเงินก่อนปิดพอร์ต",
            label="🔒 ปิดพอร์ต",
        )

    if kind == "close":
        joined = joined_at_ms(conn, pid)
        if joined is None:
            return Block(
                code="no_deposit",
                toast="ยังไม่มีเงินในพอร์ต",
                label="🔒 ปิดพอร์ต",
            )
        open_d = close_open_date(joined)
        today = datetime.fromtimestamp(int(now_ms) / 1000.0, tz=BANGKOK).date()
        days_left = (open_d - today).days
        if days_left > 0:
            return Block(
                code="close_too_soon",
                toast=(f"ปิดพอร์ตได้เมื่อเข้าร่วมครบ {CLOSE_MIN_DAYS} วัน · ตั้งแต่ {open_d.strftime('%d/%m/%Y')}"),
                label=f"🔒 ปิดพอร์ต · อีก {days_left} วัน",
            )
        return None

    if nav is None:
        return None

    units = ps.units_from_e8(int(row["units_e8"]))
    cost = ps.usdt_from_micro(int(row["cost_micro"]))
    fee_rate = ps.fee_rate_from_bps(int(row["fee_rate_bps"]))
    n = Decimal(nav)

    if kind == "convert":
        plan = pl.plan_convert(units=units, cost=cost, fee_rate=fee_rate, nav=n)
        if isinstance(plan, pl.Refusal):
            return Block(
                code="no_profit",
                toast="ยังไม่มีกำไร ทำรายการนี้ไม่ได้",
                label="🔒 ทบกำไร",
            )
        if plan.profit < MIN_CONVERT_PROFIT:
            return Block(
                code="convert_min",
                toast=f"ทบกำไรได้เมื่อกำไรก่อนหักค่าธรรมเนียมถึง {MIN_CONVERT_PROFIT} USDT",
                label="🔒 ทบกำไร",
            )
        return None

    # withdraw_profit
    plan_w = pl.plan_withdraw_profit(units=units, cost=cost, fee_rate=fee_rate, nav=n)
    if isinstance(plan_w, pl.Refusal):
        if plan_w.reason == "no_profit":
            return Block(
                code="no_profit",
                toast="ยังไม่มีกำไร ทำรายการนี้ไม่ได้",
                label="🔒 ถอนกำไร",
            )
        return Block(
            code="withdraw_min",
            toast=f"ถอนกำไรได้เมื่อยอดถอนถึง {MIN_WITHDRAW_CASH} USDT",
            label="🔒 ถอนกำไร",
        )
    if plan_w.cash < MIN_WITHDRAW_CASH:
        return Block(
            code="withdraw_min",
            toast=f"ถอนกำไรได้เมื่อยอดถอนถึง {MIN_WITHDRAW_CASH} USDT",
            label="🔒 ถอนกำไร",
        )
    return None
