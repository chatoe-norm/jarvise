"""One-off historical owner_redeem for the 2026-11-19 150 USDT withdrawal.

No Binance calls, no Telegram, and no HTTP. The default path is a dry-run.
``--apply`` re-checks every pre-state and golden value inside one transaction
and rolls back unless the written row matches.
"""

from __future__ import annotations

import sqlite3
import time
from decimal import Decimal
from typing import Any, Final

from jarvise_assist import partner_ledger as pl
from jarvise_assist import partner_store as ps
from jarvise_ingest.db import transaction

HIST_EVENT_ID: Final = "pe_hist_tran432610282749"
TRAN_ID: Final = "432610282749"
AMOUNT: Final = Decimal("150.000000")
ACCOUNT_VALUE_AFTER: Final = Decimal("289.120000")
PAYOUTS_DUE: Final = Decimal("0")
EFF_MIN_MS: Final = 1_795_092_000_000
EFF_MAX_MS: Final = 1_795_092_779_999
EFF_DEFAULT_MS: Final = 1_795_092_360_000
NOTE: Final = (
    "historical owner_redeem: owner withdrew 150 USDT from USDT-M on 2026-11-19 19:46 ICT "
    "(Binance TRANSFER tranId 432610282749), not booked at the time; recorded by L1 one-off "
    "tool per owner decision 2026-11-22; no fee; NAV from post-transfer wallet 289.12"
)

ADMIN_UNITS_E8: Final = 32_675_756_800
ADMIN_COST_MICRO: Final = 326_757_568
TOTO_UNITS_E8: Final = 9_837_165_076
TOTO_COST_MICRO: Final = 99_750_000
TOTO_FEE_BPS: Final = 5000
TOTAL_UNITS_E8: Final = 42_512_921_876
NAV_Q: Final = Decimal("1.03290948")
NAV_Q_STEP: Final = Decimal("1e-8")
BURN: Final = Decimal("145.22085721")
UNITS_DELTA_E8: Final = -14_522_085_721
ADMIN_UNITS_AFTER: Final = Decimal("181.53671079")
ADMIN_UNITS_AFTER_E8: Final = 18_153_671_079
ADMIN_COST_AFTER: Final = Decimal("181.536710")
ADMIN_COST_AFTER_MICRO: Final = 181_536_710
COST_DELTA_MICRO: Final = ADMIN_COST_AFTER_MICRO - ADMIN_COST_MICRO
CASH_MICRO: Final = -150_000_000
AV_MICRO: Final = 289_120_000
TOTO_BEFORE: Final = Decimal("66.900157")
TOTO_AFTER: Final = Decimal("100.679505")


class HistoricalRecordError(Exception):
    """Apply refused. The transaction rolls back when this is raised inside it."""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(str(payload.get("reason", "refused")))
        self.payload = payload


def effective_in_window(effective_at_ms: int) -> bool:
    return EFF_MIN_MS <= int(effective_at_ms) <= EFF_MAX_MS


def _diff(field: str, expected: object, actual: object) -> dict[str, object]:
    return {"field": field, "expected": _pub(expected), "actual": _pub(actual)}


