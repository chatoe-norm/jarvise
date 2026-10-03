"""Paper fill engine — simulated ledger only. No exchange APIs."""

from __future__ import annotations

import hashlib
import os
import time
from typing import Any

from jarvise_ingest.db import (
    STARTING_PAPER_EQUITY,
    delete_paper_position,
    ensure_paper_account,
    get_paper_account,
    get_paper_position,
    insert_paper_order,
    latest_order_book,
    list_paper_positions,
    set_paper_account_value,
    upsert_paper_position,
    upsert_performance_risk_metrics,
)

# Binance spot taker (no BNB discount) — owner-protective paper EV default.
FEE_BPS = 10.0
# Liquid US ETF retail-ish default when symbol is equity (override via env).
EQUITY_FEE_BPS = 2.0
SLIP_BPS = 5.0
STRATEGY_ID = "paper"


def load_paper_fee_bps(*, symbol: str | None = None) -> float:
    """Paper fee in bps. Equity symbols use ``JARVISE_PAPER_EQUITY_FEE_BPS`` (default 2)."""
    from jarvise_ingest.providers.stooq_ohlcv import is_equity_symbol

    if symbol and is_equity_symbol(symbol):
        raw = os.environ.get("JARVISE_PAPER_EQUITY_FEE_BPS")
        if raw is None or str(raw).strip() == "":
            return float(EQUITY_FEE_BPS)
        try:
            return max(0.0, float(raw))
        except ValueError:
            return float(EQUITY_FEE_BPS)
    raw = os.environ.get("JARVISE_PAPER_FEE_BPS")
    if raw is None or str(raw).strip() == "":
        return float(FEE_BPS)
    try:
        return max(0.0, float(raw))
    except ValueError:
        return float(FEE_BPS)


def fill_price(
    mid: float,
    *,
    side: str,
    slip_bps: float = SLIP_BPS,
    bid_ask_spread: float | None = None,
) -> float:
    """Adverse slippage: buy pays more, sell receives less.

    When bid_ask_spread is a fraction of mid (e.g. 0.001 = 10 bps), use
    max(fixed slip, half-spread in bps) so EV reflects scraped microstructure.
    """
    effective = float(slip_bps)
    if bid_ask_spread is not None and bid_ask_spread >= 0:
        half_spread_bps = float(bid_ask_spread) * 10_000.0 / 2.0
        effective = max(effective, half_spread_bps)
    slip = effective / 10_000.0
    if side == "buy":
        return mid * (1.0 + slip)
    return mid * (1.0 - slip)


def effective_slip_bps(
    *,
    slip_bps: float = SLIP_BPS,
    bid_ask_spread: float | None = None,
) -> float:
    if bid_ask_spread is None or bid_ask_spread < 0:
        return float(slip_bps)
    return max(float(slip_bps), float(bid_ask_spread) * 10_000.0 / 2.0)


def fee_usd(notional: float, *, fee_bps: float | None = None) -> float:
    bps = load_paper_fee_bps() if fee_bps is None else float(fee_bps)
    return abs(notional) * (max(0.0, bps) / 10_000.0)


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
    fee_bps: float | None = None,
    slip_bps: float = SLIP_BPS,
    now_ms: int | None = None,
    approval_id: str | None = None,
    decision_source: str | None = None,
) -> dict[str, Any]:
    """Apply one analysis action to the paper ledger.

    - flat: close any open position
    - long/short: close opposite side then open that side sized by size_pct_equity
    - same side already open: no-op hold

    Fee defaults to Binance-spot-taker-equivalent 10 bps (``JARVISE_PAPER_FEE_BPS``).
    Optional ``approval_id`` / ``decision_source`` stamp fills for T2.1 attribution.
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
    use_fee_bps = (
        load_paper_fee_bps(symbol=symbol)
        if fee_bps is None
        else max(0.0, float(fee_bps))
    )
    stamp_approval = str(approval_id) if approval_id else None
    stamp_source = str(decision_source) if decision_source else None

    book = latest_order_book(conn, symbol)
    spread = None if book is None else book.get("bid_ask_spread")
    use_slip = effective_slip_bps(slip_bps=slip_bps, bid_ask_spread=spread)

    # Working copy of positions for dry-run
    working: dict[str, dict] = {
        str(p["symbol"]).upper(): dict(p) for p in list_paper_positions(conn)
    }

    def _record_fill(side: str, qty: float, price: float, reason: str) -> None:
        nonlocal cash
        notional = qty * price
        fee = fee_usd(notional, fee_bps=use_fee_bps)
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
            "fee_bps": use_fee_bps,
            "slip_bps": use_slip,
            "analysis_id": analysis_id,
            "reason": reason,
            "approval_id": stamp_approval,
            "decision_source": stamp_source,
        }
        fills.append(order)
        if not dry_run:
            insert_paper_order(conn, order)

    if pos is not None:
        pos_side = str(pos["side"])
        want = None if action == "flat" else action
        if want != pos_side:
            close_side = "sell" if pos_side == "long" else "buy"
            px = fill_price(
                mid_price, side=close_side, slip_bps=slip_bps, bid_ask_spread=spread
            )
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
        px = fill_price(
            mid_price, side=open_side, slip_bps=slip_bps, bid_ask_spread=spread
        )
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
