"""Build and send trade-assistant signals from stored 4h analysis."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from jarvise_assist import ASSIST_SYMBOLS, ASSIST_TIMEFRAME
from jarvise_assist.futures import (
    BinanceFuturesClient,
    FuturesKeyUnsafeError,
    FuturesPermissionError,
    build_futures_client,
)
from jarvise_assist.messages import (
    format_futures_down_alert,
    format_futures_recovered_alert,
    format_permission_error_alert,
    format_signal_message,
)
from jarvise_assist.sizing import (
    chase_exceeded,
    next_4h_close_ms,
    passes_signal_floor,
    size_signal,
)
from jarvise_assist.store import (
    active_signal_for_symbol,
    ensure_settings,
    get_edge,
    insert_signal,
    list_held_signals,
    mark_signal_expired,
    mark_signal_sent,
    now_ms,
    write_edge,
)
from jarvise_ingest.db import load_latest_analysis
from jarvise_notify.telegram import send_telegram_message

BANGKOK = timezone(timedelta(hours=7), name="Asia/Bangkok")
FUTURES_DOWN_ALERT_MS = 30 * 60 * 1000
SendFn = Callable[[str], bool]


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
        elif isinstance(value, str) and len(value) > 20 and value.isalnum():
            # Do not copy opaque long tokens into nested reasons.
            out[key] = value
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
    edge = get_edge(conn)
    if edge["futures_down_since_ms"] is None and not edge["futures_down_alerted"]:
        return None
    recovered = None
    if edge["futures_down_alerted"]:
        if send(format_futures_recovered_alert()):
            recovered = "recovered"
    write_edge(conn, futures_down_since_ms=None, futures_down_alerted=False)
    return recovered


def run_assist_signals(
    conn: Any,
    *,
    futures: BinanceFuturesClient | None = None,
    now_ms_value: int | None = None,
    send: SendFn | None = None,
) -> dict[str, Any]:
    """Scan seven symbols and send or hold new signals."""
    ts = int(now_ms_value if now_ms_value is not None else now_ms())
    send_fn = send or send_telegram_message
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

    edge = get_edge(conn)
    try:
        client.assert_key_safe()
    except FuturesKeyUnsafeError as exc:
        return _strip_secrets(
            {
                "ok": False,
                "error": "key_unsafe",
                "detail": str(exc),
                "paper_only": True,
                "at_ms": ts,
            }
        )

    try:
        balance = client.usdt_wallet_balance()
        open_syms = client.open_symbols()
        recovered = _handle_futures_ok(conn, send=send_fn)
    except FuturesPermissionError as exc:
        alerted = False
        if not edge["permission_error_reported"]:
            if send_fn(format_permission_error_alert()):
                write_edge(conn, permission_error_reported=True)
                alerted = True
            else:
                write_edge(conn, permission_error_reported=True)
                alerted = True
        return _strip_secrets(
            {
                "ok": False,
                "error": "permission_error",
                "detail": str(exc),
                "permission_alert": alerted,
                "paper_only": True,
                "at_ms": ts,
            }
        )
    except Exception as exc:  # noqa: BLE001 — surface as futures down, not a crash
        return _strip_secrets(_handle_futures_down(conn, now=ts, send=send_fn, error=type(exc).__name__))

    risk_pct = Decimal(str(settings["risk_pct"]))
    night = is_night_hold(now_ms=ts)
    created: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for symbol in ASSIST_SYMBOLS:
        if active_signal_for_symbol(conn, symbol, now_ms=ts) is not None:
            skipped.append({"symbol": symbol, "reason": "active_signal"})
            continue
        if symbol in open_syms:
            skipped.append({"symbol": symbol, "reason": "open_position"})
            continue
        analysis = load_latest_analysis(conn, symbol, ASSIST_TIMEFRAME)
        if analysis is None or not passes_signal_floor(analysis):
            skipped.append({"symbol": symbol, "reason": "floor"})
            continue
        try:
            mark = client.mark_price(symbol)
            step = client.step_size(symbol)
        except Exception as exc:  # noqa: BLE001
            skipped.append({"symbol": symbol, "reason": f"mark:{type(exc).__name__}"})
            continue
        sized = size_signal(
            symbol=symbol,
            side=str(analysis["action"]),
            entry=mark,
            stop=Decimal(str(analysis["invalidation_price"])),
            balance=balance,
            risk_pct=risk_pct,
            step_size=step,
            analysis_id=str(analysis.get("analysis_id") or "") or None,
        )
        if sized is None:
            skipped.append({"symbol": symbol, "reason": "size_failed"})
            continue
        expires = next_4h_close_ms(ts)
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
        sent_ok = send_fn(text)
        sid = insert_signal(
            conn,
            sized,
            status="sent" if sent_ok else "held",
            created_at_ms=ts,
            expires_at_ms=expires,
            sent_at_ms=ts if sent_ok else None,
        )
        created.append(
            {
                "id": sid,
                "symbol": symbol,
                "status": "sent" if sent_ok else "held",
                "telegram": sent_ok,
            }
        )

    return _strip_secrets(
        {
            "ok": True,
            "paper_only": True,
            "at_ms": ts,
            "night_hold": night,
            "balance_usdt": float(balance),
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
    """Re-check held signals at 08:00 Bangkok and send or expire."""
    ts = int(now_ms_value if now_ms_value is not None else now_ms())
    send_fn = send or send_telegram_message
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
        balance = client.usdt_wallet_balance()
        _handle_futures_ok(conn, send=send_fn)
    except FuturesKeyUnsafeError as exc:
        return _strip_secrets(
            {
                "ok": False,
                "error": "key_unsafe",
                "detail": str(exc),
                "paper_only": True,
                "at_ms": ts,
            }
        )
    except FuturesPermissionError as exc:
        edge = get_edge(conn)
        if not edge["permission_error_reported"]:
            send_fn(format_permission_error_alert())
            write_edge(conn, permission_error_reported=True)
        return _strip_secrets(
            {
                "ok": False,
                "error": "permission_error",
                "detail": str(exc),
                "paper_only": True,
                "at_ms": ts,
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _strip_secrets(_handle_futures_down(conn, now=ts, send=send_fn, error=type(exc).__name__))

    risk_pct = Decimal(str(settings["risk_pct"]))
    sent: list[str] = []
    expired: list[str] = []

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
        same_side = (
            analysis is not None
            and passes_signal_floor(analysis)
            and str(analysis.get("action") or "").lower() == side
        )
        chased = chase_exceeded(side=side, entry=entry0, stop=stop0, mark=mark)
        if not same_side or chased:
            mark_signal_expired(conn, str(row["id"]))
            expired.append(str(row["id"]))
            continue
        sized = size_signal(
            symbol=symbol,
            side=side,
            entry=mark,
            stop=stop0,
            balance=balance,
            risk_pct=risk_pct,
            step_size=step,
            analysis_id=str(row.get("analysis_id") or "") or None,
        )
        if sized is None:
            mark_signal_expired(conn, str(row["id"]))
            expired.append(str(row["id"]))
            continue
        expires = next_4h_close_ms(ts)
        text = format_signal_message(sized, expires_at_ms=expires, balance=balance)
        if send_fn(text):
            mark_signal_sent(conn, str(row["id"]), sized, sent_at_ms=ts, expires_at_ms=expires)
            sent.append(str(row["id"]))
        else:
            # Keep held if Telegram fails.
            pass

    return _strip_secrets(
        {
            "ok": True,
            "paper_only": True,
            "at_ms": ts,
            "sent": sent,
            "expired": expired,
            "balance_usdt": float(balance),
        }
    )
