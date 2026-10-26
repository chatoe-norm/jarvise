"""SQLite helpers for trade-assistant tables."""

from __future__ import annotations

import time
import uuid
from typing import Any

from jarvise_assist.sizing import SAME_IDEA_MS, SizedSignal

_UNSET: object = object()


def ensure_settings(conn: Any) -> dict[str, Any]:
    row = conn.execute("SELECT risk_pct, enabled FROM assist_settings WHERE id=1").fetchone()
    if row is None:
        conn.execute("INSERT INTO assist_settings (id, risk_pct, enabled) VALUES (1, 2.0, 1)")
        conn.commit()
        return {"risk_pct": 2.0, "enabled": True}
    return {"risk_pct": float(row[0]), "enabled": bool(int(row[1]))}


def get_edge(conn: Any) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT futures_down_since_ms, futures_down_alerted,
               permission_error_reported, key_unsafe_reported
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
        }
    key_unsafe = bool(int(row[3])) if len(row) > 3 and row[3] is not None else False
    return {
        "futures_down_since_ms": row[0],
        "futures_down_alerted": bool(int(row[1])),
        "permission_error_reported": bool(int(row[2])),
        "key_unsafe_reported": key_unsafe,
    }


def write_edge(
    conn: Any,
    *,
    futures_down_since_ms: Any = _UNSET,
    futures_down_alerted: bool | None = None,
    permission_error_reported: bool | None = None,
    key_unsafe_reported: bool | None = None,
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
    conn.execute(
        """
        INSERT INTO assist_edge (
            id, futures_down_since_ms, futures_down_alerted,
            permission_error_reported, key_unsafe_reported
        ) VALUES (1, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            futures_down_since_ms=excluded.futures_down_since_ms,
            futures_down_alerted=excluded.futures_down_alerted,
            permission_error_reported=excluded.permission_error_reported,
            key_unsafe_reported=excluded.key_unsafe_reported
        """,
        (since, down_alerted, perm, unsafe),
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
    """True when a sent/held/expired row for symbol+side exists within within_ms."""
    row = conn.execute(
        """
        SELECT 1 FROM assist_signal
        WHERE symbol=? AND side=? AND status IN ('sent', 'held', 'expired')
          AND created_at_ms >= ?
        LIMIT 1
        """,
        (symbol.upper(), side.lower(), int(now_ms) - int(within_ms)),
    ).fetchone()
    return row is not None


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


def now_ms() -> int:
    return int(time.time() * 1000)
