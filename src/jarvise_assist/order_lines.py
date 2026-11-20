"""Shared formatting for protective / closing orders on Telegram."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from jarvise_assist.futures import order_stop_price, order_type
from jarvise_assist.messages import _fmt_price_tick, fmt_price, fmt_qty

QUICK_LIST_CLOSING_NOTE = "✋ คุณตั้งเอง"
BOT_CLOSING_ORDER_NOTE = "🤖 ของบอท"


def close_position_flag(order: dict[str, Any]) -> bool:
    raw = order.get("closePosition")
    if raw is True:
        return True
    return str(raw or "").lower() in {"true", "1"}


def order_list_price(order: dict[str, Any]) -> Decimal | None:
    px = order_stop_price(order)
    if px is not None:
        return px
    raw = order.get("price")
    if raw in (None, "", "0", "0.0", "0.00000000"):
        return None
    try:
        dec = Decimal(str(raw))
    except Exception:  # noqa: BLE001
        return None
    return dec if dec > 0 else None


def closing_order_note(order: dict[str, Any]) -> str:
    cid = str(order.get("clientAlgoId") or order.get("clientOrderId") or "")
    if cid.startswith("jv-"):
        return BOT_CLOSING_ORDER_NOTE
    return QUICK_LIST_CLOSING_NOTE


def format_closing_order_line(
    order: dict[str, Any],
    *,
    tick: Decimal | None,
    note: str | None = None,
) -> str:
    otype = order_type(order)
    side = str(order.get("side") or "").upper()
    px = order_list_price(order)
    px_s = (
        _fmt_price_tick(px, tick)
        if px is not None and tick is not None and tick > 0
        else (fmt_price(px) if px is not None else "?")
    )
    if close_position_flag(order):
        qty_s = "closePosition"
    else:
        raw_qty = order.get("origQty") or order.get("quantity")
        qty_s = fmt_qty(Decimal(str(raw_qty))) if raw_qty not in (None, "") else "?"
    note_s = note if note is not None else closing_order_note(order)
    return f"• {otype} {side} {px_s} · {qty_s} · {note_s}"


def is_owner_reducing_order(
    order: dict[str, Any],
    *,
    position_side: str,
    trade_side: str,
) -> bool:
    cid = str(order.get("clientAlgoId") or order.get("clientOrderId") or "")
    if cid.startswith("jv-"):
        return False
    reduce_only = bool(order.get("reduceOnly")) or close_position_flag(order)
    if not reduce_only:
        return False
    close_side = "SELL" if trade_side == "long" else "BUY"
    if str(order.get("side") or "").upper() != close_side:
        return False
    ops = str(order.get("positionSide") or "BOTH").upper()
    pos_side = position_side.upper()
    if ops in {"LONG", "SHORT"}:
        return ops == pos_side
    return ops in {"BOTH", pos_side}


def owner_closing_orders(
    orders: list[dict[str, Any]],
    *,
    symbol: str,
    position_side: str,
    trade_side: str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for order in orders:
        if str(order.get("symbol") or "").upper() != symbol.upper():
            continue
        if is_owner_reducing_order(order, position_side=position_side, trade_side=trade_side):
            out.append(order)
    return out


def reduce_only_flag(order: dict[str, Any]) -> bool:
    raw = order.get("reduceOnly")
    if raw is True:
        return True
    if raw is False:
        return False
    return str(raw or "").lower() in {"true", "1"}


def is_symbol_protective_leftover(order: dict[str, Any]) -> bool:
    return reduce_only_flag(order) or close_position_flag(order)