def _pub(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {str(key): _pub(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_pub(item) for item in value]
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return value
    return str(value)


def _fetch_hist(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM partner_event WHERE id = ?", (HIST_EVENT_ID,)).fetchone()


def _pinned_nav() -> Decimal:
    total = ps.units_from_e8(TOTAL_UNITS_E8)
    nav_pre = pl.nav(ACCOUNT_VALUE_AFTER + AMOUNT - PAYOUTS_DUE, total)
    if isinstance(nav_pre, pl.Refusal):
        raise RuntimeError(nav_pre.reason)
    return nav_pre


def _idempotent(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    """Match an existing hist row to the golden row. Ignore created_at_ms."""
    admin = ps.admin_partner(conn)
    nav_pre = _pinned_nav()
    expected_partner = str(admin["id"]) if admin is not None else None
    effective = row["effective_at_ms"]
    checks: list[tuple[str, object, object]] = [
        ("partner_id", expected_partner, str(row["partner_id"])),
        ("kind", "owner_redeem", str(row["kind"])),
        ("request_id", None, row["request_id"]),
        ("related_event_id", None, row["related_event_id"]),
        ("account_value_micro", AV_MICRO, int(row["account_value_micro"])),
        ("total_units_before_e8", TOTAL_UNITS_E8, int(row["total_units_before_e8"])),
        ("nav", format(nav_pre, "f"), str(row["nav"])),
        ("units_delta_e8", UNITS_DELTA_E8, int(row["units_delta_e8"])),
        ("cost_delta_micro", COST_DELTA_MICRO, int(row["cost_delta_micro"])),
        ("cash_micro", CASH_MICRO, int(row["cash_micro"])),
        ("fee_micro", 0, int(row["fee_micro"])),
        ("units_after_e8", ADMIN_UNITS_AFTER_E8, int(row["units_after_e8"])),
        ("cost_after_micro", ADMIN_COST_AFTER_MICRO, int(row["cost_after_micro"])),
        ("actor", "admin", str(row["actor"])),
        ("note", NOTE, row["note"]),
        ("origin", "manual", row["origin"]),
        ("payouts_due_micro", 0, row["payouts_due_micro"]),
    ]
    diffs = [_diff(field, expected, actual) for field, expected, actual in checks if expected != actual]
    if effective is None or not effective_in_window(int(effective)):
        diffs.append(_diff("effective_at_ms", f"[{EFF_MIN_MS}, {EFF_MAX_MS}]", effective))
    if diffs:
        return {
            "ok": False,
            "reason": "conflict",
            "diff": diffs,
            "event_id": HIST_EVENT_ID,
        }
    return {
        "ok": True,
        "reason": "already_recorded",
        "event_id": HIST_EVENT_ID,
        "effective_at_ms": int(effective),
        "golden": _pinned_golden_values(nav_pre),
    }


def _partner_row_counts(conn: sqlite3.Connection, partner_id: str) -> tuple[int, int]:
    ev_n = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM partner_event WHERE partner_id = ?",
            (partner_id,),
        ).fetchone()["n"]
    )
    req_n = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM partner_request WHERE partner_id = ?",
            (partner_id,),
        ).fetchone()["n"]
    )
    return ev_n, req_n


def _split_partners(conn: sqlite3.Connection) -> dict[str, Any]:
    admin = ps.admin_partner(conn)
    toto: sqlite3.Row | None = None
    idle: list[sqlite3.Row] = []
    non_idle: list[sqlite3.Row] = []
    for row in ps.list_partners(conn):
        if int(row["is_admin"]) == 1:
            continue
        if str(row["name"]) == "ToTo":
            toto = row
            continue
        pid = str(row["id"])
        ev_n, req_n = _partner_row_counts(conn, pid)
        if (
            str(row["status"]) == "active"
            and int(row["units_e8"]) == 0
            and int(row["cost_micro"]) == 0
            and ev_n == 0
            and req_n == 0
        ):
            idle.append(row)
        else:
            non_idle.append(row)
    return {"admin": admin, "toto": toto, "idle": idle, "non_idle": non_idle}


def _non_idle_partner_diff(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, object]:
    pid = str(row["id"])
    ev_n, req_n = _partner_row_counts(conn, pid)
    actual = {
        "partner_id": pid,
        "status": str(row["status"]),
        "units_e8": int(row["units_e8"]),
        "cost_micro": int(row["cost_micro"]),
        "event_count": ev_n,
        "request_count": req_n,
    }
    return _diff("non_idle_partner", "idle", actual)


def _pinned_golden_values(nav_pre: Decimal) -> dict[str, Any]:
    total = ps.units_from_e8(TOTAL_UNITS_E8)
    units_after = total - BURN
    nav_post = pl.nav(ACCOUNT_VALUE_AFTER - PAYOUTS_DUE, units_after)
    if isinstance(nav_post, pl.Refusal):
        nav_delta = "nav_not_positive"
    else:
        nav_delta = format(nav_post - nav_pre, "f")
    return {
        "total_units_before_e8": TOTAL_UNITS_E8,
        "nav_pre": format(nav_pre, "f"),
        "nav_pre_q": format(nav_pre.quantize(NAV_Q_STEP), "f"),
        "burn": format(BURN, "f"),
        "units_delta_e8": UNITS_DELTA_E8,
        "admin_units_after": format(ADMIN_UNITS_AFTER, "f"),
        "admin_units_after_e8": ADMIN_UNITS_AFTER_E8,
        "admin_cost_after": format(ADMIN_COST_AFTER, "f"),
        "admin_cost_after_micro": ADMIN_COST_AFTER_MICRO,
        "cash_micro": CASH_MICRO,
        "fee_micro": 0,
        "account_value_micro": AV_MICRO,
        "payouts_due_micro": 0,
        "toto_before": format(TOTO_BEFORE, "f"),
        "toto_after": format(TOTO_AFTER, "f"),
        "nav_delta": nav_delta,
        "tran_id": TRAN_ID,
    }


