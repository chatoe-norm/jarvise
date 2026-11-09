"""Read-only futures position watch: no-stop, trail, and exit advice."""

from __future__ import annotations

import logging
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from jarvise_analyze.engine import ATR_STOP_MULT
from jarvise_assist import ASSIST_SYMBOLS, ASSIST_TIMEFRAME
from jarvise_assist.execute import (
    MOVE_STOP_TTL_MS,
    PREVIEW_TTL_MS,
    assist_execute_mode,
    build_trade_client,
    cancel_jv_orders_for_symbol,
)
from jarvise_assist.futures import (
    BinanceFuturesClient,
    FuturesKeyUnsafeError,
    FuturesPermissionError,
    FuturesPosition,
    build_futures_client,
    find_protective_stop,
    protective_stop_coverage,
    protective_stop_covered_qty,
)
from jarvise_assist.journal import journal_on_close
from jarvise_assist.keyboard import exit_confirm_keyboard, move_stop_keyboard
from jarvise_assist.messages import (
    fmt_price,
    format_exit_advice,
    format_move_stop_advice,
    format_no_stop_warning,
    format_partial_stop_warning,
)
from jarvise_assist.pause import evaluate_day_loss_pause, evaluate_pause, maybe_auto_resume
from jarvise_assist.signals import (
    _alert_key_unsafe,
    _alert_permission,
    _handle_futures_down,
    _handle_futures_ok,
    _strip_secrets,
    is_night_hold,
)
from jarvise_assist.sizing import TARGET_R, analysis_is_fresh
from jarvise_assist.store import (
    advice_already_sent,
    ensure_settings,
    find_linkable_signal,
    get_watch_position,
    insert_exec_intent,
    insert_watch_advice,
    latest_no_stop_reason,
    list_held_watch_advice,
    mark_signal_taken,
    mark_watch_advice_dropped,
    mark_watch_advice_sent,
    mark_watch_positions_closed,
    now_ms,
    reset_watch_position,
    set_watch_no_stop_latched,
    upsert_watch_position,
)
from jarvise_assist.trail_advice import (
    ONE_H_MS,
    candle_extremes_after_entry,
    current_trail_advice,
)
from jarvise_ingest.db import load_candles_in_range, load_latest_analysis
from jarvise_notify.telegram import send_message

logger = logging.getLogger(__name__)

FOUR_H_MS = 4 * 3_600_000
SendFn = Callable[..., Any]


def _default_send(text: str, *, reply_markup: dict[str, Any] | None = None) -> bool:
    """Telegram send that keeps inline buttons (exit / move-stop)."""
    return send_message(text, reply_markup=reply_markup) is not None


# Re-export for callers/tests that imported from watch.
__all__ = (
    "ONE_H_MS",
    "approx_r_multiple",
    "candle_extremes_after_entry",
    "current_trail_advice",
    "flush_held_watch_advice",
    "initial_atr_stop",
    "run_assist_watch",
)


def approx_r_multiple(
    *,
    side: str,
    entry: Decimal,
    initial_stop: Decimal,
    mark: Decimal,
) -> Decimal:
    risk = abs(entry - initial_stop)
    if risk <= 0:
        return Decimal("0")
    if side == "long":
        return (mark - entry) / risk
    return (entry - mark) / risk


def initial_atr_stop(*, side: str, entry: Decimal, atr: Decimal) -> Decimal:
    dist = Decimal(str(ATR_STOP_MULT)) * atr
    if side == "long":
        return entry - dist
    return entry + dist


