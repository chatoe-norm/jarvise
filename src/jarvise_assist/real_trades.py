"""Read-only real-vs-virtual trade report. Uses the futures read key only."""

from __future__ import annotations

import json
import logging
import statistics
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from jarvise_assist import ASSIST_SYMBOLS
from jarvise_assist.exit_log import exit_source_for_close, lookup_atr_at_entry
from jarvise_assist.futures import (
    ALL_ORDERS_PATH,
    FAPI_HISTORY_WINDOW_MS,
    USER_TRADES_PATH,
    BinanceFuturesClient,
)
from jarvise_assist.quick_tags import quick_income_predicate
from jarvise_assist.sizing import round_level_to_tick
from jarvise_assist.store import now_ms
from jarvise_assist.track import TRACK_COHORT, virtual_risk_unusable
from jarvise_ingest.db import transaction

logger = logging.getLogger(__name__)

# Asia/Bangkok has no DST. A fixed offset avoids the tzdata package.
_BANGKOK = timezone(timedelta(hours=7), name="Asia/Bangkok")
_THAI_MONTHS = (
    "ม.ค.",
    "ก.พ.",
    "มี.ค.",
    "เม.ย.",
    "พ.ค.",
    "มิ.ย.",
    "ก.ค.",
    "ส.ค.",
    "ก.ย.",
    "ต.ค.",
    "พ.ย.",
    "ธ.ค.",
)

# Deploy of PR #280 / P5 gate sample start (2026-11-04T13:51:27Z).
SPEC_START_MS = 1_793_800_287_000
REAL_SAMPLE_TARGET = 20
# REALIZED_PNL income can lag the fill. Commission is already on the fill.
INCOME_PENDING_MS = 24 * 60 * 60 * 1000
INCOME_REFRESH_LOOKBACK_MS = 7 * 24 * 60 * 60 * 1000
# Same 30-minute gap as journal.SYNC_GAP_MS. One sentinel row, not per trade.
INCOME_REFRESH_GAP_MS = 30 * 60 * 1000
INCOME_CATEGORIES = ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE")
_INCOME_REFRESH_SYMBOL = "*"
_INCOME_REFRESH_SIDE = "INCOME"
_INCOME_REFRESH_CLOSED_AT = 0
_R_INCOME_SKIP = frozenset({"pending", "missing"})


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


class HistoryPageUnreadable(RuntimeError):
    """allOrders or userTrades page was not a JSON list."""


def _history_page(
    client: Any,
    public_page: Callable[[], list[dict[str, Any]]],
    *,
    strict: bool,
    path: str,
    params: dict[str, Any],
    provider: str,
) -> list[dict[str, Any]]:
    """One history page.

    ``strict=False`` calls the public client method. ``BinanceFuturesClient``
    still collapses a non-list HTTP 200 to ``[]`` on that path, so main callers
    stay unchanged. ``strict=True`` reads the raw body via ``_signed_get``
    (futures.py is not edited) and raises when it is not a list. A client
    without ``_signed_get`` falls back to the public method and still raises
    if that method returns a non-list.
    """
    if not strict:
        return public_page()
    signed = getattr(client, "_signed_get", None)
    if not callable(signed):
        page = public_page()
        if not isinstance(page, list):
            raise HistoryPageUnreadable(provider)
        return page
    payload = signed(path, params, provider=provider)
    if not isinstance(payload, list):
        raise HistoryPageUnreadable(provider)
    return list(payload)


def _user_trades_params(
    symbol: str,
    *,
    start_ms: int | None,
    end_ms: int | None,
    from_id: int | None,
    limit: int,
) -> dict[str, Any]:
    """Same query as ``BinanceFuturesClient.user_trades``. No non-list collapse."""
    if from_id is not None and (start_ms is not None or end_ms is not None):
        raise ValueError("fromId cannot be sent with startTime or endTime")
    params: dict[str, Any] = {"symbol": symbol.upper(), "limit": int(limit)}
    if from_id is not None:
        params["fromId"] = int(from_id)
        return params
    if start_ms is None or end_ms is None:
        raise ValueError("startTime and endTime are required without fromId")
    params["startTime"] = int(start_ms)
    params["endTime"] = int(end_ms)
    return params


def _all_orders_params(
    symbol: str,
    *,
    start_ms: int | None,
    end_ms: int | None,
    order_id: int | None,
    limit: int,
) -> dict[str, Any]:
    """Same query as ``BinanceFuturesClient.all_orders``. No non-list collapse."""
    if order_id is not None and (start_ms is not None or end_ms is not None):
        raise ValueError("orderId cannot be sent with startTime or endTime")
    params: dict[str, Any] = {"symbol": symbol.upper(), "limit": int(limit)}
    if order_id is not None:
        params["orderId"] = int(order_id)
        return params
    if start_ms is None or end_ms is None:
        raise ValueError("startTime and endTime are required without orderId")
    params["startTime"] = int(start_ms)
    params["endTime"] = int(end_ms)
    return params


def _read_user_trades_page(
    client: BinanceFuturesClient,
    symbol: str,
    *,
    start_ms: int | None,
    end_ms: int | None,
    from_id: int | None,
    limit: int,
    strict: bool,
) -> list[dict[str, Any]]:
    def public() -> list[dict[str, Any]]:
        if from_id is None:
            return client.user_trades(symbol, start_ms=start_ms, end_ms=end_ms, limit=limit)
        return client.user_trades(symbol, from_id=from_id, limit=limit)

    if not strict:
        return public()
    return _history_page(
        client,
        public,
        strict=True,
        path=USER_TRADES_PATH,
        params=_user_trades_params(symbol, start_ms=start_ms, end_ms=end_ms, from_id=from_id, limit=limit),
        provider="binance_fapi_user_trades",
    )


def _read_all_orders_page(
    client: BinanceFuturesClient,
    symbol: str,
    *,
    start_ms: int | None,
    end_ms: int | None,
    order_id: int | None,
    limit: int,
    strict: bool,
) -> list[dict[str, Any]]:
    def public() -> list[dict[str, Any]]:
        if order_id is None:
            return client.all_orders(symbol, start_ms=start_ms, end_ms=end_ms, limit=limit)
        return client.all_orders(symbol, order_id=order_id, limit=limit)

    if not strict:
        return public()
    return _history_page(
        client,
        public,
        strict=True,
        path=ALL_ORDERS_PATH,
        params=_all_orders_params(symbol, start_ms=start_ms, end_ms=end_ms, order_id=order_id, limit=limit),
        provider="binance_fapi_all_orders",
    )


