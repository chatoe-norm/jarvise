"""T2.1 decision feedback: source tags, outcome sync, auto EV soft gate."""

from __future__ import annotations

import json
import os
import statistics
import time
from collections.abc import Mapping
from typing import Any

from jarvise_ingest.db import (
    insert_paper_auto_run,
    list_paper_decision_outcomes,
    upsert_paper_decision_outcome,
)

AUTO_SOURCES = frozenset({"auto_claude", "auto_rule"})
WINDOW_30D_MS = 30 * 86_400_000
DEFAULT_MIN_EV_N = 10
OWNER_REASON_MIN = 10
OWNER_DECISION_KINDS = frozenset({"ev_gate_reset", "kill_switch_off", "kill_switch_on"})


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
        return_on_notional = round(pnl / notional, 8) if notional > 0 else None
        stop_raw = open_o.get("initial_stop_price")
        r_mult = None
        if stop_raw is not None:
            risk_usd = abs(entry - float(stop_raw)) * abs(qty)
            if risk_usd > 0:
                r_mult = round(pnl / risk_usd, 8)
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
                "return_on_notional": return_on_notional,
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


def read_ev_gate_latch(conn: Any) -> dict[str, Any] | None:
    """Return the active latch, or None when the owner has not blocked the gate yet."""
    row = conn.execute(
        "SELECT latched, latched_at_ms, reason FROM paper_ev_gate_latch WHERE id = 1"
    ).fetchone()
    if row is None or not int(row[0]):
        return None
    return {
        "latched": True,
        "latched_at_ms": int(row[1]) if row[1] is not None else None,
        "reason": row[2],
    }


def latch_ev_gate(
    conn: Any,
    *,
    at_ms: int,
    reason: str,
    n: int,
    ev: float | None,
) -> None:
    """Persist the first block. A later call does not move the timestamp."""
    if read_ev_gate_latch(conn) is not None:
        return
    conn.execute(
        """
        INSERT INTO paper_ev_gate_latch (
            id, latched, latched_at_ms, reason, sample_n, sample_ev, cleared_at_ms
        )
        VALUES (1, 1, ?, ?, ?, ?, NULL)
        ON CONFLICT(id) DO UPDATE SET
            latched = 1,
            latched_at_ms = excluded.latched_at_ms,
            reason = excluded.reason,
            sample_n = excluded.sample_n,
            sample_ev = excluded.sample_ev,
            cleared_at_ms = NULL
        WHERE paper_ev_gate_latch.latched = 0
        """,
        (int(at_ms), reason, int(n), ev),
    )
    conn.commit()


def require_owner_reason(reason: str | None) -> str:
    """Strip and require a written review of at least OWNER_REASON_MIN characters."""
    text = (reason or "").strip()
    if len(text) < OWNER_REASON_MIN:
        raise ValueError("reason must be at least 10 characters")
    return text


def optional_owner_reason(reason: str | None) -> str | None:
    """Keep a short or empty engage note. Never raises."""
    text = (reason or "").strip()
    return text or None


def record_owner_decision(
    conn: Any,
    *,
    at_ms: int,
    kind: str,
    reason: str | None,
    actor: str,
    payload: Mapping[str, Any] | None = None,
) -> None:
    """Insert one owner-review row. The caller commits."""
    if kind not in OWNER_DECISION_KINDS:
        raise ValueError(f"unknown owner decision kind: {kind}")
    conn.execute(
        """
        INSERT INTO owner_decision_log (at_ms, kind, reason, actor, payload_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            int(at_ms),
            kind,
            reason,
            actor,
            json.dumps(payload or {}, separators=(",", ":"), default=str),
        ),
    )


def list_owner_decisions(conn: Any, *, limit: int = 5) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, at_ms, kind, reason, actor
        FROM owner_decision_log
        ORDER BY at_ms DESC, id DESC
        LIMIT ?
        """,
        (max(1, int(limit)),),
    ).fetchall()
    return [
        {
            "id": int(row[0]),
            "at_ms": int(row[1]),
            "kind": row[2],
            "reason": row[3],
            "actor": row[4],
        }
        for row in rows
    ]


def reset_ev_gate_latch(
    conn: Any,
    *,
    reason: str,
    at_ms: int | None = None,
    actor: str = "owner",
) -> dict[str, Any]:
    """Clear the latch after a written reason. The latch rule itself is unchanged."""
    text = require_owner_reason(reason)
    ts = int(at_ms if at_ms is not None else time.time() * 1000)
    row = conn.execute("SELECT sample_n, sample_ev FROM paper_ev_gate_latch WHERE id = 1").fetchone()
    sample_n = None if row is None or row[0] is None else int(row[0])
    sample_ev = None if row is None or row[1] is None else float(row[1])
    conn.execute(
        """
        INSERT INTO paper_ev_gate_latch (id, latched, cleared_at_ms)
        VALUES (1, 0, ?)
        ON CONFLICT(id) DO UPDATE SET
            latched = 0,
            cleared_at_ms = excluded.cleared_at_ms
        """,
        (ts,),
    )
    record_owner_decision(
        conn,
        at_ms=ts,
        kind="ev_gate_reset",
        reason=text,
        actor=actor,
        payload={"sample_n": sample_n, "sample_ev": sample_ev},
    )
    conn.commit()
    return {"ok": True, "latched": False, "cleared_at_ms": ts}


