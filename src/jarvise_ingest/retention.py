"""Retention for unbounded market tables. Never touches paper, approval, or live tables."""

from __future__ import annotations

import sqlite3
import time
from typing import Any

PRUNABLE_TABLES = ("order_book_microstructure", "exchange_balances", "derivatives_analytics")
PROTECTED_TABLES = (
    "paper_account",
    "paper_orders",
    "paper_positions",
    "approval_queue",
    "approval_llm_reviews",
    "live_orders",
    "market_technicals",
    "analysis_output",
    "universe_membership",
    "performance_risk_metrics",
    "macro_onchain_sentiment",
    "paper_equity_snapshots",
)


def parse_age(spec: str) -> int:
    """'180d' / '12h' / '90m' → milliseconds."""
    raw = str(spec).strip().lower()
    if not raw:
        raise ValueError("empty age")
    unit = raw[-1]
    mult = {"d": 86_400_000, "h": 3_600_000, "m": 60_000}.get(unit)
    if mult is None:
        raise ValueError(f"age must end in d/h/m: {spec}")
    try:
        value = float(raw[:-1])
    except ValueError as exc:
        raise ValueError(f"invalid age: {spec}") from exc
    if value <= 0:
        raise ValueError("age must be positive")
    return int(value * mult)


def _count(conn: sqlite3.Connection, sql: str, params: tuple) -> int:
    return int(conn.execute(sql, params).fetchone()[0] or 0)


def prune(
    conn: sqlite3.Connection,
    *,
    older_than_ms: int,
    now_ms: int | None = None,
    dry_run: bool = False,
    vacuum: bool = False,
) -> dict[str, Any]:
    """Delete stale rows from PRUNABLE_TABLES.

    - order_book_microstructure: rows older than cutoff.
    - exchange_balances: rows older than cutoff, except the latest snapshot per venue.
    - derivatives_analytics: for bars older than cutoff keep only the newest
      ``ingested_at`` version per (symbol, timestamp); the latest view stays intact.
    """
    ts = int(now_ms if now_ms is not None else time.time() * 1000)
    cutoff = ts - int(older_than_ms)
    deleted: dict[str, int] = {}

    deleted["order_book_microstructure"] = _count(
        conn, "SELECT COUNT(*) FROM order_book_microstructure WHERE timestamp < ?", (cutoff,)
    )
    deleted["exchange_balances"] = _count(
        conn,
        """
        SELECT COUNT(*) FROM exchange_balances e
        WHERE e.fetched_at_ms < ?
          AND e.fetched_at_ms < (SELECT MAX(fetched_at_ms) FROM exchange_balances x WHERE x.venue = e.venue)
        """,
        (cutoff,),
    )
    deleted["derivatives_analytics"] = _count(
        conn,
        """
        SELECT COUNT(*) FROM derivatives_analytics d
        WHERE d.timestamp < ?
          AND d.ingested_at < (
            SELECT MAX(ingested_at) FROM derivatives_analytics x
            WHERE x.symbol = d.symbol AND x.timestamp = d.timestamp
          )
        """,
        (cutoff,),
    )

    if not dry_run:
        conn.execute("DELETE FROM order_book_microstructure WHERE timestamp < ?", (cutoff,))
        conn.execute(
            """
            DELETE FROM exchange_balances
            WHERE fetched_at_ms < ?
              AND fetched_at_ms < (
                SELECT MAX(fetched_at_ms) FROM exchange_balances x WHERE x.venue = exchange_balances.venue
              )
            """,
            (cutoff,),
        )
        conn.execute(
            """
            DELETE FROM derivatives_analytics
            WHERE timestamp < ?
              AND ingested_at < (
                SELECT MAX(ingested_at) FROM derivatives_analytics x
                WHERE x.symbol = derivatives_analytics.symbol
                  AND x.timestamp = derivatives_analytics.timestamp
              )
            """,
            (cutoff,),
        )
        conn.commit()
        if vacuum:
            conn.execute("VACUUM")
    return {
        "ok": True,
        "dry_run": dry_run,
        "now_ms": ts,
        "cutoff_ms": cutoff,
        "deleted": deleted,
        "protected": list(PROTECTED_TABLES),
        "vacuum": bool(vacuum and not dry_run),
    }


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    names = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY 1"
        ).fetchall()
    ]
    return {name: _count(conn, f"SELECT COUNT(*) FROM {name}", ()) for name in names}