def fetch_all_user_trades(
    client: BinanceFuturesClient,
    symbol: str,
    *,
    start_ms: int,
    end_ms: int,
    strict: bool = False,
) -> list[dict[str, Any]]:
    """Walk [start, end) in 7-day windows. Page by fromId alone after the first page.

    ``strict=False`` (default) is the main-path read. ``strict=True`` raises
    ``HistoryPageUnreadable`` when any page body is not a list.
    """
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for win_start, win_end in time_windows(start_ms, end_ms):
        inclusive_end = win_end - 1
        from_id: int | None = None
        while True:
            if from_id is None:
                page = _read_user_trades_page(
                    client,
                    symbol,
                    start_ms=win_start,
                    end_ms=inclusive_end,
                    from_id=None,
                    limit=1000,
                    strict=strict,
                )
            else:
                page = _read_user_trades_page(
                    client,
                    symbol,
                    start_ms=None,
                    end_ms=None,
                    from_id=from_id,
                    limit=1000,
                    strict=strict,
                )
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
    strict: bool = False,
) -> list[dict[str, Any]]:
    """Walk [start, end) in 7-day windows. Page by orderId alone after the first page.

    ``strict=False`` (default) is the main-path read. ``strict=True`` raises
    ``HistoryPageUnreadable`` when any page body is not a list.
    """
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for win_start, win_end in time_windows(start_ms, end_ms):
        inclusive_end = win_end - 1
        order_id: int | None = None
        while True:
            if order_id is None:
                page = _read_all_orders_page(
                    client,
                    symbol,
                    start_ms=win_start,
                    end_ms=inclusive_end,
                    order_id=None,
                    limit=1000,
                    strict=strict,
                )
            else:
                page = _read_all_orders_page(
                    client,
                    symbol,
                    start_ms=None,
                    end_ms=None,
                    order_id=order_id,
                    limit=1000,
                    strict=strict,
                )
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


class IncomeIncompleteError(RuntimeError):
    """Income history hit the page safety cap; the book is truncated."""


def _income_dedupe_key(row: dict[str, Any]) -> tuple[str, ...]:
    """Full-row key so a shared tranId across trades stays distinct.

    Matches ``_income_row_key`` fields plus ``asset``. An identical row
    returned on two overlapping cursor pages collapses to one.
    """
    return (
        str(row.get("symbol") or "").upper(),
        str(row.get("incomeType") or ""),
        str(row.get("time") or ""),
        str(row.get("tradeId") or ""),
        str(row.get("income") or ""),
        str(row.get("tranId") or ""),
        str(row.get("asset") or ""),
    )


def fetch_all_income(
    client: Any,
    *,
    start_ms: int,
    end_ms: int,
    income_type: str | None = None,
    symbol: str | None = None,
    limit: int = 1000,
    page_cap: int = 100,
) -> list[dict[str, Any]]:
    """Page ``/fapi/v1/income`` until a short page. Never sends ``page``.

    Advances ``startTime`` to the last row's ``time`` (inclusive) and dedupes
    by the full row key (symbol, type, time, tradeId, income, tranId, asset).
    Windows stay within ``time_windows`` (7-day max). Raises
    ``IncomeIncompleteError`` when ``page_cap`` is hit so callers do not
    treat a truncated book as complete.
    """
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    pages = 0
    # time_windows is half-open [start, end); Binance endTime is inclusive.
    for win_start, win_end_excl in time_windows(int(start_ms), int(end_ms) + 1):
        cursor = int(win_start)
        win_end = int(win_end_excl) - 1
        while cursor <= win_end:
            if pages >= page_cap:
                raise IncomeIncompleteError("income_incomplete")
            hist_kwargs: dict[str, Any] = {
                "start_ms": cursor,
                "end_ms": win_end,
                "limit": limit,
            }
            if income_type is not None:
                hist_kwargs["income_type"] = income_type
            if symbol is not None:
                hist_kwargs["symbol"] = symbol
            page = client.income_history(**hist_kwargs)
            pages += 1
            if not page:
                break
            max_time = cursor
            new_count = 0
            for row in page:
                t = int(row.get("time") or 0)
                if t > max_time:
                    max_time = t
                key = _income_dedupe_key(row)
                if key in seen:
                    continue
                seen.add(key)
                rows.append(row)
                new_count += 1
            if len(page) < limit:
                break
            if max_time > cursor:
                cursor = max_time
                continue
            # Full page stuck at the same timestamp — cannot advance safely.
            raise IncomeIncompleteError("income_incomplete")
    return rows


def fetch_income_range(
    client: BinanceFuturesClient,
    *,
    symbol: str,
    start_ms: int,
    end_ms: int,
    income_type: str,
) -> list[dict[str, Any]]:
    """Fetch income for ``[start_ms, end_ms)`` with full pagination.

    ``end_ms`` is exclusive (legacy callers pass ``exit_time + 1``).
    """
    if end_ms <= start_ms:
        return []
    return fetch_all_income(
        client,
        start_ms=start_ms,
        end_ms=end_ms - 1,
        income_type=income_type,
        symbol=symbol,
    )


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


def linked_signal_id(preview: dict[str, Any]) -> str | None:
    """Card ``signal_id`` when the tap had one, otherwise the seen idea id."""
    card = preview.get("signal_id")
    if card:
        return str(card)
    seen = preview.get("seen_signal_id")
    if seen:
        return str(seen)
    return None


def slip_entry_r(
    *,
    side: str,
    entry_fill_avg: float | None,
    plan_entry: float | None,
    planned_stop: float | None,
) -> float | None:
    """Entry slip in R. Positive means the fill was worse than the plan.

    ``plan_entry`` is the preview entry. None when an input is missing or the
    plan distance is <= 0. Not stored.
    """
    if entry_fill_avg is None or plan_entry is None or planned_stop is None:
        return None
    side_l = str(side).lower()
    if side_l == "long":
        sgn = 1.0
    elif side_l == "short":
        sgn = -1.0
    else:
        return None
    distance = abs(float(plan_entry) - float(planned_stop))
    if distance <= 0:
        return None
    return sgn * (float(entry_fill_avg) - float(plan_entry)) / distance


def _risk_actual(
    *,
    closed: bool,
    planned_stop: float | None,
    entry_fill_avg: float | None,
    entry_qty: float | None,
    net_usdt: float,
) -> tuple[float | None, float | None]:
    """Return (risk_actual_usdt, net_r_actual). net_r_actual is None when R is undefined."""
    if not closed or planned_stop is None or entry_fill_avg is None or entry_qty is None:
        return None, None
    risk = abs(float(entry_fill_avg) - float(planned_stop)) * float(entry_qty)
    if risk <= 0:
        return risk, None
    return risk, net_usdt / risk


