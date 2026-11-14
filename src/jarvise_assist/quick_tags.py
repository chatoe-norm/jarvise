"""Read-only quick-trade helpers safe for main modules to import.

Must not import pause, weekly, real_trades, watch, or menu (avoids cycles).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any


def _id_set(raw: Any) -> set[int]:
    if not raw:
        return set()
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError, json.JSONDecodeError):
        return set()
    if not isinstance(data, list):
        return set()
    out: set[int] = set()
    for item in data:
        try:
            tid = int(item)
        except (TypeError, ValueError):
            continue
        if tid > 0:
            out.add(tid)
    return out


def quick_income_predicate(conn: Any, *, now_ms: int) -> Callable[[dict[str, Any]], bool]:
    """Return True when a Binance income row belongs to a quick trade.

    Loads quick trades once. With none, returns a predicate that is always False.
    """
    rows = conn.execute(
        """
        SELECT symbol, side, status, entry_time_ms, closed_at_ms,
               entry_trade_ids, exit_trade_ids
        FROM assist_quick_trade
        """
    ).fetchall()
    if not rows:
        return lambda _row: False

    realized: set[tuple[str, int]] = set()
    commission: set[tuple[str, int]] = set()
    funding_spans: list[tuple[str, int, int]] = []
    open_syms: list[tuple[str, int]] = []

    for row in rows:
        sym = str(row["symbol"] or "").upper()
        entry_ids = _id_set(row["entry_trade_ids"])
        exit_ids = _id_set(row["exit_trade_ids"])
        for tid in exit_ids:
            realized.add((sym, tid))
            commission.add((sym, tid))
        for tid in entry_ids:
            commission.add((sym, tid))
        entry_ms = row["entry_time_ms"]
        if entry_ms is None:
            continue
        status = str(row["status"] or "")
        if status == "closed" and row["closed_at_ms"] is not None:
            funding_spans.append((sym, int(entry_ms), int(row["closed_at_ms"])))
        elif status == "open":
            funding_spans.append((sym, int(entry_ms), int(now_ms)))
            open_syms.append((sym, int(entry_ms)))

    def is_quick(row: dict[str, Any]) -> bool:
        sym = str(row.get("symbol") or "").upper()
        itype = str(row.get("incomeType") or "")
        tms = int(row.get("time") or 0)
        if itype == "REALIZED_PNL":
            try:
                tid = int(row.get("tradeId") or 0)
            except (TypeError, ValueError):
                tid = 0
            if tid > 0 and (sym, tid) in realized:
                return True
        elif itype == "COMMISSION":
            try:
                tid = int(row.get("tradeId") or 0)
            except (TypeError, ValueError):
                tid = 0
            if tid > 0 and (sym, tid) in commission:
                return True
        elif itype == "FUNDING_FEE":
            if any(s == sym and opened < tms <= closed for s, opened, closed in funding_spans):
                return True
        # Any type on an open quick symbol after entry (mutual exclusion).
        if any(s == sym and tms >= opened for s, opened in open_syms):
            return True
        return False

    return is_quick
