"""Build and send trade-assistant signals from stored 4h analysis."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from jarvise_analyze.engine import ATR_STOP_MULT
from jarvise_assist import ASSIST_SYMBOLS, ASSIST_TIMEFRAME
from jarvise_assist.futures import (
    BinanceFuturesClient,
    FuturesKeyUnsafeError,
    FuturesPermissionError,
    FuturesPosition,
    SymbolFilters,
    build_futures_client,
    find_protective_stop,
)
from jarvise_assist.keyboard import skip_inline_keyboard
from jarvise_assist.messages import (
    format_futures_down_alert,
    format_futures_recovered_alert,
    format_key_unsafe_alert,
    format_permission_error_alert,
    format_signal_message,
)
from jarvise_assist.pause import evaluate_pause, is_paused, maybe_auto_resume
from jarvise_assist.sizing import (
    IDEA_LOOKBACK_MS,
    ExistingIdea,
    SignalCandidate,
    admit_signals,
    analysis_is_fresh,
    assign_shared_leverage,
    below_min_notional,
    chase_exceeded,
    next_assist_run_ms,
    passes_signal_floor,
    position_risk_usdt,
    size_signal,
)
from jarvise_assist.store import (
    ensure_settings,
    expire_older_held_keep_newest,
    find_linkable_signal,
    get_edge,
    insert_signal,
    list_active_idea_signals,
    list_held_signals,
    mark_signal_expired,
    mark_signal_sent,
    now_ms,
    previous_analysis_action,
    recent_same_idea,
    set_signal_telegram_message_id,
    write_edge,
)
from jarvise_ingest.db import load_candles_in_range, load_latest_analysis
from jarvise_notify.telegram import send_message

BANGKOK = timezone(timedelta(hours=7), name="Asia/Bangkok")
FUTURES_DOWN_ALERT_MS = 30 * 60 * 1000
SendFn = Callable[..., Any]


def _invoke_send(
    send: SendFn,
    text: str,
    *,
    reply_markup: dict[str, Any] | None = None,
) -> tuple[bool, int | None]:
    try:
        result = send(text, reply_markup=reply_markup)
    except TypeError:
        result = send(text)
    if isinstance(result, bool):
        return result, None
    if isinstance(result, int):
        return True, result
    return False, None


def _default_send(text: str, *, reply_markup: dict[str, Any] | None = None) -> int | None:
    return send_message(text, reply_markup=reply_markup)


def is_night_hold(*, now_ms: int) -> bool:
    """True from 23:30 inclusive through 07:30 exclusive Asia/Bangkok."""
    local = datetime.fromtimestamp(now_ms / 1000.0, tz=BANGKOK)
    minutes = local.hour * 60 + local.minute
    return minutes >= 23 * 60 + 30 or minutes < 7 * 60 + 30


def _strip_secrets(payload: dict[str, Any]) -> dict[str, Any]:
    """Ensure API key material never appears in job payloads."""
    banned = ("api_key", "apikey", "secret", "private_key", "signature", "BINANCE_API")
    out: dict[str, Any] = {}
    for key, value in payload.items():
        key_l = str(key).lower()
        if any(b.lower() in key_l for b in banned):
            continue
        if isinstance(value, dict):
            out[key] = _strip_secrets(value)
        elif isinstance(value, list):
            out[key] = [_strip_secrets(v) if isinstance(v, dict) else v for v in value]
        else:
            out[key] = value
    return out


def _handle_futures_down(
    conn: Any,
    *,
    now: int,
    send: SendFn,
    error: str,
) -> dict[str, Any]:
    edge = get_edge(conn)
    since = edge["futures_down_since_ms"]
    if since is None:
        write_edge(conn, futures_down_since_ms=now, futures_down_alerted=False)
        since = now
    alerted = False
    if not edge["futures_down_alerted"] and (now - int(since)) >= FUTURES_DOWN_ALERT_MS:
        minutes = max(30, int((now - int(since)) / 60_000))
        if send(format_futures_down_alert(minutes=minutes)):
            write_edge(conn, futures_down_alerted=True)
            alerted = True
    return {
        "ok": False,
        "error": "futures_unavailable",
        "detail": error,
        "futures_down_alert": alerted,
        "paper_only": True,
        "at_ms": now,
    }


def _handle_futures_ok(conn: Any, *, send: SendFn) -> str | None:
    """Clear down/permission/key-unsafe flags after a successful futures read."""
    edge = get_edge(conn)
    recovered = None
    if edge["futures_down_alerted"]:
        if send(format_futures_recovered_alert()):
            recovered = "recovered"
    write_edge(
        conn,
        futures_down_since_ms=None,
        futures_down_alerted=False,
        permission_error_reported=False,
        key_unsafe_reported=False,
    )
    return recovered


def _alert_key_unsafe(conn: Any, *, send: SendFn, detail: str, now: int) -> dict[str, Any]:
    edge = get_edge(conn)
    alerted = False
    if not edge["key_unsafe_reported"]:
        if send(format_key_unsafe_alert()):
            write_edge(conn, key_unsafe_reported=True)
            alerted = True
    return {
        "ok": False,
        "error": "key_unsafe",
        "detail": detail,
        "key_unsafe_alert": alerted,
        "paper_only": True,
        "at_ms": now,
    }


def _alert_permission(conn: Any, *, send: SendFn, detail: str, now: int) -> dict[str, Any]:
    edge = get_edge(conn)
    alerted = False
    if not edge["permission_error_reported"]:
        if send(format_permission_error_alert()):
            write_edge(conn, permission_error_reported=True)
            alerted = True
    return {
        "ok": False,
        "error": "permission_error",
        "detail": detail,
        "permission_alert": alerted,
        "paper_only": True,
        "at_ms": now,
    }


def _same_idea_skip(
    conn: Any,
    *,
    symbol: str,
    side: str,
    now_ms: int,
) -> bool:
    """True when we should not resend the same side within 24h without a side flip."""
    prev = previous_analysis_action(conn, symbol=symbol, timeframe=ASSIST_TIMEFRAME)
    if prev is not None and prev != side:
        return False
    return recent_same_idea(conn, symbol=symbol, side=side, now_ms=now_ms)


def _latest_atr(conn: Any, symbol: str, *, now_ms: int) -> Decimal | None:
    since = int(now_ms) - 30 * 24 * 3_600_000
    rows = load_candles_in_range(conn, symbol, ASSIST_TIMEFRAME, since_ms=since, until_ms=int(now_ms))
    for row in reversed(rows):
        atr = row.get("atr_14")
        if atr is not None:
            return Decimal(str(atr))
    return None


def _stop_for_open_position(
    conn: Any,
    pos: FuturesPosition,
    *,
    orders: list[dict[str, Any]],
    now_ms: int,
) -> Decimal | None:
    trade_side = pos.side
    protective = find_protective_stop(
        orders,
        symbol=pos.symbol,
        position_side=pos.position_side,
        trade_side=trade_side,
    )
    if protective is not None:
        return protective
    linked = find_linkable_signal(conn, symbol=pos.symbol, side=trade_side, now_ms=now_ms)
    if linked is not None:
        return Decimal(str(linked["stop"]))
    atr = _latest_atr(conn, pos.symbol, now_ms=now_ms)
    if atr is None or atr <= 0:
        return None
    dist = Decimal(str(ATR_STOP_MULT)) * atr
    if trade_side == "long":
        return pos.entry_price - dist
    return pos.entry_price + dist


def _floor_skip_reason(analysis: dict[str, Any] | None) -> str:
    """Map a failed floor gate to flat vs low_confidence for owner-facing copy."""
    if analysis is None:
        return "flat"
    action = str(analysis.get("action") or "").lower()
    if action not in {"long", "short"}:
        return "flat"
    if analysis.get("invalidation_price") is None:
        return "flat"
    return "low_confidence"


def build_existing_ideas(
    conn: Any,
    *,
    positions: list[FuturesPosition],
    orders: list[dict[str, Any]],
    balance: Decimal,
    risk_pct: Decimal,
    now_ms: int,
    include_sent_held: bool = True,
) -> list[ExistingIdea]:
    """Open universe positions plus recent sent/held signals not already open.

    When ``include_sent_held`` is False (on-demand menu), only open positions
    occupy risk slots so a prior push does not hide a still-valid idea.
    """
    ideas: list[ExistingIdea] = []
    open_keys: set[tuple[str, str]] = set()
    for pos in positions:
        if pos.symbol not in ASSIST_SYMBOLS:
            continue
        trade_side = pos.side
        open_keys.add((pos.symbol, trade_side))
        stop = _stop_for_open_position(conn, pos, orders=orders, now_ms=now_ms)
        risk = position_risk_usdt(
            entry=pos.entry_price,
            qty=pos.qty,
            stop=stop,
            balance=balance,
            risk_pct=risk_pct,
        )
        ideas.append(ExistingIdea(symbol=pos.symbol, side=trade_side, risk_usdt=risk))
    if include_sent_held:
        for row in list_active_idea_signals(conn, now_ms=now_ms, within_ms=IDEA_LOOKBACK_MS):
            key = (str(row["symbol"]).upper(), str(row["side"]).lower())
            if key in open_keys:
                continue
            ideas.append(
                ExistingIdea(
                    symbol=key[0],
                    side=key[1],
                    risk_usdt=Decimal(str(row["loss_at_stop"])),
                )
            )
            open_keys.add(key)
    return ideas


def scan_signal_candidates(
    conn: Any,
    *,
    futures: BinanceFuturesClient,
    now_ms: int,
    persist: bool = True,
    on_demand: bool = False,
) -> dict[str, Any]:
    """Shared scan for signal job and the anything-now menu.

    When ``persist`` is False, no rows are inserted and Telegram is not used.
    When ``on_demand`` is True, skip push dedupe (same_idea and sent/held risk slots).
    """
    settings = ensure_settings(conn)
    balance, available = futures.usdt_wallet_and_available()
    positions = futures.position_risk()
    open_syms = {p.symbol for p in positions}
    try:
        orders = futures.all_open_orders()
    except AttributeError:
        orders = [*futures.open_orders(), *futures.open_algo_orders()]

    risk_pct = Decimal(str(settings["risk_pct"]))
    skipped: list[dict[str, Any]] = []
    candidates: list[SignalCandidate] = []
    symbol_rank = {sym: i for i, sym in enumerate(ASSIST_SYMBOLS)}

    for symbol in ASSIST_SYMBOLS:
        if symbol in open_syms:
            skipped.append({"symbol": symbol, "reason": "open_position"})
            continue
        analysis = load_latest_analysis(conn, symbol, ASSIST_TIMEFRAME)
        if analysis is None or not passes_signal_floor(analysis):
            skipped.append({"symbol": symbol, "reason": _floor_skip_reason(analysis)})
            continue
        if not analysis_is_fresh(analysis, now_ms=now_ms):
            skipped.append({"symbol": symbol, "reason": "stale_analysis"})
            continue
        side = str(analysis["action"]).lower()
        if not on_demand and _same_idea_skip(conn, symbol=symbol, side=side, now_ms=now_ms):
            skipped.append({"symbol": symbol, "reason": "same_idea"})
            continue
        try:
            mark = futures.mark_price(symbol)
            if hasattr(futures, "symbol_filters"):
                filters = futures.symbol_filters(symbol)
            else:
                filters = SymbolFilters(
                    step_size=futures.step_size(symbol),
                    min_qty=Decimal("0"),
                    min_notional=Decimal("0"),
                )
        except Exception as exc:  # noqa: BLE001
            skipped.append({"symbol": symbol, "reason": f"mark:{type(exc).__name__}"})
            continue
        sized = size_signal(
            symbol=symbol,
            side=side,
            entry=mark,
            stop=Decimal(str(analysis["invalidation_price"])),
            balance=balance,
            risk_pct=risk_pct,
            step_size=filters.step_size,
            analysis_id=str(analysis.get("analysis_id") or "") or None,
        )
        if sized is None:
            skipped.append({"symbol": symbol, "reason": "size_failed"})
            continue
        if below_min_notional(
            qty=sized.qty,
            notional=sized.notional,
            min_qty=filters.min_qty,
            min_notional=filters.min_notional,
        ):
            skipped.append(
                {
                    "symbol": symbol,
                    "reason": "below_min_notional",
                    "min_notional": float(filters.min_notional),
                    "notional": float(sized.notional),
                }
            )
            continue
        candidates.append(
            SignalCandidate(
                sized=sized,
                confidence=float(analysis.get("confidence_score") or 0.0),
                symbol_rank=symbol_rank[symbol],
            )
        )

    existing = build_existing_ideas(
        conn,
        positions=positions,
        orders=orders,
        balance=balance,
        risk_pct=risk_pct,
        now_ms=now_ms,
        include_sent_held=not on_demand,
    )
    admitted, rejected = admit_signals(existing=existing, candidates=candidates, balance=balance)
    for cand in rejected:
        skipped.append({"symbol": cand.sized.symbol, "reason": "risk_cap"})
    admitted, margin_dropped = assign_shared_leverage(
        admitted,
        available_balance=available,
    )
    for cand in margin_dropped:
        skipped.append({"symbol": cand.sized.symbol, "reason": "margin_cap"})
    return {
        "admitted": admitted,
        "skipped": skipped,
        "balance_usdt": float(balance),
        "available_usdt": float(available),
        "positions": positions,
        "orders": orders,
        "persist": persist,
        "on_demand": on_demand,
    }


def run_assist_signals(
    conn: Any,
    *,
    futures: BinanceFuturesClient | None = None,
    now_ms_value: int | None = None,
    send: SendFn | None = None,
) -> dict[str, Any]:
    """Scan seven symbols and send or hold new signals."""
    ts = int(now_ms_value if now_ms_value is not None else now_ms())
    send_fn = send or _default_send
    settings = ensure_settings(conn)
    if not settings["enabled"]:
        return _strip_secrets(
            {
                "ok": True,
                "skipped": "assist_disabled",
                "paper_only": True,
                "at_ms": ts,
            }
        )

    maybe_auto_resume(conn, now_ms=ts, send=send_fn)
    settings = ensure_settings(conn)

    client = futures if futures is not None else build_futures_client()
    if client is None:
        return _strip_secrets(
            {
                "ok": False,
                "error": "no_binance_auth",
                "paper_only": True,
                "at_ms": ts,
            }
        )

    try:
        client.assert_key_safe()
        client.usdt_wallet_balance()
        recovered = _handle_futures_ok(conn, send=send_fn)
    except FuturesKeyUnsafeError as exc:
        return _strip_secrets(_alert_key_unsafe(conn, send=send_fn, detail=str(exc), now=ts))
    except FuturesPermissionError as exc:
        return _strip_secrets(_alert_permission(conn, send=send_fn, detail=str(exc), now=ts))
    except Exception as exc:  # noqa: BLE001 — surface as futures down, not a crash
        return _strip_secrets(_handle_futures_down(conn, now=ts, send=send_fn, error=type(exc).__name__))

    pause_info = evaluate_pause(conn, futures=client, now_ms=ts, send=send_fn)
    settings = ensure_settings(conn)
    if is_paused(settings, now_ms=ts) or pause_info.get("paused"):
        return _strip_secrets(
            {
                "ok": True,
                "skipped": "streak_pause",
                "paper_only": True,
                "at_ms": ts,
                "pause": pause_info,
                "futures_recovered": recovered,
            }
        )

    try:
        scanned = scan_signal_candidates(conn, futures=client, now_ms=ts, persist=True)
    except FuturesPermissionError as exc:
        return _strip_secrets(_alert_permission(conn, send=send_fn, detail=str(exc), now=ts))
    except Exception as exc:  # noqa: BLE001
        return _strip_secrets(_handle_futures_down(conn, now=ts, send=send_fn, error=type(exc).__name__))

    balance = Decimal(str(scanned["balance_usdt"]))
    night = is_night_hold(now_ms=ts)
    created: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = list(scanned["skipped"])
    admitted: list[SignalCandidate] = list(scanned["admitted"])
    expires = next_assist_run_ms(ts)
    for cand in admitted:
        sized = cand.sized
        symbol = sized.symbol
        if night:
            sid = insert_signal(
                conn,
                sized,
                status="held",
                created_at_ms=ts,
                expires_at_ms=expires,
            )
            created.append({"id": sid, "symbol": symbol, "status": "held"})
            continue
        text = format_signal_message(sized, expires_at_ms=expires, balance=balance)
        sid = insert_signal(
            conn,
            sized,
            status="held",
            created_at_ms=ts,
            expires_at_ms=expires,
        )
        sent_ok, mid = _invoke_send(send_fn, text, reply_markup=skip_inline_keyboard(sid))
        if sent_ok:
            mark_signal_sent(conn, sid, sized, sent_at_ms=ts, expires_at_ms=expires)
            if mid is not None:
                set_signal_telegram_message_id(conn, sid, mid)
            created.append({"id": sid, "symbol": symbol, "status": "sent", "telegram": True})
        else:
            created.append({"id": sid, "symbol": symbol, "status": "held", "telegram": False})

    return _strip_secrets(
        {
            "ok": True,
            "paper_only": True,
            "at_ms": ts,
            "night_hold": night,
            "balance_usdt": float(balance),
            "available_usdt": scanned.get("available_usdt"),
            "created": created,
            "skipped": skipped,
            "futures_recovered": recovered,
        }
    )


def run_assist_morning(
    conn: Any,
    *,
    futures: BinanceFuturesClient | None = None,
    now_ms_value: int | None = None,
    send: SendFn | None = None,
) -> dict[str, Any]:
    """Re-check held signals at 08:20 Bangkok (after the 08:00 paper-run writes fresh analysis) and send or expire."""
    ts = int(now_ms_value if now_ms_value is not None else now_ms())
    send_fn = send or _default_send
    settings = ensure_settings(conn)
    if not settings["enabled"]:
        return _strip_secrets(
            {
                "ok": True,
                "skipped": "assist_disabled",
                "paper_only": True,
                "at_ms": ts,
            }
        )

    maybe_auto_resume(conn, now_ms=ts, send=send_fn)
    settings = ensure_settings(conn)

    client = futures if futures is not None else build_futures_client()
    if client is None:
        return _strip_secrets(
            {
                "ok": False,
                "error": "no_binance_auth",
                "paper_only": True,
                "at_ms": ts,
            }
        )

    try:
        client.assert_key_safe()
        balance, available = client.usdt_wallet_and_available()
        client.open_symbols()
        _handle_futures_ok(conn, send=send_fn)
    except FuturesKeyUnsafeError as exc:
        return _strip_secrets(_alert_key_unsafe(conn, send=send_fn, detail=str(exc), now=ts))
    except FuturesPermissionError as exc:
        return _strip_secrets(_alert_permission(conn, send=send_fn, detail=str(exc), now=ts))
    except Exception as exc:  # noqa: BLE001
        return _strip_secrets(_handle_futures_down(conn, now=ts, send=send_fn, error=type(exc).__name__))

    pause_info = evaluate_pause(conn, futures=client, now_ms=ts, send=send_fn)
    settings = ensure_settings(conn)
    if is_paused(settings, now_ms=ts) or pause_info.get("paused"):
        return _strip_secrets(
            {
                "ok": True,
                "skipped": "streak_pause",
                "paper_only": True,
                "at_ms": ts,
                "pause": pause_info,
            }
        )

    risk_pct = Decimal(str(settings["risk_pct"]))
    sent: list[str] = []
    expired: list[str] = list(expire_older_held_keep_newest(conn))
    symbol_rank = {sym: i for i, sym in enumerate(ASSIST_SYMBOLS)}
    morning_candidates: list[tuple[str, SignalCandidate]] = []

    for row in list_held_signals(conn):
        symbol = str(row["symbol"]).upper()
        side = str(row["side"]).lower()
        entry0 = Decimal(str(row["entry"]))
        stop0 = Decimal(str(row["stop"]))
        analysis = load_latest_analysis(conn, symbol, ASSIST_TIMEFRAME)
        try:
            mark = client.mark_price(symbol)
            step = client.step_size(symbol)
        except Exception:  # noqa: BLE001
            mark_signal_expired(conn, str(row["id"]))
            expired.append(str(row["id"]))
            continue
        if not analysis_is_fresh(analysis, now_ms=ts):
            mark_signal_expired(conn, str(row["id"]))
            expired.append(str(row["id"]))
            continue
        same_side = (
            analysis is not None
            and passes_signal_floor(analysis)
            and str(analysis.get("action") or "").lower() == side
        )
        chased = chase_exceeded(side=side, entry=entry0, stop=stop0, mark=mark)
        if analysis is None or not same_side or chased:
            mark_signal_expired(conn, str(row["id"]))
            expired.append(str(row["id"]))
            continue
        new_stop = Decimal(str(analysis["invalidation_price"]))
        sized = size_signal(
            symbol=symbol,
            side=side,
            entry=mark,
            stop=new_stop,
            balance=balance,
            risk_pct=risk_pct,
            step_size=step,
            analysis_id=str(analysis.get("analysis_id") or row.get("analysis_id") or "") or None,
        )
        if sized is None:
            mark_signal_expired(conn, str(row["id"]))
            expired.append(str(row["id"]))
            continue
        morning_candidates.append(
            (
                str(row["id"]),
                SignalCandidate(
                    sized=sized,
                    confidence=float(analysis.get("confidence_score") or 0.0),
                    symbol_rank=symbol_rank.get(symbol, 99),
                ),
            )
        )

    morning_sized = [c for _, c in morning_candidates]
    admitted_morning, margin_dropped = assign_shared_leverage(
        morning_sized,
        available_balance=available,
    )
    dropped_set = set(margin_dropped)
    for sid, cand in morning_candidates:
        if cand in dropped_set:
            mark_signal_expired(conn, sid)
            expired.append(sid)

    # Admitted rows are new SignalCandidate objects; pair by symbol (one held per symbol).
    sized_by_symbol = {c.sized.symbol: c.sized for c in admitted_morning}
    expires = next_assist_run_ms(ts)
    for sid, cand in morning_candidates:
        if cand in dropped_set:
            continue
        sized = sized_by_symbol.get(cand.sized.symbol)
        if sized is None:
            continue
        text = format_signal_message(sized, expires_at_ms=expires, balance=balance)
        sent_ok, mid = _invoke_send(send_fn, text, reply_markup=skip_inline_keyboard(sid))
        if sent_ok:
            mark_signal_sent(conn, sid, sized, sent_at_ms=ts, expires_at_ms=expires)
            if mid is not None:
                set_signal_telegram_message_id(conn, sid, mid)
            sent.append(sid)

    # Late import: watch.py imports alert helpers from this module.
    from jarvise_assist.watch import flush_held_watch_advice

    watch_flush = flush_held_watch_advice(conn, futures=client, now_ms_value=ts, send=send_fn)

    return _strip_secrets(
        {
            "ok": True,
            "paper_only": True,
            "at_ms": ts,
            "sent": sent,
            "expired": expired,
            "balance_usdt": float(balance),
            "available_usdt": float(available),
            "watch_advice": watch_flush,
        }
    )
