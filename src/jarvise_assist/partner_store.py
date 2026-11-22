"""SQLite persistence for the unit/NAV partner ledger.

Money: USDT as INTEGER micro (×1e6), units as INTEGER ×1e8.
Every write runs inside ``jarvise_ingest.db.transaction``. No HTTP, no Telegram.

Callers pass the raw futures-wallet value. The store uses
``V_pool = account_value - payouts_due_total`` for NAV so confirmed-but-unpaid
cash does not inflate everyone else's mark.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import unicodedata
import uuid
from decimal import ROUND_DOWN, Decimal
from typing import Any

from jarvise_assist import partner_fx as fx
from jarvise_assist import partner_kyc as pk
from jarvise_assist.partner_ledger import (
    ClosePlan,
    ConvertPlan,
    DepositPlan,
    OwnerDepositPlan,
    OwnerRedeemPlan,
    Refusal,
    WithdrawPlan,
    nav,
    plan_adjust_cost,
    plan_close,
    plan_convert,
    plan_deposit,
    plan_owner_deposit,
    plan_owner_redeem,
    plan_withdraw_profit,
    value_of,
)
from jarvise_ingest.db import transaction

MICRO_SCALE = Decimal("1000000")
UNIT_SCALE = Decimal("100000000")
BPS = Decimal("10000")
USDT_QUANT = Decimal("0.000001")

REQUEST_TTL_MS = 86_400_000
INVITE_TTL_MS = 86_400_000
MIN_DEPOSIT_USDT = Decimal("50")
_REQUEST_KINDS = frozenset({"deposit", "convert", "withdraw_profit", "close"})
_REQUEST_SOURCES = frozenset({"admin", "portal"})
_CANCEL_ON_CONFIRM = frozenset({"no_profit", "below_min"})


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


def _new_request_id() -> str:
    return f"pr_{uuid.uuid4().hex[:16]}"


def fee_rate_from_bps(bps: int) -> Decimal:
    return Decimal(int(bps)) / BPS


def _bad_number(value: Decimal) -> dict[str, Any] | None:
    if not Decimal(value).is_finite():
        return {"ok": False, "reason": "bad_number"}
    return None


def _q_usdt(value: Decimal) -> Decimal:
    return Decimal(value).quantize(USDT_QUANT, rounding=ROUND_DOWN)


def name_key(name: str) -> str:
    """NFKC, trimmed, case-folded identity of a partner name."""
    return unicodedata.normalize("NFKC", str(name)).strip().casefold()


def create_partner(
    conn: sqlite3.Connection,
    *,
    name: str,
    is_admin: bool = False,
    now_ms: int,
    fee_rate_bps: int = 5000,
) -> dict[str, Any]:
    """Insert a partner row. Admin forces fee_rate_bps = 0.

    The name check and the insert share one transaction. A second admin still
    raises ``IntegrityError`` from the single-admin index.
    """
    stored = str(name).strip()
    key = name_key(stored)
    if not key:
        return {"ok": False, "reason": "bad_name"}
    pid = _new_partner_id()
    fee = 0 if is_admin else int(fee_rate_bps)
    ts = int(now_ms)
    with transaction(conn):
        taken = conn.execute("SELECT name FROM partner").fetchall()
        if any(name_key(str(row["name"])) == key for row in taken):
            return {"ok": False, "reason": "duplicate_name"}
        conn.execute(
            """
            INSERT INTO partner (
                id, name, is_admin, fee_rate_bps, units_e8, cost_micro, status,
                created_at_ms, updated_at_ms
            ) VALUES (?, ?, ?, ?, 0, 0, 'active', ?, ?)
            """,
            (pid, stored, 1 if is_admin else 0, fee, ts, ts),
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


def payouts_due_total(conn: sqlite3.Connection) -> Decimal:
    row = conn.execute(
        "SELECT COALESCE(SUM(payout_micro), 0) AS s FROM partner_request WHERE payout_state = 'due'"
    ).fetchone()
    return usdt_from_micro(int(row["s"]))


def create_invite(conn: sqlite3.Connection, partner_id: str, *, now_ms: int) -> dict[str, Any]:
    """Issue a one-time invite code; store only the sha256 hash."""
    row = get_partner(conn, partner_id)
    if row is None:
        return {"ok": False, "reason": "not_found"}
    if str(row["status"]) == "closed":
        return {"ok": False, "reason": "closed"}
    if int(row["is_admin"]) == 1:
        return {"ok": False, "reason": "admin_no_invite"}
    if row["telegram_user_id"] is not None:
        return {"ok": False, "reason": "already_bound"}
    code = secrets.token_urlsafe(24)
    digest = hashlib.sha256(code.encode()).hexdigest()
    expires = int(now_ms) + INVITE_TTL_MS
    with transaction(conn):
        conn.execute(
            """
            UPDATE partner
            SET invite_hash = ?, invite_expires_at_ms = ?, updated_at_ms = ?
            WHERE id = ?
            """,
            (digest, expires, int(now_ms), partner_id),
        )
    return {"ok": True, "code": code, "expires_at_ms": expires}


def bind_invite(
    conn: sqlite3.Connection,
    code: str,
    telegram_user_id: str,
    *,
    admin_chat_id: str,
    now_ms: int,
) -> dict[str, Any]:
    """Bind a Telegram user via invite code, in one transaction.

    A Telegram id still on a closed partner is moved onto the new row only
    when that closed row has no payout due. The old unbind and the new bind
    commit together. A due payout returns ``closed_payout_due`` and writes
    nothing (the new invite hash and the old binding stay as they are).
    There is no reopen: the new row is a separate account.
    """
    tg = str(telegram_user_id)
    if tg == str(admin_chat_id):
        return {"ok": False, "reason": "admin_id"}
    digest = hashlib.sha256(str(code).encode()).hexdigest()
    try:
        with transaction(conn):
            row = conn.execute(
                "SELECT * FROM partner WHERE invite_hash = ?",
                (digest,),
            ).fetchone()
            if (
                row is None
                or str(row["status"]) == "closed"
                or int(row["is_admin"]) == 1
                or row["invite_expires_at_ms"] is None
                or int(row["invite_expires_at_ms"]) <= int(now_ms)
            ):
                return {"ok": False, "reason": "invite_invalid"}
            existing = conn.execute(
                "SELECT * FROM partner WHERE telegram_user_id = ?",
                (tg,),
            ).fetchone()
            if existing is not None:
                if str(existing["id"]) == str(row["id"]) or str(existing["status"]) != "closed":
                    return {"ok": False, "reason": "tg_in_use"}
                if fx.open_settlements(conn, str(existing["id"])):
                    return {"ok": False, "reason": "closed_payout_due"}
                conn.execute(
                    """
                    UPDATE partner
                    SET telegram_user_id = NULL, invite_hash = NULL,
                        invite_expires_at_ms = NULL, updated_at_ms = ?
                    WHERE id = ?
                    """,
                    (int(now_ms), str(existing["id"])),
                )
            conn.execute(
                """
                UPDATE partner
                SET telegram_user_id = ?, invite_hash = NULL, invite_expires_at_ms = NULL,
                    updated_at_ms = ?
                WHERE id = ?
                """,
                (tg, int(now_ms), str(row["id"])),
            )
            return {"ok": True, "partner_id": str(row["id"]), "name": str(row["name"])}
    except sqlite3.IntegrityError:
        return {"ok": False, "reason": "tg_in_use"}


def unbind_partner(conn: sqlite3.Connection, partner_id: str, *, now_ms: int) -> dict[str, Any]:
    with transaction(conn):
        row = get_partner(conn, partner_id)
        if row is None:
            return {"ok": False, "reason": "not_found"}
        conn.execute(
            """
            UPDATE partner
            SET telegram_user_id = NULL, invite_hash = NULL, invite_expires_at_ms = NULL,
                updated_at_ms = ?
            WHERE id = ?
            """,
            (int(now_ms), partner_id),
        )
    return {"ok": True}


def partner_by_telegram_id(conn: sqlite3.Connection, telegram_user_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM partner WHERE telegram_user_id = ?",
        (str(telegram_user_id),),
    ).fetchone()


def get_request(conn: sqlite3.Connection, request_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM partner_request WHERE id = ?", (request_id,)).fetchone()


def list_due_payouts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT * FROM partner_request
            WHERE payout_state = 'due'
            ORDER BY decided_at_ms, id
            """
        )
    )


