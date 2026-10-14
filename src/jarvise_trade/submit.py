"""Approve â†’ live spot MARKET submit + live_orders audit, plus the shared live exit primitives."""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from jarvise_exchange.binance_spot import BinanceAuth
from jarvise_exchange.permissions import audit_key_permissions, fetch_api_restrictions
from jarvise_ingest.db import (
    get_approval,
    get_live_order_by_client_id,
    insert_live_order,
    list_live_orders_open,
    live_spot_inventory,
    sum_live_realized_pnl_utc_day,
    update_live_order_fill,
)
from jarvise_risk import estimated_notional, load_risk_caps, utc_day_bounds_ms
from jarvise_trade.auth import resolve_trade_auth
from jarvise_trade.binance_market import (
    TERMINAL_VENUE_STATUSES,
    SymbolFilters,
    cancel_order,
    client_order_id_for_approval,
    client_order_id_for_stop,
    fetch_free_balance,
    fetch_symbol_filters,
    floor_to_step,
    place_spot_market_order,
    place_spot_stop_loss_limit,
    query_order,
    summarize_fill,
)
from jarvise_trade.pnl import realized_pnl_usd

# Statuses that mean "the venue already has this order" â€” never POST again.
VENUE_HAS_ORDER_STATUSES = frozenset({"submitted", "partially_filled", "filled"})
_STOP_LIMIT_FACTOR = Decimal("0.999")


@dataclass(frozen=True)
class SellSize:
    """Venue-sellable quantity for one symbol; ``qty`` is None when nothing can be sold."""

    qty: Decimal | None
    filters: SymbolFilters
    free: Decimal
    reason: str | None = None
    dust: bool = False


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


def _fill_row(
    conn: Any,
    base_row: dict[str, Any],
    *,
    row_id: str,
    ts: int,
    payload: dict[str, Any],
    error: str | None = None,
) -> dict[str, Any]:
    fill = summarize_fill(payload)
    return insert_live_order(
        conn,
        {
            **base_row,
            "id": row_id,
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
                side=str(base_row["side"]),
                symbol=str(base_row["symbol"]),
                executed_qty=fill["executed_qty"],
                quote_qty=fill["cummulative_quote_qty"],
            ),
            "error": error,
        },
    )


def _existing_venue_order(
    conn: Any,
    *,
    auth: BinanceAuth,
    base_row: dict[str, Any],
    row_id: str,
    ts: int,
    http_client: httpx.Client | None,
) -> dict[str, Any] | None:
    """Result when this client order id must not be POSTed (already held, or absence unproven).

    None means the ledger and the venue both lack the order, so one POST is safe.
    """
    client_order_id = str(base_row["client_order_id"])
    existing = get_live_order_by_client_id(conn, client_order_id)
    if existing is not None and existing.get("status") in VENUE_HAS_ORDER_STATUSES:
        return {"ok": True, "live_order": existing, "duplicate": True, "error": None}

    # The venue may hold the order even if we never recorded it (crash or lost response
    # after POST). Query before any new POST.
    try:
        prior = query_order(
            auth,
            symbol=str(base_row["symbol"]),
            orig_client_order_id=client_order_id,
            client=http_client,
            timestamp_ms=ts,
        )
    except Exception as exc:  # noqa: BLE001 â€” cannot prove absence â†’ do not POST
        err = f"pre-submit order query failed: {exc}"
        row = insert_live_order(
            conn, {**base_row, "id": row_id, "created_at_ms": ts, "status": "error", "error": err}
        )
        return {"ok": False, "live_order": row, "error": err}
    if prior is None:
        return None
    row = _fill_row(
        conn,
        base_row,
        row_id=row_id,
        ts=ts,
        payload=prior,
        error="recovered: venue already held this client order id",
    )
    return {"ok": True, "live_order": row, "duplicate": True, "venue_response": prior, "error": None}


def _post_market(
    conn: Any,
    *,
    auth: BinanceAuth,
    base_row: dict[str, Any],
    row_id: str,
    ts: int,
    http_client: httpx.Client | None,
    quote_order_qty: float | None = None,
    quantity: Decimal | None = None,
) -> dict[str, Any]:
    client_order_id = str(base_row["client_order_id"])
    try:
        payload = place_spot_market_order(
            auth,
            symbol=str(base_row["symbol"]),
            side=str(base_row["side"]),
            quote_order_qty=quote_order_qty,
            quantity=quantity,
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
                symbol=str(base_row["symbol"]),
                orig_client_order_id=client_order_id,
                client=http_client,
                timestamp_ms=ts,
            )
        except Exception:  # noqa: BLE001 â€” stay with the original error
            recovered = None
        if recovered is not None:
            row = _fill_row(
                conn,
                base_row,
                row_id=row_id,
                ts=ts,
                payload=recovered,
                error=f"submit response lost ({type(exc).__name__}); recovered via order query",
            )
            return {
                "ok": True,
                "live_order": row,
                "venue_response": recovered,
                "recovered": True,
                "error": None,
            }
        row = insert_live_order(
            conn,
            {**base_row, "id": row_id, "created_at_ms": ts, "status": "error", "error": str(exc)},
        )
        return {"ok": False, "live_order": row, "error": str(exc)}
    row = _fill_row(conn, base_row, row_id=row_id, ts=ts, payload=payload)
    return {"ok": True, "live_order": row, "venue_response": payload, "error": None}


