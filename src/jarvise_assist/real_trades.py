"""Read-only real-vs-virtual trade report. Uses the futures read key only."""

from __future__ import annotations

import json
import statistics
from decimal import Decimal
from typing import Any

from jarvise_assist import ASSIST_SYMBOLS
from jarvise_assist.futures import FAPI_HISTORY_WINDOW_MS, BinanceFuturesClient
from jarvise_assist.sizing import round_level_to_tick
from jarvise_assist.store import now_ms
from jarvise_assist.track import TRACK_COHORT
from jarvise_ingest.db import transaction

# Deploy of PR #280 / P5 gate sample start (2026-11-04T13:51:27Z).
SPEC_START_MS = 1_793_800_287_000
REAL_SAMPLE_TARGET = 20


def time_windows(
    start_ms: int, end_ms: int, *, max_span_ms: int = FAPI_HISTORY_WINDOW_MS
) -> list[tuple[int, int]]:
    """Half-open [start, end) windows of at most max_span_ms. No gaps or overlaps."""
    if end_ms <= start_ms:
        return []
    out: list[tuple[int, int]] = []
    t = int(start_ms)
    end = int(end_ms)
    span = int(max_span_ms)
    while t < end:
        nxt = min(t + span, end)
        out.append((t, nxt))
        t = nxt
    return out


def adverse_bps(*, fill: float, ref: float, side: str) -> float | None:
    """Adverse slippage in bps; sign flipped for shorts. None when ref <= 0.

    ``side`` is the trade side that fills: position side for entry, closing
    side for a stop exit (long stop is a sell → lower fill is adverse).
    """
    if ref <= 0:
        return None
    raw = (fill - ref) / ref * 10_000.0
    side_l = side.lower()
    if side_l in {"short", "sell"}:
        return -raw
    return raw


def _weighted_avg(fills: list[tuple[float, float]]) -> tuple[float, float]:
    """Return (qty_sum, qty-weighted avg price) from (qty, price) pairs."""
    qty = sum(q for q, _ in fills)
    if qty <= 0:
        return 0.0, 0.0
    return qty, sum(q * p for q, p in fills) / qty


def _cid_step(client_id: str) -> str | None:
    cid = str(client_id or "")
    if not cid.startswith("jv-"):
        return None
    for tag in ("mkt", "sl0", "sl1", "tp", "cls", "ms", "ex"):
        if f"-{tag}" in cid:
            return tag
    return "other"


def classify_exit_kind(order: dict[str, Any]) -> tuple[str, str | None]:
    """Return (exit_kind, stop_detail). Never reads stopPrice (empty after trigger).

    VPS finding (2026-11-04): algo-triggered jv- stops land in allOrders as
    type=MARKET / origType=MARKET with clientOrderId still carrying -sl0/-sl1
    and an empty stopPrice. Classify by clientOrderId suffix first.
    """
    cid = str(order.get("clientOrderId") or "")
    typ = str(order.get("type") or "").upper()
    orig = str(order.get("origType") or "").upper()
    step = _cid_step(cid)

    if step == "cls":
        return "emergency", None
    if step == "ex":
        return "owner_exit", None
    if step in {"sl0", "sl1"}:
        return "stop", "initial"
    if step == "ms":
        return "stop", "moved"
    if typ == "STOP_MARKET" or orig == "STOP_MARKET":
        return "stop", None
    if step == "tp" or typ.startswith("TAKE_PROFIT") or orig.startswith("TAKE_PROFIT"):
        return "tp", None
    if not cid.startswith("jv-") and typ in {"MARKET", "LIMIT"}:
        return "manual", None
    return "unknown", None


def fetch_all_user_trades(
    client: BinanceFuturesClient,
    symbol: str,
    *,
    start_ms: int,
    end_ms: int,
) -> list[dict[str, Any]]:
    """Walk [start, end) in 7-day windows. Page by fromId alone after the first page."""
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for win_start, win_end in time_windows(start_ms, end_ms):
        inclusive_end = win_end - 1
        from_id: int | None = None
        while True:
            if from_id is None:
                page = client.user_trades(
                    symbol,
                    start_ms=win_start,
                    end_ms=inclusive_end,
                    limit=1000,
                )
            else:
                page = client.user_trades(symbol, from_id=from_id, limit=1000)
            if not page:
                break
            past_window = False
            for row in page:
                tms = int(row.get("time") or 0)
                if tms > inclusive_end:
                    past_window = True
                    break
                tid = int(row.get("id") or 0)
                if tid and tid in seen:
                    continue
                if tid:
                    seen.add(tid)
                rows.append(row)
            if past_window or len(page) < 1000:
                break
            last = int(page[-1].get("id") or 0)
            if last <= 0:
                break
            from_id = last + 1
    return rows


