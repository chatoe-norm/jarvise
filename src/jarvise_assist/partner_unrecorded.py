"""Read-only helpers for unrecorded Binance transfer state.

Portal and overview may only see a boolean / tri-state here — never guard
text or amounts. ``partner_audit`` is lazy-imported inside functions so that
importing ``partner_portal`` does not load ``partner_audit`` into sys.modules.

The display cache lives here (not in ``partner_portal``) so any fresh guard
evidence — portal, admin overview, or admin confirm — can invalidate it.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

UnrecordedStatus = Literal["clear", "unrecorded", "unknown"]

_UNRECORDED_CODES = frozenset({"transfer_in_unrecorded", "transfer_out_unrecorded"})
DISPLAY_CACHE_TTL_MS = 10 * 60 * 1000

# (ledger_key, status, checked_at_ms). Never caches "unknown".
_display_cache: tuple[tuple[str, int, int, int], str, int] | None = None


def reset_display_cache() -> None:
    """Test hook: clear the shared unrecorded display cache."""
    global _display_cache
    _display_cache = None


def invalidate_display_cache() -> None:
    """Drop any cached clear/unrecorded so the next display re-queries."""
    global _display_cache
    _display_cache = None


def store_display_status(
    key: tuple[str, int, int, int],
    status: str,
    now_ms: int,
) -> None:
    """Store only clear/unrecorded. Unknown (or anything else) clears the cache."""
    global _display_cache
    if status == "clear" or status == "unrecorded":
        _display_cache = (key, status, int(now_ms))
    else:
        _display_cache = None


def cached_display_status(conn: sqlite3.Connection, now_ms: int) -> UnrecordedStatus | None:
    """Return a TTL+ledger-keyed clear/unrecorded hit, else None.

    Raises if ``ledger_cache_key`` fails (caller decides whether to invalidate).
    Unknown is never returned from cache even if a poisoned entry exists.
    """
    if _display_cache is None:
        return None
    key = ledger_cache_key(conn)
    cached_key, status, at = _display_cache
    if cached_key == key and int(now_ms) - at < DISPLAY_CACHE_TTL_MS and status in ("clear", "unrecorded"):
        return status  # type: ignore[return-value]
    return None


def _coerce_fresh_status(result_or_status: Any) -> UnrecordedStatus:
    """Map a tri-state string or GuardResult-like object to UnrecordedStatus."""
    if isinstance(result_or_status, str):
        if result_or_status in ("clear", "unrecorded", "unknown"):
            return result_or_status  # type: ignore[return-value]
        return "unknown"
    status = str(getattr(result_or_status, "status", "") or "")
    if status != "ok":
        return "unknown"
    findings = getattr(result_or_status, "findings", None) or []
    if any(str(getattr(f, "code", "") or "") in _UNRECORDED_CODES for f in findings):
        return "unrecorded"
    return "clear"


def note_fresh_guard(
    result_or_status: Any,
    *,
    conn: sqlite3.Connection | None = None,
    now_ms: int | None = None,
) -> None:
    """Apply fresh guard evidence to the shared display cache.

    Non-ok / unrecorded / unknown → invalidate (unrecorded may store a lock when
    ``conn``+``now_ms`` are provided). Ok with no unrecorded findings → may
    refresh a clear entry. Never stores unknown.
    """
    status = _coerce_fresh_status(result_or_status)
    if status == "clear":
        if conn is not None and now_ms is not None:
            try:
                store_display_status(ledger_cache_key(conn), "clear", int(now_ms))
            except Exception:  # noqa: BLE001
                invalidate_display_cache()
        return
    if status == "unrecorded":
        if conn is not None and now_ms is not None:
            try:
                store_display_status(ledger_cache_key(conn), "unrecorded", int(now_ms))
                return
            except Exception:  # noqa: BLE001
                pass
        invalidate_display_cache()
        return
    invalidate_display_cache()


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
    """Live tri-state from ``guard_check``. Fail closed on any fetch problem.

    Every branch notes fresh evidence so a portal cached clear cannot survive
    admin-side or portal-side discovery of unrecorded/unknown/errors.
    """
    if client is None:
        note_fresh_guard("unknown")
        return "unknown"
    from jarvise_assist.partner_audit import guard_check

    try:
        result = guard_check(conn, client, now_ms=int(now_ms))
    except Exception as exc:  # noqa: BLE001
        logger.warning("unrecorded guard_check failed type=%s", type(exc).__name__)
        note_fresh_guard("unknown")
        return "unknown"
    if str(result.status) != "ok":
        note_fresh_guard(result, conn=conn, now_ms=int(now_ms))
        return "unknown"
    if any(str(f.code) in _UNRECORDED_CODES for f in result.findings):
        note_fresh_guard(result, conn=conn, now_ms=int(now_ms))
        return "unrecorded"
    note_fresh_guard(result, conn=conn, now_ms=int(now_ms))
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
        # Hint raises concern after a live unknown — store the lock for display.
        note_fresh_guard("unrecorded", conn=conn, now_ms=int(now_ms))
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
