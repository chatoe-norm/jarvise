"""SQLite helpers for trade-assistant tables."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from jarvise_assist.sizing import SAME_IDEA_MS, SizedSignal

_UNSET: object = object()


RISK_MAX_PCT = 3.0


def clamp_stored_risk_pct(
    conn: Any,
    *,
    send: Any | None = None,
    reply_keyboard_fn: Any | None = None,
) -> float:
    """If stored risk_pct > 3, write 3 and notify once. Returns the effective pct."""
    settings = ensure_settings(conn)
    pct = float(settings["risk_pct"])
    if pct <= RISK_MAX_PCT:
        return pct
    write_settings(conn, risk_pct=RISK_MAX_PCT)
    if send is not None:
        markup = None
        if reply_keyboard_fn is not None:
            markup = reply_keyboard_fn(RISK_MAX_PCT)
        try:
            send(
                "ความเสี่ยงที่บันทึกไว้เกิน 3% — ปรับเป็น 3% แล้ว",
                reply_markup=markup,
            )
        except TypeError:
            send("ความเสี่ยงที่บันทึกไว้เกิน 3% — ปรับเป็น 3% แล้ว")
    return RISK_MAX_PCT


def ensure_settings(conn: Any) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT risk_pct, enabled, paused_until_ms, awaiting_risk, pending_risk_pct,
               awaiting_explore
        FROM assist_settings WHERE id=1
        """
    ).fetchone()
    if row is None:
        conn.execute("INSERT INTO assist_settings (id, risk_pct, enabled) VALUES (1, 1.0, 1)")
        conn.commit()
        return {
            "risk_pct": 1.0,
            "enabled": True,
            "paused_until_ms": None,
            "awaiting_risk": None,
            "pending_risk_pct": None,
            "awaiting_explore": False,
        }
    # Older DBs before migration 16 may lack the trailing columns.
    paused = row[2] if len(row) > 2 else None
    awaiting = row[3] if len(row) > 3 else None
    pending = row[4] if len(row) > 4 else None
    exploring = row[5] if len(row) > 5 else 0
    return {
        "risk_pct": float(row[0]),
        "enabled": bool(int(row[1])),
        "paused_until_ms": int(paused) if paused is not None else None,
        "awaiting_risk": str(awaiting) if awaiting else None,
        "pending_risk_pct": float(pending) if pending is not None else None,
        "awaiting_explore": bool(int(exploring or 0)),
    }


def write_settings(
    conn: Any,
    *,
    risk_pct: float | None = None,
    enabled: bool | None = None,
    paused_until_ms: Any = _UNSET,
    awaiting_risk: Any = _UNSET,
    pending_risk_pct: Any = _UNSET,
    awaiting_explore: Any = _UNSET,
) -> None:
    cur = ensure_settings(conn)
    rp = float(cur["risk_pct"] if risk_pct is None else risk_pct)
    en = int(cur["enabled"] if enabled is None else enabled)
    paused = cur["paused_until_ms"] if paused_until_ms is _UNSET else paused_until_ms
    awaiting = cur["awaiting_risk"] if awaiting_risk is _UNSET else awaiting_risk
    pending = cur["pending_risk_pct"] if pending_risk_pct is _UNSET else pending_risk_pct
    exploring = cur["awaiting_explore"] if awaiting_explore is _UNSET else bool(awaiting_explore)
    conn.execute(
        """
        INSERT INTO assist_settings (
            id, risk_pct, enabled, paused_until_ms, awaiting_risk, pending_risk_pct,
            awaiting_explore
        ) VALUES (1, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            risk_pct=excluded.risk_pct,
            enabled=excluded.enabled,
            paused_until_ms=excluded.paused_until_ms,
            awaiting_risk=excluded.awaiting_risk,
            pending_risk_pct=excluded.pending_risk_pct,
            awaiting_explore=excluded.awaiting_explore
        """,
        (rp, en, paused, awaiting, pending, int(bool(exploring))),
    )
    conn.commit()


