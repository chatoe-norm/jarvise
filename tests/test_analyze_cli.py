"""CLI tests for jarvise analyze."""

import json
from pathlib import Path

from jarvise_analyze.cli import run
from jarvise_ingest.db import open_db, upsert_market_technicals
from jarvise_ingest.series import recompute_indicators


def _seed_trend(db: Path) -> None:
    conn = open_db(db)
    rows = []
    for i in range(700):
        close = 100.0 + i * 0.5
        rows.append(
            {
                "symbol": "BTCUSDT",
                "timestamp": 1_600_000_000_000 + i * 14_400_000,
                "timeframe": "4h",
                "open": close - 0.2,
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "volume": 10.0,
            }
        )
    upsert_market_technicals(conn, rows)
    recompute_indicators(conn, "BTCUSDT", "4h")
    conn.close()


def test_analyze_missing_symbol_exit_2():
    assert run([]) == 2


def test_analyze_dry_run_writes_nothing(tmp_path: Path, capsys):
    db = tmp_path / "a.db"
    _seed_trend(db)

    code = run(
        [
            "--symbol",
            "BTCUSDT",
            "--timeframe",
            "4h",
            "--dry-run",
            "--json",
            "--db",
            str(db),
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["analyses"][0]["action"] in {"long", "flat"}
    conn = open_db(db)
    n = conn.execute("SELECT COUNT(*) FROM analysis_output").fetchone()[0]
    conn.close()
    assert n == 0


def test_analyze_persists_analysis_output(tmp_path: Path, capsys):
    db = tmp_path / "a.db"
    _seed_trend(db)

    code = run(
        ["--symbol", "BTCUSDT", "--timeframe", "4h", "--json", "--db", str(db)]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    analysis_id = payload["analyses"][0]["analysis_id"]

    conn = open_db(db)
    row = conn.execute(
        "SELECT action, regime_state, timeframe FROM analysis_output WHERE analysis_id=?",
        (analysis_id,),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == payload["analyses"][0]["action"]
    assert row[2] == "4h"
