"""Read-only real-vs-virtual trade report. Uses the futures read key only."""

from __future__ import annotations

import json
import statistics
from typing import Any

from jarvise_assist import ASSIST_SYMBOLS
from jarvise_assist.futures import FAPI_HISTORY_WINDOW_MS, BinanceFuturesClient
from jarvise_assist.store import now_ms
from jarvise_assist.track import TRACK_COHORT

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
    """Adverse slippage in bps; sign flipped for shorts. None when ref <= 0."""
    if ref <= 0:
        return None
    raw = (fill - ref) / ref * 10_000.0
    return -raw if side == "short" else raw


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
    for tag in ("mkt", "sl0", "sl1", "tp", "cls", "ms"):
        if f"-{tag}" in cid:
            return tag
    return "other"


def classify_exit_kind(order: dict[str, Any]) -> tuple[str, float | None]:
    """Return (exit_kind, stop_trigger). Unknown when the shape does not match a known rule.

    VPS finding (2026-11-04): algo-triggered jv- stops land in allOrders as
    type=MARKET / origType=MARKET with clientOrderId still carrying -sl0/-sl1 and
    an empty stopPrice. Classify by clientOrderId suffix first.
    """
    cid = str(order.get("clientOrderId") or "")
    typ = str(order.get("type") or "").upper()
    orig = str(order.get("origType") or "").upper()
    step = _cid_step(cid)
    stop_raw = order.get("stopPrice")
    stop_trigger: float | None = None
    try:
        if stop_raw not in (None, "", "0", "0.0", "0.00000000"):
            sp = float(stop_raw)
            if sp > 0:
                stop_trigger = sp
    except (TypeError, ValueError):
        stop_trigger = None

    if step == "cls":
        return "emergency", None
    if step in {"sl0", "sl1"} or typ == "STOP_MARKET" or orig == "STOP_MARKET":
        return "stop", stop_trigger
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
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for win_start, win_end in time_windows(start_ms, end_ms):
        from_id: int | None = None
        while True:
            page = client.user_trades(
                symbol,
                start_ms=win_start,
                end_ms=win_end - 1,
                from_id=from_id,
                limit=1000,
            )
            if not page:
                break
            for row in page:
                tid = int(row.get("id") or 0)
                if tid and tid in seen:
                    continue
                if tid:
                    seen.add(tid)
                rows.append(row)
            if len(page) < 1000:
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
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for win_start, win_end in time_windows(start_ms, end_ms):
        order_id: int | None = None
        while True:
            page = client.all_orders(
                symbol,
                start_ms=win_start,
                end_ms=win_end - 1,
                order_id=order_id,
                limit=1000,
            )
            if not page:
                break
            for row in page:
                oid = int(row.get("orderId") or 0)
                if oid and oid in seen:
                    continue
                if oid:
                    seen.add(oid)
                rows.append(row)
            if len(page) < 1000:
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
        if hasattr(row, "keys"):
            data = dict(row)
        else:
            data = {
                "intent_id": row[0],
                "client_order_id": row[1],
                "order_or_algo_id": row[2],
                "created_at_ms": row[3],
            }
        cid = str(data.get("client_order_id") or "")
        if cid:
            out[cid] = data
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


def _existing_closed(conn: Any, entry_order_id: str) -> bool:
    row = conn.execute(
        "SELECT status FROM assist_real_trade WHERE entry_order_id=?",
        (entry_order_id,),
    ).fetchone()
    return row is not None and str(row[0]) == "closed"


def _upsert_real_trade(conn: Any, row: dict[str, Any]) -> None:
    if _existing_closed(conn, str(row["entry_order_id"])):
        return
    cols = (
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
    placeholders = ",".join("?" for _ in cols)
    col_sql = ",".join(cols)
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "entry_order_id")
    values = [row.get(c) for c in cols]
    conn.execute(
        f"""
        INSERT INTO assist_real_trade ({col_sql}) VALUES ({placeholders})
        ON CONFLICT(entry_order_id) DO UPDATE SET {updates}
        WHERE assist_real_trade.status != 'closed'
        """,
        values,
    )