def get_edge(conn: Any) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT futures_down_since_ms, futures_down_alerted,
               permission_error_reported, key_unsafe_reported,
               streak_anchor_ms, week_pause_id,
               listener_down_since_ms, listener_down_alerted, resume_alerted,
               day_pause_id, day_resume_net_r, day_resume_id, day_deeper_pause_id
        FROM assist_edge WHERE id=1
        """
    ).fetchone()
    if row is None:
        conn.execute(
            """
            INSERT INTO assist_edge (
                id, futures_down_since_ms, futures_down_alerted,
                permission_error_reported, key_unsafe_reported
            ) VALUES (1, NULL, 0, 0, 0)
            """
        )
        conn.commit()
        return {
            "futures_down_since_ms": None,
            "futures_down_alerted": False,
            "permission_error_reported": False,
            "key_unsafe_reported": False,
            "streak_anchor_ms": None,
            "week_pause_id": None,
            "listener_down_since_ms": None,
            "listener_down_alerted": False,
            "resume_alerted": False,
            "day_pause_id": None,
            "day_resume_net_r": None,
            "day_resume_id": None,
            "day_deeper_pause_id": None,
        }

    def _i(idx: int) -> Any:
        return row[idx] if len(row) > idx else None

    def _b(idx: int) -> bool:
        raw = _i(idx)
        return bool(int(raw)) if raw is not None else False

    return {
        "futures_down_since_ms": _i(0),
        "futures_down_alerted": _b(1),
        "permission_error_reported": _b(2),
        "key_unsafe_reported": _b(3),
        "streak_anchor_ms": int(_i(4)) if _i(4) is not None else None,
        "week_pause_id": str(_i(5)) if _i(5) else None,
        "listener_down_since_ms": int(_i(6)) if _i(6) is not None else None,
        "listener_down_alerted": _b(7),
        "resume_alerted": _b(8),
        "day_pause_id": str(_i(9)) if _i(9) else None,
        "day_resume_net_r": float(_i(10)) if _i(10) is not None else None,
        "day_resume_id": str(_i(11)) if _i(11) else None,
        "day_deeper_pause_id": str(_i(12)) if _i(12) else None,
    }


def write_edge(
    conn: Any,
    *,
    futures_down_since_ms: Any = _UNSET,
    futures_down_alerted: bool | None = None,
    permission_error_reported: bool | None = None,
    key_unsafe_reported: bool | None = None,
    streak_anchor_ms: Any = _UNSET,
    week_pause_id: Any = _UNSET,
    listener_down_since_ms: Any = _UNSET,
    listener_down_alerted: bool | None = None,
    resume_alerted: bool | None = None,
    day_pause_id: Any = _UNSET,
    day_resume_net_r: Any = _UNSET,
    day_resume_id: Any = _UNSET,
    day_deeper_pause_id: Any = _UNSET,
) -> None:
    cur = get_edge(conn)
    since = cur["futures_down_since_ms"] if futures_down_since_ms is _UNSET else futures_down_since_ms
    down_alerted = (
        int(futures_down_alerted) if futures_down_alerted is not None else int(cur["futures_down_alerted"])
    )
    perm = (
        int(permission_error_reported)
        if permission_error_reported is not None
        else int(cur["permission_error_reported"])
    )
    unsafe = int(key_unsafe_reported) if key_unsafe_reported is not None else int(cur["key_unsafe_reported"])
    streak = cur["streak_anchor_ms"] if streak_anchor_ms is _UNSET else streak_anchor_ms
    week = cur["week_pause_id"] if week_pause_id is _UNSET else week_pause_id
    listen_since = (
        cur["listener_down_since_ms"] if listener_down_since_ms is _UNSET else listener_down_since_ms
    )
    listen_alerted = (
        int(listener_down_alerted) if listener_down_alerted is not None else int(cur["listener_down_alerted"])
    )
    resume = int(resume_alerted) if resume_alerted is not None else int(cur["resume_alerted"])
    day_pause = cur["day_pause_id"] if day_pause_id is _UNSET else day_pause_id
    day_resume_r = cur["day_resume_net_r"] if day_resume_net_r is _UNSET else day_resume_net_r
    day_resume = cur["day_resume_id"] if day_resume_id is _UNSET else day_resume_id
    day_deeper = cur["day_deeper_pause_id"] if day_deeper_pause_id is _UNSET else day_deeper_pause_id
    conn.execute(
        """
        INSERT INTO assist_edge (
            id, futures_down_since_ms, futures_down_alerted,
            permission_error_reported, key_unsafe_reported,
            streak_anchor_ms, week_pause_id,
            listener_down_since_ms, listener_down_alerted, resume_alerted,
            day_pause_id, day_resume_net_r, day_resume_id, day_deeper_pause_id
        ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            futures_down_since_ms=excluded.futures_down_since_ms,
            futures_down_alerted=excluded.futures_down_alerted,
            permission_error_reported=excluded.permission_error_reported,
            key_unsafe_reported=excluded.key_unsafe_reported,
            streak_anchor_ms=excluded.streak_anchor_ms,
            week_pause_id=excluded.week_pause_id,
            listener_down_since_ms=excluded.listener_down_since_ms,
            listener_down_alerted=excluded.listener_down_alerted,
            resume_alerted=excluded.resume_alerted,
            day_pause_id=excluded.day_pause_id,
            day_resume_net_r=excluded.day_resume_net_r,
            day_resume_id=excluded.day_resume_id,
            day_deeper_pause_id=excluded.day_deeper_pause_id
        """,
        (
            since,
            down_alerted,
            perm,
            unsafe,
            streak,
            week,
            listen_since,
            listen_alerted,
            resume,
            day_pause,
            day_resume_r,
            day_resume,
            day_deeper,
        ),
    )
    conn.commit()


def list_held_signals(conn: Any) -> list[dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT id, analysis_id, symbol, side, entry, stop, target, qty, notional,
               leverage, loss_at_stop, created_at_ms, expires_at_ms, status, sent_at_ms,
               leverage_capped, uncapped_leverage
        FROM assist_signal
        WHERE status='held'
        ORDER BY created_at_ms ASC
        """
    )
    return [dict(r) for r in cur.fetchall()]


def expire_older_held(conn: Any, symbol: str, *, keep_id: str) -> list[str]:
    """Expire other held rows for symbol; return expired ids."""
    cur = conn.execute(
        """
        SELECT id FROM assist_signal
        WHERE symbol=? AND status='held' AND id != ?
        """,
        (symbol.upper(), keep_id),
    )
    ids = [str(r[0]) for r in cur.fetchall()]
    if ids:
        conn.executemany(
            "UPDATE assist_signal SET status='expired' WHERE id=?",
            [(i,) for i in ids],
        )
        conn.commit()
    return ids


def expire_older_held_keep_newest(conn: Any) -> list[str]:
    """Per symbol keep newest held; expire the rest. Returns expired ids."""
    expired: list[str] = []
    cur = conn.execute(
        """
        SELECT symbol, id FROM assist_signal
        WHERE status='held'
        ORDER BY symbol ASC, created_at_ms DESC
        """
    )
    seen: set[str] = set()
    for symbol, signal_id in cur.fetchall():
        sym = str(symbol).upper()
        if sym in seen:
            mark_signal_expired(conn, str(signal_id))
            expired.append(str(signal_id))
        else:
            seen.add(sym)
    return expired


