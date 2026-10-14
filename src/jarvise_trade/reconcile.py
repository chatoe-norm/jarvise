"""Reconcile open live orders against the venue (read-only GET). No new orders."""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from jarvise_ingest.db import list_live_orders_open, update_live_order_fill
from jarvise_trade.auth import resolve_trade_auth
from jarvise_trade.binance_market import query_order, summarize_fill
from jarvise_trade.pnl import realized_pnl_usd


def reconcile_live_orders(
    conn: Any,
    *,
    now_ms: int | None = None,
    http_client: httpx.Client | None = None,
    dry_run: bool = False,
    limit: int = 200,
) -> dict[str, Any]:
    """Re-query every non-terminal live order by client order id and persist fill state.

    Safe with live trading off: GET-only, and a no-op when no open orders exist.
    """
    ts = int(now_ms if now_ms is not None else time.time() * 1000)
    open_rows = list_live_orders_open(conn, limit=limit)
    result: dict[str, Any] = {
        "ok": True,
        "paper_only": False,
        "read_only": True,
        "dry_run": dry_run,
        "at_ms": ts,
        "open": len(open_rows),
        "updated": [],
        "unchanged": [],
        "missing_on_venue": [],
        "errors": [],
    }
    if not open_rows:
        return result

    try:
        auth = resolve_trade_auth()
    except FileNotFoundError as exc:
        result["ok"] = False
        result["errors"].append(f"trade auth: {exc}")
        return result
    if auth is None:
        result["ok"] = False
        result["errors"].append("missing BINANCE_TRADE_API_KEY credentials; cannot reconcile")
        return result

    for row in open_rows:
        oid = str(row["id"])
        client_id = str(row["client_order_id"])
        try:
            payload = query_order(
                auth,
                symbol=str(row["symbol"]),
                orig_client_order_id=client_id,
                client=http_client,
                timestamp_ms=ts,
            )
        except Exception as exc:  # noqa: BLE001 — keep going; report per order
            result["errors"].append(f"{oid}: {type(exc).__name__}: {exc}")
            continue
        if payload is None:
            result["missing_on_venue"].append(oid)
            if not dry_run:
                update_live_order_fill(
                    conn,
                    oid,
                    status="canceled",
                    venue_status="NOT_FOUND",
                    executed_qty=row.get("executed_qty"),
                    cummulative_quote_qty=row.get("cummulative_quote_qty"),
                    fills_count=row.get("fills_count"),
                    venue_order_id=None,
                    venue_response_json=None,
                    reconciled_at_ms=ts,
                    error="venue reports no order for this client order id",
                )
            continue
        fill = summarize_fill(payload)
        changed = (
            fill["status"] != row.get("status")
            or fill["venue_status"] != row.get("venue_status")
            or fill["executed_qty"] != row.get("executed_qty")
        )
        if not changed:
            result["unchanged"].append(oid)
            if not dry_run:
                update_live_order_fill(
                    conn,
                    oid,
                    status=str(row["status"]),
                    venue_status=row.get("venue_status"),
                    executed_qty=row.get("executed_qty"),
                    cummulative_quote_qty=row.get("cummulative_quote_qty"),
                    fills_count=row.get("fills_count"),
                    venue_order_id=None,
                    venue_response_json=None,
                    reconciled_at_ms=ts,
                )
            continue
        result["updated"].append({"id": oid, "from": row.get("status"), "to": fill["status"]})
        if not dry_run:
            update_live_order_fill(
                conn,
                oid,
                status=fill["status"],
                venue_status=fill["venue_status"],
                executed_qty=fill["executed_qty"],
                cummulative_quote_qty=fill["cummulative_quote_qty"],
                fills_count=fill["fills_count"],
                venue_order_id=fill["venue_order_id"],
                venue_response_json=json.dumps(payload),
                reconciled_at_ms=ts,
                realized_pnl_usd=realized_pnl_usd(
                    conn,
                    side=str(row.get("side") or ""),
                    symbol=str(row["symbol"]),
                    executed_qty=fill["executed_qty"],
                    quote_qty=fill["cummulative_quote_qty"],
                    exclude_id=oid,
                ),
            )
    result["ok"] = not result["errors"]
    return result
