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


def test_doctrine_allowlist_is_trading_only() -> None:
    # jarvise_doctrine indexes preferred protocol extracts + openclaw — not UI / Learn / scrapes.
    cfg = json.loads((ROOT / "config" / "rag-sources.json").read_text(encoding="utf-8"))
    assert cfg["fetch"] == []
    assert cfg["firecrawl"] == []
    assert cfg["notebook"]["alias"] == "jarvise"
    assert "PREFERRED" in cfg["note"] or "protocol" in cfg["note"].lower()
    # If fetch/firecrawl are re-enabled later, keep markdown/.txt (SPA HTML shells are empty).
    for section in ("fetch", "firecrawl"):
        for entry in cfg.get(section) or []:
            url = entry["url"]
            path = urlparse(url).path
            assert path.endswith((".md", ".txt")) or urlparse(url).hostname != "developers.binance.com", url


def test_is_indexable_doctrine_path_filters_scrapes(tmp_path: Path) -> None:
    sources = tmp_path / "data" / "analytics" / "sources"
    keep = [
        sources / "jarvise-doctrine.txt",
        sources / "jarvise-analyzer-stack.txt",
        sources / "binance-api-intro-jarvise.txt",
        sources / "notebook" / "jarvise-autonomous-trader-core-protocol-2f474ccb.md",
        sources / "notebook" / "jarvise-crypto-trader-2-37169594.md",
        sources / "openclaw" / "2026-09-22-btc-ops-verify.md",
    ]
    drop = [
        sources / "notebook" / "what-is-macd-deebae93.md",
        sources / "notebook" / "90-warren-buffett-quotes-on-investing-business-and-life-7f3a6fa7.md",
        sources / "notebook" / "catalog.md",
        tmp_path / "data" / "analytics" / "stack.md",
    ]
    for path in keep + drop:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("body", encoding="utf-8")
    for path in keep:
        assert rag.is_indexable_doctrine_path(path), path
    for path in drop:
        assert not rag.is_indexable_doctrine_path(path), path


def test_collect_source_files_skips_notebook_scrapes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JARVISE_ROOT", str(tmp_path))
    sources = tmp_path / "data" / "analytics" / "sources"
    (sources / "jarvise-doctrine.txt").parent.mkdir(parents=True, exist_ok=True)
    (sources / "jarvise-doctrine.txt").write_text("owner", encoding="utf-8")
    (sources / "notebook").mkdir(parents=True, exist_ok=True)
    (sources / "notebook" / "what-is-macd.md").write_text("scrape", encoding="utf-8")
    (sources / "notebook" / "jarvise-doctrine-expectancy.md").write_text("protocol", encoding="utf-8")
    (sources / "openclaw").mkdir(parents=True, exist_ok=True)
    (sources / "openclaw" / "note.md").write_text("ops", encoding="utf-8")
    (tmp_path / "data" / "analytics" / "stack.md").write_text("stack", encoding="utf-8")
    names = sorted(p.name for p in rag.collect_source_files(tmp_path))
    assert names == [
        "jarvise-doctrine-expectancy.md",
        "jarvise-doctrine.txt",
        "note.md",
    ]


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
