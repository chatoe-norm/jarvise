"""SQLite helpers for Jarvise MVAS tables.

Timestamps are INTEGER Unix milliseconds (UTC).
GET-only analytics storage — no order placement.

Derivatives rows are bitemporal: `timestamp` is event time (the bar CoinGlass
attributes) and `ingested_at` is when Jarvise learned the values. Revisions
append a new version; they never overwrite the prior one.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

SCHEMA_SQL = """
-- Timestamps: INTEGER Unix milliseconds (UTC)
CREATE TABLE IF NOT EXISTS market_technicals (
    symbol TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    timeframe TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume REAL,
    vwap REAL,
    atr_14 REAL,
    rsi_14 REAL,
    adx_14 REAL,
    ema_20 REAL,
    ema_200 REAL,
    PRIMARY KEY (symbol, timestamp, timeframe)
);

CREATE TABLE IF NOT EXISTS derivatives_analytics (
    symbol TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    ingested_at INTEGER NOT NULL,
    open_interest_usd REAL,
    funding_rate REAL,
    long_short_ratio REAL,
    liquidations_24h_usd REAL,
    PRIMARY KEY (symbol, timestamp, ingested_at)
);

CREATE TABLE IF NOT EXISTS order_book_microstructure (
    symbol TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    bid_ask_spread REAL,
    bid_depth_1pct_usd REAL,
    ask_depth_1pct_usd REAL,
    largest_buy_wall_price REAL,
    largest_sell_wall_price REAL,
    spoof_wall_detected INTEGER,
    PRIMARY KEY (symbol, timestamp)
);

CREATE TABLE IF NOT EXISTS macro_onchain_sentiment (
    timestamp INTEGER NOT NULL PRIMARY KEY,
    fear_greed_index INTEGER,
    altcoin_season_index INTEGER,
    btc_dominance_pct REAL,
    exchange_netflow_btc REAL,
    exchange_reserve_btc REAL,
    etf_net_flow_usd REAL
);

CREATE TABLE IF NOT EXISTS performance_risk_metrics (
    strategy_id TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    expected_value_ev REAL,
    sharpe_ratio REAL,
    sortino_ratio REAL,
    max_drawdown_pct REAL,
    daily_pnl_usd REAL,
    regime_state TEXT,
    confidence_score REAL,
    PRIMARY KEY (strategy_id, timestamp)
);