def pre_state_diffs(conn: sqlite3.Connection, effective_at_ms: int) -> list[dict[str, object]]:
    """Field-by-field diff against the pinned 19 Nov pre-state. Empty means it matches."""
    diffs: list[dict[str, object]] = []
    events = conn.execute(
        "SELECT id, kind, created_at_ms, effective_at_ms FROM partner_event ORDER BY rowid"
    ).fetchall()
    if any(str(row["id"]) == HIST_EVENT_ID for row in events):
        diffs.append(_diff("hist_event", "absent", "present"))
    kinds = [str(row["kind"]) for row in events]
    if len(events) != 2 or any(kind != "deposit" for kind in kinds):
        diffs.append(_diff("events", "2 deposits", kinds))
    split = _split_partners(conn)
    admin = split["admin"]
    if admin is None:
        diffs.append(_diff("admin", "present", None))
    else:
        if str(admin["status"]) != "active":
            diffs.append(_diff("admin.status", "active", str(admin["status"])))
        if int(admin["is_admin"]) != 1:
            diffs.append(_diff("admin.is_admin", 1, int(admin["is_admin"])))
        if int(admin["units_e8"]) != ADMIN_UNITS_E8:
            diffs.append(_diff("admin.units_e8", ADMIN_UNITS_E8, int(admin["units_e8"])))
        if int(admin["cost_micro"]) != ADMIN_COST_MICRO:
            diffs.append(_diff("admin.cost_micro", ADMIN_COST_MICRO, int(admin["cost_micro"])))
    toto = split["toto"]
    if toto is None:
        diffs.append(_diff("toto", "present", None))
    else:
        if str(toto["status"]) != "active":
            diffs.append(_diff("toto.status", "active", str(toto["status"])))
        if int(toto["units_e8"]) != TOTO_UNITS_E8:
            diffs.append(_diff("toto.units_e8", TOTO_UNITS_E8, int(toto["units_e8"])))
        if int(toto["cost_micro"]) != TOTO_COST_MICRO:
            diffs.append(_diff("toto.cost_micro", TOTO_COST_MICRO, int(toto["cost_micro"])))
        if int(toto["fee_rate_bps"]) != TOTO_FEE_BPS:
            diffs.append(_diff("toto.fee_rate_bps", TOTO_FEE_BPS, int(toto["fee_rate_bps"])))
    for row in split["non_idle"]:
        diffs.append(_non_idle_partner_diff(conn, row))
    due = ps.payouts_due_total(conn)
    if due != PAYOUTS_DUE:
        diffs.append(_diff("payouts_due_total", PAYOUTS_DUE, due))
    pending = conn.execute("SELECT COUNT(*) AS n FROM partner_request WHERE status = 'pending'").fetchone()
    if int(pending["n"]) != 0:
        diffs.append(_diff("pending_requests", 0, int(pending["n"])))
    late_ev = conn.execute(
        """
        SELECT COUNT(*) AS n FROM partner_event
        WHERE created_at_ms > ?
           OR COALESCE(effective_at_ms, created_at_ms) > ?
        """,
        (int(effective_at_ms), int(effective_at_ms)),
    ).fetchone()
    if int(late_ev["n"]) != 0:
        diffs.append(_diff("event_after_effective", 0, int(late_ev["n"])))
    late_req = conn.execute(
        """
        SELECT COUNT(*) AS n FROM partner_request
        WHERE created_at_ms > ?
           OR (decided_at_ms IS NOT NULL AND decided_at_ms > ?)
           OR (paid_at_ms IS NOT NULL AND paid_at_ms > ?)
        """,
        (int(effective_at_ms), int(effective_at_ms), int(effective_at_ms)),
    ).fetchone()
    if int(late_req["n"]) != 0:
        diffs.append(_diff("request_after_effective", 0, int(late_req["n"])))
    verified = ps.verify_ledger(conn)
    if not verified["ok"]:
        diffs.append(_diff("verify_ledger", True, verified["mismatches"]))
    return diffs


