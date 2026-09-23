from pathlib import Path

from jarvise_ingest.db import (
    append_derivatives,
    count_market,
    list_analysis_output,
    open_db,
    upsert_analysis_output,
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


def test_append_derivatives_skips_identical_payload(tmp_path: Path):
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
    assert append_derivatives(conn, [row], ingested_at=1_800_000_000_000) == 1
    assert append_derivatives(conn, [row], ingested_at=1_800_000_100_000) == 0
    assert count_derivatives_rows(conn) == 1
    conn.close()


def count_derivatives_rows(conn) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM derivatives_analytics").fetchone()[0])


def test_list_analysis_output_filters(tmp_path: Path):
    db = tmp_path / "a.db"
    conn = open_db(db)
    upsert_analysis_output(
        conn,
        {
            "analysis_id": "a1",
            "timestamp": 100,
            "symbol": "BTCUSDT",
            "timeframe": "4h",
            "regime_state": "trend_up",
            "confidence_score": 0.7,
            "action": "long",
            "invalidation_price": 90.0,
            "size_pct_equity": 1.0,
            "thesis": "btc 4h",
        },
    )
    upsert_analysis_output(
        conn,
        {
            "analysis_id": "a2",
            "timestamp": 200,
            "symbol": "ETHUSDT",
            "timeframe": "1h",
            "regime_state": "range",
            "confidence_score": 0.4,
            "action": "flat",
            "invalidation_price": None,
            "size_pct_equity": 0.0,
            "thesis": "eth 1h",
        },
    )
    upsert_analysis_output(
        conn,
        {
            "analysis_id": "a3",
            "timestamp": 150,
            "symbol": "BTCUSDT",
            "timeframe": "1h",
            "regime_state": "trend_down",
            "confidence_score": 0.6,
            "action": "short",
            "invalidation_price": 110.0,
            "size_pct_equity": 1.2,
            "thesis": "btc 1h",
        },
    )

    all_rows = list_analysis_output(conn, limit=10)
    assert [r["analysis_id"] for r in all_rows] == ["a2", "a3", "a1"]

    btc = list_analysis_output(conn, symbol="btcusdt")
    assert [r["analysis_id"] for r in btc] == ["a3", "a1"]

    btc_4h = list_analysis_output(conn, symbol="BTCUSDT", timeframe="4h")
    assert len(btc_4h) == 1
    assert btc_4h[0]["analysis_id"] == "a1"
    assert btc_4h[0]["timeframe"] == "4h"
    conn.close()


def test_migrate_adds_timeframe_to_legacy_analysis(tmp_path: Path):
    db = tmp_path / "legacy.db"
    conn = open_db(db)
    conn.execute("DROP TABLE analysis_output")
    conn.execute(
        """
        CREATE TABLE analysis_output (
            analysis_id TEXT NOT NULL PRIMARY KEY,
            timestamp INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            regime_state TEXT NOT NULL,
            confidence_score REAL NOT NULL,
            action TEXT NOT NULL,
            invalidation_price REAL,
            size_pct_equity REAL,
            thesis TEXT
        )
        """
    )
    conn.commit()
    conn.close()

    conn = open_db(db)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(analysis_output)")}
    assert "timeframe" in cols
    conn.close()