def _emit_advice(
    conn: Any,
    *,
    symbol: str,
    position_side: str,
    kind: str,
    reason: str | None,
    level: float | None,
    approx_r: float | None,
    text: str,
    night: bool,
    now_ms: int,
    send: SendFn,
    hold_ok: bool,
    since_ms: int | None = None,
    reply_markup: dict[str, Any] | None = None,
) -> str:
    """Send or hold; return status sent/held/skipped."""
    if advice_already_sent(
        conn,
        symbol=symbol,
        position_side=position_side,
        kind=kind,
        reason=reason,
        level=level,
        since_ms=since_ms,
    ):
        return "skipped"
    if night and hold_ok:
        insert_watch_advice(
            conn,
            symbol=symbol,
            position_side=position_side,
            kind=kind,
            reason=reason,
            level=level,
            approx_r=approx_r,
            status="held",
            created_at_ms=now_ms,
        )
        return "held"
    try:
        sent_ok = bool(send(text, reply_markup=reply_markup) if reply_markup is not None else send(text))
    except TypeError:
        if reply_markup is not None:
            logger.warning(
                "assist watch: send() rejected reply_markup; %s %s sent without buttons", symbol, kind
            )
        sent_ok = bool(send(text))
    if sent_ok:
        insert_watch_advice(
            conn,
            symbol=symbol,
            position_side=position_side,
            kind=kind,
            reason=reason,
            level=level,
            approx_r=approx_r,
            status="sent",
            created_at_ms=now_ms,
            sent_at_ms=now_ms,
        )
        return "sent"
    if hold_ok:
        # Soft-fail: hold move/exit for morning retry.
        insert_watch_advice(
            conn,
            symbol=symbol,
            position_side=position_side,
            kind=kind,
            reason=reason,
            level=level,
            approx_r=approx_r,
            status="held",
            created_at_ms=now_ms,
        )
        return "held"
    # no_stop: do not latch or store on failed send; retry next poll.
    return "failed"


def _entry_shifted(existing: dict[str, Any], pos: FuturesPosition) -> bool:
    """True when Binance entryPrice moved more than 0.05% (add or reopen)."""
    raw = existing.get("entry")
    if raw is None:
        return False
    stored = Decimal(str(raw))
    if stored <= 0:
        return False
    return abs(pos.entry_price - stored) / stored > Decimal("0.0005")


def _ensure_watch_row(
    conn: Any,
    pos: FuturesPosition,
    *,
    now_ms: int,
    in_universe: bool,
) -> dict[str, Any]:
    existing = get_watch_position(conn, pos.symbol, pos.position_side)
    if existing is not None and (
        existing.get("closed_at_ms") is not None
        or str(existing.get("side") or "") != pos.side
        or (existing.get("closed_at_ms") is None and _entry_shifted(existing, pos))
    ):
        # A new trade on the same symbol: never reuse the old stop, entry time,
        # signal link or no-stop latch. Entry shift >0.05% is add/reopen
        # (partial closes keep Binance entryPrice).
        reset_watch_position(conn, pos.symbol, pos.position_side)
        existing = None
    entry_time = pos.update_time_ms
    if entry_time is None:
        entry_time = int(existing["entry_time_ms"]) if existing and existing.get("entry_time_ms") else now_ms
    row = {
        "symbol": pos.symbol,
        "position_side": pos.position_side,
        "side": pos.side,
        "entry": float(pos.entry_price),
        "qty": float(pos.qty),
        "in_universe": 1 if in_universe else 0,
        "signal_id": existing.get("signal_id") if existing else None,
        "initial_stop": existing.get("initial_stop") if existing else None,
        "initial_stop_source": existing.get("initial_stop_source") if existing else None,
        "entry_time_ms": int(existing["entry_time_ms"])
        if existing and existing.get("entry_time_ms")
        else entry_time,
        "no_stop_latched": int(existing["no_stop_latched"]) if existing else 0,
        "last_seen_ms": int(now_ms),
        "closed_at_ms": None,
    }
    upsert_watch_position(conn, row)
    stored = get_watch_position(conn, pos.symbol, pos.position_side)
    assert stored is not None
    return stored


def _link_or_freeze_stop(
    conn: Any,
    pos: FuturesPosition,
    stored: dict[str, Any],
    *,
    now_ms: int,
) -> dict[str, Any]:
    if stored.get("initial_stop") is not None:
        return stored
    linked = find_linkable_signal(conn, symbol=pos.symbol, side=pos.side, now_ms=now_ms)
    if linked is not None:
        mark_signal_taken(conn, str(linked["id"]))
        conn.execute(
            """
            UPDATE assist_watch_position
            SET signal_id=?, initial_stop=?, initial_stop_source='signal'
            WHERE symbol=? AND position_side=?
            """,
            (
                str(linked["id"]),
                float(linked["stop"]),
                pos.symbol,
                pos.position_side,
            ),
        )
        conn.commit()
        refreshed = get_watch_position(conn, pos.symbol, pos.position_side)
        assert refreshed is not None
        return refreshed

    since = int(now_ms) - 30 * 24 * 3_600_000
    candles = load_candles_in_range(conn, pos.symbol, ASSIST_TIMEFRAME, since_ms=since, until_ms=int(now_ms))
    atr: Decimal | None = None
    for row in reversed(candles):
        if row.get("atr_14") is not None:
            atr = Decimal(str(row["atr_14"]))
            break
    if atr is None or atr <= 0:
        return stored
    stop = initial_atr_stop(side=pos.side, entry=pos.entry_price, atr=atr)
    conn.execute(
        """
        UPDATE assist_watch_position
        SET initial_stop=?, initial_stop_source='atr'
        WHERE symbol=? AND position_side=?
        """,
        (float(stop), pos.symbol, pos.position_side),
    )
    conn.commit()
    refreshed = get_watch_position(conn, pos.symbol, pos.position_side)
    assert refreshed is not None
    return refreshed


