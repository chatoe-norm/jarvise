"""Mark paper positions to latest stored closes and persist equity snapshots."""

from __future__ import annotations

from typing import Any

from jarvise_ingest.db import (
    get_paper_account,
    insert_equity_snapshot,
    list_paper_positions,
    load_newest_close,
    set_paper_account_value,
    upsert_paper_position,
)
from jarvise_paper.engine import mark_equity


def quote_positions(conn: Any, positions: list[dict]) -> list[dict]:
    """Copy positions with unrealized PnL from the newest stored close.

    Does not write the ledger. The stored ``unrealized_pnl`` column stays until
    ``mark_to_market`` runs.
    """
    quoted = [dict(pos) for pos in positions]
    marks = mark_prices(conn, [str(pos["symbol"]) for pos in quoted])
    for pos in quoted:
        symbol = str(pos["symbol"]).upper()
        stored = marks.get(symbol)
        mark = float(pos["entry_price"]) if stored is None else stored
        marks[symbol] = mark
        entry = float(pos["entry_price"])
        qty = float(pos["qty"])
        if str(pos["side"]) == "long":
            unreal = (mark - entry) * qty
        else:
            unreal = (entry - mark) * qty
        pos["unrealized_pnl"] = round(unreal, 8)
        pos["mark_price"] = mark
    return quoted


def mark_prices(conn: Any, symbols: list[str]) -> dict[str, float]:
    marks: dict[str, float] = {}
    for raw in symbols:
        symbol = str(raw).upper()
        close = load_newest_close(conn, symbol)
        if close is not None:
            marks[symbol] = close
    return marks


def mark_to_market(conn: Any, *, now_ms: int, persist: bool = True) -> dict[str, Any]:
    positions = list_paper_positions(conn)
    acct = get_paper_account(conn)
    cash = float(acct["cash"])
    marks = mark_prices(conn, [str(p["symbol"]) for p in positions])
    for pos in positions:
        symbol = str(pos["symbol"]).upper()
        mark = float(marks.get(symbol, pos["entry_price"]))
        marks[symbol] = mark
        entry = float(pos["entry_price"])
        qty = float(pos["qty"])
        if str(pos["side"]) == "long":
            unreal = (mark - entry) * qty
        else:
            unreal = (entry - mark) * qty
        pos["unrealized_pnl"] = round(unreal, 8)
        upsert_paper_position(conn, pos)
    equity = mark_equity(cash, positions, marks)
    set_paper_account_value(conn, "equity", round(equity, 8))
    if persist:
        insert_equity_snapshot(
            conn,
            ts=int(now_ms),
            equity=round(equity, 8),
            cash=round(cash, 8),
            source="mtm",
        )
        conn.commit()
    return {"equity": round(equity, 8), "cash": round(cash, 8), "marks": marks}
