"""Partner portal request rules (PR-A). SELECT-only; no writes; no Binance.

Binds the partner portal only. Admin ``p cv/wd/close`` and ``pa:*`` stay free,
and an admin ``p cv`` does not count toward the partner's monthly portal convert.
Must not import partner_audit or partner_portal.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from jarvise_assist import partner_ledger as pl
from jarvise_assist import partner_store as ps
from jarvise_assist.pause import BANGKOK

MIN_CONVERT_PROFIT = Decimal("20")
MIN_WITHDRAW_CASH = Decimal("20")
CLOSE_MIN_DAYS = 30

# Owner decision pending: does a convert whose request has source NULL (created
# before migration 31) use up the monthly portal convert? False = it does not
# lock (current behavior). Flip to True to treat NULL as 'portal'.
NULL_SOURCE_LOCKS = False


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


def next_month_start(now_ms: int) -> date:
    local = datetime.fromtimestamp(int(now_ms) / 1000.0, tz=BANGKOK)
    if local.month == 12:
        return date(local.year + 1, 1, 1)
    return date(local.year, local.month + 1, 1)


def convert_used_this_month(conn: sqlite3.Connection, partner_id: str, now_ms: int) -> bool:
    """True when a confirmed portal convert was REQUESTED in the current Bangkok month.

    The month is taken from ``partner_request.created_at_ms``, not the confirm
    (event) time: a request made 30/11 and confirmed 01/12 uses November, and
    December stays free. Only confirmed converts (a convert event exists) count;
    a still-pending request is covered by the one-pending rule instead.

    Admin ``p cv`` converts (request source 'admin') do not use up the
    partner's monthly portal convert. NULL source follows ``NULL_SOURCE_LOCKS``.
    """
    local = datetime.fromtimestamp(int(now_ms) / 1000.0, tz=BANGKOK)
    month_start = datetime(local.year, local.month, 1, tzinfo=BANGKOK)
    nxt = next_month_start(now_ms)
    next_dt = datetime(nxt.year, nxt.month, nxt.day, tzinfo=BANGKOK)
    lo = int(month_start.timestamp() * 1000)
    hi = int(next_dt.timestamp() * 1000)
    # The only NULL-source rule. NULL = request created before migration 31.
    source_is_portal = (
        "COALESCE(r.source, 'portal') = 'portal'" if NULL_SOURCE_LOCKS else "r.source = 'portal'"
    )
    row = conn.execute(
        f"""
        SELECT 1 FROM partner_event e
        JOIN partner_request r ON r.id = e.request_id
        WHERE e.partner_id = ? AND e.kind = 'convert' AND {source_is_portal}
          AND r.created_at_ms >= ? AND r.created_at_ms < ?
        LIMIT 1
        """,
        (str(partner_id), lo, hi),
    ).fetchone()
    return row is not None


def convert_month_block(now_ms: int) -> Block:
    nxt = next_month_start(now_ms)
    nxt_ms = int(datetime(nxt.year, nxt.month, nxt.day, tzinfo=BANGKOK).timestamp() * 1000)
    return Block(
        code="convert_month",
        toast=f"ทบกำไรได้เดือนละครั้ง · ครั้งถัดไป {fmt_date(nxt_ms)}",
        label="🔒 ทบกำไร",
    )


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

    if kind == "convert" and convert_used_this_month(conn, pid, now_ms):
        return convert_month_block(now_ms)

    if nav is None:
        return None

    units = ps.units_from_e8(int(row["units_e8"]))
    cost = ps.usdt_from_micro(int(row["cost_micro"]))
    fee_rate = ps._fee_rate_from_bps(int(row["fee_rate_bps"]))
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