def sellable_qty(
    conn: Any,
    auth: BinanceAuth,
    symbol: str,
    *,
    cap_qty: float,
    price: float | None = None,
    http_client: httpx.Client | None = None,
) -> SellSize:
    """min(ledger cap, free base balance) floored to LOT_SIZE.

    Binance takes the BUY taker fee from the received base asset, so the free balance
    sits below the ledger ``executedQty``; selling the ledger amount would be rejected.
    ``price`` (default: ledger average entry) only feeds the minNotional check.
    """
    filters = fetch_symbol_filters(symbol, client=http_client)
    free = fetch_free_balance(auth, filters.base_asset, client=http_client)
    if price is None:
        _, price = live_spot_inventory(conn, symbol)
    ref = Decimal(str(price or 0))
    cap = Decimal(str(cap_qty)) if cap_qty > 0 else Decimal("0")

    def tradable(qty: Decimal) -> bool:
        return qty > 0 and qty >= filters.min_qty and (ref <= 0 or qty * ref >= filters.min_notional)

    if not tradable(floor_to_step(cap, filters.step_size)):
        return SellSize(
            None,
            filters,
            free,
            reason=f"dust: {format(cap, 'f')} {filters.base_asset} below venue minimum",
            dust=True,
        )
    qty = floor_to_step(min(cap, free), filters.step_size)
    if not tradable(qty):
        return SellSize(
            None,
            filters,
            free,
            reason=(
                f"insufficient_free_balance: free {format(free, 'f')} {filters.base_asset}"
                f" for ledger {format(cap, 'f')}"
            ),
        )
    return SellSize(qty, filters, free)


def place_protective_stop(
    conn: Any,
    *,
    auth: BinanceAuth,
    approval_id: str,
    symbol: str,
    qty_cap: float | None,
    invalidation_price: float | None,
    client_order_id: str,
    ts: int,
    http_client: httpx.Client | None = None,
    kill_switch_clear: bool = True,
) -> dict[str, Any] | None:
    """STOP_LOSS_LIMIT SELL at the invalidation price, sized to the sellable balance."""
    if qty_cap is None or float(qty_cap) <= 0 or invalidation_price is None or float(invalidation_price) <= 0:
        return None
    base_row = {
        "id": _order_id(client_order_id, ts),
        "created_at_ms": ts,
        "approval_id": approval_id,
        "venue": "binance",
        "symbol": symbol,
        "side": "SELL",
        "order_type": "STOP_LOSS_LIMIT",
        "requested_qty": float(qty_cap),
        "kill_switch_clear": int(kill_switch_clear),
        "caps_ok": 1,
        "client_order_id": client_order_id,
        "realized_pnl_usd": 0.0,
    }
    try:
        size = sellable_qty(
            conn,
            auth,
            symbol,
            cap_qty=float(qty_cap),
            price=float(invalidation_price),
            http_client=http_client,
        )
        if size.qty is None:
            raise ValueError(size.reason)
        tick = size.filters.tick_size
        stop = floor_to_step(Decimal(str(invalidation_price)), tick)
        payload = place_spot_stop_loss_limit(
            auth,
            symbol=symbol,
            side="SELL",
            quantity=size.qty,
            stop_price=stop,
            limit_price=floor_to_step(stop * _STOP_LIMIT_FACTOR, tick),
            client=http_client,
            timestamp_ms=ts,
            new_client_order_id=client_order_id,
        )
    except Exception as exc:  # noqa: BLE001
        return insert_live_order(
            conn, {**base_row, "status": "error", "error": f"protective_stop_failed: {exc}"}
        )
    summary = summarize_fill(payload)
    return insert_live_order(
        conn,
        {
            **base_row,
            "requested_qty": float(size.qty),
            "status": summary["status"],
            "venue_order_id": summary["venue_order_id"],
            "venue_status": summary["venue_status"],
            "executed_qty": summary["executed_qty"],
            "cummulative_quote_qty": summary["cummulative_quote_qty"],
            "fills_count": summary["fills_count"],
            "venue_response_json": json.dumps(payload) if isinstance(payload, dict) else None,
        },
    )


