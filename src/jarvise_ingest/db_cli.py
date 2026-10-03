"""Typer commands for SQLite housekeeping: schema status and retention prune."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from jarvise_ingest.db import SCHEMA_VERSION, db_pragmas, open_db
from jarvise_ingest.retention import parse_age, prune, table_counts

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = REPO_ROOT / "data" / "analytics" / "jarvise.db"

db_app = typer.Typer(
    name="db",
    help="SQLite housekeeping: schema version/pragmas and retention prune (paper/approval/live tables are never pruned).",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)


@db_app.command("status")
def status(
    db: Annotated[Path | None, typer.Option("--db")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show user_version, journal mode, and row counts. Opening also applies pending migrations."""
    path = db or DEFAULT_DB
    conn = open_db(path)
    try:
        payload = {
            "ok": True,
            "db": str(path),
            "schema_version_expected": SCHEMA_VERSION,
            **db_pragmas(conn),
            "tables": table_counts(conn),
        }
    finally:
        conn.close()
    if as_json:
        typer.echo(json.dumps(payload))
    else:
        typer.echo(
            f"db={path} user_version={payload['user_version']}/{SCHEMA_VERSION} "
            f"journal={payload['journal_mode']} busy_timeout_ms={payload['busy_timeout_ms']}"
        )
        for name, count in payload["tables"].items():
            typer.echo(f"  {name}: {count}")


@db_app.command("prune")
def prune_cmd(
    older_than: Annotated[str, typer.Option("--older-than", help="e.g. 180d, 12h")] = "180d",
    db: Annotated[Path | None, typer.Option("--db")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    vacuum: Annotated[bool, typer.Option("--vacuum", help="VACUUM after delete")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Delete stale order-book / exchange-balance rows and superseded derivatives versions."""
    try:
        age_ms = parse_age(older_than)
    except ValueError as exc:
        typer.echo(f"invalid --older-than: {exc}", err=True)
        raise typer.Exit(2) from exc
    path = db or DEFAULT_DB
    if not path.exists():
        payload = {"ok": True, "skipped": True, "reason": "no_database", "db": str(path)}
        typer.echo(json.dumps(payload) if as_json else "no database; nothing to prune")
        raise typer.Exit(0)
    conn = open_db(path)
    try:
        result = prune(conn, older_than_ms=age_ms, dry_run=dry_run, vacuum=vacuum)
    finally:
        conn.close()
    result["db"] = str(path)
    result["older_than"] = older_than
    if as_json:
        typer.echo(json.dumps(result))
    else:
        verb = "would delete" if dry_run else "deleted"
        for table, n in result["deleted"].items():
            typer.echo(f"{verb} {n} rows from {table}")
    raise typer.Exit(0)