def compute_golden(conn: sqlite3.Connection) -> dict[str, Any]:
    """Golden numbers from ``nav`` / ``value_of`` on the current rows. NAV stays unrounded."""
    diffs: list[dict[str, object]] = []
    split = _split_partners(conn)
    admin = split["admin"]
    if admin is None:
        return {"ok": False, "diffs": [_diff("admin", "present", None)], "values": {}}
    toto = split["toto"]
    if toto is None:
        diffs.append(_diff("toto", "present", None))
    for row in split["non_idle"]:
        diffs.append(_non_idle_partner_diff(conn, row))
    if diffs:
        return {"ok": False, "diffs": diffs, "values": {}}
    assert toto is not None
    u_before = ps.total_units(conn)
    if ps.units_to_e8(u_before) != TOTAL_UNITS_E8:
        diffs.append(_diff("total_units_before_e8", TOTAL_UNITS_E8, ps.units_to_e8(u_before)))
    nav_pre = pl.nav(ACCOUNT_VALUE_AFTER + AMOUNT - PAYOUTS_DUE, u_before)
    if isinstance(nav_pre, pl.Refusal):
        diffs.append(_diff("nav_pre", "decimal", nav_pre.reason))
        return {"ok": False, "diffs": diffs, "values": {}}
    if nav_pre.quantize(NAV_Q_STEP) != NAV_Q:
        diffs.append(_diff("nav_pre_q", NAV_Q, nav_pre.quantize(NAV_Q_STEP)))
    plan = pl.plan_owner_redeem(
        AMOUNT,
        nav_pre,
        ps.units_from_e8(int(admin["units_e8"])),
        ps.usdt_from_micro(int(admin["cost_micro"])),
    )
    if isinstance(plan, pl.Refusal):
        diffs.append(_diff("plan", "OwnerRedeemPlan", plan.reason))
        return {"ok": False, "diffs": diffs, "values": {}}
    if plan.burned_units != BURN:
        diffs.append(_diff("burn", BURN, plan.burned_units))
    if ps.units_to_e8(-plan.burned_units) != UNITS_DELTA_E8:
        diffs.append(_diff("units_delta_e8", UNITS_DELTA_E8, ps.units_to_e8(-plan.burned_units)))
    if plan.units_after != ADMIN_UNITS_AFTER:
        diffs.append(_diff("admin_units_after", ADMIN_UNITS_AFTER, plan.units_after))
    if ps.units_to_e8(plan.units_after) != ADMIN_UNITS_AFTER_E8:
        diffs.append(_diff("admin_units_after_e8", ADMIN_UNITS_AFTER_E8, ps.units_to_e8(plan.units_after)))
    if plan.cost_after != ADMIN_COST_AFTER:
        diffs.append(_diff("admin_cost_after", ADMIN_COST_AFTER, plan.cost_after))
    if ps.usdt_to_micro(plan.cost_after) != ADMIN_COST_AFTER_MICRO:
        diffs.append(
            _diff("admin_cost_after_micro", ADMIN_COST_AFTER_MICRO, ps.usdt_to_micro(plan.cost_after))
        )
    if ps.usdt_to_micro(plan.cash) != CASH_MICRO:
        diffs.append(_diff("cash_micro", CASH_MICRO, ps.usdt_to_micro(plan.cash)))
    if plan.fee != 0:
        diffs.append(_diff("fee", 0, plan.fee))
    toto_units = ps.units_from_e8(int(toto["units_e8"]))
    toto_cost = ps.usdt_from_micro(int(toto["cost_micro"]))
    fee_rate = ps.fee_rate_from_bps(int(toto["fee_rate_bps"]))
    nav_distorted = pl.nav(ACCOUNT_VALUE_AFTER - PAYOUTS_DUE, u_before)
    if isinstance(nav_distorted, pl.Refusal):
        diffs.append(_diff("nav_distorted", "decimal", nav_distorted.reason))
        return {"ok": False, "diffs": diffs, "values": {}}
    before = pl.value_of(units=toto_units, cost=toto_cost, fee_rate=fee_rate, nav=nav_distorted)
    u_after = u_before - plan.burned_units
    nav_post = pl.nav(ACCOUNT_VALUE_AFTER - PAYOUTS_DUE, u_after)
    if isinstance(nav_post, pl.Refusal):
        diffs.append(_diff("nav_post", "decimal", nav_post.reason))
        return {"ok": False, "diffs": diffs, "values": {}}
    after = pl.value_of(units=toto_units, cost=toto_cost, fee_rate=fee_rate, nav=nav_post)
    if before.net != TOTO_BEFORE:
        diffs.append(_diff("toto_before", TOTO_BEFORE, before.net))
    if after.net != TOTO_AFTER:
        diffs.append(_diff("toto_after", TOTO_AFTER, after.net))
    nav_delta = nav_post - nav_pre
    if abs(nav_delta) >= Decimal("1e-9"):
        diffs.append(_diff("nav_delta", "<1e-9", nav_delta))
    values = {
        "total_units_before_e8": ps.units_to_e8(u_before),
        "nav_pre": format(nav_pre, "f"),
        "nav_pre_q": format(nav_pre.quantize(NAV_Q_STEP), "f"),
        "burn": format(plan.burned_units, "f"),
        "units_delta_e8": ps.units_to_e8(-plan.burned_units),
        "admin_units_after": format(plan.units_after, "f"),
        "admin_units_after_e8": ps.units_to_e8(plan.units_after),
        "admin_cost_after": format(plan.cost_after, "f"),
        "admin_cost_after_micro": ps.usdt_to_micro(plan.cost_after),
        "cash_micro": ps.usdt_to_micro(plan.cash),
        "fee_micro": 0,
        "account_value_micro": ps.usdt_to_micro(ACCOUNT_VALUE_AFTER),
        "payouts_due_micro": ps.usdt_to_micro(PAYOUTS_DUE),
        "toto_before": format(before.net, "f"),
        "toto_after": format(after.net, "f"),
        "nav_delta": format(nav_delta, "f"),
        "tran_id": TRAN_ID,
    }
    return {
        "ok": not diffs,
        "diffs": diffs,
        "values": values,
        "nav_pre": nav_pre,
        "plan": plan,
        "admin_id": str(admin["id"]),
        "toto_id": str(toto["id"]),
        "admin_cost": ps.usdt_from_micro(int(admin["cost_micro"])),
    }