def recent_same_idea(
    conn: Any,
    *,
    symbol: str,
    side: str,
    now_ms: int,
    within_ms: int = SAME_IDEA_MS,
) -> bool:
    """True when the owner already saw this idea (sent, taken, or skipped) within within_ms.

    Held or expired rows never reached the owner, so they must not block a resend.
    """
    row = conn.execute(
        """
        SELECT 1 FROM assist_signal
        WHERE symbol=? AND side=? AND status IN ('sent', 'taken', 'skipped')
          AND created_at_ms >= ?
        LIMIT 1
        """,
        (symbol.upper(), side.lower(), int(now_ms) - int(within_ms)),
    ).fetchone()
    return row is not None


def latest_seen_idea(
    conn: Any,
    *,
    symbol: str,
    side: str,
    now_ms: int,
    within_ms: int = SAME_IDEA_MS,
) -> dict[str, Any] | None:
    """Newest sent/taken/skipped row for symbol+side within within_ms, or None."""
    cur = conn.execute(
        """
        SELECT id, symbol, side, entry, stop, target, qty, loss_at_stop,
               created_at_ms, sent_at_ms, status
        FROM assist_signal
        WHERE symbol=? AND side=? AND status IN ('sent', 'taken', 'skipped')
          AND created_at_ms >= ?
        ORDER BY COALESCE(sent_at_ms, created_at_ms) DESC, created_at_ms DESC
        LIMIT 1
        """,
        (symbol.upper(), side.lower(), int(now_ms) - int(within_ms)),
    )
    row = cur.fetchone()
    return dict(row) if row is not None else None


def previous_analysis_action(
    conn: Any,
    *,
    symbol: str,
    timeframe: str,
) -> str | None:
    """Action of the second-newest analysis row, or None when absent."""
    cur = conn.execute(
        """
        SELECT action FROM analysis_output
        WHERE symbol=? AND timeframe=?
        ORDER BY timestamp DESC
        LIMIT 2
        """,
        (symbol.upper(), timeframe),
    )
    rows = cur.fetchall()
    if len(rows) < 2:
        return None
    return str(rows[1][0] or "").lower() or None


def _track_cohort() -> str:
    # Function-local: jarvise_assist.track imports close_track from this module.
    from jarvise_assist.track import TRACK_COHORT

    return TRACK_COHORT


def _open_track_for_symbol_side(
    conn: Any,
    symbol: str,
    side: str,
    *,
    exclude_blocked: bool = True,
) -> bool:
    sql = """
        SELECT 1 FROM assist_track
        WHERE symbol=? AND side=? AND exit_at_ms IS NULL
        """
    if exclude_blocked:
        sql += " AND kind != 'blocked'"
    sql += " LIMIT 1"
    row = conn.execute(sql, (symbol.upper(), side.lower())).fetchone()
    return row is not None


_BLOCKED_REASONS = frozenset({"risk_cap", "margin_cap", "streak_pause"})


def record_blocked_signal(
    conn: Any,
    sized: SizedSignal,
    *,
    reason: str,
    created_at_ms: int,
) -> str | None:
    """Insert a blocked virtual track. Never enters sent/held sample or risk slots.

    Returns the new signal id, or None when an open track already exists for
    symbol+side (any kind) or the reason is not allowed.
    """
    if reason not in _BLOCKED_REASONS:
        return None
    # Any open track (including blocked) blocks another blocked row for the pair.
    if _open_track_for_symbol_side(conn, sized.symbol, sized.side, exclude_blocked=False):
        return None
    signal_id = f"as_{uuid.uuid4().hex[:16]}"
    ts = int(created_at_ms)
    conn.execute(
        """
        INSERT INTO assist_signal (
            id, analysis_id, symbol, side, entry, stop, target, qty, notional,
            leverage, loss_at_stop, created_at_ms, expires_at_ms, status, sent_at_ms,
            leverage_capped, uncapped_leverage
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, 'blocked', ?,
            ?, ?
        )
        """,
        (
            signal_id,
            sized.analysis_id,
            sized.symbol,
            sized.side,
            float(sized.entry),
            float(sized.stop),
            float(sized.target),
            float(sized.qty),
            float(sized.notional),
            int(sized.leverage),
            float(sized.loss_at_stop),
            ts,
            ts,
            ts,
            1 if sized.leverage_capped else 0,
            int(sized.uncapped_leverage),
        ),
    )
    cohort = _track_cohort()
    conn.execute(
        """
        INSERT OR IGNORE INTO assist_track (
            signal_id, symbol, side, entry, stop, target, qty, sent_at_ms,
            cohort, kind, block_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'blocked', ?)
        """,
        (
            signal_id,
            sized.symbol,
            sized.side,
            float(sized.entry),
            float(sized.stop),
            float(sized.target),
            float(sized.qty),
            ts,
            cohort,
            reason,
        ),
    )
    conn.commit()
    return signal_id


