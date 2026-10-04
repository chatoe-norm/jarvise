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
    insert_live_order,
    live_spot_inventory,
    sum_live_realized_pnl_utc_day,
)
from jarvise_risk import estimated_notional, load_risk_caps, utc_day_bounds_ms
from jarvise_trade.auth import resolve_trade_auth
from jarvise_trade.binance_market import (
    client_order_id_for_approval,
    client_order_id_for_stop,
    place_spot_market_order,
    place_spot_stop_loss_limit,
    query_order,
    summarize_fill,
)
from jarvise_trade.pnl import realized_pnl_usd

# Statuses that mean "the venue already has this order" — never POST again.
_VENUE_HAS_ORDER_STATUSES = frozenset({"submitted", "partially_filled", "filled"})


def _place_protective_stop(
    conn: Any,
    *,
    auth: Any,
    approval: dict[str, Any],
    approval_id: str,
    symbol: str,
    fill: dict[str, Any],
    ts: int,
    http_client: httpx.Client | None,
) -> dict[str, Any] | None:
    qty = fill.get("executed_qty")
    inv = approval.get("invalidation_price")
    if qty is None or float(qty) <= 0 or inv is None:
        return None
    stop = float(inv)
    limit_px = stop * 0.999 if stop > 0 else stop
    cid = client_order_id_for_stop(approval_id)
    try:
        payload = place_spot_stop_loss_limit(
            auth,
            symbol=symbol,
            side="SELL",
            quantity=float(qty),
            stop_price=stop,
            limit_price=limit_px,
            client=http_client,
            timestamp_ms=ts,
            new_client_order_id=cid,
        )
    except Exception as exc:  # noqa: BLE001
        return insert_live_order(
            conn,
            {
                "id": _order_id(approval_id + "stop", ts),
                "created_at_ms": ts,
                "approval_id": approval_id,
                "venue": "binance",
                "symbol": symbol,
                "side": "SELL",
                "order_type": "STOP_LOSS_LIMIT",
                "requested_qty": float(qty),
                "status": "error",
                "error": f"protective_stop_failed: {exc}",
                "kill_switch_clear": 1,
                "caps_ok": 1,
                "client_order_id": cid,
                "realized_pnl_usd": 0.0,
            },
        )
    summary = summarize_fill(payload)
    return insert_live_order(
        conn,
        {
            "id": _order_id(approval_id + "stop", ts),
            "created_at_ms": ts,
            "approval_id": approval_id,
            "venue": "binance",
            "symbol": symbol,
            "side": "SELL",
            "order_type": "STOP_LOSS_LIMIT",
            "requested_qty": float(qty),
            "status": summary["status"],
            "venue_order_id": summary["venue_order_id"],
            "venue_status": summary["venue_status"],
            "executed_qty": summary["executed_qty"],
            "cummulative_quote_qty": summary["cummulative_quote_qty"],
            "fills_count": summary["fills_count"],
            "venue_response_json": json.dumps(payload) if isinstance(payload, dict) else None,
            "kill_switch_clear": 1,
            "caps_ok": 1,
            "client_order_id": cid,
            "realized_pnl_usd": 0.0,
        },
    )


def _live_equity_usd(_conn: Any) -> float:
    raw = (os.environ.get("JARVISE_LIVE_EQUITY_USD") or "").strip()
    if not raw:
        raise ValueError("live_equity_unknown")
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError("live_equity_unknown") from exc
    if value <= 0:
        raise ValueError("live_equity_unknown")
    return value


def live_preflight(_conn: Any, approval: dict[str, Any]) -> str | None:
    action = str(approval.get("action") or "flat").lower()
    if action == "short":
        return "spot live submit does not support short opens"
    if action == "long":
        inv = approval.get("invalidation_price")
        if inv is None:
            return "missing_invalidation"
        try:
            if float(inv) <= 0:
                return "missing_invalidation"
        except (TypeError, ValueError):
            return "missing_invalidation"
    return None


def live_day_loss_breach(conn: Any, *, now_ms: int) -> str | None:
    """Block if realized live PnL for UTC day is at/under -max_daily_loss."""
    caps = load_risk_caps()
    start, end = utc_day_bounds_ms(now_ms)
    pnl = sum_live_realized_pnl_utc_day(conn, day_start_ms=start, day_end_ms=end)
    if pnl <= -caps.max_daily_loss_usd:
        return f"live_max_daily_loss: day_pnl ${pnl:.2f} <= -${caps.max_daily_loss_usd:.2f}"
    return None


def _order_id(approval_id: str, now_ms: int) -> str:
    material = f"live|{approval_id}|{now_ms}"
    return hashlib.sha256(material.encode()).hexdigest()[:20]


def _side_for_action(action: str) -> str | None:
    act = (action or "").lower()
    if act == "long":
        return "BUY"
    if act == "flat":
        return "SELL"
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

    pre = live_preflight(conn, approval)
    if pre:
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
                "status": "blocked",
                "error": pre,
                "kill_switch_clear": 1,
                "caps_ok": 0,
            },
        )
        return {"ok": False, "live_order": row, "error": pre, "paper_only": False}

    try:
        equity = _live_equity_usd(conn)
    except ValueError as exc:
        err = str(exc)
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
                "status": "blocked",
                "error": err,
                "kill_switch_clear": 1,
                "caps_ok": 0,
            },
        )
        return {"ok": False, "live_order": row, "error": err, "paper_only": False}

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
    sell_qty: float | None = None
    if side == "SELL":
        inv_qty, avg_entry = live_spot_inventory(conn, symbol)
        if inv_qty <= 0:
            side = None
            notional = 0.0
        else:
            sell_qty = inv_qty
            notional = inv_qty * avg_entry if avg_entry > 0 else 0.0
    else:
        notional = estimated_notional(equity=equity, size_pct_equity=size_f) if size_f is not None else 0.0
    if side is None or (side == "BUY" and notional <= 0) or (side == "SELL" and (sell_qty or 0) <= 0):
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
        reason = f"max_notional: ${notional:.2f} > ${caps.max_notional_per_order:.2f}"
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
        "requested_notional_usd": notional if side == "BUY" else None,
        "requested_qty": sell_qty if side == "SELL" else None,
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
            {
                **base_row,
                "id": _order_id(approval_id, ts),
                "created_at_ms": ts,
                "status": "error",
                "error": err,
            },
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
                "realized_pnl_usd": realized_pnl_usd(
                    conn,
                    side=side,
                    symbol=symbol,
                    executed_qty=fill["executed_qty"],
                    quote_qty=fill["cummulative_quote_qty"],
                ),
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
            quantity=sell_qty if side == "SELL" else None,
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
                    "realized_pnl_usd": realized_pnl_usd(
                        conn,
                        side=side,
                        symbol=symbol,
                        executed_qty=fill["executed_qty"],
                        quote_qty=fill["cummulative_quote_qty"],
                    ),
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
            "realized_pnl_usd": realized_pnl_usd(
                conn,
                side=side,
                symbol=symbol,
                executed_qty=fill["executed_qty"],
                quote_qty=fill["cummulative_quote_qty"],
            ),
        },
    )
    stop_row = None
    if side == "BUY" and fill["status"] in {"filled", "partially_filled", "submitted"}:
        stop_row = _place_protective_stop(
            conn,
            auth=auth,
            approval=approval,
            approval_id=approval_id,
            symbol=symbol,
            fill=fill,
            ts=ts,
            http_client=http_client,
        )
    return {
        "ok": True,
        "live_order": row,
        "stop_order": stop_row,
        "venue_response": payload,
        "error": None,
        "paper_only": False,
    }