def _label_pair_roles(rows: list[dict[str, Any]]) -> None:
    """First real trade for a signal is the pair. Later ones are re_entry.

    Order is entry_time_ms, then entry_order_id. Unmatched rows are never re_entry.
    The flag is report-only and is not stored.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        row["re_entry"] = False
        if str(row.get("status") or "") not in {"closed", "open"}:
            continue
        if str(row.get("side") or "") not in {"long", "short"}:
            continue
        sid = row.get("signal_id")
        if not sid:
            continue
        groups.setdefault(str(sid), []).append(row)
    for group in groups.values():
        group.sort(key=lambda r: (int(r.get("entry_time_ms") or 0), str(r.get("entry_order_id") or "")))
        for extra in group[1:]:
            extra["re_entry"] = True


def format_bkk_window(start_ms: int, end_ms: int) -> str:
    """Bangkok calendar dates for a report window, e.g. ``01–07 พ.ย. (เวลาไทย)``."""
    start = datetime.fromtimestamp(int(start_ms) / 1000.0, tz=_BANGKOK)
    end = datetime.fromtimestamp(int(end_ms) / 1000.0, tz=_BANGKOK)
    start_m = _THAI_MONTHS[start.month - 1]
    end_m = _THAI_MONTHS[end.month - 1]
    if (start.year, start.month, start.day) == (end.year, end.month, end.day):
        body = f"{start.day:02d} {start_m}"
    elif start.year == end.year and start.month == end.month:
        body = f"{start.day:02d}–{end.day:02d} {start_m}"
    elif start.year == end.year:
        body = f"{start.day:02d} {start_m}–{end.day:02d} {end_m}"
    else:
        body = f"{start.day:02d} {start_m} {start.year}–{end.day:02d} {end_m} {end.year}"
    return f"{body} (เวลาไทย)"


def _virtual_net_r(conn: Any, signal_id: str | None) -> float | None:
    if not signal_id:
        return None
    row = conn.execute(
        """
        SELECT t.net_r, t.entry, t.stop, s.loss_at_stop
        FROM assist_track t
        LEFT JOIN assist_signal s ON s.id = t.signal_id
        WHERE t.signal_id=?
        """,
        (signal_id,),
    ).fetchone()
    if row is None:
        return None
    if hasattr(row, "keys"):
        net, entry, stop, loss = row["net_r"], row["entry"], row["stop"], row["loss_at_stop"]
    else:
        net, entry, stop, loss = row[0], row[1], row[2], row[3]
    if virtual_risk_unusable(entry=entry, stop=stop, loss_at_stop=loss):
        return None
    if net is None:
        return None
    return float(net)


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


def _income_number_cache(
    conn: Any,
    sql: str,
    params: tuple[Any, ...],
) -> dict[str, dict[str, float]]:
    cur = conn.execute(sql, params)
    out: dict[str, dict[str, float]] = {}
    for row in cur.fetchall():
        if hasattr(row, "keys"):
            oid = str(row["entry_order_id"])
            funding = row["funding_usdt"]
            realized = row["realized_pnl_usdt"]
        else:
            oid = str(row[0])
            funding = row[1]
            realized = row[2]
        out[oid] = {
            "funding_usdt": 0.0 if funding is None else float(funding),
            "realized_pnl_usdt": 0.0 if realized is None else float(realized),
        }
    return out


def _complete_income_cache(conn: Any, since_ms: int) -> dict[str, dict[str, float]]:
    """Closed rows whose REALIZED_PNL income already matched. Skip the refetch.

    Pending, missing, and NULL (rows written before income_status) are not
    served. A stored realized of 0 is not evidence that income arrived.
    """
    return _income_number_cache(
        conn,
        """
        SELECT entry_order_id, funding_usdt, realized_pnl_usdt
        FROM assist_real_trade
        WHERE status='closed'
          AND entry_time_ms >= ?
          AND income_status = 'complete'
          AND realized_pnl_usdt IS NOT NULL
        """,
        (int(since_ms),),
    )


def _frozen_missing_income(conn: Any, since_ms: int, now_ms: int) -> dict[str, dict[str, float]]:
    """Missing rows older than 7 days. Keep the stored numbers. Do not refetch."""
    cutoff = int(now_ms) - INCOME_REFRESH_LOOKBACK_MS
    return _income_number_cache(
        conn,
        """
        SELECT entry_order_id, funding_usdt, realized_pnl_usdt
        FROM assist_real_trade
        WHERE status='closed'
          AND entry_time_ms >= ?
          AND income_status = 'missing'
          AND exit_time_ms IS NOT NULL
          AND exit_time_ms < ?
        """,
        (int(since_ms), cutoff),
    )


def _sum_income_by_trade(income_rows: list[dict[str, Any]]) -> dict[int, float]:
    out: dict[int, float] = {}
    for row in income_rows:
        try:
            tid = int(row.get("tradeId") or 0)
        except (TypeError, ValueError):
            tid = 0
        if tid <= 0:
            continue
        try:
            amount = float(row.get("income") or 0)
        except (TypeError, ValueError):
            amount = 0.0
        out[tid] = out.get(tid, 0.0) + amount
    return out


def _income_status_for_close(
    *,
    needed: set[int],
    matched: dict[int, float],
    exit_time_ms: int,
    now_ms: int,
) -> str:
    """complete when every nonzero exit fill has a REALIZED_PNL row.

    Absent FUNDING_FEE is not an input. Commission is taken from fills.
    """
    if not needed or needed <= set(matched):
        return "complete"
    if int(now_ms) - int(exit_time_ms) > INCOME_PENDING_MS:
        return "missing"
    return "pending"


def _scaled_income(matched: dict[int, float], fractions: dict[int, float]) -> float:
    total = 0.0
    for tid, frac in fractions.items():
        if frac <= 0 or tid not in matched:
            continue
        total += matched[tid] * float(frac)
    return total


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


# Credits / stables treated as 1:1 USDT for commission conversion (owner decision).
USDT_PEGGED_FEE_ASSETS = frozenset({"BNFCR", "USDC", "FDUSD", "BFUSD"})


def _fee_usdt_and_other(
    fill: dict[str, Any],
    *,
    fraction: float = 1.0,
    asset_prices: dict[str, float] | None = None,
) -> tuple[float, str | None]:
    """Convert fill commission to USDT. Returns (usdt_fee, other_asset_tag).

    USDT and pegged stables (BNFCR/USDC/FDUSD/BFUSD) are 1:1. Other assets
    (e.g. BNB) need ``asset_prices[asset]`` or ``asset_prices['BNBUSDT']`` as
    the USDT price (preferred production source: fill-time 1m kline close).
    Without a price the fee stays in ``other`` and callers must not treat
    ``net_r_real`` as complete. Never invents a rate; does not call the network.
    """
    asset = str(fill.get("commissionAsset") or "").upper()
    try:
        fee = float(fill.get("commission") or 0) * fraction
    except (TypeError, ValueError):
        fee = 0.0
    if asset == "USDT":
        return -abs(fee), None
    if asset in USDT_PEGGED_FEE_ASSETS:
        return -abs(fee), None
    if asset and fee:
        prices = asset_prices or {}
        price = prices.get(asset)
        if price is None:
            price = prices.get(f"{asset}USDT")
        try:
            px = float(price) if price is not None else 0.0
        except (TypeError, ValueError):
            px = 0.0
        if px > 0:
            return -abs(abs(fee) * px), None
        return 0.0, f"{asset}:{fee}"
    return 0.0, None


def _logged_client_ids(conn: Any) -> set[str]:
    """Every client id Jarvise logged when placing an order (mkt, sl, tp, ms, ex, cls)."""
    cur = conn.execute(
        "SELECT DISTINCT client_order_id FROM assist_exec_log WHERE client_order_id IS NOT NULL"
    )
    return {str(row[0]) for row in cur.fetchall() if row[0]}


def _quick_owned_client_and_order_ids(
    conn: Any,
) -> tuple[set[str], set[tuple[str, str]], set[tuple[str, str]]]:
    """Quick-owned ids scoped by symbol; trade order ids kept separate from algo ids.

    Returns ``(cids, order_ids, algo_ids)`` where the id sets are ``(symbol, id)``.
    Mixing algoIds into trade orderId exclusion hid unrelated owner fills that
    shared a numeric id on another symbol.
    """
    cids: set[str] = set()
    order_ids: set[tuple[str, str]] = set()
    algo_ids: set[tuple[str, str]] = set()
    _ALGO_STEPS = frozenset(
        {
            "stop",
            "tp",
            "recover_slr",
            "dup_stop_cancel",
            "orphan_cancel",
            "ms",
            "move_stop",
        }
    )
    rows = conn.execute(
        """
        SELECT l.client_order_id, l.order_or_algo_id, l.step, i.preview_json, i.symbol
        FROM assist_exec_log l
        JOIN assist_exec_intent i ON i.id = l.intent_id
        """
    ).fetchall()
    for row in rows:
        try:
            preview = json.loads(str(row["preview_json"] or "{}"))
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(preview, dict) or preview.get("origin") != "quick":
            continue
        sym = str(row["symbol"] or preview.get("symbol") or "").upper()
        if row["client_order_id"]:
            cids.add(str(row["client_order_id"]))
        oid = str(row["order_or_algo_id"] or "")
        if not oid or not sym:
            continue
        step = str(row["step"] or "")
        if step in _ALGO_STEPS or step.endswith("_algo"):
            algo_ids.add((sym, oid))
        else:
            order_ids.add((sym, oid))
    return cids, order_ids, algo_ids


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
    asset_prices: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Fetch Binance history and build assist_real_trade rows in memory. No writes.

    ``asset_prices`` maps commission assets (e.g. ``BNB`` / ``BNBUSDT``) to a
    USDT price for fee conversion. Prefer fill-time 1m close when building the
    map; omit an asset to leave its fee incomplete for day-loss R.
    """
    end = int(until_ms if until_ms is not None else now_ms())
    start = int(since_ms)
    fee_prices = asset_prices
    market_logs = _log_market_by_client(conn)
    move_stop_by_cid = _move_stop_intent_by_client(conn)
    logged_cids = _logged_client_ids(conn)
    quick_cids, quick_order_ids, _quick_algo_ids = _quick_owned_client_and_order_ids(conn)
    income_cache = _complete_income_cache(conn, start)
    frozen_missing = _frozen_missing_income(conn, start, end)
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

        # Resolve non-USDT commission prices (injected map wins; else client 1m close).
        prices: dict[str, float] = dict(fee_prices or {})
        lookup = getattr(client, "asset_usdt_close_1m", None)
        if callable(lookup):
            for fill in chron_trades:
                asset = str(fill.get("commissionAsset") or "").upper()
                if (
                    not asset
                    or asset == "USDT"
                    or asset in USDT_PEGGED_FEE_ASSETS
                    or asset in prices
                    or f"{asset}USDT" in prices
                ):
                    continue
                try:
                    fee_raw = float(fill.get("commission") or 0)
                except (TypeError, ValueError):
                    fee_raw = 0.0
                if not fee_raw:
                    continue
                px = lookup(asset, int(fill.get("time") or 0))
                try:
                    px_f = float(px) if px is not None else 0.0
                except (TypeError, ValueError):
                    px_f = 0.0
                if px_f > 0:
                    prices[asset] = px_f

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
            intent_id = str(log["intent_id"])
            meta = _intent_preview(conn, intent_id)
            preview = meta.get("preview") or {}
            # Bot Quick never becomes assist_real_trade / day-loss R / main journal.
            # Skip here; quick_owned_* also blocks the unmatched loop for SL/TP/cls.
            if isinstance(preview, dict) and preview.get("origin") == "quick":
                continue
            mkt_entry_order_ids.add(oid)
            side = str(meta.get("side") or preview.get("side") or "").lower()
            if side in {"buy", "sell"}:
                side = "long" if side == "buy" else "short"
            if side not in {"long", "short"}:
                side = "long" if str(order.get("side") or "").upper() == "BUY" else "short"
            signal_id = linked_signal_id(preview)
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
                    "fill_realized_usdt": 0.0,
                    "pnl_trade_ids": set(),
                    "commission_usdt": 0.0,
                    "other_assets": [],
                    "entry_fills": fills,
                }
            )

        # Entry commission (full).
        for entry in open_entries:
            for f in entry["entry_fills"]:
                usdt, other = _fee_usdt_and_other(f, fraction=1.0, asset_prices=prices)
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
                try:
                    pnl = float(t.get("realizedPnl") or 0)
                except (TypeError, ValueError):
                    pnl = 0.0
                entry["fill_realized_usdt"] += pnl * fraction
                if tid and _realized_pnl_nonzero(t):
                    entry["pnl_trade_ids"].add(tid)
                usdt, other = _fee_usdt_and_other(t, fraction=fraction, asset_prices=prices)
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
            income_status: str | None = None
            exit_time: int | None
            exit_avg: float | None
            order_key = str(entry["entry_order_id"])
            cached = income_cache.get(order_key)
            frozen = frozen_missing.get(order_key)
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
                fill_realized = float(entry["fill_realized_usdt"])
                if cached is not None:
                    funding_usdt = cached["funding_usdt"]
                    realized = cached["realized_pnl_usdt"]
                    income_status = "complete"
                elif frozen is not None:
                    funding_usdt = frozen["funding_usdt"]
                    realized = frozen["realized_pnl_usdt"]
                    income_status = "missing"
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
                    matched = _sum_income_by_trade(income_pnl)
                    needed = {int(tid) for tid in entry["pnl_trade_ids"]}
                    income_status = _income_status_for_close(
                        needed=needed,
                        matched=matched,
                        exit_time_ms=exit_time,
                        now_ms=end,
                    )
                    if income_status == "complete" and needed:
                        realized = _scaled_income(matched, entry["exit_trade_fractions"])
                    else:
                        # Fills already carry realizedPnl. Income replaces that
                        # only once every nonzero exit fill has a row.
                        realized = fill_realized
            else:
                exit_time = None
                exit_avg = None
                exit_kind = None

            loss = entry.get("loss_at_stop")
            net_usdt = realized + commission_usdt + funding_usdt
            net_r_real = None
            # Known parts only (realized + converted USDT fees + funding). A
            # missing non-USDT fee slightly overstates net so day-loss still
            # counts the loss; commission_other_asset keeps mean-R excluded.
            if closed and loss is not None and float(loss) > 0:
                net_r_real = net_usdt / float(loss)
            risk_actual, net_r_actual = _risk_actual(
                closed=closed,
                planned_stop=entry.get("planned_stop"),
                entry_fill_avg=entry.get("entry_fill_avg"),
                entry_qty=entry.get("entry_qty"),
                net_usdt=net_usdt,
            )
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
                trade_ids = sorted(int(tid) for tid in entry["exit_trade_ids"] if tid)
                entry_trade_ids = sorted(int(tid) for tid in entry["entry_trade_ids"] if tid)
                if trade_ids:
                    exit_meta["trade_ids"] = trade_ids
                if entry_trade_ids:
                    exit_meta["entry_trade_ids"] = entry_trade_ids
                if entry.get("stop_detail"):
                    exit_meta["stop_detail"] = entry["stop_detail"]
                if entry.get("stop_source"):
                    exit_meta["stop_source"] = entry["stop_source"]
                # Prefer first stop detail seen.
                for d in entry["exit_details"]:
                    if d and "stop_detail" not in exit_meta:
                        exit_meta["stop_detail"] = d

            stop_detail = entry.get("stop_detail")
            if stop_detail is None:
                for item in entry["exit_details"]:
                    if item:
                        stop_detail = item
                        break
            exit_source = None
            if closed:
                exit_source = exit_source_for_close(
                    conn,
                    exit_kind=exit_kind,
                    stop_detail=str(stop_detail) if stop_detail else None,
                    symbol=symbol,
                    side=str(entry["side"]),
                    entry_time_ms=entry.get("entry_time_ms"),
                    exit_time_ms=exit_time,
                    orders_by_id=orders_by_id,
                    exit_order_ids=[str(oid) for oid in entry["exit_order_ids"]],
                    move_stop_by_cid=move_stop_by_cid,
                )
            atr_at_entry = lookup_atr_at_entry(
                conn,
                symbol=symbol,
                side=str(entry["side"]),
                entry_time_ms=entry.get("entry_time_ms"),
            )
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
                    "risk_actual_usdt": risk_actual,
                    "net_r_actual": net_r_actual,
                    "net_r_virtual": net_r_virtual,
                    "slip_entry_bps": slip_entry,
                    "drift_signal_bps": drift,
                    "slip_stop_bps": slip_stop,
                    "income_status": income_status,
                    "exit_source": exit_source,
                    "atr_at_entry": atr_at_entry,
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
            # Exclude Quick-owned trade orders/fills (symbol-scoped; never match algo ids).
            if (symbol, oid) in quick_order_ids or (cid and cid in quick_cids):
                continue
            fill_cids = {str(f.get("clientOrderId") or "") for f in fills}
            if fill_cids & quick_cids:
                continue
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
                    "risk_actual_usdt": None,
                    "net_r_actual": None,
                    "net_r_virtual": None,
                    "slip_entry_bps": None,
                    "drift_signal_bps": None,
                    "slip_stop_bps": None,
                    "income_status": None,
                    "exit_source": None,
                    "atr_at_entry": None,
                    "status": "unmatched",
                    "updated_at_ms": end,
                }
            )

    _label_pair_roles(rows_out)
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
    "risk_actual_usdt",
    "net_r_actual",
    "net_r_virtual",
    "slip_entry_bps",
    "drift_signal_bps",
    "slip_stop_bps",
    "income_status",
    "exit_source",
    "atr_at_entry",
    "status",
    "updated_at_ms",
)


