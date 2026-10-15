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
from jarvise_paper.engine import mark_equity
from jarvise_paper.marking import quote_positions


def load_paper_ledger(path: Path) -> tuple[dict[str, Any], str | None]:
    if not path.exists():
        return {"account": None, "positions": [], "orders": []}, f"Database not found: {path}"
    try:
        conn = open_db(path)
        try:
            ensure_paper_account(conn)
            positions = quote_positions(conn, list_paper_positions(conn))
            account = get_paper_account(conn)
            if positions:
                marks = {str(pos["symbol"]).upper(): float(pos["mark_price"]) for pos in positions}
                account["equity"] = round(
                    mark_equity(float(account["cash"]), positions, marks),
                    8,
                )
            payload = {
                "account": account,
                "positions": positions,
                "orders": list_paper_orders(conn, limit=20),
            }
        finally:
            conn.close()
        return payload, None
    except Exception as exc:  # noqa: BLE001
        return {"account": None, "positions": [], "orders": []}, str(exc)