def _verify_pub(conn: sqlite3.Connection) -> dict[str, Any]:
    verified = ps.verify_ledger(conn)
    return {"ok": bool(verified["ok"]), "mismatches": _pub(verified["mismatches"])}


def build_preview(conn: sqlite3.Connection, effective_at_ms: int) -> dict[str, Any]:
    """Read-only preview. Caller must already have checked the effective-time window."""
    hist = _fetch_hist(conn)
    if hist is not None:
        payload = _idempotent(conn, hist)
        payload["mode"] = "dry-run"
        payload["verify"] = _verify_pub(conn)
        return _pub(payload)
    diffs = pre_state_diffs(conn, effective_at_ms)
    if diffs:
        return {
            "ok": False,
            "reason": "state_mismatch",
            "mode": "dry-run",
            "diff": diffs,
            "verify": _verify_pub(conn),
        }
    golden = compute_golden(conn)
    if not golden["ok"]:
        return {
            "ok": False,
            "reason": "golden_mismatch",
            "mode": "dry-run",
            "diff": golden["diffs"],
            "golden": golden["values"],
            "verify": _verify_pub(conn),
        }
    idle_count = len(_split_partners(conn)["idle"])
    return {
        "ok": True,
        "reason": "ready",
        "mode": "dry-run",
        "golden": golden["values"],
        "pre": {
            "admin_units_e8": ADMIN_UNITS_E8,
            "admin_cost_micro": ADMIN_COST_MICRO,
            "toto_units_e8": TOTO_UNITS_E8,
            "toto_net": format(TOTO_BEFORE, "f"),
            "event_count": 2,
            "idle_partner_count": idle_count,
        },
        "post": {
            "admin_units_e8": ADMIN_UNITS_AFTER_E8,
            "admin_cost_micro": ADMIN_COST_AFTER_MICRO,
            "toto_net": format(TOTO_AFTER, "f"),
        },
        "verify": _verify_pub(conn),
        "effective_at_ms": int(effective_at_ms),
    }


def preview_historical(conn: sqlite3.Connection, effective_at_ms: int) -> dict[str, Any]:
    if not effective_in_window(effective_at_ms):
        return {
            "ok": False,
            "reason": "effective_at_ms_out_of_window",
            "mode": "dry-run",
            "effective_at_ms": int(effective_at_ms),
        }
    return build_preview(conn, effective_at_ms)


