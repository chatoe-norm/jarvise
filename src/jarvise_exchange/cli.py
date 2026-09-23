"""Typer commands for read-only exchange spot balances. No order placement."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Optional

import typer

from jarvise_exchange.binance_spot import BinanceSpotClient, resolve_binance_credentials
from jarvise_exchange.sync import sync_spot_balances

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = REPO_ROOT / "data" / "analytics" / "jarvise.db"

exchange_app = typer.Typer(
    name="exchange",
    help="Read-only exchange spot balances (no order placement).",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)


@exchange_app.command("sync-balances")
def sync_balances(
    db: Annotated[Optional[Path], typer.Option("--db")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    creds = resolve_binance_credentials()
    if creds is None:
        typer.echo(
            "Missing BINANCE_API_KEY / BINANCE_API_SECRET.\n"
            "Example:\n"
            "  export BINANCE_API_KEY=...\n"
            "  export BINANCE_API_SECRET=...\n"
            "  jarvise exchange sync-balances --json",
            err=True,
        )
        raise typer.Exit(2)
    api_key, api_secret = creds
    db_path = db or DEFAULT_DB
    try:
        client = BinanceSpotClient(api_key, api_secret)
        result = sync_spot_balances(client=client, db_path=db_path, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001
        msg = f"exchange sync failed: {exc}"
        if as_json:
            typer.echo(json.dumps({"ok": False, "error": msg, "read_only": True}))
        else:
            typer.echo(msg, err=True)
        raise typer.Exit(1) from exc
    payload = {
        "ok": result.ok,
        "dry_run": result.dry_run,
        "venue": result.venue,
        "fetched_at_ms": result.fetched_at_ms,
        "inserted": result.inserted,
        "balances": [
            {
                "asset": b.asset,
                "free": str(b.free),
                "locked": str(b.locked),
                "total": str(b.total),
            }
            for b in result.balances
        ],
        "read_only": True,
        "paper_only": True,
    }
    if as_json:
        typer.echo(json.dumps(payload))
    else:
        typer.echo(
            f"venue={result.venue} inserted={result.inserted} assets={len(result.balances)}"
        )
