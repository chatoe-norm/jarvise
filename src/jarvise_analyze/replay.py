"""Historical analyze replay — paper only. No exchange orders."""

from __future__ import annotations

from typing import Any, Literal

from jarvise_analyze.engine import CONFIDENCE_THRESHOLD, analyze_mtf, analyze_snapshot
from jarvise_analyze.strategies import STRATEGY_NAMES, StrategyName, decide
from jarvise_ingest.db import (
    get_paper_position,
    load_candles_in_range,
    reset_paper_ledger,
    set_position_stop,
    upsert_analysis_output,
)
from jarvise_ingest.timeframes import INTERVAL_MS
from jarvise_paper.auto_decide import flat_exit_needed, same_side_already_open
from jarvise_paper.engine import apply_signal
from jarvise_paper.metrics import compute_paper_metrics
from jarvise_paper.profit_lock import trail_stop
from jarvise_paper.stops import resting_stop_fill

PaperPolicy = Literal["naive", "rules"]
PAPER_POLICIES: tuple[str, ...] = ("naive", "rules")
StopMode = Literal["off", "fixed", "trail"]
STOP_MODES: tuple[str, ...] = ("off", "fixed", "trail")
ExitMode = Literal["confidence", "regime"]
EXIT_MODES: tuple[str, ...] = ("confidence", "regime")
FROZEN_STRATEGY_NOTE = (
    "Frozen strategy. In-sample and out-of-sample are separate holdouts. "
    "Do not keep the window with the better EV. Do not suggest a confidence floor."
)
REPLAY_HTF = "1d"


def closed_htf_candle(
    htf_bars: list[dict[str, Any]],
    ltf_ts: int,
    *,
    htf_ms: int,
) -> dict[str, Any] | None:
    """Latest HTF bar that has closed by ``ltf_ts``.

    Timestamps are open times. The bar is known only once ``open + htf_ms``
    has passed, and only when ``ema_200`` is already stored.
    """
    lo = 0
    hi = len(htf_bars)
    while lo < hi:
        mid = (lo + hi) // 2
        if int(htf_bars[mid]["timestamp"]) + htf_ms <= int(ltf_ts):
            lo = mid + 1
        else:
            hi = mid
    idx = lo - 1
    if idx < 0:
        return None
    bar = htf_bars[idx]
    if bar.get("ema_200") is None:
        return None
    return bar


