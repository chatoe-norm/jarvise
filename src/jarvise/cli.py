from __future__ import annotations

import json
import os
import sys
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Optional

import typer

from jarvise import config as store
from jarvise import __version__

ROOT_EPILOG = """Examples:
  jarvise status
  jarvise init --yes
  jarvise config --help
  jarvise ingest --symbol BTCUSDT --skip-derivatives --dry-run --json
"""

STATUS_EPILOG = """Examples:
  jarvise status
  jarvise status --output json
  jarvise status --path ./project
"""

INIT_EPILOG = """Examples:
  jarvise init --yes
  jarvise init --path ./project --yes
  jarvise init --dry-run
"""

CONFIG_EPILOG = """Examples:
  jarvise config get --key name
  jarvise config set --key name --value jarvise
  cat config.json | jarvise config import --stdin
"""

CONFIG_GET_EPILOG = """Examples:
  jarvise config get --key name
  jarvise config get --key name --output json
"""

CONFIG_SET_EPILOG = """Examples:
  jarvise config set --key name --value jarvise
  jarvise config set --key name --value jarvise --dry-run
"""

CONFIG_IMPORT_EPILOG = """Examples:
  cat config.json | jarvise config import --stdin
  cat config.json | jarvise config import --stdin --dry-run
"""

app = typer.Typer(
    name="jarvise",
    help="Local workspace status and config.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
    epilog=ROOT_EPILOG,
)

config_app = typer.Typer(
    name="config",
    help="Get, set, or import workspace config.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
    epilog=CONFIG_EPILOG,
)
app.add_typer(config_app, name="config")

rag_app = typer.Typer(
    name="rag",
    help="Doctrine RAG: NotebookLM/OpenClaw/fetch/Firecrawl → Qdrant (paper only).",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)
app.add_typer(rag_app, name="rag")


class OutputFormat(str, Enum):
    text = "text"
    json = "json"


PathOpt = Annotated[
    Path,
    typer.Option("--path", help="Workspace root (default: current directory)."),
]
OutputOpt = Annotated[
    OutputFormat,
    typer.Option("--output", help="text or json."),
]
DryRunOpt = Annotated[
    bool,
    typer.Option("--dry-run", help="Preview the write without changing files."),
]


def fail(message: str, *examples: str) -> None:
    lines = [f"Error: {message}"]
    lines.extend(f"  {example}" for example in examples)
    typer.echo("\n".join(lines), err=True)
    raise typer.Exit(code=1)


def emit_text(fields: dict[str, Any]) -> None:
    for key, value in fields.items():
        typer.echo(f"{key}: {value}")


def emit(fields: dict[str, Any], output: OutputFormat) -> None:
    if output is OutputFormat.json:
        typer.echo(json.dumps(fields))
        return
    if output is OutputFormat.text:
        emit_text(fields)
        return
    raise AssertionError(f"unhandled output format: {output}")


def require_initialized(root: Path) -> None:
    if store.is_initialized(root):
        return
    fail(
        "Workspace is not initialized.",
        f"jarvise init --path {root} --yes",
        "jarvise init --yes",
    )


@app.command(epilog=STATUS_EPILOG)
def status(
    path: PathOpt = Path("."),
    output: OutputOpt = OutputFormat.text,
) -> None:
    """Show whether this workspace is initialized."""
    root = store.resolve_root(path)
    config_path = store.config_file(root)
    emit(
        {
            "initialized": store.is_initialized(root),
            "path": str(config_path),
            "python": sys.version.split()[0],
            "version": __version__,
        },
        output,
    )


@app.command(epilog=INIT_EPILOG)
def init(
    path: PathOpt = Path("."),
    dry_run: DryRunOpt = False,
    _yes: Annotated[
        bool,
        typer.Option("--yes", help="Do not prompt (agents should always pass this)."),
    ] = False,
) -> None:
    """Create local config. Safe to run twice."""
    root = store.resolve_root(path)
    config_path = store.config_file(root)
    if store.is_initialized(root):
        emit_text({"already initialized": True, "path": config_path})
        return
    if dry_run:
        emit_text({"dry-run": "would create", "path": config_path})
        return
    store.write_config(root, {})
    emit_text({"initialized": True, "path": config_path})


