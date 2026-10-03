"""SQLite durability: pragmas, user_version migrations, schema doc sync, retention prune."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jarvise.cli import app
from jarvise_exchange.db import insert_snapshot, latest_snapshot
from jarvise_exchange.db import open_db as exchange_open_db
from jarvise_exchange.models import SpotBalance
from jarvise_ingest.db import (
    MIGRATIONS,
    SCHEMA_SQL,
    SCHEMA_VERSION,
    append_derivatives,
    db_pragmas,
    latest_derivatives_as_of,
    migrate,
    open_db,
    schema_version,
    upsert_order_book,
)
from jarvise_ingest.retention import PROTECTED_TABLES, parse_age, prune

ROOT = Path(__file__).resolve().parents[1]


def test_connect_pragmas(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "p.db")
    pragmas = db_pragmas(conn)
    assert pragmas["journal_mode"].lower() == "wal"
    assert pragmas["busy_timeout_ms"] == 5000
    assert pragmas["foreign_keys"] is True
    assert pragmas["synchronous"] == 1  # NORMAL
    assert pragmas["user_version"] == SCHEMA_VERSION
    conn.close()


def test_migrations_are_numbered_and_idempotent(tmp_path: Path) -> None:
    versions = [v for v, _, _, _ in MIGRATIONS]
    assert versions == list(range(1, len(MIGRATIONS) + 1))
    # Only the destructive bitemporal rebuild is version-gated; additive steps self-heal.
    assert [repair for _, _, _, repair in MIGRATIONS] == [
        False,
        True,
        True,
        True,
        True,
        True,
        True,
    ]
    conn = open_db(tmp_path / "m.db")
    assert schema_version(conn) == SCHEMA_VERSION
    # Second migrate advances nothing.
    assert migrate(conn) == []
    conn.close()


def test_legacy_db_upgrades_from_user_version_zero(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(str(path))
    raw.executescript(
        """
        CREATE TABLE paper_orders (
            order_id TEXT NOT NULL PRIMARY KEY, ts INTEGER NOT NULL, symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL, side TEXT NOT NULL, qty REAL NOT NULL, price REAL NOT NULL,
            fee_usd REAL NOT NULL, slip_bps REAL NOT NULL, analysis_id TEXT, reason TEXT
        );
        CREATE TABLE live_orders (
            id TEXT NOT NULL PRIMARY KEY, created_at_ms INTEGER NOT NULL, approval_id TEXT,
            venue TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL, order_type TEXT NOT NULL,
            requested_qty REAL, requested_notional_usd REAL, status TEXT NOT NULL,
            venue_order_id TEXT, venue_response_json TEXT, error TEXT,
            kill_switch_clear INTEGER NOT NULL, caps_ok INTEGER NOT NULL, realized_pnl_usd REAL
        );
        """
    )
    raw.commit()
    raw.close()
    conn = open_db(path)
    cols_paper = {r[1] for r in conn.execute("PRAGMA table_info(paper_orders)")}
    cols_live = {r[1] for r in conn.execute("PRAGMA table_info(live_orders)")}
    assert "fee_bps" in cols_paper
    assert {"client_order_id", "executed_qty", "reconciled_at_ms"} <= cols_live
    assert schema_version(conn) == SCHEMA_VERSION
    assert "exchange_balances" in {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    conn.close()


def test_exchange_db_shares_schema_owner(tmp_path: Path) -> None:
    conn = exchange_open_db(tmp_path / "x.db")
    assert schema_version(conn) == SCHEMA_VERSION
    insert_snapshot(
        conn,
        "binance",
        [SpotBalance(venue="binance", asset="BTC", free=1, locked=0, total=1)],
        1_000,
    )
    conn.commit()
    ts, rows = latest_snapshot(conn, "binance")
    assert ts == 1_000 and rows[0].asset == "BTC"
    conn.close()


def test_schema_doc_mirrors_runtime_tables() -> None:
    doc = (ROOT / "data" / "analytics" / "mvas-schema.sql").read_text(encoding="utf-8")
    runtime_tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", SCHEMA_SQL))
    doc_tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", doc))
    assert runtime_tables <= doc_tables
    assert "fee_bps" in doc and "client_order_id" in doc and "exchange_balances" in doc
    assert f"SCHEMA_VERSION = {SCHEMA_VERSION}" in doc


def test_parse_age() -> None:
    assert parse_age("180d") == 180 * 86_400_000
    assert parse_age("12h") == 12 * 3_600_000
    assert parse_age("90m") == 90 * 60_000
    for bad in ("", "10", "x", "-1d", "0d"):
        with pytest.raises(ValueError):
            parse_age(bad)


def test_prune_trims_only_unbounded_tables(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "r.db")
    now = 1_000_000_000_000
    old = now - 400 * 86_400_000
    recent = now - 10 * 86_400_000
    for ts in (old, old + 1, recent):
        upsert_order_book(
            conn,
            {
                "symbol": "BTCUSDT",
                "timestamp": ts,
                "bid_ask_spread": 0.0001,
                "bid_depth_1pct_usd": 1.0,
                "ask_depth_1pct_usd": 1.0,
                "largest_buy_wall_price": None,
                "largest_sell_wall_price": None,
                "spoof_wall_detected": 0,
            },
        )
    # Two snapshots per venue: the old one is prunable, the newest is always kept.
    bal = [SpotBalance(venue="binance", asset="BTC", free=1, locked=0, total=1)]
    insert_snapshot(conn, "binance", bal, old)
    insert_snapshot(conn, "binance", bal, old + 5)
    # Derivatives: two versions of one old bar; only the superseded version goes.
    append_derivatives(
        conn,
        [{"symbol": "BTC", "timestamp": old, "open_interest_usd": 1.0, "funding_rate": 0.01, "long_short_ratio": None, "liquidations_24h_usd": None}],
        ingested_at=old + 10,
    )
    append_derivatives(
        conn,
        [{"symbol": "BTC", "timestamp": old, "open_interest_usd": 2.0, "funding_rate": 0.01, "long_short_ratio": None, "liquidations_24h_usd": None}],
        ingested_at=old + 20,
    )
    conn.commit()

    dry = prune(conn, older_than_ms=parse_age("180d"), now_ms=now, dry_run=True)
    assert dry["deleted"] == {
        "order_book_microstructure": 2,
        "exchange_balances": 1,
        "derivatives_analytics": 1,
    }
    assert conn.execute("SELECT COUNT(*) FROM order_book_microstructure").fetchone()[0] == 3

    wet = prune(conn, older_than_ms=parse_age("180d"), now_ms=now)
    assert wet["deleted"] == dry["deleted"] and wet["dry_run"] is False
    assert conn.execute("SELECT COUNT(*) FROM order_book_microstructure").fetchone()[0] == 1
    assert latest_snapshot(conn, "binance")[0] == old + 5
    latest = latest_derivatives_as_of(conn, "BTC", as_of_ms=now)
    assert latest is not None and latest["open_interest_usd"] == 2.0
    assert set(PROTECTED_TABLES) >= {"paper_orders", "approval_queue", "live_orders", "market_technicals"}
    conn.close()


def test_db_cli_status_and_prune(tmp_path: Path) -> None:
    db = tmp_path / "cli.db"
    open_db(db).close()
    runner = CliRunner()
    res = runner.invoke(app, ["db", "status", "--db", str(db), "--json"])
    assert res.exit_code == 0, res.output
    assert f'"user_version": {SCHEMA_VERSION}' in res.output
    res = runner.invoke(app, ["db", "prune", "--db", str(db), "--older-than", "30d", "--dry-run", "--json"])
    assert res.exit_code == 0, res.output
    assert '"dry_run": true' in res.output
    res = runner.invoke(app, ["db", "prune", "--db", str(db), "--older-than", "bogus"])
    assert res.exit_code == 2
