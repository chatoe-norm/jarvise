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
    timeframe TEXT NOT NULL DEFAULT '',
    regime_state TEXT NOT NULL,
    confidence_score REAL NOT NULL,
    action TEXT NOT NULL,
    invalidation_price REAL,
    size_pct_equity REAL,
    thesis TEXT
);

-- Point-in-time tradable set. listed_at / delisted_at are event times (ms UTC).
-- A symbol is eligible at as_of when listed_at <= as_of AND
-- (delisted_at IS NULL OR delisted_at > as_of).
CREATE TABLE IF NOT EXISTS universe_membership (
    universe_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    listed_at INTEGER NOT NULL,
    delisted_at INTEGER,
    PRIMARY KEY (universe_id, symbol, listed_at)
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


def _migrate_analysis_timeframe(conn: sqlite3.Connection) -> None:
    """Add timeframe column to legacy analysis_output tables."""
    cols = _table_columns(conn, "analysis_output")
    if not cols or "timeframe" in cols:
        return
    conn.execute(
        "ALTER TABLE analysis_output ADD COLUMN timeframe TEXT NOT NULL DEFAULT ''"
    )


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    _migrate_derivatives_bitemporal(conn)
    _migrate_analysis_timeframe(conn)
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


def load_latest_candle(
    conn: sqlite3.Connection, symbol: str, timeframe: str
) -> dict | None:
    cur = conn.execute(
        """
        SELECT symbol, timestamp, timeframe, open, high, low, close, volume,
               atr_14, rsi_14, ema_20, ema_200
        FROM market_technicals
        WHERE symbol=? AND timeframe=?
        ORDER BY timestamp DESC
        LIMIT 1
        """,
        (symbol.upper(), timeframe),
    )
    row = cur.fetchone()
    return dict(row) if row is not None else None


def upsert_analysis_output(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO analysis_output (
            analysis_id, timestamp, symbol, timeframe, regime_state, confidence_score,
            action, invalidation_price, size_pct_equity, thesis
        ) VALUES (
            :analysis_id, :timestamp, :symbol, :timeframe, :regime_state, :confidence_score,
            :action, :invalidation_price, :size_pct_equity, :thesis
        )
        ON CONFLICT(analysis_id) DO UPDATE SET
            timestamp=excluded.timestamp,
            symbol=excluded.symbol,
            timeframe=excluded.timeframe,
            regime_state=excluded.regime_state,
            confidence_score=excluded.confidence_score,
            action=excluded.action,
            invalidation_price=excluded.invalidation_price,
            size_pct_equity=excluded.size_pct_equity,
            thesis=excluded.thesis
        """,
        {
            "analysis_id": row["analysis_id"],
            "timestamp": row["timestamp"],
            "symbol": row["symbol"],
            "timeframe": row.get("timeframe") or "",
            "regime_state": row["regime_state"],
            "confidence_score": row["confidence_score"],
            "action": row["action"],
            "invalidation_price": row.get("invalidation_price"),
            "size_pct_equity": row["size_pct_equity"],
            "thesis": row.get("thesis"),
        },
    )
    conn.commit()


def list_analysis_output(
    conn: sqlite3.Connection,
    *,
    symbol: str | None = None,
    timeframe: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Return latest analysis_output rows, newest first."""
    clauses: list[str] = []
    params: list[object] = []
    if symbol:
        clauses.append("symbol = ?")
        params.append(symbol.upper())
    if timeframe:
        clauses.append("timeframe = ?")
        params.append(timeframe)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    lim = max(1, min(int(limit), 500))
    params.append(lim)
    cur = conn.execute(
        f"""
        SELECT analysis_id, timestamp, symbol, timeframe, regime_state,
               confidence_score, action, invalidation_price, size_pct_equity, thesis
        FROM analysis_output
        {where}
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        params,
    )
    return [dict(row) for row in cur.fetchall()]


def record_membership(
    conn: sqlite3.Connection,
    *,
    universe_id: str,
    symbol: str,
    asset_class: str,
    listed_at: int,
    delisted_at: int | None = None,
) -> int:
    """Insert one membership interval; return 1 if new, 0 if already present."""
    existing = conn.execute(
        "SELECT 1 FROM universe_membership "
        "WHERE universe_id=? AND symbol=? AND listed_at=?",
        (universe_id, symbol.upper(), listed_at),
    ).fetchone()
    if existing is not None:
        return 0
    conn.execute(
        """
        INSERT INTO universe_membership (
            universe_id, symbol, asset_class, listed_at, delisted_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (universe_id, symbol.upper(), asset_class, listed_at, delisted_at),
    )
    conn.commit()
    return 1


def universe_as_of(
    conn: sqlite3.Connection, universe_id: str, as_of_ms: int
) -> list[str]:
    """Symbols eligible in `universe_id` at knowledge/event cutoff `as_of_ms`."""
    cur = conn.execute(
        """
        SELECT symbol FROM universe_membership
        WHERE universe_id=?
          AND listed_at <= ?
          AND (delisted_at IS NULL OR delisted_at > ?)
        ORDER BY symbol
        """,
        (universe_id, as_of_ms, as_of_ms),
    )
    return [str(row[0]) for row in cur.fetchall()]
