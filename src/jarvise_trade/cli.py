"""Typer commands for the gated live path: read-only reconcile and kill-switch flatten.

Flatten sells only while JARVISE_LIVE_TRADING is on and the kill-switch is known-engaged.
New positions are opened only via Approve.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from jarvise_ingest.db import open_db
from jarvise_trade.flatten import flatten_live_positions
from jarvise_trade.reconcile import reconcile_live_orders

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = REPO_ROOT / "data" / "analytics" / "jarvise.db"

trade_app = typer.Typer(
    name="trade",
    help=("Live-order audit and kill-switch flatten. New orders open only via Approve when live is enabled."),
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)


@trade_app.command("reconcile")
def reconcile(
    db: Annotated[Path | None, typer.Option("--db")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Query the venue but write nothing")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Re-query open live orders (submitted / partially_filled) and persist fill state."""
    path = db or DEFAULT_DB
    if not path.exists():
        payload = {"ok": True, "open": 0, "skipped": True, "reason": "no_database", "read_only": True}
        typer.echo(json.dumps(payload) if as_json else "no database; nothing to reconcile")
        raise typer.Exit(0)
    conn = open_db(path)
    try:
        result = reconcile_live_orders(conn, dry_run=dry_run)
    finally:
        conn.close()
    if as_json:
        typer.echo(json.dumps(result, default=str))
    else:
        typer.echo(
            f"open={result['open']} updated={len(result['updated'])} "
            f"unchanged={len(result['unchanged'])} missing={len(result['missing_on_venue'])} "
            f"errors={len(result['errors'])} dry_run={dry_run}"
        )
        for err in result["errors"]:
            typer.echo(f"  ! {err}", err=True)
    raise typer.Exit(0 if result["ok"] else 1)


@trade_app.command("flatten")
def flatten(
    db: Annotated[Path | None, typer.Option("--db")] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="List what would be cancelled/sold; no venue HTTP")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Cancel Jarvise's protective stops and MARKET SELL live spot positions.

    Runs only when JARVISE_LIVE_TRADING is on and the kill-switch is known-engaged
    (engage it on Ops first). Idempotent: a rerun never posts a second sell.
    """
    path = db or DEFAULT_DB
    if not path.exists():
        payload = {"ok": True, "ran": False, "skipped_reason": "no_database"}
        typer.echo(json.dumps(payload) if as_json else "no database; nothing to flatten")
        raise typer.Exit(0)
    conn = open_db(path)
    try:
        result = flatten_live_positions(conn, dry_run=dry_run)
    finally:
        conn.close()
    if as_json:
        typer.echo(json.dumps(result, default=str))
        raise typer.Exit(0 if result["ok"] else 1)
    if not result["ran"]:
        typer.echo(f"flatten skipped: {result['skipped_reason']}")
        raise typer.Exit(0 if result["ok"] else 1)
    typer.echo(
        f"reason={result['reason']} symbols={len(result['symbols'])} sold={len(result['sold'])} "
        f"cancelled={len(result['cancelled'])} dust={len(result['dust'])} "
        f"errors={len(result['errors'])} dry_run={dry_run}"
    )
    for entry in result["symbols"]:
        typer.echo(
            f"  {entry['symbol']}: {entry['status']} qty={entry['inventory']} id={entry['client_order_id']}"
        )
    for symbol in result["unprotected"]:
        typer.echo(f"  !! UNPROTECTED {symbol}: close it on Binance now", err=True)
    for err in result["errors"]:
        typer.echo(f"  ! {err}", err=True)
    raise typer.Exit(0 if result["ok"] else 1)
