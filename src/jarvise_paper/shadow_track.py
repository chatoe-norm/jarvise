"""Counterfactual paper track. One position per symbol, independent of verdicts.

The walk is a pure function of stored analysis rows and closed candles up to
``as_of_ms``. It does not read verdicts or the paper ledger. ``approval_queue``
is read only to pick which analysis row represents a bar.
"""

from __future__ import annotations

import os
from typing import Any

from jarvise_analyze.replay import bar_range_after
from jarvise_ingest.db import universe_as_of
from jarvise_paper.engine import fill_price
from jarvise_paper.stops import evaluate_stop

FEE_BPS = 10.0
SLIP_BPS = 5.0
MAX_BARS = 30
REF_USD_PER_R = 100.0
# 2026-10-18T06:00:00Z. Entries before this bar are not tracked.
SHADOW_START_MS = 1_792_303_200_000
BAR_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


def bar_ms(timeframe: str) -> int:
    try:
        return BAR_MS[timeframe]
    except KeyError as exc:
        raise ValueError(f"unsupported timeframe: {timeframe}") from exc


def paper_universe_id() -> str:
    return (os.environ.get("JARVISE_PAPER_UNIVERSE") or "paper_core").strip() or "paper_core"


def paper_timeframe() -> str:
    return (os.environ.get("JARVISE_PAPER_TIMEFRAME") or "4h").strip() or "4h"


def _fee(price: float) -> float:
    return abs(price) * (FEE_BPS / 10_000.0)


def _spread_at(conn: Any, symbol: str, ts: int) -> float | None:
    row = conn.execute(
        """
        SELECT bid_ask_spread FROM order_book_microstructure
        WHERE symbol=? AND timestamp<=?
        ORDER BY timestamp DESC LIMIT 1
        """,
        (symbol, int(ts)),
    ).fetchone()
    if row is None or row["bid_ask_spread"] is None:
        return None
    return float(row["bid_ask_spread"])


def _fill(mid: float, *, side: str, spread: float | None) -> float:
    return fill_price(mid, side=side, slip_bps=SLIP_BPS, bid_ask_spread=spread)


def _rounds(conn: Any, symbol: str, timeframe: str, *, as_of_ms: int, width: int) -> list[dict[str, Any]]:
    """Closed bars only: a bar is closed once ``timestamp + bar`` is at or before ``as_of_ms``."""
    rows = conn.execute(
        """
        SELECT timestamp, close, high, low FROM market_technicals
        WHERE symbol=? AND timeframe=? AND timestamp + ? <= ?
        ORDER BY timestamp
        """,
        (symbol, timeframe, int(width), int(as_of_ms)),
    ).fetchall()
    return [dict(row) for row in rows]


