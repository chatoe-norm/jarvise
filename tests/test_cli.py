import json
import os
import sqlite3
from pathlib import Path

import pytest

from jarvise_ingest.cli import run

BACKFILL_BASE = ["--symbol", "BTCUSDT", "--skip-derivatives"]


def test_missing_symbol_exit_2():
    assert run([]) == 2


def test_until_without_since_exit_2():
    assert run([*BACKFILL_BASE, "--until", "2026-01-01"]) == 2


def test_unparseable_since_exit_2():
    assert run([*BACKFILL_BASE, "--since", "last tuesday"]) == 2


def test_since_after_until_exit_2():
    assert run([*BACKFILL_BASE, "--since", "2026-02-01", "--until", "2026-01-01"]) == 2


def test_dry_run_reports_the_backfill_window(tmp_path: Path, capsys):
    db = tmp_path / "missing" / "jarvise.db"
    code = run(
        [*BACKFILL_BASE, "--since", "2026-01-01", "--dry-run", "--json", "--db", str(db)]
    )

    assert code == 0
    planned = json.loads(capsys.readouterr().out)["market_technicals"]["BTCUSDT"]
    assert planned["since"] == "2026-01-01T00:00:00+00:00"
    assert planned["until"] is None
    # backfill pages at the request cap unless --limit says otherwise
    assert planned["page_limit"] == 1000
    assert not db.exists()


@pytest.mark.skipif(
    not os.environ.get("JARVISE_NET_TESTS"),
    reason="hits the public Binance endpoint; set JARVISE_NET_TESTS=1",
)
def test_backfill_pages_past_the_single_request_cap(tmp_path: Path):
    db = tmp_path / "net.db"

    code = run(
        [
            *BACKFILL_BASE,
            "--timeframe",
            "4h",
            "--since",
            "2025-01-01",
            "--json",
            "--db",
            str(db),
        ]
    )

    assert code == 0
    conn = sqlite3.connect(db)
    stored = conn.execute("SELECT COUNT(*) FROM market_technicals").fetchone()[0]
    conn.close()
    assert stored > 1000


def test_dry_run_does_not_create_db(tmp_path: Path):
    db = tmp_path / "missing" / "jarvise.db"
    code = run(
        [
            "--symbol",
            "BTCUSDT",
            "--skip-derivatives",
            "--dry-run",
            "--json",
            "--db",
            str(db),
        ]
    )
    assert code == 0
    assert not db.exists()