CREATE TABLE IF NOT EXISTS analysis_output (
    analysis_id TEXT NOT NULL PRIMARY KEY,
    timestamp INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    regime_state TEXT NOT NULL,
    confidence_score REAL NOT NULL,
    action TEXT NOT NULL,
    invalidation_price REAL,
    size_pct_equity REAL,
    thesis TEXT
);
"""

_DERIV_METRIC_KEYS = (
    "open_interest_usd",
    "funding_rate",
    "long_short_ratio",
    "liquidations_24h_usd",
)


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_derivatives_bitemporal(conn: sqlite3.Connection) -> None:
    """Rebuild legacy (symbol, timestamp) PK tables to include ingested_at."""
    cols = _table_columns(conn, "derivatives_analytics")
    if not cols or "ingested_at" in cols:
        return

    conn.executescript(
        """
        ALTER TABLE derivatives_analytics RENAME TO derivatives_analytics_legacy;
        CREATE TABLE derivatives_analytics (
            symbol TEXT NOT NULL,
            timestamp INTEGER NOT NULL,
            ingested_at INTEGER NOT NULL,
            open_interest_usd REAL,
            funding_rate REAL,
            long_short_ratio REAL,
            liquidations_24h_usd REAL,
            PRIMARY KEY (symbol, timestamp, ingested_at)
        );
        INSERT INTO derivatives_analytics (
            symbol, timestamp, ingested_at,
            open_interest_usd, funding_rate, long_short_ratio, liquidations_24h_usd
        )
        SELECT
            symbol, timestamp, timestamp,
            open_interest_usd, funding_rate, long_short_ratio, liquidations_24h_usd
        FROM derivatives_analytics_legacy;
        DROP TABLE derivatives_analytics_legacy;
        """
    )


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    _migrate_derivatives_bitemporal(conn)
    conn.commit()


def open_db(db_path: Path) -> sqlite3.Connection:
    conn = connect(db_path)
    migrate(conn)
    return conn


def upsert_market_technicals(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """Write OHLCV only.

    Indicator columns are derived from the whole stored series by
    `series.recompute_indicators`, never from one ingest window.
    """
    if not rows:
        return 0
    sql = """
    INSERT INTO market_technicals (
        symbol, timestamp, timeframe, open, high, low, close, volume
    ) VALUES (
        :symbol, :timestamp, :timeframe, :open, :high, :low, :close, :volume
    )
    ON CONFLICT(symbol, timestamp, timeframe) DO UPDATE SET
        open=excluded.open,
        high=excluded.high,
        low=excluded.low,
        close=excluded.close,
        volume=excluded.volume
    """
    conn.executemany(sql, rows)
    conn.commit()
    return len(rows)


def load_candle_series(
    conn: sqlite3.Connection, symbol: str, timeframe: str
) -> list[sqlite3.Row]:
    cur = conn.execute(
        "SELECT timestamp, high, low, close FROM market_technicals "
        "WHERE symbol=? AND timeframe=? ORDER BY timestamp",
        (symbol, timeframe),
    )
    return cur.fetchall()


def write_indicators(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    sql = """
    UPDATE market_technicals SET
        atr_14=:atr_14,
        rsi_14=:rsi_14,
        ema_20=:ema_20,
        ema_200=:ema_200
    WHERE symbol=:symbol AND timeframe=:timeframe AND timestamp=:timestamp
    """
    conn.executemany(sql, rows)
    conn.commit()
    return len(rows)


def _metrics_equal(left: dict | sqlite3.Row, right: dict) -> bool:
    for key in _DERIV_METRIC_KEYS:
        lv = left[key] if not isinstance(left, dict) else left.get(key)
        rv = right.get(key)
        if lv is None and rv is None:
            continue
        if lv is None or rv is None:
            return False
        if float(lv) != float(rv):
            return False
    return True


def append_derivatives(
    conn: sqlite3.Connection,
    rows: list[dict],
    *,
    ingested_at: int | None = None,
) -> int:
    """Append a knowledge-time version of each row; skip unchanged payloads.

    `timestamp` is event time. `ingested_at` is when Jarvise learned the values.
    """
    if not rows:
        return 0
    stamp = ingested_at if ingested_at is not None else int(time.time() * 1000)
    insert_sql = """
    INSERT INTO derivatives_analytics (
        symbol, timestamp, ingested_at, open_interest_usd, funding_rate,
        long_short_ratio, liquidations_24h_usd
    ) VALUES (
        :symbol, :timestamp, :ingested_at, :open_interest_usd, :funding_rate,
        :long_short_ratio, :liquidations_24h_usd
    )
    """
    latest_sql = """
    SELECT open_interest_usd, funding_rate, long_short_ratio, liquidations_24h_usd
    FROM derivatives_analytics
    WHERE symbol=? AND timestamp=?
    ORDER BY ingested_at DESC
    LIMIT 1
    """
    written = 0
    for row in rows:
        previous = conn.execute(
            latest_sql, (row["symbol"], row["timestamp"])
        ).fetchone()
        if previous is not None and _metrics_equal(previous, row):
            continue
        payload = {
            "symbol": row["symbol"],
            "timestamp": row["timestamp"],
            "ingested_at": stamp,
            "open_interest_usd": row.get("open_interest_usd"),
            "funding_rate": row.get("funding_rate"),
            "long_short_ratio": row.get("long_short_ratio"),
            "liquidations_24h_usd": row.get("liquidations_24h_usd"),
        }
        conn.execute(insert_sql, payload)
        written += 1
    conn.commit()
    return written


def as_of_derivatives(
    conn: sqlite3.Connection, symbol: str, as_of_ms: int
) -> list[dict]:
    """Latest version of each event-time row that was known by `as_of_ms`."""
    sql = """
    SELECT d.symbol, d.timestamp, d.ingested_at,
           d.open_interest_usd, d.funding_rate,
           d.long_short_ratio, d.liquidations_24h_usd
    FROM derivatives_analytics d
    INNER JOIN (
        SELECT timestamp, MAX(ingested_at) AS ingested_at
        FROM derivatives_analytics
        WHERE symbol=? AND ingested_at <= ?
        GROUP BY timestamp
    ) latest
      ON d.timestamp = latest.timestamp
     AND d.ingested_at = latest.ingested_at
    WHERE d.symbol=?
    ORDER BY d.timestamp
    """
    cur = conn.execute(sql, (symbol, as_of_ms, symbol))
    return [dict(row) for row in cur.fetchall()]


def upsert_derivatives(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """Backward-compatible alias — appends versions; does not overwrite."""
    return append_derivatives(conn, rows)


def count_market(conn: sqlite3.Connection, symbol: str, timeframe: str) -> int:
    cur = conn.execute(
        "SELECT COUNT(*) FROM market_technicals WHERE symbol=? AND timeframe=?",
        (symbol, timeframe),
    )
    return int(cur.fetchone()[0])


def count_derivatives(conn: sqlite3.Connection, symbol: str) -> int:
    cur = conn.execute(
        "SELECT COUNT(*) FROM derivatives_analytics WHERE symbol=?",
        (symbol,),
    )
    return int(cur.fetchone()[0])