def _exit_reason(
    conn: Any,
    *,
    symbol: str,
    side: str,
    entry: Decimal,
    initial_stop: Decimal,
    mark: Decimal,
    now_ms: int,
) -> str | None:
    risk = abs(entry - initial_stop)
    if risk > 0:
        if side == "long" and mark >= entry + TARGET_R * risk:
            return "target_2r"
        if side == "short" and mark <= entry - TARGET_R * risk:
            return "target_2r"
    analysis = load_latest_analysis(conn, symbol, ASSIST_TIMEFRAME)
    if not analysis_is_fresh(analysis, now_ms=now_ms):
        return None
    action = str((analysis or {}).get("action") or "").lower()
    if action == "flat":
        return "flat"
    if action in {"long", "short"} and action != side:
        return "opposite"
    return None


def _process_universe_position(
    conn: Any,
    client: BinanceFuturesClient,
    pos: FuturesPosition,
    *,
    orders: list[dict[str, Any]],
    night: bool,
    now_ms: int,
    send: SendFn,
    actions: list[dict[str, Any]],
) -> None:
    stored = _ensure_watch_row(conn, pos, now_ms=now_ms, in_universe=True)
    stored = _link_or_freeze_stop(conn, pos, stored, now_ms=now_ms)
    initial_stop = stored.get("initial_stop")
    if initial_stop is None:
        actions.append({"symbol": pos.symbol, "action": "no_initial_stop"})
        return
    stop_dec = Decimal(str(initial_stop))
    since = int(stored["entry_time_ms"]) if stored.get("entry_time_ms") else None
    try:
        step = client.step_size(pos.symbol)
    except Exception:  # noqa: BLE001
        step = Decimal("0")
    coverage = protective_stop_coverage(
        orders,
        symbol=pos.symbol,
        position_side=pos.position_side,
        trade_side=pos.side,
        position_qty=pos.qty,
        step_size=step,
    )
    protective = find_protective_stop(
        orders,
        symbol=pos.symbol,
        position_side=pos.position_side,
        trade_side=pos.side,
    )
    if coverage in {"none", "partial"}:
        no_stop_reason = "partial" if coverage == "partial" else "missing"
        # One latch bit for both reasons: clear when coverage flips so the
        # other warning can fire once (partial → missing or the reverse).
        if stored.get("no_stop_latched"):
            prior = latest_no_stop_reason(conn, pos.symbol, pos.position_side)
            if prior is not None and prior != no_stop_reason:
                set_watch_no_stop_latched(conn, pos.symbol, pos.position_side, latched=False)
                stored["no_stop_latched"] = 0
        if not stored.get("no_stop_latched"):
            if coverage == "partial":
                covered = protective_stop_covered_qty(
                    orders,
                    symbol=pos.symbol,
                    position_side=pos.position_side,
                    trade_side=pos.side,
                )
                text = format_partial_stop_warning(
                    symbol=pos.symbol,
                    side=pos.side,
                    covered=covered,
                    position_qty=pos.qty,
                    stop=stop_dec,
                )
            else:
                text = format_no_stop_warning(symbol=pos.symbol, side=pos.side, stop=stop_dec)
            # Never hold no-stop overnight.
            status = _emit_advice(
                conn,
                symbol=pos.symbol,
                position_side=pos.position_side,
                kind="no_stop",
                reason=no_stop_reason,
                level=float(stop_dec),
                approx_r=None,
                text=text,
                night=False,
                now_ms=now_ms,
                send=send,
                hold_ok=False,
                since_ms=since,
            )
            if status == "sent":
                set_watch_no_stop_latched(conn, pos.symbol, pos.position_side, latched=True)
            actions.append(
                {
                    "symbol": pos.symbol,
                    "action": "no_stop",
                    "status": status,
                    "reason": no_stop_reason,
                }
            )
    elif stored.get("no_stop_latched"):
        set_watch_no_stop_latched(conn, pos.symbol, pos.position_side, latched=False)

    # Compare trail advice to the resting stop order, else the frozen initial stop.
    compare_stop = protective if protective is not None else stop_dec

    mark = pos.mark_price if pos.mark_price > 0 else client.mark_price(pos.symbol)
    entry_time = int(stored.get("entry_time_ms") or now_ms)
    candles_1h = load_candles_in_range(conn, pos.symbol, "1h", since_ms=entry_time, until_ms=int(now_ms))
    try:
        tick = client.symbol_filters(pos.symbol).tick_size
    except Exception:  # noqa: BLE001
        tick = Decimal("0.01")
    trail = current_trail_advice(
        side=pos.side,
        entry=pos.entry_price,
        initial_stop=stop_dec,
        candles_1h=candles_1h,
        entry_time_ms=entry_time,
        now_ms=now_ms,
        tick=tick,
    )
    if trail is not None:
        suggested_d, _source = trail
        tighter = suggested_d > compare_stop if pos.side == "long" else suggested_d < compare_stop
        if tighter:
            expires_at = now_ms + MOVE_STOP_TTL_MS
            text = format_move_stop_advice(
                symbol=pos.symbol,
                side=pos.side,
                qty=pos.qty,
                old_stop=protective,
                new_stop=suggested_d,
                mark=mark,
                entry=pos.entry_price,
                initial_stop=stop_dec,
                expires_at_ms=expires_at,
                tick=tick,
            )
            move_markup = None
            # Buttons/intents only in live daytime sends that will not dedupe-skip.
            if (
                assist_execute_mode() == "live"
                and not night
                and not advice_already_sent(
                    conn,
                    symbol=pos.symbol,
                    position_side=pos.position_side,
                    kind="move_stop",
                    reason=_source,
                    level=float(suggested_d),
                    since_ms=since,
                )
            ):
                intent_id = insert_exec_intent(
                    conn,
                    kind="move_stop",
                    symbol=pos.symbol,
                    side=pos.side,
                    preview={
                        "stop": str(suggested_d),
                        "qty": str(pos.qty),
                        "advice_text": text,
                        "source": _source,
                    },
                    created_at_ms=now_ms,
                    expires_at_ms=expires_at,
                )
                move_markup = move_stop_keyboard(intent_id, new_stop=fmt_price(suggested_d))
            status = _emit_advice(
                conn,
                symbol=pos.symbol,
                position_side=pos.position_side,
                kind="move_stop",
                reason=_source,
                level=float(suggested_d),
                approx_r=None,
                text=text,
                night=night,
                now_ms=now_ms,
                send=send,
                hold_ok=True,
                since_ms=since,
                reply_markup=move_markup,
            )
            actions.append(
                {
                    "symbol": pos.symbol,
                    "action": "move_stop",
                    "level": float(suggested_d),
                    "status": status,
                }
            )

    reason = _exit_reason(
        conn,
        symbol=pos.symbol,
        side=pos.side,
        entry=pos.entry_price,
        initial_stop=stop_dec,
        mark=mark,
        now_ms=now_ms,
    )
    if reason is not None:
        r_mult = approx_r_multiple(side=pos.side, entry=pos.entry_price, initial_stop=stop_dec, mark=mark)
        text = format_exit_advice(symbol=pos.symbol, side=pos.side, reason=reason, approx_r=r_mult)
        exit_markup = None
        if (
            assist_execute_mode() == "live"
            and not night
            and not advice_already_sent(
                conn,
                symbol=pos.symbol,
                position_side=pos.position_side,
                kind="exit",
                reason=reason,
                level=None,
                since_ms=since,
            )
        ):
            intent_id = insert_exec_intent(
                conn,
                kind="exit",
                symbol=pos.symbol,
                side=pos.side,
                preview={"qty": str(pos.qty), "entry": str(pos.entry_price)},
                created_at_ms=now_ms,
                expires_at_ms=now_ms + PREVIEW_TTL_MS,
            )
            exit_markup = exit_confirm_keyboard(intent_id)
        status = _emit_advice(
            conn,
            symbol=pos.symbol,
            position_side=pos.position_side,
            kind="exit",
            reason=reason,
            level=None,
            approx_r=float(r_mult),
            text=text,
            night=night,
            now_ms=now_ms,
            send=send,
            hold_ok=True,
            since_ms=since,
            reply_markup=exit_markup,
        )
        actions.append({"symbol": pos.symbol, "action": "exit", "reason": reason, "status": status})


