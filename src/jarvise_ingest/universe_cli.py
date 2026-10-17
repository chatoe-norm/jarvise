"""Read-only view of the active paper universe. Never seeds or writes."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import typer

from jarvise_ingest.db import universe_as_of
from jarvise_ingest.universe import preset_members

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = Path(os.environ.get("JARVISE_DB") or (REPO_ROOT / "data" / "analytics" / "jarvise.db"))
EMPTY_HINT = "No active symbols in this universe. This command does not seed membership."

universe_app = typer.Typer(
    name="universe",
    help="Read the active paper universe. Does not add, remove, or seed symbols.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)


def _env_text(name: str, default: str) -> str:
    raw = (os.environ.get(name) or "").strip()
    return raw or default


def connect_readonly(path: Path) -> sqlite3.Connection:
    """Open SQLite with mode=ro. Does not create the file or run migrations."""
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


@universe_app.command("show")
def show(
    db: Path | None = typer.Option(
        None, "--db", help="SQLite path (default JARVISE_DB or data/analytics/jarvise.db)"
    ),
    as_json: bool = typer.Option(False, "--json", help="Print one JSON object"),
) -> None:
    """Print the active universe. Read-only: no migrations and no seed."""
    path = db or DEFAULT_DB
    if not path.is_file():
        typer.echo(f"Error: database file not found: {path}", err=True)
        raise typer.Exit(1)
    universe_id = _env_text("JARVISE_PAPER_UNIVERSE", "paper_core")
    timeframe = _env_text("JARVISE_PAPER_TIMEFRAME", "4h")
    as_of_ms = int(time.time() * 1000)
    try:
        conn = connect_readonly(path)
    except sqlite3.Error as exc:
        typer.echo(f"Error: cannot open database read-only: {exc}", err=True)
        raise typer.Exit(1) from exc
    try:
        try:
            active = universe_as_of(conn, universe_id, as_of_ms)
        except sqlite3.Error as exc:
            typer.echo(f"Error: cannot read universe_membership: {exc}", err=True)
            raise typer.Exit(1) from exc
    finally:
        conn.close()
    payload: dict[str, object] = {
        "ok": True,
        "universe_id": universe_id,
        "timeframe": timeframe,
        "as_of_ms": as_of_ms,
        "active_symbols": active,
        "preset_members": preset_members(universe_id),
        "db": str(path),
    }
    if not active:
        payload["hint"] = EMPTY_HINT
    if as_json:
        typer.echo(json.dumps(payload))
        return
    typer.echo(f"universe_id={universe_id} timeframe={timeframe} as_of_ms={as_of_ms} db={path}")
    typer.echo("active: " + (" ".join(active) if active else "(none)"))
    preset = " ".join(symbol for symbol, _asset in preset_members(universe_id))
    typer.echo("preset: " + (preset or "(none)"))
    if not active:
        typer.echo(EMPTY_HINT)
