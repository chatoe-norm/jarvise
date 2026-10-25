"""Periodic paper risk monitor: MTM, stop exits, daily drawdown halt."""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from jarvise.freshness import risk_stale_threshold_ms
from jarvise_ingest.db import (
    cancel_pending_approvals,
    day_open_equity,
    ensure_paper_account,
    get_paper_account,
    list_paper_positions,
    listed_timeframes_for_symbol,
    load_newest_candle_ts,
)
from jarvise_ingest.timeframes import INTERVAL_MS
from jarvise_notify.telegram import notify_risk_candle_fresh, notify_risk_candle_stale
from jarvise_paper.marking import mark_to_market
from jarvise_paper.profit_lock import run_profit_lock
from jarvise_paper.stops import run_stop_monitor
from jarvise_risk import check_caps, engage_kill_switch, load_risk_caps, utc_day_bounds_ms

logger = logging.getLogger(__name__)

CANDLE_STALE_KEY = "jarvise:risk_monitor:candle_stale"


def _redis_client():
    url = (os.environ.get("REDIS_URL") or "").strip()
    if not url:
        return None
    import redis

    return redis.Redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=2.0,
        socket_timeout=2.0,
    )


def _read_candle_stale_flag() -> bool | None:
    """Previous edge state. None when Redis is missing or unreadable."""
    try:
        client = _redis_client()
        if client is None:
            return None
        raw = client.get(CANDLE_STALE_KEY)
    except Exception:  # noqa: BLE001 — edge state must not block the skip path
        return None
    if raw is None:
        return None
    return str(raw).strip() in {"1", "true", "on", "yes"}


def _write_candle_stale_flag(stale: bool) -> None:
    try:
        client = _redis_client()
        if client is None:
            return
        client.set(CANDLE_STALE_KEY, "1" if stale else "0")
    except Exception:  # noqa: BLE001
        logger.warning("risk_monitor candle_stale flag write soft-fail")


def _stale_open_positions(conn: Any, *, now_ms: int) -> list[dict[str, Any]]:
    stale: list[dict[str, Any]] = []
    for pos in list_paper_positions(conn):
        symbol = str(pos.get("symbol") or "").upper()
        if not symbol:
            continue
        newest_ts = load_newest_candle_ts(conn, symbol)
        tfs = listed_timeframes_for_symbol(conn, symbol)
        intervals = [INTERVAL_MS[tf] for tf in tfs if tf in INTERVAL_MS]
        if newest_ts is None or not intervals:
            stale.append(
                {
                    "symbol": symbol,
                    "age_ms": None if newest_ts is None else int(now_ms) - int(newest_ts),
                    "newest_ts": newest_ts,
                    "threshold_ms": None,
                }
            )
            continue
        threshold = risk_stale_threshold_ms(min(intervals))
        age_ms = int(now_ms) - int(newest_ts)
        if age_ms > threshold:
            stale.append(
                {
                    "symbol": symbol,
                    "age_ms": age_ms,
                    "newest_ts": int(newest_ts),
                    "threshold_ms": threshold,
                }
            )
    return stale


def _edge_candle_alerts(stale_now: bool, stale_rows: list[dict[str, Any]]) -> dict[str, Any]:
    prev = _read_candle_stale_flag()
    alerted: str | None = None
    if stale_now and prev is not True:
        if notify_risk_candle_stale(stale_rows):
            alerted = "stale"
    elif not stale_now and prev is True:
        if notify_risk_candle_fresh():
            alerted = "fresh"
    _write_candle_stale_flag(stale_now)
    return {"prev": prev, "alerted": alerted}


def run_risk_monitor(conn: Any, *, now_ms: int | None = None) -> dict[str, Any]:
    ts = int(now_ms if now_ms is not None else time.time() * 1000)
    ensure_paper_account(conn)
    positions = list_paper_positions(conn)
    if positions:
        stale_rows = _stale_open_positions(conn, now_ms=ts)
        if stale_rows:
            edge = _edge_candle_alerts(True, stale_rows)
            return {
                "ok": True,
                "skipped": "stale_data",
                "stale": stale_rows,
                "paper_only": True,
                "at_ms": ts,
                "candle_stale_alert": edge.get("alerted"),
            }
        _edge_candle_alerts(False, [])

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
