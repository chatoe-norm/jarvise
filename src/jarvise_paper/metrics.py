"""Paper expectancy metrics — round-trips from paper_orders. No exchange APIs."""

from __future__ import annotations

import math
import statistics
import time
from typing import Any

from jarvise_ingest.db import (
    STARTING_PAPER_EQUITY,
    get_paper_account,
    list_paper_orders_asc,
    list_paper_positions,
    upsert_performance_risk_metrics,
)
from jarvise_paper.feedback import (
    WINDOW_30D_MS,
    auto_ev_gate_status,
    metrics_by_decision_source,
    sync_decision_outcomes,
)

PAPER_STRATEGY_ID = "paper"
MIN_TRADES_FOR_RATIOS = 30


def _is_open(reason: str | None) -> bool:
    return bool(reason) and str(reason).startswith("open_")


def _is_close(reason: str | None) -> bool:
    return bool(reason) and str(reason).startswith("close_")


def _position_side_from_open(reason: str) -> str:
    return "long" if "long" in reason else "short"


def _trade_pnl(open_o: dict, close_o: dict, *, side: str) -> float:
    qty = float(open_o["qty"])
    entry = float(open_o["price"])
    exit_px = float(close_o["price"])
    if side == "long":
        gross = (exit_px - entry) * qty
    else:
        gross = (entry - exit_px) * qty
    fees = float(open_o.get("fee_usd") or 0) + float(close_o.get("fee_usd") or 0)
    return gross - fees


def reconstruct_closed_trades(orders: list[dict]) -> tuple[list[dict], int, int]:
    """FIFO-pair open_* with close_* per symbol.

    Returns (trades, unmatched_orders, open_legs_remaining).
    """
    stacks: dict[str, list[dict]] = {}
    trades: list[dict] = []
    unmatched = 0

    for order in orders:
        reason = order.get("reason")
        sym = str(order.get("symbol") or "").upper()
        if not sym:
            unmatched += 1
            continue
        if _is_open(reason):
            stacks.setdefault(sym, []).append(order)
            continue
        if _is_close(reason):
            stack = stacks.get(sym) or []
            if not stack:
                unmatched += 1
                continue
            open_o = stack.pop(0)
            side = _position_side_from_open(str(open_o.get("reason") or ""))
            pnl = _trade_pnl(open_o, order, side=side)
            trades.append(
                {
                    "symbol": sym,
                    "side": side,
                    "open_order_id": open_o["order_id"],
                    "close_order_id": order["order_id"],
                    "open_ts": int(open_o["ts"]),
                    "close_ts": int(order["ts"]),
                    "qty": float(open_o["qty"]),
                    "entry_price": float(open_o["price"]),
                    "exit_price": float(order["price"]),
                    "pnl_usd": round(pnl, 8),
                }
            )
            continue
        unmatched += 1

    open_remaining = sum(len(v) for v in stacks.values())
    return trades, unmatched, open_remaining


def _max_drawdown_pct(pnls: list[float], *, start: float) -> float | None:
    if not pnls:
        return None
    equity = start
    peak = start
    max_dd = 0.0
    for pnl in pnls:
        equity += pnl
        if equity > peak:
            peak = equity
        if peak > 0:
            dd = (peak - equity) / peak
            if dd > max_dd:
                max_dd = dd
    return round(max_dd * 100.0, 6)


def _sharpe_sortino(returns: list[float]) -> tuple[float | None, float | None]:
    if len(returns) < MIN_TRADES_FOR_RATIOS:
        return None, None
    mean_r = statistics.mean(returns)
    try:
        stdev = statistics.stdev(returns)
    except statistics.StatisticsError:
        return None, None
    scale = math.sqrt(len(returns))
    sharpe = (mean_r / stdev * scale) if stdev > 0 else None
    downside = [r for r in returns if r < 0]
    if len(downside) >= 2:
        dstd = statistics.pstdev(downside)
        sortino = (mean_r / dstd * scale) if dstd > 0 else None
    elif len(downside) == 1 and downside[0] != 0:
        sortino = mean_r / abs(downside[0]) * scale
    else:
        sortino = None
    return (
        round(sharpe, 6) if sharpe is not None else None,
        round(sortino, 6) if sortino is not None else None,
    )


