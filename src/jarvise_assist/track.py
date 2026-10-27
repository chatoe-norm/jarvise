"""Virtual track for sent assist signals (counterfactual R, never writes paper).

Resting-stop gap fill is a copy of the two-line rule in
``jarvise_paper.stops.resting_stop_fill`` — do not edit ``stops.py``.
"""

from __future__ import annotations

from typing import Any

from jarvise_assist.store import close_track, list_open_tracks
from jarvise_ingest.db import load_analysis_available_at, load_candles_in_range
from jarvise_paper.profit_lock import trail_stop_with_source

TRACK_TF = "1h"
ANALYSIS_TF = "4h"
# 4h bar open → available after close + ~1h (timestamp + 5h).
ANALYSIS_LAG_MS = 5 * 3_600_000
FEE_BPS_PER_SIDE = 4.0


def gap_aware_stop_fill(
    *,
    side: str,
    stop_f: float,
    open_px: float,
    low: float,
    high: float,
) -> float | None:
    """Copy of the two-line gap fill from ``resting_stop_fill`` (parity-tested)."""
    if side == "long":
        if low > stop_f:
            return None
        # open already through the stop → fill at open; else resting stop.
        return open_px if open_px <= stop_f else stop_f
    if high < stop_f:
        return None
    return open_px if open_px >= stop_f else stop_f


def _bar_close_ms(bar: dict[str, Any], *, tf_ms: int = 3_600_000) -> int:
    return int(bar["timestamp"]) + int(tf_ms)


def _analysis_exit_reason(
    conn: Any,
    *,
    symbol: str,
    side: str,
    asof_ms: int,
) -> str | None:
    """flat or opposite on the newest 4h analysis available by asof_ms (no lookahead)."""
    row = load_analysis_available_at(
        conn,
        symbol,
        ANALYSIS_TF,
        asof_ms=int(asof_ms),
        lag_ms=ANALYSIS_LAG_MS,
    )
    if row is None:
        return None
    action = str(row.get("action") or "").lower()
    if action == "flat":
        return "flat"
    if action in {"long", "short"} and action != side:
        return "opposite"
    return None


def _fees_usdt(*, entry: float, exit_px: float, qty: float) -> float:
    rate = FEE_BPS_PER_SIDE / 10_000.0
    return abs(entry) * abs(qty) * rate + abs(exit_px) * abs(qty) * rate


def _r_and_pnl(
    *,
    side: str,
    entry: float,
    stop: float,
    exit_px: float,
    qty: float,
    loss_at_stop: float,
) -> tuple[float, float, float, float]:
    risk = abs(entry - stop)
    if risk <= 0 or loss_at_stop <= 0:
        return 0.0, 0.0, _fees_usdt(entry=entry, exit_px=exit_px, qty=qty), 0.0
    if side == "long":
        gross_usd = (exit_px - entry) * qty
    else:
        gross_usd = (entry - exit_px) * qty
    fee = _fees_usdt(entry=entry, exit_px=exit_px, qty=qty)
    net_usd = gross_usd - fee
    gross_r = gross_usd / loss_at_stop
    net_r = net_usd / loss_at_stop
    return gross_r, net_r, fee, net_usd


def resolve_open_tracks(conn: Any, *, now_ms: int) -> dict[str, Any]:
    """Resolve open assist_track rows on closed 1h bars after sent_at_ms."""
    closed: list[dict[str, Any]] = []
    for track in list_open_tracks(conn):
        result = _resolve_one(conn, track, now_ms=int(now_ms))
        if result is not None:
            closed.append(result)
    return {"ok": True, "closed": closed, "n": len(closed)}