def close_summary(conn: sqlite3.Connection, partner_id: str) -> dict[str, Any]:
    """Join date, close date, close cash, and every due or paid payout.

    ``close_payout_state`` is the payout state of the confirmed close request
    only (due, paid, none, or None when there is no such request). A paid
    profit withdrawal does not change it.

    Read-only. Used by the closed partner card and ``p closed``. Does not
    read the futures wallet, so it still works when account value cannot be read.
    """
    pid = str(partner_id)
    row = get_partner(conn, pid)
    if row is None:
        return {"ok": False, "reason": "not_found"}
    joined = conn.execute(
        """
        SELECT MIN(created_at_ms) AS m
        FROM partner_event
        WHERE partner_id = ? AND kind = 'deposit'
        """,
        (pid,),
    ).fetchone()
    joined_at = int(joined["m"]) if joined is not None and joined["m"] is not None else None
    close_ev = conn.execute(
        """
        SELECT created_at_ms, cash_micro
        FROM partner_event
        WHERE partner_id = ? AND kind = 'close'
        ORDER BY rowid DESC
        LIMIT 1
        """,
        (pid,),
    ).fetchone()
    if close_ev is None:
        closed_at: int | None = None
        close_cash = Decimal("0")
    else:
        closed_at = int(close_ev["created_at_ms"])
        close_cash = usdt_from_micro(abs(int(close_ev["cash_micro"])))
    dues = [
        {
            "request_id": str(due["id"]),
            "kind": str(due["kind"]),
            "cash": usdt_from_micro(int(due["payout_micro"] or 0)),
            "decided_at_ms": int(due["decided_at_ms"] or due["created_at_ms"]),
        }
        for due in list_due_payouts(conn)
        if str(due["partner_id"]) == pid
    ]
    paid_rows = conn.execute(
        """
        SELECT * FROM partner_request
        WHERE partner_id = ? AND payout_state = 'paid'
        ORDER BY paid_at_ms, id
        """,
        (pid,),
    ).fetchall()
    paid = [
        {
            "request_id": str(item["id"]),
            "kind": str(item["kind"]),
            "cash": usdt_from_micro(int(item["payout_micro"] or 0)),
            "paid_at_ms": int(item["paid_at_ms"] or 0),
        }
        for item in paid_rows
    ]
    close_req = conn.execute(
        """
        SELECT payout_state FROM partner_request
        WHERE partner_id = ? AND kind = 'close' AND status = 'confirmed'
        ORDER BY decided_at_ms DESC, id DESC
        LIMIT 1
        """,
        (pid,),
    ).fetchone()
    close_payout_state = (
        str(close_req["payout_state"])
        if close_req is not None and close_req["payout_state"] is not None
        else None
    )
    return {
        "ok": True,
        "partner_id": pid,
        "name": str(row["name"]),
        "status": str(row["status"]),
        "joined_at_ms": joined_at,
        "closed_at_ms": closed_at,
        "close_cash": close_cash,
        "close_payout_state": close_payout_state,
        "dues": dues,
        "paid": paid,
    }


def list_pending_requests(conn: sqlite3.Connection, *, now_ms: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT * FROM partner_request
            WHERE status = 'pending' AND expires_at_ms > ?
            ORDER BY created_at_ms, id
            """,
            (int(now_ms),),
        )
    )


def _pool_value(account_value: Decimal, due: Decimal) -> Decimal:
    """Pool value from a due figure already read. Does not query."""
    return Decimal(account_value) - Decimal(due)


def view_partner(conn: sqlite3.Connection, partner_id: str, account_value: Decimal) -> dict[str, Any]:
    bad = _bad_number(Decimal(account_value))
    if bad:
        return bad
    v_raw = _q_usdt(Decimal(account_value))
    row = get_partner(conn, partner_id)
    if row is None:
        return {"ok": False, "reason": "not_found"}
    u_total = total_units(conn)
    due = payouts_due_total(conn)
    v_pool = _pool_value(v_raw, due)
    n = nav(v_pool, u_total)
    if isinstance(n, Refusal):
        return {"ok": False, "reason": n.reason}
    v = value_of(
        units=units_from_e8(int(row["units_e8"])),
        cost=usdt_from_micro(int(row["cost_micro"])),
        fee_rate=fee_rate_from_bps(int(row["fee_rate_bps"])),
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
    if int(fee_rate_bps) <= 0:
        return None
    admin = admin_partner(conn)
    if admin is None or str(admin["status"]) != "active":
        return {"ok": False, "reason": "no_admin"}
    return None


def _has_pending_deposit(conn: sqlite3.Connection, now_ms: int) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM partner_request
        WHERE kind = 'deposit' AND status = 'pending' AND expires_at_ms > ?
        LIMIT 1
        """,
        (int(now_ms),),
    ).fetchone()
    return row is not None


