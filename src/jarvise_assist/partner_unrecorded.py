"""Read-only helpers for unrecorded Binance transfer state.

Portal and overview may only see a boolean / tri-state here — never guard
text or amounts. ``partner_audit`` is lazy-imported inside functions so that
importing ``partner_portal`` does not load ``partner_audit`` into sys.modules.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

UnrecordedStatus = Literal["clear", "unrecorded", "unknown"]

_UNRECORDED_CODES = frozenset({"transfer_in_unrecorded", "transfer_out_unrecorded"})


def _db_file(conn: sqlite3.Connection) -> str:
    row = conn.execute("PRAGMA database_list").fetchone()
    if row is None:
        return ""
    return str(row["file"] if hasattr(row, "keys") else row[2] or "")


def state_path_for(conn: sqlite3.Connection) -> Path | None:
    db_file = _db_file(conn)
    if not db_file:
        return None
    return Path(db_file).with_name("partner_audit_state.json")


def open_unrecorded_hint(conn: sqlite3.Connection) -> list[str]:
    """Fingerprints from the daily audit state file (hint only; may be stale)."""
    from jarvise_assist.partner_audit import load_state

    path = state_path_for(conn)
    if path is None:
        return []
    state = load_state(path)
    open_list = state.get("open_unrecorded") or []
    if not isinstance(open_list, list):
        return []
    return [str(x) for x in open_list if str(x)]


def guard_unrecorded_status(
    conn: sqlite3.Connection,
    client: Any,
    *,
    now_ms: int,
) -> UnrecordedStatus:
    """Live tri-state from ``guard_check``. Fail closed on any fetch problem."""
    if client is None:
        return "unknown"
    from jarvise_assist.partner_audit import guard_check

    try:
        result = guard_check(conn, client, now_ms=int(now_ms))
    except Exception as exc:  # noqa: BLE001
        logger.warning("unrecorded guard_check failed type=%s", type(exc).__name__)
        return "unknown"
    if str(result.status) != "ok":
        return "unknown"
    if any(str(f.code) in _UNRECORDED_CODES for f in result.findings):
        return "unrecorded"
    return "clear"


def resolve_unrecorded_status(
    conn: sqlite3.Connection,
    client: Any,
    *,
    now_ms: int,
) -> UnrecordedStatus:
    """Authoritative status for portal/overview.

    A fresh successful guard with no unrecorded findings overrides a stale
    state-file list. Unknown and unrecorded both lock money actions.
    """
    live = guard_unrecorded_status(conn, client, now_ms=now_ms)
    if live == "clear":
        return "clear"
    if live == "unrecorded":
        return "unrecorded"
    # unknown: state hint can only raise concern, never clear
    if open_unrecorded_hint(conn):
        return "unrecorded"
    return "unknown"


def ledger_cache_key(conn: sqlite3.Connection) -> tuple[str, int, int, int]:
    """Display-cache key: db file, total units e8, payouts due micro, latest event rowid."""
    from jarvise_assist import partner_store as ps

    db_file = _db_file(conn)
    units = ps.units_to_e8(ps.total_units(conn))
    due = ps.usdt_to_micro(ps.payouts_due_total(conn))
    row = conn.execute("SELECT MAX(rowid) AS r FROM partner_event").fetchone()
    latest = int(row["r"] or 0) if row is not None else 0
    return (db_file, int(units), int(due), latest)


def remove_booked_tran(conn: sqlite3.Connection, tran_id: str) -> None:
    """Best-effort drop ``bx:*:{tranId}`` from the state file after A2/B1 booking."""
    from jarvise_assist.partner_audit import remove_open_unrecorded_for_tran

    path = state_path_for(conn)
    if path is None:
        return
    remove_open_unrecorded_for_tran(path, tran_id)