def apply_historical(
    conn: sqlite3.Connection,
    effective_at_ms: int,
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Insert the historical row or refuse. Raises ``HistoricalRecordError`` on refusal."""
    if not effective_in_window(effective_at_ms):
        raise HistoricalRecordError(
            {
                "ok": False,
                "reason": "effective_at_ms_out_of_window",
                "mode": "apply",
                "effective_at_ms": int(effective_at_ms),
            }
        )
    stamp = int(time.time() * 1000) if now_ms is None else int(now_ms)
    with transaction(conn):
        hist = _fetch_hist(conn)
        if hist is not None:
            payload = _idempotent(conn, hist)
            payload["mode"] = "apply"
            payload["verify"] = _verify_pub(conn)
            if not payload["ok"]:
                raise HistoricalRecordError(_pub(payload))
            return _pub(payload)
        diffs = pre_state_diffs(conn, effective_at_ms)
        if diffs:
            raise HistoricalRecordError(
                {
                    "ok": False,
                    "reason": "state_mismatch",
                    "mode": "apply",
                    "diff": diffs,
                    "verify": _verify_pub(conn),
                }
            )
        golden = compute_golden(conn)
        if not golden["ok"]:
            raise HistoricalRecordError(
                {
                    "ok": False,
                    "reason": "golden_mismatch",
                    "mode": "apply",
                    "diff": golden["diffs"],
                    "golden": golden["values"],
                    "verify": _verify_pub(conn),
                }
            )
        plan = golden["plan"]
        assert isinstance(plan, pl.OwnerRedeemPlan)
        ps._update_partner(
            conn,
            str(golden["admin_id"]),
            units=plan.units_after,
            cost=plan.cost_after,
            now_ms=stamp,
        )
        ps._insert_event(
            conn,
            event_id=HIST_EVENT_ID,
            partner_id=str(golden["admin_id"]),
            kind="owner_redeem",
            account_value=ACCOUNT_VALUE_AFTER,
            total_units_before=ps.units_from_e8(TOTAL_UNITS_E8),
            nav_value=golden["nav_pre"],
            units_delta=-plan.burned_units,
            cost_delta=plan.cost_after - golden["admin_cost"],
            cash=plan.cash,
            fee=Decimal("0"),
            units_after=plan.units_after,
            cost_after=plan.cost_after,
            now_ms=stamp,
            payouts_due=PAYOUTS_DUE,
            effective_at_ms=int(effective_at_ms),
            origin="manual",
            note=NOTE,
            actor="admin",
        )
        verified = ps.verify_ledger(conn)
        post_diffs: list[dict[str, object]] = []
        if not verified["ok"]:
            post_diffs.append(_diff("verify_ledger", True, verified["mismatches"]))
        admin = ps.admin_partner(conn)
        if admin is None or int(admin["units_e8"]) != ADMIN_UNITS_AFTER_E8:
            actual_units = None if admin is None else int(admin["units_e8"])
            post_diffs.append(_diff("admin.units_e8", ADMIN_UNITS_AFTER_E8, actual_units))
        if admin is None or int(admin["cost_micro"]) != ADMIN_COST_AFTER_MICRO:
            actual_cost = None if admin is None else int(admin["cost_micro"])
            post_diffs.append(_diff("admin.cost_micro", ADMIN_COST_AFTER_MICRO, actual_cost))
        view = ps.view_partner(conn, str(golden["toto_id"]), ACCOUNT_VALUE_AFTER)
        if not view.get("ok") or view.get("net") != TOTO_AFTER:
            post_diffs.append(_diff("toto_net", TOTO_AFTER, view.get("net")))
        written = _fetch_hist(conn)
        if written is None:
            post_diffs.append(_diff("hist_event", "present", "absent"))
        else:
            written_check = _idempotent(conn, written)
            if not written_check["ok"]:
                post_diffs.extend(written_check["diff"])
        if post_diffs:
            raise HistoricalRecordError(
                {
                    "ok": False,
                    "reason": "rolled_back",
                    "mode": "apply",
                    "diff": post_diffs,
                    "verify": {"ok": bool(verified["ok"]), "mismatches": _pub(verified["mismatches"])},
                }
            )
        idle_count = len(_split_partners(conn)["idle"])
        return {
            "ok": True,
            "reason": "recorded",
            "mode": "apply",
            "event_id": HIST_EVENT_ID,
            "effective_at_ms": int(effective_at_ms),
            "created_at_ms": stamp,
            "golden": golden["values"],
            "pre": {
                "admin_units_e8": ADMIN_UNITS_E8,
                "admin_cost_micro": ADMIN_COST_MICRO,
                "toto_net": format(TOTO_BEFORE, "f"),
                "event_count": 2,
                "idle_partner_count": idle_count,
            },
            "post": {
                "admin_units_e8": int(admin["units_e8"]) if admin is not None else None,
                "admin_cost_micro": int(admin["cost_micro"]) if admin is not None else None,
                "toto_net": format(TOTO_AFTER, "f"),
                "event_count": 3,
            },
            "verify": {"ok": True, "mismatches": []},
        }
