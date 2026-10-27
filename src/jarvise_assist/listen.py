"""Telegram long-poll listener for the trade assistant.

Runs as a daemon thread started from ``jarvise.jobs.main`` inside the existing
``jobs`` container. That process already has ``TELEGRAM_*``, the SQLite bind, and
no public HTTPS. A second compose service would be a second writer on the same
DB. The HTTP server stays on ``ThreadingHTTPServer``; this thread never takes a
job lock. Exceptions are caught inside the loop so the jobs process never exits.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from jarvise_assist.futures import build_futures_client
from jarvise_assist.keyboard import reply_keyboard
from jarvise_assist.menu import (
    handle_menu_text,
    handle_risk_callback,
    handle_risk_number,
)
from jarvise_assist.store import (
    ensure_settings,
    get_edge,
    get_signal,
    get_telegram_state,
    mark_signal_skipped,
    now_ms,
    write_edge,
    write_telegram_state,
)
from jarvise_ingest.db import open_db
from jarvise_notify.telegram import (
    answer_callback,
    edit_message,
    get_updates,
    notify_configured,
    send_message,
)

logger = logging.getLogger(__name__)

LISTENER_DOWN_ALERT_MS = 30 * 60 * 1000
BACKOFF_SLEEP_S = (5, 15, 60)
POLL_TIMEOUT_S = 25

_listener_started = False
_listener_lock = threading.Lock()


def owner_chat_id() -> str:
    return (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()


def process_update(
    conn: Any,
    update: dict[str, Any],
    *,
    send: Callable[..., Any] | None = None,
    answer: Callable[..., Any] | None = None,
    edit: Callable[..., Any] | None = None,
    futures: Any = None,
    now_ms_value: int | None = None,
) -> dict[str, Any]:
    """Handle one Telegram update. Ignores foreign chats with no reply."""
    ts = int(now_ms_value if now_ms_value is not None else now_ms())
    send_fn = send or send_message
    answer_fn = answer or answer_callback
    edit_fn = edit or edit_message
    expected = owner_chat_id()
    if not expected:
        return {"ignored": "no_owner_chat"}

    callback = update.get("callback_query")
    if isinstance(callback, dict):
        msg = callback.get("message") or {}
        chat = (msg.get("chat") or {}).get("id")
        if str(chat) != expected:
            return {"ignored": "foreign_chat"}
        data = str(callback.get("data") or "")
        cq_id = str(callback.get("id") or "")
        if data.startswith("risk:") or data.startswith("risk_yes:") or data == "risk_no":
            answer_fn(cq_id)
            return handle_risk_callback(conn, data, send=send_fn)
        row = get_signal(conn, data) if data else None
        if row is not None:
            signal_id = data
            changed = mark_signal_skipped(conn, signal_id)
            answer_fn(cq_id, text="ข้ามแล้ว" if changed else None)
            if changed:
                mid = row.get("telegram_message_id") or (msg.get("message_id"))
                base = str(msg.get("text") or "") or _signal_text_fallback(row)
                if "(ข้ามแล้ว)" not in base:
                    base = base + "\n(ข้ามแล้ว)"
                if mid is not None:
                    edit_fn(int(mid), base, reply_markup={"inline_keyboard": []})
            return {"ok": True, "skip": signal_id, "changed": changed}
        answer_fn(cq_id)
        return {"ok": True, "unhandled_callback": data}

    message = update.get("message")
    if not isinstance(message, dict):
        return {"ignored": "no_message"}
    chat = (message.get("chat") or {}).get("id")
    if str(chat) != expected:
        return {"ignored": "foreign_chat"}
    text = str(message.get("text") or "")
    risk_num = handle_risk_number(conn, text, send=send_fn)
    if risk_num.get("handled"):
        return risk_num
    client = futures if futures is not None else build_futures_client()
    return handle_menu_text(conn, text, futures=client, now_ms=ts, send=send_fn)


def _signal_text_fallback(row: dict[str, Any]) -> str:
    side = str(row.get("side") or "").lower()
    emoji = "🔺" if side == "long" else "🔻"
    label = "LONG" if side == "long" else "SHORT"
    return f"{emoji} {row.get('symbol')} · {label} (4h)\nเข้า: {row.get('entry')}\nStop: {row.get('stop')}"


def poll_once(
    conn: Any,
    *,
    get_updates_fn: Callable[..., list[dict[str, Any]] | None] | None = None,
    process_fn: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One getUpdates cycle. Persists offset. Returns status for edge handling."""
    state = get_telegram_state(conn)
    offset = None if state["last_update_id"] is None else int(state["last_update_id"]) + 1
    fetch = get_updates_fn or get_updates
    updates = fetch(offset=offset, timeout=POLL_TIMEOUT_S)
    if updates is None:
        return {"ok": False, "error": "transport"}
    max_id = state["last_update_id"]
    handler = process_fn or process_update
    for upd in updates:
        uid = upd.get("update_id")
        if uid is not None:
            max_id = int(uid) if max_id is None else max(int(max_id), int(uid))
        try:
            handler(conn, upd)
        except Exception:  # noqa: BLE001
            logger.exception("assist listener update failed")
    if max_id is not None and max_id != state["last_update_id"]:
        write_telegram_state(conn, last_update_id=max_id)
    return {"ok": True, "n": len(updates), "last_update_id": max_id}


