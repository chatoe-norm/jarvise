"""Paper approval queue — simulated fills only. No exchange order APIs."""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any

from jarvise_ingest.db import (
    claim_approval_for_fill,
    ensure_paper_account,
    expire_pending_approvals,
    get_approval,
    get_paper_account,
    get_paper_position,
    list_expired_pending_approvals,
    load_latest_candle,
    mark_approval_failed,
    resolve_approval,
    set_approval_paper_order_ids,
    upsert_pending_approval,
)
from jarvise_paper.engine import apply_signal
from jarvise_risk import check_caps, engage_kill_switch, load_risk_caps

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


def _account_snapshot(conn: Any) -> dict[str, float]:
    ensure_paper_account(conn)
    return get_paper_account(conn)


def _risk_breach(
    conn: Any,
    *,
    action: str | None,
    size_pct_equity: float | None,
) -> str | None:
    caps = load_risk_caps()
    acct = _account_snapshot(conn)
    return check_caps(
        caps,
        equity=float(acct["equity"]),
        starting_equity=float(acct["starting_equity"]),
        size_pct_equity=size_pct_equity,
        action=action,
    )


def _fail_risk(
    conn: Any,
    approval_id: str,
    *,
    reason: str,
    ts: int,
    row: dict | None = None,
) -> dict[str, Any]:
    engage_kill_switch(reason=reason)
    failed = mark_approval_failed(
        conn,
        approval_id,
        resolve_reason=reason,
        resolved_at_ms=ts,
    )
    return {
        "ok": False,
        "approval": failed or row or get_approval(conn, approval_id),
        "fills": [],
        "error": reason,
        "paper_only": True,
        "kill_switch_engaged": True,
    }


def enqueue_approval(
    conn: Any,
    *,
    analysis: dict[str, Any],
    timeframe: str,
    now_ms: int | None = None,
) -> dict[str, Any]:
    ts = int(now_ms if now_ms is not None else time.time() * 1000)
    symbol = str(analysis["symbol"]).upper()
    action = str(analysis.get("action") or "flat")
    size = analysis.get("size_pct_equity")
    breach = _risk_breach(
        conn,
        action=action,
        size_pct_equity=float(size) if size is not None else None,
    )
    if breach:
        engage_kill_switch(reason=breach)
        return {
            "ok": False,
            "skipped": True,
            "error": breach,
            "kill_switch_engaged": True,
            "paper_only": True,
            "symbol": symbol,
            "timeframe": timeframe,
        }
    return upsert_pending_approval(
        conn,
        {
            "id": _new_id(symbol, timeframe, analysis.get("analysis_id"), ts),
            "created_at_ms": ts,
            "expires_at_ms": ts + approval_timeout_ms(),
            "symbol": symbol,
            "timeframe": timeframe,
            "analysis_id": analysis.get("analysis_id"),
            "action": action,
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

    size = row.get("size_pct_equity")
    breach = _risk_breach(
        conn,
        action=str(row.get("action") or "flat"),
        size_pct_equity=float(size) if size is not None else None,
    )
    if breach:
        return _fail_risk(conn, approval_id, reason=breach, ts=ts, row=row)

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
    """Mark timed-out pendings and apply paper FLAT (roadmap: timeout → FLAT)."""
    ts = int(now_ms if now_ms is not None else time.time() * 1000)
    expired_rows = list_expired_pending_approvals(conn, now_ms=ts)
    flat_fills = 0
    flat_errors: list[str] = []
    for row in expired_rows:
        symbol = str(row["symbol"]).upper()
        timeframe = str(row["timeframe"])
        pos = get_paper_position(conn, symbol)
        if pos is not None:
            candle = load_latest_candle(conn, symbol, timeframe)
            if candle is None:
                flat_errors.append(f"{symbol}: no candle for timeout FLAT")
            else:
                try:
                    applied = apply_signal(
                        conn,
                        analysis={
                            "analysis_id": row.get("analysis_id") or f"timeout-{row['id']}",
                            "symbol": symbol,
                            "action": "flat",
                            "regime_state": row.get("regime_state") or "range",
                            "confidence_score": 0.0,
                            "size_pct_equity": 0.0,
                        },
                        mid_price=float(candle["close"]),
                        timeframe=timeframe,
                        now_ms=ts,
                    )
                    flat_fills += len(applied.get("fills") or [])
                except Exception as exc:  # noqa: BLE001
                    flat_errors.append(f"{symbol}: {exc}")
        resolve_approval(
            conn,
            row["id"],
            status="timed_out",
            resolve_reason="timeout_flat",
            resolved_at_ms=ts,
        )
    # Safety net for any race
    n = expire_pending_approvals(conn, now_ms=ts)
    expired_count = max(len(expired_rows), n)
    return {
        "ok": True,
        "expired": expired_count,
        "flat_fills": flat_fills,
        "flat_errors": flat_errors,
        "paper_only": True,
        "note": "timeout → FLAT on open paper positions",
    }
