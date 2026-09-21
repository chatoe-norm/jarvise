from __future__ import annotations

import json
from pathlib import Path

from jarvise import rag


def test_chunk_text_overlap() -> None:
    chunks = rag.chunk_text("word " * 200, size=40, overlap=10)
    assert len(chunks) > 1
    assert all(chunks)


def test_slugify() -> None:
    assert rag.slugify("Hello World!") == "hello-world"


def test_ingest_fetch_dry_run(tmp_path: Path, monkeypatch) -> None:
    cfg = {
        "fetch": [{"id": "example", "url": "https://example.com"}],
        "firecrawl": [],
        "notebook": {},
    }
    cfg_path = tmp_path / "rag-sources.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("JARVISE_ROOT", str(tmp_path))
    monkeypatch.setattr(rag, "DEFAULT_SOURCES_CONFIG", Path("rag-sources.json"))
    # load_sources_config uses repo_root / DEFAULT_SOURCES_CONFIG — put file at root
    result = rag.ingest_fetch(dry_run=True)
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["count"] == 1


def test_sync_notebook_dry_run(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("JARVISE_ROOT", str(tmp_path))
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "rag-sources.json").write_text(
        json.dumps({"notebook": {"alias": "jarvise", "nlm_profile": "chatoe"}}),
        encoding="utf-8",
    )
    result = rag.sync_notebook(dry_run=True)
    assert result["dry_run"] is True
    assert result["alias"] == "jarvise"


def test_rag_refresh_cli_dry_run() -> None:
    from typer.testing import CliRunner

    from jarvise.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["rag", "refresh", "--dry-run", "--output", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert payload["paper_only"] is True
