"""SQLite helpers for trade-assistant tables."""

from __future__ import annotations

import time
import uuid
from typing import Any

from jarvise_assist.sizing import SAME_IDEA_MS, SizedSignal

_UNSET: object = object()


def ensure_settings(conn: Any) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT risk_pct, enabled, paused_until_ms, awaiting_risk, pending_risk_pct
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
        }
    # Older DBs before migration 16 may lack the trailing columns.
    paused = row[2] if len(row) > 2 else None
    awaiting = row[3] if len(row) > 3 else None
    pending = row[4] if len(row) > 4 else None
    return {
        "risk_pct": float(row[0]),
        "enabled": bool(int(row[1])),
        "paused_until_ms": int(paused) if paused is not None else None,
        "awaiting_risk": str(awaiting) if awaiting else None,
        "pending_risk_pct": float(pending) if pending is not None else None,
    }


def write_settings(
    conn: Any,
    *,
    risk_pct: float | None = None,
    enabled: bool | None = None,
    paused_until_ms: Any = _UNSET,
    awaiting_risk: Any = _UNSET,
    pending_risk_pct: Any = _UNSET,
) -> None:
    cur = ensure_settings(conn)
    rp = float(cur["risk_pct"] if risk_pct is None else risk_pct)
    en = int(cur["enabled"] if enabled is None else enabled)
    paused = cur["paused_until_ms"] if paused_until_ms is _UNSET else paused_until_ms
    awaiting = cur["awaiting_risk"] if awaiting_risk is _UNSET else awaiting_risk
    pending = cur["pending_risk_pct"] if pending_risk_pct is _UNSET else pending_risk_pct
    conn.execute(
        """
        INSERT INTO assist_settings (
            id, risk_pct, enabled, paused_until_ms, awaiting_risk, pending_risk_pct
        ) VALUES (1, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            risk_pct=excluded.risk_pct,
            enabled=excluded.enabled,
            paused_until_ms=excluded.paused_until_ms,
            awaiting_risk=excluded.awaiting_risk,
            pending_risk_pct=excluded.pending_risk_pct
        """,
        (rp, en, paused, awaiting, pending),
    )
    conn.commit()


def get_edge(conn: Any) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT futures_down_since_ms, futures_down_alerted,
               permission_error_reported, key_unsafe_reported,
               streak_anchor_ms, week_pause_id,
               listener_down_since_ms, listener_down_alerted, resume_alerted
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
    conn.execute(
        """
        INSERT INTO assist_edge (
            id, futures_down_since_ms, futures_down_alerted,
            permission_error_reported, key_unsafe_reported,
            streak_anchor_ms, week_pause_id,
            listener_down_since_ms, listener_down_alerted, resume_alerted
        ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            futures_down_since_ms=excluded.futures_down_since_ms,
            futures_down_alerted=excluded.futures_down_alerted,
            permission_error_reported=excluded.permission_error_reported,
            key_unsafe_reported=excluded.key_unsafe_reported,
            streak_anchor_ms=excluded.streak_anchor_ms,
            week_pause_id=excluded.week_pause_id,
            listener_down_since_ms=excluded.listener_down_since_ms,
            listener_down_alerted=excluded.listener_down_alerted,
            resume_alerted=excluded.resume_alerted
        """,
        (since, down_alerted, perm, unsafe, streak, week, listen_since, listen_alerted, resume),
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
        SELECT id, symbol, side, entry, stop, target, created_at_ms, sent_at_ms, status
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
    if status == "sent" and sent_at_ms is not None:
        conn.execute(
            """
            INSERT OR IGNORE INTO assist_track (
                signal_id, symbol, side, entry, stop, target, qty, sent_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
    conn.execute(
        """
        INSERT OR IGNORE INTO assist_track (
            signal_id, symbol, side, entry, stop, target, qty, sent_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
) -> bool:
    sql = """
        SELECT 1 FROM assist_watch_advice
        WHERE symbol=? AND position_side=? AND kind=? AND status IN ('sent', 'held')
    """
    params: list[Any] = [symbol.upper(), position_side.upper(), kind]
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
) -> None:
    conn.execute(
        """
        UPDATE assist_track SET
            exit_at_ms=?, exit_px=?, exit_reason=?,
            gross_r=?, net_r=?, fee_usdt=?, pnl_usdt=?
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
            signal_id,
        ),
    )
    conn.commit()


def list_closed_tracks_since(conn: Any, *, since_ms: int) -> list[dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT signal_id, symbol, side, entry, stop, target, qty, sent_at_ms,
               exit_at_ms, exit_px, exit_reason, gross_r, net_r, fee_usdt, pnl_usdt
        FROM assist_track
        WHERE exit_at_ms IS NOT NULL AND exit_at_ms >= ?
        ORDER BY exit_at_ms ASC
        """,
        (int(since_ms),),
    )
    return [dict(r) for r in cur.fetchall()]


def count_closed_tracks_all(conn: Any) -> int:
    row = conn.execute("SELECT COUNT(*) FROM assist_track WHERE exit_at_ms IS NOT NULL").fetchone()
    return int(row[0]) if row else 0


def count_signals_since(conn: Any, *, since_ms: int) -> dict[str, int]:
    cur = conn.execute(
        """
        SELECT status, COUNT(*) FROM assist_signal
        WHERE created_at_ms >= ?
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
