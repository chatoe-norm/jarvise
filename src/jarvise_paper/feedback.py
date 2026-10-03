"""T2.1 decision feedback: source tags, outcome sync, auto EV soft gate."""

from __future__ import annotations

import json
import os
import statistics
import time
from typing import Any

from jarvise_ingest.db import (
    insert_paper_auto_run,
    list_paper_decision_outcomes,
    upsert_paper_decision_outcome,
)

AUTO_SOURCES = frozenset({"auto_claude", "auto_rule"})
WINDOW_30D_MS = 30 * 86_400_000
DEFAULT_MIN_EV_N = 10


def decision_source_from_reason(reason: str | None) -> str:
    """Map approval resolve_reason / claim reason to a stable source tag."""
    text = str(reason or "").strip().lower()
    if text.startswith("auto:rule:"):
        return "auto_rule"
    if text.startswith("auto:claude:") or text.startswith("auto:apply_failed:"):
        return "auto_claude"
    if text.startswith("auto:"):
        return "auto_claude"
    if not text or text in {"paper_fill", "manual", "approved"}:
        return "manual"
    return "manual"


def sync_decision_outcomes(
    conn: Any,
    trades: list[dict[str, Any]],
    orders_by_id: dict[str, dict[str, Any]],
    *,
    prompt_version: str | None = None,
) -> int:
    """Upsert outcome rows from reconstructed closed trades. Returns rows written."""
    written = 0
    for trade in trades:
        open_id = str(trade["open_order_id"])
        close_id = str(trade["close_order_id"])
        open_o = orders_by_id.get(open_id) or {}
        source = str(open_o.get("decision_source") or "").strip()
        if not source:
            source = "unknown"
        hold_ms = int(trade["close_ts"]) - int(trade["open_ts"])
        entry = float(trade["entry_price"])
        qty = float(trade["qty"])
        notional = abs(entry * qty)
        pnl = float(trade["pnl_usd"])
        r_mult = round(pnl / notional, 8) if notional > 0 else None
        upsert_paper_decision_outcome(
            conn,
            {
                "approval_id": open_o.get("approval_id"),
                "open_order_id": open_id,
                "close_order_id": close_id,
                "symbol": trade["symbol"],
                "side": trade["side"],
                "pnl_usd": pnl,
                "r_multiple": r_mult,
                "hold_ms": hold_ms,
                "decision_source": source,
                "prompt_version": prompt_version,
                "closed_at_ms": int(trade["close_ts"]),
            },
        )
        written += 1
    if written:
        conn.commit()
    return written


def _bucket_stats(pnls: list[float]) -> dict[str, Any]:
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    scratches = [p for p in pnls if p == 0]
    decided = len(wins) + len(losses)
    return {
        "closed_trades": len(pnls),
        "wins": len(wins),
        "losses": len(losses),
        "scratches": len(scratches),
        "expected_value_ev": round(statistics.mean(pnls), 8) if pnls else None,
        "win_rate": round(len(wins) / decided, 6) if decided else None,
    }


def metrics_by_decision_source(
    conn: Any,
    *,
    since_ms: int | None = None,
) -> dict[str, dict[str, Any]]:
    rows = list_paper_decision_outcomes(conn, since_ms=since_ms, limit=5_000)
    buckets: dict[str, list[float]] = {}
    for row in rows:
        src = str(row.get("decision_source") or "unknown")
        buckets.setdefault(src, []).append(float(row["pnl_usd"]))
    # Also aggregate all auto_* under "auto" for soft-gate consumers.
    auto_pnls = [p for src, pnls in buckets.items() if src in AUTO_SOURCES for p in pnls]
    out = {src: _bucket_stats(pnls) for src, pnls in sorted(buckets.items())}
    if auto_pnls:
        out["auto"] = _bucket_stats(auto_pnls)
    return out


def load_auto_ev_gate_config() -> tuple[float | None, int]:
    """Return (min_ev or None if disabled, min_n)."""
    raw = (os.environ.get("JARVISE_AUTO_DECIDE_MIN_EV") or "").strip()
    min_n = DEFAULT_MIN_EV_N
    try:
        min_n = max(1, int(os.environ.get("JARVISE_AUTO_DECIDE_MIN_EV_N") or DEFAULT_MIN_EV_N))
    except ValueError:
        min_n = DEFAULT_MIN_EV_N
    if raw == "":
        return None, min_n
    try:
        return float(raw), min_n
    except ValueError:
        return None, min_n


def auto_ev_gate_status(
    conn: Any,
    *,
    now_ms: int | None = None,
    min_ev: float | None = None,
    min_n: int | None = None,
) -> dict[str, Any]:
    """Fail-closed soft gate: block auto-decide when 30d auto EV is below min_ev."""
    cfg_ev, cfg_n = load_auto_ev_gate_config()
    threshold = cfg_ev if min_ev is None else min_ev
    need_n = cfg_n if min_n is None else max(1, int(min_n))
    ts = int(now_ms if now_ms is not None else time.time() * 1000)
    base: dict[str, Any] = {
        "enabled": threshold is not None,
        "blocked": False,
        "min_ev": threshold,
        "min_n": need_n,
        "n": 0,
        "ev": None,
        "window_ms": WINDOW_30D_MS,
        "reason": None,
    }
    if threshold is None:
        return base
    by_src = metrics_by_decision_source(conn, since_ms=ts - WINDOW_30D_MS)
    auto = by_src.get("auto") or _bucket_stats([])
    n = int(auto["closed_trades"])
    ev = auto["expected_value_ev"]
    base["n"] = n
    base["ev"] = ev
    if n < need_n:
        return base
    if ev is not None and float(ev) < float(threshold):
        base["blocked"] = True
        base["reason"] = "auto_ev_gate"
    return base


def persist_auto_run(conn: Any, payload: dict[str, Any]) -> None:
    """Best-effort durable summary of one auto-decide run."""
    try:
        slim = {
            k: payload.get(k)
            for k in (
                "ok",
                "skipped",
                "reason",
                "halted",
                "doctrine_unavailable",
                "approved",
                "rejected",
                "deferred",
                "filtered_out",
                "apply_failed",
            )
            if k in payload
        }
        insert_paper_auto_run(
            conn,
            {
                "at_ms": int(payload.get("at_ms") or time.time() * 1000),
                "model": payload.get("model"),
                "prompt_version": payload.get("prompt_version"),
                "ok": bool(payload.get("ok")),
                "skipped": bool(payload.get("skipped")),
                "reason": payload.get("reason") or payload.get("halted"),
                "processed": payload.get("processed"),
                "approved_n": len(payload.get("approved") or []),
                "rejected_n": len(payload.get("rejected") or []),
                "deferred_n": len(payload.get("deferred") or []),
                "apply_failed_n": len(payload.get("apply_failed") or []),
                "duration_s": payload.get("duration_s"),
                "payload_json": json.dumps(slim, ensure_ascii=False, default=str)[:8_000],
            },
        )
    except Exception:  # noqa: BLE001 — never fail the job on audit persistence
        pass
