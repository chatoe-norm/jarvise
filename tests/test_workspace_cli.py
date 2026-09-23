import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jarvise.cli import app

runner = CliRunner()


def invoke(*args: str, input: str | None = None):
    return runner.invoke(app, list(args), input=input)


def combined(result) -> str:
    return f"{result.stdout}{result.stderr}"


@pytest.mark.parametrize(
    "args",
    [
        ["--help"],
        ["status", "--help"],
        ["init", "--help"],
        ["config", "--help"],
        ["config", "get", "--help"],
        ["config", "set", "--help"],
        ["config", "import", "--help"],
        ["ingest", "--help"],
    ],
)
def test_help_includes_examples(args: list[str]) -> None:
    result = invoke(*args)
    assert result.exit_code == 0
    assert "Examples:" in result.stdout


def test_root_help_stays_layered() -> None:
    result = invoke("--help")
    assert result.exit_code == 0
    assert "status" in result.stdout
    assert "init" in result.stdout
    assert "ingest" in result.stdout
    assert "config" in result.stdout
    assert "--value" not in result.stdout


def test_config_get_without_key_fails_with_example() -> None:
    result = invoke("config", "get")
    assert result.exit_code == 1
    assert "Error: No key specified." in combined(result)
    assert "jarvise config get --key <key>" in combined(result)


def test_config_set_without_key_fails_with_example() -> None:
    result = invoke("config", "set")
    assert result.exit_code == 1
    assert "Error: No key specified." in combined(result)
    assert "jarvise config set --key <key> --value <value>" in combined(result)


def test_config_set_without_value_fails_with_example() -> None:
    result = invoke("config", "set", "--key", "name")
    assert result.exit_code == 1
    assert "Error: No value specified." in combined(result)
    assert "jarvise config set --key name --value <value>" in combined(result)


def test_config_import_without_stdin_fails_with_example() -> None:
    result = invoke("config", "import")
    assert result.exit_code == 1
    assert "Error: No stdin specified." in combined(result)
    assert "jarvise config import --stdin" in combined(result)


def test_status_json_when_uninitialized(tmp_path: Path) -> None:
    result = invoke("status", "--path", str(tmp_path), "--output", "json")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["initialized"] is False
    assert payload["path"].endswith(".jarvise/config.json") or payload["path"].endswith(
        ".jarvise\\config.json"
    )
    assert "python" in payload


def test_init_is_idempotent(tmp_path: Path) -> None:
    first = invoke("init", "--path", str(tmp_path), "--yes")
    assert first.exit_code == 0
    config_file = tmp_path / ".jarvise" / "config.json"
    assert config_file.is_file()
    assert json.loads(config_file.read_text(encoding="utf-8")) == {}

    second = invoke("init", "--path", str(tmp_path), "--yes")
    assert second.exit_code == 0
    assert "already initialized" in second.stdout
    assert json.loads(config_file.read_text(encoding="utf-8")) == {}


def test_init_dry_run_does_not_write(tmp_path: Path) -> None:
    result = invoke("init", "--path", str(tmp_path), "--dry-run", "--yes")
    assert result.exit_code == 0
    assert "dry-run" in result.stdout
    assert not (tmp_path / ".jarvise").exists()


def test_config_set_and_get(tmp_path: Path) -> None:
    invoke("init", "--path", str(tmp_path), "--yes")
    set_result = invoke(
        "config",
        "set",
        "--path",
        str(tmp_path),
        "--key",
        "name",
        "--value",
        "jarvise",
    )
    assert set_result.exit_code == 0
    assert "key: name" in set_result.stdout
    assert "value: jarvise" in set_result.stdout

    get_result = invoke(
        "config",
        "get",
        "--path",
        str(tmp_path),
        "--key",
        "name",
        "--output",
        "json",
    )
    assert get_result.exit_code == 0
    assert json.loads(get_result.stdout) == {"key": "name", "value": "jarvise"}


def test_config_set_same_value_is_noop(tmp_path: Path) -> None:
    invoke("init", "--path", str(tmp_path), "--yes")
    invoke(
        "config",
        "set",
        "--path",
        str(tmp_path),
        "--key",
        "name",
        "--value",
        "jarvise",
    )
    config_file = tmp_path / ".jarvise" / "config.json"
    before = config_file.read_text(encoding="utf-8")

    result = invoke(
        "config",
        "set",
        "--path",
        str(tmp_path),
        "--key",
        "name",
        "--value",
        "jarvise",
    )
    assert result.exit_code == 0
    assert "already set" in result.stdout
    assert config_file.read_text(encoding="utf-8") == before


def test_config_set_dry_run_does_not_write(tmp_path: Path) -> None:
    invoke("init", "--path", str(tmp_path), "--yes")
    result = invoke(
        "config",
        "set",
        "--path",
        str(tmp_path),
        "--key",
        "name",
        "--value",
        "jarvise",
        "--dry-run",
    )
    assert result.exit_code == 0
    assert "dry-run" in result.stdout
    assert json.loads((tmp_path / ".jarvise" / "config.json").read_text(encoding="utf-8")) == {}


def test_config_get_unknown_key_fails_with_example(tmp_path: Path) -> None:
    invoke("init", "--path", str(tmp_path), "--yes")
    result = invoke("config", "get", "--path", str(tmp_path), "--key", "missing")
    assert result.exit_code == 1
    assert "Error: Key 'missing' not found." in combined(result)
    assert "jarvise config set --key missing --value <value>" in combined(result)


def test_config_import_stdin(tmp_path: Path) -> None:
    invoke("init", "--path", str(tmp_path), "--yes")
    result = invoke(
        "config",
        "import",
        "--path",
        str(tmp_path),
        "--stdin",
        input='{"name": "jarvise", "env": "local"}\n',
    )
    assert result.exit_code == 0
    data = json.loads((tmp_path / ".jarvise" / "config.json").read_text(encoding="utf-8"))
    assert data == {"name": "jarvise", "env": "local"}


def test_config_import_dry_run_does_not_write(tmp_path: Path) -> None:
    invoke("init", "--path", str(tmp_path), "--yes")
    result = invoke(
        "config",
        "import",
        "--path",
        str(tmp_path),
        "--stdin",
        "--dry-run",
        input='{"name": "jarvise"}\n',
    )
    assert result.exit_code == 0
    assert "dry-run" in result.stdout
    assert json.loads((tmp_path / ".jarvise" / "config.json").read_text(encoding="utf-8")) == {}


def test_ingest_missing_symbol_exit_2() -> None:
    result = invoke("ingest")
    assert result.exit_code == 2
    assert "Error: --symbol or --universe is required." in combined(result)


def test_ingest_dry_run_delegates(tmp_path: Path) -> None:
    db = tmp_path / "missing" / "jarvise.db"
    result = invoke(
        "ingest",
        "--symbol",
        "BTCUSDT",
        "--skip-derivatives",
        "--dry-run",
        "--json",
        "--db",
        str(db),
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert not db.exists()


def test_config_set_requires_init(tmp_path: Path) -> None:
    result = invoke(
        "config",
        "set",
        "--path",
        str(tmp_path),
        "--key",
        "name",
        "--value",
        "jarvise",
    )
    assert result.exit_code == 1
    assert "Error: Workspace is not initialized." in combined(result)
    assert "jarvise init" in combined(result)
