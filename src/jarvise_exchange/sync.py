"""Shared sync helper for read-only spot balances. No order placement."""

from __future__ import annotations

import time
from pathlib import Path

from jarvise_exchange.db import insert_snapshot, migrate, open_db
from jarvise_exchange.models import SyncResult
from jarvise_exchange.protocol import VenueClient


def sync_spot_balances(
    *,
    client: VenueClient,
    db_path: Path,
    dry_run: bool = False,
    venue: str = "binance",
    fetched_at_ms: int | None = None,
) -> SyncResult:
    raw = client.list_spot_balances()
    balances = [b for b in raw if b.free != 0 or b.locked != 0]
    ts = fetched_at_ms if fetched_at_ms is not None else int(time.time() * 1000)
    if dry_run:
        return SyncResult(True, True, venue, ts, balances, 0, None)
    conn = open_db(db_path)
    try:
        migrate(conn)
        n = insert_snapshot(conn, venue, balances, ts)
        conn.commit()
    finally:
        conn.close()
    return SyncResult(True, False, venue, ts, balances, n, None)