def flush_held_watch_advice(
    conn: Any,
    *,
    futures: BinanceFuturesClient,
    now_ms_value: int,
    send: SendFn,
) -> dict[str, Any]:
    """Morning re-check: drop stale held move/exit, send what still applies."""
    held = list_held_watch_advice(conn)
    if not held:
        return {"sent": [], "dropped": []}
    positions = {(p.symbol, p.position_side): p for p in futures.position_risk()}
    try:
        orders = futures.all_open_orders()
    except AttributeError:
        orders = [*futures.open_orders(), *futures.open_algo_orders()]
    sent: list[str] = []
    dropped: list[str] = []
    for row in held:
        key = (str(row["symbol"]).upper(), str(row["position_side"]).upper())
        pos = positions.get(key)
        kind = str(row["kind"])
        if pos is None:
            mark_watch_advice_dropped(conn, str(row["id"]))
            dropped.append(str(row["id"]))
            continue
        if kind == "no_stop":
            # Should not be held; drop leftovers.
            mark_watch_advice_dropped(conn, str(row["id"]))
            dropped.append(str(row["id"]))
            continue
        stored = get_watch_position(conn, pos.symbol, pos.position_side)
        if stored is None or stored.get("initial_stop") is None:
            mark_watch_advice_dropped(conn, str(row["id"]))
            dropped.append(str(row["id"]))
            continue
        stop_dec = Decimal(str(stored["initial_stop"]))
        mark = pos.mark_price if pos.mark_price > 0 else futures.mark_price(pos.symbol)
        if kind == "exit":
            reason = _exit_reason(
                conn,
                symbol=pos.symbol,
                side=pos.side,
                entry=pos.entry_price,
                initial_stop=stop_dec,
                mark=mark,
                now_ms=now_ms_value,
            )
            if reason is None or reason != str(row.get("reason") or ""):
                mark_watch_advice_dropped(conn, str(row["id"]))
                dropped.append(str(row["id"]))
                continue
            r_mult = approx_r_multiple(side=pos.side, entry=pos.entry_price, initial_stop=stop_dec, mark=mark)
            text = format_exit_advice(symbol=pos.symbol, side=pos.side, reason=reason, approx_r=r_mult)
            exit_markup = None
            if assist_execute_mode() == "live":
                # Intent from the current position (not the held row); xx: matches qty+entry.
                intent_id = insert_exec_intent(
                    conn,
                    kind="exit",
                    symbol=pos.symbol,
                    side=pos.side,
                    preview={"qty": str(pos.qty), "entry": str(pos.entry_price)},
                    created_at_ms=now_ms_value,
                    expires_at_ms=now_ms_value + PREVIEW_TTL_MS,
                )
                exit_markup = exit_confirm_keyboard(intent_id)
            sent_ok = bool(send(text, reply_markup=exit_markup) if exit_markup is not None else send(text))
            if sent_ok:
                mark_watch_advice_sent(conn, str(row["id"]), sent_at_ms=now_ms_value)
                sent.append(str(row["id"]))
            continue
        if kind == "move_stop":
            protective = find_protective_stop(
                orders,
                symbol=pos.symbol,
                position_side=pos.position_side,
                trade_side=pos.side,
            )
            level = row.get("level")
            if level is None:
                mark_watch_advice_dropped(conn, str(row["id"]))
                dropped.append(str(row["id"]))
                continue
            compare_stop = protective if protective is not None else stop_dec
            suggested = Decimal(str(level))
            still_tighter = suggested > compare_stop if pos.side == "long" else suggested < compare_stop
            if not still_tighter:
                mark_watch_advice_dropped(conn, str(row["id"]))
                dropped.append(str(row["id"]))
                continue
            try:
                tick = futures.symbol_filters(pos.symbol).tick_size
            except Exception:  # noqa: BLE001
                tick = Decimal("0.01")
            text = format_move_stop_advice(
                symbol=pos.symbol,
                side=pos.side,
                qty=pos.qty,
                old_stop=protective,
                new_stop=suggested,
                mark=mark,
                entry=pos.entry_price,
                initial_stop=stop_dec,
                expires_at_ms=now_ms_value + MOVE_STOP_TTL_MS,
                tick=tick,
            )
            if send(text):
                mark_watch_advice_sent(conn, str(row["id"]), sent_at_ms=now_ms_value)
                sent.append(str(row["id"]))
    return {"sent": sent, "dropped": dropped}


