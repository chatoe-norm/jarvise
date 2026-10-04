"""Periodic paper risk monitor: MTM, stop exits, daily drawdown halt."""

from __future__ import annotations

import time
from typing import Any

from jarvise_ingest.db import (
    cancel_pending_approvals,
    day_open_equity,
    ensure_paper_account,
    get_paper_account,
)
from jarvise_paper.marking import mark_to_market
from jarvise_paper.stops import run_stop_monitor
from jarvise_risk import check_caps, engage_kill_switch, load_risk_caps, utc_day_bounds_ms


def run_risk_monitor(conn: Any, *, now_ms: int | None = None) -> dict[str, Any]:
    ts = int(now_ms if now_ms is not None else time.time() * 1000)
    ensure_paper_account(conn)
    mtm = mark_to_market(conn, now_ms=ts, persist=True)
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
        "stops": stops,
        "day_mtm_pnl": round(mtm_pnl, 8),
        "halted": halted,
        "halt_reason": breach,
        "pending_cancelled": cancelled,
        "prior_mtm": mtm,
    }
