"""Paper fill engine — simulated ledger only. No exchange APIs."""

from __future__ import annotations

import hashlib
import time
from typing import Any

from jarvise_ingest.db import (
    STARTING_PAPER_EQUITY,
    delete_paper_position,
    ensure_paper_account,
    get_paper_account,
    get_paper_position,
    insert_paper_order,
    list_paper_positions,
    set_paper_account_value,
    upsert_paper_position,
    upsert_performance_risk_metrics,
)

FEE_BPS = 5.0
SLIP_BPS = 5.0
STRATEGY_ID = "paper"


def fill_price(mid: float, *, side: str, slip_bps: float = SLIP_BPS) -> float:
    """Adverse slippage: buy pays more, sell receives less."""
    slip = slip_bps / 10_000.0
    if side == "buy":
        return mid * (1.0 + slip)
    return mid * (1.0 - slip)


def fee_usd(notional: float, *, fee_bps: float = FEE_BPS) -> float:
    return abs(notional) * (fee_bps / 10_000.0)


def mark_equity(cash: float, positions: list[dict], marks: dict[str, float]) -> float:
    """cash + long MTM − short MTM."""
    total = cash
    for p in positions:
        sym = str(p["symbol"]).upper()
        mark = float(marks.get(sym, p["entry_price"]))
        qty = float(p["qty"])
        if str(p["side"]) == "long":
            total += qty * mark
        else:
            total -= qty * mark
    return total


def _order_id(material: str) -> str:
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def apply_signal(
    conn: Any,
    *,
    analysis: dict[str, Any],
    mid_price: float,
    timeframe: str,
    dry_run: bool = False,
    fee_bps: float = FEE_BPS,
    slip_bps: float = SLIP_BPS,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Apply one analysis action to the paper ledger.

    - flat: close any open position
    - long/short: close opposite side then open that side sized by size_pct_equity
    - same side already open: no-op hold
    """
    ensure_paper_account(conn)
    account = get_paper_account(conn)
    symbol = str(analysis["symbol"]).upper()
    action = str(analysis.get("action") or "flat").lower()
    size_pct = float(analysis.get("size_pct_equity") or 0.0)
    analysis_id = analysis.get("analysis_id")
    ts = int(now_ms if now_ms is not None else time.time() * 1000)
    pos = get_paper_position(conn, symbol)
    fills: list[dict[str, Any]] = []
    cash = float(account["cash"])
    realized_delta = 0.0

    # Working copy of positions for dry-run
    working: dict[str, dict] = {
        str(p["symbol"]).upper(): dict(p) for p in list_paper_positions(conn)
    }

    def _record_fill(side: str, qty: float, price: float, reason: str) -> None:
        nonlocal cash
        notional = qty * price
        fee = fee_usd(notional, fee_bps=fee_bps)
        if side == "buy":
            cash -= notional + fee
        else:
            cash += notional - fee
        order = {
            "order_id": _order_id(f"{symbol}|{ts}|{side}|{qty:.12g}|{price:.12g}|{reason}"),
            "ts": ts,
            "symbol": symbol,
            "timeframe": timeframe,
            "side": side,
            "qty": qty,
            "price": price,
            "fee_usd": round(fee, 8),
            "slip_bps": slip_bps,
            "analysis_id": analysis_id,
            "reason": reason,
        }
        fills.append(order)
        if not dry_run:
            insert_paper_order(conn, order)

    if pos is not None:
        pos_side = str(pos["side"])
        want = None if action == "flat" else action
        if want != pos_side:
            close_side = "sell" if pos_side == "long" else "buy"
            px = fill_price(mid_price, side=close_side, slip_bps=slip_bps)
            qty = float(pos["qty"])
            entry = float(pos["entry_price"])
            if pos_side == "long":
                realized_delta += (px - entry) * qty
            else:
                realized_delta += (entry - px) * qty
            _record_fill(close_side, qty, px, f"close_{pos_side}")
            working.pop(symbol, None)
            if not dry_run:
                delete_paper_position(conn, symbol)
            pos = None

    if action in {"long", "short"} and size_pct > 0 and pos is None:
        equity_now = mark_equity(cash, list(working.values()), {symbol: mid_price})
        target_notional = max(0.0, equity_now) * (size_pct / 100.0)
        open_side = "buy" if action == "long" else "sell"
        px = fill_price(mid_price, side=open_side, slip_bps=slip_bps)
        if px > 0 and target_notional > 0:
            qty = target_notional / px
            _record_fill(open_side, qty, px, f"open_{action}")
            new_pos = {
                "symbol": symbol,
                "side": action,
                "qty": qty,
                "entry_price": px,
                "entry_ts": ts,
                "unrealized_pnl": 0.0,
                "realized_pnl": 0.0,
            }
            working[symbol] = new_pos
            if not dry_run:
                upsert_paper_position(conn, new_pos)

    marks = {symbol: mid_price}
    for sym, p in working.items():
        if sym not in marks:
            marks[sym] = float(p["entry_price"])
        entry = float(p["entry_price"])
        qty = float(p["qty"])
        mark = marks[sym]
        if str(p["side"]) == "long":
            unreal = (mark - entry) * qty
        else:
            unreal = (entry - mark) * qty
        p["unrealized_pnl"] = round(unreal, 8)
        if not dry_run:
            upsert_paper_position(conn, p)

    equity_out = mark_equity(cash, list(working.values()), marks)
    if not dry_run:
        set_paper_account_value(conn, "cash", round(cash, 8))
        set_paper_account_value(conn, "equity", round(equity_out, 8))
        upsert_performance_risk_metrics(
            conn,
            {
                "strategy_id": STRATEGY_ID,
                "timestamp": ts,
                "expected_value_ev": None,
                "sharpe_ratio": None,
                "sortino_ratio": None,
                "max_drawdown_pct": None,
                "daily_pnl_usd": round(equity_out - STARTING_PAPER_EQUITY, 8),
                "regime_state": analysis.get("regime_state"),
                "confidence_score": analysis.get("confidence_score"),
            },
        )
        conn.commit()

    return {
        "symbol": symbol,
        "action": action,
        "fills": fills,
        "realized_delta": round(realized_delta, 8),
        "cash": round(cash, 8),
        "equity": round(equity_out, 8),
        "dry_run": dry_run,
    }