def _request_origin(source: object) -> str | None:
    """Map a partner_request source onto partner_event.origin.

    ``admin`` (Telegram owner command) is ``manual``. ``portal`` stays ``portal``.
    NULL or any other source is left NULL.
    """
    if source == "admin":
        return "manual"
    if source == "portal":
        return "portal"
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
    payouts_due: Decimal,
    request_id: str | None = None,
    related_event_id: str | None = None,
    note: str | None = None,
    actor: str = "admin",
    effective_at_ms: int | None = None,
    origin: str | None = None,
) -> None:
    """Append one partner_event. ``payouts_due`` is required and is not re-read."""
    conn.execute(
        """
        INSERT INTO partner_event (
            id, partner_id, kind, request_id, related_event_id,
            account_value_micro, total_units_before_e8, nav,
            units_delta_e8, cost_delta_micro, cash_micro, fee_micro,
            units_after_e8, cost_after_micro, actor, note, created_at_ms,
            effective_at_ms, origin, payouts_due_micro
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            None if effective_at_ms is None else int(effective_at_ms),
            origin,
            usdt_to_micro(payouts_due),
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
        cur = conn.execute(
            """
            UPDATE partner
            SET units_e8 = ?, cost_micro = ?, updated_at_ms = ?
            WHERE id = ? AND status = 'active'
            """,
            (units_to_e8(units), usdt_to_micro(cost), int(now_ms), partner_id),
        )
    else:
        cur = conn.execute(
            """
            UPDATE partner
            SET units_e8 = ?, cost_micro = ?, status = ?, updated_at_ms = ?
            WHERE id = ? AND status = 'active'
            """,
            (units_to_e8(units), usdt_to_micro(cost), status, int(now_ms), partner_id),
        )
    if cur.rowcount != 1:
        raise RuntimeError("partner_changed")


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
    payouts_due: Decimal,
    origin: str | None,
) -> str | None:
    if fee_units <= 0:
        return None
    admin = admin_partner(conn)
    if admin is None or str(admin["status"]) != "active":
        raise RuntimeError("no_admin")
    admin_id = str(admin["id"])
    admin_units = units_from_e8(int(admin["units_e8"]))
    admin_cost = usdt_from_micro(int(admin["cost_micro"]))
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
        payouts_due=payouts_due,
        related_event_id=related_event_id,
        origin=origin,
    )
    return eid


def _insert_confirmed_payout_request(
    conn: sqlite3.Connection,
    *,
    partner_id: str,
    kind: str,
    cash: Decimal,
    now_ms: int,
    request_id: str | None = None,
) -> str:
    rid = request_id if request_id is not None else _new_request_id()
    ts = int(now_ms)
    payout_kyc_id: str | None = None
    if cash > 0:
        payout_state = "due"
        payout_micro: int | None = usdt_to_micro(cash)
        current = pk.current_kyc(conn, partner_id)
        if current is not None:
            payout_kyc_id = str(current["id"])
    else:
        payout_state = "none"
        payout_micro = None
    conn.execute(
        """
        INSERT INTO partner_request (
            id, partner_id, kind, status, payout_state, payout_micro,
            created_at_ms, expires_at_ms, decided_at_ms, source, payout_kyc_id
        ) VALUES (?, ?, ?, 'confirmed', ?, ?, ?, ?, ?, 'admin', ?)
        """,
        (rid, partner_id, kind, payout_state, payout_micro, ts, ts, ts, payout_kyc_id),
    )
    return rid


def deposit_kyc_required(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    """True when a non-admin partner's first deposit still needs a saved KYC row.

    Partners who already have a deposit event are not blocked. The admin partner
    and ``p init`` are exempt. Inline SELECT; does not import partner_rules.
    """
    if int(row["is_admin"] or 0) == 1:
        return False
    hit = conn.execute(
        """
        SELECT 1 FROM partner_event
        WHERE partner_id = ? AND kind = 'deposit'
        LIMIT 1
        """,
        (str(row["id"]),),
    ).fetchone()
    if hit is not None:
        return False
    return pk.kyc_state(conn, str(row["id"])) == "none"


def _apply_deposit_tx(
    conn: sqlite3.Connection,
    partner_id: str,
    amount: Decimal,
    account_value_incl_deposit: Decimal,
    now_ms: int,
    note: str | None = None,
    request_id: str | None = None,
    origin: str | None = None,
) -> dict[str, Any]:
    bad = _bad_number(amount) or _bad_number(account_value_incl_deposit)
    if bad:
        return bad
    amount_q = _q_usdt(amount)
    v_raw = _q_usdt(account_value_incl_deposit)
    row = get_partner(conn, partner_id)
    if row is None:
        return {"ok": False, "reason": "not_found"}
    closed = _refuse_if_closed(row)
    if closed:
        return closed
    if deposit_kyc_required(conn, row):
        return {"ok": False, "reason": "kyc_required"}
    u_before = total_units(conn)
    due = payouts_due_total(conn)
    v_pool = _pool_value(v_raw, due)
    partner_units = units_from_e8(int(row["units_e8"]))
    partner_cost = usdt_from_micro(int(row["cost_micro"]))
    plan = plan_deposit(
        amount=amount_q,
        account_value_incl=v_pool,
        total_units=u_before,
        cost=partner_cost,
        units=partner_units,
    )
    if isinstance(plan, Refusal):
        return {"ok": False, "reason": plan.reason}
    assert isinstance(plan, DepositPlan)
    eid = _new_event_id()
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
        account_value=v_raw,
        total_units_before=u_before,
        nav_value=plan.nav_pre,
        units_delta=plan.units_issued,
        cost_delta=plan.amount,
        cash=plan.amount,
        fee=Decimal("0"),
        units_after=plan.units_after,
        cost_after=plan.cost_after,
        now_ms=now_ms,
        payouts_due=due,
        request_id=request_id,
        note=note,
        origin=origin,
    )
    return {
        "ok": True,
        "units_issued": plan.units_issued,
        "units_after": plan.units_after,
        "cost_after": plan.cost_after,
        "nav_pre": plan.nav_pre,
        "event_ids": [eid],
        "cash": Decimal("0"),
    }


def _apply_convert_tx(
    conn: sqlite3.Connection,
    partner_id: str,
    account_value: Decimal,
    now_ms: int,
    request_id: str | None = None,
    origin: str | None = None,
) -> dict[str, Any]:
    bad = _bad_number(account_value)
    if bad:
        return bad
    v_raw = _q_usdt(account_value)
    row = get_partner(conn, partner_id)
    if row is None:
        return {"ok": False, "reason": "not_found"}
    closed = _refuse_if_closed(row)
    if closed:
        return closed
    if _has_pending_deposit(conn, now_ms):
        return {"ok": False, "reason": "deposit_pending"}
    no_admin = _require_admin_for_fee(conn, int(row["fee_rate_bps"]))
    if no_admin:
        return no_admin
    u_before = total_units(conn)
    due = payouts_due_total(conn)
    v_pool = _pool_value(v_raw, due)
    n = nav(v_pool, u_before)
    if isinstance(n, Refusal):
        return {"ok": False, "reason": n.reason}
    plan = plan_convert(
        units=units_from_e8(int(row["units_e8"])),
        cost=usdt_from_micro(int(row["cost_micro"])),
        fee_rate=fee_rate_from_bps(int(row["fee_rate_bps"])),
        nav=n,
    )
    if isinstance(plan, Refusal):
        return {"ok": False, "reason": plan.reason}
    assert isinstance(plan, ConvertPlan)
    eid = _new_event_id()
    event_ids = [eid]
    cost_before = usdt_from_micro(int(row["cost_micro"]))
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
        account_value=v_raw,
        total_units_before=u_before,
        nav_value=plan.nav,
        units_delta=-plan.fee_units,
        cost_delta=plan.cost_after - cost_before,
        cash=Decimal("0"),
        fee=plan.fee,
        units_after=plan.units_after,
        cost_after=plan.cost_after,
        now_ms=now_ms,
        payouts_due=due,
        request_id=request_id,
        origin=origin,
    )
    fee_eid = _credit_fee_to_admin(
        conn,
        fee_units=plan.fee_units,
        fee=plan.fee,
        account_value=v_raw,
        total_units_before=u_before,
        nav_value=plan.nav,
        related_event_id=eid,
        now_ms=now_ms,
        payouts_due=due,
        origin=origin,
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
        "cash": Decimal("0"),
    }


def _apply_withdraw_profit_tx(
    conn: sqlite3.Connection,
    partner_id: str,
    account_value: Decimal,
    now_ms: int,
    request_id: str | None = None,
    origin: str | None = None,
) -> dict[str, Any]:
    bad = _bad_number(account_value)
    if bad:
        return bad
    v_raw = _q_usdt(account_value)
    row = get_partner(conn, partner_id)
    if row is None:
        return {"ok": False, "reason": "not_found"}
    closed = _refuse_if_closed(row)
    if closed:
        return closed
    if _has_pending_deposit(conn, now_ms):
        return {"ok": False, "reason": "deposit_pending"}
    no_admin = _require_admin_for_fee(conn, int(row["fee_rate_bps"]))
    if no_admin:
        return no_admin
    u_before = total_units(conn)
    due = payouts_due_total(conn)
    v_pool = _pool_value(v_raw, due)
    n = nav(v_pool, u_before)
    if isinstance(n, Refusal):
        return {"ok": False, "reason": n.reason}
    plan = plan_withdraw_profit(
        units=units_from_e8(int(row["units_e8"])),
        cost=usdt_from_micro(int(row["cost_micro"])),
        fee_rate=fee_rate_from_bps(int(row["fee_rate_bps"])),
        nav=n,
    )
    if isinstance(plan, Refusal):
        return {"ok": False, "reason": plan.reason}
    assert isinstance(plan, WithdrawPlan)
    eid = _new_event_id()
    event_ids = [eid]
    units_delta = -(plan.fee_units + plan.redeemed_units)
    out_request_id = request_id if request_id is not None else _new_request_id()
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
        account_value=v_raw,
        total_units_before=u_before,
        nav_value=plan.nav,
        units_delta=units_delta,
        cost_delta=Decimal("0"),
        cash=-plan.cash,
        fee=plan.fee,
        units_after=plan.units_after,
        cost_after=plan.cost_after,
        now_ms=now_ms,
        payouts_due=due,
        request_id=out_request_id,
        origin=origin,
    )
    fee_eid = _credit_fee_to_admin(
        conn,
        fee_units=plan.fee_units,
        fee=plan.fee,
        account_value=v_raw,
        total_units_before=u_before,
        nav_value=plan.nav,
        related_event_id=eid,
        now_ms=now_ms,
        payouts_due=due,
        origin=origin,
    )
    if fee_eid:
        event_ids.append(fee_eid)
    if request_id is None:
        _insert_confirmed_payout_request(
            conn,
            partner_id=partner_id,
            kind="withdraw_profit",
            cash=plan.cash,
            now_ms=now_ms,
            request_id=out_request_id,
        )
    return {
        "ok": True,
        "cash": plan.cash,
        "fee": plan.fee,
        "fee_units": plan.fee_units,
        "redeemed_units": plan.redeemed_units,
        "units_after": plan.units_after,
        "cost_after": plan.cost_after,
        "event_ids": event_ids,
        "request_id": out_request_id,
    }


def _apply_close_tx(
    conn: sqlite3.Connection,
    partner_id: str,
    account_value: Decimal,
    now_ms: int,
    request_id: str | None = None,
    origin: str | None = None,
) -> dict[str, Any]:
    bad = _bad_number(account_value)
    if bad:
        return bad
    v_raw = _q_usdt(account_value)
    row = get_partner(conn, partner_id)
    if row is None:
        return {"ok": False, "reason": "not_found"}
    closed = _refuse_if_closed(row)
    if closed:
        return closed
    if _has_pending_deposit(conn, now_ms):
        return {"ok": False, "reason": "deposit_pending"}
    no_admin = _require_admin_for_fee(conn, int(row["fee_rate_bps"]))
    if no_admin:
        return no_admin
    if int(row["is_admin"]) == 1:
        other = conn.execute(
            """
            SELECT 1 FROM partner
            WHERE id != ? AND status = 'active' AND units_e8 > 0
            LIMIT 1
            """,
            (partner_id,),
        ).fetchone()
        if other is not None:
            return {"ok": False, "reason": "admin_has_partners"}
    u_before = total_units(conn)
    due = payouts_due_total(conn)
    v_pool = _pool_value(v_raw, due)
    n = nav(v_pool, u_before)
    if isinstance(n, Refusal):
        return {"ok": False, "reason": n.reason}
    partner_units = units_from_e8(int(row["units_e8"]))
    partner_cost = usdt_from_micro(int(row["cost_micro"]))
    plan = plan_close(
        units=partner_units,
        cost=partner_cost,
        fee_rate=fee_rate_from_bps(int(row["fee_rate_bps"])),
        nav=n,
    )
    if isinstance(plan, Refusal):
        return {"ok": False, "reason": plan.reason}
    assert isinstance(plan, ClosePlan)
    eid = _new_event_id()
    event_ids = [eid]
    out_request_id = request_id if request_id is not None else _new_request_id()
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
        account_value=v_raw,
        total_units_before=u_before,
        nav_value=plan.nav,
        units_delta=-partner_units,
        cost_delta=-partner_cost,
        cash=-plan.cash,
        fee=plan.fee,
        units_after=plan.units_after,
        cost_after=plan.cost_after,
        now_ms=now_ms,
        payouts_due=due,
        request_id=out_request_id,
        origin=origin,
    )
    fee_eid = _credit_fee_to_admin(
        conn,
        fee_units=plan.fee_units,
        fee=plan.fee,
        account_value=v_raw,
        total_units_before=u_before,
        nav_value=plan.nav,
        related_event_id=eid,
        now_ms=now_ms,
        payouts_due=due,
        origin=origin,
    )
    if fee_eid:
        event_ids.append(fee_eid)
    if request_id is None:
        _insert_confirmed_payout_request(
            conn,
            partner_id=partner_id,
            kind="close",
            cash=plan.cash,
            now_ms=now_ms,
            request_id=out_request_id,
        )
    return {
        "ok": True,
        "cash": plan.cash,
        "fee": plan.fee,
        "fee_units": plan.fee_units,
        "units_after": plan.units_after,
        "cost_after": plan.cost_after,
        "event_ids": event_ids,
        "request_id": out_request_id,
    }


def _apply_adjust_cost_tx(
    conn: sqlite3.Connection,
    partner_id: str,
    new_cost: Decimal,
    reason: str,
    account_value: Decimal,
    now_ms: int,
    origin: str | None = None,
) -> dict[str, Any]:
    bad = _bad_number(new_cost) or _bad_number(account_value)
    if bad:
        return bad
    new_cost_q = _q_usdt(new_cost)
    v_raw = _q_usdt(account_value)
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
        new_cost=new_cost_q,
        reason=reason,
    )
    if isinstance(plan, Refusal):
        return {"ok": False, "reason": plan.reason}
    u_before = total_units(conn)
    due = payouts_due_total(conn)
    v_pool = _pool_value(v_raw, due)
    n = nav(v_pool, u_before)
    if isinstance(n, Refusal):
        return {"ok": False, "reason": n.reason}
    eid = _new_event_id()
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
        account_value=v_raw,
        total_units_before=u_before,
        nav_value=n,
        units_delta=Decimal("0"),
        cost_delta=plan.cost_delta,
        cash=Decimal("0"),
        fee=Decimal("0"),
        units_after=plan.units_after,
        cost_after=plan.cost_after,
        now_ms=now_ms,
        payouts_due=due,
        note=reason,
        origin=origin,
    )
    return {
        "ok": True,
        "cost_after": plan.cost_after,
        "units_after": plan.units_after,
        "event_ids": [eid],
    }


def apply_deposit(
    conn: sqlite3.Connection,
    partner_id: str,
    amount: Decimal,
    account_value_incl_deposit: Decimal,
    now_ms: int,
    note: str | None = None,
) -> dict[str, Any]:
    with transaction(conn):
        return _apply_deposit_tx(
            conn,
            partner_id,
            amount,
            account_value_incl_deposit,
            now_ms,
            note=note,
            origin="manual",
        )


def apply_convert(
    conn: sqlite3.Connection,
    partner_id: str,
    account_value: Decimal,
    now_ms: int,
) -> dict[str, Any]:
    with transaction(conn):
        return _apply_convert_tx(conn, partner_id, account_value, now_ms, origin="manual")


def apply_withdraw_profit(
    conn: sqlite3.Connection,
    partner_id: str,
    account_value: Decimal,
    now_ms: int,
) -> dict[str, Any]:
    """Withdraw profit share. ``account_value`` is the raw wallet value."""
    with transaction(conn):
        return _apply_withdraw_profit_tx(conn, partner_id, account_value, now_ms, origin="manual")


def apply_close(
    conn: sqlite3.Connection,
    partner_id: str,
    account_value: Decimal,
    now_ms: int,
) -> dict[str, Any]:
    """Close partner. ``account_value`` is the raw wallet value."""
    with transaction(conn):
        return _apply_close_tx(conn, partner_id, account_value, now_ms, origin="manual")


def apply_adjust_cost(
    conn: sqlite3.Connection,
    partner_id: str,
    new_cost: Decimal,
    reason: str,
    account_value: Decimal,
    now_ms: int,
) -> dict[str, Any]:
    with transaction(conn):
        return _apply_adjust_cost_tx(
            conn, partner_id, new_cost, reason, account_value, now_ms, origin="manual"
        )


def create_request(
    conn: sqlite3.Connection,
    partner_id: str,
    kind: str,
    now_ms: int,
    amount: Decimal | None = None,
    *,
    source: str,
) -> dict[str, Any]:
    """Create a pending partner_request.

    For kind ``deposit``, ``quote_gross_micro`` holds the amount the partner
    states; all other quote_* columns stay NULL in this phase. ``source`` is
    required: ``portal`` when the partner submitted it, ``admin`` for owner
    commands. Unknown values return ``bad_source``.
    """
    if kind not in _REQUEST_KINDS:
        return {"ok": False, "reason": "bad_kind"}
    if source not in _REQUEST_SOURCES:
        return {"ok": False, "reason": "bad_source"}
    with transaction(conn):
        quote_gross: int | None = None
        if kind == "deposit":
            if amount is None:
                return {"ok": False, "reason": "bad_args"}
            bad = _bad_number(amount)
            if bad:
                return bad
            amount_q = _q_usdt(amount)
            if amount_q < MIN_DEPOSIT_USDT:
                return {"ok": False, "reason": "below_min_deposit"}
            quote_gross = usdt_to_micro(amount_q)
        elif amount is not None:
            return {"ok": False, "reason": "bad_args"}

        row = get_partner(conn, partner_id)
        if row is None:
            return {"ok": False, "reason": "not_found"}
        if str(row["status"]) == "closed":
            return {"ok": False, "reason": "closed"}
        if kind == "deposit" and deposit_kyc_required(conn, row):
            return {"ok": False, "reason": "kyc_required"}

        ts = int(now_ms)
        conn.execute(
            """
            UPDATE partner_request
            SET status = 'expired', decided_at_ms = ?
            WHERE partner_id = ? AND status = 'pending' AND expires_at_ms <= ?
            """,
            (ts, partner_id, ts),
        )
        pending = conn.execute(
            """
            SELECT 1 FROM partner_request
            WHERE partner_id = ? AND status = 'pending'
            LIMIT 1
            """,
            (partner_id,),
        ).fetchone()
        if pending is not None:
            return {"ok": False, "reason": "request_pending"}

        rid = _new_request_id()
        expires = ts + REQUEST_TTL_MS
        conn.execute(
            """
            INSERT INTO partner_request (
                id, partner_id, kind, status, payout_state,
                quote_gross_micro, created_at_ms, expires_at_ms, source
            ) VALUES (?, ?, ?, 'pending', 'none', ?, ?, ?, ?)
            """,
            (rid, partner_id, kind, quote_gross, ts, expires, source),
        )
        return {"ok": True, "request_id": rid, "expires_at_ms": expires}


def _cancel_request(conn: sqlite3.Connection, request_id: str, now_ms: int) -> None:
    conn.execute(
        """
        UPDATE partner_request
        SET status = 'cancelled', decided_at_ms = ?
        WHERE id = ? AND status = 'pending'
        """,
        (int(now_ms), request_id),
    )


def _confirm_request_row(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    now_ms: int,
    cash: Decimal,
    kind: str,
    payout_kyc_id: str | None = None,
) -> None:
    if kind in ("withdraw_profit", "close") and cash > 0:
        payout_state = "due"
        payout_micro: int | None = usdt_to_micro(cash)
    else:
        payout_state = "none"
        payout_micro = None
        payout_kyc_id = None
    cur = conn.execute(
        """
        UPDATE partner_request
        SET status = 'confirmed', decided_at_ms = ?, payout_state = ?, payout_micro = ?,
            payout_kyc_id = ?
        WHERE id = ? AND status = 'pending'
        """,
        (int(now_ms), payout_state, payout_micro, payout_kyc_id, request_id),
    )
    if cur.rowcount != 1:
        raise RuntimeError("request_changed")


def confirm_request(
    conn: sqlite3.Connection,
    request_id: str,
    account_value: Decimal,
    now_ms: int,
    amount_received: Decimal | None = None,
) -> dict[str, Any]:
    with transaction(conn):
        req = conn.execute("SELECT * FROM partner_request WHERE id = ?", (request_id,)).fetchone()
        if req is None or str(req["status"]) != "pending" or int(req["expires_at_ms"]) <= int(now_ms):
            return {"ok": False, "reason": "request_not_pending"}

        kind = str(req["kind"])
        partner_id = str(req["partner_id"])
        origin = _request_origin(req["source"])
        if kind == "deposit":
            if amount_received is None:
                return {"ok": False, "reason": "bad_args"}
            bad = _bad_number(amount_received) or _bad_number(account_value)
            if bad:
                return bad
            amount_q = _q_usdt(amount_received)
            if amount_q <= 0:
                return {"ok": False, "reason": "bad_args"}
            result = _apply_deposit_tx(
                conn,
                partner_id,
                amount_q,
                _q_usdt(account_value),
                now_ms,
                request_id=request_id,
                origin=origin,
            )
        else:
            if amount_received is not None:
                return {"ok": False, "reason": "bad_args"}
            bad = _bad_number(account_value)
            if bad:
                return bad
            v_raw = _q_usdt(account_value)
            if kind == "convert":
                result = _apply_convert_tx(
                    conn, partner_id, v_raw, now_ms, request_id=request_id, origin=origin
                )
            elif kind == "withdraw_profit":
                result = _apply_withdraw_profit_tx(
                    conn, partner_id, v_raw, now_ms, request_id=request_id, origin=origin
                )
            elif kind == "close":
                result = _apply_close_tx(
                    conn, partner_id, v_raw, now_ms, request_id=request_id, origin=origin
                )
            else:
                return {"ok": False, "reason": "bad_kind"}

        if not result.get("ok"):
            reason = str(result.get("reason", ""))
            if reason in _CANCEL_ON_CONFIRM and kind in (
                "convert",
                "withdraw_profit",
            ):
                _cancel_request(conn, request_id, now_ms)
            return result

        cash = Decimal(result.get("cash", Decimal("0")))
        payout_kyc_id: str | None = None
        if kind in ("withdraw_profit", "close") and cash > 0:
            current = pk.current_kyc(conn, partner_id)
            if current is not None:
                payout_kyc_id = str(current["id"])
        _confirm_request_row(
            conn,
            request_id=request_id,
            now_ms=now_ms,
            cash=cash,
            kind=kind,
            payout_kyc_id=payout_kyc_id,
        )
        out = dict(result)
        out["request_id"] = request_id
        return out


def reject_request(conn: sqlite3.Connection, request_id: str, now_ms: int) -> dict[str, Any]:
    with transaction(conn):
        cur = conn.execute(
            """
            UPDATE partner_request
            SET status = 'rejected', decided_at_ms = ?
            WHERE id = ? AND status = 'pending'
            """,
            (int(now_ms), request_id),
        )
        if cur.rowcount != 1:
            return {"ok": False, "reason": "request_not_pending"}
        return {"ok": True}


def expire_requests(conn: sqlite3.Connection, now_ms: int) -> int:
    with transaction(conn):
        cur = conn.execute(
            """
            UPDATE partner_request
            SET status = 'expired', decided_at_ms = ?
            WHERE status = 'pending' AND expires_at_ms <= ?
            """,
            (int(now_ms), int(now_ms)),
        )
        return int(cur.rowcount)


def mark_paid(conn: sqlite3.Connection, request_id: str, now_ms: int) -> dict[str, Any]:
    with transaction(conn):
        cur = conn.execute(
            """
            UPDATE partner_request
            SET payout_state = 'paid', paid_at_ms = ?
            WHERE id = ? AND status = 'confirmed' AND payout_state = 'due'
            """,
            (int(now_ms), request_id),
        )
        if cur.rowcount != 1:
            return {"ok": False, "reason": "not_due"}
        return {"ok": True}


def _owner_amount_refusal(amount: Decimal) -> dict[str, Any] | None:
    dec = Decimal(amount)
    if not dec.is_finite() or dec <= 0:
        return {"ok": False, "reason": "bad_amount"}
    exp = dec.as_tuple().exponent
    if isinstance(exp, int) and exp < -6:
        return {"ok": False, "reason": "bad_amount"}
    return None


def _owner_admin_row(conn: sqlite3.Connection) -> tuple[sqlite3.Row | None, dict[str, Any] | None]:
    admin = admin_partner(conn)
    if admin is None:
        any_partner = conn.execute("SELECT 1 FROM partner LIMIT 1").fetchone()
        if any_partner is None:
            return None, {"ok": False, "reason": "no_admin"}
        return None, {"ok": False, "reason": "not_admin"}
    if int(admin["is_admin"]) != 1:
        return None, {"ok": False, "reason": "not_admin"}
    if str(admin["status"]) == "closed":
        return None, {"ok": False, "reason": "closed"}
    return admin, None


def _apply_owner_tx(
    conn: sqlite3.Connection,
    *,
    kind: str,
    amount: Decimal,
    account_value: Decimal,
    now_ms: int,
) -> dict[str, Any]:
    """Book one owner_redeem or owner_deposit on the admin. No transfer guard."""
    refused = _owner_amount_refusal(amount)
    if refused:
        return refused
    account = Decimal(account_value)
    bad = _bad_number(account)
    if bad:
        return bad
    admin, err = _owner_admin_row(conn)
    if err is not None or admin is None:
        return err if err is not None else {"ok": False, "reason": "no_admin"}
    if _has_pending_deposit(conn, now_ms):
        return {"ok": False, "reason": "deposit_pending"}
    verified = verify_ledger(conn)
    if not verified["ok"]:
        return {"ok": False, "reason": "ledger_unverified"}
    amount_q = _q_usdt(Decimal(amount))
    v_raw = _q_usdt(account)
    due = payouts_due_total(conn)
    u_before = total_units(conn)
    admin_units = units_from_e8(int(admin["units_e8"]))
    admin_cost = usdt_from_micro(int(admin["cost_micro"]))
    if kind == "owner_redeem":
        nav_pre = nav(v_raw + amount_q - due, u_before)
    else:
        nav_pre = nav(v_raw - amount_q - due, u_before)
    if isinstance(nav_pre, Refusal):
        return {"ok": False, "reason": nav_pre.reason}
    if kind == "owner_redeem":
        plan_r = plan_owner_redeem(amount_q, nav_pre, admin_units, admin_cost)
        if isinstance(plan_r, Refusal):
            return {"ok": False, "reason": plan_r.reason}
        assert isinstance(plan_r, OwnerRedeemPlan)
        units_after = plan_r.units_after
        cost_after = plan_r.cost_after
        units_delta = -plan_r.burned_units
        cash = plan_r.cash
        burned = plan_r.burned_units
        minted = Decimal("0")
    else:
        plan_d = plan_owner_deposit(amount_q, nav_pre, admin_units, admin_cost)
        if isinstance(plan_d, Refusal):
            return {"ok": False, "reason": plan_d.reason}
        assert isinstance(plan_d, OwnerDepositPlan)
        units_after = plan_d.units_after
        cost_after = plan_d.cost_after
        units_delta = plan_d.units_issued
        cash = plan_d.cash
        burned = Decimal("0")
        minted = plan_d.units_issued
    admin_id = str(admin["id"])
    eid = _new_event_id()
    _update_partner(conn, admin_id, units=units_after, cost=cost_after, now_ms=now_ms)
    _insert_event(
        conn,
        event_id=eid,
        partner_id=admin_id,
        kind=kind,
        account_value=v_raw,
        total_units_before=u_before,
        nav_value=nav_pre,
        units_delta=units_delta,
        cost_delta=cost_after - admin_cost,
        cash=cash,
        fee=Decimal("0"),
        units_after=units_after,
        cost_after=cost_after,
        now_ms=now_ms,
        payouts_due=due,
        effective_at_ms=int(now_ms),
        origin="manual",
        actor="admin",
    )
    return {
        "ok": True,
        "kind": kind,
        "nav_pre": nav_pre,
        "burned_units": burned,
        "units_issued": minted,
        "units_after": units_after,
        "cost_after": cost_after,
        "cash": cash,
        "fee": Decimal("0"),
        "event_ids": [eid],
        "payouts_due": due,
    }


def apply_owner_redeem(
    conn: sqlite3.Connection,
    *,
    amount: Decimal,
    account_value: Decimal,
    now_ms: int,
) -> dict[str, Any]:
    """Owner withdrawal already out of the wallet. ``account_value`` is the reading after."""
    with transaction(conn):
        return _apply_owner_tx(
            conn,
            kind="owner_redeem",
            amount=amount,
            account_value=account_value,
            now_ms=now_ms,
        )


def apply_owner_deposit(
    conn: sqlite3.Connection,
    *,
    amount: Decimal,
    account_value: Decimal,
    now_ms: int,
) -> dict[str, Any]:
    """Owner deposit already in the wallet. ``account_value`` is the reading after."""
    with transaction(conn):
        return _apply_owner_tx(
            conn,
            kind="owner_deposit",
            amount=amount,
            account_value=account_value,
            now_ms=now_ms,
        )


def verify_ledger(conn: sqlite3.Connection) -> dict[str, Any]:
    """Fold partner_event by rowid and compare to cached partner rows."""
    mismatches: list[dict[str, Any]] = []
    partners = list_partners(conn)
    for p in partners:
        pid = str(p["id"])
        units = Decimal("0")
        cost = Decimal("0")
        events = conn.execute(
            """
            SELECT id, kind, units_delta_e8, cost_delta_micro,
                   units_after_e8, cost_after_micro
            FROM partner_event
            WHERE partner_id = ?
            ORDER BY rowid
            """,
            (pid,),
        ).fetchall()
        last_kind: str | None = None
        for ev in events:
            units = units + units_from_e8(int(ev["units_delta_e8"]))
            cost = cost + usdt_from_micro(int(ev["cost_delta_micro"]))
            after_u = units_from_e8(int(ev["units_after_e8"]))
            after_c = usdt_from_micro(int(ev["cost_after_micro"]))
            if units != after_u or cost != after_c:
                mismatches.append(
                    {
                        "partner_id": pid,
                        "event_id": str(ev["id"]),
                        "folded_units": units,
                        "event_units_after": after_u,
                        "folded_cost": cost,
                        "event_cost_after": after_c,
                    }
                )
                break
            last_kind = str(ev["kind"])
        else:
            cached_u = units_from_e8(int(p["units_e8"]))
            cached_c = usdt_from_micro(int(p["cost_micro"]))
            status = str(p["status"])
            expect_closed = last_kind == "close"
            if expect_closed:
                if status != "closed" or cached_u != 0 or cached_c != 0:
                    mismatches.append(
                        {
                            "partner_id": pid,
                            "status": status,
                            "expected_status": "closed",
                            "cached_units": cached_u,
                            "cached_cost": cached_c,
                        }
                    )
            else:
                if status == "closed":
                    mismatches.append(
                        {
                            "partner_id": pid,
                            "status": status,
                            "expected_status": "active",
                            "last_kind": last_kind,
                        }
                    )
                elif units != cached_u or cost != cached_c:
                    mismatches.append(
                        {
                            "partner_id": pid,
                            "folded_units": units,
                            "cached_units": cached_u,
                            "folded_cost": cost,
                            "cached_cost": cached_c,
                        }
                    )

    requests = conn.execute("SELECT * FROM partner_request").fetchall()
    for req in requests:
        rid = str(req["id"])
        kind = str(req["kind"])
        status = str(req["status"])
        matching = conn.execute(
            """
            SELECT id, kind FROM partner_event
            WHERE request_id = ? AND kind != 'fee_credit'
            """,
            (rid,),
        ).fetchall()
        if status == "confirmed":
            if len(matching) != 1 or str(matching[0]["kind"]) != kind:
                mismatches.append(
                    {
                        "request_id": rid,
                        "status": status,
                        "matching_events": len(matching),
                        "expected_kind": kind,
                    }
                )
            event = (
                conn.execute(
                    """
                    SELECT cash_micro FROM partner_event
                    WHERE request_id = ? AND kind = ?
                    """,
                    (rid, kind),
                ).fetchone()
                if len(matching) == 1
                else None
            )
            payout_state = str(req["payout_state"])
            payout_micro = req["payout_micro"]
            if kind in ("deposit", "convert"):
                if payout_state != "none":
                    mismatches.append({"request_id": rid, "payout_state": payout_state, "expected": "none"})
            elif kind in ("withdraw_profit", "close") and event is not None:
                cash_abs = abs(int(event["cash_micro"]))
                if cash_abs > 0:
                    if payout_state not in ("due", "paid"):
                        mismatches.append(
                            {
                                "request_id": rid,
                                "payout_state": payout_state,
                                "expected": "due|paid",
                            }
                        )
                    if payout_micro is None or int(payout_micro) != cash_abs:
                        mismatches.append(
                            {
                                "request_id": rid,
                                "payout_micro": payout_micro,
                                "cash_micro": cash_abs,
                            }
                        )
                else:
                    if payout_state != "none":
                        mismatches.append(
                            {
                                "request_id": rid,
                                "payout_state": payout_state,
                                "expected": "none",
                            }
                        )
            if payout_state in ("due", "paid") and status != "confirmed":
                mismatches.append({"request_id": rid, "payout_state": payout_state, "status": status})
        else:
            if matching:
                mismatches.append(
                    {
                        "request_id": rid,
                        "status": status,
                        "unexpected_events": len(matching),
                    }
                )
            if str(req["payout_state"]) in ("due", "paid"):
                mismatches.append(
                    {
                        "request_id": rid,
                        "status": status,
                        "payout_state": str(req["payout_state"]),
                    }
                )

    events_all = conn.execute(
        """
        SELECT id, partner_id, kind, request_id
        FROM partner_event
        ORDER BY rowid
        """
    ).fetchall()
    for ev in events_all:
        eid = str(ev["id"])
        kind = str(ev["kind"])
        rid = ev["request_id"]
        if kind in ("owner_deposit", "owner_redeem"):
            # Owner flows have no request_id. Do not flag them as withdraw/close.
            continue
        if kind in ("withdraw_profit", "close") and rid is None:
            mismatches.append({"event_id": eid, "kind": kind, "request_id": None})
            continue
        if rid is None:
            continue
        rid_s = str(rid)
        linked = conn.execute(
            """
            SELECT partner_id, kind, status FROM partner_request
            WHERE id = ?
            """,
            (rid_s,),
        ).fetchone()
        if (
            linked is None
            or str(linked["partner_id"]) != str(ev["partner_id"])
            or str(linked["kind"]) != kind
            or str(linked["status"]) != "confirmed"
        ):
            mismatches.append(
                {
                    "event_id": eid,
                    "request_id": rid_s,
                    "partner_id": str(ev["partner_id"]),
                    "kind": kind,
                }
            )

    mismatches.extend(_owner_flow_invariants(conn))
    return {"ok": len(mismatches) == 0, "mismatches": mismatches}


def _owner_flow_invariants(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """I2 total-units chain, I3 owner NAV identity, I5 non-negative, I6 admin-only."""
    rows = conn.execute(
        """
        SELECT e.id, e.partner_id, e.kind, e.related_event_id,
               e.total_units_before_e8, e.units_delta_e8, e.units_after_e8,
               e.cost_after_micro, e.nav, e.account_value_micro, e.cash_micro,
               e.payouts_due_micro, p.is_admin
        FROM partner_event e
        JOIN partner p ON p.id = e.partner_id
        ORDER BY e.rowid
        """
    ).fetchall()
    out: list[dict[str, Any]] = []
    running = 0
    seen_before: dict[str, int] = {}
    for row in rows:
        eid = str(row["id"])
        kind = str(row["kind"])
        before = int(row["total_units_before_e8"])
        delta_u = int(row["units_delta_e8"])
        units_after = int(row["units_after_e8"])
        cost_after = int(row["cost_after_micro"])
        if kind == "fee_credit":
            rel = row["related_event_id"]
            expected = seen_before.get(str(rel)) if rel is not None else None
            if expected is None or before != expected:
                actual_delta = before - expected if expected is not None else None
                out.append(
                    {
                        "invariant": "I2",
                        "event_id": eid,
                        "expected": expected,
                        "actual": before,
                        "delta": actual_delta,
                    }
                )
        elif before != running:
            out.append(
                {
                    "invariant": "I2",
                    "event_id": eid,
                    "expected": running,
                    "actual": before,
                    "delta": before - running,
                }
            )
        seen_before[eid] = before
        running += delta_u
        if units_after < 0 or cost_after < 0 or before < 0 or running < 0:
            out.append(
                {
                    "invariant": "I5",
                    "event_id": eid,
                    "expected": 0,
                    "actual": min(units_after, cost_after, before, running),
                    "delta": min(units_after, cost_after, before, running),
                }
            )
        if kind in ("owner_deposit", "owner_redeem") and int(row["is_admin"]) != 1:
            out.append(
                {
                    "invariant": "I6",
                    "event_id": eid,
                    "expected": 1,
                    "actual": int(row["is_admin"]),
                    "delta": int(row["is_admin"]) - 1,
                }
            )
        if kind not in ("owner_deposit", "owner_redeem"):
            continue
        if row["payouts_due_micro"] is None:
            continue
        av = int(row["account_value_micro"])
        cash = int(row["cash_micro"])
        due_m = int(row["payouts_due_micro"])
        pool_micro = av - cash - due_m
        stored = Decimal(str(row["nav"]))
        if before <= 0:
            out.append(
                {
                    "invariant": "I3",
                    "event_id": eid,
                    "expected": "positive_units",
                    "actual": before,
                    "delta": before,
                }
            )
            continue
        expected_nav = (Decimal(pool_micro) / MICRO_SCALE) / units_from_e8(before)
        nav_gap = stored - expected_nav
        if abs(nav_gap) > Decimal("1e-12"):
            out.append(
                {
                    "invariant": "I3",
                    "event_id": eid,
                    "expected": format(expected_nav, "f"),
                    "actual": format(stored, "f"),
                    "delta": format(nav_gap, "f"),
                }
            )
        after_e8 = before + delta_u
        pool_after_micro = av - due_m
        if after_e8 == 0:
            if pool_after_micro != 0:
                out.append(
                    {
                        "invariant": "I3",
                        "event_id": eid,
                        "expected": 0,
                        "actual": pool_after_micro,
                        "delta": pool_after_micro,
                    }
                )
            continue
        if after_e8 < 0:
            continue
        nav_after = (Decimal(pool_after_micro) / MICRO_SCALE) / units_from_e8(after_e8)
        post_gap = nav_after - stored
        if abs(post_gap) >= Decimal("1e-9"):
            out.append(
                {
                    "invariant": "I3",
                    "event_id": eid,
                    "expected": format(stored, "f"),
                    "actual": format(nav_after, "f"),
                    "delta": format(post_gap, "f"),
                }
            )
    return out
