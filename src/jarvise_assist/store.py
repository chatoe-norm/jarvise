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


def now_ms() -> int:
    return int(time.time() * 1000)
