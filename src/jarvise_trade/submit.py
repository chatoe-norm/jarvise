"""Approve → live spot MARKET submit + live_orders audit."""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any

import httpx

from jarvise_exchange.permissions import audit_key_permissions, fetch_api_restrictions
from jarvise_ingest.db import (
    get_live_order_by_client_id,
    get_paper_account,
    insert_live_order,
    sum_live_realized_pnl_utc_day,
)
from jarvise_risk import check_caps, estimated_notional, load_risk_caps
from jarvise_trade.auth import resolve_trade_auth
from jarvise_trade.binance_market import (
    client_order_id_for_approval,
    place_spot_market_order,
    query_order,
    summarize_fill,
)

# Statuses that mean "the venue already has this order" — never POST again.
_VENUE_HAS_ORDER_STATUSES = frozenset({"submitted", "partially_filled", "filled"})


def _live_equity_usd(conn: Any) -> float:
    raw = (os.environ.get("JARVISE_LIVE_EQUITY_USD") or "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    acct = get_paper_account(conn)
    return float(acct.get("equity") or 0.0)


def _utc_day_bounds_ms(now_ms: int) -> tuple[int, int]:
    day_ms = 86_400_000
    start = (int(now_ms) // day_ms) * day_ms
    return start, start + day_ms


def live_day_loss_breach(conn: Any, *, now_ms: int) -> str | None:
    """Block if realized live PnL for UTC day is at/under -max_daily_loss."""
    caps = load_risk_caps()
    start, end = _utc_day_bounds_ms(now_ms)
    pnl = sum_live_realized_pnl_utc_day(conn, day_start_ms=start, day_end_ms=end)
    if pnl <= -caps.max_daily_loss_usd:
        return (
            f"live_max_daily_loss: day_pnl ${pnl:.2f} <= "
            f"-${caps.max_daily_loss_usd:.2f}"
        )
    return None


def _order_id(approval_id: str, now_ms: int) -> str:
    material = f"live|{approval_id}|{now_ms}"
    return hashlib.sha256(material.encode()).hexdigest()[:20]


def _side_for_action(action: str) -> str | None:
    act = (action or "").lower()
    if act == "long":
        return "BUY"
    if act == "flat":
        return None  # no HTTP unless inventory path later
    if act == "short":
        raise ValueError("spot live submit does not support short opens")
    return None


def submit_live_for_approval(
    conn: Any,
    *,
    approval: dict[str, Any],
    now_ms: int | None = None,
    http_client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Place one size-capped MARKET order (or skip flat/zero). Writes live_orders.

    Caller must have already claimed the approval and passed kill-switch + paper caps.
    """
    ts = int(now_ms if now_ms is not None else time.time() * 1000)
    approval_id = str(approval["id"])
    symbol = str(approval["symbol"]).upper()
    action = str(approval.get("action") or "flat")
    size = approval.get("size_pct_equity")
    size_f = float(size) if size is not None else None

    day_breach = live_day_loss_breach(conn, now_ms=ts)
    if day_breach:
        row = insert_live_order(
            conn,
            {
                "id": _order_id(approval_id, ts),
                "created_at_ms": ts,
                "approval_id": approval_id,
                "venue": "binance",
                "symbol": symbol,
                "side": "NONE",
                "order_type": "MARKET",
                "requested_notional_usd": None,
                "status": "blocked",
                "error": day_breach,
                "kill_switch_clear": 1,
                "caps_ok": 0,
            },
        )
        return {
            "ok": False,
            "live_order": row,
            "error": day_breach,
            "paper_only": False,
        }

    try:
        side = _side_for_action(action)
    except ValueError as exc:
        row = insert_live_order(
            conn,
            {
                "id": _order_id(approval_id, ts),
                "created_at_ms": ts,
                "approval_id": approval_id,
                "venue": "binance",
                "symbol": symbol,
                "side": "NONE",
                "order_type": "MARKET",
                "status": "error",
                "error": str(exc),
                "kill_switch_clear": 1,
                "caps_ok": 1,
            },
        )
        return {
            "ok": False,
            "live_order": row,
            "error": str(exc),
            "paper_only": False,
        }

    caps = load_risk_caps()
    equity = _live_equity_usd(conn)
    notional = (
        estimated_notional(equity=equity, size_pct_equity=size_f)
        if size_f is not None
        else 0.0
    )
    if side is None or notional <= 0:
        row = insert_live_order(
            conn,
            {
                "id": _order_id(approval_id, ts),
                "created_at_ms": ts,
                "approval_id": approval_id,
                "venue": "binance",
                "symbol": symbol,
                "side": "NONE",
                "order_type": "MARKET",
                "requested_notional_usd": notional or None,
                "status": "skipped",
                "error": "flat_or_zero_size",
                "kill_switch_clear": 1,
                "caps_ok": 1,
            },
        )
        return {
            "ok": True,
            "live_order": row,
            "error": None,
            "paper_only": False,
            "skipped": True,
        }

    if notional > caps.max_notional_per_order:
        reason = (
            f"max_notional: ${notional:.2f} > ${caps.max_notional_per_order:.2f}"
        )
        row = insert_live_order(
            conn,
            {
                "id": _order_id(approval_id, ts),
                "created_at_ms": ts,
                "approval_id": approval_id,
                "venue": "binance",
                "symbol": symbol,
                "side": side,
                "order_type": "MARKET",
                "requested_notional_usd": notional,
                "status": "blocked",
                "error": reason,
                "kill_switch_clear": 1,
                "caps_ok": 0,
            },
        )
        return {
            "ok": False,
            "live_order": row,
            "error": reason,
            "paper_only": False,
        }

    try:
        auth = resolve_trade_auth()
    except FileNotFoundError as exc:
        row = insert_live_order(
            conn,
            {
                "id": _order_id(approval_id, ts),
                "created_at_ms": ts,
                "approval_id": approval_id,
                "venue": "binance",
                "symbol": symbol,
                "side": side,
                "order_type": "MARKET",
                "requested_notional_usd": notional,
                "status": "error",
                "error": str(exc),
                "kill_switch_clear": 1,
                "caps_ok": 1,
            },
        )
        return {
            "ok": False,
            "live_order": row,
            "error": str(exc),
            "paper_only": False,
        }

    if auth is None:
        err = "missing BINANCE_TRADE_API_KEY credentials"
        row = insert_live_order(
            conn,
            {
                "id": _order_id(approval_id, ts),
                "created_at_ms": ts,
                "approval_id": approval_id,
                "venue": "binance",
                "symbol": symbol,
                "side": side,
                "order_type": "MARKET",
                "requested_notional_usd": notional,
                "status": "error",
                "error": err,
                "kill_switch_clear": 1,
                "caps_ok": 1,
            },
        )
        return {
            "ok": False,
            "live_order": row,
            "error": err,
            "paper_only": False,
        }

    try:
        audit = audit_key_permissions(fetch_api_restrictions(auth, client=http_client))
        if not audit["ok_for_trade"]:
            err = "trade key permissions unsafe: " + ", ".join(audit["block_reasons"] or ["unknown"])
            row = insert_live_order(
                conn,
                {
                    "id": _order_id(approval_id, ts),
                    "created_at_ms": ts,
                    "approval_id": approval_id,
                    "venue": "binance",
                    "symbol": symbol,
                    "side": side,
                    "order_type": "MARKET",
                    "requested_notional_usd": notional,
                    "status": "blocked",
                    "error": err,
                    "kill_switch_clear": 1,
                    "caps_ok": 1,
                },
            )
            return {
                "ok": False,
                "live_order": row,
                "error": err,
                "paper_only": False,
            }
    except Exception as exc:  # noqa: BLE001
        err = f"trade key permission check failed: {exc}"
        row = insert_live_order(
            conn,
            {
                "id": _order_id(approval_id, ts),
                "created_at_ms": ts,
                "approval_id": approval_id,
                "venue": "binance",
                "symbol": symbol,
                "side": side,
                "order_type": "MARKET",
                "requested_notional_usd": notional,
                "status": "error",
                "error": err,
                "kill_switch_clear": 1,
                "caps_ok": 1,
            },
        )
        return {
            "ok": False,
            "live_order": row,
            "error": err,
            "paper_only": False,
        }

    client_order_id = client_order_id_for_approval(approval_id)
    base_row = {
        "approval_id": approval_id,
        "venue": "binance",
        "symbol": symbol,
        "side": side,
        "order_type": "MARKET",
        "requested_notional_usd": notional,
        "kill_switch_clear": 1,
        "caps_ok": 1,
        "client_order_id": client_order_id,
    }

    # Idempotency 1/2: our own ledger already holds a venue-accepted order for this approval.
    existing = get_live_order_by_client_id(conn, client_order_id)
    if existing is not None and existing.get("status") in _VENUE_HAS_ORDER_STATUSES:
        return {
            "ok": True,
            "live_order": existing,
            "duplicate": True,
            "error": None,
            "paper_only": False,
        }

    # Idempotency 2/2: the venue may hold the order even if we never recorded it
    # (crash or lost response after POST). Query before any new POST.
    try:
        prior = query_order(
            auth,
            symbol=symbol,
            orig_client_order_id=client_order_id,
            client=http_client,
            timestamp_ms=ts,
        )
    except Exception as exc:  # noqa: BLE001 — cannot prove absence → do not POST
        err = f"pre-submit order query failed: {exc}"
        row = insert_live_order(
            conn,
            {**base_row, "id": _order_id(approval_id, ts), "created_at_ms": ts, "status": "error", "error": err},
        )
        return {"ok": False, "live_order": row, "error": err, "paper_only": False}
    if prior is not None:
        fill = summarize_fill(prior)
        row = insert_live_order(
            conn,
            {
                **base_row,
                "id": _order_id(approval_id, ts),
                "created_at_ms": ts,
                "status": fill["status"],
                "venue_order_id": fill["venue_order_id"],
                "venue_status": fill["venue_status"],
                "executed_qty": fill["executed_qty"],
                "cummulative_quote_qty": fill["cummulative_quote_qty"],
                "fills_count": fill["fills_count"],
                "venue_response_json": json.dumps(prior),
                "reconciled_at_ms": ts,
                "realized_pnl_usd": 0.0,
                "error": "recovered: venue already held this client order id",
            },
        )
        return {
            "ok": True,
            "live_order": row,
            "duplicate": True,
            "venue_response": prior,
            "error": None,
            "paper_only": False,
        }

    try:
        payload = place_spot_market_order(
            auth,
            symbol=symbol,
            side=side,
            quote_order_qty=notional if side == "BUY" else None,
            quantity=None if side == "BUY" else notional,  # SELL path reserved
            client=http_client,
            timestamp_ms=ts,
            new_client_order_id=client_order_id,
        )
    except Exception as exc:  # noqa: BLE001
        # The POST may have reached the venue (timeout / dropped response). Check once
        # before recording an error so the ledger never hides a real fill.
        recovered = None
        try:
            recovered = query_order(
                auth,
                symbol=symbol,
                orig_client_order_id=client_order_id,
                client=http_client,
                timestamp_ms=ts,
            )
        except Exception:  # noqa: BLE001 — stay with the original error
            recovered = None
        if recovered is not None:
            fill = summarize_fill(recovered)
            row = insert_live_order(
                conn,
                {
                    **base_row,
                    "id": _order_id(approval_id, ts),
                    "created_at_ms": ts,
                    "status": fill["status"],
                    "venue_order_id": fill["venue_order_id"],
                    "venue_status": fill["venue_status"],
                    "executed_qty": fill["executed_qty"],
                    "cummulative_quote_qty": fill["cummulative_quote_qty"],
                    "fills_count": fill["fills_count"],
                    "venue_response_json": json.dumps(recovered),
                    "reconciled_at_ms": ts,
                    "realized_pnl_usd": 0.0,
                    "error": f"submit response lost ({type(exc).__name__}); recovered via order query",
                },
            )
            return {
                "ok": True,
                "live_order": row,
                "venue_response": recovered,
                "recovered": True,
                "error": None,
                "paper_only": False,
            }
        row = insert_live_order(
            conn,
            {
                **base_row,
                "id": _order_id(approval_id, ts),
                "created_at_ms": ts,
                "status": "error",
                "error": str(exc),
                "venue_response_json": None,
            },
        )
        return {
            "ok": False,
            "live_order": row,
            "error": str(exc),
            "paper_only": False,
        }

    fill = summarize_fill(payload)
    row = insert_live_order(
        conn,
        {
            **base_row,
            "id": _order_id(approval_id, ts),
            "created_at_ms": ts,
            "status": fill["status"],
            "venue_order_id": fill["venue_order_id"] or (fill["client_order_id"] or None),
            "venue_status": fill["venue_status"],
            "executed_qty": fill["executed_qty"],
            "cummulative_quote_qty": fill["cummulative_quote_qty"],
            "fills_count": fill["fills_count"],
            "venue_response_json": json.dumps(payload),
            "reconciled_at_ms": ts if fill["venue_status"] else None,
            "realized_pnl_usd": 0.0,
        },
    )
    return {
        "ok": True,
        "live_order": row,
        "venue_response": payload,
        "error": None,
        "paper_only": False,
    }
