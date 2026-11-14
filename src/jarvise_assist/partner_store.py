"""SQLite persistence for the unit/NAV partner ledger.

Money: USDT as INTEGER micro (×1e6), units as INTEGER ×1e8.
Every write runs inside ``jarvise_ingest.db.transaction``. No HTTP, no Telegram.
"""

from __future__ import annotations

import sqlite3
import uuid
from decimal import Decimal
from typing import Any

from jarvise_assist.partner_ledger import (
    ClosePlan,
    ConvertPlan,
    DepositPlan,
    Refusal,
    WithdrawPlan,
    nav,
    plan_adjust_cost,
    plan_close,
    plan_convert,
    plan_deposit,
    plan_withdraw_profit,
    value_of,
)
from jarvise_ingest.db import transaction

MICRO_SCALE = Decimal("1000000")
UNIT_SCALE = Decimal("100000000")
BPS = Decimal("10000")


def usdt_to_micro(value: Decimal) -> int:
    return int((Decimal(value) * MICRO_SCALE).to_integral_value())


def usdt_from_micro(micro: int) -> Decimal:
    return Decimal(int(micro)) / MICRO_SCALE


def units_to_e8(value: Decimal) -> int:
    return int((Decimal(value) * UNIT_SCALE).to_integral_value())


def units_from_e8(e8: int) -> Decimal:
    return Decimal(int(e8)) / UNIT_SCALE


def _new_partner_id() -> str:
    return f"pt_{uuid.uuid4().hex[:16]}"


def _new_event_id() -> str:
    return f"pe_{uuid.uuid4().hex[:16]}"


def _fee_rate_from_bps(bps: int) -> Decimal:
    return Decimal(int(bps)) / BPS


def create_partner(
    conn: sqlite3.Connection,
    *,
    name: str,
    is_admin: bool = False,
    now_ms: int,
    fee_rate_bps: int = 5000,
) -> dict[str, Any]:
    """Insert a partner row. Admin forces fee_rate_bps = 0."""
    pid = _new_partner_id()
    fee = 0 if is_admin else int(fee_rate_bps)
    ts = int(now_ms)
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO partner (
                id, name, is_admin, fee_rate_bps, units_e8, cost_micro, status,
                created_at_ms, updated_at_ms
            ) VALUES (?, ?, ?, ?, 0, 0, 'active', ?, ?)
            """,
            (pid, name, 1 if is_admin else 0, fee, ts, ts),
        )
    return {"ok": True, "partner_id": pid}


def get_partner(conn: sqlite3.Connection, partner_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM partner WHERE id = ?", (partner_id,)).fetchone()


def list_partners(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM partner ORDER BY created_at_ms, id"))


def admin_partner(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM partner WHERE is_admin = 1").fetchone()


def total_units(conn: sqlite3.Connection) -> Decimal:
    row = conn.execute(
        "SELECT COALESCE(SUM(units_e8), 0) AS s FROM partner WHERE status = 'active'"
    ).fetchone()
    return units_from_e8(int(row["s"]))


def view_partner(conn: sqlite3.Connection, partner_id: str, account_value: Decimal) -> dict[str, Any]:
    row = get_partner(conn, partner_id)
    if row is None:
        return {"ok": False, "reason": "not_found"}
    u_total = total_units(conn)
    n = nav(Decimal(account_value), u_total)
    if isinstance(n, Refusal):
        return {"ok": False, "reason": n.reason}
    v = value_of(
        units=units_from_e8(int(row["units_e8"])),
        cost=usdt_from_micro(int(row["cost_micro"])),
        fee_rate=_fee_rate_from_bps(int(row["fee_rate_bps"])),
        nav=n,
    )
    return {
        "ok": True,
        "gross": v.gross,
        "profit": v.profit,
        "share": v.share,
        "fee": v.fee,
        "net": v.net,
        "state": v.state,
        "nav": n,
        "units": v.units,
        "cost": v.cost,
    }


def _refuse_if_closed(row: sqlite3.Row) -> dict[str, Any] | None:
    if str(row["status"]) == "closed":
        return {"ok": False, "reason": "closed"}
    return None


def _require_admin_for_fee(conn: sqlite3.Connection, fee_rate_bps: int) -> dict[str, Any] | None:
    if int(fee_rate_bps) > 0 and admin_partner(conn) is None:
        return {"ok": False, "reason": "no_admin"}
    return None


def _insert_event(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    partner_id: str,
    kind: str,
    account_value: Decimal,
    total_units_before: Decimal,
    nav_value: Decimal,
    units_delta: Decimal,
    cost_delta: Decimal,
    cash: Decimal,
    fee: Decimal,
    units_after: Decimal,
    cost_after: Decimal,
    now_ms: int,
    request_id: str | None = None,
    related_event_id: str | None = None,
    note: str | None = None,
    actor: str = "admin",
) -> None:
    conn.execute(
        """
        INSERT INTO partner_event (
            id, partner_id, kind, request_id, related_event_id,
            account_value_micro, total_units_before_e8, nav,
            units_delta_e8, cost_delta_micro, cash_micro, fee_micro,
            units_after_e8, cost_after_micro, actor, note, created_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            partner_id,
            kind,
            request_id,
            related_event_id,
            usdt_to_micro(account_value),
            units_to_e8(total_units_before),
            format(nav_value, "f"),
            units_to_e8(units_delta),
            usdt_to_micro(cost_delta),
            usdt_to_micro(cash),
            usdt_to_micro(fee),
            units_to_e8(units_after),
            usdt_to_micro(cost_after),
            actor,
            note,
            int(now_ms),
        ),
    )


