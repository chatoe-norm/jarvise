"""jarvise universe show is read-only."""

import json
from pathlib import Path

from typer.testing import CliRunner

from jarvise.cli import app
from jarvise_ingest.db import add_universe_symbol, open_db, remove_universe_symbol, universe_as_of
from jarvise_ingest.universe import seed_named_universe
from jarvise_ingest.universe_cli import connect_readonly

runner = CliRunner()


def test_show_matches_universe_as_of_and_does_not_write(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "u.db"
    conn = open_db(db)
    seed_named_universe(conn, "paper_liquid")
    changed_at = 1_700_000_000_000
    assert add_universe_symbol(conn, universe_id="paper_liquid", symbol="ADAUSDT", now_ms=changed_at)
    assert remove_universe_symbol(conn, universe_id="paper_liquid", symbol="BNBUSDT", now_ms=changed_at)
    conn.close()
    before = db.read_bytes()
    monkeypatch.setenv("JARVISE_PAPER_UNIVERSE", "paper_liquid")
    monkeypatch.setenv("JARVISE_PAPER_TIMEFRAME", "4h")
    result = runner.invoke(app, ["universe", "show", "--json", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert db.read_bytes() == before
    payload = json.loads(result.stdout)
    ro = connect_readonly(db)
    try:
        expected = universe_as_of(ro, payload["universe_id"], int(payload["as_of_ms"]))
    finally:
        ro.close()
    assert payload["active_symbols"] == expected
    assert "ADAUSDT" in payload["active_symbols"]
    assert "BNBUSDT" not in payload["active_symbols"]
    assert [symbol for symbol, _asset in payload["preset_members"]] == [
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT",
        "BNBUSDT",
    ]
    assert payload["timeframe"] == "4h"
    assert payload["db"] == str(db)
    assert db.read_bytes() == before


def test_show_empty_universe_does_not_seed(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "empty.db"
    conn = open_db(db)
    conn.close()
    before = db.read_bytes()
    monkeypatch.setenv("JARVISE_PAPER_UNIVERSE", "paper_liquid")
    result = runner.invoke(app, ["universe", "show", "--json", "--db", str(db)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["active_symbols"] == []
    assert "hint" in payload
    assert "does not seed" in payload["hint"]
    assert db.read_bytes() == before


def test_show_missing_db_exits_1(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"
    result = runner.invoke(app, ["universe", "show", "--db", str(missing)])
    assert result.exit_code == 1
    assert "not found" in result.stderr


def test_show_usage_error_exits_2() -> None:
    result = runner.invoke(app, ["universe", "show", "--not-a-flag"])
    assert result.exit_code == 2