def insert_signal(
    conn: Any,
    sized: SizedSignal,
    *,
    status: str,
    created_at_ms: int,
    expires_at_ms: int,
    sent_at_ms: int | None = None,
) -> str:
    signal_id = f"as_{uuid.uuid4().hex[:16]}"
    conn.execute(
        """
        INSERT INTO assist_signal (
            id, analysis_id, symbol, side, entry, stop, target, qty, notional,
            leverage, loss_at_stop, created_at_ms, expires_at_ms, status, sent_at_ms,
            leverage_capped, uncapped_leverage
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?,
            ?, ?
        )
        """,
        (
            signal_id,
            sized.analysis_id,
            sized.symbol,
            sized.side,
            float(sized.entry),
            float(sized.stop),
            float(sized.target),
            float(sized.qty),
            float(sized.notional),
            int(sized.leverage),
            float(sized.loss_at_stop),
            int(created_at_ms),
            int(expires_at_ms),
            status,
            sent_at_ms,
            1 if sized.leverage_capped else 0,
            int(sized.uncapped_leverage),
        ),
    )
    if status in {"held", "sent"}:
        expire_older_held(conn, sized.symbol, keep_id=signal_id)
    cohort = _track_cohort()
    if status == "held":
        if not _open_track_for_symbol_side(conn, sized.symbol, sized.side):
            conn.execute(
                """
                INSERT OR IGNORE INTO assist_track (
                    signal_id, symbol, side, entry, stop, target, qty, sent_at_ms,
                    cohort, kind
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'held')
                """,
                (
                    signal_id,
                    sized.symbol,
                    sized.side,
                    float(sized.entry),
                    float(sized.stop),
                    float(sized.target),
                    float(sized.qty),
                    int(created_at_ms),
                    cohort,
                ),
            )
    elif status == "sent" and sent_at_ms is not None:
        conn.execute(
            """
            INSERT OR IGNORE INTO assist_track (
                signal_id, symbol, side, entry, stop, target, qty, sent_at_ms,
                cohort, kind
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'sent')
            """,
            (
                signal_id,
                sized.symbol,
                sized.side,
                float(sized.entry),
                float(sized.stop),
                float(sized.target),
                float(sized.qty),
                int(sent_at_ms),
                cohort,
            ),
        )
    conn.commit()
    return signal_id


def mark_signal_sent(
    conn: Any,
    signal_id: str,
    sized: SizedSignal,
    *,
    sent_at_ms: int,
    expires_at_ms: int,
) -> None:
    conn.execute(
        """
        UPDATE assist_signal SET
            status='sent',
            sent_at_ms=?,
            entry=?,
            stop=?,
            target=?,
            qty=?,
            notional=?,
            leverage=?,
            loss_at_stop=?,
            expires_at_ms=?,
            leverage_capped=?,
            uncapped_leverage=?
        WHERE id=?
        """,
        (
            int(sent_at_ms),
            float(sized.entry),
            float(sized.stop),
            float(sized.target),
            float(sized.qty),
            float(sized.notional),
            int(sized.leverage),
            float(sized.loss_at_stop),
            int(expires_at_ms),
            1 if sized.leverage_capped else 0,
            int(sized.uncapped_leverage),
            signal_id,
        ),
    )
    cohort = _track_cohort()
    # Promote an open held track for this signal; leave a closed held row alone.
    cur = conn.execute(
        """
        UPDATE assist_track SET
            kind='sent',
            entry=?,
            stop=?,
            target=?,
            qty=?,
            sent_at_ms=?,
            cohort=?
        WHERE signal_id=? AND exit_at_ms IS NULL
        """,
        (
            float(sized.entry),
            float(sized.stop),
            float(sized.target),
            float(sized.qty),
            int(sent_at_ms),
            cohort,
            signal_id,
        ),
    )
    # A sent signal always gets its own track, even when another track for the same
    # symbol+side is open (a superseded night idea, or a repeat idea after 24 h).
    if int(cur.rowcount or 0) == 0:
        conn.execute(
            """
            INSERT OR IGNORE INTO assist_track (
                signal_id, symbol, side, entry, stop, target, qty, sent_at_ms,
                cohort, kind
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'sent')
            """,
            (
                signal_id,
                sized.symbol,
                sized.side,
                float(sized.entry),
                float(sized.stop),
                float(sized.target),
                float(sized.qty),
                int(sent_at_ms),
                cohort,
            ),
        )
    conn.commit()


def mark_signal_expired(conn: Any, signal_id: str) -> None:
    conn.execute(
        "UPDATE assist_signal SET status='expired' WHERE id=?",
        (signal_id,),
    )
    conn.commit()


def mark_signal_taken(conn: Any, signal_id: str) -> None:
    conn.execute(
        "UPDATE assist_signal SET status='taken' WHERE id=?",
        (signal_id,),
    )
    conn.commit()


def mark_signal_skipped(conn: Any, signal_id: str) -> bool:
    """Set status=skipped when still sent or held. Returns True when a row changed."""
    cur = conn.execute(
        """
        UPDATE assist_signal SET status='skipped'
        WHERE id=? AND status IN ('sent', 'held')
        """,
        (signal_id,),
    )
    conn.commit()
    return int(cur.rowcount or 0) > 0


def set_signal_telegram_message_id(conn: Any, signal_id: str, message_id: int) -> None:
    conn.execute(
        "UPDATE assist_signal SET telegram_message_id=? WHERE id=?",
        (int(message_id), signal_id),
    )
    conn.commit()


def get_signal(conn: Any, signal_id: str) -> dict[str, Any] | None:
    cur = conn.execute(
        """
        SELECT id, analysis_id, symbol, side, entry, stop, target, qty, notional,
               leverage, loss_at_stop, created_at_ms, expires_at_ms, status, sent_at_ms,
               leverage_capped, uncapped_leverage, telegram_message_id
        FROM assist_signal WHERE id=?
        """,
        (signal_id,),
    )
    row = cur.fetchone()
    return dict(row) if row is not None else None


def get_telegram_state(conn: Any) -> dict[str, Any]:
    row = conn.execute("SELECT last_update_id, keyboard_sent FROM assist_telegram WHERE id=1").fetchone()
    if row is None:
        conn.execute(
            "INSERT OR IGNORE INTO assist_telegram (id, last_update_id, keyboard_sent) VALUES (1, NULL, 0)"
        )
        conn.commit()
        return {"last_update_id": None, "keyboard_sent": False}
    return {
        "last_update_id": int(row[0]) if row[0] is not None else None,
        "keyboard_sent": bool(int(row[1])),
    }


def write_telegram_state(
    conn: Any,
    *,
    last_update_id: Any = _UNSET,
    keyboard_sent: bool | None = None,
) -> None:
    cur = get_telegram_state(conn)
    uid = cur["last_update_id"] if last_update_id is _UNSET else last_update_id
    ks = int(cur["keyboard_sent"] if keyboard_sent is None else keyboard_sent)
    conn.execute(
        """
        INSERT INTO assist_telegram (id, last_update_id, keyboard_sent)
        VALUES (1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            last_update_id=excluded.last_update_id,
            keyboard_sent=excluded.keyboard_sent
        """,
        (uid, ks),
    )
    conn.commit()


