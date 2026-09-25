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


STARTING_PAPER_EQUITY = 10_000.0


def ensure_paper_account(
    conn: sqlite3.Connection, *, starting_equity: float = STARTING_PAPER_EQUITY
) -> dict[str, float]:
    """Ensure paper_account keys exist; return cash/equity/starting_equity."""
    row = conn.execute(
        "SELECT value FROM paper_account WHERE key='starting_equity'"
    ).fetchone()
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
    conn.execute(
        """
        INSERT INTO paper_orders (
            order_id, ts, symbol, timeframe, side, qty, price,
            fee_usd, slip_bps, analysis_id, reason
        ) VALUES (
            :order_id, :ts, :symbol, :timeframe, :side, :qty, :price,
            :fee_usd, :slip_bps, :analysis_id, :reason
        )
        """,
        row,
    )


def upsert_paper_position(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO paper_positions (
            symbol, side, qty, entry_price, entry_ts, unrealized_pnl, realized_pnl
        ) VALUES (
            :symbol, :side, :qty, :entry_price, :entry_ts, :unrealized_pnl, :realized_pnl
        )
        ON CONFLICT(symbol) DO UPDATE SET
            side=excluded.side,
            qty=excluded.qty,
            entry_price=excluded.entry_price,
            entry_ts=excluded.entry_ts,
            unrealized_pnl=excluded.unrealized_pnl,
            realized_pnl=excluded.realized_pnl
        """,
        row,
    )


def delete_paper_position(conn: sqlite3.Connection, symbol: str) -> None:
    conn.execute("DELETE FROM paper_positions WHERE symbol=?", (symbol.upper(),))


def get_paper_position(conn: sqlite3.Connection, symbol: str) -> dict | None:
    cur = conn.execute(
        """
        SELECT symbol, side, qty, entry_price, entry_ts, unrealized_pnl, realized_pnl
        FROM paper_positions WHERE symbol=?
        """,
        (symbol.upper(),),
    )
    row = cur.fetchone()
    return dict(row) if row is not None else None


def list_paper_positions(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute(
        """
        SELECT symbol, side, qty, entry_price, entry_ts, unrealized_pnl, realized_pnl
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
               fee_usd, slip_bps, analysis_id, reason
        FROM paper_orders
        ORDER BY ts DESC
        LIMIT ?
        """,
        (lim,),
    )
    return [dict(row) for row in cur.fetchall()]


def load_latest_analysis(
    conn: sqlite3.Connection, symbol: str, timeframe: str
) -> dict | None:
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


_APPROVAL_COLUMNS = """
    id, created_at_ms, expires_at_ms, symbol, timeframe, analysis_id,
    action, regime_state, confidence_score, size_pct_equity, status,
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
    }
    try:
        conn.execute(
            """
            INSERT INTO approval_queue (
                id, created_at_ms, expires_at_ms, symbol, timeframe, analysis_id,
                action, regime_state, confidence_score, size_pct_equity, status,
                resolved_at_ms, resolve_reason, paper_order_ids_json
            ) VALUES (
                :id, :created_at_ms, :expires_at_ms, :symbol, :timeframe, :analysis_id,
                :action, :regime_state, :confidence_score, :size_pct_equity, 'pending',
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
                status='pending',
                resolved_at_ms=NULL,
                resolve_reason=NULL,
                paper_order_ids_json=NULL
            WHERE id = :id
            """,
            {**payload, "id": approval_id},
        )
        conn.commit()
    return dict(get_approval(conn, approval_id))


def claim_approval_for_fill(
    conn: sqlite3.Connection,
    approval_id: str,
    *,
    now_ms: int,
) -> dict | None:
    """Atomically claim a pending, unexpired row before paper fill."""
    ts = int(now_ms)
    cur = conn.execute(
        """
        UPDATE approval_queue SET
            status = 'approved',
            resolved_at_ms = ?,
            resolve_reason = 'paper_fill'
        WHERE id = ? AND status = 'pending' AND expires_at_ms > ?
        """,
        (ts, approval_id, ts),
    )
    conn.commit()
    if cur.rowcount == 0:
        return None
    row = get_approval(conn, approval_id)
    return dict(row) if row is not None else None


def set_approval_paper_order_ids(
    conn: sqlite3.Connection,
    approval_id: str,
    paper_order_ids_json: str | None,
) -> dict | None:
    conn.execute(
        """
        UPDATE approval_queue SET paper_order_ids_json = ?
        WHERE id = ?
        """,
        (paper_order_ids_json, approval_id),
    )
    conn.commit()
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


def resolve_approval(
    conn: sqlite3.Connection,
    approval_id: str,
    *,
    status: str,
    resolve_reason: str | None = None,
    paper_order_ids_json: str | None = None,
    resolved_at_ms: int | None = None,
) -> dict | None:
    resolved = (
        int(resolved_at_ms)
        if resolved_at_ms is not None
        else int(time.time() * 1000)
    )
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
