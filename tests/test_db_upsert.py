from pathlib import Path

from jarvise_ingest.db import (
    count_market,
    open_db,
    upsert_derivatives,
    upsert_market_technicals,
)


def test_upsert_market_idempotent(tmp_path: Path):
    db = tmp_path / "t.db"
    conn = open_db(db)
    row = {
        "symbol": "BTCUSDT",
        "timestamp": 1_700_000_000_000,
        "timeframe": "1h",
        "open": 1.0,
        "high": 2.0,
        "low": 0.5,
        "close": 1.5,
        "volume": 100.0,
        "vwap": None,
        "atr_14": 0.1,
        "rsi_14": 50.0,
        "adx_14": None,
        "ema_20": 1.4,
        "ema_200": None,
    }
    upsert_market_technicals(conn, [row])
    row2 = dict(row)
    row2["close"] = 1.6
    upsert_market_technicals(conn, [row2])
    assert count_market(conn, "BTCUSDT", "1h") == 1
    cur = conn.execute(
        "SELECT close FROM market_technicals WHERE symbol=? AND timestamp=?",
        ("BTCUSDT", 1_700_000_000_000),
    )
    assert cur.fetchone()[0] == 1.6
    conn.close()


def test_upsert_derivatives_idempotent(tmp_path: Path):
    db = tmp_path / "t.db"
    conn = open_db(db)
    row = {
        "symbol": "BTC",
        "timestamp": 1_700_000_000_000,
        "open_interest_usd": 1e9,
        "funding_rate": 0.0001,
        "long_short_ratio": None,
        "liquidations_24h_usd": 1e6,
    }
    upsert_derivatives(conn, [row])
    upsert_derivatives(conn, [row])
    assert count_derivatives_rows(conn) == 1
    conn.close()


def count_derivatives_rows(conn) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM derivatives_analytics").fetchone()[0])
