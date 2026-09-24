"""Paper approval queue — simulated fills only. No exchange order APIs."""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any

from jarvise_ingest.db import (
    expire_pending_approvals,
    get_approval,
    load_latest_candle,
    resolve_approval,
    upsert_pending_approval,
)
from jarvise_paper.engine import apply_signal

DEFAULT_TIMEOUT_MIN = 60


def approval_timeout_ms() -> int:
    raw = os.environ.get("JARVISE_APPROVAL_TIMEOUT_MIN", str(DEFAULT_TIMEOUT_MIN))
    try:
        minutes = max(1, int(raw))
    except ValueError:
        minutes = DEFAULT_TIMEOUT_MIN
    return minutes * 60_000


def _new_id(symbol: str, timeframe: str, analysis_id: str | None, now_ms: int) -> str:
    material = f"{symbol}|{timeframe}|{analysis_id}|{now_ms}"
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def enqueue_approval(
    conn: Any,
    *,
    analysis: dict[str, Any],
    timeframe: str,
    now_ms: int | None = None,
) -> dict[str, Any]:
    ts = int(now_ms if now_ms is not None else time.time() * 1000)
    symbol = str(analysis["symbol"]).upper()
    return upsert_pending_approval(
        conn,
        {
            "id": _new_id(symbol, timeframe, analysis.get("analysis_id"), ts),
            "created_at_ms": ts,
            "expires_at_ms": ts + approval_timeout_ms(),
            "symbol": symbol,
            "timeframe": timeframe,
            "analysis_id": analysis.get("analysis_id"),
            "action": str(analysis.get("action") or "flat"),
            "regime_state": analysis.get("regime_state"),
            "confidence_score": analysis.get("confidence_score"),
            "size_pct_equity": analysis.get("size_pct_equity"),
            "status": "pending",
        },
    )


def approve_approval(
    conn: Any,
    approval_id: str,
    *,
    kill_switch: bool,
    now_ms: int | None = None,
) -> dict[str, Any]:
    ts = int(now_ms if now_ms is not None else time.time() * 1000)
    row = get_approval(conn, approval_id)
    if row is None or row["status"] != "pending":
        return {
            "ok": False,
            "approval": row,
            "fills": [],
            "error": "approval not pending",
            "paper_only": True,
        }
    if kill_switch:
        return {
            "ok": False,
            "approval": row,
            "fills": [],
            "error": "kill_switch engaged",
            "paper_only": True,
        }
    candle = load_latest_candle(conn, row["symbol"], row["timeframe"])
    if candle is None:
        updated = resolve_approval(
            conn,
            approval_id,
            status="failed",
            resolve_reason="no stored candles",
            resolved_at_ms=ts,
        )
        return {
            "ok": False,
            "approval": updated,
            "fills": [],
            "error": "no stored candles",
            "paper_only": True,
        }
    analysis = {
        "analysis_id": row.get("analysis_id"),
        "symbol": row["symbol"],
        "action": row["action"],
        "regime_state": row.get("regime_state"),
        "confidence_score": row.get("confidence_score"),
        "size_pct_equity": row.get("size_pct_equity"),
    }
    applied = apply_signal(
        conn,
        analysis=analysis,
        mid_price=float(candle["close"]),
        timeframe=str(row["timeframe"]),
        now_ms=ts,
    )
    order_ids = [f.get("order_id") for f in applied.get("fills") or [] if f.get("order_id")]
    updated = resolve_approval(
        conn,
        approval_id,
        status="approved",
        resolved_at_ms=ts,
        paper_order_ids_json=json.dumps(order_ids) if order_ids else None,
    )
    return {
        "ok": True,
        "approval": updated,
        "fills": applied.get("fills") or [],
        "error": None,
        "paper_only": True,
        "equity": applied.get("equity"),
    }


def reject_approval(
    conn: Any,
    approval_id: str,
    *,
    reason: str | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    ts = int(now_ms if now_ms is not None else time.time() * 1000)
    updated = resolve_approval(
        conn,
        approval_id,
        status="rejected",
        resolve_reason=reason,
        resolved_at_ms=ts,
    )
    return {
        "ok": updated is not None,
        "approval": updated,
        "error": None if updated else "approval not pending",
        "paper_only": True,
    }


def expire_approvals(conn: Any, *, now_ms: int | None = None) -> dict[str, Any]:
    ts = int(now_ms if now_ms is not None else time.time() * 1000)
    n = expire_pending_approvals(conn, now_ms=ts)
    return {"ok": True, "expired": n, "paper_only": True}
