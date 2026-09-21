from pathlib import Path

from jarvise_ingest.cli import run


def test_missing_symbol_exit_2():
    assert run([]) == 2


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
