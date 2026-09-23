from decimal import Decimal
from pathlib import Path

from jarvise_exchange.db import insert_snapshot, latest_snapshot, migrate, open_db
from jarvise_exchange.models import SpotBalance


def test_insert_two_batches_latest_is_newest(tmp_path: Path):
    db = tmp_path / "t.db"
    conn = open_db(db)
    migrate(conn)
    b1 = [SpotBalance("binance", "BTC", Decimal("1"), Decimal("0"), Decimal("1"))]
    b2 = [SpotBalance("binance", "ETH", Decimal("2"), Decimal("0"), Decimal("2"))]
    insert_snapshot(conn, "binance", b1, fetched_at_ms=1000)
    insert_snapshot(conn, "binance", b2, fetched_at_ms=2000)
    latest = latest_snapshot(conn, "binance")
    assert latest is not None
    ts, rows = latest
    assert ts == 2000
    assert rows[0].asset == "ETH"
    conn.close()
