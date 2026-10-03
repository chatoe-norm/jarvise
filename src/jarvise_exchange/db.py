"""SQLite storage for read-only exchange spot balance snapshots.

Schema ownership moved to ``jarvise_ingest.db`` (one ``migrate()`` owns every table);
this module keeps the balance helpers and delegates open/migrate so callers see one DB.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

from jarvise_exchange.models import SpotBalance
from jarvise_ingest.db import migrate as _migrate_all
from jarvise_ingest.db import open_db as _open_all

# Kept for reference / docs; the canonical DDL lives in jarvise_ingest.db.SCHEMA_SQL.
SCHEMA = """
CREATE TABLE IF NOT EXISTS exchange_balances (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  venue TEXT NOT NULL,
  asset TEXT NOT NULL,
  free TEXT NOT NULL,
  locked TEXT NOT NULL,
  total TEXT NOT NULL,
  fetched_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exchange_balances_venue_fetched
  ON exchange_balances (venue, fetched_at_ms DESC);
"""


def open_db(path: Path) -> sqlite3.Connection:
    """Open (and migrate) the shared Jarvise DB with WAL/busy_timeout pragmas."""
    return _open_all(path)


def migrate(conn: sqlite3.Connection) -> None:
    _migrate_all(conn)


def insert_snapshot(
    conn: sqlite3.Connection,
    venue: str,
    balances: list[SpotBalance],
    fetched_at_ms: int,
) -> int:
    rows = [
        (
            venue,
            b.asset,
            format(b.free, "f"),
            format(b.locked, "f"),
            format(b.total, "f"),
            fetched_at_ms,
        )
        for b in balances
    ]
    conn.executemany(
        """
        INSERT INTO exchange_balances (venue, asset, free, locked, total, fetched_at_ms)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    return len(rows)


def latest_snapshot(
    conn: sqlite3.Connection, venue: str
) -> tuple[int, list[SpotBalance]] | None:
    row = conn.execute(
        "SELECT MAX(fetched_at_ms) AS ts FROM exchange_balances WHERE venue=?",
        (venue,),
    ).fetchone()
    if row is None or row["ts"] is None:
        return None
    ts = int(row["ts"])
    cur = conn.execute(
        """
        SELECT asset, free, locked, total
        FROM exchange_balances
        WHERE venue=? AND fetched_at_ms=?
        ORDER BY asset
        """,
        (venue, ts),
    )
    balances = [
        SpotBalance(
            venue=venue,
            asset=r["asset"],
            free=Decimal(r["free"]),
            locked=Decimal(r["locked"]),
            total=Decimal(r["total"]),
        )
        for r in cur.fetchall()
    ]
    return ts, balances
