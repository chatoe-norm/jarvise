"""Historical analyze replay — paper only. No exchange orders."""

from __future__ import annotations

from typing import Any, Literal

from jarvise_analyze.engine import CONFIDENCE_THRESHOLD, analyze_snapshot
from jarvise_ingest.db import (
    get_paper_position,
    load_candles_in_range,
    reset_paper_ledger,
    upsert_analysis_output,
)
from jarvise_paper.auto_decide import flat_exit_needed, same_side_already_open
from jarvise_paper.engine import apply_signal
from jarvise_paper.metrics import compute_paper_metrics

PaperPolicy = Literal["naive", "rules"]
PAPER_POLICIES: tuple[str, ...] = ("naive", "rules")


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
) -> dict[str, Any]:
    """Walk closed candles in range; upsert analysis; optionally paper-fill.

    ``paper_policy="rules"`` matches auto-decide *code* gates (no Claude): same-side
    hold, opposite-side defer (does not flip), FLAT closes an open position.
    Does not change ``apply_signal`` behavior for the live paper path.
    """
    if paper_policy not in PAPER_POLICIES:
        raise ValueError(f"paper_policy must be one of {PAPER_POLICIES}")
    if apply_paper and not dry_run:
        reset_paper_ledger(conn)

    analyses: list[dict[str, Any]] = []
    fills_total = 0
    bars = 0
    errors: list[str] = []
    by_symbol: dict[str, int] = {}
    policy_counts = {"hold": 0, "defer": 0, "fill_bars": 0, "flat_exit": 0, "skip": 0}

    for sym in symbols:
        candles = load_candles_in_range(conn, sym, timeframe, since_ms=since_ms, until_ms=until_ms)
        if not candles:
            errors.append(f"{sym} {timeframe}: no stored candles in range")
            continue
        by_symbol[sym] = 0
        for candle in candles:
            bars += 1
            by_symbol[sym] += 1
            result = analyze_snapshot(candle, confidence_threshold=confidence_threshold)
            if not dry_run:
                upsert_analysis_output(conn, result)
            analyses.append(result)
            if not (apply_paper and not dry_run):
                continue
            if paper_policy == "rules":
                disposition = _rules_disposition(conn, result)
                policy_counts[disposition] = policy_counts.get(disposition, 0) + 1
                if disposition in {"hold", "defer", "skip"}:
                    continue
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
    if apply_paper and paper_policy == "rules":
        out["policy_counts"] = policy_counts
    return out


def _rules_disposition(conn: Any, result: dict[str, Any]) -> str:
    """Return hold / defer / flat_exit / fill_bars / skip. Never mutates the ledger."""
    action = str(result.get("action") or "").lower()
    if same_side_already_open(conn, result):
        return "hold"
    pos = get_paper_position(conn, str(result.get("symbol") or ""))
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
