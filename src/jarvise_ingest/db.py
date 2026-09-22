"""SQLite helpers for Jarvise MVAS tables.

Timestamps are INTEGER Unix milliseconds (UTC).
GET-only analytics storage — no order placement.
"""

from __future__ import annotations

import sqlite3
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
    open_interest_usd REAL,
    funding_rate REAL,
    long_short_ratio REAL,
    liquidations_24h_usd REAL,
    PRIMARY KEY (symbol, timestamp)
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


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
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


def upsert_derivatives(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    sql = """
    INSERT INTO derivatives_analytics (
        symbol, timestamp, open_interest_usd, funding_rate,
        long_short_ratio, liquidations_24h_usd
    ) VALUES (
        :symbol, :timestamp, :open_interest_usd, :funding_rate,
        :long_short_ratio, :liquidations_24h_usd
    )
    ON CONFLICT(symbol, timestamp) DO UPDATE SET
        open_interest_usd=excluded.open_interest_usd,
        funding_rate=excluded.funding_rate,
        long_short_ratio=excluded.long_short_ratio,
        liquidations_24h_usd=excluded.liquidations_24h_usd
    """
    conn.executemany(sql, rows)
    conn.commit()
    return len(rows)


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