def sync_real_trades(
    conn: Any,
    client: BinanceFuturesClient,
    *,
    since_ms: int = SPEC_START_MS,
    until_ms: int | None = None,
    asset_prices: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Build all rows, then replace [since_ms, …) in one transaction. All or nothing."""
    start = int(since_ms)
    rows = build_real_rows(conn, client, since_ms=start, until_ms=until_ms, asset_prices=asset_prices)
    with transaction(conn):
        # A stored ATR must survive the delete/insert. COALESCE(preserved, built).
        preserved: dict[str, Any] = {}
        prior = conn.execute(
            """
            SELECT entry_order_id, atr_at_entry FROM assist_real_trade
            WHERE entry_time_ms >= ? AND atr_at_entry IS NOT NULL
            """,
            (start,),
        )
        for raw in prior:
            item = dict(raw) if hasattr(raw, "keys") else None
            if item is None:
                preserved[str(raw[0])] = raw[1]
            else:
                preserved[str(item["entry_order_id"])] = item["atr_at_entry"]
        conn.execute(
            "DELETE FROM assist_real_trade WHERE entry_time_ms >= ?",
            (start,),
        )
        placeholders = ",".join("?" for _ in _INSERT_COLS)
        col_sql = ",".join(_INSERT_COLS)
        for row in rows:
            oid = str(row.get("entry_order_id") or "")
            built = row.get("atr_at_entry")
            stored_atr = preserved.get(oid)
            # COALESCE(preserved, built): a non-NULL snapshot is never replaced.
            row["atr_at_entry"] = stored_atr if stored_atr is not None else built
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


def _time_in_window(raw: Any, *, since_ms: int, until_ms: int | None) -> bool:
    if raw is None:
        return False
    stamp = int(raw)
    if stamp < int(since_ms):
        return False
    if until_ms is not None and stamp > int(until_ms):
        return False
    return True


def _in_report_window(row: dict[str, Any], *, since_ms: int, until_ms: int | None) -> bool:
    """Entry-time window. Unmatched orders have no exit."""
    return _time_in_window(row.get("entry_time_ms"), since_ms=since_ms, until_ms=until_ms)


def _exit_in_report_window(row: dict[str, Any], *, since_ms: int, until_ms: int | None) -> bool:
    """Closed real rows belong to the window by exit time, inclusive."""
    return _time_in_window(row.get("exit_time_ms"), since_ms=since_ms, until_ms=until_ms)


def _load_real_rows(conn: Any) -> list[dict[str, Any]]:
    """Every synced real row. Pair roles need the full history, not one window."""
    col_sql = ", ".join(_INSERT_COLS)
    raw = conn.execute(f"SELECT {col_sql} FROM assist_real_trade").fetchall()
    rows: list[dict[str, Any]] = []
    for item in raw:
        if hasattr(item, "keys"):
            rows.append(dict(item))
        else:
            rows.append(dict(zip(_INSERT_COLS, item, strict=True)))
    return rows


def _income_blocks_r(row: dict[str, Any]) -> bool:
    """Pending and missing trades stay out of the R averages."""
    return str(row.get("income_status") or "") in _R_INCOME_SKIP


def _virtual_gap_counts(
    conn: Any,
    *,
    since_ms: int,
    until_ms: int | None,
    linked_signal_ids: set[str],
) -> tuple[int, int]:
    """Sent tracks whose exit is in the window: (no real trade, risk <= 0)."""
    sql = """
        SELECT t.signal_id, t.entry, t.stop, s.loss_at_stop
        FROM assist_track t
        LEFT JOIN assist_signal s ON s.id = t.signal_id
        WHERE t.exit_at_ms IS NOT NULL
          AND t.exit_at_ms >= ?
          AND t.cohort = ?
          AND t.kind = 'sent'
    """
    params: list[Any] = [int(since_ms), TRACK_COHORT]
    if until_ms is not None:
        sql += " AND t.exit_at_ms <= ?"
        params.append(int(until_ms))
    unpaired = 0
    dropped = 0
    for row in conn.execute(sql, params):
        if hasattr(row, "keys"):
            sid, entry, stop, loss = row["signal_id"], row["entry"], row["stop"], row["loss_at_stop"]
        else:
            sid, entry, stop, loss = row[0], row[1], row[2], row[3]
        if virtual_risk_unusable(entry=entry, stop=stop, loss_at_stop=loss):
            dropped += 1
            continue
        if str(sid) not in linked_signal_ids:
            unpaired += 1
    return unpaired, dropped


def _is_diff_pair(row: dict[str, Any]) -> bool:
    """Closed sent-track row used in the real-vs-virtual difference."""
    if _income_blocks_r(row):
        return False
    if str(row.get("income_status") or "") != "complete":
        return False
    if row.get("re_entry"):
        return False
    if row.get("commission_other_asset"):
        return False
    if row.get("net_r_real") is None or row.get("net_r_virtual") is None:
        return False
    return True


def summarize_real_vs_virtual(
    conn: Any,
    *,
    since_ms: int = SPEC_START_MS,
    until_ms: int | None = None,
    rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Window stats by exit time. Pair roles are labelled on the full history first."""
    all_rows = _load_real_rows(conn) if rows is None else list(rows)
    _label_pair_roles(all_rows)
    open_n = sum(1 for r in all_rows if str(r.get("status") or "") == "open")
    window_rows: list[dict[str, Any]] = []
    for r in all_rows:
        status = str(r.get("status") or "")
        if status == "open":
            continue
        if status == "unmatched":
            if _in_report_window(r, since_ms=since_ms, until_ms=until_ms):
                window_rows.append(r)
            continue
        if status == "closed" and _exit_in_report_window(r, since_ms=since_ms, until_ms=until_ms):
            window_rows.append(r)
    rows = window_rows

    closed_cohort: list[dict[str, Any]] = []
    unmatched_by: dict[str, int] = {
        "jv_before_start": 0,
        "jv_no_log": 0,
        "owner": 0,
    }
    unknown_n = 0
    unlinked_n = 0
    stop_trigger_missing_n = 0
    non_usdt_fee_n = 0
    income_pending_n = 0
    income_missing_n = 0

    for r in rows:
        status = str(r.get("status") or "")
        if status == "unmatched":
            reason = str(r.get("ref_source") or "owner")
            if reason not in unmatched_by:
                reason = "owner"
            unmatched_by[reason] += 1
            continue
        if status != "closed":
            continue
        income_state = str(r.get("income_status") or "")
        if income_state == "pending":
            income_pending_n += 1
        elif income_state == "missing":
            income_missing_n += 1
        if _income_blocks_r(r) or income_state != "complete":
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
    actuals: list[float] = []
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
        if net_r is not None and not other and not _income_blocks_r(r):
            nets.append(float(net_r))
            actual = r.get("net_r_actual")
            if actual is not None:
                actuals.append(float(actual))
            if virt is not None and not r.get("re_entry"):
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
    linked_ids = {
        str(r["signal_id"])
        for r in all_rows
        if r.get("signal_id")
        and str(r.get("status") or "") in {"closed", "open"}
        and str(r.get("side") or "") in {"long", "short"}
    }
    n_virtual_without, virtual_dropped_n = _virtual_gap_counts(
        conn,
        since_ms=since_ms,
        until_ms=until_ms,
        linked_signal_ids=linked_ids,
    )
    pair_rows = [r for r in closed_cohort if _is_diff_pair(r)]
    closed_real_n = sum(
        1
        for r in rows
        if str(r.get("status") or "") == "closed"
        and str(r.get("side") or "") in {"long", "short"}
        and str(r.get("income_status") or "") == "complete"
    )
    n_re_entry = sum(1 for r in rows if r.get("re_entry"))
    entry_slip_rs: list[float] = []
    for r in rows:
        if str(r.get("status") or "") != "closed":
            continue
        if _income_blocks_r(r) or str(r.get("income_status") or "") != "complete":
            continue
        value = slip_entry_r(
            side=str(r.get("side") or ""),
            entry_fill_avg=r.get("entry_fill_avg"),
            plan_entry=r.get("signal_entry"),
            planned_stop=r.get("planned_stop"),
        )
        if value is not None:
            entry_slip_rs.append(value)
    window_end = int(until_ms) if until_ms is not None else now_ms()
    return {
        "closed_n": len(closed_cohort),
        "mean_net_r": statistics.mean(nets) if nets else None,
        "mean_net_r_actual": statistics.mean(actuals) if actuals else None,
        "mean_slip_entry_r": statistics.mean(entry_slip_rs) if entry_slip_rs else None,
        "slip_entry_r_n": len(entry_slip_rs),
        "mean_diff_r": statistics.mean(diffs) if diffs else None,
        "n_pairs": len(pair_rows),
        "n_real_without_pair": closed_real_n - len(pair_rows),
        "n_virtual_without_real": n_virtual_without,
        "n_re_entry": n_re_entry,
        "virtual_dropped_n": virtual_dropped_n,
        "window_start_ms": int(since_ms),
        "window_end_ms": window_end,
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
        "income_pending_n": income_pending_n,
        "income_missing_n": income_missing_n,
        "target_n": REAL_SAMPLE_TARGET,
    }


def _r_text(value: Any) -> str:
    if value is None:
        return "—"
    return _signed_r(float(value))


def format_bkk_stamp(ms: int) -> str:
    """Bangkok clock for one income row, e.g. ``09 พ.ย. 14:05``."""
    stamp = datetime.fromtimestamp(int(ms) / 1000.0, tz=_BANGKOK)
    month = _THAI_MONTHS[stamp.month - 1]
    return f"{stamp.day:02d} {month} {stamp.hour:02d}:{stamp.minute:02d}"


def _fmt_usdt(value: Any) -> str:
    if value is None:
        return "—"
    number = float(value)
    if number > 0:
        return f"+{number:.2f}"
    return f"{number:.2f}"


def _report_window(summary: dict[str, Any]) -> str:
    start = summary.get("window_start_ms")
    end = summary.get("window_end_ms")
    if start is None or end is None:
        return "—"
    return format_bkk_window(int(start), int(end))


def format_real_lines(summary: dict[str, Any]) -> list[str]:
    """Real-vs-virtual block. Each line states the unit, the Bangkok window, and n."""
    window = _report_window(summary)
    n = int(summary.get("closed_n") or 0)
    target = int(summary.get("target_n") or REAL_SAMPLE_TARGET)
    planned = _r_text(summary.get("mean_net_r"))
    actual = _r_text(summary.get("mean_net_r_actual"))
    entry_n = int(summary.get("entry_slip_n") or 0)
    stop_n = int(summary.get("stop_n") or 0)
    slip_r_n = int(summary.get("slip_entry_r_n") or 0)
    mean_e_s = _fmt_bps(summary.get("mean_slip_entry_bps"))
    p90_e_s = _fmt_bps(summary.get("p90_slip_entry_bps"), n=entry_n)
    mean_d_s = _fmt_bps(summary.get("mean_drift_bps"))
    mean_s_s = _fmt_bps(summary.get("mean_slip_stop_bps"))
    p90_s_s = _fmt_bps(summary.get("p90_slip_stop_bps"), n=stop_n)
    lines = [
        (
            f"ไม้จริง {window}: n={n} · เฉลี่ย {planned} (planned) · {actual} (actual) · "
            f"ต้องการอย่างน้อย {target} ไม้ก่อนสรุป"
        ),
        (
            f"slip เข้าเป็น R {window}: n={slip_r_n} · "
            f"เฉลี่ย {_r_text(summary.get('mean_slip_entry_r'))} (บวก = เข้าแย่กว่าแผน)"
        ),
        (
            f"คู่จริง-จำลอง {window}: n={int(summary.get('n_pairs') or 0)} · "
            f"ต่างเฉลี่ย (จริง − จำลอง) {_r_text(summary.get('mean_diff_r'))}"
        ),
        f"ไม้จริงที่ไม่มีคู่ {window}: n={int(summary.get('n_real_without_pair') or 0)}",
        f"ไม้จำลองที่ไม่มีไม้จริง {window}: n={int(summary.get('n_virtual_without_real') or 0)}",
        f"re_entry {window}: n={int(summary.get('n_re_entry') or 0)}",
        (f"ไม้จำลองที่ความเสี่ยง ≤ 0 {window}: n={int(summary.get('virtual_dropped_n') or 0)} · ไม่นับในค่าเฉลี่ย R"),
        (
            f"slip เข้า {window}: n={entry_n} · เฉลี่ย {mean_e_s} bps · p90 {p90_e_s} bps "
            f"(อ้างอิง confirm_mark {int(summary.get('confirm_n') or 0)} / "
            f"preview {int(summary.get('preview_n') or 0)}) · "
            f"ราคาวิ่งจากสัญญาณ เฉลี่ย {mean_d_s} bps"
        ),
        f"slip stop {window}: n={stop_n} · เฉลี่ย {mean_s_s} bps · p90 {p90_s_s} bps",
        f"ไม้ที่ยังถือ {window}: n={int(summary.get('open_n') or 0)}",
        (
            f"จับคู่ไม่ได้ {window}: n={int(summary.get('unmatched_n') or 0)} "
            f"(ก่อนเริ่ม {int(summary.get('unmatched_jv_before_start') or 0)} · "
            f"ไม่มี log {int(summary.get('unmatched_jv_no_log') or 0)} · "
            f"เจ้าของ {int(summary.get('unmatched_owner') or 0)}) · "
            f"exit ไม่ทราบชนิด n={int(summary.get('unknown_n') or 0)} · "
            f"ไม่มี track n={int(summary.get('unlinked_n') or 0)} · "
            f"ค่าธรรมเนียมนอก USDT n={int(summary.get('non_usdt_fee_n') or 0)} · "
            f"stop ไม่มี trigger n={int(summary.get('stop_trigger_missing_n') or 0)}"
        ),
    ]
    lines.extend(_reconcile_lines(summary, window))
    return lines


def _reconcile_lines(summary: dict[str, Any], window: str) -> list[str]:
    """Income counts and the Binance-vs-trades block. Detail lines follow."""
    rec = summary.get("reconcile")
    if not isinstance(rec, dict):
        rec = {}
    totals = rec.get("totals")
    if not isinstance(totals, dict):
        totals = {}
    parts: list[str] = []
    for category in INCOME_CATEGORIES:
        item = totals.get(category)
        binance = None
        attributed = None
        if isinstance(item, dict):
            binance = item.get("binance")
            attributed = item.get("trades")
        parts.append(f"{category} ไบนานซ์ {_fmt_usdt(binance)} · ในไม้ {_fmt_usdt(attributed)}")
    income_n = int(rec.get("income_n") or 0)
    un_n = int(rec.get("unattributed_n") or 0)
    lines = [
        f"รอข้อมูล income {window}: n={int(summary.get('income_pending_n') or 0)}",
        f"income ขาด {window}: n={int(summary.get('income_missing_n') or 0)}",
        f"กระทบยอด income {window}: n={income_n} · " + " · ".join(parts),
        f"จับ income ไม่ได้ {window}: n={un_n}",
    ]
    quick_n = int(rec.get("quick_n") or 0)
    if quick_n > 0:
        lines.append(f"income ของไม้ quick (แยก) {window}: n={quick_n}")
    shown = list(rec.get("unattributed") or [])[:10]
    for item in shown:
        if not isinstance(item, dict):
            continue
        lines.append(
            "· "
            f"{item.get('symbol')} {format_bkk_stamp(int(item.get('time_ms') or 0))} "
            f"{item.get('category')} {_fmt_usdt(item.get('amount'))}"
        )
    rest = un_n - len(shown)
    if rest > 0:
        lines.append(f"และอีก {rest} รายการ")
    missing = rec.get("missing")
    if isinstance(missing, list):
        for item in missing:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"· {item.get('symbol')} {format_bkk_stamp(int(item.get('time_ms') or 0))} income ขาด"
            )
    return lines


