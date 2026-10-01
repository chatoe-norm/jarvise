"""Approve → live spot MARKET submit + live_orders audit."""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any

import httpx

from jarvise_ingest.db import (
    get_paper_account,
    insert_live_order,
    sum_live_realized_pnl_utc_day,
)
from jarvise_risk import check_caps, estimated_notional, load_risk_caps
from jarvise_trade.auth import resolve_trade_auth
from jarvise_trade.binance_market import place_spot_market_order


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
        payload = place_spot_market_order(
            auth,
            symbol=symbol,
            side=side,
            quote_order_qty=notional if side == "BUY" else None,
            quantity=None if side == "BUY" else notional,  # SELL path reserved
            client=http_client,
            timestamp_ms=ts,
        )
    except Exception as exc:  # noqa: BLE001
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
                "venue_response_json": None,
            },
        )
        return {
            "ok": False,
            "live_order": row,
            "error": str(exc),
            "paper_only": False,
        }

    venue_order_id = str(payload.get("orderId") or payload.get("clientOrderId") or "")
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
            "status": "submitted",
            "venue_order_id": venue_order_id or None,
            "venue_response_json": json.dumps(payload),
            "kill_switch_clear": 1,
            "caps_ok": 1,
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