def replay_range(
    conn: Any,
    *,
    symbols: list[str],
    timeframe: str,
    since_ms: int,
    until_ms: int,
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
    apply_paper: bool = False,
    dry_run: bool = False,
    paper_policy: PaperPolicy = "naive",
    stops: StopMode = "off",
    mtf: bool = False,
    exit_mode: ExitMode = "confidence",
    no_breakeven: bool = False,
    strategy: StrategyName = "current",
) -> dict[str, Any]:
    """Walk closed candles in range; upsert analysis; optionally paper-fill.

    ``paper_policy="rules"`` matches auto-decide *code* gates (no Claude): same-side
    hold, opposite-side defer (does not flip), FLAT closes an open position.
    Does not change ``apply_signal`` behavior for the live paper path.

    ``mtf=True`` confirms each LTF bar with the latest closed ``1d`` candle that
    already has ``ema_200``. The flag does not read ``JARVISE_ANALYZE_MTF``.
    A daily bar is visible only after its open time plus one day, so the same
    day's daily candle cannot leak into earlier LTF bars.
    """
    if paper_policy not in PAPER_POLICIES:
        raise ValueError(f"paper_policy must be one of {PAPER_POLICIES}")
    if stops not in STOP_MODES:
        raise ValueError(f"stops must be one of {STOP_MODES}")
    if exit_mode not in EXIT_MODES:
        raise ValueError(f"exit_mode must be one of {EXIT_MODES}")
    if no_breakeven and stops != "trail":
        raise ValueError("no_breakeven requires stops='trail'")
    if strategy not in STRATEGY_NAMES:
        raise ValueError(f"strategy must be one of {STRATEGY_NAMES}")
    if strategy != "current" and mtf:
        raise ValueError("a non-default strategy does not combine with mtf")
    if strategy != "current" and paper_policy != "rules":
        raise ValueError("a non-default strategy requires paper_policy='rules'")
    if strategy != "current" and confidence_threshold != CONFIDENCE_THRESHOLD:
        raise ValueError("a non-default strategy keeps the production confidence floor")
    if apply_paper and not dry_run:
        reset_paper_ledger(conn)

    analyses: list[dict[str, Any]] = []
    fills_total = 0
    bars = 0
    errors: list[str] = []
    by_symbol: dict[str, int] = {}
    policy_counts = {"hold": 0, "defer": 0, "fill_bars": 0, "flat_exit": 0, "skip": 0}
    action_counts = {"long": 0, "short": 0, "flat": 0}
    mtf_reasons: dict[str, int] = {}
    htf_joined = 0
    htf_ms = INTERVAL_MS[REPLAY_HTF]

    for sym in symbols:
        htf_bars: list[dict[str, Any]] = []
        if mtf:
            htf_bars = [
                bar
                for bar in load_candles_in_range(conn, sym, REPLAY_HTF, since_ms=0, until_ms=until_ms)
                if bar.get("ema_200") is not None
            ]
        if strategy == "current":
            candles = load_candles_in_range(conn, sym, timeframe, since_ms=since_ms, until_ms=until_ms)
        else:
            # Prior bars fill the 20-bar channel. They are context only.
            candles = load_candles_in_range(conn, sym, timeframe, since_ms=0, until_ms=until_ms)
        if not any(int(bar["timestamp"]) >= int(since_ms) for bar in candles):
            errors.append(f"{sym} {timeframe}: no stored candles in range")
            continue
        by_symbol[sym] = 0
        for index, candle in enumerate(candles):
            if int(candle["timestamp"]) < int(since_ms):
                continue
            bars += 1
            by_symbol[sym] += 1
            if strategy != "current":
                position_side = _open_position_side(conn, sym) if apply_paper and not dry_run else None
                result = decide(strategy, candles[: index + 1], position_side)
            elif mtf:
                htf = closed_htf_candle(htf_bars, int(candle["timestamp"]), htf_ms=htf_ms)
                if htf is not None:
                    htf_joined += 1
                result = analyze_mtf(candle, htf, confidence_threshold=confidence_threshold)
                reason = str(result.get("mtf_reason") or "")
                if reason:
                    mtf_reasons[reason] = mtf_reasons.get(reason, 0) + 1
            else:
                result = analyze_snapshot(candle, confidence_threshold=confidence_threshold)
            action = str(result.get("action") or "").lower()
            if action in action_counts:
                action_counts[action] += 1
            if not dry_run:
                upsert_analysis_output(conn, result)
            analyses.append(result)
            if not (apply_paper and not dry_run):
                continue
            skip_fill = False
            disposition = ""
            if paper_policy == "rules":
                disposition = _rules_disposition(conn, result, exit_mode=exit_mode)
                policy_counts[disposition] = policy_counts.get(disposition, 0) + 1
                skip_fill = disposition in {"hold", "defer", "skip"}
            if not skip_fill:
                applied = apply_signal(
                    conn,
                    analysis=_analysis_for_fill(result, disposition=disposition, exit_mode=exit_mode),
                    mid_price=float(candle["close"]),
                    timeframe=timeframe,
                    now_ms=int(candle["timestamp"]),
                )
                fills_total += len(applied.get("fills") or [])
            if stops != "off":
                fills_total += apply_replay_stop(
                    conn,
                    {**candle, "symbol": sym},
                    mode=stops,
                    no_breakeven=no_breakeven,
                )

    metrics = None
    if apply_paper and not dry_run:
        metrics = compute_paper_metrics(conn)
        metrics = {k: v for k, v in metrics.items() if k != "trades"}

    ok = bars > 0 and not (errors and bars == 0)
    if bars == 0:
        ok = False
    out: dict[str, Any] = {
        "ok": ok,
        "paper_only": True,
        "replay": True,
        "dry_run": dry_run,
        "apply_paper": apply_paper,
        "paper_policy": paper_policy,
        "stops": stops,
        "mtf": bool(mtf),
        "exit": exit_mode,
        "no_breakeven": bool(no_breakeven),
        "strategy": strategy,
        "timeframe": timeframe,
        "since_ms": int(since_ms),
        "until_ms": int(until_ms),
        "bars": bars,
        "analyses": len(analyses),
        "fills": fills_total,
        "by_symbol": by_symbol,
        "errors": errors,
        "metrics": metrics,
        "action_counts": action_counts,
        "sample": analyses[:3],
    }
    if mtf:
        out["mtf_reasons"] = mtf_reasons
        out["htf_joined"] = htf_joined
    if apply_paper and paper_policy == "rules":
        out["policy_counts"] = policy_counts
    return out