def _update_partner(
    conn: sqlite3.Connection,
    partner_id: str,
    *,
    units: Decimal,
    cost: Decimal,
    now_ms: int,
    status: str | None = None,
) -> None:
    if status is None:
        conn.execute(
            """
            UPDATE partner
            SET units_e8 = ?, cost_micro = ?, updated_at_ms = ?
            WHERE id = ?
            """,
            (units_to_e8(units), usdt_to_micro(cost), int(now_ms), partner_id),
        )
    else:
        conn.execute(
            """
            UPDATE partner
            SET units_e8 = ?, cost_micro = ?, status = ?, updated_at_ms = ?
            WHERE id = ?
            """,
            (units_to_e8(units), usdt_to_micro(cost), status, int(now_ms), partner_id),
        )


def _credit_fee_to_admin(
    conn: sqlite3.Connection,
    *,
    fee_units: Decimal,
    fee: Decimal,
    account_value: Decimal,
    total_units_before: Decimal,
    nav_value: Decimal,
    related_event_id: str,
    now_ms: int,
) -> str | None:
    if fee_units <= 0:
        return None
    admin = admin_partner(conn)
    if admin is None:
        raise RuntimeError("no_admin")
    admin_id = str(admin["id"])
    admin_units = units_from_e8(int(admin["units_e8"]))
    admin_cost = usdt_from_micro(int(admin["cost_micro"]))
    # Fee units move without changing admin cost (performance fee in units).
    new_units = admin_units + fee_units
    eid = _new_event_id()
    _update_partner(conn, admin_id, units=new_units, cost=admin_cost, now_ms=now_ms)
    _insert_event(
        conn,
        event_id=eid,
        partner_id=admin_id,
        kind="fee_credit",
        account_value=account_value,
        total_units_before=total_units_before,
        nav_value=nav_value,
        units_delta=fee_units,
        cost_delta=Decimal("0"),
        cash=Decimal("0"),
        fee=fee,
        units_after=new_units,
        cost_after=admin_cost,
        now_ms=now_ms,
        related_event_id=related_event_id,
    )
    return eid