def _trade_side_buy_sell(side: str) -> tuple[str, str]:
    if side == "long":
        return "BUY", "SELL"
    return "SELL", "BUY"


def sync_real_trades(
    conn: Any,
    client: BinanceFuturesClient,
    *,
    since_ms: int = SPEC_START_MS,
    until_ms: int | None = None,
) -> dict[str, Any]:
    """Fetch fills for the seven universe symbols and upsert assist_real_trade rows."""
    end = int(until_ms if until_ms is not None else now_ms())
    start = int(since_ms)
    market_logs = _log_market_by_client(conn)
    matched_order_ids: set[str] = set()
    n_upserted = 0

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

        # Chronological trades for FIFO exit matching.
        chron_trades = sorted(trades, key=lambda r: (int(r.get("time") or 0), int(r.get("id") or 0)))

        open_entries: list[dict[str, Any]] = []
        for order in sorted(orders, key=lambda r: int(r.get("time") or r.get("updateTime") or 0)):
            oid = str(order.get("orderId") or "")
            cid = str(order.get("clientOrderId") or "")
            log = market_logs.get(cid)
            if log is None or _cid_step(cid) != "mkt":
                continue
            fills = trades_by_order.get(oid) or []
            if not fills:
                continue
            qty, avg = _weighted_avg([(float(f.get("qty") or 0), float(f.get("price") or 0)) for f in fills])
            if qty <= 0:
                continue
            intent_id = str(log["intent_id"])
            meta = _intent_preview(conn, intent_id)
            preview = meta.get("preview") or {}
            side = str(meta.get("side") or preview.get("side") or order.get("side") or "").lower()
            if side in {"buy", "sell"}:
                # Prefer intent side; fall back from opening side.
                side = "long" if side == "buy" else "short" if side == "sell" else side
            if side not in {"long", "short"}:
                # Infer from market order side.
                side = "long" if str(order.get("side") or "").upper() == "BUY" else "short"
            signal_id = preview.get("signal_id")
            signal_entry = float(preview["entry"]) if preview.get("entry") is not None else None
            planned_stop = float(preview["stop"]) if preview.get("stop") is not None else None
            loss_at_stop = float(preview["loss_at_stop"]) if preview.get("loss_at_stop") is not None else None
            if meta.get("ref_price") not in (None, ""):
                ref_price = float(meta["ref_price"])
                ref_source = str(meta.get("ref_source") or "confirm_mark")
            elif signal_entry is not None:
                ref_price = signal_entry
                ref_source = "preview_entry"
            else:
                ref_price = avg
                ref_source = "preview_entry"
            entry_time = min(int(f.get("time") or 0) for f in fills)
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
                    "stop_trigger": None,
                    "entry_trade_ids": {int(f.get("id") or 0) for f in fills},
                    "exit_trade_ids": set(),
                    "commission_parts": list(fills),
                }
            )
            matched_order_ids.add(oid)

        # Assign later opposite-side fills FIFO.
        for entry in open_entries:
            open_side, close_side = _trade_side_buy_sell(entry["side"])
            _ = open_side
            for t in chron_trades:
                if entry["remaining"] <= 1e-12:
                    break
                if int(t.get("time") or 0) <= int(entry["entry_time_ms"]):
                    continue
                if str(t.get("side") or "").upper() != close_side:
                    continue
                oid = str(t.get("orderId") or "")
                if oid == entry["entry_order_id"]:
                    continue
                qty = float(t.get("qty") or 0)
                if qty <= 0:
                    continue
                take = min(qty, entry["remaining"])
                px = float(t.get("price") or 0)
                entry["exit_fills"].append((take, px))
                entry["remaining"] -= take
                entry["exit_order_ids"].append(oid)
                entry["exit_trade_ids"].add(int(t.get("id") or 0))
                entry["commission_parts"].append(t)
                matched_order_ids.add(oid)
                order = orders_by_id.get(oid) or {}
                kind, trigger = classify_exit_kind(order if order else {"clientOrderId": ""})
                entry["exit_kinds"].append(kind)
                if kind == "stop" and trigger is not None and entry["stop_trigger"] is None:
                    entry["stop_trigger"] = trigger

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
            # Commission: Binance reports a positive fee; store negated USDT for the net formula.
            commission_usdt = 0.0
            other_assets: list[str] = []
            for f in entry["commission_parts"]:
                asset = str(f.get("commissionAsset") or "").upper()
                try:
                    fee = float(f.get("commission") or 0)
                except (TypeError, ValueError):
                    fee = 0.0
                if asset == "USDT":
                    commission_usdt -= abs(fee)
                elif asset and fee:
                    other_assets.append(f"{asset}:{fee}")
            funding_usdt = 0.0
            realized = 0.0
            exit_time: int | None
            exit_avg: float | None
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
                exit_ids = {str(i) for i in entry["exit_trade_ids"]}
                for row in income_pnl:
                    tid = str(row.get("tradeId") or "")
                    if tid in exit_ids:
                        realized += float(row.get("income") or 0)
            else:
                exit_time = None
                exit_avg = None
                exit_kind = None

            loss = entry.get("loss_at_stop")
            net_r_real = None
            if closed and loss is not None and float(loss) > 0:
                net_r_real = (realized + commission_usdt + funding_usdt) / float(loss)
            net_r_virtual = _virtual_net_r(conn, entry.get("signal_id"))
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
            if closed and exit_kind == "stop" and entry.get("stop_trigger") and exit_avg:
                slip_stop = adverse_bps(
                    fill=float(exit_avg),
                    ref=float(entry["stop_trigger"]),
                    side=entry["side"],
                )

            row = {
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
                "exit_order_ids": json.dumps(list(dict.fromkeys(entry["exit_order_ids"])))
                if entry["exit_order_ids"]
                else None,
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
            _upsert_real_trade(conn, row)
            n_upserted += 1

        # Unmatched traded orders (not entry, not used as exit).
        for oid, fills in trades_by_order.items():
            if oid in matched_order_ids:
                continue
            order = orders_by_id.get(oid) or {}
            qty, avg = _weighted_avg([(float(f.get("qty") or 0), float(f.get("price") or 0)) for f in fills])
            if qty <= 0:
                continue
            side_raw = str(order.get("side") or (fills[0].get("side") if fills else "") or "").upper()
            side = "long" if side_raw == "BUY" else "short"
            entry_time = min(int(f.get("time") or 0) for f in fills)
            _upsert_real_trade(
                conn,
                {
                    "intent_id": None,
                    "signal_id": None,
                    "symbol": symbol,
                    "side": side,
                    "entry_order_id": oid,
                    "entry_client_id": str(order.get("clientOrderId") or "") or None,
                    "entry_time_ms": entry_time,
                    "entry_qty": qty,
                    "entry_fill_avg": avg,
                    "ref_price": None,
                    "ref_source": None,
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
                },
            )
            n_upserted += 1

    conn.commit()
    return {"ok": True, "n": n_upserted, "since_ms": start, "until_ms": end}


def _p90(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    # Nearest-rank: ceil(0.9 * n) as 1-based index.
    idx = max(1, int((0.9 * len(ordered) + 0.999999) // 1)) - 1
    idx = min(idx, len(ordered) - 1)
    return ordered[idx]


def _signed_r(value: float) -> str:
    if value > 0:
        return f"+{value:.2f}R"
    return f"{value:.2f}R"


def summarize_real_vs_virtual(conn: Any, *, since_ms: int = SPEC_START_MS) -> dict[str, Any]:
    """Closed cohort-linked rows plus unmatched/unknown counts since the gate start."""
    closed = conn.execute(
        """
        SELECT r.net_r_real, r.net_r_virtual, r.slip_entry_bps, r.drift_signal_bps,
               r.slip_stop_bps, r.ref_source, r.exit_kind
        FROM assist_real_trade r
        JOIN assist_track t ON t.signal_id = r.signal_id
        WHERE r.status='closed'
          AND r.entry_time_ms >= ?
          AND t.cohort=?
          AND t.kind='sent'
        """,
        (int(since_ms), TRACK_COHORT),
    ).fetchall()
    unmatched_n = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM assist_real_trade
            WHERE status='unmatched' AND entry_time_ms >= ?
            """,
            (int(since_ms),),
        ).fetchone()[0]
    )
    unknown_n = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM assist_real_trade
            WHERE status='closed' AND exit_kind='unknown' AND entry_time_ms >= ?
            """,
            (int(since_ms),),
        ).fetchone()[0]
    )

    nets: list[float] = []
    diffs: list[float] = []
    entry_slips: list[float] = []
    drifts: list[float] = []
    stop_slips: list[float] = []
    confirm_n = 0
    preview_n = 0
    for r in closed:
        if hasattr(r, "keys"):
            net_r = r["net_r_real"]
            virt = r["net_r_virtual"]
            slip_e = r["slip_entry_bps"]
            drift = r["drift_signal_bps"]
            slip_s = r["slip_stop_bps"]
            src = str(r["ref_source"] or "")
            kind = str(r["exit_kind"] or "")
        else:
            net_r, virt, slip_e, drift, slip_s, src_raw, kind_raw = r
            src = str(src_raw or "")
            kind = str(kind_raw or "")
        if net_r is not None:
            nets.append(float(net_r))
            if virt is not None:
                diffs.append(float(net_r) - float(virt))
        if slip_e is not None:
            entry_slips.append(float(slip_e))
        if drift is not None:
            drifts.append(float(drift))
        if src == "confirm_mark":
            confirm_n += 1
        elif src == "preview_entry":
            preview_n += 1
        if kind == "stop" and slip_s is not None:
            stop_slips.append(float(slip_s))

    return {
        "closed_n": len(closed),
        "mean_net_r": statistics.mean(nets) if nets else None,
        "mean_diff_r": statistics.mean(diffs) if diffs else None,
        "mean_slip_entry_bps": statistics.mean(entry_slips) if entry_slips else None,
        "p90_slip_entry_bps": _p90(entry_slips),
        "confirm_n": confirm_n,
        "preview_n": preview_n,
        "mean_drift_bps": statistics.mean(drifts) if drifts else None,
        "mean_slip_stop_bps": statistics.mean(stop_slips) if stop_slips else None,
        "p90_slip_stop_bps": _p90(stop_slips),
        "stop_n": len(stop_slips),
        "unmatched_n": unmatched_n,
        "unknown_n": unknown_n,
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
    mean_e = summary.get("mean_slip_entry_bps")
    p90_e = summary.get("p90_slip_entry_bps")
    mean_d = summary.get("mean_drift_bps")
    mean_e_s = f"{mean_e:.1f}" if mean_e is not None else "—"
    p90_e_s = f"{p90_e:.1f}" if p90_e is not None else "—"
    mean_d_s = f"{mean_d:.1f}" if mean_d is not None else "—"
    lines.append(
        f"slip เข้า เฉลี่ย {mean_e_s} bps · p90 {p90_e_s} "
        f"(อ้างอิง confirm_mark {int(summary.get('confirm_n') or 0)} / "
        f"preview {int(summary.get('preview_n') or 0)}) · "
        f"ราคาวิ่งจากสัญญาณ เฉลี่ย {mean_d_s} bps"
    )
    mean_s = summary.get("mean_slip_stop_bps")
    p90_s = summary.get("p90_slip_stop_bps")
    mean_s_s = f"{mean_s:.1f}" if mean_s is not None else "—"
    p90_s_s = f"{p90_s:.1f}" if p90_s is not None else "—"
    lines.append(f"slip stop เฉลี่ย {mean_s_s} bps · p90 {p90_s_s} (n={int(summary.get('stop_n') or 0)})")
    lines.append(
        f"จับคู่ไม่ได้: {int(summary.get('unmatched_n') or 0)} ไม้ · "
        f"exit ไม่ทราบชนิด: {int(summary.get('unknown_n') or 0)}"
    )
    return lines


def run_real_report(
    conn: Any,
    client: BinanceFuturesClient,
    *,
    since_ms: int = SPEC_START_MS,
) -> dict[str, Any]:
    sync = sync_real_trades(conn, client, since_ms=since_ms)
    summary = summarize_real_vs_virtual(conn, since_ms=since_ms)
    lines = format_real_lines(summary)
    return {"ok": True, "sync": sync, "summary": summary, "lines": lines, "text": "\n".join(lines)}
