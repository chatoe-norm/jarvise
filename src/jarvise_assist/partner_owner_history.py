"""Generic historical owner deposit/withdraw record (``assist partner-record-owner-tx``).

For an owner transfer the live ``p owner dep|wd`` path can no longer book (older than
the live max age, another transfer pending, ...). No Binance, no Telegram, no HTTP:
the owner supplies the pool value right after the transfer.

Books through the same store functions, event id and amount rule as the live
confirm (``ps.apply_owner_deposit`` / ``ps.apply_owner_redeem``), so ledger
invariants (verify before/after, pending-deposit block, idempotency) still hold.
Dry-run (``preview_owner_tx``) never writes.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from jarvise_assist import partner_store as ps
from jarvise_assist.partner_owner_settings import owner_hist_max_lookback_days, owner_hist_max_lookback_ms

KIND_BY_SIDE: Final = {"dep": "owner_deposit", "wd": "owner_redeem"}
CONFLICT: Final = "conflict"


def owner_hist_note(tran_id: str) -> str:
    return f"owner tx via assist partner-record-owner-tx (historical); Binance TRANSFER tranId {tran_id}"


def _pub(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {str(k): _pub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_pub(v) for v in value]
    return value


def _parse_decimal(raw: str | Decimal) -> Decimal | None:
    try:
        dec = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        return None
    return dec if dec.is_finite() else None


def validate_args(
    *,
    side: str,
    tran_id: str,
    amount: str | Decimal,
    account_value: str | Decimal,
    effective_at_ms: int,
    now_ms: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return (params, None) or (None, refusal). Pure; no DB."""
    from jarvise_assist import partner_admin as pa

    if side not in KIND_BY_SIDE:
        return None, {"ok": False, "reason": "bad_kind"}
    if not pa._TRAN_ID_RE.match(str(tran_id)):
        return None, {"ok": False, "reason": "bad_tran_id"}
    dec = _parse_decimal(amount)
    if dec is None:
        return None, {"ok": False, "reason": "bad_amount"}
    amount_n = ps.normalize_owner_amount(dec)
    if ps.owner_amount_refusal(amount_n) is not None:
        return None, {"ok": False, "reason": "bad_amount"}
    if ps.usdt_to_micro(amount_n) > pa._OWNER_MAX_MICRO:
        return None, {"ok": False, "reason": "amount_too_large"}
    av = _parse_decimal(account_value)
    if av is None or av <= 0:
        return None, {"ok": False, "reason": "bad_account_value"}
    eff = int(effective_at_ms)
    if eff > int(now_ms):
        return None, {"ok": False, "reason": "effective_in_future"}
    if int(now_ms) - eff > owner_hist_max_lookback_ms():
        return None, {
            "ok": False,
            "reason": "effective_too_old",
            "max_lookback_days": owner_hist_max_lookback_days(),
        }
    return {
        "side": side,
        "kind": KIND_BY_SIDE[side],
        "tran_id": str(tran_id),
        "amount": amount_n,
        "account_value": av,
        "effective_at_ms": eff,
        "event_id": pa._owner_event_id(str(tran_id)),
        "note": owner_hist_note(str(tran_id)),
    }, None


def _already_booked(conn: sqlite3.Connection, tran_id: str) -> bool:
    """Same tranId booked by any owner path (live confirm, one-off, this CLI), either side."""
    from jarvise_assist import partner_admin as pa

    return any(pa._owner_already_recorded(conn, side=side, tran_id=tran_id) for side in KIND_BY_SIDE)


def _base(params: dict[str, Any], mode: str) -> dict[str, Any]:
    return {
        "mode": mode,
        "kind": params["kind"],
        "tran_id": params["tran_id"],
        "event_id": params["event_id"],
        "amount": params["amount"],
        "account_value": params["account_value"],
        "effective_at_ms": params["effective_at_ms"],
    }


def _numbers(plan: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "nav_pre",
        "total_units_before",
        "admin_units_before",
        "admin_cost_before",
        "units_delta",
        "units_after",
        "cost_after",
        "cash",
        "payouts_due",
    )
    return {k: plan[k] for k in keys if k in plan}


def preview_owner_tx(conn: sqlite3.Connection, params: dict[str, Any], *, now_ms: int) -> dict[str, Any]:
    """Read-only dry-run. Works on a read-only connection."""
    out = _base(params, "dry-run")
    if _already_booked(conn, params["tran_id"]):
        return _pub({**out, "ok": False, "reason": CONFLICT})
    late = ps.owner_events_after(conn, params["effective_at_ms"])
    if late is not None:
        return _pub({**out, "ok": False, "reason": late})
    plan = ps.plan_owner_tx(
        conn,
        kind=params["kind"],
        amount=params["amount"],
        account_value=params["account_value"],
        now_ms=int(now_ms),
    )
    if not plan.get("ok"):
        return _pub({**out, "ok": False, "reason": str(plan.get("reason") or "refused")})
    return _pub({**out, "ok": True, "reason": "would_record", "plan": _numbers(plan)})


def apply_owner_tx(conn: sqlite3.Connection, params: dict[str, Any], *, now_ms: int) -> dict[str, Any]:
    """Book through ``ps.apply_owner_*`` (own transaction, verify before/after)."""
    out = _base(params, "apply")
    if _already_booked(conn, params["tran_id"]):
        return _pub({**out, "ok": False, "reason": CONFLICT})
    fn = ps.apply_owner_deposit if params["side"] == "dep" else ps.apply_owner_redeem
    result = fn(
        conn,
        amount=params["amount"],
        account_value=params["account_value"],
        now_ms=int(now_ms),
        effective_at_ms=params["effective_at_ms"],
        note=params["note"],
        event_id=params["event_id"],
        refuse_events_after_ms=params["effective_at_ms"],
    )
    if result.get("reason") == "already_recorded":
        return _pub({**out, "ok": False, "reason": CONFLICT})
    if not result.get("ok"):
        return _pub({**out, "ok": False, "reason": str(result.get("reason") or "refused")})
    from jarvise_assist import partner_unrecorded as pu

    pu.remove_booked_tran(conn, params["tran_id"])
    keep = ("nav_pre", "burned_units", "units_issued", "units_after", "cost_after", "cash", "payouts_due")
    return _pub(
        {
            **out,
            "ok": True,
            "reason": "recorded",
            "result": {k: result[k] for k in keep if k in result},
            "verify": {"ok": bool(ps.verify_ledger(conn)["ok"])},
        }
    )
