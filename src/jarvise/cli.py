from __future__ import annotations

import json
import os
import sys
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from jarvise import __version__
from jarvise import config as store
from jarvise.charts import analytics_db_path, export_candle_graphs
from jarvise_assist.explore import run_explore_cli
from jarvise_exchange.cli import exchange_app
from jarvise_ingest.db_cli import db_app
from jarvise_ingest.universe_cli import universe_app
from jarvise_notify.weekly import build_weekly_report
from jarvise_trade.cli import trade_app

ROOT_EPILOG = """Examples:
  jarvise status
  jarvise init --yes
  jarvise config --help
  jarvise ingest --symbol BTCUSDT --skip-derivatives --dry-run --json
  jarvise analyze --symbol BTCUSDT --timeframe 4h --json
  jarvise paper run --symbol BTCUSDT --timeframe 4h --json
  jarvise paper status --json
  jarvise exchange sync-balances --json
  jarvise trade reconcile --dry-run --json
  jarvise db status --json
  jarvise universe show --json
  jarvise db prune --older-than 180d --dry-run --json
  jarvise charts export --symbol BTCUSDT --timeframe 4h --output json
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
app.add_typer(exchange_app, name="exchange")
app.add_typer(trade_app, name="trade")
app.add_typer(db_app, name="db")
app.add_typer(universe_app, name="universe")

charts_app = typer.Typer(
    name="charts",
    help="Draw SVG snapshots from stored candles (not a price feed).",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)
app.add_typer(charts_app, name="charts")

context_app = typer.Typer(
    name="context",
    help="Research context that does not change a trade signal.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)
app.add_typer(context_app, name="context")


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


def fail(message: str, *examples: str) -> NoReturn:
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


@context_app.command("news-reaction")
def context_news_reaction(
    output: Annotated[
        OutputFormat,
        typer.Option("--output", help="text prints the context lines; json prints the report."),
    ] = OutputFormat.text,
) -> None:
    """Compare watchlist headlines and saved digests with the next daily closes.

    Writes data/research/news-reaction/latest.json. Does not change approvals.
    """
    from jarvise_context.news_sources import default_report_path, run_news_reaction
    from jarvise_ingest.db import open_db

    db_path = analytics_db_path()
    conn = open_db(db_path) if db_path.is_file() else None
    try:
        report = run_news_reaction(conn)
    finally:
        if conn is not None:
            conn.close()
    path = default_report_path()
    if output is OutputFormat.json:
        typer.echo(json.dumps(report, ensure_ascii=False))
        return
    for line in report.get("lines") or []:
        typer.echo(line)
    if report.get("digest_note"):
        typer.echo(report["digest_note"])
    for err in report.get("errors") or []:
        typer.echo(f"source: {err}")
    typer.echo(f"report: {path}")


@charts_app.command("export")
def charts_export(
    symbol: Annotated[str, typer.Option("--symbol", help="Comma-separated symbols.")] = "BTCUSDT,ETHUSDT",
    timeframe: Annotated[str, typer.Option("--timeframe", help="Comma-separated timeframes.")] = "1h,4h,1d",
    output: OutputOpt = OutputFormat.text,
) -> None:
    """Write candle SVGs from the analytics database. Does not fetch prices."""
    result = export_candle_graphs(
        analytics_db_path(),
        [part.strip() for part in symbol.split(",") if part.strip()],
        [part.strip() for part in timeframe.split(",") if part.strip()],
    )
    emit(result, output)
    if not result.get("ok"):
        raise typer.Exit(code=2)


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
    key: Annotated[str | None, typer.Option("--key", help="Config key to read.")] = None,
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
    key: Annotated[str | None, typer.Option("--key", help="Config key to write.")] = None,
    value: Annotated[str | None, typer.Option("--value", help="Value to store.")] = None,
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


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    add_help_option=False,
)
def analyze(ctx: typer.Context) -> None:
    """Paper analyzer: regime / confidence / invalidation / size (no orders)."""
    from jarvise_analyze.cli import main as analyze_main

    code = analyze_main(ctx.args)
    raise typer.Exit(code if isinstance(code, int) else 0)


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    add_help_option=False,
)
def paper(ctx: typer.Context) -> None:
    """Paper auto-trade ledger: simulated fills only (no exchange orders)."""
    from jarvise_paper.cli import main as paper_main

    code = paper_main(ctx.args)
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
    query: Annotated[str | None, typer.Option("--query", help="Smoke-search after index.")] = None,
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


assist_app = typer.Typer(
    name="assist",
    help="Trade assistant. Advice only; never places orders.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)
app.add_typer(assist_app, name="assist")


@assist_app.command("explore")
def assist_explore(
    json_output: Annotated[bool, typer.Option("--json", help="Print the job JSON.")] = False,
) -> None:
    """Refresh explore candles and analysis. Sends nothing and places no orders."""
    raise typer.Exit(run_explore_cli(json_output=json_output))


@assist_app.command("real-report")
def assist_real_report(
    since_ms: Annotated[
        int | None,
        typer.Option("--since-ms", help="Include fills at or after this Unix ms (UTC)."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print the report JSON.")] = False,
    do_sync: Annotated[
        bool,
        typer.Option("--sync", help="Write assist_real_trade (deployed jobs path only)."),
    ] = False,
) -> None:
    """Print the real-vs-virtual one-tap report. Read key only; no orders.

    Default opens the DB read-only and never writes. ``--sync`` migrates and
    rebuilds ``assist_real_trade`` (for weekly/jobs after deploy).
    """
    from jarvise_assist.futures import build_futures_client
    from jarvise_assist.real_trades import SPEC_START_MS, run_real_report
    from jarvise_ingest.db import SCHEMA_VERSION, open_db
    from jarvise_ingest.universe_cli import connect_readonly

    db_path = analytics_db_path()
    if not db_path.is_file():
        typer.echo("analytics db missing", err=True)
        raise typer.Exit(code=2)
    client = build_futures_client()
    if client is None:
        typer.echo("no read-only Binance auth", err=True)
        raise typer.Exit(code=2)
    since = int(since_ms if since_ms is not None else SPEC_START_MS)
    if do_sync:
        conn = open_db(db_path)
        try:
            payload = run_real_report(conn, client, since_ms=since, sync=True)
        finally:
            conn.close()
    else:
        conn = connect_readonly(db_path)
        try:
            user_version = int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)
            if user_version != SCHEMA_VERSION:
                typer.echo(
                    f"schema mismatch: user_version={user_version} expected={SCHEMA_VERSION}",
                    err=True,
                )
                raise typer.Exit(code=2)
            payload = run_real_report(conn, client, since_ms=since, sync=False)
        finally:
            conn.close()
    if json_output:
        # Counts and bps only — no order ids or keys.
        out = {
            "ok": payload.get("ok"),
            "summary": payload.get("summary"),
            "lines": payload.get("lines"),
        }
        typer.echo(json.dumps(out, ensure_ascii=False))
    else:
        typer.echo(payload.get("text") or "")
    if payload.get("ok") is False:
        raise typer.Exit(code=1)


@assist_app.command("partner-audit")
def assist_partner_audit(
    days: Annotated[
        int,
        typer.Option("--days", help="Report window in days (1..90). Default 30."),
    ] = 30,
    no_binance: Annotated[
        bool,
        typer.Option("--no-binance", help="Skip Binance; verify_ledger only."),
    ] = False,
    csv_path: Annotated[
        Path | None,
        typer.Option("--csv", help="Export partner_event CSV to this new path."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
) -> None:
    """Reconcile futures transfers vs partner ledger. Read-only; never sends Telegram."""
    import sqlite3
    from decimal import Decimal

    from jarvise_assist.futures import build_futures_client
    from jarvise_assist.partner_audit import (
        CLI_DAYS_DEFAULT,
        MAX_DAYS,
        audit_report,
        export_events_csv,
        ledger_start_ms,
    )
    from jarvise_assist.partner_store import verify_ledger
    from jarvise_ingest.db import SCHEMA_VERSION
    from jarvise_ingest.universe_cli import connect_readonly

    if days < 1 or days > MAX_DAYS:
        typer.echo(f"--days ต้องอยู่ระหว่าง 1 และ {MAX_DAYS}", err=True)
        raise typer.Exit(code=2)
    use_days = int(days) if days else CLI_DAYS_DEFAULT

    db_path = analytics_db_path()
    if not db_path.is_file():
        typer.echo("ไม่พบฐานข้อมูล analytics", err=True)
        raise typer.Exit(code=2)

    if csv_path is not None and csv_path.exists():
        typer.echo("มีไฟล์อยู่แล้ว", err=True)
        raise typer.Exit(code=2)

    conn = connect_readonly(db_path)
    conn.row_factory = sqlite3.Row
    try:
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)
        if user_version != SCHEMA_VERSION:
            typer.echo(
                f"schema mismatch: user_version={user_version} expected={SCHEMA_VERSION}",
                err=True,
            )
            raise typer.Exit(code=2)

        if csv_path is not None:
            try:
                export_events_csv(conn, csv_path)
            except FileExistsError:
                typer.echo("มีไฟล์อยู่แล้ว", err=True)
                raise typer.Exit(code=2) from None

        verify = verify_ledger(conn)
        start = ledger_start_ms(conn)
        if start is None:
            if json_output:
                typer.echo(
                    json.dumps(
                        {"ok": True, "status": "not_init", "verify_ok": verify.get("ok")},
                        ensure_ascii=False,
                    )
                )
            else:
                typer.echo("ยังไม่ได้ตั้งต้น")
                typer.echo("verify_ledger: ผ่าน" if verify.get("ok") else "verify_ledger: ไม่ผ่าน")
            raise typer.Exit(code=0)

        if no_binance:
            if json_output:
                typer.echo(
                    json.dumps(
                        {
                            "ok": bool(verify.get("ok")),
                            "status": "no_binance",
                            "verify_ok": verify.get("ok"),
                            "mismatches": verify.get("mismatches") or [],
                        },
                        ensure_ascii=False,
                        default=str,
                    )
                )
            else:
                typer.echo("verify_ledger: ผ่าน" if verify.get("ok") else "verify_ledger: ไม่ผ่าน")
                typer.echo("ข้าม Binance (--no-binance)")
            raise typer.Exit(code=0 if verify.get("ok") else 1)

        client = build_futures_client()
        if client is None:
            typer.echo("ไม่มี read key — ยังไม่ได้ตรวจ Binance", err=True)
            raise typer.Exit(code=2)

        import time

        now_ms = int(time.time() * 1000)
        result = audit_report(conn, client, now_ms=now_ms, days=use_days)
        if result.status != "ok":
            err = result.error or ""
            typer.echo(
                f"ตรวจ Binance ไม่ได้ ({result.status}" + (f"/{err}" if err else "") + ") — ยังไม่มีข้อสรุป",
                err=True,
            )
            raise typer.Exit(code=2)

        alertable = [f for f in result.findings if f.alertable]
        exit_code = 0
        if not verify.get("ok") or alertable:
            exit_code = 1

        def _dec(v: Decimal) -> str:
            return str(v)

        payload = {
            "ok": exit_code == 0,
            "status": result.status,
            "verify_ok": bool(verify.get("ok")),
            "matched": result.matched_n,
            "pending": result.pending_n,
            "pre_init_ignored": result.pre_init_ignored_n,
            "binance_in": _dec(result.binance_in),
            "binance_out": _dec(result.binance_out),
            "ledger_in": _dec(result.ledger_in),
            "ledger_out": _dec(result.ledger_out),
            "findings": [
                {
                    "code": f.code,
                    "fingerprint": f.fingerprint,
                    "time_ms": f.time_ms,
                    "amount": _dec(f.amount),
                    "partner_name": f.partner_name,
                    "detail": f.detail,
                    "alertable": f.alertable,
                }
                for f in result.findings
            ],
        }
        if json_output:
            typer.echo(json.dumps(payload, ensure_ascii=False))
        else:
            typer.echo("verify_ledger: ผ่าน" if verify.get("ok") else "verify_ledger: ไม่ผ่าน")
            typer.echo(f"จับคู่ได้ {result.matched_n}")
            typer.echo(f"รอในระยะผ่อนผัน {result.pending_n}")
            typer.echo(f"Binance เข้า {_dec(result.binance_in)} / ออก {_dec(result.binance_out)}")
            typer.echo(f"สมุด เข้า {_dec(result.ledger_in)} / ออก {_dec(result.ledger_out)}")
            typer.echo(f"โอนก่อนตั้งต้น (ไม่นับ) {result.pre_init_ignored_n}")
            if result.findings:
                typer.echo("รายการที่พบ:")
                for f in result.findings:
                    flag = "เตือน" if f.alertable else "รอ"
                    name = f.partner_name or "-"
                    typer.echo(f"  [{flag}] {f.code} {name} {_dec(f.amount)} ({f.fingerprint})")
            else:
                typer.echo("ไม่พบรายการค้าง")
        raise typer.Exit(code=exit_code)
    finally:
        conn.close()


@assist_app.command("partner-record-owner-redeem-20261119")
def assist_partner_record_owner_redeem_20261119(
    db: Annotated[Path, typer.Option("--db", help="SQLite file. Required; no default and no env fallback.")],
    effective_at_ms: Annotated[
        int | None,
        typer.Option("--effective-at-ms", help="Effective time (ms). Default 2026-11-19 19:46 ICT."),
    ] = None,
    do_apply: Annotated[
        bool,
        typer.Option("--apply", help="Write the historical owner_redeem. Default is dry-run."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Print the preview or result as JSON.")] = False,
) -> None:
    """Record the 2026-11-19 150 USDT owner withdrawal. Dry-run unless ``--apply``."""
    import sqlite3

    from jarvise_assist.partner_history import (
        EFF_DEFAULT_MS,
        HistoricalRecordError,
        apply_historical,
        effective_in_window,
        preview_historical,
    )
    from jarvise_ingest.db import SCHEMA_VERSION, open_db
    from jarvise_ingest.universe_cli import connect_readonly

    when = EFF_DEFAULT_MS if effective_at_ms is None else int(effective_at_ms)
    if not effective_in_window(when):
        payload = {
            "ok": False,
            "reason": "effective_at_ms_out_of_window",
            "mode": "apply" if do_apply else "dry-run",
            "effective_at_ms": when,
        }
        if json_output:
            typer.echo(json.dumps(payload, ensure_ascii=False))
        else:
            typer.echo("effective_at_ms out of window", err=True)
        raise typer.Exit(code=2)
    if not db.is_file():
        typer.echo("analytics db missing", err=True)
        raise typer.Exit(code=2)

    def _echo(payload: dict[str, Any], code: int) -> None:
        if json_output:
            typer.echo(json.dumps(payload, ensure_ascii=False))
        elif payload.get("ok"):
            typer.echo(str(payload.get("reason") or "ok"))
        else:
            typer.echo(str(payload.get("reason") or "refused"), err=True)
        raise typer.Exit(code=code)

    probe = connect_readonly(db)
    probe.row_factory = sqlite3.Row
    try:
        user_version = int(probe.execute("PRAGMA user_version").fetchone()[0] or 0)
    finally:
        probe.close()
    if user_version != SCHEMA_VERSION:
        typer.echo(
            f"schema mismatch: user_version={user_version} expected={SCHEMA_VERSION}",
            err=True,
        )
        raise typer.Exit(code=2)

    if not do_apply:
        conn = connect_readonly(db)
        conn.row_factory = sqlite3.Row
        try:
            payload = preview_historical(conn, when)
        finally:
            conn.close()
        if payload.get("reason") == "conflict":
            _echo(payload, 3)
        _echo(payload, 0 if payload.get("ok") else 1)

    try:
        conn = open_db(db)
        try:
            try:
                payload = apply_historical(conn, when)
            except HistoricalRecordError as exc:
                payload = exc.payload
            except sqlite3.Error:
                payload = {"ok": False, "reason": "db_error", "mode": "apply"}
        finally:
            conn.close()
    except sqlite3.Error:
        payload = {"ok": False, "reason": "db_error", "mode": "apply"}
    if payload.get("reason") == "conflict":
        _echo(payload, 3)
    _echo(payload, 0 if payload.get("ok") else 1)


@assist_app.command("partner-record-owner-deposit-20261123")
def assist_partner_record_owner_deposit_20261123(
    db: Annotated[Path, typer.Option("--db", help="SQLite file. Required; no default and no env fallback.")],
    effective_at_ms: Annotated[
        int | None,
        typer.Option("--effective-at-ms", help="Effective time (ms). Default 2026-11-23 18:59:15 ICT."),
    ] = None,
    do_apply: Annotated[
        bool,
        typer.Option("--apply", help="Write the historical owner_deposit. Default is dry-run."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Print the preview or result as JSON.")] = False,
) -> None:
    """Record the 2026-11-23 588.525 USDT owner top-up. Dry-run unless ``--apply``."""
    import sqlite3

    from jarvise_assist.partner_history_20261123 import (
        EFF_DEFAULT_MS,
        HistoricalRecordError,
        apply_historical,
        effective_in_window,
        preview_historical,
    )
    from jarvise_ingest.db import SCHEMA_VERSION, open_db
    from jarvise_ingest.universe_cli import connect_readonly

    when = EFF_DEFAULT_MS if effective_at_ms is None else int(effective_at_ms)
    if not effective_in_window(when):
        payload = {
            "ok": False,
            "reason": "effective_at_ms_out_of_window",
            "mode": "apply" if do_apply else "dry-run",
            "effective_at_ms": when,
        }
        if json_output:
            typer.echo(json.dumps(payload, ensure_ascii=False))
        else:
            typer.echo("effective_at_ms out of window", err=True)
        raise typer.Exit(code=2)
    if not db.is_file():
        typer.echo("analytics db missing", err=True)
        raise typer.Exit(code=2)

    def _echo(payload: dict[str, Any], code: int) -> None:
        if json_output:
            typer.echo(json.dumps(payload, ensure_ascii=False))
        elif payload.get("ok"):
            typer.echo(str(payload.get("reason") or "ok"))
        else:
            typer.echo(str(payload.get("reason") or "refused"), err=True)
        raise typer.Exit(code=code)

    probe = connect_readonly(db)
    probe.row_factory = sqlite3.Row
    try:
        user_version = int(probe.execute("PRAGMA user_version").fetchone()[0] or 0)
    finally:
        probe.close()
    if user_version != SCHEMA_VERSION:
        typer.echo(
            f"schema mismatch: user_version={user_version} expected={SCHEMA_VERSION}",
            err=True,
        )
        raise typer.Exit(code=2)

    if not do_apply:
        conn = connect_readonly(db)
        conn.row_factory = sqlite3.Row
        try:
            payload = preview_historical(conn, when)
        finally:
            conn.close()
        if payload.get("reason") == "conflict":
            _echo(payload, 3)
        _echo(payload, 0 if payload.get("ok") else 1)

    try:
        conn = open_db(db)
        try:
            try:
                payload = apply_historical(conn, when)
            except HistoricalRecordError as exc:
                payload = exc.payload
            except sqlite3.Error:
                payload = {"ok": False, "reason": "db_error", "mode": "apply"}
        finally:
            conn.close()
    except sqlite3.Error:
        payload = {"ok": False, "reason": "db_error", "mode": "apply"}
    if payload.get("reason") == "conflict":
        _echo(payload, 3)
    _echo(payload, 0 if payload.get("ok") else 1)


@assist_app.command("partner-record-owner-tx")
def assist_partner_record_owner_tx(
    db: Annotated[Path, typer.Option("--db", help="SQLite file. Required; no default and no env fallback.")],
    kind: Annotated[str, typer.Option("--kind", help="dep (owner deposit) or wd (owner withdrawal).")],
    tran_id: Annotated[str, typer.Option("--tran-id", help="Binance transfer tranId / withdraw id.")],
    amount: Annotated[str, typer.Option("--amount", help="USDT amount as Binance shows it (8 dp ok).")],
    account_value: Annotated[
        str,
        typer.Option(
            "--account-value", help="Pool value V (USDT) right after the transfer, as p owner shows."
        ),
    ],
    effective_at_ms: Annotated[int, typer.Option("--effective-at-ms", help="Transfer time (epoch ms).")],
    do_apply: Annotated[
        bool,
        typer.Option("--apply", help="Write the owner_deposit/owner_redeem. Default is dry-run."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Print the preview or result as JSON.")] = False,
) -> None:
    """Record a past owner dep/wd the live ``p owner`` cannot book. Dry-run unless ``--apply``.

    Exit 0 ok, 1 refused, 2 bad input / db / schema, 3 conflict (tranId already booked).
    """
    import sqlite3
    import time

    from jarvise_assist.partner_owner_history import apply_owner_tx, preview_owner_tx, validate_args
    from jarvise_ingest.db import SCHEMA_VERSION, open_db
    from jarvise_ingest.universe_cli import connect_readonly

    mode = "apply" if do_apply else "dry-run"

    def _echo(payload: dict[str, Any], code: int) -> None:
        if json_output:
            typer.echo(json.dumps(payload, ensure_ascii=False))
        elif payload.get("ok"):
            typer.echo(str(payload.get("reason") or "ok"))
            for key, value in (payload.get("plan") or payload.get("result") or {}).items():
                typer.echo(f"  {key}: {value}")
        else:
            typer.echo(str(payload.get("reason") or "refused"), err=True)
        raise typer.Exit(code=code)

    def _code(payload: dict[str, Any]) -> int:
        if payload.get("ok"):
            return 0
        return 3 if payload.get("reason") == "conflict" else 1

    now_ms = int(time.time() * 1000)
    params, err = validate_args(
        side=str(kind).strip().lower(),
        tran_id=str(tran_id).strip(),
        amount=amount,
        account_value=account_value,
        effective_at_ms=int(effective_at_ms),
        now_ms=now_ms,
    )
    if err is not None or params is None:
        _echo({**(err or {"ok": False, "reason": "bad_args"}), "mode": mode}, 2)
        return
    if not db.is_file():
        typer.echo("analytics db missing", err=True)
        raise typer.Exit(code=2)
    probe = connect_readonly(db)
    try:
        user_version = int(probe.execute("PRAGMA user_version").fetchone()[0] or 0)
    finally:
        probe.close()
    if user_version != SCHEMA_VERSION:
        typer.echo(f"schema mismatch: user_version={user_version} expected={SCHEMA_VERSION}", err=True)
        raise typer.Exit(code=2)

    if not do_apply:
        conn = connect_readonly(db)
        conn.row_factory = sqlite3.Row
        try:
            payload = preview_owner_tx(conn, params, now_ms=now_ms)
        finally:
            conn.close()
        _echo(payload, _code(payload))
        return

    try:
        conn = open_db(db)
        try:
            payload = apply_owner_tx(conn, params, now_ms=now_ms)
        except sqlite3.Error:
            payload = {"ok": False, "reason": "db_error", "mode": "apply"}
        finally:
            conn.close()
    except sqlite3.Error:
        payload = {"ok": False, "reason": "db_error", "mode": "apply"}
    _echo(payload, _code(payload))


report_app = typer.Typer(help="Read-only owner reports. No orders.")
app.add_typer(report_app, name="report")


@report_app.command("weekly")
def report_weekly(
    json_output: Annotated[bool, typer.Option("--json", help="Print the report JSON.")] = False,
    send: Annotated[bool, typer.Option("--send", help="Send the report on Telegram.")] = False,
) -> None:
    """Build the weekly paper report. Telegram failures stay in the JSON."""
    payload = build_weekly_report(send=send)
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False))
    else:
        typer.echo(payload.get("text") or "")
    if payload.get("ok") is False:
        raise typer.Exit(code=1)