def auto_ev_gate_status(
    conn: Any,
    *,
    now_ms: int | None = None,
    min_ev: float | None = None,
    min_n: int | None = None,
) -> dict[str, Any]:
    """Block auto-decide when 30-day auto EV is below min_ev and the sample is large enough.

    The first block writes a SQLite latch with a timestamp and reason. A later sample
    with fewer than min_n trades does not release that latch, and a thin sample does
    not block before the first latch. The owner clears it with the CLI or the Ops button.
    """
    cfg_ev, cfg_n = load_auto_ev_gate_config()
    threshold = cfg_ev if min_ev is None else min_ev
    need_n = cfg_n if min_n is None else max(1, int(min_n))
    ts = int(now_ms if now_ms is not None else time.time() * 1000)
    base: dict[str, Any] = {
        "enabled": threshold is not None,
        "blocked": False,
        "latched": False,
        "latched_at_ms": None,
        "latch_reason": None,
        "min_ev": threshold,
        "min_n": need_n,
        "n": 0,
        "ev": None,
        "window_ms": WINDOW_30D_MS,
        "reason": None,
    }
    latch = read_ev_gate_latch(conn)
    if threshold is None and latch is None:
        return base
    if threshold is not None:
        by_src = metrics_by_decision_source(conn, since_ms=ts - WINDOW_30D_MS)
        auto = by_src.get("auto") or _bucket_stats([])
        n = int(auto["closed_trades"])
        ev = auto["expected_value_ev"]
        base["n"] = n
        base["ev"] = ev
        sample_blocks = n >= need_n and ev is not None and float(ev) < float(threshold)
        if sample_blocks:
            latch_ev_gate(conn, at_ms=ts, reason="auto_ev_gate", n=n, ev=float(ev))
            latch = read_ev_gate_latch(conn)
    if latch is not None:
        base["blocked"] = True
        base["latched"] = True
        base["enabled"] = True
        base["latched_at_ms"] = latch["latched_at_ms"]
        base["latch_reason"] = latch["reason"]
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


def get_outcome_memory(
    conn: Any,
    *,
    symbol: str | None = None,
    limit: int = 5,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Retrieve structured memory of recent closed trades, winrate, and loss lessons."""
    ts = int(now_ms if now_ms is not None else time.time() * 1000)
    all_outcomes = list_paper_decision_outcomes(conn, limit=50)

    # 30-day stats
    by_src = metrics_by_decision_source(conn, since_ms=ts - WINDOW_30D_MS)
    auto_stats = by_src.get("auto_claude") or _bucket_stats([])

    # Symbol-specific stats
    sym_rows = (
        [r for r in all_outcomes if str(r.get("symbol") or "").upper() == str(symbol or "").upper()]
        if symbol
        else []
    )
    sym_pnls = [float(r["pnl_usd"]) for r in sym_rows]
    sym_stats = _bucket_stats(sym_pnls) if sym_pnls else None

    # Recent closed trades
    recent_trades = []
    for r in all_outcomes[:limit]:
        pnl = float(r.get("pnl_usd") or 0.0)
        is_win = pnl > 0
        hold_hrs = round(float(r.get("hold_ms") or 0.0) / 3_600_000.0, 1)
        r_raw = r.get("r_multiple")
        ron_raw = r.get("return_on_notional")
        recent_trades.append(
            {
                "symbol": str(r.get("symbol") or ""),
                "side": str(r.get("side") or ""),
                "pnl_usd": round(pnl, 2),
                "r_multiple": round(float(r_raw), 4) if r_raw is not None else "N/A",
                "return_on_notional": round(float(ron_raw), 4) if ron_raw is not None else "N/A",
                "hold_hours": hold_hrs,
                "decision_source": str(r.get("decision_source") or ""),
                "result": "win" if is_win else "loss",
            }
        )

    # Synthesize loss lesson bullets
    lessons: list[str] = []
    if auto_stats.get("closed_trades", 0) > 0:
        wr = auto_stats.get("win_rate") or 0.0
        ev = auto_stats.get("expected_value_ev") or 0.0
        lessons.append(
            f"30d auto winrate: {wr:.1%} ({auto_stats.get('wins', 0)}W, {auto_stats.get('losses', 0)}L), EV: ${ev:.2f}"
        )
    if recent_trades:
        loss_sides = [t["side"] for t in recent_trades if t["result"] == "loss"]
        win_sides = [t["side"] for t in recent_trades if t["result"] == "win"]
        if "long" in loss_sides and "short" in win_sides:
            lessons.append(
                "Recent winning trades were shorts; recent long attempts experienced adverse reversals."
            )
        elif len(loss_sides) > len(win_sides):
            lessons.append(
                f"Caution: {len(loss_sides)} of last {len(recent_trades)} closed trades were losses."
            )

    return {
        "auto_30d": {
            "closed_trades": auto_stats.get("closed_trades", 0),
            "wins": auto_stats.get("wins", 0),
            "losses": auto_stats.get("losses", 0),
            "win_rate": auto_stats.get("win_rate"),
            "expected_value_ev": auto_stats.get("expected_value_ev"),
        },
        "symbol_stats": sym_stats,
        "recent_closed_trades": recent_trades,
        "loss_lessons": lessons,
    }