@config_app.command("get", epilog=CONFIG_GET_EPILOG)
def config_get(
    key: Annotated[Optional[str], typer.Option("--key", help="Config key to read.")] = None,
    path: PathOpt = Path("."),
    output: OutputOpt = OutputFormat.text,
) -> None:
    """Read one config value."""
    if key is None:
        fail("No key specified.", "jarvise config get --key <key>")
    root = store.resolve_root(path)
    require_initialized(root)
    data = store.read_config(root)
    if key not in data:
        fail(
            f"Key '{key}' not found.",
            "jarvise config get --key <key>",
            f"jarvise config set --key {key} --value <value>",
        )
    emit({"key": key, "value": data[key]}, output)


@config_app.command("set", epilog=CONFIG_SET_EPILOG)
def config_set(
    key: Annotated[Optional[str], typer.Option("--key", help="Config key to write.")] = None,
    value: Annotated[Optional[str], typer.Option("--value", help="Value to store.")] = None,
    path: PathOpt = Path("."),
    dry_run: DryRunOpt = False,
) -> None:
    """Write one config value. Same value twice is a no-op."""
    if key is None:
        fail(
            "No key specified.",
            "jarvise config set --key <key> --value <value>",
        )
    if value is None:
        fail(
            "No value specified.",
            f"jarvise config set --key {key} --value <value>",
        )
    root = store.resolve_root(path)
    require_initialized(root)
    data = store.read_config(root)
    if data.get(key) == value:
        emit_text({"already set": True, "key": key, "value": value})
        return
    if dry_run:
        emit_text({"dry-run": "would set", "key": key, "value": value})
        return
    data[key] = value
    store.write_config(root, data)
    emit_text({"key": key, "value": value, "path": store.config_file(root)})


@config_app.command("import", epilog=CONFIG_IMPORT_EPILOG)
def config_import(
    stdin: Annotated[
        bool,
        typer.Option("--stdin", help="Read a JSON object from stdin."),
    ] = False,
    path: PathOpt = Path("."),
    dry_run: DryRunOpt = False,
) -> None:
    """Merge a JSON object from stdin into config."""
    if not stdin:
        fail(
            "No stdin specified.",
            "cat config.json | jarvise config import --stdin",
        )
    raw = sys.stdin.read()
    try:
        incoming = json.loads(raw)
    except json.JSONDecodeError:
        fail(
            "Invalid JSON on stdin.",
            "cat config.json | jarvise config import --stdin",
        )
    if not isinstance(incoming, dict):
        fail(
            "Stdin must be a JSON object.",
            "cat config.json | jarvise config import --stdin",
        )
    root = store.resolve_root(path)
    require_initialized(root)
    data = store.read_config(root)
    merged = {**data, **incoming}
    keys = ", ".join(str(k) for k in incoming)
    if dry_run:
        emit_text({"dry-run": "would import", "keys": keys})
        return
    if merged == data:
        emit_text({"already imported": True, "keys": keys, "path": store.config_file(root)})
        return
    store.write_config(root, merged)
    emit_text({"imported": True, "keys": keys, "path": store.config_file(root)})


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    add_help_option=False,
)
def ingest(ctx: typer.Context) -> None:
    """Paper market ingest into SQLite (no order placement)."""
    from jarvise_ingest.cli import main as ingest_main

    code = ingest_main(ctx.args)
    raise typer.Exit(code if isinstance(code, int) else 0)


@rag_app.command("sync-notebook")
def rag_sync_notebook(
    dry_run: DryRunOpt = False,
    output: OutputOpt = OutputFormat.text,
) -> None:
    """Export NotebookLM sources into data/analytics/sources/notebook/."""
    from jarvise import rag as ragmod

    result = ragmod.sync_notebook(dry_run=dry_run)
    ragmod.publish_redis_status("jarvise:rag:notebook", result)
    emit(result, output)
    if not result.get("ok") and not dry_run:
        raise typer.Exit(code=2)