def list_active_idea_signals(
    conn: Any,
    *,
    now_ms: int,
    within_ms: int,
) -> list[dict[str, Any]]:
    """sent/held signals created in the lookback window and not yet expired."""
    cur = conn.execute(
        """
        SELECT id, analysis_id, symbol, side, entry, stop, target, qty, notional,
               leverage, loss_at_stop, created_at_ms, expires_at_ms, status, sent_at_ms,
               leverage_capped, uncapped_leverage
        FROM assist_signal
        WHERE status IN ('sent', 'held')
          AND created_at_ms >= ?
          AND expires_at_ms > ?
        ORDER BY created_at_ms DESC
        """,
        (int(now_ms) - int(within_ms), int(now_ms)),
    )
    return [dict(r) for r in cur.fetchall()]


def find_linkable_signal(
    conn: Any,
    *,
    symbol: str,
    side: str,
    now_ms: int,
) -> dict[str, Any] | None:
    """Newest still-valid sent/held signal for symbol+side."""
    cur = conn.execute(
        """
        SELECT id, analysis_id, symbol, side, entry, stop, target, qty, notional,
               leverage, loss_at_stop, created_at_ms, expires_at_ms, status, sent_at_ms,
               leverage_capped, uncapped_leverage
        FROM assist_signal
        WHERE symbol=? AND side=? AND status IN ('sent', 'held')
          AND expires_at_ms > ?
        ORDER BY created_at_ms DESC
        LIMIT 1
        """,
        (symbol.upper(), side.lower(), int(now_ms)),
    )
    row = cur.fetchone()
    return dict(row) if row is not None else None


def upsert_watch_position(conn: Any, row: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO assist_watch_position (
            symbol, position_side, side, entry, qty, in_universe, signal_id,
            initial_stop, initial_stop_source, entry_time_ms, no_stop_latched,
            last_seen_ms, closed_at_ms
        ) VALUES (
            :symbol, :position_side, :side, :entry, :qty, :in_universe, :signal_id,
            :initial_stop, :initial_stop_source, :entry_time_ms, :no_stop_latched,
            :last_seen_ms, :closed_at_ms
        )
        ON CONFLICT(symbol, position_side) DO UPDATE SET
            side=excluded.side,
            entry=excluded.entry,
            qty=excluded.qty,
            in_universe=excluded.in_universe,
            signal_id=COALESCE(assist_watch_position.signal_id, excluded.signal_id),
            initial_stop=COALESCE(assist_watch_position.initial_stop, excluded.initial_stop),
            initial_stop_source=COALESCE(
                assist_watch_position.initial_stop_source, excluded.initial_stop_source
            ),
            entry_time_ms=COALESCE(assist_watch_position.entry_time_ms, excluded.entry_time_ms),
            no_stop_latched=excluded.no_stop_latched,
            last_seen_ms=excluded.last_seen_ms,
            closed_at_ms=excluded.closed_at_ms
        """,
        row,
    )
    conn.commit()


def get_watch_position(conn: Any, symbol: str, position_side: str) -> dict[str, Any] | None:
    cur = conn.execute(
        """
        SELECT symbol, position_side, side, entry, qty, in_universe, signal_id,
               initial_stop, initial_stop_source, entry_time_ms, no_stop_latched,
               last_seen_ms, closed_at_ms
        FROM assist_watch_position
        WHERE symbol=? AND position_side=?
        """,
        (symbol.upper(), position_side.upper()),
    )
    row = cur.fetchone()
    return dict(row) if row is not None else None


def reset_watch_position(conn: Any, symbol: str, position_side: str) -> None:
    """Forget a finished position so a new one on the same key starts clean.

    Drops its held advice too; sent advice stays as history and is ignored by
    ``advice_already_sent(since_ms=entry_time)`` for the new position.
    """
    key = (symbol.upper(), position_side.upper())
    conn.execute("DELETE FROM assist_watch_position WHERE symbol=? AND position_side=?", key)
    conn.execute(
        """
        UPDATE assist_watch_advice SET status='dropped'
        WHERE symbol=? AND position_side=? AND status='held'
        """,
        key,
    )
    conn.commit()


def set_watch_no_stop_latched(conn: Any, symbol: str, position_side: str, *, latched: bool) -> None:
    conn.execute(
        """
        UPDATE assist_watch_position SET no_stop_latched=?
        WHERE symbol=? AND position_side=?
        """,
        (1 if latched else 0, symbol.upper(), position_side.upper()),
    )
    if not latched:
        # Allow a new warning after a stop appears and later disappears.
        conn.execute(
            """
            UPDATE assist_watch_advice SET status='dropped'
            WHERE symbol=? AND position_side=? AND kind='no_stop'
              AND status IN ('sent', 'held')
            """,
            (symbol.upper(), position_side.upper()),
        )
    conn.commit()


def latest_no_stop_reason(conn: Any, symbol: str, position_side: str) -> str | None:
    """Most recent sent/held no-stop reason for this position, or None."""
    row = conn.execute(
        """
        SELECT reason FROM assist_watch_advice
        WHERE symbol=? AND position_side=? AND kind='no_stop'
          AND status IN ('sent', 'held')
        ORDER BY created_at_ms DESC
        LIMIT 1
        """,
        (symbol.upper(), position_side.upper()),
    ).fetchone()
    if row is None:
        return None
    raw = row["reason"] if hasattr(row, "keys") else row[0]
    return str(raw) if raw is not None else None


def mark_watch_positions_closed(
    conn: Any, *, open_keys: set[tuple[str, str]], now_ms: int
) -> list[tuple[str, str]]:
    """Mark previously open watch rows closed when absent from open_keys."""
    cur = conn.execute(
        """
        SELECT symbol, position_side FROM assist_watch_position
        WHERE closed_at_ms IS NULL
        """
    )
    closed: list[tuple[str, str]] = []
    for symbol, position_side in cur.fetchall():
        key = (str(symbol).upper(), str(position_side).upper())
        if key not in open_keys:
            conn.execute(
                """
                UPDATE assist_watch_position SET closed_at_ms=?
                WHERE symbol=? AND position_side=?
                """,
                (int(now_ms), key[0], key[1]),
            )
            closed.append(key)
    if closed:
        conn.commit()
    return closed


def advice_already_sent(
    conn: Any,
    *,
    symbol: str,
    position_side: str,
    kind: str,
    reason: str | None = None,
    level: float | None = None,
    since_ms: int | None = None,
) -> bool:
    sql = """
        SELECT 1 FROM assist_watch_advice
        WHERE symbol=? AND position_side=? AND kind=? AND status IN ('sent', 'held')
    """
    params: list[Any] = [symbol.upper(), position_side.upper(), kind]
    if since_ms is not None:
        # Only advice for the current position counts; an earlier trade on the
        # same symbol must not silence it.
        sql += " AND created_at_ms >= ?"
        params.append(int(since_ms))
    if reason is not None:
        sql += " AND reason=?"
        params.append(reason)
    if level is not None:
        sql += " AND level=?"
        params.append(float(level))
    sql += " LIMIT 1"
    return conn.execute(sql, params).fetchone() is not None


def insert_watch_advice(
    conn: Any,
    *,
    symbol: str,
    position_side: str,
    kind: str,
    reason: str | None,
    level: float | None,
    approx_r: float | None,
    status: str,
    created_at_ms: int,
    sent_at_ms: int | None = None,
) -> str:
    advice_id = f"awa_{uuid.uuid4().hex[:16]}"
    conn.execute(
        """
        INSERT INTO assist_watch_advice (
            id, symbol, position_side, kind, reason, level, approx_r,
            status, created_at_ms, sent_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            advice_id,
            symbol.upper(),
            position_side.upper(),
            kind,
            reason,
            level,
            approx_r,
            status,
            int(created_at_ms),
            sent_at_ms,
        ),
    )
    conn.commit()
    return advice_id


