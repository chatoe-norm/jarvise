"""Periodic paper risk monitor: MTM, stop exits, daily drawdown halt."""

from __future__ import annotations

import time
from typing import Any

from jarvise.freshness import risk_stale_threshold_ms
from jarvise_ingest.db import (
    cancel_pending_approvals,
    day_open_equity,
    ensure_paper_account,
    get_paper_account,
    list_paper_positions,
    load_newest_candle_close_ms,
    set_paper_account_value,
)
from jarvise_notify.telegram import notify_risk_candle_fresh, notify_risk_candle_stale
from jarvise_paper.marking import mark_to_market
from jarvise_paper.profit_lock import run_profit_lock
from jarvise_paper.stops import run_stop_monitor
from jarvise_risk import check_caps, engage_kill_switch, load_risk_caps, utc_day_bounds_ms

CANDLE_STALE_KEY = "risk_monitor_candle_stale"


def _read_candle_stale_flag(conn: Any) -> bool | None:
    """Previous edge state from SQLite. None when the key has never been written."""
    row = conn.execute(
        "SELECT value FROM paper_account WHERE key=?",
        (CANDLE_STALE_KEY,),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return float(row[0]) >= 0.5


def _write_candle_stale_flag(conn: Any, stale: bool) -> None:
    set_paper_account_value(conn, CANDLE_STALE_KEY, 1.0 if stale else 0.0)
    conn.commit()


def _stale_open_positions(conn: Any, *, now_ms: int) -> list[dict[str, Any]]:
    stale: list[dict[str, Any]] = []
    for pos in list_paper_positions(conn):
        symbol = str(pos.get("symbol") or "").upper()
        if not symbol:
            continue
        newest_close, shortest = load_newest_candle_close_ms(conn, symbol)
        if newest_close is None or shortest is None:
            stale.append(
                {
                    "symbol": symbol,
                    "age_ms": None,
                    "newest_ts": newest_close,
                    "threshold_ms": None,
                }
            )
            continue
        threshold = risk_stale_threshold_ms(shortest)
        age_ms = int(now_ms) - int(newest_close)
        if age_ms > threshold:
            stale.append(
                {
                    "symbol": symbol,
                    "age_ms": age_ms,
                    "newest_ts": int(newest_close),
                    "threshold_ms": threshold,
                }
            )
    return stale


def _edge_candle_alerts(
    conn: Any,
    stale_now: bool,
    stale_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    prev = _read_candle_stale_flag(conn)
    alerted: str | None = None
    if stale_now and prev is not True:
        if notify_risk_candle_stale(stale_rows):
            alerted = "stale"
    elif not stale_now and prev is True:
        if notify_risk_candle_fresh():
            alerted = "fresh"
    _write_candle_stale_flag(conn, stale_now)
    return {"prev": prev, "alerted": alerted}


def run_risk_monitor(conn: Any, *, now_ms: int | None = None) -> dict[str, Any]:
    ts = int(now_ms if now_ms is not None else time.time() * 1000)
    ensure_paper_account(conn)
    positions = list_paper_positions(conn)
    if positions:
        stale_rows = _stale_open_positions(conn, now_ms=ts)
        if stale_rows:
            edge = _edge_candle_alerts(conn, True, stale_rows)
            return {
                "ok": True,
                "skipped": "stale_data",
                "stale": stale_rows,
                "paper_only": True,
                "at_ms": ts,
                "candle_stale_alert": edge.get("alerted"),
            }
        _edge_candle_alerts(conn, False, [])

    mtm = mark_to_market(conn, now_ms=ts, persist=True)
    profit_lock = run_profit_lock(conn, now_ms=ts)
    stops = run_stop_monitor(conn, now_ms=ts)
    mtm_after = mark_to_market(conn, now_ms=ts + 1, persist=True)
    caps = load_risk_caps()
    acct = get_paper_account(conn)
    day_start, _end = utc_day_bounds_ms(ts)
    open_eq = day_open_equity(conn, day_start)
    if open_eq is None:
        open_eq = float(acct["starting_equity"])
    mtm_pnl = float(acct["equity"]) - float(open_eq)
    breach = check_caps(
        caps,
        equity=float(acct["equity"]),
        starting_equity=float(acct["starting_equity"]),
        action="flat",
        utc_day_mtm_pnl_usd=mtm_pnl,
    )
    halted = False
    cancelled = 0
    if breach:
        engage_kill_switch(reason=breach)
        cancelled = cancel_pending_approvals(conn, reason="drawdown_halt", ts=ts)
        halted = True
    return {
        "ok": True,
        "paper_only": True,
        "at_ms": ts,
        "mtm": mtm_after,
        "profit_lock": profit_lock,
        "stops": stops,
        "day_mtm_pnl": round(mtm_pnl, 8),
        "halted": halted,
        "halt_reason": breach,
        "pending_cancelled": cancelled,
        "prior_mtm": mtm,
    }
