"""Additive exit telemetry. Reads and inserts only; never places or changes an order.

Stored because a later replay cannot rebuild it: the mark at advice time, the
spot wick (including the entry-hour 15m slice, which is not in SQLite), the
owner's tap or ignore, and the exchange stop that was resting then.

Realised commission and funding stay on ``assist_real_trade`` (``commission_usdt``,
``funding_usdt``). This module adds the exit source and the ATR snapshot beside them.
"""

from __future__ import annotations

import json
import logging
import uuid
from decimal import Decimal
from typing import Any

from jarvise_assist.messages import approx_r_after_fees
from jarvise_assist.trail_advice import ONE_H_MS, candle_extremes_after_entry, post_entry_extremes
from jarvise_ingest.db import load_candles_in_range

logger = logging.getLogger(__name__)

# Finer than assist_real_trade.exit_kind, which records every stop as "stop".
EXIT_SOURCES = (
    "initial_stop",
    "be_stop",
    "trail_stop",
    "moved_stop",
    "tp",
    "manual",
    "early_exit",
    "owner_exit",
    "emergency",
    "unknown",
)


def _row(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    return dict(raw)


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def resolve_exit_source(
    *,
    exit_kind: str | None,
    stop_detail: str | None,
    move_source: str | None,
    early_exit: bool,
) -> str | None:
    """Map a closed fill to the source a replay needs. Does not read the network."""
    kind = str(exit_kind or "")
    if not kind:
        return None
    if kind == "tp":
        return "tp"
    if kind == "manual":
        return "manual"
    if kind == "emergency":
        return "emergency"
    if kind == "unknown":
        return "unknown"
    if kind == "owner_exit":
        return "early_exit" if early_exit else "owner_exit"
    if kind == "stop":
        detail = str(stop_detail or "")
        if detail == "initial":
            return "initial_stop"
        if detail == "moved":
            if move_source == "breakeven":
                return "be_stop"
            if move_source == "trail":
                return "trail_stop"
            return "moved_stop"
        return "manual"
    return None


def _preview(conn: Any, intent_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT preview_json FROM assist_exec_intent WHERE id=?",
        (intent_id,),
    ).fetchone()
    if row is None:
        return {}
    raw = _row(row).get("preview_json")
    try:
        parsed = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def move_source_for_orders(
    conn: Any,
    *,
    orders_by_id: dict[str, dict[str, Any]],
    exit_order_ids: list[str],
    move_stop_by_cid: dict[str, str],
) -> str | None:
    """Preview source (breakeven/trail) of the move-stop intent that filled, if any."""
    for oid in exit_order_ids:
        order = orders_by_id.get(str(oid)) or {}
        cid = str(order.get("clientOrderId") or "")
        intent_id = move_stop_by_cid.get(cid)
        if not intent_id:
            continue
        source = _preview(conn, intent_id).get("source")
        if source in {"breakeven", "trail"}:
            return str(source)
    return None


def early_exit_was_tapped(
    conn: Any,
    *,
    symbol: str,
    side: str,
    entry_time_ms: int | None,
    exit_time_ms: int | None,
) -> bool:
    """True when a done ⚠️ exit intent in the trade window carried early_close."""
    if entry_time_ms is None or exit_time_ms is None:
        return False
    # The ⚠️ offer is logged on assist_exit_card. The exit intent preview itself
    # stays qty/entry/source/level, so this does not depend on a new preview key.
    row = conn.execute(
        """
        SELECT 1
        FROM assist_exit_response r
        INNER JOIN assist_exit_card c ON c.id = r.card_id
        WHERE c.symbol=? AND c.side=? AND r.kind='early_exit' AND r.action='tap'
          AND r.action_at_ms >= ? AND r.action_at_ms <= ?
        LIMIT 1
        """,
        (str(symbol).upper(), str(side).lower(), int(entry_time_ms), int(exit_time_ms)),
    ).fetchone()
    return row is not None


def exit_source_for_close(
    conn: Any,
    *,
    exit_kind: str | None,
    stop_detail: str | None,
    symbol: str,
    side: str,
    entry_time_ms: int | None,
    exit_time_ms: int | None,
    orders_by_id: dict[str, dict[str, Any]] | None = None,
    exit_order_ids: list[str] | None = None,
    move_stop_by_cid: dict[str, str] | None = None,
) -> str | None:
    move_source = None
    if orders_by_id is not None and exit_order_ids is not None and move_stop_by_cid is not None:
        move_source = move_source_for_orders(
            conn,
            orders_by_id=orders_by_id,
            exit_order_ids=exit_order_ids,
            move_stop_by_cid=move_stop_by_cid,
        )
    early = early_exit_was_tapped(
        conn,
        symbol=symbol,
        side=side,
        entry_time_ms=entry_time_ms,
        exit_time_ms=exit_time_ms,
    )
    return resolve_exit_source(
        exit_kind=exit_kind,
        stop_detail=stop_detail,
        move_source=move_source,
        early_exit=early,
    )


def lookup_atr_at_entry(
    conn: Any,
    *,
    symbol: str,
    side: str,
    entry_time_ms: int | None,
) -> float | None:
    """ATR frozen on the watch row nearest this entry. None when it was never snapshotted."""
    if entry_time_ms is None:
        row = conn.execute(
            """
            SELECT atr_at_entry FROM assist_watch_position
            WHERE symbol=? AND side=? AND atr_at_entry IS NOT NULL
            ORDER BY entry_time_ms DESC LIMIT 1
            """,
            (symbol.upper(), side.lower()),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT atr_at_entry FROM assist_watch_position
            WHERE symbol=? AND side=? AND atr_at_entry IS NOT NULL
            ORDER BY ABS(COALESCE(entry_time_ms, 0) - ?) ASC
            LIMIT 1
            """,
            (symbol.upper(), side.lower(), int(entry_time_ms)),
        ).fetchone()
    if row is None:
        return None
    return _f(_row(row).get("atr_at_entry"))


def _extremes(
    candles_1h: list[dict[str, Any]] | None,
    *,
    symbol: str,
    side: str,
    entry_time_ms: int | None,
    now_ms: int,
    entry_bar: bool,
) -> tuple[float | None, float | None]:
    """(wick used by the rule, last closed 1h spot close)."""
    if not candles_1h or entry_time_ms is None:
        return None, None
    if entry_bar:
        best_low, best_high = post_entry_extremes(
            candles_1h,
            symbol=symbol,
            entry_time_ms=int(entry_time_ms),
            now_ms=int(now_ms),
        )
    else:
        # 1h spot bars only. The entry-hour 15m fetch is a network call; the
        # morning flush must not add one. The decision-time card already stored
        # that wick when the advice was held.
        best_low, best_high = candle_extremes_after_entry(
            candles_1h,
            entry_time_ms=int(entry_time_ms),
            now_ms=int(now_ms),
            bar_ms=ONE_H_MS,
        )
    wick = best_high if side.lower() == "long" else best_low
    close_1h: float | None = None
    last_ts: int | None = None
    for row in candles_1h:
        try:
            ts = int(row["timestamp"])
        except (KeyError, TypeError, ValueError):
            continue
        if ts + ONE_H_MS > int(now_ms):
            continue
        if last_ts is None or ts >= last_ts:
            last_ts = ts
            close_1h = _f(row.get("close"))
    return _f(wick), close_1h


def log_card(
    conn: Any,
    *,
    symbol: str,
    position_side: str,
    side: str,
    kind: str,
    shown_at_ms: int,
    source: str | None = None,
    mark: Decimal | float | None = None,
    entry: Decimal | float | None = None,
    initial_stop: Decimal | float | None = None,
    qty: Decimal | float | None = None,
    suggested_stop: Decimal | float | None = None,
    r_now: Decimal | float | None = None,
    exchange_stop: Decimal | float | None = None,
    had_button: bool = False,
    delivered: bool = True,
    advice_id: str | None = None,
    intent_id: str | None = None,
    entry_time_ms: int | None = None,
    candles_1h: list[dict[str, Any]] | None = None,
    wick: float | None = None,
    close_1h: float | None = None,
    atr_at_entry: float | None = None,
    entry_bar: bool = True,
) -> str | None:
    """Insert one advice-card snapshot. Failures are logged and swallowed."""
    try:
        if candles_1h is None and entry_time_ms is not None and (wick is None or close_1h is None):
            candles_1h = load_candles_in_range(
                conn,
                symbol,
                "1h",
                since_ms=int(entry_time_ms),
                until_ms=int(shown_at_ms),
            )
        if wick is None or close_1h is None:
            got_wick, got_close = _extremes(
                candles_1h,
                symbol=symbol,
                side=side,
                entry_time_ms=entry_time_ms,
                now_ms=int(shown_at_ms),
                entry_bar=entry_bar,
            )
            if wick is None:
                wick = got_wick
            if close_1h is None:
                close_1h = got_close
        after: float | None = None
        if entry is not None and initial_stop is not None and qty is not None and suggested_stop is not None:
            net = approx_r_after_fees(
                side=side,
                qty=Decimal(str(qty)),
                entry=Decimal(str(entry)),
                initial_stop=Decimal(str(initial_stop)),
                new_stop=Decimal(str(suggested_stop)),
            )
            after = _f(net)
        if atr_at_entry is None:
            atr_at_entry = lookup_atr_at_entry(conn, symbol=symbol, side=side, entry_time_ms=entry_time_ms)
        card_id = f"exc_{uuid.uuid4().hex[:16]}"
        conn.execute(
            """
            INSERT INTO assist_exit_card (
                id, advice_id, intent_id, symbol, position_side, side, kind, source,
                shown_at_ms, mark, wick, close_1h, r_now, suggested_stop,
                approx_r_after_fees, exchange_stop, had_button, delivered, atr_at_entry
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                card_id,
                advice_id,
                intent_id,
                symbol.upper(),
                position_side.upper(),
                side.lower(),
                kind,
                source,
                int(shown_at_ms),
                _f(mark),
                wick,
                close_1h,
                _f(r_now),
                _f(suggested_stop),
                after,
                _f(exchange_stop),
                1 if had_button else 0,
                1 if delivered else 0,
                atr_at_entry,
            ),
        )
        conn.commit()
        return card_id
    except Exception:  # noqa: BLE001 — a log write must not change advice or orders
        logger.exception("assist exit card log failed")
        return None


def _has_early_card(conn: Any, intent_id: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM assist_exit_card
        WHERE intent_id=? AND kind='early_exit_offer' LIMIT 1
        """,
        (intent_id,),
    ).fetchone()
    return row is not None


def _card_for_intent(conn: Any, intent_id: str, kind: str) -> dict[str, Any] | None:
    card_kind = "early_exit_offer" if kind == "early_exit" else kind
    row = conn.execute(
        """
        SELECT id, shown_at_ms, symbol FROM assist_exit_card
        WHERE intent_id=? AND kind=?
        ORDER BY shown_at_ms DESC LIMIT 1
        """,
        (intent_id, card_kind),
    ).fetchone()
    if row is None:
        row = conn.execute(
            """
            SELECT id, shown_at_ms, symbol FROM assist_exit_card
            WHERE intent_id=?
            ORDER BY shown_at_ms DESC LIMIT 1
            """,
            (intent_id,),
        ).fetchone()
    if row is None:
        return None
    return _row(row)


def record_response(
    conn: Any,
    *,
    symbol: str,
    kind: str,
    action: str,
    action_at_ms: int,
    shown_at_ms: int | None = None,
    intent_id: str | None = None,
    card_id: str | None = None,
) -> None:
    """One tap, ignore, or manual-flush outcome. Duplicate (intent, action) is ignored."""
    try:
        if card_id is None and intent_id is not None:
            found = _card_for_intent(conn, intent_id, kind)
            if found is not None:
                card_id = str(found["id"])
                if shown_at_ms is None and found.get("shown_at_ms") is not None:
                    shown_at_ms = int(found["shown_at_ms"])
        latency = None
        if shown_at_ms is not None:
            latency = int(action_at_ms) - int(shown_at_ms)
        conn.execute(
            """
            INSERT OR IGNORE INTO assist_exit_response (
                id, card_id, intent_id, symbol, kind, action,
                shown_at_ms, action_at_ms, latency_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"exr_{uuid.uuid4().hex[:16]}",
                card_id,
                intent_id,
                symbol.upper(),
                kind,
                action,
                shown_at_ms,
                int(action_at_ms),
                latency,
            ),
        )
        conn.commit()
    except Exception:  # noqa: BLE001 — a log write must not change the tap path
        logger.exception("assist exit response log failed")


def record_intent_action(conn: Any, intent: dict[str, Any], *, action: str, action_at_ms: int) -> None:
    """Log a move-stop or exit tap/ignore. Entry confirms (ey:) are not exit telemetry."""
    kind = str(intent.get("kind") or "")
    if kind not in {"move_stop", "exit"}:
        return
    preview = intent.get("preview")
    response_kind = kind
    intent_id = str(intent.get("id") or "")
    if kind == "exit" and isinstance(preview, dict) and preview.get("source") == "progress":
        # ⚠️ is the progress exit whose offer row was stored. ✅ ปิดไม้ has no offer row.
        # The exit preview itself is not given an early_close key.
        if preview.get("early_close") or (intent_id and _has_early_card(conn, intent_id)):
            response_kind = "early_exit"
    record_response(
        conn,
        symbol=str(intent.get("symbol") or ""),
        kind=response_kind,
        action=action,
        action_at_ms=int(action_at_ms),
        shown_at_ms=int(intent["created_at_ms"]) if intent.get("created_at_ms") is not None else None,
        intent_id=intent_id or None,
    )


def sweep_expired_move_ignores(conn: Any, *, now_ms: int) -> None:
    """Mark move-stop previews past their TTL as ignore. Does not change intent status."""
    try:
        rows = conn.execute(
            """
            SELECT id, symbol, created_at_ms, expires_at_ms
            FROM assist_exec_intent
            WHERE kind='move_stop' AND status='preview' AND expires_at_ms < ?
            """,
            (int(now_ms),),
        ).fetchall()
    except Exception:  # noqa: BLE001
        logger.exception("assist exit ignore sweep failed")
        return
    for raw in rows:
        item = _row(raw)
        record_response(
            conn,
            symbol=str(item.get("symbol") or ""),
            kind="move_stop",
            action="ignore",
            action_at_ms=int(item["expires_at_ms"]),
            shown_at_ms=int(item["created_at_ms"]) if item.get("created_at_ms") is not None else None,
            intent_id=str(item.get("id") or "") or None,
        )


def note_early_exit_ignores(
    conn: Any,
    closed: list[tuple[str, str]],
    *,
    now_ms: int,
) -> None:
    """A ⚠️ offer with no tap, once the position is gone, is an ignore."""
    for symbol, position_side in closed:
        try:
            rows = conn.execute(
                """
                SELECT id, intent_id, symbol, shown_at_ms
                FROM assist_exit_card
                WHERE symbol=? AND position_side=? AND kind='early_exit_offer'
                """,
                (symbol.upper(), position_side.upper()),
            ).fetchall()
        except Exception:  # noqa: BLE001
            logger.exception("assist early-exit ignore lookup failed")
            continue
        for raw in rows:
            item = _row(raw)
            intent_id = item.get("intent_id")
            tapped = None
            if intent_id:
                tapped = conn.execute(
                    """
                    SELECT 1 FROM assist_exit_response
                    WHERE intent_id=? AND action='tap' LIMIT 1
                    """,
                    (str(intent_id),),
                ).fetchone()
            if tapped is not None:
                continue
            record_response(
                conn,
                symbol=str(item.get("symbol") or symbol),
                kind="early_exit",
                action="ignore",
                action_at_ms=int(now_ms),
                shown_at_ms=int(item["shown_at_ms"]) if item.get("shown_at_ms") is not None else None,
                intent_id=str(intent_id) if intent_id else None,
                card_id=str(item["id"]),
            )


def note_exchange_stop(
    conn: Any,
    *,
    symbol: str,
    position_side: str,
    observed_at_ms: int,
    stop_price: Decimal | float | None,
    covered_qty: Decimal | float | None,
    position_qty: Decimal | float | None,
) -> None:
    """Append the resting protective stop when it changes. Same value is not repeated."""
    try:
        price = _f(stop_price)
        covered = _f(covered_qty)
        qty = _f(position_qty)
        last = conn.execute(
            """
            SELECT stop_price, covered_qty FROM assist_exchange_stop
            WHERE symbol=? AND position_side=?
            ORDER BY observed_at_ms DESC, id DESC LIMIT 1
            """,
            (symbol.upper(), position_side.upper()),
        ).fetchone()
        if last is not None:
            prev = _row(last)
            if _f(prev.get("stop_price")) == price and _f(prev.get("covered_qty")) == covered:
                return
        conn.execute(
            """
            INSERT INTO assist_exchange_stop (
                symbol, position_side, observed_at_ms, stop_price, covered_qty, position_qty
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (symbol.upper(), position_side.upper(), int(observed_at_ms), price, covered, qty),
        )
        conn.commit()
    except Exception:  # noqa: BLE001 — stop history must not change the watch decision
        logger.exception("assist exchange stop log failed")


def note_atr_at_entry(
    conn: Any,
    *,
    symbol: str,
    position_side: str,
    atr: Decimal | float,
) -> None:
    """Freeze ATR once per watch row. Later passes do not overwrite it."""
    try:
        conn.execute(
            """
            UPDATE assist_watch_position
            SET atr_at_entry=?
            WHERE symbol=? AND position_side=? AND atr_at_entry IS NULL
            """,
            (float(atr), symbol.upper(), position_side.upper()),
        )
        conn.commit()
    except Exception:  # noqa: BLE001
        logger.exception("assist atr snapshot failed")