def replay_is_oos(
    conn: Any,
    *,
    symbols: list[str],
    timeframe: str,
    since_ms: int,
    oos_since_ms: int,
    until_ms: int,
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
    apply_paper: bool = False,
    dry_run: bool = False,
    paper_policy: PaperPolicy = "naive",
    stops: StopMode = "off",
    mtf: bool = False,
    exit_mode: ExitMode = "confidence",
    no_breakeven: bool = False,
    strategy: StrategyName = "current",
) -> dict[str, Any]:
    """Score one frozen strategy on two chronological windows.

    In-sample is ``since <= timestamp < oos_since``. Out-of-sample is
    ``oos_since <= timestamp <= until``. Each ``--apply-paper`` pass resets the
    ledger, so an in-sample position cannot close in the out-of-sample window.
    Indicators stay the values already stored on the candles.
    """
    if not (int(since_ms) < int(oos_since_ms) < int(until_ms)):
        raise ValueError("oos_since must be strictly after since and strictly before until")
    in_sample = replay_range(
        conn,
        symbols=symbols,
        timeframe=timeframe,
        since_ms=int(since_ms),
        until_ms=int(oos_since_ms) - 1,
        confidence_threshold=confidence_threshold,
        apply_paper=apply_paper,
        dry_run=dry_run,
        paper_policy=paper_policy,
        stops=stops,
        mtf=mtf,
        exit_mode=exit_mode,
        no_breakeven=no_breakeven,
        strategy=strategy,
    )
    out_of_sample = replay_range(
        conn,
        symbols=symbols,
        timeframe=timeframe,
        since_ms=int(oos_since_ms),
        until_ms=int(until_ms),
        confidence_threshold=confidence_threshold,
        apply_paper=apply_paper,
        dry_run=dry_run,
        paper_policy=paper_policy,
        stops=stops,
        mtf=mtf,
        exit_mode=exit_mode,
        no_breakeven=no_breakeven,
        strategy=strategy,
    )
    return {
        "ok": bool(in_sample.get("ok")) and bool(out_of_sample.get("ok")),
        "paper_only": True,
        "replay": True,
        "split": "in_sample_out_of_sample",
        "dry_run": dry_run,
        "apply_paper": apply_paper,
        "paper_policy": paper_policy,
        "stops": stops,
        "mtf": bool(mtf),
        "exit": exit_mode,
        "no_breakeven": bool(no_breakeven),
        "strategy": strategy,
        "timeframe": timeframe,
        "since_ms": int(since_ms),
        "oos_since_ms": int(oos_since_ms),
        "until_ms": int(until_ms),
        "in_sample": in_sample,
        "out_of_sample": out_of_sample,
        "note": FROZEN_STRATEGY_NOTE,
    }


def bar_range_after(
    conn: Any,
    symbol: str,
    timeframe: str,
    *,
    after_ts: int,
    until_ms: int,
) -> tuple[float | None, float | None]:
    """Low/high of ``timeframe`` bars that open after ``after_ts`` up to ``until_ms``.

    Replay fills at the close of the bar that opens at ``after_ts``, so that
    bar's own high and low happened before the fill and are excluded. Other
    timeframes are excluded too: a ``1d`` bar that opens after the fill spans up
    to a day past the bar being replayed.
    """
    row = conn.execute(
        """
        SELECT MIN(low), MAX(high) FROM market_technicals
        WHERE symbol=? AND timeframe=? AND timestamp > ? AND timestamp <= ?
        """,
        (symbol.upper(), timeframe, int(after_ts), int(until_ms)),
    ).fetchone()
    if row is None:
        return None, None
    lo, hi = row[0], row[1]
    return (None if lo is None else float(lo), None if hi is None else float(hi))