@rag_app.command("sync-openclaw")
def rag_sync_openclaw(
    dry_run: DryRunOpt = False,
    output: OutputOpt = OutputFormat.text,
) -> None:
    """Copy OpenClaw exports into data/analytics/sources/openclaw/ for doctrine RAG."""
    from jarvise import rag as ragmod

    result = ragmod.sync_openclaw(dry_run=dry_run)
    ragmod.publish_redis_status("jarvise:rag:openclaw", result)
    emit(result, output)
    if not result.get("ok") and not dry_run:
        raise typer.Exit(code=2)


@rag_app.command("ingest-sources")
def rag_ingest_sources(
    dry_run: DryRunOpt = False,
    skip_fetch: Annotated[bool, typer.Option("--skip-fetch")] = False,
    skip_firecrawl: Annotated[bool, typer.Option("--skip-firecrawl")] = False,
    output: OutputOpt = OutputFormat.text,
) -> None:
    """Fetch + Firecrawl allowlisted URLs from config/rag-sources.json."""
    from jarvise import rag as ragmod

    payload: dict[str, Any] = {"paper_only": True}
    if not skip_fetch:
        payload["fetch"] = ragmod.ingest_fetch(dry_run=dry_run)
    if not skip_firecrawl:
        payload["firecrawl"] = ragmod.ingest_firecrawl(dry_run=dry_run)
    ok = True
    for key in ("fetch", "firecrawl"):
        part = payload.get(key)
        if isinstance(part, dict) and part.get("ok") is False:
            ok = False
    payload["ok"] = ok
    ragmod.publish_redis_status("jarvise:rag:ingest_sources", payload)
    emit(payload, output)
    if not ok and not dry_run:
        raise typer.Exit(code=2)


@rag_app.command("index")
def rag_index(
    query: Annotated[Optional[str], typer.Option("--query", help="Smoke-search after index.")] = None,
    skip_index: Annotated[bool, typer.Option("--skip-index")] = False,
    output: OutputOpt = OutputFormat.text,
) -> None:
    """Index local doctrine/source files into Qdrant collection jarvise_doctrine."""
    from jarvise import rag as ragmod

    result = ragmod.index_sources(query=query, skip_index=skip_index)
    ragmod.publish_redis_status("jarvise:rag:last", {**result, "at": ragmod.utc_now_iso()})
    emit(result, output)
    if not result.get("ok"):
        raise typer.Exit(code=2)


@rag_app.command("refresh")
def rag_refresh(
    dry_run: DryRunOpt = False,
    output: OutputOpt = OutputFormat.text,
) -> None:
    """Full pipeline: notebook → fetch/firecrawl → openclaw → Qdrant index."""
    from jarvise import rag as ragmod

    if os.environ.get("REDIS_URL"):
        try:
            import redis

            if redis.Redis.from_url(os.environ["REDIS_URL"], decode_responses=True).get(
                "jarvise:kill_switch"
            ) in {"1", "true", "on", "yes"}:
                payload = {"ok": False, "skipped": True, "reason": "kill_switch engaged", "paper_only": True}
                emit(payload, output)
                raise typer.Exit(code=3)
        except ImportError:
            pass

    steps: dict[str, Any] = {"paper_only": True, "dry_run": dry_run}
    steps["notebook"] = ragmod.sync_notebook(dry_run=dry_run)
    steps["fetch"] = ragmod.ingest_fetch(dry_run=dry_run)
    steps["firecrawl"] = ragmod.ingest_firecrawl(dry_run=dry_run)
    steps["openclaw"] = ragmod.sync_openclaw(dry_run=dry_run)
    if dry_run:
        steps["index"] = {"ok": True, "dry_run": True}
        steps["ok"] = True
    else:
        steps["index"] = ragmod.index_sources()
        steps["ok"] = bool(steps["index"].get("ok"))
    ragmod.publish_redis_status("jarvise:rag:last", {**steps, "at": ragmod.utc_now_iso()})
    emit(steps, output)
    if not steps.get("ok"):
        raise typer.Exit(code=2)
