"""Tighten an open stop after price has moved one initial-risk unit in favor.

The entry stop stays 1.5× ATR. This only moves that stop tighter, by one R
from the best price since entry. It never loosens and never adds a take-profit.
"""

from __future__ import annotations

from typing import Any

from jarvise_ingest.db import list_paper_positions, range_since_entry, set_position_stop


def trail_stop(
    position: dict[str, Any],
    *,
    best_low: float | None,
    best_high: float | None,
) -> float | None:
    """Return a tighter stop once favorable travel reaches 1R, else None.

    R is the distance from entry to the initial stop. A short trails to
    ``best_low + R`` (lower is tighter). A long trails to ``best_high - R``.
    """
    side = str(position.get("side") or "").lower()
    entry = _num(position.get("entry_price"))
    initial = _num(position.get("initial_stop_price"))
    if initial is None:
        initial = _num(position.get("stop_price"))
    current = _num(position.get("stop_price"))
    if entry is None or initial is None or current is None:
        return None
    risk = abs(entry - initial)
    if risk <= 0:
        return None
    if side == "short":
        if best_low is None or entry - float(best_low) < risk:
            return None
        candidate = float(best_low) + risk
        if candidate < current:
            return candidate
        return None
    if side == "long":
        if best_high is None or float(best_high) - entry < risk:
            return None
        candidate = float(best_high) - risk
        if candidate > current:
            return candidate
        return None
    return None


def run_profit_lock(conn: Any, *, now_ms: int) -> dict[str, Any]:
    """Tighten stops that have reached 1R. Does not close positions."""
    moved: list[dict[str, Any]] = []
    for pos in list_paper_positions(conn):
        if pos.get("stop_price") is None or pos.get("entry_ts") is None:
            continue
        symbol = str(pos["symbol"])
        low, high = range_since_entry(conn, symbol, entry_ts=int(pos["entry_ts"]))
        new_stop = trail_stop(pos, best_low=low, best_high=high)
        if new_stop is None:
            continue
        set_position_stop(
            conn,
            symbol,
            stop_price=new_stop,
            stop_source="trail",
            moved_ts=int(now_ms),
        )
        moved.append({"symbol": symbol.upper(), "stop_price": new_stop})
    return {"ok": True, "paper_only": True, "moved": moved}


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number
