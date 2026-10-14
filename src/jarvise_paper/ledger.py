"""Paper ledger loaders used by the control API."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jarvise_ingest.db import (
    ensure_paper_account,
    get_paper_account,
    list_paper_orders,
    list_paper_positions,
    open_db,
)


def load_paper_ledger(path: Path) -> tuple[dict[str, Any], str | None]:
    if not path.exists():
        return {"account": None, "positions": [], "orders": []}, f"Database not found: {path}"
    try:
        conn = open_db(path)
        try:
            ensure_paper_account(conn)
            payload = {
                "account": get_paper_account(conn),
                "positions": list_paper_positions(conn),
                "orders": list_paper_orders(conn, limit=20),
            }
        finally:
            conn.close()
        return payload, None
    except Exception as exc:  # noqa: BLE001
        return {"account": None, "positions": [], "orders": []}, str(exc)
