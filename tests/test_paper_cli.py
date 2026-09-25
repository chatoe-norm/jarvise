"""CLI tests for jarvise paper."""

import json
from pathlib import Path

from jarvise_analyze.engine import analyze_snapshot
from jarvise_ingest.db import (
    list_approvals,
    list_paper_orders,
    load_latest_candle,
    open_db,
    upsert_analysis_output,
    upsert_market_technicals,
)
from jarvise_ingest.series import recompute_indicators
from jarvise_paper.cli import main, run


def _seed(db: Path, *, symbol: str = "BTCUSDT", timeframe: str = "4h") -> None:
    conn = open_db(db)
    rows = []
    for i in range(700):
        close = 100.0 + i * 0.5
        rows.append(
            {
                "symbol": symbol,
                "timestamp": 1_600_000_000_000 + i * 14_400_000,
                "timeframe": timeframe,
                "open": close - 0.2,
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "volume": 10.0,
            }
        )
    upsert_market_technicals(conn, rows)
    recompute_indicators(conn, symbol, timeframe)
    conn.close()


def _seed_analysis(db: Path, *, symbol: str = "BTCUSDT", timeframe: str = "4h") -> None:
    conn = open_db(db)
    candle = load_latest_candle(conn, symbol, timeframe)
    assert candle is not None
    analysis = analyze_snapshot(candle)
    upsert_analysis_output(conn, analysis)
    conn.close()


def _seed_long_analysis(db: Path, *, symbol: str = "BTCUSDT", timeframe: str = "4h") -> None:
    conn = open_db(db)
    candle = load_latest_candle(conn, symbol, timeframe)
    assert candle is not None
    upsert_analysis_output(
        conn,
        {
            "analysis_id": "cli-long-seed",
            "timestamp": int(candle["timestamp"]),
            "symbol": symbol,
            "timeframe": timeframe,
            "regime_state": "trend_up",
            "confidence_score": 0.9,
            "action": "long",
            "invalidation_price": None,
            "size_pct_equity": 10.0,
            "thesis": "cli test",
        },
    )
    conn.close()


def test_paper_run_requires_symbol():
    assert run(["run"]) == 2


def test_paper_run_dry_run(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    db = tmp_path / "a.db"
    _seed(db)
    code = run(
        [
            "run",
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
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert payload["results"]


def test_paper_run_and_status(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    db = tmp_path / "a.db"
    _seed(db)
    code = run(
        ["run", "--symbol", "BTCUSDT", "--timeframe", "4h", "--json", "--db", str(db)]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["paper_only"] is True

    code = run(["status", "--json", "--db", str(db)])
    assert code == 0
    status = json.loads(capsys.readouterr().out)
    assert status["account"]["starting_equity"] == 10_000.0
    assert "equity" in status["account"]


def test_paper_run_kill_switch(tmp_path: Path, capsys, monkeypatch):
    db = tmp_path / "a.db"
    _seed(db)
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")

    class FakeRedis:
        def get(self, key: str):
            return "1"

    class FakeMod:
        @staticmethod
        def from_url(*_a, **_k):
            return FakeRedis()

    monkeypatch.setitem(__import__("sys").modules, "redis", FakeMod)
    # jarvise_paper.cli imports redis inside kill_switch_engaged
    import jarvise_paper.cli as paper_cli

    monkeypatch.setattr(
        paper_cli,
        "kill_switch_engaged",
        lambda: True,
    )
    code = run(
        ["run", "--symbol", "BTCUSDT", "--timeframe", "4h", "--json", "--db", str(db)]
    )
    assert code == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["skipped"] is True


def test_paper_run_enqueues_without_fill(tmp_path, monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    db = tmp_path / "cli.db"
    _seed(db)
    _seed_analysis(db)
    code = main(
        [
            "run",
            "--symbol",
            "BTCUSDT",
            "--timeframe",
            "4h",
            "--db",
            str(db),
            "--json",
            "--skip-analyze",
        ]
    )
    assert code == 0
    conn = open_db(db)
    try:
        assert list_approvals(conn, status="pending")
        assert list_paper_orders(conn) == []
    finally:
        conn.close()


def test_paper_run_auto_fill_writes_orders(tmp_path, monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    db = tmp_path / "cli2.db"
    _seed(db)
    _seed_long_analysis(db)
    code = main(
        [
            "run",
            "--symbol",
            "BTCUSDT",
            "--timeframe",
            "4h",
            "--db",
            str(db),
            "--json",
            "--skip-analyze",
            "--auto-fill",
        ]
    )
    assert code == 0
    conn = open_db(db)
    try:
        assert list_paper_orders(conn)
    finally:
        conn.close()
