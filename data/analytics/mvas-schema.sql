-- Jarvise SQLite schema (documentation mirror of jarvise_ingest.db.SCHEMA_SQL)
-- Regenerate: python -c "from jarvise_ingest.db import SCHEMA_SQL; print(SCHEMA_SQL)"
-- Runtime owner: src/jarvise_ingest/db.py (migrate() applies SCHEMA_SQL + numbered
-- migrations tracked in PRAGMA user_version; current SCHEMA_VERSION = 6).
-- Connection pragmas: journal_mode=WAL, synchronous=NORMAL, busy_timeout=5000, foreign_keys=ON.
-- Retention: `jarvise db prune` trims order_book_microstructure, exchange_balances,
-- and superseded derivatives_analytics versions only; paper/approval/live tables are never pruned.
-- Notebook: Jarvise : Crypto Trader (14e11c63-e2ee-4b49-898f-b0cc4c61cb4e)
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
    reason TEXT
);

CREATE TABLE IF NOT EXISTS paper_positions (
    symbol TEXT NOT NULL PRIMARY KEY,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    entry_price REAL NOT NULL,
    entry_ts INTEGER NOT NULL,
    unrealized_pnl REAL NOT NULL DEFAULT 0,
    realized_pnl REAL NOT NULL DEFAULT 0
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
