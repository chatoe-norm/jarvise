"""SQLite helpers for Jarvise MVAS tables.

Timestamps are INTEGER Unix milliseconds (UTC).
GET-only analytics storage — no order placement.

Derivatives rows are bitemporal: `timestamp` is event time (the bar CoinGlass
attributes) and `ingested_at` is when Jarvise learned the values. Revisions
append a new version; they never overwrite the prior one.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

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
    sma_20 REAL,
    macd_line REAL,
    macd_signal REAL,
    macd_hist REAL,
    bb_mid REAL,
    bb_upper REAL,
    bb_lower REAL,
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
    etf_net_flow_usd REAL,
    global_market_cap_usd REAL
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

-- Simulated paper ledger (P2). No exchange order placement.
CREATE TABLE IF NOT EXISTS paper_account (
    key TEXT NOT NULL PRIMARY KEY,
    value REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_orders (
    order_id TEXT NOT NULL PRIMARY KEY,
    ts INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    price REAL NOT NULL,
    fee_usd REAL NOT NULL,
    fee_bps REAL,
    slip_bps REAL NOT NULL,
    analysis_id TEXT,
    reason TEXT,
    approval_id TEXT,
    decision_source TEXT
);

CREATE TABLE IF NOT EXISTS paper_positions (
    symbol TEXT NOT NULL PRIMARY KEY,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    entry_price REAL NOT NULL,
    entry_ts INTEGER NOT NULL,
    unrealized_pnl REAL NOT NULL DEFAULT 0,
    realized_pnl REAL NOT NULL DEFAULT 0,
    stop_price REAL,
    stop_source TEXT,
    approval_id TEXT
);

CREATE TABLE IF NOT EXISTS approval_queue (
    id TEXT NOT NULL PRIMARY KEY,
    created_at_ms INTEGER NOT NULL,
    expires_at_ms INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    analysis_id TEXT,
    action TEXT NOT NULL,
    regime_state TEXT,
    confidence_score REAL,
    size_pct_equity REAL,
    invalidation_price REAL,
    status TEXT NOT NULL,
    resolved_at_ms INTEGER,
    resolve_reason TEXT,
    paper_order_ids_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_approval_queue_status_expires
    ON approval_queue (status, expires_at_ms);
CREATE INDEX IF NOT EXISTS idx_approval_queue_symbol_tf_status
    ON approval_queue (symbol, timeframe, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_approval_queue_pending_symbol_tf
    ON approval_queue (symbol, timeframe) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS paper_equity_snapshots (
    ts INTEGER PRIMARY KEY,
    equity REAL NOT NULL,
    cash REAL NOT NULL,
    source TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS live_orders (
    id TEXT NOT NULL PRIMARY KEY,
    created_at_ms INTEGER NOT NULL,
    approval_id TEXT,
    venue TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    order_type TEXT NOT NULL,
    requested_qty REAL,
    requested_notional_usd REAL,
    status TEXT NOT NULL,
    venue_order_id TEXT,
    venue_response_json TEXT,
    error TEXT,
    kill_switch_clear INTEGER NOT NULL,
    caps_ok INTEGER NOT NULL,
    realized_pnl_usd REAL,
    client_order_id TEXT,
    venue_status TEXT,
    executed_qty REAL,
    cummulative_quote_qty REAL,
    fills_count INTEGER,
    reconciled_at_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_live_orders_created
    ON live_orders (created_at_ms);
-- idx_live_orders_client is created by migration 5 (after the column exists on legacy DBs).

-- Read-only exchange spot wallet snapshots (P3). Amounts as TEXT decimal strings.
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

-- Second-layer LLM reviews for paper approvals (auto-decide audit). Paper only.
CREATE TABLE IF NOT EXISTS approval_llm_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    approval_id TEXT NOT NULL,
    model TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT,
    brief_hash TEXT,
    created_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_approval_llm_reviews_approval
    ON approval_llm_reviews (approval_id, created_at_ms);

-- Closed-trade outcomes attributed to the approving decision (T2.1 feedback).
CREATE TABLE IF NOT EXISTS paper_decision_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    approval_id TEXT,
    open_order_id TEXT NOT NULL,
    close_order_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    pnl_usd REAL NOT NULL,
    r_multiple REAL,
    hold_ms INTEGER,
    decision_source TEXT NOT NULL,
    prompt_version TEXT,
    closed_at_ms INTEGER NOT NULL,
    UNIQUE(open_order_id, close_order_id)
);
CREATE INDEX IF NOT EXISTS idx_paper_decision_outcomes_source_closed
    ON paper_decision_outcomes (decision_source, closed_at_ms);

-- Durable auto-decide run summaries (Redis last-run alone is not enough).
CREATE TABLE IF NOT EXISTS paper_auto_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at_ms INTEGER NOT NULL,
    model TEXT,
    prompt_version TEXT,
    ok INTEGER NOT NULL,
    skipped INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    processed INTEGER,
    approved_n INTEGER,
    rejected_n INTEGER,
    deferred_n INTEGER,
    apply_failed_n INTEGER,
    duration_s REAL,
    payload_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_paper_auto_runs_at
    ON paper_auto_runs (at_ms DESC);
"""

_DERIV_METRIC_KEYS = (
    "open_interest_usd",
    "funding_rate",
    "long_short_ratio",
    "liquidations_24h_usd",
)


BUSY_TIMEOUT_MS = 5000


def connect(db_path: Path) -> sqlite3.Connection:
    """Open SQLite with durability + concurrency pragmas.

    WAL lets jobs, web, and the CLI read while one writer commits; busy_timeout turns
    "database is locked" into a short wait; synchronous=NORMAL is safe under WAL.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=BUSY_TIMEOUT_MS / 1000.0)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        # Read-only filesystems or exotic mounts may refuse WAL; keep working with the default.
        pass
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)


def db_pragmas(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        "user_version": schema_version(conn),
        "journal_mode": str(conn.execute("PRAGMA journal_mode").fetchone()[0]),
        "synchronous": int(conn.execute("PRAGMA synchronous").fetchone()[0]),
        "busy_timeout_ms": int(conn.execute("PRAGMA busy_timeout").fetchone()[0]),
        "foreign_keys": bool(conn.execute("PRAGMA foreign_keys").fetchone()[0]),
    }


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
    conn.execute("ALTER TABLE analysis_output ADD COLUMN timeframe TEXT NOT NULL DEFAULT ''")


def _migrate_macro_global_mcap(conn: sqlite3.Connection) -> None:
    """Add global_market_cap_usd to legacy macro_onchain_sentiment tables."""
    cols = _table_columns(conn, "macro_onchain_sentiment")
    if not cols or "global_market_cap_usd" in cols:
        return
    conn.execute("ALTER TABLE macro_onchain_sentiment ADD COLUMN global_market_cap_usd REAL")


def _migrate_paper_orders_fee_bps(conn: sqlite3.Connection) -> None:
    """Add fee_bps to legacy paper_orders (bps used on that fill)."""
    cols = _table_columns(conn, "paper_orders")
    if not cols or "fee_bps" in cols:
        return
    conn.execute("ALTER TABLE paper_orders ADD COLUMN fee_bps REAL")


_LIVE_ORDER_RECONCILE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("client_order_id", "TEXT"),
    ("venue_status", "TEXT"),
    ("executed_qty", "REAL"),
    ("cummulative_quote_qty", "REAL"),
    ("fills_count", "INTEGER"),
    ("reconciled_at_ms", "INTEGER"),
)


def _migrate_live_orders_reconcile(conn: sqlite3.Connection) -> None:
    """Add idempotency + fill reconciliation columns to legacy live_orders."""
    cols = _table_columns(conn, "live_orders")
    if not cols:
        return
    for name, typ in _LIVE_ORDER_RECONCILE_COLUMNS:
        if name not in cols:
            conn.execute(f"ALTER TABLE live_orders ADD COLUMN {name} {typ}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_live_orders_client ON live_orders (client_order_id)")


def _migrate_exchange_balances(conn: sqlite3.Connection) -> None:
    """exchange_balances used to live in jarvise_exchange.db; SCHEMA_SQL now owns it."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS exchange_balances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            venue TEXT NOT NULL,
            asset TEXT NOT NULL,
            free TEXT NOT NULL,
            locked TEXT NOT NULL,
            total TEXT NOT NULL,
            fetched_at_ms INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_exchange_balances_venue_fetched "
        "ON exchange_balances (venue, fetched_at_ms DESC)"
    )


