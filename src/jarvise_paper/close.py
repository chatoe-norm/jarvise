"""Owner close of one open paper position. No exchange order."""

from __future__ import annotations

import time
from typing import Any

from jarvise_ingest.db import get_paper_position, load_latest_candle, load_newest_close
from jarvise_paper.engine import apply_signal
from jarvise_trade import live_trading_enabled


def close_position(conn: Any, symbol: str, *, now_ms: int | None = None) -> dict[str, Any]:
    """Close one paper position at the newest stored close.

    Refuses while live trading is on. Allowed while the kill switch is engaged,
    same as a stop exit: closing reduces exposure.
    """
    if live_trading_enabled():
        return {"ok": False, "error": "live_trading_enabled", "paper_only": False, "fills": []}
    sym = str(symbol or "").upper()
    if not sym:
        return {"ok": False, "error": "missing_symbol", "paper_only": True, "fills": []}
    pos = get_paper_position(conn, sym)
    if pos is None:
        return {"ok": False, "error": "no_position", "paper_only": True, "fills": []}
    mid = load_newest_close(conn, sym)
    if mid is None:
        return {"ok": False, "error": "no_price", "paper_only": True, "fills": []}
    ts = int(now_ms if now_ms is not None else time.time() * 1000)
    candle = load_latest_candle(conn, sym, "4h")
    timeframe = str(candle["timeframe"]) if candle else "4h"
    applied = apply_signal(
        conn,
        analysis={
            "analysis_id": f"close-{sym}-{ts}",
            "symbol": sym,
            "action": "flat",
            "regime_state": "manual",
            "confidence_score": 0.0,
            "size_pct_equity": 0.0,
        },
        mid_price=float(mid),
        timeframe=timeframe,
        now_ms=ts,
        decision_source="manual",
        exit_reason="close_manual",
    )
    return {
        "ok": True,
        "paper_only": True,
        "symbol": sym,
        "fills": applied.get("fills") or [],
    }
