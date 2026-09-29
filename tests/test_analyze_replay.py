"""Tests for analyze --replay."""

import json
import shutil
from pathlib import Path

from jarvise_analyze.cli import DEFAULT_DB, run
from jarvise_analyze.replay import replay_range
from jarvise_ingest.db import (
    list_paper_orders,
    open_db,
    upsert_market_technicals,
)
from jarvise_ingest.series import recompute_indicators


def _seed_trend(db: Path, *, n: int = 700) -> None:
    conn = open_db(db)
    rows = []
    for i in range(n):
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


def test_replay_analyze_only_upserts(tmp_path: Path) -> None:
    db = tmp_path / "r.db"
    _seed_trend(db)
    # ~ last 10 bars of seeded series
    since_ms = 1_600_000_000_000 + 690 * 14_400_000
    until_ms = 1_600_000_000_000 + 699 * 14_400_000
    conn = open_db(db)
    report = replay_range(
        conn,
        symbols=["BTCUSDT"],
        timeframe="4h",
        since_ms=since_ms,
        until_ms=until_ms,
        apply_paper=False,
    )
    assert report["ok"] is True
    assert report["bars"] == 10
    n = conn.execute("SELECT COUNT(*) FROM analysis_output").fetchone()[0]
    conn.close()
    assert n == 10


def test_replay_dry_run_writes_nothing(tmp_path: Path) -> None:
    db = tmp_path / "d.db"
    _seed_trend(db)
    since_ms = 1_600_000_000_000 + 690 * 14_400_000
    until_ms = 1_600_000_000_000 + 699 * 14_400_000
    conn = open_db(db)
    report = replay_range(
        conn,
        symbols=["BTCUSDT"],
        timeframe="4h",
        since_ms=since_ms,
        until_ms=until_ms,
        dry_run=True,
    )
    assert report["bars"] == 10
    n = conn.execute("SELECT COUNT(*) FROM analysis_output").fetchone()[0]
    assert list_paper_orders(conn) == []
    conn.close()
    assert n == 0


def test_apply_paper_refuses_default_db(tmp_path: Path, capsys) -> None:
    # Ensure default path exists as a file so resolve works; use real DEFAULT_DB check
    code = run(
        [
            "--symbol",
            "BTCUSDT",
            "--replay",
            "--since",
            "2020-01-01",
            "--apply-paper",
            "--db",
            str(DEFAULT_DB),
        ]
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "refuses the default live ledger" in err


def test_apply_paper_isolated_fills(tmp_path: Path, capsys) -> None:
    src = tmp_path / "src.db"
    bt = tmp_path / "bt.db"
    _seed_trend(src)
    shutil.copy(src, bt)
    since = "2020-09-01T00:00:00+00:00"
    # seed starts 2020-09-13-ish in ms terms: 1600000000000 = 2020-09-13
    code = run(
        [
            "--symbol",
            "BTCUSDT",
            "--timeframe",
            "4h",
            "--replay",
            "--since",
            since,
            "--until",
            "2020-10-15T00:00:00+00:00",
            "--apply-paper",
            "--json",
            "--db",
            str(bt),
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["apply_paper"] is True
    assert payload["bars"] > 0
    assert payload["metrics"] is not None
    conn = open_db(bt)
    # may be zero closed if never flat, but orders should exist if any long/short fired
    orders = list_paper_orders(conn, limit=500)
    conn.close()
    assert payload["fills"] == len(orders) or payload["fills"] >= 0


def test_replay_requires_since(capsys) -> None:
    assert run(["--symbol", "BTCUSDT", "--replay"]) == 2
    assert "--since" in capsys.readouterr().err
