"""4h trend + counter-trend risk for Quick coin picker (A0b) and A1.

Pure helpers. No Telegram, no orders. Callers pass live ``effective_quick_values``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from jarvise_assist import ASSIST_SYMBOLS
from jarvise_assist.explore import connection_path, explore_db_path, latest_explore_analysis
from jarvise_assist.sizing import analysis_is_fresh
from jarvise_assist.store import list_explore_symbols
from jarvise_ingest.db import load_latest_analysis, open_db

Trend = Literal["up", "down", "unknown"]


def quick_trend_4h(
    conn: Any,
    symbol: str,
    *,
    now_ms: int,
    explore_conn: Any | None = None,
) -> Trend:
    """Fresh 4h long→up, fresh short→down; else unknown (stale/flat/missing/errors)."""
    sym = str(symbol).upper()
    analysis: dict[str, Any] | None = None
    try:
        if sym in ASSIST_SYMBOLS:
            analysis = load_latest_analysis(conn, sym, "4h")
        elif sym in list_explore_symbols(conn):
            analysis = _explore_analysis(conn, sym, explore_conn=explore_conn)
        else:
            return "unknown"
    except Exception:  # noqa: BLE001 — any read failure is unknown, never crash the listener
        return "unknown"

    if not analysis_is_fresh(analysis, now_ms=now_ms):
        return "unknown"
    action = str((analysis or {}).get("action") or "").lower()
    if action == "long":
        return "up"
    if action == "short":
        return "down"
    return "unknown"


def _explore_analysis(
    conn: Any,
    symbol: str,
    *,
    explore_conn: Any | None,
) -> dict[str, Any] | None:
    if explore_conn is not None:
        return latest_explore_analysis(explore_conn, symbol)
    path = explore_db_path(connection_path(conn))
    if not path.exists():
        return None
    ec = open_db(path)
    try:
        return latest_explore_analysis(ec, symbol)
    finally:
        ec.close()


def quick_side_risk(
    trend: Trend,
    side: str,
    eff: dict[str, Any],
) -> tuple[bool, Decimal]:
    """Return (is_counter, risk_pct) from live effective values.

    Counter when trend is unknown or the side opposes the trend.
    """
    side_l = str(side).lower()
    if side_l not in {"long", "short"}:
        side_l = "long"
    if trend == "unknown":
        is_counter = True
    elif trend == "up":
        is_counter = side_l == "short"
    elif trend == "down":
        is_counter = side_l == "long"
    else:
        is_counter = True
    key = "counter_used" if is_counter else "risk_used"
    return is_counter, Decimal(str(eff[key]))
