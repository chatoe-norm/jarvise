"""Ingest health: is indicator warm-up complete and is the stored series fresh?

Stale alerts use one candle interval plus grace (`max_age_min`), because the age of the
newest closed candle naturally cycles from 0 up to one full interval on a healthy series.

Read-only. Alert-only — this module never triggers ingest and never places orders.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from jarvise_ingest.db import count_indicator_ready, count_market, load_latest_candle
from jarvise_ingest.series import find_gaps
from jarvise_ingest.timeframes import INTERVAL_MS

BACKFILL_HINT = (
    "jarvise ingest --symbol {symbols} --timeframe {timeframe} --since 2021-01-01 --skip-derivatives --json"
)


def ingest_health(
    conn: sqlite3.Connection,
    symbols: list[str],
    timeframe: str,
    *,
    now_ms: int | None = None,
    max_age_min: float = 60.0,
) -> dict[str, Any]:
    ts = int(now_ms if now_ms is not None else time.time() * 1000)
    step = INTERVAL_MS[timeframe]
    stale_after_min = step / 60_000.0 + max_age_min
    out: dict[str, dict[str, Any]] = {}
    alerts: list[str] = []
    for raw in symbols:
        sym = raw.strip().upper()
        if not sym:
            continue
        rows = count_market(conn, sym, timeframe)
        ready = count_indicator_ready(conn, sym, timeframe, column="ema_200")
        latest = load_latest_candle(conn, sym, timeframe)
        newest_age_min: float | None = None
        if latest is not None:
            closed_at = int(latest["timestamp"]) + step
            newest_age_min = round(max(0.0, (ts - closed_at) / 60_000.0), 1)
        gaps = len(find_gaps(conn, sym, timeframe))
        out[sym] = {
            "rows": rows,
            "ema200_ready": ready,
            "newest_age_min": newest_age_min,
            "gaps": gaps,
        }
        if rows == 0:
            alerts.append(f"{sym}: no {timeframe} candles stored")
        elif ready == 0:
            alerts.append(f"{sym}: ema_200 not ready ({rows} rows) — analyze stays flat")
        if newest_age_min is not None and newest_age_min > stale_after_min:
            alerts.append(
                f"{sym}: newest {timeframe} candle closed {newest_age_min:.0f} min ago "
                f"(> {stale_after_min:.0f} = one {timeframe} interval + {max_age_min:.0f} grace)"
            )
        if gaps > 0:
            alerts.append(f"{sym}: {gaps} gap(s) in stored {timeframe} series")
    return {
        "ok": not alerts,
        "timeframe": timeframe,
        "symbols": out,
        "alerts": alerts,
        "backfill_hint": BACKFILL_HINT.format(symbols=",".join(out), timeframe=timeframe),
        "at_ms": ts,
        "stale_after_min": stale_after_min,
        "paper_only": True,
    }