def _analyses(
    conn: Any, symbol: str, timeframe: str, *, as_of_ms: int, width: int
) -> dict[int, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT rowid, analysis_id, timestamp, symbol, timeframe, regime_state, confidence_score,
               action, invalidation_price
        FROM analysis_output
        WHERE symbol=? AND timeframe=? AND timestamp + ? <= ?
        ORDER BY timestamp, rowid
        """,
        (symbol, timeframe, int(width), int(as_of_ms)),
    ).fetchall()
    queued = {
        str(row["analysis_id"])
        for row in conn.execute(
            """
            SELECT analysis_id FROM approval_queue
            WHERE symbol=? AND timeframe=? AND analysis_id IS NOT NULL
            """,
            (symbol, timeframe),
        )
        if row["analysis_id"] is not None
    }
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(int(row["timestamp"]), []).append(dict(row))
    return {ts: _choose_analysis(group, queued) for ts, group in grouped.items()}


def _choose_analysis(rows: list[dict[str, Any]], queued: set[str]) -> dict[str, Any]:
    """Flat wins. Otherwise the queued analysis. Otherwise the highest rowid."""
    flats = [row for row in rows if str(row.get("action") or "").lower() == "flat"]
    if flats:
        return max(flats, key=lambda row: int(row["rowid"]))
    cited = [row for row in rows if str(row["analysis_id"]) in queued]
    if cited:
        return max(cited, key=lambda row: int(row["rowid"]))
    return max(rows, key=lambda row: int(row["rowid"]))


def _symbols(conn: Any, universe_id: str, as_of_ms: int) -> list[str]:
    return universe_as_of(conn, universe_id, as_of_ms)


def _member(conn: Any, universe_id: str, symbol: str, ts: int, cache: dict[int, set[str]]) -> bool:
    if ts not in cache:
        cache[ts] = set(universe_as_of(conn, universe_id, ts))
    return symbol in cache[ts]


def _pnl(
    *,
    side: str,
    entry_mid: float,
    invalidation: float,
    entry_fill: float,
    exit_fill: float,
) -> tuple[float, float, float]:
    risk = abs(entry_mid - invalidation)
    entry_fee = _fee(entry_fill)
    exit_fee = _fee(exit_fill)
    if side == "long":
        gross = exit_fill - entry_fill
    else:
        gross = entry_fill - exit_fill
    net = gross - entry_fee - exit_fee
    gross_r = gross / risk
    net_r = net / risk
    return gross_r, net_r, net_r * REF_USD_PER_R


def _close_row(
    open_row: dict[str, Any],
    *,
    exit_ts: int,
    exit_px: float,
    exit_reason: str,
    bars_held: int,
) -> dict[str, Any]:
    gross_r, net_r, pnl_ref = _pnl(
        side=str(open_row["side"]),
        entry_mid=float(open_row["entry_mid"]),
        invalidation=float(open_row["invalidation"]),
        entry_fill=float(open_row["entry_px"]),
        exit_fill=exit_px,
    )
    return {
        **open_row,
        "exit_ts": int(exit_ts),
        "exit_px": float(exit_px),
        "exit_reason": exit_reason,
        "gross_r": gross_r,
        "net_r": net_r,
        "pnl_ref_usd": pnl_ref,
        "bars_held": int(bars_held),
        "resolved_at": int(exit_ts),
    }


def build_track(
    conn: Any,
    *,
    as_of_ms: int,
    universe_id: str | None = None,
    timeframe: str | None = None,
) -> list[dict[str, Any]]:
    """Return track rows whose entry bar has closed by ``as_of_ms``.

    An open row has null exit fields. A closed row is resolved on its exit bar.
    """
    universe = universe_id or paper_universe_id()
    tf = timeframe or paper_timeframe()
    width = bar_ms(tf)
    membership: dict[int, set[str]] = {}
    built: list[dict[str, Any]] = []
    for symbol in _symbols(conn, universe, as_of_ms):
        candles = _rounds(conn, symbol, tf, as_of_ms=as_of_ms, width=width)
        by_ts = _analyses(conn, symbol, tf, as_of_ms=as_of_ms, width=width)
        position: dict[str, Any] | None = None
        for candle in candles:
            ts = int(candle["timestamp"])
            close = candle.get("close")
            if close is None:
                continue
            analysis = by_ts.get(ts)
            if position is not None and ts > int(position["entry_ts"]):
                held = sum(1 for item in candles if int(position["entry_ts"]) < int(item["timestamp"]) <= ts)
                low, high = bar_range_after(conn, symbol, tf, after_ts=int(position["entry_ts"]), until_ms=ts)
                hit = evaluate_stop(
                    {
                        "symbol": symbol,
                        "side": position["side"],
                        "stop_price": position["invalidation"],
                    },
                    mid=float(close),
                    low_since_entry=low,
                    high_since_entry=high,
                )
                exit_reason: str | None = None
                exit_px: float | None = None
                if hit is not None:
                    exit_reason = "stop"
                    exit_side = "sell" if position["side"] == "long" else "buy"
                    exit_px = _fill(float(hit.fill_mid), side=exit_side, spread=_spread_at(conn, symbol, ts))
                elif analysis is not None and str(analysis["action"]).lower() in {
                    "flat",
                    _opposite(str(position["side"])),
                }:
                    action = str(analysis["action"]).lower()
                    exit_reason = "flat" if action == "flat" else "opposite"
                    exit_side = "sell" if position["side"] == "long" else "buy"
                    exit_px = _fill(float(close), side=exit_side, spread=_spread_at(conn, symbol, ts))
                elif held >= MAX_BARS:
                    exit_reason = "time"
                    exit_side = "sell" if position["side"] == "long" else "buy"
                    exit_px = _fill(float(close), side=exit_side, spread=_spread_at(conn, symbol, ts))
                if exit_reason is not None and exit_px is not None:
                    built.append(
                        _close_row(
                            position, exit_ts=ts, exit_px=exit_px, exit_reason=exit_reason, bars_held=held
                        )
                    )
                    position = None
                    continue
            if (
                position is None
                and analysis is not None
                and int(analysis["timestamp"]) >= SHADOW_START_MS
                and _member(conn, universe, symbol, ts, membership)
            ):
                opened = _try_open(conn, analysis, close=float(close))
                if opened is not None:
                    position = opened
        if position is not None:
            built.append(position)
    return built


def _opposite(side: str) -> str:
    return "short" if side == "long" else "long"


def _try_open(conn: Any, analysis: dict[str, Any], *, close: float) -> dict[str, Any] | None:
    action = str(analysis.get("action") or "").lower()
    if action not in {"long", "short"}:
        return None
    raw = analysis.get("invalidation_price")
    if raw is None:
        return None
    invalidation = float(raw)
    if abs(close - invalidation) <= 0:
        return None
    ts = int(analysis["timestamp"])
    symbol = str(analysis["symbol"]).upper()
    spread = _spread_at(conn, symbol, ts)
    entry_side = "buy" if action == "long" else "sell"
    return {
        "analysis_id": str(analysis["analysis_id"]),
        "symbol": symbol,
        "side": action,
        "entry_ts": ts,
        "entry_px": _fill(close, side=entry_side, spread=spread),
        "entry_mid": close,
        "invalidation": invalidation,
        "exit_ts": None,
        "exit_px": None,
        "exit_reason": None,
        "gross_r": None,
        "net_r": None,
        "pnl_ref_usd": None,
        "bars_held": None,
        "resolved_at": None,
    }


def sync_track(conn: Any, rows: list[dict[str, Any]]) -> int:
    """Cache ``build_track`` rows. Does not delete rows the walk did not return."""
    for row in rows:
        conn.execute(
            """
            INSERT INTO paper_shadow_track (
                analysis_id, symbol, side, entry_ts, entry_px, invalidation,
                exit_ts, exit_px, exit_reason, gross_r, net_r, pnl_ref_usd,
                bars_held, resolved_at
            ) VALUES (
                :analysis_id, :symbol, :side, :entry_ts, :entry_px, :invalidation,
                :exit_ts, :exit_px, :exit_reason, :gross_r, :net_r, :pnl_ref_usd,
                :bars_held, :resolved_at
            )
            ON CONFLICT(analysis_id) DO UPDATE SET
                symbol=excluded.symbol,
                side=excluded.side,
                entry_ts=excluded.entry_ts,
                entry_px=excluded.entry_px,
                invalidation=excluded.invalidation,
                exit_ts=excluded.exit_ts,
                exit_px=excluded.exit_px,
                exit_reason=excluded.exit_reason,
                gross_r=excluded.gross_r,
                net_r=excluded.net_r,
                pnl_ref_usd=excluded.pnl_ref_usd,
                bars_held=excluded.bars_held,
                resolved_at=excluded.resolved_at
            """,
            row,
        )
    conn.commit()
    return len(rows)