def apply_deposit(
    conn: sqlite3.Connection,
    partner_id: str,
    amount: Decimal,
    account_value_incl_deposit: Decimal,
    now_ms: int,
    note: str | None = None,
) -> dict[str, Any]:
    row = get_partner(conn, partner_id)
    if row is None:
        return {"ok": False, "reason": "not_found"}
    closed = _refuse_if_closed(row)
    if closed:
        return closed
    u_before = total_units(conn)
    partner_units = units_from_e8(int(row["units_e8"]))
    partner_cost = usdt_from_micro(int(row["cost_micro"]))
    plan = plan_deposit(
        amount=Decimal(amount),
        account_value_incl=Decimal(account_value_incl_deposit),
        total_units=u_before,
        cost=partner_cost,
        units=partner_units,
    )
    if isinstance(plan, Refusal):
        return {"ok": False, "reason": plan.reason}
    assert isinstance(plan, DepositPlan)
    eid = _new_event_id()
    with transaction(conn):
        _update_partner(
            conn,
            partner_id,
            units=plan.units_after,
            cost=plan.cost_after,
            now_ms=now_ms,
        )
        _insert_event(
            conn,
            event_id=eid,
            partner_id=partner_id,
            kind="deposit",
            account_value=Decimal(account_value_incl_deposit),
            total_units_before=u_before,
            nav_value=plan.nav_pre,
            units_delta=plan.units_issued,
            cost_delta=plan.amount,
            cash=plan.amount,
            fee=Decimal("0"),
            units_after=plan.units_after,
            cost_after=plan.cost_after,
            now_ms=now_ms,
            note=note,
        )
    return {
        "ok": True,
        "units_issued": plan.units_issued,
        "units_after": plan.units_after,
        "cost_after": plan.cost_after,
        "nav_pre": plan.nav_pre,
        "event_ids": [eid],
    }


def apply_convert(
    conn: sqlite3.Connection,
    partner_id: str,
    account_value: Decimal,
    now_ms: int,
    request_id: str | None = None,
) -> dict[str, Any]:
    row = get_partner(conn, partner_id)
    if row is None:
        return {"ok": False, "reason": "not_found"}
    closed = _refuse_if_closed(row)
    if closed:
        return closed
    no_admin = _require_admin_for_fee(conn, int(row["fee_rate_bps"]))
    if no_admin:
        return no_admin
    u_before = total_units(conn)
    n = nav(Decimal(account_value), u_before)
    if isinstance(n, Refusal):
        return {"ok": False, "reason": n.reason}
    plan = plan_convert(
        units=units_from_e8(int(row["units_e8"])),
        cost=usdt_from_micro(int(row["cost_micro"])),
        fee_rate=_fee_rate_from_bps(int(row["fee_rate_bps"])),
        nav=n,
    )
    if isinstance(plan, Refusal):
        return {"ok": False, "reason": plan.reason}
    assert isinstance(plan, ConvertPlan)
    eid = _new_event_id()
    event_ids = [eid]
    with transaction(conn):
        _update_partner(
            conn,
            partner_id,
            units=plan.units_after,
            cost=plan.cost_after,
            now_ms=now_ms,
        )
        _insert_event(
            conn,
            event_id=eid,
            partner_id=partner_id,
            kind="convert",
            account_value=Decimal(account_value),
            total_units_before=u_before,
            nav_value=plan.nav,
            units_delta=-plan.fee_units,
            cost_delta=plan.cost_after - usdt_from_micro(int(row["cost_micro"])),
            cash=Decimal("0"),
            fee=plan.fee,
            units_after=plan.units_after,
            cost_after=plan.cost_after,
            now_ms=now_ms,
            request_id=request_id,
        )
        fee_eid = _credit_fee_to_admin(
            conn,
            fee_units=plan.fee_units,
            fee=plan.fee,
            account_value=Decimal(account_value),
            total_units_before=u_before,
            nav_value=plan.nav,
            related_event_id=eid,
            now_ms=now_ms,
        )
        if fee_eid:
            event_ids.append(fee_eid)
    return {
        "ok": True,
        "fee": plan.fee,
        "fee_units": plan.fee_units,
        "units_after": plan.units_after,
        "cost_after": plan.cost_after,
        "event_ids": event_ids,
    }


