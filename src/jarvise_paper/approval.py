"""Paper approval queue — simulated fills only. No exchange order APIs."""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any

from jarvise_ingest.db import (
    claim_approval_for_fill,
    expire_pending_approvals,
    get_approval,
    load_latest_candle,
    mark_approval_failed,
    resolve_approval,
    set_approval_paper_order_ids,
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


def _claim_failure(conn: Any, approval_id: str, *, ts: int) -> dict[str, Any]:
    row = get_approval(conn, approval_id)
    if row is None:
        error = "approval not found"
    elif row["status"] != "pending":
        error = "approval not pending"
    elif int(row["expires_at_ms"]) <= ts:
        updated = resolve_approval(
            conn,
            approval_id,
            status="timed_out",
            resolved_at_ms=ts,
        )
        row = updated or row
        error = "approval expired"
    else:
        error = "approval not pending"
    return {
        "ok": False,
        "approval": row,
        "fills": [],
        "error": error,
        "paper_only": True,
    }


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
    if int(row["expires_at_ms"]) <= ts:
        updated = resolve_approval(
            conn,
            approval_id,
            status="timed_out",
            resolved_at_ms=ts,
        )
        return {
            "ok": False,
            "approval": updated or get_approval(conn, approval_id),
            "fills": [],
            "error": "approval expired",
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
    claimed = claim_approval_for_fill(conn, approval_id, now_ms=ts)
    if claimed is None:
        return _claim_failure(conn, approval_id, ts=ts)
    analysis = {
        "analysis_id": claimed.get("analysis_id"),
        "symbol": claimed["symbol"],
        "action": claimed["action"],
        "regime_state": claimed.get("regime_state"),
        "confidence_score": claimed.get("confidence_score"),
        "size_pct_equity": claimed.get("size_pct_equity"),
    }
    try:
        applied = apply_signal(
            conn,
            analysis=analysis,
            mid_price=float(candle["close"]),
            timeframe=str(claimed["timeframe"]),
            now_ms=ts,
        )
    except Exception as exc:  # noqa: BLE001
        failed = mark_approval_failed(
            conn,
            approval_id,
            resolve_reason=str(exc),
            resolved_at_ms=ts,
        )
        return {
            "ok": False,
            "approval": failed or get_approval(conn, approval_id),
            "fills": [],
            "error": str(exc),
            "paper_only": True,
        }
    order_ids = [f.get("order_id") for f in applied.get("fills") or [] if f.get("order_id")]
    updated = set_approval_paper_order_ids(
        conn,
        approval_id,
        json.dumps(order_ids) if order_ids else None,
    )
    return {
        "ok": True,
        "approval": updated or claimed,
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
