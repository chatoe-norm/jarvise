"""Realized PnL from live fills vs average entry."""

from __future__ import annotations

from typing import Any

from jarvise_ingest.db import live_spot_inventory


def realized_pnl_usd(
    conn: Any,
    *,
    side: str,
    symbol: str,
    executed_qty: float | None,
    quote_qty: float | None,
    exclude_id: str | None = None,
) -> float:
    if str(side).upper() != "SELL":
        return 0.0
    qty = float(executed_qty or 0.0)
    quote = float(quote_qty or 0.0)
    if qty <= 0:
        return 0.0
    _inv, avg = live_spot_inventory(conn, symbol, exclude_id=exclude_id)
    if avg <= 0:
        return 0.0
    return round(quote - avg * qty, 8)