def _resolve_one(conn: Any, track: dict[str, Any], *, now_ms: int) -> dict[str, Any] | None:
    symbol = str(track["symbol"]).upper()
    side = str(track["side"]).lower()
    entry = float(track["entry"])
    stop = float(track["stop"])
    target = float(track["target"])
    qty = float(track["qty"])
    sent_at = int(track["sent_at_ms"])
    loss_at_stop = float(track.get("loss_at_stop") or abs(entry - stop) * abs(qty))
    initial_stop = stop

    rows = load_candles_in_range(
        conn,
        symbol,
        TRACK_TF,
        since_ms=sent_at,
        until_ms=int(now_ms),
    )
    # Strict after entry (same cut as resting_stop_fill / bar_range_after).
    bars = [r for r in rows if int(r["timestamp"]) > sent_at and _bar_close_ms(r) <= int(now_ms)]
    if not bars:
        return None

    best_high: float | None = None
    best_low: float | None = None
    current_stop = stop

    for bar in bars:
        open_px = float(bar["open"])
        high = float(bar["high"])
        low = float(bar["low"])
        close = float(bar["close"])
        bar_ts = int(bar["timestamp"])
        close_ms = _bar_close_ms(bar)

        fill = gap_aware_stop_fill(side=side, stop_f=current_stop, open_px=open_px, low=low, high=high)
        if fill is not None:
            return _finalize(
                conn,
                track,
                exit_at_ms=close_ms,
                exit_px=float(fill),
                exit_reason="stop",
                entry=entry,
                stop=initial_stop,
                qty=qty,
                loss_at_stop=loss_at_stop,
                side=side,
            )

        if best_high is None or high > best_high:
            best_high = high
        if best_low is None or low < best_low:
            best_low = low

        pos = {
            "side": side,
            "entry_price": entry,
            "initial_stop_price": initial_stop,
            "stop_price": current_stop,
        }
        trailed = trail_stop_with_source(pos, best_low=best_low, best_high=best_high)
        if trailed is not None:
            current_stop = float(trailed[0])

        # 2R target on this bar.
        if side == "long" and high >= target:
            return _finalize(
                conn,
                track,
                exit_at_ms=close_ms,
                exit_px=target,
                exit_reason="target_2r",
                entry=entry,
                stop=initial_stop,
                qty=qty,
                loss_at_stop=loss_at_stop,
                side=side,
            )
        if side == "short" and low <= target:
            return _finalize(
                conn,
                track,
                exit_at_ms=close_ms,
                exit_px=target,
                exit_reason="target_2r",
                entry=entry,
                stop=initial_stop,
                qty=qty,
                loss_at_stop=loss_at_stop,
                side=side,
            )

        reason = _analysis_exit_reason(conn, symbol=symbol, side=side, asof_ms=close_ms)
        if reason is not None:
            return _finalize(
                conn,
                track,
                exit_at_ms=close_ms,
                exit_px=close,
                exit_reason=reason,
                entry=entry,
                stop=initial_stop,
                qty=qty,
                loss_at_stop=loss_at_stop,
                side=side,
            )

        _ = bar_ts  # keep bar_ts used for clarity / future trail cut

    return None


def _finalize(
    conn: Any,
    track: dict[str, Any],
    *,
    exit_at_ms: int,
    exit_px: float,
    exit_reason: str,
    entry: float,
    stop: float,
    qty: float,
    loss_at_stop: float,
    side: str,
) -> dict[str, Any]:
    gross_r, net_r, fee, pnl = _r_and_pnl(
        side=side,
        entry=entry,
        stop=stop,
        exit_px=exit_px,
        qty=qty,
        loss_at_stop=loss_at_stop,
    )
    close_track(
        conn,
        str(track["signal_id"]),
        exit_at_ms=exit_at_ms,
        exit_px=exit_px,
        exit_reason=exit_reason,
        gross_r=gross_r,
        net_r=net_r,
        fee_usdt=fee,
        pnl_usdt=pnl,
    )
    return {
        "signal_id": track["signal_id"],
        "exit_reason": exit_reason,
        "exit_px": exit_px,
        "gross_r": gross_r,
        "net_r": net_r,
        "fee_usdt": fee,
        "pnl_usdt": pnl,
    }