def _ensure_keyboard(conn: Any) -> None:
    state = get_telegram_state(conn)
    if state["keyboard_sent"]:
        return
    settings = ensure_settings(conn)
    mid = send_message("เมนูผู้ช่วยพร้อมใช้", reply_markup=reply_keyboard(settings["risk_pct"]))
    if mid is not None:
        write_telegram_state(conn, keyboard_sent=True)


def _note_listener_failure(conn: Any, *, now: int, send: Callable[..., Any]) -> None:
    edge = get_edge(conn)
    since = edge["listener_down_since_ms"]
    if since is None:
        write_edge(conn, listener_down_since_ms=now, listener_down_alerted=False)
        return
    if not edge["listener_down_alerted"] and (now - int(since)) >= LISTENER_DOWN_ALERT_MS:
        if send("Jarvise trade assist: Telegram listener หยุดเกิน 30 นาที"):
            write_edge(conn, listener_down_alerted=True)


def _note_listener_ok(conn: Any, *, send: Callable[..., Any]) -> None:
    edge = get_edge(conn)
    if edge["listener_down_alerted"]:
        send("Jarvise trade assist: Telegram listener กลับมาแล้ว")
    if edge["listener_down_since_ms"] is not None or edge["listener_down_alerted"]:
        write_edge(conn, listener_down_since_ms=None, listener_down_alerted=False)


def _default_db_path() -> Path:
    raw = (os.environ.get("JARVISE_DB") or "").strip()
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parents[2] / "data" / "analytics" / "jarvise.db"


def listener_loop(
    *,
    db_path: Path | str | None = None,
    stop_event: threading.Event | None = None,
    get_updates_fn: Callable[..., list[dict[str, Any]] | None] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> None:
    """Blocking poll loop. Never raises out; sleeps on backoff."""
    path = Path(db_path) if db_path is not None else _default_db_path()
    fail_streak = 0
    while stop_event is None or not stop_event.is_set():
        try:
            if not notify_configured():
                sleep_fn(60)
                continue
            if not path.exists():
                sleep_fn(30)
                continue
            conn = open_db(path)
            try:
                _ensure_keyboard(conn)
                result = poll_once(conn, get_updates_fn=get_updates_fn)
                now = now_ms()
                if result.get("ok"):
                    _note_listener_ok(conn, send=send_message)
                    fail_streak = 0
                else:
                    _note_listener_failure(conn, now=now, send=send_message)
                    fail_streak += 1
                    sleep_fn(BACKOFF_SLEEP_S[min(fail_streak - 1, len(BACKOFF_SLEEP_S) - 1)])
                    continue
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            logger.exception("assist listener loop error")
            fail_streak += 1
            sleep_fn(BACKOFF_SLEEP_S[min(fail_streak - 1, len(BACKOFF_SLEEP_S) - 1)])


def start_listener_thread(*, db_path: Path | str | None = None) -> threading.Thread | None:
    """Start the daemon once. Returns the thread, or None if already started or chat id unset."""
    global _listener_started
    if not owner_chat_id():
        logger.warning("assist telegram listener not started: TELEGRAM_CHAT_ID is empty")
        return None
    with _listener_lock:
        if _listener_started:
            return None
        _listener_started = True
    thread = threading.Thread(
        target=listener_loop,
        kwargs={"db_path": db_path},
        name="jarvise-assist-listen",
        daemon=True,
    )
    thread.start()
    logger.info("assist telegram listener thread started")
    return thread
