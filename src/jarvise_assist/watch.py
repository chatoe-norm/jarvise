"""Read-only futures position watch: no-stop, trail, and exit advice."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

from jarvise_analyze.engine import ATR_STOP_MULT
from jarvise_assist import ASSIST_SYMBOLS, ASSIST_TIMEFRAME
from jarvise_assist.futures import (
    BinanceFuturesClient,
    FuturesKeyUnsafeError,
    FuturesPermissionError,
    FuturesPosition,
    build_futures_client,
    find_protective_stop,
)
from jarvise_assist.messages import (
    format_exit_advice,
    format_move_stop_advice,
    format_no_stop_warning,
)
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
    insert_watch_advice,
    list_held_watch_advice,
    mark_signal_taken,
    mark_watch_advice_dropped,
    mark_watch_advice_sent,
    mark_watch_positions_closed,
    now_ms,
    set_watch_no_stop_latched,
    upsert_watch_position,
)
from jarvise_ingest.db import load_candles_in_range, load_latest_analysis
from jarvise_notify.telegram import send_telegram_message
from jarvise_paper.profit_lock import trail_stop_with_source

FOUR_H_MS = 4 * 3_600_000
SendFn = Callable[[str], bool]


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


def candle_extremes_after_entry(
    candles: list[dict[str, Any]],
    *,
    entry_time_ms: int,
    now_ms: int,
) -> tuple[float | None, float | None]:
    """Best high/low of 4h candles that closed after entry and by now."""
    best_high: float | None = None
    best_low: float | None = None
    for row in candles:
        ts = int(row["timestamp"])
        close_ms = ts + FOUR_H_MS
        if close_ms <= int(entry_time_ms) or close_ms > int(now_ms):
            continue
        high = row.get("high")
        low = row.get("low")
        if high is not None:
            h = float(high)
            best_high = h if best_high is None else max(best_high, h)
        if low is not None:
            lo = float(low)
            best_low = lo if best_low is None else min(best_low, lo)
    return best_low, best_high


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
) -> str:
    """Send or hold; return status sent/held/skipped."""
    if advice_already_sent(
        conn,
        symbol=symbol,
        position_side=position_side,
        kind=kind,
        reason=reason,
        level=level,
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
    if send(text):
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


def _ensure_watch_row(
    conn: Any,
    pos: FuturesPosition,
    *,
    now_ms: int,
    in_universe: bool,
) -> dict[str, Any]:
    existing = get_watch_position(conn, pos.symbol, pos.position_side)
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
    protective = find_protective_stop(
        orders,
        symbol=pos.symbol,
        position_side=pos.position_side,
        trade_side=pos.side,
    )
    if protective is None:
        if not stored.get("no_stop_latched"):
            text = format_no_stop_warning(symbol=pos.symbol, side=pos.side, stop=stop_dec)
            # Never hold no-stop overnight.
            status = _emit_advice(
                conn,
                symbol=pos.symbol,
                position_side=pos.position_side,
                kind="no_stop",
                reason="missing",
                level=float(stop_dec),
                approx_r=None,
                text=text,
                night=False,
                now_ms=now_ms,
                send=send,
                hold_ok=False,
            )
            if status == "sent":
                set_watch_no_stop_latched(conn, pos.symbol, pos.position_side, latched=True)
            actions.append({"symbol": pos.symbol, "action": "no_stop", "status": status})
        return

    if stored.get("no_stop_latched"):
        set_watch_no_stop_latched(conn, pos.symbol, pos.position_side, latched=False)

    mark = pos.mark_price if pos.mark_price > 0 else client.mark_price(pos.symbol)
    entry_time = int(stored.get("entry_time_ms") or now_ms)
    since = min(entry_time, now_ms) - FOUR_H_MS
    candles = load_candles_in_range(conn, pos.symbol, ASSIST_TIMEFRAME, since_ms=since, until_ms=int(now_ms))
    best_low, best_high = candle_extremes_after_entry(candles, entry_time_ms=entry_time, now_ms=now_ms)
    trail_pos = {
        "side": pos.side,
        "entry_price": float(pos.entry_price),
        "initial_stop_price": float(stop_dec),
        "stop_price": float(stop_dec),
    }
    trail = trail_stop_with_source(trail_pos, best_low=best_low, best_high=best_high)
    if trail is not None:
        suggested, _source = trail
        suggested_d = Decimal(str(suggested))
        tighter = suggested_d > protective if pos.side == "long" else suggested_d < protective
        if tighter:
            text = format_move_stop_advice(symbol=pos.symbol, side=pos.side, stop=suggested_d)
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
            if send(text):
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
            if protective is None or level is None:
                mark_watch_advice_dropped(conn, str(row["id"]))
                dropped.append(str(row["id"]))
                continue
            suggested = Decimal(str(level))
            still_tighter = suggested > protective if pos.side == "long" else suggested < protective
            if not still_tighter:
                mark_watch_advice_dropped(conn, str(row["id"]))
                dropped.append(str(row["id"]))
                continue
            text = format_move_stop_advice(symbol=pos.symbol, side=pos.side, stop=suggested)
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
    send_fn = send or send_telegram_message
    settings = ensure_settings(conn)
    if not settings["enabled"]:
        return _strip_secrets({"ok": True, "skipped": "assist_disabled", "paper_only": True, "at_ms": ts})

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
    return _strip_secrets(
        {
            "ok": True,
            "paper_only": True,
            "at_ms": ts,
            "night_hold": night,
            "actions": actions,
            "closed": [{"symbol": s, "position_side": p} for s, p in closed],
            "futures_recovered": recovered,
        }
    )