def list_held_watch_advice(conn: Any) -> list[dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT id, symbol, position_side, kind, reason, level, approx_r,
               status, created_at_ms, sent_at_ms
        FROM assist_watch_advice
        WHERE status='held'
        ORDER BY created_at_ms ASC
        """
    )
    return [dict(r) for r in cur.fetchall()]


def mark_watch_advice_sent(conn: Any, advice_id: str, *, sent_at_ms: int) -> None:
    conn.execute(
        """
        UPDATE assist_watch_advice SET status='sent', sent_at_ms=?
        WHERE id=?
        """,
        (int(sent_at_ms), advice_id),
    )
    conn.commit()


def mark_watch_advice_dropped(conn: Any, advice_id: str) -> None:
    conn.execute(
        "UPDATE assist_watch_advice SET status='dropped' WHERE id=?",
        (advice_id,),
    )
    conn.commit()


def list_open_tracks(conn: Any) -> list[dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT t.signal_id, t.symbol, t.side, t.entry, t.stop, t.target, t.qty, t.sent_at_ms,
               s.loss_at_stop
        FROM assist_track t
        JOIN assist_signal s ON s.id = t.signal_id
        WHERE t.exit_at_ms IS NULL
        ORDER BY t.sent_at_ms ASC
        """
    )
    return [dict(r) for r in cur.fetchall()]


def close_track(
    conn: Any,
    signal_id: str,
    *,
    exit_at_ms: int,
    exit_px: float,
    exit_reason: str,
    gross_r: float,
    net_r: float,
    fee_usdt: float,
    pnl_usdt: float,
    funding_usdt: float = 0.0,
    funding_missing: int = 0,
) -> None:
    conn.execute(
        """
        UPDATE assist_track SET
            exit_at_ms=?, exit_px=?, exit_reason=?,
            gross_r=?, net_r=?, fee_usdt=?, pnl_usdt=?, funding_usdt=?, funding_missing=?
        WHERE signal_id=? AND exit_at_ms IS NULL
        """,
        (
            int(exit_at_ms),
            float(exit_px),
            exit_reason,
            float(gross_r),
            float(net_r),
            float(fee_usdt),
            float(pnl_usdt),
            float(funding_usdt),
            int(funding_missing),
            signal_id,
        ),
    )
    conn.commit()


def list_closed_tracks_since(
    conn: Any,
    *,
    since_ms: int,
    cohort: str | None = None,
    kind: str | None = None,
) -> list[dict[str, Any]]:
    sql = """
        SELECT signal_id, symbol, side, entry, stop, target, qty, sent_at_ms,
               exit_at_ms, exit_px, exit_reason, gross_r, net_r, fee_usdt, pnl_usdt,
               funding_usdt, funding_missing, cohort, kind, block_reason
        FROM assist_track
        WHERE exit_at_ms IS NOT NULL AND exit_at_ms >= ?
        """
    params: list[Any] = [int(since_ms)]
    if cohort is not None:
        sql += " AND cohort=?"
        params.append(cohort)
    if kind is not None:
        sql += " AND kind=?"
        params.append(kind)
    sql += " ORDER BY exit_at_ms ASC"
    cur = conn.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def count_closed_tracks_all(
    conn: Any,
    *,
    cohort: str | None = None,
    kind: str | None = None,
) -> int:
    sql = "SELECT COUNT(*) FROM assist_track WHERE exit_at_ms IS NOT NULL"
    params: list[Any] = []
    if cohort is not None:
        sql += " AND cohort=?"
        params.append(cohort)
    if kind is not None:
        sql += " AND kind=?"
        params.append(kind)
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row else 0