def apply_replay_stop(
    conn: Any,
    candle: dict[str, Any],
    *,
    mode: str,
    no_breakeven: bool = False,
) -> int:
    """Close one open paper position if this bar hits its stop. Returns fill count.

    ``fixed`` keeps the entry stop. ``trail`` tightens it by 1R from the best
    price since entry. Each bar is first checked against the stop it started
    with. A stop moved by this bar's extreme can then be hit only by this bar's
    close, because the order of high and low inside one bar is unknown.
    """
    if mode not in {"fixed", "trail"}:
        return 0
    symbol = str(candle.get("symbol") or "").upper()
    if not symbol:
        return 0
    pos = get_paper_position(conn, symbol)
    if pos is None or pos.get("stop_price") is None or pos.get("entry_ts") is None:
        return 0
    ts = int(candle["timestamp"])
    timeframe = str(candle.get("timeframe") or "4h")
    close = float(candle["close"])
    hit = resting_stop_fill(conn, pos, mid=close, until_ms=ts, timeframe=timeframe)
    if hit is None and mode == "trail":
        low, high = bar_range_after(conn, symbol, timeframe, after_ts=int(pos["entry_ts"]), until_ms=ts)
        if no_breakeven:
            new_stop = trail_stop(pos, best_low=low, best_high=high, breakeven_r=None)
        else:
            new_stop = trail_stop(pos, best_low=low, best_high=high)
        if new_stop is not None:
            set_position_stop(
                conn,
                symbol,
                stop_price=new_stop,
                stop_source="trail",
                moved_ts=ts,
            )
            pos = get_paper_position(conn, symbol)
            if pos is None:
                return 0
            # Stop moved on this bar: high/low order unknown; only close can fill,
            # and the fill is at the close when the close is beyond the stop.
            hit = resting_stop_fill(conn, pos, mid=close, until_ms=ts, timeframe=timeframe)
    if hit is None:
        return 0
    prefix = "trail" if str(pos.get("stop_source") or "") == "trail" else "stop"
    applied = apply_signal(
        conn,
        analysis={
            "analysis_id": f"{prefix}-{symbol}-{ts}",
            "symbol": symbol,
            "action": "flat",
            "regime_state": "stop",
            "confidence_score": 0.0,
            "size_pct_equity": 0.0,
        },
        mid_price=hit.fill_mid,
        timeframe=str(candle.get("timeframe") or "4h"),
        now_ms=ts,
        decision_source="stop",
        exit_reason="stop_hit",
    )
    return len(applied.get("fills") or [])


def _open_position_side(conn: Any, symbol: str) -> str | None:
    pos = get_paper_position(conn, symbol)
    if pos is None:
        return None
    side = str(pos.get("side") or "").lower()
    return side or None


def _regime_matches_side(side: str, regime: str) -> bool:
    if side == "long":
        return regime == "trend_up"
    if side == "short":
        return regime == "trend_down"
    return False


def _analysis_for_fill(result: dict[str, Any], *, disposition: str, exit_mode: str) -> dict[str, Any]:
    """Regime flat-exit closes only. An opposite signal must not flip on the same bar."""
    action = str(result.get("action") or "").lower()
    if exit_mode == "regime" and disposition == "flat_exit" and action != "flat":
        return {**result, "action": "flat", "size_pct_equity": 0.0, "invalidation_price": None}
    return result


def _rules_disposition(conn: Any, result: dict[str, Any], *, exit_mode: ExitMode = "confidence") -> str:
    """Return hold / defer / flat_exit / fill_bars / skip. Never mutates the ledger.

    ``regime`` closes an open position when ``regime_state`` no longer matches
    the position side, including when the bar's action is the opposite side.
    That close does not flip. A matching regime keeps same-side hold, flat skip,
    and opposite defer. ``confidence`` is unchanged.
    """
    action = str(result.get("action") or "").lower()
    pos = get_paper_position(conn, str(result.get("symbol") or ""))
    if exit_mode == "regime" and pos is not None:
        side = str(pos.get("side") or "").lower()
        regime = str(result.get("regime_state") or "")
        if not _regime_matches_side(side, regime):
            return "flat_exit"
        if same_side_already_open(conn, result):
            return "hold"
        if action == "flat":
            return "skip"
        if action in {"long", "short"} and side != action:
            return "defer"
    if same_side_already_open(conn, result):
        return "hold"
    if pos is not None and action in {"long", "short"}:
        pos_side = str(pos.get("side") or "").lower()
        if pos_side and pos_side != action:
            return "defer"
    if action == "flat":
        if flat_exit_needed(conn, result):
            return "flat_exit"
        return "skip"
    if action in {"long", "short"}:
        return "fill_bars"
    return "skip"
