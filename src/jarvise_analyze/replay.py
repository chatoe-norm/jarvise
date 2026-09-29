"""Historical analyze replay — paper only. No exchange orders."""

from __future__ import annotations

from typing import Any

from jarvise_analyze.engine import CONFIDENCE_THRESHOLD, analyze_snapshot
from jarvise_ingest.db import (
    load_candles_in_range,
    reset_paper_ledger,
    upsert_analysis_output,
)
from jarvise_paper.engine import apply_signal
from jarvise_paper.metrics import compute_paper_metrics


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
) -> dict[str, Any]:
    """Walk closed candles in range; upsert analysis; optionally paper-fill."""
    if apply_paper and not dry_run:
        reset_paper_ledger(conn)

    analyses: list[dict[str, Any]] = []
    fills_total = 0
    bars = 0
    errors: list[str] = []
    by_symbol: dict[str, int] = {}

    for sym in symbols:
        candles = load_candles_in_range(
            conn, sym, timeframe, since_ms=since_ms, until_ms=until_ms
        )
        if not candles:
            errors.append(f"{sym} {timeframe}: no stored candles in range")
            continue
        by_symbol[sym] = 0
        for candle in candles:
            bars += 1
            by_symbol[sym] += 1
            result = analyze_snapshot(
                candle, confidence_threshold=confidence_threshold
            )
            if not dry_run:
                upsert_analysis_output(conn, result)
            analyses.append(result)
            if apply_paper and not dry_run:
                applied = apply_signal(
                    conn,
                    analysis=result,
                    mid_price=float(candle["close"]),
                    timeframe=timeframe,
                    now_ms=int(candle["timestamp"]),
                )
                fills_total += len(applied.get("fills") or [])

    metrics = None
    if apply_paper and not dry_run:
        metrics = compute_paper_metrics(conn)
        # Drop bulky trade list from embedded summary unless caller wants it
        metrics = {k: v for k, v in metrics.items() if k != "trades"}

    ok = bars > 0 and not (errors and bars == 0)
    if bars == 0:
        ok = False
    return {
        "ok": ok,
        "paper_only": True,
        "replay": True,
        "dry_run": dry_run,
        "apply_paper": apply_paper,
        "timeframe": timeframe,
        "since_ms": int(since_ms),
        "until_ms": int(until_ms),
        "bars": bars,
        "analyses": len(analyses),
        "fills": fills_total,
        "by_symbol": by_symbol,
        "errors": errors,
        "metrics": metrics,
        "sample": analyses[:3],
    }
