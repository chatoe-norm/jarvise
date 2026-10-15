"""Two-stage profit protection: Breakeven (+20 bps fee buffer) at 0.5R, then 1R trail.

The entry stop stays 1.5× ATR. Once price moves 0.5R in favor, the stop tightens
to breakeven plus a fee buffer (20 bps default) to guarantee capital preservation.
Once price moves 1.0R or more in favor, it continues trailing by 1R from the
best price since entry. It never loosens and never adds a take-profit.
"""

from __future__ import annotations

from typing import Any

from jarvise_ingest.db import list_paper_positions, range_since_entry, set_position_stop

DEFAULT_BREAKEVEN_R = 0.5
DEFAULT_FEE_BPS = 20.0


def trail_stop_with_source(
    position: dict[str, Any],
    *,
    best_low: float | None,
    best_high: float | None,
    breakeven_r: float | None = DEFAULT_BREAKEVEN_R,
    fee_bps: float = DEFAULT_FEE_BPS,
) -> tuple[float, str] | None:
    """Return (new_stop, stop_source) once travel reaches breakeven or 1R, else None.

    Milestone 1 (favorable travel >= breakeven_r * R):
      Stop moves to entry +- fee_bps (breakeven).
    Milestone 2 (favorable travel >= 1.0 * R):
      Stop trails to best_low + R (short) or best_high - R (long).
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

    fee_mult = fee_bps / 10_000.0
    candidate: float | None = None
    source: str = "trail"

    if side == "short":
        if best_low is None:
            return None
        favorable = entry - float(best_low)

        # Check Stage 2: 1R trail
        if favorable >= risk:
            candidate = float(best_low) + risk
            source = "trail"
        # Check Stage 1: Breakeven (entry minus fee buffer for short)
        elif breakeven_r is not None and favorable >= breakeven_r * risk:
            candidate = entry * (1.0 - fee_mult)
            source = "breakeven"

        if candidate is not None and candidate < current:
            return round(candidate, 8), source
        return None

    if side == "long":
        if best_high is None:
            return None
        favorable = float(best_high) - entry

        # Check Stage 2: 1R trail
        if favorable >= risk:
            candidate = float(best_high) - risk
            source = "trail"
        # Check Stage 1: Breakeven (entry plus fee buffer for long)
        elif breakeven_r is not None and favorable >= breakeven_r * risk:
            candidate = entry * (1.0 + fee_mult)
            source = "breakeven"

        if candidate is not None and candidate > current:
            return round(candidate, 8), source
        return None

    return None


def trail_stop(
    position: dict[str, Any],
    *,
    best_low: float | None,
    best_high: float | None,
    breakeven_r: float | None = DEFAULT_BREAKEVEN_R,
    fee_bps: float = DEFAULT_FEE_BPS,
) -> float | None:
    """Return a tighter stop once travel reaches breakeven or 1R, else None."""
    res = trail_stop_with_source(
        position,
        best_low=best_low,
        best_high=best_high,
        breakeven_r=breakeven_r,
        fee_bps=fee_bps,
    )
    return res[0] if res is not None else None


def run_profit_lock(conn: Any, *, now_ms: int) -> dict[str, Any]:
    """Tighten stops that have reached 0.5R or 1R. Does not close positions."""
    moved: list[dict[str, Any]] = []
    for pos in list_paper_positions(conn):
        if pos.get("stop_price") is None or pos.get("entry_ts") is None:
            continue
        symbol = str(pos["symbol"])
        low, high = range_since_entry(conn, symbol, entry_ts=int(pos["entry_ts"]))
        res = trail_stop_with_source(pos, best_low=low, best_high=high)
        if res is None:
            continue
        new_stop, source = res
        set_position_stop(
            conn,
            symbol,
            stop_price=new_stop,
            stop_source=source,
            moved_ts=int(now_ms),
        )
        moved.append({"symbol": symbol.upper(), "stop_price": new_stop, "stop_source": source})
    return {"ok": True, "paper_only": True, "moved": moved}


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number
