"""Kill-switch live flatten: cancel our protective stops, then MARKET SELL live spot inventory.

Runs only when live trading is on **and** the kill-switch is known-engaged. An unreadable
kill-switch still blocks new orders elsewhere, but never sells here.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from jarvise_exchange.binance_spot import BinanceAuth
from jarvise_ingest.db import (
    get_latest_live_buy,
    get_live_order_by_client_id,
    list_live_orders_open,
    list_live_symbols,
    live_spot_inventory,
)
from jarvise_notify import notify_live_flatten
from jarvise_risk import engage_kill_switch, kill_switch_state
from jarvise_trade.auth import resolve_trade_auth
from jarvise_trade.binance_market import client_order_id_for_approval
from jarvise_trade.flags import live_trading_enabled
from jarvise_trade.reconcile import reconcile_live_orders
from jarvise_trade.submit import VENUE_HAS_ORDER_STATUSES, live_day_loss_breach, sell_inventory


def flatten_client_order_id(buy_row_id: str) -> str:
    """One flatten SELL per position: keyed to the latest filled BUY row."""
    return client_order_id_for_approval("f" + buy_row_id)


def _targets(conn: Any) -> list[tuple[str, float, str]]:
    targets: list[tuple[str, float, str]] = []
    for symbol in list_live_symbols(conn):
        inv_qty, _ = live_spot_inventory(conn, symbol)
        buy = get_latest_live_buy(conn, symbol)
        if inv_qty > 0 and buy is not None:
            targets.append((symbol, inv_qty, flatten_client_order_id(str(buy["id"]))))
    return targets


def _resolve_auth(errors: list[str]) -> BinanceAuth | None:
    try:
        auth = resolve_trade_auth()
    except FileNotFoundError as exc:
        errors.append(f"trade auth: {exc}")
        return None
    if auth is None:
        errors.append("missing BINANCE_TRADE_API_KEY credentials; cannot flatten")
    return auth


def flatten_live_positions(
    conn: Any,
    *,
    reason: str | None = None,
    now_ms: int | None = None,
    http_client: httpx.Client | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Close every live spot position Jarvise holds while the kill-switch is engaged.

    Per symbol: cancel our open STOP_LOSS_LIMIT orders, then one idempotent MARKET SELL of
    the fee-net, LOT_SIZE-floored inventory (``jrv-f`` + latest BUY row id). Dust and
    already-sold positions are recorded once and not re-alerted. ``dry_run`` makes no HTTP.
    """
    ts = int(now_ms if now_ms is not None else time.time() * 1000)
    result: dict[str, Any] = {
        "ok": True,
        "ran": False,
        "dry_run": dry_run,
        "at_ms": ts,
        "reason": reason,
        "skipped_reason": None,
        "symbols": [],
        "cancelled": [],
        "sold": [],
        "dust": [],
        "stop_replaced": [],
        "unprotected": [],
        "errors": [],
        "notified": False,
    }
    if not live_trading_enabled():
        return {**result, "ok": False, "skipped_reason": "live_trading_disabled"}
    ks = kill_switch_state(strict=True)
    if not ks["known"]:
        return {**result, "ok": False, "skipped_reason": f"kill_switch_unknown: {ks.get('error')}"}
    if not ks["engaged"]:
        return {**result, "skipped_reason": "kill_switch_clear"}
    result["ran"] = True
    result["reason"] = reason or ks.get("reason") or "engaged"

    targets = _targets(conn)
    if not targets:
        return result

    auth = None if dry_run else _resolve_auth(result["errors"])
    if result["errors"]:
        result["ok"] = False
        result["notified"] = notify_live_flatten(result)
        return result

    open_stops = list_live_orders_open(conn) if dry_run else []
    for symbol, inv_qty, cid in targets:
        entry: dict[str, Any] = {"symbol": symbol, "inventory": inv_qty, "client_order_id": cid}
        prior = get_live_order_by_client_id(conn, cid)
        if prior is not None and prior.get("status") in VENUE_HAS_ORDER_STATUSES:
            result["symbols"].append({**entry, "status": "already_sold", "live_order_id": prior["id"]})
            continue
        if prior is not None and prior.get("status") == "skipped":
            result["symbols"].append({**entry, "status": "already_recorded", "reason": prior.get("error")})
            continue
        if dry_run or auth is None:
            stops = [
                str(row["client_order_id"])
                for row in open_stops
                if str(row["symbol"]).upper() == symbol and row.get("order_type") == "STOP_LOSS_LIMIT"
            ]
            result["symbols"].append({**entry, "status": "would_sell", "stops_to_cancel": stops})
            continue

        sold = sell_inventory(
            conn,
            auth=auth,
            symbol=symbol,
            client_order_id=cid,
            approval_id=None,
            now_ms=ts,
            http_client=http_client,
            kill_switch_clear=False,
        )
        row = sold.get("live_order") or {}
        result["cancelled"].extend(sold["cancelled"])
        if sold["stop_replaced"]:
            result["stop_replaced"].append(
                {
                    "symbol": symbol,
                    "id": sold["stop_replaced"]["id"],
                    "status": sold["stop_replaced"]["status"],
                }
            )
        if sold["unprotected"]:
            result["unprotected"].append(symbol)
        if not sold["ok"]:
            status = "error"
            result["errors"].append(f"{symbol}: {sold['error']}")
        elif sold["dust"]:
            status = "dust"
            result["dust"].append({"symbol": symbol, "reason": sold["reason"]})
        elif sold["skipped"]:
            status = "flat"
        else:
            status = "sold"
            result["sold"].append(
                {
                    "symbol": symbol,
                    "qty": row.get("executed_qty"),
                    "status": row.get("status"),
                    "client_order_id": cid,
                }
            )
        result["symbols"].append(
            {
                **entry,
                "status": status,
                "live_order_id": row.get("id"),
                "cancelled": sold["cancelled"],
                "error": sold["error"],
            }
        )

    result["ok"] = not result["errors"]
    if not dry_run:
        result["notified"] = notify_live_flatten(result)
    return result


def live_risk_tick(
    conn: Any,
    *,
    now_ms: int | None = None,
    http_client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Live block of the risk monitor: reconcile, live day-loss halt, then flatten if engaged."""
    ts = int(now_ms if now_ms is not None else time.time() * 1000)
    reconcile = reconcile_live_orders(conn, now_ms=ts, http_client=http_client)
    breach = live_day_loss_breach(conn, now_ms=ts)
    engaged = engage_kill_switch(reason=breach) if breach else False
    flatten = flatten_live_positions(
        conn, reason=breach if engaged else None, now_ms=ts, http_client=http_client
    )
    return {
        "ok": bool(reconcile["ok"]) and bool(flatten["ok"]) and (breach is None or engaged),
        "day_loss_breach": breach,
        "kill_switch_engaged_now": engaged,
        "reconcile": reconcile,
        "flatten": flatten,
    }