def count_signals_since(conn: Any, *, since_ms: int) -> dict[str, int]:
    cur = conn.execute(
        """
        SELECT status, COUNT(*) FROM assist_signal
        WHERE created_at_ms >= ?
          AND status != 'blocked'
        GROUP BY status
        """,
        (int(since_ms),),
    )
    out = {"sent": 0, "skipped": 0, "taken": 0, "held": 0, "expired": 0}
    for status, n in cur.fetchall():
        key = str(status)
        if key in out:
            out[key] = int(n)
    return out


def now_ms() -> int:
    return int(time.time() * 1000)


def list_explore_symbols(conn: Any) -> list[str]:
    rows = conn.execute("SELECT symbol FROM assist_explore_symbols ORDER BY added_at_ms, symbol").fetchall()
    return [str(row[0]) for row in rows]


def insert_explore_symbol(conn: Any, symbol: str, added_at_ms: int) -> None:
    conn.execute(
        "INSERT INTO assist_explore_symbols (symbol, added_at_ms) VALUES (?, ?)",
        (symbol, int(added_at_ms)),
    )
    conn.commit()


def delete_explore_symbol(conn: Any, symbol: str) -> bool:
    cur = conn.execute("DELETE FROM assist_explore_symbols WHERE symbol=?", (symbol,))
    conn.commit()
    return cur.rowcount > 0


def insert_exec_intent(
    conn: Any,
    *,
    kind: str,
    symbol: str,
    side: str,
    preview: dict[str, Any],
    created_at_ms: int,
    expires_at_ms: int,
    telegram_message_id: int | None = None,
) -> str:
    intent_id = f"aei_{uuid.uuid4().hex[:16]}"
    conn.execute(
        """
        INSERT INTO assist_exec_intent (
            id, kind, symbol, side, preview_json, created_at_ms, expires_at_ms,
            status, result_text, telegram_message_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'preview', NULL, ?)
        """,
        (
            intent_id,
            kind,
            symbol.upper(),
            side.lower(),
            json.dumps(preview, separators=(",", ":"), default=str),
            int(created_at_ms),
            int(expires_at_ms),
            telegram_message_id,
        ),
    )
    conn.commit()
    return intent_id


