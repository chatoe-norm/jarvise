"""Paper stop-loss: evaluate, monitor, backfill from analysis or 1.5×ATR."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jarvise_analyze.engine import ATR_STOP_MULT
from jarvise_ingest.db import (
    get_analysis_output,
    list_paper_orders_asc,
    list_paper_positions,
    load_candles_in_range,
    load_latest_candle,
    load_newest_close,
    set_position_stop,
)
from jarvise_paper.engine import apply_signal

# Shortest first when the preferred timeframe has no bars in the stop window.
_STOP_TF_FALLBACK = ("1h", "4h", "1d")


@dataclass(frozen=True)
class StopHit:
    symbol: str
    side: str
    stop: float
    fill_mid: float
    trigger: str


def evaluate_stop(
    position: dict[str, Any],
    *,
    mid: float | None,
    low_since_entry: float | None,
    high_since_entry: float | None,
) -> StopHit | None:
    """Shadow / pre-registered fill: worst extreme since entry. Not the paper path."""
    stop = position.get("stop_price")
    if stop is None:
        return None
    stop_f = float(stop)
    side = str(position.get("side") or "").lower()
    symbol = str(position.get("symbol") or "").upper()
    mid_f = None if mid is None else float(mid)
    if side == "long":
        low = None if low_since_entry is None else float(low_since_entry)
        hit_px: float | None = None
        trigger = "mid"
        if low is not None and low <= stop_f:
            hit_px = low
            trigger = "bar_low"
        if mid_f is not None and mid_f <= stop_f:
            hit_px = mid_f if hit_px is None else min(hit_px, mid_f)
            trigger = "mid"
        if hit_px is None:
            return None
        fill_mid = min(stop_f, hit_px)
        return StopHit(symbol=symbol, side=side, stop=stop_f, fill_mid=fill_mid, trigger=trigger)
    if side == "short":
        high = None if high_since_entry is None else float(high_since_entry)
        hit_px = None
        trigger = "mid"
        if high is not None and high >= stop_f:
            hit_px = high
            trigger = "bar_high"
        if mid_f is not None and mid_f >= stop_f:
            hit_px = mid_f if hit_px is None else max(hit_px, mid_f)
            trigger = "mid"
        if hit_px is None:
            return None
        fill_mid = max(stop_f, hit_px)
        return StopHit(symbol=symbol, side=side, stop=stop_f, fill_mid=fill_mid, trigger=trigger)
    return None


def _open_order_timeframe(conn: Any, symbol: str) -> str | None:
    latest: dict[str, Any] | None = None
    for order in list_paper_orders_asc(conn):
        if str(order.get("symbol") or "").upper() != symbol.upper():
            continue
        reason = str(order.get("reason") or "")
        if reason.startswith("open_"):
            latest = order
        elif reason.startswith("close_") or reason == "stop_hit":
            latest = None
    if latest is None:
        return None
    tf = latest.get("timeframe")
    return None if tf is None else str(tf)


def _bars_after_for_stop(
    conn: Any,
    symbol: str,
    *,
    after_ts: int,
    until_ms: int,
    preferred_tf: str | None,
) -> tuple[str, list[dict[str, Any]]]:
    ordered: list[str] = []
    if preferred_tf:
        ordered.append(str(preferred_tf))
    for tf in _STOP_TF_FALLBACK:
        if tf not in ordered:
            ordered.append(tf)
    for tf in ordered:
        rows = load_candles_in_range(
            conn,
            symbol,
            tf,
            since_ms=int(after_ts),
            until_ms=int(until_ms),
        )
        bars = [row for row in rows if int(row["timestamp"]) > int(after_ts)]
        if bars:
            return tf, bars
    return preferred_tf or "4h", []


def resting_stop_fill(
    conn: Any,
    position: dict[str, Any],
    *,
    mid: float | None,
    until_ms: int,
    timeframe: str | None = None,
) -> StopHit | None:
    """Fill a stop as a resting order: at the stop, or at a gap open.

    Scans one timeframe after ``stop_moved_ts`` (else ``entry_ts``). Does not
    change the stop level. Slippage and fees are applied by the fill path.
    """
    stop = position.get("stop_price")
    if stop is None:
        return None
    stop_f = float(stop)
    side = str(position.get("side") or "").lower()
    symbol = str(position.get("symbol") or "").upper()
    if side not in {"long", "short"}:
        return None
    raw_since = position.get("stop_moved_ts")
    if raw_since is None:
        raw_since = position.get("entry_ts")
    if raw_since is None:
        return None
    preferred = timeframe or _open_order_timeframe(conn, symbol) or "4h"
    _, bars = _bars_after_for_stop(
        conn,
        symbol,
        after_ts=int(raw_since),
        until_ms=int(until_ms),
        preferred_tf=preferred,
    )
    for bar in bars:
        open_px = float(bar["open"])
        if side == "long":
            low = float(bar["low"])
            if low > stop_f:
                continue
            fill_mid = open_px if open_px <= stop_f else stop_f
            return StopHit(
                symbol=symbol,
                side=side,
                stop=stop_f,
                fill_mid=fill_mid,
                trigger="bar_low",
            )
        high = float(bar["high"])
        if high < stop_f:
            continue
        fill_mid = open_px if open_px >= stop_f else stop_f
        return StopHit(
            symbol=symbol,
            side=side,
            stop=stop_f,
            fill_mid=fill_mid,
            trigger="bar_high",
        )
    mid_f = None if mid is None else float(mid)
    if mid_f is None:
        return None
    if side == "long" and mid_f <= stop_f:
        return StopHit(
            symbol=symbol,
            side=side,
            stop=stop_f,
            fill_mid=min(stop_f, mid_f),
            trigger="mid",
        )
    if side == "short" and mid_f >= stop_f:
        return StopHit(
            symbol=symbol,
            side=side,
            stop=stop_f,
            fill_mid=max(stop_f, mid_f),
            trigger="mid",
        )
    return None


def run_stop_monitor(conn: Any, *, now_ms: int) -> dict[str, Any]:
    """FLAT any paper position whose stop is hit. Allowed while kill-switch is engaged."""
    closed = 0
    hits: list[dict[str, Any]] = []
    for pos in list_paper_positions(conn):
        if pos.get("stop_price") is None:
            continue
        symbol = str(pos["symbol"])
        mid = load_newest_close(conn, symbol)
        hit = resting_stop_fill(conn, pos, mid=mid, until_ms=int(now_ms))
        if hit is None:
            continue
        preferred = _open_order_timeframe(conn, symbol) or "4h"
        candle = load_latest_candle(conn, symbol, preferred)
        timeframe = str(candle["timeframe"]) if candle else preferred
        prefix = "trail" if str(pos.get("stop_source") or "") == "trail" else "stop"
        apply_signal(
            conn,
            analysis={
                "analysis_id": f"{prefix}-{symbol}-{now_ms}",
                "symbol": symbol,
                "action": "flat",
                "regime_state": "stop",
                "confidence_score": 0.0,
                "size_pct_equity": 0.0,
            },
            mid_price=hit.fill_mid,
            timeframe=timeframe,
            now_ms=now_ms,
            approval_id=pos.get("approval_id"),
            decision_source="stop",
            exit_reason="stop_hit",
        )
        closed += 1
        hits.append({"symbol": symbol, "fill_mid": hit.fill_mid, "trigger": hit.trigger})
    return {"ok": True, "closed": closed, "hits": hits, "paper_only": True}


def backfill_stops(conn: Any, *, dry_run: bool) -> dict[str, Any]:
    would = 0
    updated = 0
    details: list[dict[str, Any]] = []
    orders = list_paper_orders_asc(conn)
    latest_open: dict[str, dict[str, Any]] = {}
    for order in orders:
        reason = str(order.get("reason") or "")
        sym = str(order["symbol"]).upper()
        if reason.startswith("open_"):
            latest_open[sym] = order
        elif reason.startswith("close_") or reason == "stop_hit":
            latest_open.pop(sym, None)
    for pos in list_paper_positions(conn):
        if pos.get("stop_price") is not None:
            continue
        symbol = str(pos["symbol"]).upper()
        stop = None
        source = "backfill"
        open_fill = latest_open.get(symbol)
        if open_fill and open_fill.get("analysis_id"):
            analysis = get_analysis_output(conn, str(open_fill["analysis_id"]))
            if analysis and analysis.get("invalidation_price") is not None:
                stop = float(analysis["invalidation_price"])
                source = "backfill"
        if stop is None:
            entry = float(pos["entry_price"])
            candle = load_latest_candle(conn, symbol, "4h") or {}
            atr = candle.get("atr_14")
            if atr is None:
                details.append({"symbol": symbol, "skipped": "no_atr"})
                continue
            atr_f = float(atr)
            if str(pos["side"]).lower() == "long":
                stop = entry - ATR_STOP_MULT * atr_f
            else:
                stop = entry + ATR_STOP_MULT * atr_f
        would += 1
        details.append({"symbol": symbol, "stop_price": stop, "stop_source": source})
        if not dry_run:
            set_position_stop(conn, symbol, stop_price=stop, stop_source=source)
            updated += 1
    return {
        "ok": True,
        "dry_run": dry_run,
        "would_update": would,
        "updated": updated,
        "details": details,
        "paper_only": True,
    }


def remaining_stop(pos: dict[str, Any]) -> dict[str, Any] | None:
    stop = pos.get("stop_price")
    if stop is None:
        return None
    entry = float(pos.get("entry_price") or 0.0)
    return {
        "stop_price": float(stop),
        "distance_pct": None if entry == 0 else abs(entry - float(stop)) / abs(entry) * 100.0,
    }