def cancel_open_stops(
    conn: Any,
    *,
    auth: BinanceAuth,
    symbol: str,
    now_ms: int,
    http_client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Cancel our own open STOP_LOSS_LIMIT rows for one symbol by client order id.

    Returns ``cancelled`` (refreshed rows now terminal on the venue) and per-row ``errors``.
    """
    sym = symbol.upper()
    stops = [
        row
        for row in list_live_orders_open(conn)
        if str(row["symbol"]).upper() == sym
        and row.get("order_type") == "STOP_LOSS_LIMIT"
        and str(row.get("side") or "").upper() == "SELL"
        and row.get("venue_status") not in TERMINAL_VENUE_STATUSES
    ]
    cancelled: list[dict[str, Any]] = []
    errors: list[str] = []
    for row in stops:
        oid = str(row["id"])
        cid = str(row["client_order_id"])
        try:
            payload = cancel_order(
                auth, symbol=sym, orig_client_order_id=cid, client=http_client, timestamp_ms=now_ms
            )
            if payload is None:
                # -2011: already filled / canceled / never placed â€” re-read before trusting it.
                payload = query_order(
                    auth, symbol=sym, orig_client_order_id=cid, client=http_client, timestamp_ms=now_ms
                )
        except Exception as exc:  # noqa: BLE001 â€” report; the stop may still be live
            errors.append(f"{oid}: {type(exc).__name__}: {exc}")
            continue
        if payload is None:
            updated = update_live_order_fill(
                conn,
                oid,
                status="canceled",
                venue_status="NOT_FOUND",
                executed_qty=row.get("executed_qty"),
                cummulative_quote_qty=row.get("cummulative_quote_qty"),
                fills_count=row.get("fills_count"),
                venue_order_id=None,
                venue_response_json=None,
                reconciled_at_ms=now_ms,
                error="venue reports no order for this client order id",
            )
            cancelled.append(updated or row)
            continue
        fill = summarize_fill(payload)
        updated = update_live_order_fill(
            conn,
            oid,
            status=fill["status"],
            venue_status=fill["venue_status"],
            executed_qty=fill["executed_qty"],
            cummulative_quote_qty=fill["cummulative_quote_qty"],
            fills_count=fill["fills_count"],
            venue_order_id=fill["venue_order_id"],
            venue_response_json=json.dumps(payload),
            reconciled_at_ms=now_ms,
            realized_pnl_usd=realized_pnl_usd(
                conn,
                side="SELL",
                symbol=sym,
                executed_qty=fill["executed_qty"],
                quote_qty=fill["cummulative_quote_qty"],
                exclude_id=oid,
            ),
        )
        if fill["venue_status"] in TERMINAL_VENUE_STATUSES:
            cancelled.append(updated or row)
        else:
            errors.append(f"{oid}: still {fill['venue_status']} after cancel")
    return {"stops": stops, "cancelled": cancelled, "errors": errors}


def _restore_stop(
    conn: Any,
    *,
    auth: BinanceAuth,
    symbol: str,
    cancelled: list[dict[str, Any]],
    ts: int,
    http_client: httpx.Client | None,
    kill_switch_clear: bool,
) -> tuple[dict[str, Any] | None, bool]:
    """Re-place one stop for the remaining inventory after a failed exit. Returns (row, unprotected)."""
    source = next((row for row in reversed(cancelled) if row.get("approval_id")), None)
    if source is None:
        return None, bool(cancelled)
    inv_qty, _ = live_spot_inventory(conn, symbol)
    if inv_qty <= 0:
        return None, False
    approval_id = str(source["approval_id"])
    approval = get_approval(conn, approval_id) or {}
    row = place_protective_stop(
        conn,
        auth=auth,
        approval_id=approval_id,
        symbol=symbol,
        qty_cap=inv_qty,
        invalidation_price=approval.get("invalidation_price"),
        client_order_id=client_order_id_for_approval("xr" + approval_id),
        ts=ts,
        http_client=http_client,
        kill_switch_clear=kill_switch_clear,
    )
    return row, row is None or row.get("status") not in VENUE_HAS_ORDER_STATUSES


def sell_inventory(
    conn: Any,
    *,
    auth: BinanceAuth,
    symbol: str,
    client_order_id: str,
    approval_id: str | None,
    now_ms: int,
    http_client: httpx.Client | None = None,
    kill_switch_clear: bool = True,
) -> dict[str, Any]:
    """Cancel our protective stops, then MARKET SELL the fee-net, LOT_SIZE-floored inventory.

    Idempotent on ``client_order_id`` (ledger, then venue query, before any POST). When the
    exit fails after stops were cancelled, one stop is re-placed; ``unprotected`` is True if
    that also fails. Dust / already-flat outcomes are recorded as ``skipped`` rows.
    """
    sym = symbol.upper()
    ts = int(now_ms)
    row_id = _order_id(client_order_id, ts)
    base_row: dict[str, Any] = {
        "approval_id": approval_id,
        "venue": "binance",
        "symbol": sym,
        "side": "SELL",
        "order_type": "MARKET",
        "kill_switch_clear": int(kill_switch_clear),
        "caps_ok": 1,
        "client_order_id": client_order_id,
    }
    out: dict[str, Any] = {
        "ok": False,
        "symbol": sym,
        "live_order": None,
        "skipped": False,
        "dust": False,
        "reason": None,
        "cancelled": [],
        "stop_replaced": None,
        "unprotected": False,
        "error": None,
    }

    held = _existing_venue_order(
        conn, auth=auth, base_row=base_row, row_id=row_id, ts=ts, http_client=http_client
    )
    if held is not None:
        return {**out, **held}

    stops = cancel_open_stops(conn, auth=auth, symbol=sym, now_ms=ts, http_client=http_client)
    out["cancelled"] = [str(row["id"]) for row in stops["cancelled"]]

    def failed(
        err: str, *, requested_qty: float | None, result: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        row = (result or {}).get("live_order") or insert_live_order(
            conn,
            {
                **base_row,
                "id": row_id,
                "created_at_ms": ts,
                "requested_qty": requested_qty,
                "status": "error",
                "error": err,
            },
        )
        stop_row, unprotected = _restore_stop(
            conn,
            auth=auth,
            symbol=sym,
            cancelled=stops["cancelled"],
            ts=ts,
            http_client=http_client,
            kill_switch_clear=kill_switch_clear,
        )
        return {**out, "live_order": row, "error": err, "stop_replaced": stop_row, "unprotected": unprotected}

    def skipped(reason: str, *, requested_qty: float | None, dust: bool = False) -> dict[str, Any]:
        row = insert_live_order(
            conn,
            {
                **base_row,
                "id": row_id,
                "created_at_ms": ts,
                "requested_qty": requested_qty,
                "status": "skipped",
                "error": reason,
            },
        )
        return {**out, "ok": True, "live_order": row, "skipped": True, "dust": dust, "reason": reason}

    inv_qty, avg_entry = live_spot_inventory(conn, sym)
    if stops["errors"]:
        return failed("stop_cancel_failed: " + "; ".join(stops["errors"]), requested_qty=inv_qty)
    if inv_qty <= 0:
        return skipped("flat_or_zero_size", requested_qty=None)
    try:
        size = sellable_qty(conn, auth, sym, cap_qty=inv_qty, price=avg_entry, http_client=http_client)
    except Exception as exc:  # noqa: BLE001
        return failed(f"sell_sizing_failed: {exc}", requested_qty=inv_qty)
    if size.qty is None:
        if size.dust:
            return skipped(str(size.reason), requested_qty=inv_qty, dust=True)
        return failed(str(size.reason), requested_qty=inv_qty)

    posted = _post_market(
        conn,
        auth=auth,
        base_row={**base_row, "requested_qty": float(size.qty)},
        row_id=row_id,
        ts=ts,
        http_client=http_client,
        quantity=size.qty,
    )
    if not posted["ok"]:
        return failed(str(posted["error"]), requested_qty=float(size.qty), result=posted)
    return {**out, **posted}


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
    if side == "SELL":
        sold = sell_inventory(
            conn,
            auth=auth,
            symbol=symbol,
            client_order_id=client_order_id,
            approval_id=approval_id,
            now_ms=ts,
            http_client=http_client,
        )
        return {**sold, "paper_only": False}

    base_row = {
        "approval_id": approval_id,
        "venue": "binance",
        "symbol": symbol,
        "side": "BUY",
        "order_type": "MARKET",
        "requested_notional_usd": notional,
        "kill_switch_clear": 1,
        "caps_ok": 1,
        "client_order_id": client_order_id,
    }
    row_id = _order_id(approval_id, ts)
    held = _existing_venue_order(
        conn, auth=auth, base_row=base_row, row_id=row_id, ts=ts, http_client=http_client
    )
    if held is not None:
        return {**held, "paper_only": False}

    bought = _post_market(
        conn,
        auth=auth,
        base_row=base_row,
        row_id=row_id,
        ts=ts,
        http_client=http_client,
        quote_order_qty=notional,
    )
    if not bought["ok"] or bought.get("recovered"):
        return {**bought, "paper_only": False}

    row = bought["live_order"]
    stop_row = None
    if row.get("status") in VENUE_HAS_ORDER_STATUSES:
        stop_row = place_protective_stop(
            conn,
            auth=auth,
            approval_id=approval_id,
            symbol=symbol,
            qty_cap=row.get("executed_qty"),
            invalidation_price=approval.get("invalidation_price"),
            client_order_id=client_order_id_for_stop(approval_id),
            ts=ts,
            http_client=http_client,
        )
    return {**bought, "stop_order": stop_row, "paper_only": False}