def get_exec_intent(conn: Any, intent_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT id, kind, symbol, side, preview_json, created_at_ms, expires_at_ms,
               status, result_text, telegram_message_id, ref_price, ref_source, ref_at_ms
        FROM assist_exec_intent WHERE id=?
        """,
        (intent_id,),
    ).fetchone()
    if row is None:
        return None
    data = (
        dict(row)
        if hasattr(row, "keys")
        else {
            "id": row[0],
            "kind": row[1],
            "symbol": row[2],
            "side": row[3],
            "preview_json": row[4],
            "created_at_ms": row[5],
            "expires_at_ms": row[6],
            "status": row[7],
            "result_text": row[8],
            "telegram_message_id": row[9],
            "ref_price": row[10],
            "ref_source": row[11],
            "ref_at_ms": row[12],
        }
    )
    raw = data.get("preview_json")
    try:
        data["preview"] = json.loads(str(raw or "{}"))
    except Exception:  # noqa: BLE001
        data["preview"] = {}
    return data


def set_exec_intent_ref(
    conn: Any,
    intent_id: str,
    *,
    ref_price: str,
    ref_source: str,
    ref_at_ms: int,
) -> None:
    """Store the confirm-time mark. Does not change status or preview_json."""
    conn.execute(
        """
        UPDATE assist_exec_intent
        SET ref_price=?, ref_source=?, ref_at_ms=?
        WHERE id=?
        """,
        (str(ref_price), str(ref_source), int(ref_at_ms), intent_id),
    )
    conn.commit()


def claim_exec_intent(conn: Any, intent_id: str) -> bool:
    """Atomic preview → confirmed. True only for the caller that won the race.

    Writes a worker_claim assist_exec_log row in the same transaction so recovery
    can see recent activity and leave a live worker alone.
    """
    cur = conn.execute(
        """
        UPDATE assist_exec_intent SET status='confirmed'
        WHERE id=? AND status='preview'
        """,
        (intent_id,),
    )
    if cur.rowcount == 1:
        conn.execute(
            """
            INSERT INTO assist_exec_log (
                intent_id, step, endpoint, client_order_id, http_status,
                binance_code, order_or_algo_id, created_at_ms
            ) VALUES (?, 'worker_claim', 'local', NULL, NULL, NULL, NULL, ?)
            """,
            (intent_id, int(now_ms())),
        )
    conn.commit()
    return cur.rowcount == 1


def claim_exec_intent_for_recovery(
    conn: Any,
    intent_id: str,
    *,
    now_ms_value: int,
    stale_after_ms: int = 10 * 60_000,
    lease_ms: int = 4 * 60_000,
) -> int | None:
    """Claim unknown, leased recovering, or confirmed with no log row in 10 minutes.

    recovering is claimable only when there is no recover_claim log in the last lease_ms.
    The recover_claim INSERT runs in the same transaction as the UPDATE.
    Returns the recover_claim log row id, or None when this caller lost the claim.
    """
    confirmed_cutoff = int(now_ms_value) - int(stale_after_ms)
    lease_cutoff = int(now_ms_value) - int(lease_ms)
    cur = conn.execute(
        """
        UPDATE assist_exec_intent SET status='recovering'
        WHERE id=? AND (
            status='unknown'
            OR (
                status='recovering'
                AND NOT EXISTS (
                    SELECT 1 FROM assist_exec_log
                    WHERE intent_id=assist_exec_intent.id
                      AND step='recover_claim'
                      AND created_at_ms>?
                )
            )
            OR (
                status='confirmed'
                AND NOT EXISTS (
                    SELECT 1 FROM assist_exec_log
                    WHERE intent_id=assist_exec_intent.id AND created_at_ms>=?
                )
            )
        )
        """,
        (intent_id, lease_cutoff, confirmed_cutoff),
    )
    token: int | None = None
    if cur.rowcount == 1:
        inserted = conn.execute(
            """
            INSERT INTO assist_exec_log (
                intent_id, step, endpoint, client_order_id, http_status,
                binance_code, order_or_algo_id, created_at_ms
            ) VALUES (?, 'recover_claim', 'local', NULL, NULL, NULL, NULL, ?)
            """,
            (intent_id, int(now_ms_value)),
        )
        token = int(inserted.lastrowid)
    conn.commit()
    return token


def renew_exec_recovery_lease(
    conn: Any,
    intent_id: str,
    token: int,
    *,
    now_ms_value: int,
) -> bool:
    """Extend this pass's lease only while its claim row is still the latest one."""
    cur = conn.execute(
        """
        UPDATE assist_exec_log SET created_at_ms=?
        WHERE id=? AND step='recover_claim'
          AND id=(
            SELECT MAX(id) FROM assist_exec_log
            WHERE intent_id=? AND step='recover_claim'
          )
        """,
        (int(now_ms_value), int(token), intent_id),
    )
    conn.commit()
    return cur.rowcount == 1


def finish_exec_intent(
    conn: Any,
    intent_id: str,
    *,
    status: str,
    result_text: str,
    telegram_message_id: int | None = None,
    expect_status: str | None = None,
) -> bool:
    """Write a terminal or intermediate status. When expect_status is set, require it."""
    if telegram_message_id is not None:
        if expect_status is not None:
            cur = conn.execute(
                """
                UPDATE assist_exec_intent
                SET status=?, result_text=?, telegram_message_id=?
                WHERE id=? AND status=?
                """,
                (status, result_text, int(telegram_message_id), intent_id, expect_status),
            )
        else:
            cur = conn.execute(
                """
                UPDATE assist_exec_intent
                SET status=?, result_text=?, telegram_message_id=?
                WHERE id=?
                """,
                (status, result_text, int(telegram_message_id), intent_id),
            )
    elif expect_status is not None:
        cur = conn.execute(
            """
            UPDATE assist_exec_intent SET status=?, result_text=? WHERE id=? AND status=?
            """,
            (status, result_text, intent_id, expect_status),
        )
    else:
        cur = conn.execute(
            """
            UPDATE assist_exec_intent SET status=?, result_text=? WHERE id=?
            """,
            (status, result_text, intent_id),
        )
    conn.commit()
    return cur.rowcount == 1


def symbol_has_in_flight_exec(conn: Any, symbol: str, *, exclude_id: str | None = None) -> bool:
    """True when another intent for the symbol is confirmed/unknown/recovering."""
    if exclude_id is not None:
        row = conn.execute(
            """
            SELECT 1 FROM assist_exec_intent
            WHERE symbol=? AND status IN ('confirmed', 'unknown', 'recovering') AND id!=?
            LIMIT 1
            """,
            (symbol.upper(), exclude_id),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT 1 FROM assist_exec_intent
            WHERE symbol=? AND status IN ('confirmed', 'unknown', 'recovering')
            LIMIT 1
            """,
            (symbol.upper(),),
        ).fetchone()
    return row is not None


def list_recoverable_exec_intents(conn: Any) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, kind, symbol, side, preview_json, created_at_ms, expires_at_ms,
               status, result_text, telegram_message_id, ref_price, ref_source, ref_at_ms
        FROM assist_exec_intent
        WHERE status IN ('unknown', 'recovering', 'confirmed')
        ORDER BY created_at_ms ASC
        """
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        data = (
            dict(row)
            if hasattr(row, "keys")
            else {
                "id": row[0],
                "kind": row[1],
                "symbol": row[2],
                "side": row[3],
                "preview_json": row[4],
                "created_at_ms": row[5],
                "expires_at_ms": row[6],
                "status": row[7],
                "result_text": row[8],
                "telegram_message_id": row[9],
                "ref_price": row[10],
                "ref_source": row[11],
                "ref_at_ms": row[12],
            }
        )
        raw = data.get("preview_json")
        try:
            data["preview"] = json.loads(str(raw or "{}"))
        except Exception:  # noqa: BLE001
            data["preview"] = {}
        out.append(data)
    return out


def set_exec_intent_message_id(conn: Any, intent_id: str, message_id: int) -> None:
    conn.execute(
        "UPDATE assist_exec_intent SET telegram_message_id=? WHERE id=?",
        (int(message_id), intent_id),
    )
    conn.commit()


def cancel_exec_intent(conn: Any, intent_id: str) -> bool:
    cur = conn.execute(
        """
        UPDATE assist_exec_intent SET status='cancelled'
        WHERE id=? AND status='preview'
        """,
        (intent_id,),
    )
    conn.commit()
    return cur.rowcount == 1


def insert_exec_log(
    conn: Any,
    *,
    intent_id: str,
    step: str,
    endpoint: str,
    client_order_id: str | None = None,
    http_status: int | None = None,
    binance_code: int | None = None,
    order_or_algo_id: str | None = None,
    created_at_ms: int | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO assist_exec_log (
            intent_id, step, endpoint, client_order_id, http_status,
            binance_code, order_or_algo_id, created_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            intent_id,
            step,
            endpoint,
            client_order_id,
            http_status,
            binance_code,
            order_or_algo_id,
            int(created_at_ms if created_at_ms is not None else now_ms()),
        ),
    )
    conn.commit()
