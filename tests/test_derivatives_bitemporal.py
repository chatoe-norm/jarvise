"""Derivatives versions are append-only: event time + knowledge time.

CoinGlass revises funding / OI after the fact. Overwriting the prior row would
make a backtest see values that did not exist at decision time.
"""

from pathlib import Path

from jarvise_ingest.db import (
    append_derivatives,
    as_of_derivatives,
    open_db,
)


def _row(
    *,
    funding: float,
    oi: float = 1e9,
    ts: int = 1_700_000_000_000,
    symbol: str = "BTC",
) -> dict:
    return {
        "symbol": symbol,
        "timestamp": ts,
        "open_interest_usd": oi,
        "funding_rate": funding,
        "long_short_ratio": None,
        "liquidations_24h_usd": 1e6,
    }


def test_identical_reingest_does_not_add_a_version(tmp_path: Path):
    conn = open_db(tmp_path / "t.db")
    row = _row(funding=0.0001)

    assert append_derivatives(conn, [row], ingested_at=1_800_000_000_000) == 1
    assert append_derivatives(conn, [row], ingested_at=1_800_000_100_000) == 0
    assert _version_count(conn) == 1
    conn.close()


def test_revised_funding_keeps_both_versions(tmp_path: Path):
    conn = open_db(tmp_path / "t.db")
    ts = 1_700_000_000_000

    append_derivatives(conn, [_row(funding=0.0001)], ingested_at=1_800_000_000_000)
    append_derivatives(conn, [_row(funding=0.0005)], ingested_at=1_800_000_200_000)

    assert _version_count(conn) == 2
    cur = conn.execute(
        "SELECT funding_rate, ingested_at FROM derivatives_analytics "
        "WHERE symbol=? AND timestamp=? ORDER BY ingested_at",
        ("BTC", ts),
    )
    rows = cur.fetchall()
    assert rows[0][0] == 0.0001
    assert rows[1][0] == 0.0005
    conn.close()


def test_as_of_returns_the_version_known_at_decision_time(tmp_path: Path):
    conn = open_db(tmp_path / "t.db")
    ts = 1_700_000_000_000

    append_derivatives(conn, [_row(funding=0.0001)], ingested_at=1_800_000_000_000)
    append_derivatives(conn, [_row(funding=0.0005)], ingested_at=1_800_000_200_000)

    before_revision = as_of_derivatives(conn, "BTC", as_of_ms=1_800_000_100_000)
    after_revision = as_of_derivatives(conn, "BTC", as_of_ms=1_800_000_300_000)

    assert len(before_revision) == 1
    assert before_revision[0]["timestamp"] == ts
    assert before_revision[0]["funding_rate"] == 0.0001
    assert after_revision[0]["funding_rate"] == 0.0005
    conn.close()


def test_as_of_hides_rows_ingested_after_the_cutoff(tmp_path: Path):
    conn = open_db(tmp_path / "t.db")

    append_derivatives(conn, [_row(funding=0.0001)], ingested_at=1_800_000_000_000)

    assert as_of_derivatives(conn, "BTC", as_of_ms=1_799_999_999_999) == []
    conn.close()


def test_as_of_picks_latest_eligible_version_per_event_time(tmp_path: Path):
    conn = open_db(tmp_path / "t.db")
    early = 1_700_000_000_000
    late = 1_700_003_600_000

    append_derivatives(
        conn,
        [_row(funding=0.0001, ts=early), _row(funding=0.0002, ts=late)],
        ingested_at=1_800_000_000_000,
    )
    append_derivatives(
        conn,
        [_row(funding=0.0009, ts=early)],
        ingested_at=1_800_000_500_000,
    )

    snapshot = as_of_derivatives(conn, "BTC", as_of_ms=1_800_000_600_000)
    by_ts = {row["timestamp"]: row["funding_rate"] for row in snapshot}

    assert by_ts[early] == 0.0009
    assert by_ts[late] == 0.0002
    conn.close()


def test_migrate_adds_ingested_at_to_legacy_table(tmp_path: Path):
    """Existing DBs created before this change still open and query."""
    import sqlite3

    db = tmp_path / "legacy.db"
    raw = sqlite3.connect(str(db))
    raw.executescript(
        """
        CREATE TABLE derivatives_analytics (
            symbol TEXT NOT NULL,
            timestamp INTEGER NOT NULL,
            open_interest_usd REAL,
            funding_rate REAL,
            long_short_ratio REAL,
            liquidations_24h_usd REAL,
            PRIMARY KEY (symbol, timestamp)
        );
        INSERT INTO derivatives_analytics VALUES
            ('BTC', 1700000000000, 1e9, 0.0001, NULL, 1e6);
        """
    )
    raw.commit()
    raw.close()

    conn = open_db(db)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(derivatives_analytics)")}
    assert "ingested_at" in cols

    snapshot = as_of_derivatives(conn, "BTC", as_of_ms=9_000_000_000_000)
    assert len(snapshot) == 1
    assert snapshot[0]["funding_rate"] == 0.0001
    conn.close()


def _version_count(conn) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM derivatives_analytics").fetchone()[0])
