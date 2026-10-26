"""SQLite helpers for trade-assistant tables."""

from __future__ import annotations

import time
import uuid
from typing import Any

from jarvise_assist.sizing import SizedSignal

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
        SELECT futures_down_since_ms, futures_down_alerted, permission_error_reported
        FROM assist_edge WHERE id=1
        """
    ).fetchone()
    if row is None:
        conn.execute(
            """
            INSERT INTO assist_edge (
                id, futures_down_since_ms, futures_down_alerted, permission_error_reported
            ) VALUES (1, NULL, 0, 0)
            """
        )
        conn.commit()
        return {
            "futures_down_since_ms": None,
            "futures_down_alerted": False,
            "permission_error_reported": False,
        }
    return {
        "futures_down_since_ms": row[0],
        "futures_down_alerted": bool(int(row[1])),
        "permission_error_reported": bool(int(row[2])),
    }


def write_edge(
    conn: Any,
    *,
    futures_down_since_ms: Any = _UNSET,
    futures_down_alerted: bool | None = None,
    permission_error_reported: bool | None = None,
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
    conn.execute(
        """
        INSERT INTO assist_edge (
            id, futures_down_since_ms, futures_down_alerted, permission_error_reported
        ) VALUES (1, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            futures_down_since_ms=excluded.futures_down_since_ms,
            futures_down_alerted=excluded.futures_down_alerted,
            permission_error_reported=excluded.permission_error_reported
        """,
        (since, down_alerted, perm),
    )
    conn.commit()


def active_signal_for_symbol(conn: Any, symbol: str, *, now_ms: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT id, status, expires_at_ms, side, entry, stop
        FROM assist_signal
        WHERE symbol=? AND status IN ('sent', 'held') AND expires_at_ms > ?
        ORDER BY created_at_ms DESC
        LIMIT 1
        """,
        (symbol.upper(), int(now_ms)),
    ).fetchone()
    return dict(row) if row is not None else None


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