def _exit_meta(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        data = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if isinstance(data, dict):
        return data
    return {}


def _id_list(raw: Any) -> list[int]:
    if not isinstance(raw, list):
        return []
    out: list[int] = []
    for item in raw:
        try:
            tid = int(item)
        except (TypeError, ValueError):
            continue
        if tid > 0:
            out.append(tid)
    return out


def _income_row_key(row: dict[str, Any]) -> tuple[str, str, int, str, str, str]:
    return (
        str(row.get("symbol") or "").upper(),
        str(row.get("incomeType") or ""),
        int(row.get("time") or 0),
        str(row.get("tradeId") or ""),
        str(row.get("income") or ""),
        str(row.get("tranId") or ""),
    )


def build_income_reconcile(
    client: BinanceFuturesClient,
    rows: list[dict[str, Any]],
    *,
    since_ms: int,
    until_ms: int,
    is_quick: Callable[[dict[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    """Binance income for ASSIST_SYMBOLS versus the sums stored on trades.

    Read-only. Does not place, cancel, or edit an order.
    """
    symbols = tuple(ASSIST_SYMBOLS)
    symbol_set = {str(sym).upper() for sym in symbols}
    fetched: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int, str, str, str]] = set()
    start = int(since_ms)
    end = int(until_ms)
    quick_n = 0
    for symbol in symbols:
        for category in INCOME_CATEGORIES:
            page = fetch_income_range(
                client,
                symbol=symbol,
                start_ms=start,
                end_ms=end + 1,
                income_type=category,
            )
            for row in page:
                sym = str(row.get("symbol") or "").upper()
                if sym not in symbol_set:
                    continue
                if str(row.get("incomeType") or "") != category:
                    continue
                tms = int(row.get("time") or 0)
                if tms < start or tms > end:
                    continue
                key = _income_row_key(row)
                if key in seen:
                    continue
                seen.add(key)
                if is_quick is not None and is_quick(row):
                    quick_n += 1
                    continue
                fetched.append(row)

    realized_ids: set[tuple[str, int]] = set()
    commission_ids: set[tuple[str, int]] = set()
    funding_spans: list[tuple[str, int, int]] = []
    for row in rows:
        status = str(row.get("status") or "")
        side = str(row.get("side") or "")
        if status not in {"closed", "open"} or side not in {"long", "short"}:
            continue
        sym = str(row.get("symbol") or "").upper()
        if sym not in symbol_set:
            continue
        meta = _exit_meta(row.get("exit_order_ids"))
        for tid in _id_list(meta.get("trade_ids")):
            realized_ids.add((sym, tid))
            commission_ids.add((sym, tid))
        for tid in _id_list(meta.get("entry_trade_ids")):
            commission_ids.add((sym, tid))
        entry_ms = row.get("entry_time_ms")
        if entry_ms is None:
            continue
        exit_ms = row.get("exit_time_ms")
        span_end = int(exit_ms) if exit_ms is not None else end
        funding_spans.append((sym, int(entry_ms), span_end))

    totals = {category: {"binance": 0.0, "trades": 0.0} for category in INCOME_CATEGORIES}
    unattributed: list[dict[str, Any]] = []
    for row in fetched:
        category = str(row.get("incomeType") or "")
        if category not in totals:
            continue
        try:
            amount = float(row.get("income") or 0)
        except (TypeError, ValueError):
            amount = 0.0
        totals[category]["binance"] += amount
        sym = str(row.get("symbol") or "").upper()
        tms = int(row.get("time") or 0)
        attributed = False
        if category == "FUNDING_FEE":
            attributed = any(
                span_sym == sym and opened < tms <= closed_ms for span_sym, opened, closed_ms in funding_spans
            )
        else:
            try:
                tid = int(row.get("tradeId") or 0)
            except (TypeError, ValueError):
                tid = 0
            pool = realized_ids if category == "REALIZED_PNL" else commission_ids
            attributed = tid > 0 and (sym, tid) in pool
        if not attributed:
            unattributed.append(
                {
                    "symbol": sym,
                    "time_ms": tms,
                    "category": category,
                    "amount": amount,
                }
            )
    unattributed.sort(key=lambda item: (int(item["time_ms"]), str(item["symbol"]), str(item["category"])))

    for row in rows:
        if str(row.get("status") or "") != "closed":
            continue
        if str(row.get("side") or "") not in {"long", "short"}:
            continue
        if str(row.get("symbol") or "").upper() not in symbol_set:
            continue
        if not _exit_in_report_window(row, since_ms=start, until_ms=end):
            continue
        if row.get("realized_pnl_usdt") is not None:
            totals["REALIZED_PNL"]["trades"] += float(row["realized_pnl_usdt"])
        if row.get("commission_usdt") is not None:
            totals["COMMISSION"]["trades"] += float(row["commission_usdt"])
        if row.get("funding_usdt") is not None:
            totals["FUNDING_FEE"]["trades"] += float(row["funding_usdt"])

    missing: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("status") or "") != "closed":
            continue
        if str(row.get("income_status") or "") != "missing":
            continue
        if str(row.get("side") or "") not in {"long", "short"}:
            continue
        if not _exit_in_report_window(row, since_ms=start, until_ms=end):
            continue
        missing.append(
            {
                "symbol": str(row.get("symbol") or "").upper(),
                "time_ms": int(row.get("exit_time_ms") or 0),
                "entry_order_id": str(row.get("entry_order_id") or ""),
            }
        )
    missing.sort(key=lambda item: (int(item["time_ms"]), str(item["symbol"]), str(item["entry_order_id"])))
    out: dict[str, Any] = {
        "totals": totals,
        "income_n": len(fetched),
        "unattributed": unattributed,
        "unattributed_n": len(unattributed),
        "missing": missing,
    }
    if quick_n > 0:
        out["quick_n"] = quick_n
    return out


def _income_refresh_due(conn: Any, now_ms: int) -> bool:
    row = conn.execute(
        """
        SELECT last_attempt_ms FROM assist_journal_sync
        WHERE symbol=? AND position_side=? AND closed_at_ms=?
        """,
        (_INCOME_REFRESH_SYMBOL, _INCOME_REFRESH_SIDE, _INCOME_REFRESH_CLOSED_AT),
    ).fetchone()
    if row is None:
        return True
    last = row["last_attempt_ms"] if hasattr(row, "keys") else row[0]
    if last is None:
        return True
    return int(now_ms) - int(last) >= INCOME_REFRESH_GAP_MS


def _mark_income_refresh(conn: Any, now_ms: int) -> None:
    conn.execute(
        """
        INSERT INTO assist_journal_sync (
            symbol, position_side, closed_at_ms, attempts, last_attempt_ms, gave_up_logged
        ) VALUES (?, ?, ?, 1, ?, 0)
        ON CONFLICT(symbol, position_side, closed_at_ms) DO UPDATE SET
            attempts = attempts + 1,
            last_attempt_ms = excluded.last_attempt_ms
        """,
        (_INCOME_REFRESH_SYMBOL, _INCOME_REFRESH_SIDE, _INCOME_REFRESH_CLOSED_AT, int(now_ms)),
    )
    conn.commit()


def _due_income_rows(conn: Any, now_ms: int) -> list[dict[str, Any]]:
    """NULL, pending, or missing closes whose exit is still inside 7 days."""
    cutoff = int(now_ms) - INCOME_REFRESH_LOOKBACK_MS
    cur = conn.execute(
        """
        SELECT entry_order_id, entry_time_ms, exit_time_ms
        FROM assist_real_trade
        WHERE status='closed'
          AND exit_time_ms IS NOT NULL
          AND exit_time_ms >= ?
          AND (income_status IS NULL OR income_status IN ('pending', 'missing'))
        """,
        (cutoff,),
    )
    out: list[dict[str, Any]] = []
    for row in cur.fetchall():
        if hasattr(row, "keys"):
            out.append(dict(row))
        else:
            out.append(
                {
                    "entry_order_id": row[0],
                    "entry_time_ms": row[1],
                    "exit_time_ms": row[2],
                }
            )
    return out


def _upsert_real_row(conn: Any, row: dict[str, Any]) -> None:
    placeholders = ",".join("?" for _ in _INSERT_COLS)
    col_sql = ",".join(_INSERT_COLS)
    updates = ", ".join(
        "atr_at_entry=COALESCE(assist_real_trade.atr_at_entry, excluded.atr_at_entry)"
        if col == "atr_at_entry"
        else f"{col}=excluded.{col}"
        for col in _INSERT_COLS
        if col != "entry_order_id"
    )
    conn.execute(
        f"INSERT INTO assist_real_trade ({col_sql}) VALUES ({placeholders}) "
        f"ON CONFLICT(entry_order_id) DO UPDATE SET {updates}",
        [row.get(col) for col in _INSERT_COLS],
    )


def refresh_incomplete_income(
    conn: Any,
    client: BinanceFuturesClient,
    *,
    now_ms: int,
) -> dict[str, Any]:
    """Re-read incomplete closes from the last 7 days, at most once per 30 minutes.

    A failure returns without writing. Callers must not let it stop watch.
    Missing rows older than 7 days are left as they are.
    """
    if not _income_refresh_due(conn, now_ms):
        return {"ok": True, "refreshed": False, "reason": "throttled"}
    due = _due_income_rows(conn, now_ms)
    _mark_income_refresh(conn, now_ms)
    if not due:
        return {"ok": True, "refreshed": False, "reason": "none", "n": 0}
    starts = [
        int(item["entry_time_ms"]) if item.get("entry_time_ms") is not None else int(item["exit_time_ms"])
        for item in due
    ]
    since = min(starts) - 60_000
    due_ids = {str(item["entry_order_id"]) for item in due}
    try:
        built = build_real_rows(conn, client, since_ms=since, until_ms=int(now_ms))
    except Exception as exc:  # noqa: BLE001 — watch, journal, and pauses must continue
        logger.exception("assist income refresh failed")
        return {"ok": False, "refreshed": False, "error": type(exc).__name__}
    updated: list[str] = []
    with transaction(conn):
        for row in built:
            oid = str(row.get("entry_order_id") or "")
            if oid not in due_ids or str(row.get("status") or "") != "closed":
                continue
            _upsert_real_row(conn, row)
            updated.append(oid)
    return {"ok": True, "refreshed": True, "n": len(updated), "ids": updated}


def run_real_report(
    conn: Any,
    client: BinanceFuturesClient,
    *,
    since_ms: int = SPEC_START_MS,
    until_ms: int | None = None,
    sync: bool = True,
) -> dict[str, Any]:
    """Sync (optional) and summarize. ``sync=False`` builds in memory and never writes."""
    end = int(until_ms if until_ms is not None else now_ms())
    is_quick = quick_income_predicate(conn, now_ms=end)
    if sync:
        sync_result = sync_real_trades(conn, client, since_ms=since_ms, until_ms=end)
        summary = summarize_real_vs_virtual(conn, since_ms=since_ms, until_ms=end)
        stored = _load_real_rows(conn)
        summary["reconcile"] = build_income_reconcile(
            client, stored, since_ms=since_ms, until_ms=end, is_quick=is_quick
        )
        lines = format_real_lines(summary)
        return {
            "ok": True,
            "sync": sync_result,
            "summary": summary,
            "lines": lines,
            "text": "\n".join(lines),
        }
    rows = build_real_rows(conn, client, since_ms=since_ms, until_ms=end)
    summary = summarize_real_vs_virtual(conn, since_ms=since_ms, until_ms=end, rows=rows)
    summary["reconcile"] = build_income_reconcile(
        client, rows, since_ms=since_ms, until_ms=end, is_quick=is_quick
    )
    lines = format_real_lines(summary)
    return {
        "ok": True,
        "sync": None,
        "summary": summary,
        "lines": lines,
        "text": "\n".join(lines),
        "rows": rows,
    }