def _migrate_decision_feedback(conn: sqlite3.Connection) -> None:
    """Stamp paper fills with approval attribution + durable outcome/auto-run tables."""
    cols = _table_columns(conn, "paper_orders")
    if cols:
        if "approval_id" not in cols:
            conn.execute("ALTER TABLE paper_orders ADD COLUMN approval_id TEXT")
        if "decision_source" not in cols:
            conn.execute("ALTER TABLE paper_orders ADD COLUMN decision_source TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_orders_approval ON paper_orders (approval_id)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_decision_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            approval_id TEXT,
            open_order_id TEXT NOT NULL,
            close_order_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            pnl_usd REAL NOT NULL,
            r_multiple REAL,
            hold_ms INTEGER,
            decision_source TEXT NOT NULL,
            prompt_version TEXT,
            closed_at_ms INTEGER NOT NULL,
            UNIQUE(open_order_id, close_order_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_paper_decision_outcomes_source_closed "
        "ON paper_decision_outcomes (decision_source, closed_at_ms)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_auto_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at_ms INTEGER NOT NULL,
            model TEXT,
            prompt_version TEXT,
            ok INTEGER NOT NULL,
            skipped INTEGER NOT NULL DEFAULT 0,
            reason TEXT,
            processed INTEGER,
            approved_n INTEGER,
            rejected_n INTEGER,
            deferred_n INTEGER,
            apply_failed_n INTEGER,
            duration_s REAL,
            payload_json TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_auto_runs_at ON paper_auto_runs (at_ms DESC)")


def _add_column_if_missing(conn: sqlite3.Connection, table: str, name: str, typ: str) -> None:
    cols = _table_columns(conn, table)
    if not cols or name in cols:
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")


def _migrate_stop_and_marks(conn: sqlite3.Connection) -> None:
    """Stops on approvals/positions + paper equity snapshots (additive)."""
    _add_column_if_missing(conn, "approval_queue", "invalidation_price", "REAL")
    _add_column_if_missing(conn, "paper_positions", "stop_price", "REAL")
    _add_column_if_missing(conn, "paper_positions", "stop_source", "TEXT")
    _add_column_if_missing(conn, "paper_positions", "approval_id", "TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_equity_snapshots (
            ts INTEGER PRIMARY KEY,
            equity REAL NOT NULL,
            cash REAL NOT NULL,
            source TEXT NOT NULL
        )
        """
    )


def _migrate_indicator_context(conn: sqlite3.Connection) -> None:
    """MACD / Bollinger / SMA context columns (analyzer does not consume them)."""
    for name in (
        "sma_20",
        "macd_line",
        "macd_signal",
        "macd_hist",
        "bb_mid",
        "bb_upper",
        "bb_lower",
    ):
        _add_column_if_missing(conn, "market_technicals", name, "REAL")


# Ordered, numbered migrations. PRAGMA user_version records the last applied step so a
# multi-step evolution runs exactly once per database. ``repair=True`` marks cheap,
# idempotent additive steps (column sniff + ALTER ADD) that also re-run on every open so
# schema drift self-heals; the destructive bitemporal rebuild is version-gated only.
MIGRATIONS: tuple[tuple[int, str, Any, bool], ...] = (
    (1, "derivatives_bitemporal", _migrate_derivatives_bitemporal, False),
    (2, "analysis_timeframe", _migrate_analysis_timeframe, True),
    (3, "macro_global_mcap", _migrate_macro_global_mcap, True),
    (4, "paper_orders_fee_bps", _migrate_paper_orders_fee_bps, True),
    (5, "live_orders_reconcile", _migrate_live_orders_reconcile, True),
    (6, "exchange_balances", _migrate_exchange_balances, True),
    (7, "decision_feedback", _migrate_decision_feedback, True),
    (8, "stop_and_marks", _migrate_stop_and_marks, True),
    (9, "indicator_context", _migrate_indicator_context, True),
)
SCHEMA_VERSION = MIGRATIONS[-1][0]


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Create missing tables, apply numbered migrations above user_version, re-run repairs.

    Returns the list of ``version:name`` steps that advanced user_version.
    """
    conn.executescript(SCHEMA_SQL)
    current = schema_version(conn)
    applied: list[str] = []
    for version, name, fn, repair in MIGRATIONS:
        if version <= current:
            if repair:
                fn(conn)
            continue
        fn(conn)
        conn.execute(f"PRAGMA user_version = {int(version)}")
        applied.append(f"{version}:{name}")
    conn.commit()
    return applied


def open_db(db_path: Path) -> sqlite3.Connection:
    conn = connect(db_path)
    migrate(conn)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE → commit, or rollback on any error."""
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def _commit(conn: sqlite3.Connection, *, commit: bool) -> None:
    if commit:
        conn.commit()


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


def load_candle_series(conn: sqlite3.Connection, symbol: str, timeframe: str) -> list[sqlite3.Row]:
    cur = conn.execute(
        "SELECT timestamp, high, low, close FROM market_technicals "
        "WHERE symbol=? AND timeframe=? ORDER BY timestamp",
        (symbol, timeframe),
    )
    return cur.fetchall()


def write_indicators(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    keys = (
        "symbol",
        "timeframe",
        "timestamp",
        "atr_14",
        "rsi_14",
        "ema_20",
        "ema_200",
        "sma_20",
        "macd_line",
        "macd_signal",
        "macd_hist",
        "bb_mid",
        "bb_upper",
        "bb_lower",
    )
    filled = [{key: row.get(key) for key in keys} for row in rows]
    sql = """
    UPDATE market_technicals SET
        atr_14=:atr_14,
        rsi_14=:rsi_14,
        ema_20=:ema_20,
        ema_200=:ema_200,
        sma_20=:sma_20,
        macd_line=:macd_line,
        macd_signal=:macd_signal,
        macd_hist=:macd_hist,
        bb_mid=:bb_mid,
        bb_upper=:bb_upper,
        bb_lower=:bb_lower
    WHERE symbol=:symbol AND timeframe=:timeframe AND timestamp=:timestamp
    """
    conn.executemany(sql, filled)
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
        previous = conn.execute(latest_sql, (row["symbol"], row["timestamp"])).fetchone()
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


def as_of_derivatives(conn: sqlite3.Connection, symbol: str, as_of_ms: int) -> list[dict]:
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


_INDICATOR_COLUMNS = frozenset({"atr_14", "rsi_14", "ema_20", "ema_200"})


def count_indicator_ready(
    conn: sqlite3.Connection,
    symbol: str,
    timeframe: str,
    *,
    column: str = "ema_200",
) -> int:
    """Rows where an indicator column is populated (warm-up complete)."""
    if column not in _INDICATOR_COLUMNS:
        raise ValueError(f"unknown indicator column: {column}")
    cur = conn.execute(
        f"SELECT COUNT(*) FROM market_technicals WHERE symbol=? AND timeframe=? AND {column} IS NOT NULL",
        (symbol.upper(), timeframe),
    )
    return int(cur.fetchone()[0])


def upsert_order_book(conn: sqlite3.Connection, row: dict) -> int:
    """Insert or replace one order_book_microstructure snapshot."""
    sql = """
    INSERT INTO order_book_microstructure (
        symbol, timestamp, bid_ask_spread, bid_depth_1pct_usd, ask_depth_1pct_usd,
        largest_buy_wall_price, largest_sell_wall_price, spoof_wall_detected
    ) VALUES (
        :symbol, :timestamp, :bid_ask_spread, :bid_depth_1pct_usd, :ask_depth_1pct_usd,
        :largest_buy_wall_price, :largest_sell_wall_price, :spoof_wall_detected
    )
    ON CONFLICT(symbol, timestamp) DO UPDATE SET
        bid_ask_spread=excluded.bid_ask_spread,
        bid_depth_1pct_usd=excluded.bid_depth_1pct_usd,
        ask_depth_1pct_usd=excluded.ask_depth_1pct_usd,
        largest_buy_wall_price=excluded.largest_buy_wall_price,
        largest_sell_wall_price=excluded.largest_sell_wall_price,
        spoof_wall_detected=excluded.spoof_wall_detected
    """
    conn.execute(
        sql,
        {
            "symbol": str(row["symbol"]).upper(),
            "timestamp": int(row["timestamp"]),
            "bid_ask_spread": row.get("bid_ask_spread"),
            "bid_depth_1pct_usd": row.get("bid_depth_1pct_usd"),
            "ask_depth_1pct_usd": row.get("ask_depth_1pct_usd"),
            "largest_buy_wall_price": row.get("largest_buy_wall_price"),
            "largest_sell_wall_price": row.get("largest_sell_wall_price"),
            "spoof_wall_detected": int(row.get("spoof_wall_detected") or 0),
        },
    )
    conn.commit()
    return 1


def latest_order_book(conn: sqlite3.Connection, symbol: str, *, as_of_ms: int | None = None) -> dict | None:
    if as_of_ms is None:
        cur = conn.execute(
            """
            SELECT symbol, timestamp, bid_ask_spread, bid_depth_1pct_usd, ask_depth_1pct_usd,
                   largest_buy_wall_price, largest_sell_wall_price, spoof_wall_detected
            FROM order_book_microstructure
            WHERE symbol=?
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (symbol.upper(),),
        )
    else:
        cur = conn.execute(
            """
            SELECT symbol, timestamp, bid_ask_spread, bid_depth_1pct_usd, ask_depth_1pct_usd,
                   largest_buy_wall_price, largest_sell_wall_price, spoof_wall_detected
            FROM order_book_microstructure
            WHERE symbol=? AND timestamp <= ?
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (symbol.upper(), int(as_of_ms)),
        )
    row = cur.fetchone()
    return dict(row) if row is not None else None


def upsert_macro_sentiment(conn: sqlite3.Connection, row: dict) -> int:
    """Insert or replace one macro_onchain_sentiment snapshot."""
    sql = """
    INSERT INTO macro_onchain_sentiment (
        timestamp, fear_greed_index, altcoin_season_index, btc_dominance_pct,
        exchange_netflow_btc, exchange_reserve_btc, etf_net_flow_usd,
        global_market_cap_usd
    ) VALUES (
        :timestamp, :fear_greed_index, :altcoin_season_index, :btc_dominance_pct,
        :exchange_netflow_btc, :exchange_reserve_btc, :etf_net_flow_usd,
        :global_market_cap_usd
    )
    ON CONFLICT(timestamp) DO UPDATE SET
        fear_greed_index=COALESCE(excluded.fear_greed_index, macro_onchain_sentiment.fear_greed_index),
        altcoin_season_index=COALESCE(
            excluded.altcoin_season_index, macro_onchain_sentiment.altcoin_season_index
        ),
        btc_dominance_pct=COALESCE(
            excluded.btc_dominance_pct, macro_onchain_sentiment.btc_dominance_pct
        ),
        exchange_netflow_btc=COALESCE(
            excluded.exchange_netflow_btc, macro_onchain_sentiment.exchange_netflow_btc
        ),
        exchange_reserve_btc=COALESCE(
            excluded.exchange_reserve_btc, macro_onchain_sentiment.exchange_reserve_btc
        ),
        etf_net_flow_usd=COALESCE(excluded.etf_net_flow_usd, macro_onchain_sentiment.etf_net_flow_usd),
        global_market_cap_usd=COALESCE(
            excluded.global_market_cap_usd, macro_onchain_sentiment.global_market_cap_usd
        )
    """
    conn.execute(
        sql,
        {
            "timestamp": int(row["timestamp"]),
            "fear_greed_index": row.get("fear_greed_index"),
            "altcoin_season_index": row.get("altcoin_season_index"),
            "btc_dominance_pct": row.get("btc_dominance_pct"),
            "exchange_netflow_btc": row.get("exchange_netflow_btc"),
            "exchange_reserve_btc": row.get("exchange_reserve_btc"),
            "etf_net_flow_usd": row.get("etf_net_flow_usd"),
            "global_market_cap_usd": row.get("global_market_cap_usd"),
        },
    )
    conn.commit()
    return 1


def latest_macro_sentiment(conn: sqlite3.Connection) -> dict | None:
    cur = conn.execute(
        """
        SELECT timestamp, fear_greed_index, altcoin_season_index, btc_dominance_pct,
               exchange_netflow_btc, exchange_reserve_btc, etf_net_flow_usd,
               global_market_cap_usd
        FROM macro_onchain_sentiment
        ORDER BY timestamp DESC
        LIMIT 1
        """
    )
    row = cur.fetchone()
    return dict(row) if row is not None else None


def latest_derivatives_as_of(
    conn: sqlite3.Connection, symbol: str, *, as_of_ms: int | None = None
) -> dict | None:
    """Newest CoinGlass coin row known by as_of (pair symbol → base coin)."""
    coin = symbol.upper()
    for quote in ("USDT", "USD", "BUSD", "USDC"):
        if coin.endswith(quote) and len(coin) > len(quote):
            coin = coin[: -len(quote)]
            break
    cutoff = int(as_of_ms if as_of_ms is not None else time.time() * 1000)
    rows = as_of_derivatives(conn, coin, cutoff)
    return rows[-1] if rows else None


def load_newest_close(conn: sqlite3.Connection, symbol: str) -> float | None:
    cur = conn.execute(
        """
        SELECT close FROM market_technicals
        WHERE symbol=? AND close IS NOT NULL
        ORDER BY timestamp DESC
        LIMIT 1
        """,
        (symbol.upper(),),
    )
    row = cur.fetchone()
    if row is None or row[0] is None:
        return None
    return float(row[0])


def range_since_entry(
    conn: sqlite3.Connection, symbol: str, *, entry_ts: int
) -> tuple[float | None, float | None]:
    cur = conn.execute(
        """
        SELECT MIN(low), MAX(high) FROM market_technicals
        WHERE symbol=? AND timestamp >= ?
        """,
        (symbol.upper(), int(entry_ts)),
    )
    row = cur.fetchone()
    if row is None:
        return None, None
    lo, hi = row[0], row[1]
    return (None if lo is None else float(lo), None if hi is None else float(hi))


def load_latest_candle(conn: sqlite3.Connection, symbol: str, timeframe: str) -> dict | None:
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


def load_candle_at(
    conn: sqlite3.Connection,
    symbol: str,
    timeframe: str,
    timestamp: int,
) -> dict | None:
    """Single closed candle at exact timestamp (for confidence explainability)."""
    cur = conn.execute(
        """
        SELECT symbol, timestamp, timeframe, open, high, low, close, volume,
               atr_14, rsi_14, ema_20, ema_200
        FROM market_technicals
        WHERE symbol=? AND timeframe=? AND timestamp=?
        LIMIT 1
        """,
        (symbol.upper(), timeframe, int(timestamp)),
    )
    row = cur.fetchone()
    return dict(row) if row is not None else None


def load_recent_ohlcv(
    conn: sqlite3.Connection,
    symbol: str,
    timeframe: str,
    *,
    limit: int = 48,
) -> list[dict]:
    """Newest closed OHLC bars, oldest first. Cap 120. SQLite only (no live fetch)."""
    cap = max(1, min(int(limit), 120))
    cur = conn.execute(
        """
        SELECT timestamp, open, high, low, close
        FROM market_technicals
        WHERE symbol=? AND timeframe=?
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        (symbol.upper(), timeframe, cap),
    )
    rows = [dict(row) for row in cur.fetchall()]
    rows.reverse()
    return rows


def load_candles_in_range(
    conn: sqlite3.Connection,
    symbol: str,
    timeframe: str,
    *,
    since_ms: int,
    until_ms: int,
) -> list[dict]:
    """Closed candles with indicators in [since_ms, until_ms], oldest first."""
    cur = conn.execute(
        """
        SELECT symbol, timestamp, timeframe, open, high, low, close, volume,
               atr_14, rsi_14, ema_20, ema_200
        FROM market_technicals
        WHERE symbol=? AND timeframe=?
          AND timestamp >= ? AND timestamp <= ?
        ORDER BY timestamp ASC
        """,
        (symbol.upper(), timeframe, int(since_ms), int(until_ms)),
    )
    return [dict(row) for row in cur.fetchall()]


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


def _analysis_output_where(
    *,
    symbol: str | None = None,
    timeframe: str | None = None,
) -> tuple[str, list[object]]:
    clauses: list[str] = []
    params: list[object] = []
    if symbol:
        clauses.append("symbol = ?")
        params.append(symbol.upper())
    if timeframe:
        clauses.append("timeframe = ?")
        params.append(timeframe)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


def count_analysis_output(
    conn: sqlite3.Connection,
    *,
    symbol: str | None = None,
    timeframe: str | None = None,
) -> int:
    """Count analysis_output rows matching optional filters."""
    where, params = _analysis_output_where(symbol=symbol, timeframe=timeframe)
    cur = conn.execute(
        f"SELECT COUNT(*) AS n FROM analysis_output {where}",
        params,
    )
    row = cur.fetchone()
    return int(row["n"] if row is not None else 0)


def list_analysis_output(
    conn: sqlite3.Connection,
    *,
    symbol: str | None = None,
    timeframe: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """Return latest analysis_output rows, newest first."""
    where, params = _analysis_output_where(symbol=symbol, timeframe=timeframe)
    lim = max(1, min(int(limit), 500))
    off = max(0, int(offset))
    params.extend([lim, off])
    cur = conn.execute(
        f"""
        SELECT analysis_id, timestamp, symbol, timeframe, regime_state,
               confidence_score, action, invalidation_price, size_pct_equity, thesis
        FROM analysis_output
        {where}
        ORDER BY timestamp DESC
        LIMIT ? OFFSET ?
        """,
        params,
    )
    return [dict(row) for row in cur.fetchall()]


_SQLITE_IN_CHUNK = 400


def _parse_order_id_json(raw: object) -> list[str]:
    if raw is None or raw == "":
        return []
    parsed: object = raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if item is not None and str(item) != ""]


def _chunked(values: list[str], size: int = _SQLITE_IN_CHUNK) -> Iterator[list[str]]:
    for i in range(0, len(values), size):
        yield values[i : i + size]


def list_analysis_outcomes(
    conn: sqlite3.Connection,
    analysis_ids: list[str],
) -> dict[str, dict]:
    """Latest approval + fill counts keyed by analysis_id (empty ids omitted)."""
    ids = [str(item) for item in analysis_ids if item]
    if not ids:
        return {}
    unique = list(dict.fromkeys(ids))
    latest: dict[str, dict] = {}
    for chunk in _chunked(unique):
        placeholders = ",".join("?" * len(chunk))
        cur = conn.execute(
            f"""
            SELECT id, analysis_id, status, resolve_reason, resolved_at_ms,
                   paper_order_ids_json, created_at_ms
            FROM approval_queue
            WHERE analysis_id IN ({placeholders})
            ORDER BY COALESCE(resolved_at_ms, created_at_ms) DESC, created_at_ms DESC
            """,
            chunk,
        )
        for row in cur.fetchall():
            aid = str(row["analysis_id"] or "")
            if not aid or aid in latest:
                continue
            latest[aid] = dict(row)

    orders_by_aid: dict[str, list[dict]] = {aid: [] for aid in unique}
    orders_by_oid: dict[str, dict] = {}
    for chunk in _chunked(unique):
        placeholders = ",".join("?" * len(chunk))
        cur = conn.execute(
            f"""
            SELECT order_id, analysis_id, reason, approval_id
            FROM paper_orders
            WHERE analysis_id IN ({placeholders})
            """,
            chunk,
        )
        for row in cur.fetchall():
            rec = dict(row)
            aid = str(rec.get("analysis_id") or "")
            oid = str(rec.get("order_id") or "")
            if aid:
                orders_by_aid.setdefault(aid, []).append(rec)
            if oid:
                orders_by_oid[oid] = rec

    extra_oids: list[str] = []
    for queued_row in latest.values():
        extra_oids.extend(_parse_order_id_json(queued_row.get("paper_order_ids_json")))
    missing = [oid for oid in dict.fromkeys(extra_oids) if oid and oid not in orders_by_oid]
    for chunk in _chunked(missing):
        placeholders = ",".join("?" * len(chunk))
        cur = conn.execute(
            f"""
            SELECT order_id, analysis_id, reason, approval_id
            FROM paper_orders
            WHERE order_id IN ({placeholders})
            """,
            chunk,
        )
        for row in cur.fetchall():
            rec = dict(row)
            oid = str(rec.get("order_id") or "")
            if oid:
                orders_by_oid[oid] = rec

    out: dict[str, dict] = {}
    for aid in unique:
        approval_row: dict | None = latest.get(aid)
        json_ids = _parse_order_id_json(approval_row.get("paper_order_ids_json") if approval_row else None)
        if json_ids:
            fills = len(json_ids)
            fill_reasons = [
                str(orders_by_oid[oid].get("reason") or "")
                for oid in json_ids
                if oid in orders_by_oid and orders_by_oid[oid].get("reason")
            ]
        else:
            by_aid = orders_by_aid.get(aid) or []
            fills = len(by_aid)
            fill_reasons = [str(row.get("reason") or "") for row in by_aid if row.get("reason")]
        out[aid] = {
            "approval_id": approval_row.get("id") if approval_row else None,
            "approval_status": approval_row.get("status") if approval_row else None,
            "resolve_reason": approval_row.get("resolve_reason") if approval_row else None,
            "resolved_at_ms": approval_row.get("resolved_at_ms") if approval_row else None,
            "paper_order_ids_json": approval_row.get("paper_order_ids_json") if approval_row else None,
            "fills": fills,
            "fill_reasons": fill_reasons,
        }
    return out


def iter_analysis_outcome_facts(
    conn: sqlite3.Connection,
    *,
    symbol: str | None = None,
    timeframe: str | None = None,
) -> list[dict]:
    """Action + latest approval/fill facts for every matching analysis_output row."""
    where, params = _analysis_output_where(symbol=symbol, timeframe=timeframe)
    cur = conn.execute(
        f"SELECT analysis_id, action FROM analysis_output {where}",
        params,
    )
    rows = [dict(row) for row in cur.fetchall()]
    outcomes = list_analysis_outcomes(conn, [str(row["analysis_id"]) for row in rows])
    facts: list[dict] = []
    for row in rows:
        aid = str(row.get("analysis_id") or "")
        o = outcomes.get(aid) or {}
        approval = None
        if o.get("approval_id"):
            approval = {
                "id": o.get("approval_id"),
                "status": o.get("approval_status"),
                "resolve_reason": o.get("resolve_reason"),
                "resolved_at_ms": o.get("resolved_at_ms"),
            }
        facts.append(
            {
                "analysis_id": aid,
                "action": row.get("action"),
                "approval": approval,
                "fills": int(o.get("fills") or 0),
                "fill_reasons": list(o.get("fill_reasons") or []),
            }
        )
    return facts


STARTING_PAPER_EQUITY = 10_000.0


def reset_paper_ledger(conn: sqlite3.Connection, *, starting_equity: float = STARTING_PAPER_EQUITY) -> None:
    """Wipe paper fills/positions and reset cash/equity (for isolated backtests)."""
    conn.execute("DELETE FROM paper_orders")
    conn.execute("DELETE FROM paper_positions")
    start = float(starting_equity)
    for key, value in (
        ("starting_equity", start),
        ("cash", start),
        ("equity", start),
    ):
        conn.execute(
            """
            INSERT INTO paper_account (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, value),
        )
    conn.commit()


def ensure_paper_account(
    conn: sqlite3.Connection, *, starting_equity: float = STARTING_PAPER_EQUITY
) -> dict[str, float]:
    """Ensure paper_account keys exist; return cash/equity/starting_equity."""
    row = conn.execute("SELECT value FROM paper_account WHERE key='starting_equity'").fetchone()
    if row is None:
        conn.executemany(
            "INSERT INTO paper_account (key, value) VALUES (?, ?)",
            [
                ("starting_equity", float(starting_equity)),
                ("cash", float(starting_equity)),
                ("equity", float(starting_equity)),
            ],
        )
        conn.commit()
    return get_paper_account(conn)


def get_paper_account(conn: sqlite3.Connection) -> dict[str, float]:
    rows = conn.execute("SELECT key, value FROM paper_account").fetchall()
    data = {str(r[0]): float(r[1]) for r in rows}
    return {
        "starting_equity": data.get("starting_equity", STARTING_PAPER_EQUITY),
        "cash": data.get("cash", STARTING_PAPER_EQUITY),
        "equity": data.get("equity", STARTING_PAPER_EQUITY),
    }


def set_paper_account_value(conn: sqlite3.Connection, key: str, value: float) -> None:
    conn.execute(
        """
        INSERT INTO paper_account (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (key, float(value)),
    )


def insert_paper_order(conn: sqlite3.Connection, row: dict) -> None:
    payload = dict(row)
    payload.setdefault("fee_bps", None)
    payload.setdefault("approval_id", None)
    payload.setdefault("decision_source", None)
    conn.execute(
        """
        INSERT INTO paper_orders (
            order_id, ts, symbol, timeframe, side, qty, price,
            fee_usd, fee_bps, slip_bps, analysis_id, reason,
            approval_id, decision_source
        ) VALUES (
            :order_id, :ts, :symbol, :timeframe, :side, :qty, :price,
            :fee_usd, :fee_bps, :slip_bps, :analysis_id, :reason,
            :approval_id, :decision_source
        )
        """,
        payload,
    )


def upsert_paper_position(conn: sqlite3.Connection, row: dict) -> None:
    payload = dict(row)
    payload.setdefault("stop_price", None)
    payload.setdefault("stop_source", None)
    payload.setdefault("approval_id", None)
    conn.execute(
        """
        INSERT INTO paper_positions (
            symbol, side, qty, entry_price, entry_ts, unrealized_pnl, realized_pnl,
            stop_price, stop_source, approval_id
        ) VALUES (
            :symbol, :side, :qty, :entry_price, :entry_ts, :unrealized_pnl, :realized_pnl,
            :stop_price, :stop_source, :approval_id
        )
        ON CONFLICT(symbol) DO UPDATE SET
            side=excluded.side,
            qty=excluded.qty,
            entry_price=excluded.entry_price,
            entry_ts=excluded.entry_ts,
            unrealized_pnl=excluded.unrealized_pnl,
            realized_pnl=excluded.realized_pnl,
            stop_price=excluded.stop_price,
            stop_source=excluded.stop_source,
            approval_id=excluded.approval_id
        """,
        payload,
    )


def delete_paper_position(conn: sqlite3.Connection, symbol: str) -> None:
    conn.execute("DELETE FROM paper_positions WHERE symbol=?", (symbol.upper(),))


def get_paper_position(conn: sqlite3.Connection, symbol: str) -> dict | None:
    cur = conn.execute(
        """
        SELECT symbol, side, qty, entry_price, entry_ts, unrealized_pnl, realized_pnl,
               stop_price, stop_source, approval_id
        FROM paper_positions WHERE symbol=?
        """,
        (symbol.upper(),),
    )
    row = cur.fetchone()
    return dict(row) if row is not None else None


def list_paper_positions(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute(
        """
        SELECT symbol, side, qty, entry_price, entry_ts, unrealized_pnl, realized_pnl,
               stop_price, stop_source, approval_id
        FROM paper_positions
        ORDER BY symbol
        """
    )
    return [dict(row) for row in cur.fetchall()]


def list_paper_orders(conn: sqlite3.Connection, *, limit: int = 50) -> list[dict]:
    lim = max(1, min(int(limit), 500))
    cur = conn.execute(
        """
        SELECT order_id, ts, symbol, timeframe, side, qty, price,
               fee_usd, fee_bps, slip_bps, analysis_id, reason,
               approval_id, decision_source
        FROM paper_orders
        ORDER BY ts DESC
        LIMIT ?
        """,
        (lim,),
    )
    return [dict(row) for row in cur.fetchall()]


def list_paper_orders_asc(conn: sqlite3.Connection) -> list[dict]:
    """All paper fills oldest-first (for round-trip reconstruction)."""
    cur = conn.execute(
        """
        SELECT order_id, ts, symbol, timeframe, side, qty, price,
               fee_usd, fee_bps, slip_bps, analysis_id, reason,
               approval_id, decision_source
        FROM paper_orders
        ORDER BY ts ASC, order_id ASC
        """
    )
    return [dict(row) for row in cur.fetchall()]


def upsert_paper_decision_outcome(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO paper_decision_outcomes (
            approval_id, open_order_id, close_order_id, symbol, side,
            pnl_usd, r_multiple, hold_ms, decision_source, prompt_version, closed_at_ms
        ) VALUES (
            :approval_id, :open_order_id, :close_order_id, :symbol, :side,
            :pnl_usd, :r_multiple, :hold_ms, :decision_source, :prompt_version, :closed_at_ms
        )
        ON CONFLICT(open_order_id, close_order_id) DO UPDATE SET
            approval_id=excluded.approval_id,
            symbol=excluded.symbol,
            side=excluded.side,
            pnl_usd=excluded.pnl_usd,
            r_multiple=excluded.r_multiple,
            hold_ms=excluded.hold_ms,
            decision_source=excluded.decision_source,
            prompt_version=excluded.prompt_version,
            closed_at_ms=excluded.closed_at_ms
        """,
        {
            "approval_id": row.get("approval_id"),
            "open_order_id": str(row["open_order_id"]),
            "close_order_id": str(row["close_order_id"]),
            "symbol": str(row["symbol"]).upper(),
            "side": str(row["side"]),
            "pnl_usd": float(row["pnl_usd"]),
            "r_multiple": row.get("r_multiple"),
            "hold_ms": row.get("hold_ms"),
            "decision_source": str(row.get("decision_source") or "unknown"),
            "prompt_version": row.get("prompt_version"),
            "closed_at_ms": int(row["closed_at_ms"]),
        },
    )


def insert_paper_auto_run(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO paper_auto_runs (
            at_ms, model, prompt_version, ok, skipped, reason, processed,
            approved_n, rejected_n, deferred_n, apply_failed_n, duration_s, payload_json
        ) VALUES (
            :at_ms, :model, :prompt_version, :ok, :skipped, :reason, :processed,
            :approved_n, :rejected_n, :deferred_n, :apply_failed_n, :duration_s, :payload_json
        )
        """,
        {
            "at_ms": int(row["at_ms"]),
            "model": row.get("model"),
            "prompt_version": row.get("prompt_version"),
            "ok": 1 if row.get("ok") else 0,
            "skipped": 1 if row.get("skipped") else 0,
            "reason": row.get("reason"),
            "processed": row.get("processed"),
            "approved_n": row.get("approved_n"),
            "rejected_n": row.get("rejected_n"),
            "deferred_n": row.get("deferred_n"),
            "apply_failed_n": row.get("apply_failed_n"),
            "duration_s": row.get("duration_s"),
            "payload_json": row.get("payload_json"),
        },
    )
    conn.commit()


def list_paper_decision_outcomes(
    conn: sqlite3.Connection,
    *,
    since_ms: int | None = None,
    limit: int = 500,
) -> list[dict]:
    lim = max(1, min(int(limit), 5_000))
    if since_ms is None:
        cur = conn.execute(
            """
            SELECT id, approval_id, open_order_id, close_order_id, symbol, side,
                   pnl_usd, r_multiple, hold_ms, decision_source, prompt_version, closed_at_ms
            FROM paper_decision_outcomes
            ORDER BY closed_at_ms DESC
            LIMIT ?
            """,
            (lim,),
        )
    else:
        cur = conn.execute(
            """
            SELECT id, approval_id, open_order_id, close_order_id, symbol, side,
                   pnl_usd, r_multiple, hold_ms, decision_source, prompt_version, closed_at_ms
            FROM paper_decision_outcomes
            WHERE closed_at_ms >= ?
            ORDER BY closed_at_ms DESC
            LIMIT ?
            """,
            (int(since_ms), lim),
        )
    return [dict(row) for row in cur.fetchall()]


def load_latest_analysis(conn: sqlite3.Connection, symbol: str, timeframe: str) -> dict | None:
    cur = conn.execute(
        """
        SELECT analysis_id, timestamp, symbol, timeframe, regime_state,
               confidence_score, action, invalidation_price, size_pct_equity, thesis
        FROM analysis_output
        WHERE symbol=? AND timeframe=?
        ORDER BY timestamp DESC
        LIMIT 1
        """,
        (symbol.upper(), timeframe),
    )
    row = cur.fetchone()
    return dict(row) if row is not None else None


def upsert_performance_risk_metrics(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO performance_risk_metrics (
            strategy_id, timestamp, expected_value_ev, sharpe_ratio, sortino_ratio,
            max_drawdown_pct, daily_pnl_usd, regime_state, confidence_score
        ) VALUES (
            :strategy_id, :timestamp, :expected_value_ev, :sharpe_ratio, :sortino_ratio,
            :max_drawdown_pct, :daily_pnl_usd, :regime_state, :confidence_score
        )
        ON CONFLICT(strategy_id, timestamp) DO UPDATE SET
            expected_value_ev=excluded.expected_value_ev,
            sharpe_ratio=excluded.sharpe_ratio,
            sortino_ratio=excluded.sortino_ratio,
            max_drawdown_pct=excluded.max_drawdown_pct,
            daily_pnl_usd=excluded.daily_pnl_usd,
            regime_state=excluded.regime_state,
            confidence_score=excluded.confidence_score
        """,
        row,
    )


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
        "SELECT 1 FROM universe_membership WHERE universe_id=? AND symbol=? AND listed_at=?",
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


def universe_as_of(conn: sqlite3.Connection, universe_id: str, as_of_ms: int) -> list[str]:
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


_APPROVAL_COLUMNS = """
    id, created_at_ms, expires_at_ms, symbol, timeframe, analysis_id,
    action, regime_state, confidence_score, size_pct_equity, invalidation_price, status,
    resolved_at_ms, resolve_reason, paper_order_ids_json
"""


def get_approval(conn: sqlite3.Connection, approval_id: str) -> dict | None:
    cur = conn.execute(
        f"""
        SELECT {_APPROVAL_COLUMNS}
        FROM approval_queue
        WHERE id = ?
        """,
        (approval_id,),
    )
    row = cur.fetchone()
    return dict(row) if row is not None else None


def list_approvals(
    conn: sqlite3.Connection,
    *,
    status: str | None = "pending",
    limit: int = 50,
) -> list[dict]:
    lim = max(1, min(int(limit), 500))
    if status is None:
        cur = conn.execute(
            f"""
            SELECT {_APPROVAL_COLUMNS}
            FROM approval_queue
            ORDER BY created_at_ms DESC
            LIMIT ?
            """,
            (lim,),
        )
    else:
        cur = conn.execute(
            f"""
            SELECT {_APPROVAL_COLUMNS}
            FROM approval_queue
            WHERE status = ?
            ORDER BY created_at_ms DESC
            LIMIT ?
            """,
            (status, lim),
        )
    return [dict(row) for row in cur.fetchall()]


def upsert_pending_approval(conn: sqlite3.Connection, row: dict) -> dict:
    symbol = str(row["symbol"]).upper()
    timeframe = str(row["timeframe"])
    existing = conn.execute(
        """
        SELECT id FROM approval_queue
        WHERE symbol = ? AND timeframe = ? AND status = 'pending'
        LIMIT 1
        """,
        (symbol, timeframe),
    ).fetchone()
    approval_id = existing["id"] if existing else str(row["id"])
    payload = {
        "id": approval_id,
        "created_at_ms": int(row["created_at_ms"]),
        "expires_at_ms": int(row["expires_at_ms"]),
        "symbol": symbol,
        "timeframe": timeframe,
        "analysis_id": row.get("analysis_id"),
        "action": str(row["action"]),
        "regime_state": row.get("regime_state"),
        "confidence_score": row.get("confidence_score"),
        "size_pct_equity": row.get("size_pct_equity"),
        "invalidation_price": row.get("invalidation_price"),
    }
    try:
        conn.execute(
            """
            INSERT INTO approval_queue (
                id, created_at_ms, expires_at_ms, symbol, timeframe, analysis_id,
                action, regime_state, confidence_score, size_pct_equity, invalidation_price, status,
                resolved_at_ms, resolve_reason, paper_order_ids_json
            ) VALUES (
                :id, :created_at_ms, :expires_at_ms, :symbol, :timeframe, :analysis_id,
                :action, :regime_state, :confidence_score, :size_pct_equity, :invalidation_price, 'pending',
                NULL, NULL, NULL
            )
            ON CONFLICT(id) DO UPDATE SET
                created_at_ms=excluded.created_at_ms,
                expires_at_ms=excluded.expires_at_ms,
                analysis_id=excluded.analysis_id,
                action=excluded.action,
                regime_state=excluded.regime_state,
                confidence_score=excluded.confidence_score,
                size_pct_equity=excluded.size_pct_equity,
                invalidation_price=excluded.invalidation_price,
                status='pending',
                resolved_at_ms=NULL,
                resolve_reason=NULL,
                paper_order_ids_json=NULL
            """,
            payload,
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        existing = conn.execute(
            """
            SELECT id FROM approval_queue
            WHERE symbol = ? AND timeframe = ? AND status = 'pending'
            LIMIT 1
            """,
            (symbol, timeframe),
        ).fetchone()
        if existing is None:
            raise
        approval_id = existing["id"]
        conn.execute(
            """
            UPDATE approval_queue SET
                created_at_ms=:created_at_ms,
                expires_at_ms=:expires_at_ms,
                analysis_id=:analysis_id,
                action=:action,
                regime_state=:regime_state,
                confidence_score=:confidence_score,
                size_pct_equity=:size_pct_equity,
                invalidation_price=:invalidation_price,
                status='pending',
                resolved_at_ms=NULL,
                resolve_reason=NULL,
                paper_order_ids_json=NULL
            WHERE id = :id
            """,
            {**payload, "id": approval_id},
        )
        conn.commit()
    return dict(get_approval(conn, approval_id) or {})


def claim_approval_for_fill(
    conn: sqlite3.Connection,
    approval_id: str,
    *,
    now_ms: int,
    resolve_reason: str = "paper_fill",
    expected_analysis_id: str | None = None,
    commit: bool = True,
) -> dict | None:
    """Atomically claim a pending, unexpired row before paper or live fill."""
    ts = int(now_ms)
    sql = """
        UPDATE approval_queue SET
            status = 'approved',
            resolved_at_ms = ?,
            resolve_reason = ?
        WHERE id = ? AND status = 'pending' AND expires_at_ms > ?
        """
    params: list[Any] = [ts, resolve_reason, approval_id, ts]
    if expected_analysis_id is not None:
        # Guards the auto path: the row must still be the exact analysis that was reviewed.
        sql += " AND analysis_id = ?"
        params.append(expected_analysis_id)
    cur = conn.execute(sql, params)
    _commit(conn, commit=commit)
    if cur.rowcount == 0:
        return None
    row = get_approval(conn, approval_id)
    return dict(row) if row is not None else None


def set_approval_paper_order_ids(
    conn: sqlite3.Connection,
    approval_id: str,
    paper_order_ids_json: str | None,
    *,
    commit: bool = True,
) -> dict | None:
    conn.execute(
        """
        UPDATE approval_queue SET paper_order_ids_json = ?
        WHERE id = ?
        """,
        (paper_order_ids_json, approval_id),
    )
    _commit(conn, commit=commit)
    row = get_approval(conn, approval_id)
    return dict(row) if row is not None else None


def mark_approval_failed(
    conn: sqlite3.Connection,
    approval_id: str,
    *,
    resolve_reason: str,
    resolved_at_ms: int,
) -> dict | None:
    conn.execute(
        """
        UPDATE approval_queue SET
            status = 'failed',
            resolve_reason = ?,
            resolved_at_ms = ?
        WHERE id = ?
        """,
        (resolve_reason, int(resolved_at_ms), approval_id),
    )
    conn.commit()
    row = get_approval(conn, approval_id)
    return dict(row) if row is not None else None


def set_position_stop(
    conn: sqlite3.Connection,
    symbol: str,
    *,
    stop_price: float,
    stop_source: str,
    commit: bool = True,
) -> None:
    conn.execute(
        """
        UPDATE paper_positions SET stop_price = ?, stop_source = ?
        WHERE symbol = ?
        """,
        (float(stop_price), stop_source, symbol.upper()),
    )
    _commit(conn, commit=commit)


def insert_equity_snapshot(
    conn: sqlite3.Connection,
    *,
    ts: int,
    equity: float,
    cash: float,
    source: str,
    commit: bool = True,
) -> None:
    conn.execute(
        """
        INSERT INTO paper_equity_snapshots (ts, equity, cash, source)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(ts) DO UPDATE SET
            equity=excluded.equity,
            cash=excluded.cash,
            source=excluded.source
        """,
        (int(ts), float(equity), float(cash), source),
    )
    _commit(conn, commit=commit)


def day_open_equity(conn: sqlite3.Connection, day_start_ms: int) -> float | None:
    cur = conn.execute(
        """
        SELECT equity FROM paper_equity_snapshots
        WHERE ts >= ?
        ORDER BY ts ASC
        LIMIT 1
        """,
        (int(day_start_ms),),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return float(row[0])


def cancel_pending_approvals(conn: sqlite3.Connection, *, reason: str, ts: int, commit: bool = True) -> int:
    cur = conn.execute(
        """
        UPDATE approval_queue SET
            status = 'rejected',
            resolve_reason = ?,
            resolved_at_ms = ?
        WHERE status = 'pending'
        """,
        (reason, int(ts)),
    )
    _commit(conn, commit=commit)
    return int(cur.rowcount)


def resolve_approval(
    conn: sqlite3.Connection,
    approval_id: str,
    *,
    status: str,
    resolve_reason: str | None = None,
    paper_order_ids_json: str | None = None,
    resolved_at_ms: int | None = None,
) -> dict | None:
    resolved = int(resolved_at_ms) if resolved_at_ms is not None else int(time.time() * 1000)
    cur = conn.execute(
        """
        UPDATE approval_queue SET
            status = ?,
            resolve_reason = ?,
            paper_order_ids_json = ?,
            resolved_at_ms = ?
        WHERE id = ? AND status = 'pending'
        """,
        (status, resolve_reason, paper_order_ids_json, resolved, approval_id),
    )
    conn.commit()
    if cur.rowcount == 0:
        return None
    row = get_approval(conn, approval_id)
    return dict(row) if row is not None else None


def expire_pending_approvals(conn: sqlite3.Connection, *, now_ms: int) -> int:
    cur = conn.execute(
        """
        UPDATE approval_queue SET
            status = 'timed_out',
            resolved_at_ms = ?
        WHERE status = 'pending' AND expires_at_ms <= ?
        """,
        (int(now_ms), int(now_ms)),
    )
    conn.commit()
    return int(cur.rowcount)


def list_expired_pending_approvals(conn: sqlite3.Connection, *, now_ms: int) -> list[dict]:
    cur = conn.execute(
        f"""
        SELECT {_APPROVAL_COLUMNS}
        FROM approval_queue
        WHERE status = 'pending' AND expires_at_ms <= ?
        ORDER BY expires_at_ms ASC
        """,
        (int(now_ms),),
    )
    return [dict(row) for row in cur.fetchall()]


def set_approval_resolve_reason(
    conn: sqlite3.Connection,
    approval_id: str,
    reason: str,
    *,
    resolved_at_ms: int | None = None,
    commit: bool = True,
) -> dict | None:
    """Overwrite resolve_reason only. With resolved_at_ms, only when the row was resolved at exactly that instant (same run)."""
    if resolved_at_ms is not None:
        cur = conn.execute(
            "UPDATE approval_queue SET resolve_reason = ? WHERE id = ? AND resolved_at_ms = ?",
            (reason, approval_id, int(resolved_at_ms)),
        )
        _commit(conn, commit=commit)
        if cur.rowcount == 0:
            return None
        return get_approval(conn, approval_id)
    conn.execute(
        "UPDATE approval_queue SET resolve_reason = ? WHERE id = ?",
        (reason, approval_id),
    )
    _commit(conn, commit=commit)
    return get_approval(conn, approval_id)


def get_analysis_output(conn: sqlite3.Connection, analysis_id: str) -> dict | None:
    cur = conn.execute(
        """
        SELECT analysis_id, timestamp, symbol, timeframe, regime_state,
               confidence_score, action, invalidation_price, size_pct_equity, thesis
        FROM analysis_output
        WHERE analysis_id = ?
        """,
        (analysis_id,),
    )
    row = cur.fetchone()
    return dict(row) if row is not None else None


_LLM_REVIEW_COLUMNS = "approval_id, model, decision, reason, brief_hash, created_at_ms"


def insert_llm_review(conn: sqlite3.Connection, row: dict) -> dict:
    """Append one LLM decision for an approval; returns the stored row."""
    conn.execute(
        f"""
        INSERT INTO approval_llm_reviews ({_LLM_REVIEW_COLUMNS})
        VALUES (:approval_id, :model, :decision, :reason, :brief_hash, :created_at_ms)
        """,
        {
            "approval_id": str(row["approval_id"]),
            "model": str(row["model"]),
            "decision": str(row["decision"]),
            "reason": row.get("reason"),
            "brief_hash": row.get("brief_hash"),
            "created_at_ms": int(row["created_at_ms"]),
        },
    )
    conn.commit()
    latest = get_latest_llm_review(conn, str(row["approval_id"]))
    if latest is None:
        raise RuntimeError("approval_llm_reviews insert did not persist")
    return latest


def get_latest_llm_review(conn: sqlite3.Connection, approval_id: str) -> dict | None:
    cur = conn.execute(
        f"""
        SELECT {_LLM_REVIEW_COLUMNS}
        FROM approval_llm_reviews
        WHERE approval_id = ?
        ORDER BY created_at_ms DESC, id DESC
        LIMIT 1
        """,
        (approval_id,),
    )
    row = cur.fetchone()
    return dict(row) if row is not None else None


_LIVE_ORDER_COLUMNS = (
    "id, created_at_ms, approval_id, venue, symbol, side, order_type, "
    "requested_qty, requested_notional_usd, status, venue_order_id, "
    "venue_response_json, error, kill_switch_clear, caps_ok, realized_pnl_usd, "
    "client_order_id, venue_status, executed_qty, cummulative_quote_qty, "
    "fills_count, reconciled_at_ms"
)
LIVE_ORDER_OPEN_STATUSES = ("submitted", "partially_filled")


def insert_live_order(conn: sqlite3.Connection, row: dict) -> dict:
    """Insert one live order audit row. Returns the stored dict."""
    conn.execute(
        """
        INSERT INTO live_orders (
            id, created_at_ms, approval_id, venue, symbol, side, order_type,
            requested_qty, requested_notional_usd, status, venue_order_id,
            venue_response_json, error, kill_switch_clear, caps_ok, realized_pnl_usd,
            client_order_id, venue_status, executed_qty, cummulative_quote_qty,
            fills_count, reconciled_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["id"],
            int(row["created_at_ms"]),
            row.get("approval_id"),
            row["venue"],
            row["symbol"],
            row["side"],
            row["order_type"],
            row.get("requested_qty"),
            row.get("requested_notional_usd"),
            row["status"],
            row.get("venue_order_id"),
            row.get("venue_response_json"),
            row.get("error"),
            int(row.get("kill_switch_clear", 0)),
            int(row.get("caps_ok", 0)),
            row.get("realized_pnl_usd"),
            row.get("client_order_id"),
            row.get("venue_status"),
            row.get("executed_qty"),
            row.get("cummulative_quote_qty"),
            row.get("fills_count"),
            row.get("reconciled_at_ms"),
        ),
    )
    conn.commit()
    return get_live_order(conn, row["id"]) or dict(row)


def get_live_order(conn: sqlite3.Connection, order_id: str) -> dict | None:
    cur = conn.execute(
        f"SELECT {_LIVE_ORDER_COLUMNS} FROM live_orders WHERE id = ?",
        (order_id,),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def get_live_order_by_client_id(conn: sqlite3.Connection, client_order_id: str) -> dict | None:
    """Latest live order row carrying this venue client order id (idempotency lookup)."""
    cur = conn.execute(
        f"""
        SELECT {_LIVE_ORDER_COLUMNS} FROM live_orders
        WHERE client_order_id = ?
        ORDER BY created_at_ms DESC
        LIMIT 1
        """,
        (client_order_id,),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def list_live_orders_open(conn: sqlite3.Connection, *, limit: int = 200) -> list[dict]:
    """Live orders not yet terminal on the venue (need reconciliation)."""
    placeholders = ",".join("?" for _ in LIVE_ORDER_OPEN_STATUSES)
    cur = conn.execute(
        f"""
        SELECT {_LIVE_ORDER_COLUMNS} FROM live_orders
        WHERE status IN ({placeholders}) AND client_order_id IS NOT NULL
        ORDER BY created_at_ms ASC
        LIMIT ?
        """,
        (*LIVE_ORDER_OPEN_STATUSES, max(1, min(int(limit), 1000))),
    )
    return [dict(row) for row in cur.fetchall()]


def live_spot_inventory(
    conn: sqlite3.Connection,
    symbol: str,
    *,
    exclude_id: str | None = None,
) -> tuple[float, float]:
    """Net filled base qty and average entry from live fills (BUY minus SELL)."""
    if exclude_id:
        rows = conn.execute(
            """
            SELECT side, executed_qty, cummulative_quote_qty
            FROM live_orders
            WHERE symbol = ?
              AND id != ?
              AND executed_qty IS NOT NULL
              AND executed_qty > 0
            ORDER BY created_at_ms ASC
            """,
            (symbol.upper(), exclude_id),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT side, executed_qty, cummulative_quote_qty
            FROM live_orders
            WHERE symbol = ?
              AND executed_qty IS NOT NULL
              AND executed_qty > 0
            ORDER BY created_at_ms ASC
            """,
            (symbol.upper(),),
        ).fetchall()
    qty = 0.0
    cost = 0.0
    for row in rows:
        filled = float(row["executed_qty"] or 0)
        quote = float(row["cummulative_quote_qty"] or 0)
        if str(row["side"]).upper() == "BUY":
            qty += filled
            cost += quote
            continue
        if qty <= 0:
            continue
        avg = cost / qty
        sold = min(filled, qty)
        cost -= avg * sold
        qty -= sold
    avg_entry = (cost / qty) if qty > 0 else 0.0
    return qty, avg_entry


def list_live_symbols(conn: sqlite3.Connection) -> list[str]:
    """Symbols with at least one live fill (candidates for non-zero inventory)."""
    cur = conn.execute(
        """
        SELECT DISTINCT symbol FROM live_orders
        WHERE executed_qty IS NOT NULL AND executed_qty > 0
        ORDER BY symbol
        """
    )
    return [str(row[0]) for row in cur.fetchall()]


def get_latest_live_buy(conn: sqlite3.Connection, symbol: str) -> dict | None:
    """Most recent filled (or partially filled) live BUY for a symbol."""
    cur = conn.execute(
        f"""
        SELECT {_LIVE_ORDER_COLUMNS} FROM live_orders
        WHERE symbol = ? AND side = 'BUY' AND executed_qty IS NOT NULL AND executed_qty > 0
        ORDER BY created_at_ms DESC, id DESC
        LIMIT 1
        """,
        (symbol.upper(),),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def update_live_order_fill(
    conn: sqlite3.Connection,
    order_id: str,
    *,
    status: str,
    venue_status: str | None,
    executed_qty: float | None,
    cummulative_quote_qty: float | None,
    fills_count: int | None,
    venue_order_id: str | None,
    venue_response_json: str | None,
    reconciled_at_ms: int,
    error: str | None = None,
    realized_pnl_usd: float | None = None,
) -> dict | None:
    """Write reconciled fill state for one live order."""
    conn.execute(
        """
        UPDATE live_orders
        SET status = ?, venue_status = ?, executed_qty = ?, cummulative_quote_qty = ?,
            fills_count = ?, venue_order_id = COALESCE(?, venue_order_id),
            venue_response_json = COALESCE(?, venue_response_json),
            reconciled_at_ms = ?, error = COALESCE(?, error),
            realized_pnl_usd = COALESCE(?, realized_pnl_usd)
        WHERE id = ?
        """,
        (
            status,
            venue_status,
            executed_qty,
            cummulative_quote_qty,
            fills_count,
            venue_order_id,
            venue_response_json,
            int(reconciled_at_ms),
            error,
            realized_pnl_usd,
            order_id,
        ),
    )
    conn.commit()
    return get_live_order(conn, order_id)


def sum_live_realized_pnl_utc_day(conn: sqlite3.Connection, *, day_start_ms: int, day_end_ms: int) -> float:
    """Sum realized_pnl_usd for live_orders created in [day_start_ms, day_end_ms)."""
    cur = conn.execute(
        """
        SELECT COALESCE(SUM(realized_pnl_usd), 0)
        FROM live_orders
        WHERE created_at_ms >= ? AND created_at_ms < ?
          AND realized_pnl_usd IS NOT NULL
        """,
        (int(day_start_ms), int(day_end_ms)),
    )
    return float(cur.fetchone()[0] or 0.0)
