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
# Binance USDⓈ-M VIP0 taker (no BNB discount). Entry, stop and TP are all market fills.
FEE_BPS_PER_SIDE = 5.0
# Adverse slippage per market fill. Cost only: exit_px and gross_r stay at the bar price.
SLIPPAGE_BPS_PER_SIDE = 1.0
# Stop exits are labelled by the stop that filled.
STOP_EXIT_REASONS = {"initial": "initial_stop", "breakeven": "be_stop", "trail": "trail_stop"}
# Virtual-track sample id for the seven-universe 4h assistant. A new symbol or
# timeframe must use a different string; never mix cohorts in weekly stats.
TRACK_COHORT = "core7_4h_v2"
# Binance USDⓈ-M funding settlements (00:00 / 08:00 / 16:00 UTC).
FUNDING_INTERVAL_MS = 8 * 3_600_000


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


def _slippage_usdt(*, entry: float, exit_px: float, qty: float) -> float:
    rate = SLIPPAGE_BPS_PER_SIDE / 10_000.0
    return abs(entry) * abs(qty) * rate + abs(exit_px) * abs(qty) * rate


def _deriv_coin(symbol: str) -> str:
    coin = symbol.upper()
    for quote in ("USDT", "USD", "BUSD", "USDC"):
        if coin.endswith(quote) and len(coin) > len(quote):
            return coin[: -len(quote)]
    return coin


_FUNDING_WINDOW_MS = 60_000


def _funding_rate_at_or_before(conn: Any, symbol: str, at_ms: int) -> float | None:
    """Funding rate in the ±60s settlement window (latest timestamp, then ingested_at).

    Candidates: base-coin or pair rows with ``t - 60_000 < timestamp <= t + 60_000``
    and a non-null rate. A row at or before ``t - 60_000`` is ignored.
    """
    pair = symbol.upper()
    coin = _deriv_coin(pair)
    t = int(at_ms)
    row = conn.execute(
        """
        SELECT funding_rate FROM derivatives_analytics
        WHERE symbol IN (?, ?)
          AND timestamp > ?
          AND timestamp <= ?
          AND funding_rate IS NOT NULL
        ORDER BY timestamp DESC, ingested_at DESC
        LIMIT 1
        """,
        (coin, pair, t - _FUNDING_WINDOW_MS, t + _FUNDING_WINDOW_MS),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return float(row[0])


def _funding_cost(
    conn: Any,
    *,
    symbol: str,
    side: str,
    entry: float,
    qty: float,
    entry_ms: int,
    exit_ms: int,
) -> tuple[float, int]:
    """Virtual funding USDT cost and count of settlements with a missing rate.

    Settlements at 00/08/16 UTC with entry_ms < t <= exit_ms. Positive rate: long
    pays, short receives. Missing rate costs 0 and increments the missing count.
    """
    first = ((int(entry_ms) // FUNDING_INTERVAL_MS) + 1) * FUNDING_INTERVAL_MS
    notional = abs(entry) * abs(qty)
    total = 0.0
    missing = 0
    t = first
    while t <= int(exit_ms):
        rate = _funding_rate_at_or_before(conn, symbol, t)
        if rate is None:
            missing += 1
        else:
            # Long pays positive rate; short receives (negative cost).
            signed = notional * rate if side == "long" else -notional * rate
            total += signed
        t += FUNDING_INTERVAL_MS
    return total, missing


def _r_and_pnl(
    *,
    side: str,
    entry: float,
    stop: float,
    exit_px: float,
    qty: float,
    loss_at_stop: float,
    funding_usdt: float = 0.0,
) -> tuple[float, float, float, float]:
    risk = abs(entry - stop)
    if risk <= 0 or loss_at_stop <= 0:
        return 0.0, 0.0, _fees_usdt(entry=entry, exit_px=exit_px, qty=qty), 0.0
    if side == "long":
        gross_usd = (exit_px - entry) * qty
    else:
        gross_usd = (entry - exit_px) * qty
    fee = _fees_usdt(entry=entry, exit_px=exit_px, qty=qty)
    net_usd = gross_usd - fee - _slippage_usdt(entry=entry, exit_px=exit_px, qty=qty) - funding_usdt
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
    stop_source = "initial"

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
                exit_reason=STOP_EXIT_REASONS.get(stop_source, "trail_stop"),
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
            stop_source = str(trailed[1])

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
    funding_usdt, funding_missing = _funding_cost(
        conn,
        symbol=str(track["symbol"]),
        side=side,
        entry=entry,
        qty=qty,
        entry_ms=int(track["sent_at_ms"]),
        exit_ms=int(exit_at_ms),
    )
    gross_r, net_r, fee, pnl = _r_and_pnl(
        side=side,
        entry=entry,
        stop=stop,
        exit_px=exit_px,
        qty=qty,
        loss_at_stop=loss_at_stop,
        funding_usdt=funding_usdt,
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
        funding_usdt=funding_usdt,
        funding_missing=funding_missing,
    )
    return {
        "signal_id": track["signal_id"],
        "exit_reason": exit_reason,
        "exit_px": exit_px,
        "gross_r": gross_r,
        "net_r": net_r,
        "fee_usdt": fee,
        "pnl_usdt": pnl,
        "funding_usdt": funding_usdt,
        "funding_missing": funding_missing,
    }