def apply_withdraw_profit(
    conn: sqlite3.Connection,
    partner_id: str,
    account_value: Decimal,
    now_ms: int,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Withdraw profit share. ``account_value`` is V before cash leaves the wallet."""
    row = get_partner(conn, partner_id)
    if row is None:
        return {"ok": False, "reason": "not_found"}
    closed = _refuse_if_closed(row)
    if closed:
        return closed
    no_admin = _require_admin_for_fee(conn, int(row["fee_rate_bps"]))
    if no_admin:
        return no_admin
    u_before = total_units(conn)
    n = nav(Decimal(account_value), u_before)
    if isinstance(n, Refusal):
        return {"ok": False, "reason": n.reason}
    plan = plan_withdraw_profit(
        units=units_from_e8(int(row["units_e8"])),
        cost=usdt_from_micro(int(row["cost_micro"])),
        fee_rate=_fee_rate_from_bps(int(row["fee_rate_bps"])),
        nav=n,
    )
    if isinstance(plan, Refusal):
        return {"ok": False, "reason": plan.reason}
    assert isinstance(plan, WithdrawPlan)
    eid = _new_event_id()
    event_ids = [eid]
    units_delta = -(plan.fee_units + plan.redeemed_units)
    with transaction(conn):
        _update_partner(
            conn,
            partner_id,
            units=plan.units_after,
            cost=plan.cost_after,
            now_ms=now_ms,
        )
        _insert_event(
            conn,
            event_id=eid,
            partner_id=partner_id,
            kind="withdraw_profit",
            account_value=Decimal(account_value),
            total_units_before=u_before,
            nav_value=plan.nav,
            units_delta=units_delta,
            cost_delta=Decimal("0"),
            cash=-plan.cash,
            fee=plan.fee,
            units_after=plan.units_after,
            cost_after=plan.cost_after,
            now_ms=now_ms,
            request_id=request_id,
        )
        fee_eid = _credit_fee_to_admin(
            conn,
            fee_units=plan.fee_units,
            fee=plan.fee,
            account_value=Decimal(account_value),
            total_units_before=u_before,
            nav_value=plan.nav,
            related_event_id=eid,
            now_ms=now_ms,
        )
        if fee_eid:
            event_ids.append(fee_eid)
    return {
        "ok": True,
        "cash": plan.cash,
        "fee": plan.fee,
        "fee_units": plan.fee_units,
        "redeemed_units": plan.redeemed_units,
        "units_after": plan.units_after,
        "cost_after": plan.cost_after,
        "event_ids": event_ids,
    }


def apply_close(
    conn: sqlite3.Connection,
    partner_id: str,
    account_value: Decimal,
    now_ms: int,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Close partner. ``account_value`` is V before cash leaves the wallet."""
    row = get_partner(conn, partner_id)
    if row is None:
        return {"ok": False, "reason": "not_found"}
    closed = _refuse_if_closed(row)
    if closed:
        return closed
    no_admin = _require_admin_for_fee(conn, int(row["fee_rate_bps"]))
    if no_admin:
        return no_admin
    u_before = total_units(conn)
    n = nav(Decimal(account_value), u_before)
    if isinstance(n, Refusal):
        return {"ok": False, "reason": n.reason}
    partner_units = units_from_e8(int(row["units_e8"]))
    partner_cost = usdt_from_micro(int(row["cost_micro"]))
    plan = plan_close(
        units=partner_units,
        cost=partner_cost,
        fee_rate=_fee_rate_from_bps(int(row["fee_rate_bps"])),
        nav=n,
    )
    if isinstance(plan, Refusal):
        return {"ok": False, "reason": plan.reason}
    assert isinstance(plan, ClosePlan)
    eid = _new_event_id()
    event_ids = [eid]
    # Units removed from partner exclude fee_units (those move to admin); remainder burned for cash.
    partner_units_to_remove = partner_units - plan.fee_units
    with transaction(conn):
        _update_partner(
            conn,
            partner_id,
            units=plan.units_after,
            cost=plan.cost_after,
            now_ms=now_ms,
            status="closed",
        )
        _insert_event(
            conn,
            event_id=eid,
            partner_id=partner_id,
            kind="close",
            account_value=Decimal(account_value),
            total_units_before=u_before,
            nav_value=plan.nav,
            units_delta=-partner_units,
            cost_delta=-partner_cost,
            cash=-plan.cash,
            fee=plan.fee,
            units_after=plan.units_after,
            cost_after=plan.cost_after,
            now_ms=now_ms,
            request_id=request_id,
        )
        fee_eid = _credit_fee_to_admin(
            conn,
            fee_units=plan.fee_units,
            fee=plan.fee,
            account_value=Decimal(account_value),
            total_units_before=u_before,
            nav_value=plan.nav,
            related_event_id=eid,
            now_ms=now_ms,
        )
        if fee_eid:
            event_ids.append(fee_eid)
        # After fee credit, partner units already 0; fee units are on admin.
        # Units burned for cash = partner_units_to_remove (no row needed beyond close).
        _ = partner_units_to_remove
    return {
        "ok": True,
        "cash": plan.cash,
        "fee": plan.fee,
        "fee_units": plan.fee_units,
        "units_after": plan.units_after,
        "cost_after": plan.cost_after,
        "event_ids": event_ids,
    }


def apply_adjust_cost(
    conn: sqlite3.Connection,
    partner_id: str,
    new_cost: Decimal,
    reason: str,
    account_value: Decimal,
    now_ms: int,
) -> dict[str, Any]:
    row = get_partner(conn, partner_id)
    if row is None:
        return {"ok": False, "reason": "not_found"}
    closed = _refuse_if_closed(row)
    if closed:
        return closed
    partner_units = units_from_e8(int(row["units_e8"]))
    partner_cost = usdt_from_micro(int(row["cost_micro"]))
    plan = plan_adjust_cost(
        units=partner_units,
        cost=partner_cost,
        new_cost=Decimal(new_cost),
        reason=reason,
    )
    if isinstance(plan, Refusal):
        return {"ok": False, "reason": plan.reason}
    u_before = total_units(conn)
    n = nav(Decimal(account_value), u_before)
    if isinstance(n, Refusal):
        return {"ok": False, "reason": n.reason}
    eid = _new_event_id()
    with transaction(conn):
        _update_partner(
            conn,
            partner_id,
            units=plan.units_after,
            cost=plan.cost_after,
            now_ms=now_ms,
        )
        _insert_event(
            conn,
            event_id=eid,
            partner_id=partner_id,
            kind="adjust_cost",
            account_value=Decimal(account_value),
            total_units_before=u_before,
            nav_value=n,
            units_delta=Decimal("0"),
            cost_delta=plan.cost_delta,
            cash=Decimal("0"),
            fee=Decimal("0"),
            units_after=plan.units_after,
            cost_after=plan.cost_after,
            now_ms=now_ms,
            note=reason,
        )
    return {
        "ok": True,
        "cost_after": plan.cost_after,
        "units_after": plan.units_after,
        "event_ids": [eid],
    }


def verify_ledger(conn: sqlite3.Connection) -> dict[str, Any]:
    """Fold partner_event from zero and compare to cached partner rows."""
    mismatches: list[dict[str, Any]] = []
    partners = list_partners(conn)
    for p in partners:
        pid = str(p["id"])
        units = Decimal("0")
        cost = Decimal("0")
        events = conn.execute(
            """
            SELECT kind, units_delta_e8, cost_delta_micro, units_after_e8, cost_after_micro
            FROM partner_event
            WHERE partner_id = ?
            ORDER BY created_at_ms, id
            """,
            (pid,),
        ).fetchall()
        for ev in events:
            units = units + units_from_e8(int(ev["units_delta_e8"]))
            cost = cost + usdt_from_micro(int(ev["cost_delta_micro"]))
            # Prefer event's after snapshot when present (handles close zeros)
            if ev["kind"] == "close":
                units = Decimal("0")
                cost = Decimal("0")
        cached_u = units_from_e8(int(p["units_e8"]))
        cached_c = usdt_from_micro(int(p["cost_micro"]))
        if units != cached_u or cost != cached_c:
            mismatches.append(
                {
                    "partner_id": pid,
                    "folded_units": units,
                    "cached_units": cached_u,
                    "folded_cost": cost,
                    "cached_cost": cached_c,
                }
            )
    return {"ok": len(mismatches) == 0, "mismatches": mismatches}
