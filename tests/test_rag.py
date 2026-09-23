from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

from jarvise import rag

ROOT = Path(__file__).resolve().parents[1]


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


def test_binance_docs_fetch_urls_are_markdown() -> None:
    # developers.binance.com renders client-side; HTML URLs fetch an empty shell.
    cfg = json.loads((ROOT / "config" / "rag-sources.json").read_text(encoding="utf-8"))
    binance = [
        e["url"]
        for e in cfg["fetch"]
        if urlparse(e["url"]).hostname == "developers.binance.com"
    ]
    assert binance
    for url in binance:
        assert urlparse(url).path.endswith((".md", ".txt")), url


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


def test_sync_openclaw_dry_run(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JARVISE_ROOT", str(tmp_path))
    export = tmp_path / "data" / "openclaw" / "exports"
    export.mkdir(parents=True)
    (export / "btc-note.md").write_text("# BTC paper thesis\n\nHold bias.", encoding="utf-8")
    (export / "empty.md").write_text("  \n", encoding="utf-8")
    result = rag.sync_openclaw(dry_run=True)
    assert result["dry_run"] is True
    assert result["candidates"] == 2
    assert "btc-note.md" in result["would_copy"]
    assert not (tmp_path / "data" / "analytics" / "sources" / "openclaw").exists()


def test_sync_openclaw_copies_and_tags(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JARVISE_ROOT", str(tmp_path))
    export = tmp_path / "data" / "openclaw" / "exports"
    export.mkdir(parents=True)
    (export / "ETH Signal.md").write_text(
        "symbol: ETHUSDT\nthesis: range\npaper_only: true\n",
        encoding="utf-8",
    )
    (export / "skip-me.md").write_text("", encoding="utf-8")
    result = rag.sync_openclaw(dry_run=False)
    assert result["ok"] is True
    assert len(result["written"]) == 1
    assert result["skipped"] == ["skip-me.md"]
    out = tmp_path / "data" / "analytics" / "sources" / "openclaw" / "eth-signal.md"
    text = out.read_text(encoding="utf-8")
    assert "<!-- kind: openclaw -->" in text
    assert "<!-- paper_only: True -->" in text
    assert "ETHUSDT" in text


def test_index_kind_detects_openclaw(tmp_path: Path) -> None:
    path = tmp_path / "data" / "analytics" / "sources" / "openclaw" / "note.md"
    path.parent.mkdir(parents=True)
    path.write_text("hello", encoding="utf-8")
    kind = "doctrine"
    for part in rag.SOURCE_KINDS:
        if part in path.parts:
            kind = part
            break
    assert kind == "openclaw"


def test_rag_refresh_cli_dry_run() -> None:
    from typer.testing import CliRunner

    from jarvise.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["rag", "refresh", "--dry-run", "--output", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert payload["paper_only"] is True
    assert "openclaw" in payload
    assert payload["openclaw"].get("dry_run") is True


def test_rag_sync_openclaw_cli_dry_run(tmp_path: Path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from jarvise.cli import app

    monkeypatch.setenv("JARVISE_ROOT", str(tmp_path))
    (tmp_path / "data" / "openclaw" / "exports").mkdir(parents=True)
    runner = CliRunner()
    result = runner.invoke(app, ["rag", "sync-openclaw", "--dry-run", "--output", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert payload["paper_only"] is True