def compute_paper_metrics(conn: Any, *, now_ms: int | None = None) -> dict[str, Any]:
    orders = list_paper_orders_asc(conn)
    trades, unmatched, open_from_orders = reconstruct_closed_trades(orders)
    orders_by_id = {str(o["order_id"]): o for o in orders}
    sync_decision_outcomes(conn, trades, orders_by_id)
    positions = list_paper_positions(conn)
    open_count = max(len(positions), open_from_orders)

    pnls = [float(t["pnl_usd"]) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    scratches = [p for p in pnls if p == 0]
    decided = len(wins) + len(losses)

    ev = round(statistics.mean(pnls), 8) if pnls else None
    win_rate = round(len(wins) / decided, 6) if decided else None
    avg_win = round(statistics.mean(wins), 8) if wins else None
    avg_loss = round(statistics.mean(losses), 8) if losses else None
    mdd = _max_drawdown_pct(pnls, start=STARTING_PAPER_EQUITY)

    returns = [p / STARTING_PAPER_EQUITY for p in pnls]
    sharpe, sortino = _sharpe_sortino(returns)

    account = get_paper_account(conn)
    equity = float(account.get("equity") or STARTING_PAPER_EQUITY)
    daily_pnl = round(equity - STARTING_PAPER_EQUITY, 8)
    ts = int(now_ms if now_ms is not None else time.time() * 1000)
    by_source = metrics_by_decision_source(conn)
    by_source_30d = metrics_by_decision_source(conn, since_ms=ts - WINDOW_30D_MS)
    gate = auto_ev_gate_status(conn, now_ms=ts)

    # Enrich reconstructed trades with attribution from open fills.
    for trade in trades:
        open_o = orders_by_id.get(str(trade["open_order_id"])) or {}
        trade["approval_id"] = open_o.get("approval_id")
        trade["decision_source"] = open_o.get("decision_source") or "unknown"

    return {
        "ok": True,
        "paper_only": True,
        "strategy_id": PAPER_STRATEGY_ID,
        "timestamp": ts,
        "closed_trades": len(trades),
        "open_count": open_count,
        "unmatched_orders": unmatched,
        "wins": len(wins),
        "losses": len(losses),
        "scratches": len(scratches),
        "expected_value_ev": ev,
        "win_rate": win_rate,
        "avg_win_usd": avg_win,
        "avg_loss_usd": avg_loss,
        "max_drawdown_pct": mdd,
        "sharpe_ratio": sharpe,
        "sortino_ratio": sortino,
        "need_trades_for_ratios": MIN_TRADES_FOR_RATIOS,
        "ratios_ready": len(trades) >= MIN_TRADES_FOR_RATIOS,
        "daily_pnl_usd": daily_pnl,
        "starting_equity": STARTING_PAPER_EQUITY,
        "equity": equity,
        "trades": trades,
        "by_decision_source": by_source,
        "by_decision_source_30d": by_source_30d,
        "auto_ev_gate": gate,
    }


def persist_metrics_snapshot(conn: Any, report: dict[str, Any]) -> None:
    upsert_performance_risk_metrics(
        conn,
        {
            "strategy_id": PAPER_STRATEGY_ID,
            "timestamp": int(report["timestamp"]),
            "expected_value_ev": report.get("expected_value_ev"),
            "sharpe_ratio": report.get("sharpe_ratio"),
            "sortino_ratio": report.get("sortino_ratio"),
            "max_drawdown_pct": report.get("max_drawdown_pct"),
            "daily_pnl_usd": report.get("daily_pnl_usd"),
            "regime_state": None,
            "confidence_score": None,
        },
    )
    conn.commit()