def fetch_all_orders(
    client: BinanceFuturesClient,
    symbol: str,
    *,
    start_ms: int,
    end_ms: int,
) -> list[dict[str, Any]]:
    """Walk [start, end) in 7-day windows. Page by orderId alone after the first page."""
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for win_start, win_end in time_windows(start_ms, end_ms):
        inclusive_end = win_end - 1
        order_id: int | None = None
        while True:
            if order_id is None:
                page = client.all_orders(
                    symbol,
                    start_ms=win_start,
                    end_ms=inclusive_end,
                    limit=1000,
                )
            else:
                page = client.all_orders(symbol, order_id=order_id, limit=1000)
            if not page:
                break
            past_window = False
            for row in page:
                tms = int(row.get("time") or row.get("updateTime") or 0)
                if tms > inclusive_end:
                    past_window = True
                    break
                oid = int(row.get("orderId") or 0)
                if oid and oid in seen:
                    continue
                if oid:
                    seen.add(oid)
                rows.append(row)
            if past_window or len(page) < 1000:
                break
            last = int(page[-1].get("orderId") or 0)
            if last <= 0:
                break
            order_id = last + 1
    return rows


def fetch_income_range(
    client: BinanceFuturesClient,
    *,
    symbol: str,
    start_ms: int,
    end_ms: int,
    income_type: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for win_start, win_end in time_windows(start_ms, end_ms):
        page = client.income_history(
            start_ms=win_start,
            end_ms=win_end - 1,
            income_type=income_type,
            symbol=symbol,
            limit=1000,
        )
        rows.extend(page)
    return rows


def _log_market_by_client(conn: Any) -> dict[str, dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT intent_id, client_order_id, order_or_algo_id, created_at_ms
        FROM assist_exec_log
        WHERE step='market' AND client_order_id IS NOT NULL
        """
    )
    out: dict[str, dict[str, Any]] = {}
    for row in cur.fetchall():
        data = (
            dict(row)
            if hasattr(row, "keys")
            else {
                "intent_id": row[0],
                "client_order_id": row[1],
                "order_or_algo_id": row[2],
                "created_at_ms": row[3],
            }
        )
        cid = str(data.get("client_order_id") or "")
        if cid:
            out[cid] = data
    return out


def _move_stop_intent_by_client(conn: Any) -> dict[str, str]:
    """Map move_stop client_order_id → intent_id (step move_stop_place)."""
    cur = conn.execute(
        """
        SELECT intent_id, client_order_id
        FROM assist_exec_log
        WHERE step='move_stop_place' AND client_order_id IS NOT NULL
        """
    )
    out: dict[str, str] = {}
    for row in cur.fetchall():
        if hasattr(row, "keys"):
            cid = str(row["client_order_id"] or "")
            iid = str(row["intent_id"] or "")
        else:
            iid = str(row[0] or "")
            cid = str(row[1] or "")
        if cid and iid:
            out[cid] = iid
    return out


def _intent_preview(conn: Any, intent_id: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT preview_json, ref_price, ref_source, symbol, side
        FROM assist_exec_intent WHERE id=?
        """,
        (intent_id,),
    ).fetchone()
    if row is None:
        return {}
    if hasattr(row, "keys"):
        data = dict(row)
    else:
        data = {
            "preview_json": row[0],
            "ref_price": row[1],
            "ref_source": row[2],
            "symbol": row[3],
            "side": row[4],
        }
    try:
        data["preview"] = json.loads(str(data.get("preview_json") or "{}"))
    except Exception:  # noqa: BLE001
        data["preview"] = {}
    return data


def _virtual_net_r(conn: Any, signal_id: str | None) -> float | None:
    if not signal_id:
        return None
    row = conn.execute(
        "SELECT net_r FROM assist_track WHERE signal_id=?",
        (signal_id,),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return float(row[0])


def _has_sent_track(conn: Any, signal_id: str | None) -> bool:
    if not signal_id:
        return False
    row = conn.execute(
        """
        SELECT 1 FROM assist_track
        WHERE signal_id=? AND cohort=? AND kind='sent'
        LIMIT 1
        """,
        (signal_id, TRACK_COHORT),
    ).fetchone()
    return row is not None


def _complete_income_cache(conn: Any, since_ms: int) -> dict[str, dict[str, float]]:
    """Closed rows that already have both income fields — reuse and skip refetch."""
    cur = conn.execute(
        """
        SELECT entry_order_id, funding_usdt, realized_pnl_usdt
        FROM assist_real_trade
        WHERE status='closed'
          AND entry_time_ms >= ?
          AND funding_usdt IS NOT NULL
          AND realized_pnl_usdt IS NOT NULL
        """,
        (int(since_ms),),
    )
    out: dict[str, dict[str, float]] = {}
    for row in cur.fetchall():
        if hasattr(row, "keys"):
            oid = str(row["entry_order_id"])
            out[oid] = {
                "funding_usdt": float(row["funding_usdt"]),
                "realized_pnl_usdt": float(row["realized_pnl_usdt"]),
            }
        else:
            out[str(row[0])] = {
                "funding_usdt": float(row[1]),
                "realized_pnl_usdt": float(row[2]),
            }
    return out


def _trade_side_buy_sell(side: str) -> tuple[str, str]:
    if side == "long":
        return "BUY", "SELL"
    return "SELL", "BUY"


def _closing_side_name(side: str) -> str:
    return "sell" if side == "long" else "buy"


def _order_reduce_only(order: dict[str, Any]) -> bool:
    raw = order.get("reduceOnly")
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.lower() in {"true", "1"}
    return bool(raw)


def _realized_pnl_nonzero(trade: dict[str, Any]) -> bool:
    try:
        return abs(float(trade.get("realizedPnl") or 0)) > 0
    except (TypeError, ValueError):
        return False


def _resolve_stop_trigger_for_entry(
    conn: Any,
    *,
    client: BinanceFuturesClient,
    symbol: str,
    exit_cid: str,
    detail: str | None,
    entry_intent_id: str,
    entry_side: str,
    entry_preview_stop: Any,
    move_stop_by_cid: dict[str, str],
) -> tuple[float | None, str | None]:
    step = _cid_step(exit_cid)
    if step in {"sl0", "sl1"} or detail == "initial":
        if entry_preview_stop in (None, ""):
            return None, None
        try:
            tick = client.symbol_filters(symbol).tick_size
        except Exception:  # noqa: BLE001
            return None, None
        if tick <= 0:
            return None, None
        rounded = round_level_to_tick(
            Decimal(str(entry_preview_stop)),
            tick,
            side=entry_side,
            kind="stop",
        )
        return float(rounded), "entry_preview_stop"
    if step == "ms" or detail == "moved":
        mid = move_stop_by_cid.get(exit_cid)
        if not mid:
            return None, None
        meta = _intent_preview(conn, mid)
        preview = meta.get("preview") or {}
        raw = preview.get("stop")
        if raw in (None, ""):
            return None, None
        try:
            return float(raw), "move_stop_preview"
        except (TypeError, ValueError):
            return None, None
    return None, None


def _fee_usdt_and_other(fill: dict[str, Any], *, fraction: float = 1.0) -> tuple[float, str | None]:
    asset = str(fill.get("commissionAsset") or "").upper()
    try:
        fee = float(fill.get("commission") or 0) * fraction
    except (TypeError, ValueError):
        fee = 0.0
    if asset == "USDT":
        return -abs(fee), None
    if asset and fee:
        return 0.0, f"{asset}:{fee}"
    return 0.0, None


def _logged_client_ids(conn: Any) -> set[str]:
    """Every client id Jarvise logged when placing an order (mkt, sl, tp, ms, ex, cls)."""
    cur = conn.execute(
        "SELECT DISTINCT client_order_id FROM assist_exec_log WHERE client_order_id IS NOT NULL"
    )
    return {str(row[0]) for row in cur.fetchall() if row[0]}


def _unmatched_reason(*, cid: str, logged: set[str]) -> str:
    """Why a traded order matched no in-window entry.

    A jv- market entry with a log always becomes an entry, so an unmatched one
    has no log. Only fills at or after since_ms are fetched, so an unmatched
    jv- exit (sl0/sl1/tp/cls/ms/ex, always reduce-only) that Jarvise logged
    closes a position whose entry opened before since_ms. A jv- exit Jarvise
    never logged is jv_no_log.
    """
    if not cid.startswith("jv-"):
        return "owner"
    if _cid_step(cid) == "mkt" or cid not in logged:
        return "jv_no_log"
    return "jv_before_start"


def build_real_rows(
    conn: Any,
    client: BinanceFuturesClient,
    *,
    since_ms: int = SPEC_START_MS,
    until_ms: int | None = None,
) -> list[dict[str, Any]]:
    """Fetch Binance history and build assist_real_trade rows in memory. No writes."""
    end = int(until_ms if until_ms is not None else now_ms())
    start = int(since_ms)
    market_logs = _log_market_by_client(conn)
    move_stop_by_cid = _move_stop_intent_by_client(conn)
    logged_cids = _logged_client_ids(conn)
    income_cache = _complete_income_cache(conn, start)
    rows_out: list[dict[str, Any]] = []

    for symbol in ASSIST_SYMBOLS:
        orders = fetch_all_orders(client, symbol, start_ms=start, end_ms=end)
        trades = fetch_all_user_trades(client, symbol, start_ms=start, end_ms=end)
        orders_by_id = {str(o.get("orderId")): o for o in orders if o.get("orderId") is not None}
        trades_by_order: dict[str, list[dict[str, Any]]] = {}
        for t in trades:
            oid = str(t.get("orderId") or "")
            if not oid:
                continue
            trades_by_order.setdefault(oid, []).append(t)

        chron_trades = sorted(trades, key=lambda r: (int(r.get("time") or 0), int(r.get("id") or 0)))

        # jv-…-mkt entry order ids for this symbol (matched or not).
        mkt_entry_order_ids: set[str] = set()
        open_entries: list[dict[str, Any]] = []

        for order in sorted(orders, key=lambda r: int(r.get("time") or r.get("updateTime") or 0)):
            oid = str(order.get("orderId") or "")
            cid = str(order.get("clientOrderId") or "")
            if _cid_step(cid) != "mkt":
                continue
            log = market_logs.get(cid)
            fills = trades_by_order.get(oid) or []
            if not fills:
                continue
            qty, avg = _weighted_avg([(float(f.get("qty") or 0), float(f.get("price") or 0)) for f in fills])
            if qty <= 0:
                continue
            entry_time = min(int(f.get("time") or 0) for f in fills)
            if log is None:
                continue
            mkt_entry_order_ids.add(oid)
            intent_id = str(log["intent_id"])
            meta = _intent_preview(conn, intent_id)
            preview = meta.get("preview") or {}
            side = str(meta.get("side") or preview.get("side") or "").lower()
            if side in {"buy", "sell"}:
                side = "long" if side == "buy" else "short"
            if side not in {"long", "short"}:
                side = "long" if str(order.get("side") or "").upper() == "BUY" else "short"
            signal_id = preview.get("signal_id")
            signal_entry = float(preview["entry"]) if preview.get("entry") is not None else None
            planned_stop = float(preview["stop"]) if preview.get("stop") is not None else None
            loss_at_stop = float(preview["loss_at_stop"]) if preview.get("loss_at_stop") is not None else None
            ref_price: float | None
            ref_source: str | None
            if meta.get("ref_price") not in (None, ""):
                ref_price = float(meta["ref_price"])
                ref_source = str(meta.get("ref_source") or "confirm_mark")
            elif signal_entry is not None:
                ref_price = signal_entry
                ref_source = "preview_entry"
            else:
                ref_price = None
                ref_source = None
            open_entries.append(
                {
                    "intent_id": intent_id,
                    "signal_id": str(signal_id) if signal_id else None,
                    "symbol": symbol,
                    "side": side,
                    "entry_order_id": oid,
                    "entry_client_id": cid,
                    "entry_time_ms": entry_time,
                    "entry_qty": qty,
                    "entry_fill_avg": avg,
                    "ref_price": ref_price,
                    "ref_source": ref_source,
                    "signal_entry": signal_entry,
                    "planned_stop": planned_stop,
                    "loss_at_stop": loss_at_stop,
                    "remaining": qty,
                    "exit_fills": [],
                    "exit_order_ids": [],
                    "exit_kinds": [],
                    "exit_details": [],
                    "stop_trigger": None,
                    "stop_source": None,
                    "stop_detail": None,
                    "entry_trade_ids": {int(f.get("id") or 0) for f in fills},
                    "exit_trade_ids": set(),
                    "exit_trade_fractions": {},  # trade_id -> fraction taken
                    "commission_usdt": 0.0,
                    "other_assets": [],
                    "entry_fills": fills,
                }
            )

        # Entry commission (full).
        for entry in open_entries:
            for f in entry["entry_fills"]:
                usdt, other = _fee_usdt_and_other(f, fraction=1.0)
                entry["commission_usdt"] += usdt
                if other:
                    entry["other_assets"].append(other)

        used_trade_ids: set[int] = set()
        for entry in open_entries:
            _, close_side = _trade_side_buy_sell(entry["side"])
            for t in chron_trades:
                if entry["remaining"] <= 1e-12:
                    break
                tid = int(t.get("id") or 0)
                if tid and tid in used_trade_ids:
                    continue
                if int(t.get("time") or 0) <= int(entry["entry_time_ms"]):
                    continue
                if str(t.get("side") or "").upper() != close_side:
                    continue
                oid = str(t.get("orderId") or "")
                if oid in mkt_entry_order_ids:
                    continue
                if oid == entry["entry_order_id"]:
                    continue
                order = orders_by_id.get(oid) or {}
                if not (_order_reduce_only(order) or _realized_pnl_nonzero(t)):
                    continue
                qty = float(t.get("qty") or 0)
                if qty <= 0:
                    continue
                take = min(qty, entry["remaining"])
                fraction = take / qty
                px = float(t.get("price") or 0)
                entry["exit_fills"].append((take, px))
                entry["remaining"] -= take
                entry["exit_order_ids"].append(oid)
                if tid:
                    used_trade_ids.add(tid)
                    entry["exit_trade_ids"].add(tid)
                    entry["exit_trade_fractions"][tid] = (
                        float(entry["exit_trade_fractions"].get(tid) or 0) + fraction
                    )
                usdt, other = _fee_usdt_and_other(t, fraction=fraction)
                entry["commission_usdt"] += usdt
                if other:
                    entry["other_assets"].append(other)
                kind, detail = classify_exit_kind(order if order else {"clientOrderId": ""})
                entry["exit_kinds"].append(kind)
                entry["exit_details"].append(detail)
                if kind == "stop" and entry["stop_trigger"] is None:
                    trigger, source = _resolve_stop_trigger_for_entry(
                        conn,
                        client=client,
                        symbol=symbol,
                        exit_cid=str(order.get("clientOrderId") or ""),
                        detail=detail,
                        entry_intent_id=entry["intent_id"],
                        entry_side=entry["side"],
                        entry_preview_stop=entry.get("planned_stop"),
                        move_stop_by_cid=move_stop_by_cid,
                    )
                    if trigger is not None:
                        entry["stop_trigger"] = trigger
                        entry["stop_source"] = source
                        entry["stop_detail"] = detail

        matched_order_ids: set[str] = set(mkt_entry_order_ids)
        for entry in open_entries:
            matched_order_ids.update(entry["exit_order_ids"])

        for entry in open_entries:
            exit_qty, exit_avg_f = _weighted_avg(entry["exit_fills"])
            closed = entry["remaining"] <= 1e-12 and exit_qty > 0
            kinds = entry["exit_kinds"]
            exit_kind: str | None
            if not kinds:
                exit_kind = None
            elif len(set(kinds)) == 1:
                exit_kind = kinds[0]
            else:
                exit_kind = "unknown"

            commission_usdt = float(entry["commission_usdt"])
            other_assets: list[str] = list(entry["other_assets"])
            funding_usdt = 0.0
            realized = 0.0
            exit_time: int | None
            exit_avg: float | None
            cached = income_cache.get(str(entry["entry_order_id"]))
            if closed:
                exit_avg = exit_avg_f
                exit_time = max(
                    (
                        int(t.get("time") or 0)
                        for t in chron_trades
                        if int(t.get("id") or 0) in entry["exit_trade_ids"]
                    ),
                    default=int(entry["entry_time_ms"]),
                )
                if cached is not None:
                    funding_usdt = cached["funding_usdt"]
                    realized = cached["realized_pnl_usdt"]
                else:
                    income_fund = fetch_income_range(
                        client,
                        symbol=symbol,
                        start_ms=int(entry["entry_time_ms"]) + 1,
                        end_ms=exit_time + 1,
                        income_type="FUNDING_FEE",
                    )
                    for row in income_fund:
                        tms = int(row.get("time") or 0)
                        if int(entry["entry_time_ms"]) < tms <= exit_time:
                            funding_usdt += float(row.get("income") or 0)
                    income_pnl = fetch_income_range(
                        client,
                        symbol=symbol,
                        start_ms=int(entry["entry_time_ms"]),
                        end_ms=exit_time + 1,
                        income_type="REALIZED_PNL",
                    )
                    for row in income_pnl:
                        try:
                            tid = int(row.get("tradeId") or 0)
                        except (TypeError, ValueError):
                            tid = 0
                        frac = float(entry["exit_trade_fractions"].get(tid) or 0)
                        if frac > 0:
                            realized += float(row.get("income") or 0) * frac
            else:
                exit_time = None
                exit_avg = None
                exit_kind = None

            loss = entry.get("loss_at_stop")
            net_r_real = None
            if closed and loss is not None and float(loss) > 0:
                net_r_real = (realized + commission_usdt + funding_usdt) / float(loss)
            net_r_virtual = _virtual_net_r(conn, entry.get("signal_id"))
            slip_entry = None
            if entry.get("ref_price") is not None:
                slip_entry = adverse_bps(
                    fill=float(entry["entry_fill_avg"]),
                    ref=float(entry["ref_price"]),
                    side=entry["side"],
                )
            drift = None
            if entry.get("signal_entry") is not None:
                drift = adverse_bps(
                    fill=float(entry["entry_fill_avg"]),
                    ref=float(entry["signal_entry"]),
                    side=entry["side"],
                )
            slip_stop = None
            if (
                closed
                and exit_kind == "stop"
                and entry.get("stop_trigger") is not None
                and exit_avg is not None
            ):
                slip_stop = adverse_bps(
                    fill=float(exit_avg),
                    ref=float(entry["stop_trigger"]),
                    side=_closing_side_name(entry["side"]),
                )

            exit_meta: dict[str, Any] | None = None
            if entry["exit_order_ids"]:
                exit_meta = {"order_ids": list(dict.fromkeys(entry["exit_order_ids"]))}
                if entry.get("stop_detail"):
                    exit_meta["stop_detail"] = entry["stop_detail"]
                if entry.get("stop_source"):
                    exit_meta["stop_source"] = entry["stop_source"]
                # Prefer first stop detail seen.
                for d in entry["exit_details"]:
                    if d and "stop_detail" not in exit_meta:
                        exit_meta["stop_detail"] = d

            rows_out.append(
                {
                    "intent_id": entry["intent_id"],
                    "signal_id": entry.get("signal_id"),
                    "symbol": symbol,
                    "side": entry["side"],
                    "entry_order_id": entry["entry_order_id"],
                    "entry_client_id": entry["entry_client_id"],
                    "entry_time_ms": entry["entry_time_ms"],
                    "entry_qty": entry["entry_qty"],
                    "entry_fill_avg": entry["entry_fill_avg"],
                    "ref_price": entry["ref_price"],
                    "ref_source": entry["ref_source"],
                    "signal_entry": entry.get("signal_entry"),
                    "planned_stop": entry.get("planned_stop"),
                    "loss_at_stop": loss,
                    "exit_time_ms": exit_time,
                    "exit_fill_avg": exit_avg if closed else None,
                    "exit_kind": exit_kind if closed else None,
                    "exit_order_ids": json.dumps(exit_meta) if exit_meta else None,
                    "stop_trigger": entry.get("stop_trigger"),
                    "commission_usdt": commission_usdt,
                    "commission_other_asset": ",".join(other_assets) if other_assets else None,
                    "funding_usdt": funding_usdt if closed else None,
                    "realized_pnl_usdt": realized if closed else None,
                    "net_r_real": net_r_real,
                    "net_r_virtual": net_r_virtual,
                    "slip_entry_bps": slip_entry,
                    "drift_signal_bps": drift,
                    "slip_stop_bps": slip_stop,
                    "status": "closed" if closed else "open",
                    "updated_at_ms": end,
                }
            )

        # Unmatched traded orders (not a matched entry and not part of a matched exit).
        for oid, fills in trades_by_order.items():
            if oid in matched_order_ids:
                continue
            order = orders_by_id.get(oid) or {}
            qty, avg = _weighted_avg([(float(f.get("qty") or 0), float(f.get("price") or 0)) for f in fills])
            if qty <= 0:
                continue
            side_raw = str(order.get("side") or (fills[0].get("side") if fills else "") or "").upper()
            side = "buy" if side_raw == "BUY" else "sell"
            entry_time = min(int(f.get("time") or 0) for f in fills)
            cid = str(order.get("clientOrderId") or "")
            reason = _unmatched_reason(cid=cid, logged=logged_cids)
            rows_out.append(
                {
                    "intent_id": None,
                    "signal_id": None,
                    "symbol": symbol,
                    "side": side,
                    "entry_order_id": oid,
                    "entry_client_id": cid or None,
                    "entry_time_ms": entry_time,
                    "entry_qty": qty,
                    "entry_fill_avg": avg,
                    "ref_price": None,
                    "ref_source": reason,
                    "signal_entry": None,
                    "planned_stop": None,
                    "loss_at_stop": None,
                    "exit_time_ms": None,
                    "exit_fill_avg": None,
                    "exit_kind": None,
                    "exit_order_ids": None,
                    "stop_trigger": None,
                    "commission_usdt": None,
                    "commission_other_asset": None,
                    "funding_usdt": None,
                    "realized_pnl_usdt": None,
                    "net_r_real": None,
                    "net_r_virtual": None,
                    "slip_entry_bps": None,
                    "drift_signal_bps": None,
                    "slip_stop_bps": None,
                    "status": "unmatched",
                    "updated_at_ms": end,
                }
            )

    return rows_out


_INSERT_COLS = (
    "intent_id",
    "signal_id",
    "symbol",
    "side",
    "entry_order_id",
    "entry_client_id",
    "entry_time_ms",
    "entry_qty",
    "entry_fill_avg",
    "ref_price",
    "ref_source",
    "signal_entry",
    "planned_stop",
    "loss_at_stop",
    "exit_time_ms",
    "exit_fill_avg",
    "exit_kind",
    "exit_order_ids",
    "stop_trigger",
    "commission_usdt",
    "commission_other_asset",
    "funding_usdt",
    "realized_pnl_usdt",
    "net_r_real",
    "net_r_virtual",
    "slip_entry_bps",
    "drift_signal_bps",
    "slip_stop_bps",
    "status",
    "updated_at_ms",
)


def sync_real_trades(
    conn: Any,
    client: BinanceFuturesClient,
    *,
    since_ms: int = SPEC_START_MS,
    until_ms: int | None = None,
) -> dict[str, Any]:
    """Build all rows, then replace [since_ms, …) in one transaction. All or nothing."""
    start = int(since_ms)
    rows = build_real_rows(conn, client, since_ms=start, until_ms=until_ms)
    with transaction(conn):
        conn.execute(
            "DELETE FROM assist_real_trade WHERE entry_time_ms >= ?",
            (start,),
        )
        placeholders = ",".join("?" for _ in _INSERT_COLS)
        col_sql = ",".join(_INSERT_COLS)
        for row in rows:
            conn.execute(
                f"INSERT INTO assist_real_trade ({col_sql}) VALUES ({placeholders})",
                [row.get(c) for c in _INSERT_COLS],
            )
    return {
        "ok": True,
        "n": len(rows),
        "since_ms": start,
        "until_ms": int(until_ms if until_ms is not None else now_ms()),
    }


def _p90(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = max(1, int((0.9 * len(ordered) + 0.999999) // 1)) - 1
    idx = min(idx, len(ordered) - 1)
    return ordered[idx]


def _signed_r(value: float) -> str:
    if value > 0:
        return f"+{value:.2f}R"
    return f"{value:.2f}R"


def _fmt_bps(value: float | None, *, n: int | None = None) -> str:
    if value is None:
        return "—"
    text = f"{value:.1f}"
    if n is not None and 0 < n < 10:
        text += " (n<10)"
    return text


def summarize_real_vs_virtual(
    conn: Any, *, since_ms: int = SPEC_START_MS, rows: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Closed cohort-linked rows plus unmatched/open/unknown counts since the gate start."""
    if rows is None:
        raw = conn.execute(
            """
            SELECT intent_id, signal_id, symbol, side, entry_order_id, entry_client_id,
                   entry_time_ms, entry_qty, entry_fill_avg, ref_price, ref_source,
                   signal_entry, planned_stop, loss_at_stop, exit_time_ms, exit_fill_avg,
                   exit_kind, exit_order_ids, stop_trigger, commission_usdt,
                   commission_other_asset, funding_usdt, realized_pnl_usdt,
                   net_r_real, net_r_virtual, slip_entry_bps, drift_signal_bps,
                   slip_stop_bps, status, updated_at_ms
            FROM assist_real_trade
            WHERE entry_time_ms >= ?
            """,
            (int(since_ms),),
        ).fetchall()
        rows = []
        for r in raw:
            if hasattr(r, "keys"):
                rows.append(dict(r))
            else:
                rows.append(dict(zip(_INSERT_COLS, r, strict=True)))

    closed_cohort: list[dict[str, Any]] = []
    unmatched_by: dict[str, int] = {
        "jv_before_start": 0,
        "jv_no_log": 0,
        "owner": 0,
    }
    open_n = 0
    unknown_n = 0
    unlinked_n = 0
    stop_trigger_missing_n = 0
    non_usdt_fee_n = 0

    for r in rows:
        status = str(r.get("status") or "")
        if status == "open":
            open_n += 1
            continue
        if status == "unmatched":
            reason = str(r.get("ref_source") or "owner")
            if reason not in unmatched_by:
                reason = "owner"
            unmatched_by[reason] += 1
            continue
        if status != "closed":
            continue
        if str(r.get("exit_kind") or "") == "unknown":
            unknown_n += 1
        if str(r.get("exit_kind") or "") == "stop" and r.get("stop_trigger") is None:
            stop_trigger_missing_n += 1
        sid = r.get("signal_id")
        linked = _has_sent_track(conn, str(sid) if sid else None)
        if not linked:
            # Closed jv- without core7_4h_v2 sent track.
            cid = str(r.get("entry_client_id") or "")
            if cid.startswith("jv-"):
                unlinked_n += 1
            continue
        closed_cohort.append(r)

    nets: list[float] = []
    diffs: list[float] = []
    entry_slips: list[float] = []
    drifts: list[float] = []
    stop_slips: list[float] = []
    confirm_n = 0
    preview_n = 0
    for r in closed_cohort:
        other = r.get("commission_other_asset")
        if other:
            non_usdt_fee_n += 1
        net_r = r.get("net_r_real")
        virt = r.get("net_r_virtual")
        if net_r is not None and not other:
            nets.append(float(net_r))
            if virt is not None:
                diffs.append(float(net_r) - float(virt))
        slip_e = r.get("slip_entry_bps")
        if slip_e is not None:
            entry_slips.append(float(slip_e))
        drift = r.get("drift_signal_bps")
        if drift is not None:
            drifts.append(float(drift))
        src = str(r.get("ref_source") or "")
        if src == "confirm_mark":
            confirm_n += 1
        elif src == "preview_entry":
            preview_n += 1
        if str(r.get("exit_kind") or "") == "stop" and r.get("slip_stop_bps") is not None:
            stop_slips.append(float(r["slip_stop_bps"]))

    unmatched_n = sum(unmatched_by.values())
    return {
        "closed_n": len(closed_cohort),
        "mean_net_r": statistics.mean(nets) if nets else None,
        "mean_diff_r": statistics.mean(diffs) if diffs else None,
        "mean_slip_entry_bps": statistics.mean(entry_slips) if entry_slips else None,
        "p90_slip_entry_bps": _p90(entry_slips),
        "entry_slip_n": len(entry_slips),
        "confirm_n": confirm_n,
        "preview_n": preview_n,
        "mean_drift_bps": statistics.mean(drifts) if drifts else None,
        "mean_slip_stop_bps": statistics.mean(stop_slips) if stop_slips else None,
        "p90_slip_stop_bps": _p90(stop_slips),
        "stop_n": len(stop_slips),
        "unmatched_n": unmatched_n,
        "unmatched_jv_before_start": unmatched_by["jv_before_start"],
        "unmatched_jv_no_log": unmatched_by["jv_no_log"],
        "unmatched_owner": unmatched_by["owner"],
        "unknown_n": unknown_n,
        "open_n": open_n,
        "unlinked_n": unlinked_n,
        "non_usdt_fee_n": non_usdt_fee_n,
        "stop_trigger_missing_n": stop_trigger_missing_n,
        "target_n": REAL_SAMPLE_TARGET,
    }


def format_real_lines(summary: dict[str, Any]) -> list[str]:
    n = int(summary.get("closed_n") or 0)
    target = int(summary.get("target_n") or REAL_SAMPLE_TARGET)
    if n == 0 or summary.get("mean_net_r") is None:
        mean_r = "—"
    else:
        mean_r = _signed_r(float(summary["mean_net_r"]))
    if summary.get("mean_diff_r") is None:
        diff_r = "—"
    else:
        diff_r = _signed_r(float(summary["mean_diff_r"]))
    lines = [
        f"ไม้จริง n={n} (ต้องการ {target}) · net R เฉลี่ย {mean_r} · ต่างจากไม้จำลอง {diff_r}",
    ]
    entry_n = int(summary.get("entry_slip_n") or 0)
    stop_n = int(summary.get("stop_n") or 0)
    mean_e_s = _fmt_bps(summary.get("mean_slip_entry_bps"))
    p90_e_s = _fmt_bps(summary.get("p90_slip_entry_bps"), n=entry_n)
    mean_d_s = _fmt_bps(summary.get("mean_drift_bps"))
    lines.append(
        f"slip เข้า เฉลี่ย {mean_e_s} bps · p90 {p90_e_s} "
        f"(อ้างอิง confirm_mark {int(summary.get('confirm_n') or 0)} / "
        f"preview {int(summary.get('preview_n') or 0)}) · "
        f"ราคาวิ่งจากสัญญาณ เฉลี่ย {mean_d_s} bps"
    )
    mean_s_s = _fmt_bps(summary.get("mean_slip_stop_bps"))
    p90_s_s = _fmt_bps(summary.get("p90_slip_stop_bps"), n=stop_n)
    lines.append(f"slip stop เฉลี่ย {mean_s_s} bps · p90 {p90_s_s} (n={stop_n})")
    lines.append(f"ไม้ที่ยังถือ {int(summary.get('open_n') or 0)}")
    lines.append(
        f"จับคู่ไม่ได้: {int(summary.get('unmatched_n') or 0)} ไม้ "
        f"(ก่อนเริ่ม {int(summary.get('unmatched_jv_before_start') or 0)} · "
        f"ไม่มี log {int(summary.get('unmatched_jv_no_log') or 0)} · "
        f"เจ้าของ {int(summary.get('unmatched_owner') or 0)}) · "
        f"exit ไม่ทราบชนิด: {int(summary.get('unknown_n') or 0)} · "
        f"ไม่มี track: {int(summary.get('unlinked_n') or 0)} · "
        f"ค่าธรรมเนียมนอก USDT: {int(summary.get('non_usdt_fee_n') or 0)} · "
        f"stop ไม่มี trigger: {int(summary.get('stop_trigger_missing_n') or 0)}"
    )
    return lines


def run_real_report(
    conn: Any,
    client: BinanceFuturesClient,
    *,
    since_ms: int = SPEC_START_MS,
    sync: bool = True,
) -> dict[str, Any]:
    """Sync (optional) and summarize. ``sync=False`` builds in memory and never writes."""
    if sync:
        sync_result = sync_real_trades(conn, client, since_ms=since_ms)
        summary = summarize_real_vs_virtual(conn, since_ms=since_ms)
        lines = format_real_lines(summary)
        return {
            "ok": True,
            "sync": sync_result,
            "summary": summary,
            "lines": lines,
            "text": "\n".join(lines),
        }
    rows = build_real_rows(conn, client, since_ms=since_ms)
    summary = summarize_real_vs_virtual(conn, since_ms=since_ms, rows=rows)
    lines = format_real_lines(summary)
    return {
        "ok": True,
        "sync": None,
        "summary": summary,
        "lines": lines,
        "text": "\n".join(lines),
        "rows": rows,
    }