def run_assist_watch(
    conn: Any,
    *,
    futures: BinanceFuturesClient | None = None,
    now_ms_value: int | None = None,
    send: SendFn | None = None,
) -> dict[str, Any]:
    """Poll open futures positions and emit no-stop / move / exit advice."""
    ts = int(now_ms_value if now_ms_value is not None else now_ms())
    send_fn = send or _default_send
    settings = ensure_settings(conn)
    if not settings["enabled"]:
        return _strip_secrets({"ok": True, "skipped": "assist_disabled", "paper_only": True, "at_ms": ts})

    maybe_auto_resume(conn, now_ms=ts, send=send_fn)

    client = futures if futures is not None else build_futures_client()
    if client is None:
        return _strip_secrets({"ok": False, "error": "no_binance_auth", "paper_only": True, "at_ms": ts})

    try:
        client.assert_key_safe()
    except FuturesKeyUnsafeError as exc:
        return _strip_secrets(_alert_key_unsafe(conn, send=send_fn, detail=str(exc), now=ts))

    try:
        client.usdt_wallet_balance()
        positions = client.position_risk()
        try:
            orders = client.all_open_orders()
        except AttributeError:
            orders = [*client.open_orders(), *client.open_algo_orders()]
        recovered = _handle_futures_ok(conn, send=send_fn)
    except FuturesPermissionError as exc:
        return _strip_secrets(_alert_permission(conn, send=send_fn, detail=str(exc), now=ts))
    except Exception as exc:  # noqa: BLE001
        return _strip_secrets(_handle_futures_down(conn, now=ts, send=send_fn, error=type(exc).__name__))

    # Pause may engage here; watch still emits advice for open positions.
    pause_info = evaluate_pause(conn, futures=client, now_ms=ts, send=send_fn)

    night = is_night_hold(now_ms=ts)
    actions: list[dict[str, Any]] = []
    open_keys: set[tuple[str, str]] = set()
    for pos in positions:
        open_keys.add((pos.symbol, pos.position_side))
        if pos.symbol not in ASSIST_SYMBOLS:
            _ensure_watch_row(conn, pos, now_ms=ts, in_universe=False)
            actions.append({"symbol": pos.symbol, "action": "outside_universe"})
            continue
        _process_universe_position(
            conn,
            client,
            pos,
            orders=orders,
            night=night,
            now_ms=ts,
            send=send_fn,
            actions=actions,
        )
    closed = mark_watch_positions_closed(conn, open_keys=open_keys, now_ms=ts)
    orphan_cancelled: list[dict[str, str]] = []
    if closed and assist_execute_mode() == "live":
        trade, _auth_reason = build_trade_client()
        if trade is not None:
            for sym, _ps in closed:
                try:
                    ids, _ok = cancel_jv_orders_for_symbol(trade, sym)
                except Exception:  # noqa: BLE001
                    logger.exception("orphan jv- cancel failed for %s", sym)
                    ids = []
                for algo_id in ids:
                    orphan_cancelled.append({"symbol": sym, "algo_id": algo_id})
    try:
        journal_on_close(conn, closed, futures=client, now_ms_value=ts)
    except Exception:  # noqa: BLE001 — a journal failure must not stop advice or orphan cancel
        logger.exception("assist journal failed")
    # Same poll as the close: local rows only, no extra Binance read.
    day_pause = evaluate_day_loss_pause(conn, now_ms=ts, send=send_fn)
    if day_pause.get("reason") in {"day_loss", "day_loss_deeper"}:
        pause_info = day_pause
    return _strip_secrets(
        {
            "ok": True,
            "paper_only": True,
            "at_ms": ts,
            "night_hold": night,
            "actions": actions,
            "closed": [{"symbol": s, "position_side": p} for s, p in closed],
            "orphan_cancelled": orphan_cancelled,
            "futures_recovered": recovered,
            "pause": pause_info,
        }
    )
