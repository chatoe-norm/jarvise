"""Typer commands for read-only exchange spot balances. No order placement."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from jarvise_exchange.binance_spot import BinanceSpotClient, resolve_binance_auth
from jarvise_exchange.permissions import audit_key_permissions, fetch_api_restrictions
from jarvise_exchange.sync import sync_spot_balances
from jarvise_trade.auth import resolve_trade_auth

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
    db: Annotated[Path | None, typer.Option("--db")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    try:
        auth = resolve_binance_auth()
    except (OSError, ValueError, TypeError) as exc:
        typer.echo(f"Binance auth config error: {exc}", err=True)
        raise typer.Exit(2) from exc
    if auth is None:
        typer.echo(
            "Missing Binance auth.\n"
            "HMAC example:\n"
            "  export BINANCE_API_KEY=...\n"
            "  export BINANCE_API_SECRET=...\n"
            "Ed25519/RSA example:\n"
            "  export BINANCE_API_KEY=...\n"
            "  export BINANCE_API_PRIVATE_KEY_PATH=/path/to/private.pem\n"
            "  jarvise exchange sync-balances --json",
            err=True,
        )
        raise typer.Exit(2)
    db_path = db or DEFAULT_DB
    try:
        client = BinanceSpotClient(auth)
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


@exchange_app.command("check-key")
def check_key(
    as_json: Annotated[bool, typer.Option("--json")] = False,
    trade: Annotated[
        bool,
        typer.Option(
            "--trade",
            help="Audit BINANCE_TRADE_* credentials instead of read-only BINANCE_API_*.",
        ),
    ] = False,
) -> None:
    """Probe API key permissions (spot ok / withdraw forbidden). No orders."""
    try:
        auth = resolve_trade_auth() if trade else resolve_binance_auth()
    except (OSError, ValueError, TypeError) as exc:
        typer.echo(f"Binance auth config error: {exc}", err=True)
        raise typer.Exit(2) from exc
    if auth is None:
        which = "BINANCE_TRADE_API_KEY (+ secret or PEM)" if trade else "BINANCE_API_KEY (+ secret or PEM)"
        typer.echo(f"Missing {which}", err=True)
        raise typer.Exit(2)
    try:
        raw = fetch_api_restrictions(auth)
        audit = audit_key_permissions(raw)
    except Exception as exc:  # noqa: BLE001
        msg = f"apiRestrictions failed: {exc}"
        if as_json:
            typer.echo(json.dumps({"ok": False, "error": msg, "trade_key": trade}))
        else:
            typer.echo(msg, err=True)
        raise typer.Exit(1) from exc

    payload = {
        "ok": True,
        "trade_key": trade,
        "ok_for_read": audit["ok_for_read"],
        "ok_for_trade": audit["ok_for_trade"],
        "enableWithdrawals": audit["enableWithdrawals"],
        "enableSpotAndMarginTrading": audit["enableSpotAndMarginTrading"],
        "enableFutures": audit["enableFutures"],
        "enableMargin": audit["enableMargin"],
        "block_reasons": audit["block_reasons"],
        "paper_only": True,
    }
    if as_json:
        typer.echo(json.dumps(payload))
    else:
        role = "trade" if trade else "read"
        typer.echo(
            f"key={role} ok_for_read={audit['ok_for_read']} "
            f"ok_for_trade={audit['ok_for_trade']} "
            f"withdraw={audit['enableWithdrawals']} spot={audit['enableSpotAndMarginTrading']}"
        )
        if audit["block_reasons"]:
            typer.echo("block: " + ", ".join(audit["block_reasons"]))
    if trade and not audit["ok_for_trade"]:
        raise typer.Exit(3)
    if (not trade) and audit["enableWithdrawals"]:
        raise typer.Exit(3)
