"""Typer commands for read-only exchange spot balances. No order placement."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from jarvise_assist.execute import resolve_trade_auth as resolve_assist_trade_auth
from jarvise_exchange.binance_spot import resolve_binance_auth
from jarvise_exchange.eterna_spot import EternaReadApiBlocked
from jarvise_exchange.permissions import audit_key_permissions, fetch_api_restrictions
from jarvise_exchange.registry import (
    default_venue,
    list_supported_venues,
    resolve_venue_client,
)
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


@exchange_app.command("venues")
def venues_cmd(
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    rows = list_supported_venues()
    if as_json:
        typer.echo(json.dumps({"ok": True, "venues": rows, "default": default_venue()}))
        return
    typer.echo(f"default={default_venue()}")
    for row in rows:
        typer.echo(f"{row['venue']}: {row['status']} ({row['role']})")


@exchange_app.command("sync-balances")
def sync_balances(
    db: Annotated[Path | None, typer.Option("--db")] = None,
    venue: Annotated[
        str | None,
        typer.Option("--venue", help="binance (default) or eterna"),
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    name = (venue or default_venue()).strip().lower()
    db_path = db or DEFAULT_DB
    try:
        client = resolve_venue_client(name)
        result = sync_spot_balances(client=client, db_path=db_path, dry_run=dry_run, venue=name)
    except EternaReadApiBlocked as exc:
        msg = str(exc)
        if as_json:
            typer.echo(
                json.dumps(
                    {
                        "ok": False,
                        "error": msg,
                        "venue": "eterna",
                        "blocked": True,
                        "read_only": True,
                    }
                )
            )
        else:
            typer.echo(msg, err=True)
        raise typer.Exit(2) from exc
    except (OSError, ValueError, TypeError) as exc:
        typer.echo(f"exchange auth/config error: {exc}", err=True)
        raise typer.Exit(2) from exc
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
        typer.echo(f"venue={result.venue} inserted={result.inserted} assets={len(result.balances)}")


@exchange_app.command("check-key")
def check_key(
    as_json: Annotated[bool, typer.Option("--json")] = False,
    trade: Annotated[
        bool,
        typer.Option(
            "--trade",
            help="Audit BINANCE_TRADE_* credentials for spot live (ok_for_trade).",
        ),
    ] = False,
    assist_trade: Annotated[
        bool,
        typer.Option(
            "--assist-trade",
            help=(
                "Audit assist one-tap trade key via jarvise_assist.execute.resolve_trade_auth "
                "(futures on, withdrawals off, IP restricted)."
            ),
        ),
    ] = False,
) -> None:
    """Probe API key permissions. No orders."""
    if trade and assist_trade:
        typer.echo("Use only one of --trade or --assist-trade", err=True)
        raise typer.Exit(2)

    if assist_trade:
        auth, reason = resolve_assist_trade_auth()
        if auth is None:
            payload = {
                "ok": False,
                "assist_trade_key": True,
                "reason": reason or "no_key",
                "paper_only": True,
            }
            if as_json:
                typer.echo(json.dumps(payload))
            else:
                typer.echo(f"assist_trade ok=false reason={reason or 'no_key'}")
            raise typer.Exit(2)
        try:
            raw = fetch_api_restrictions(auth)
        except Exception as exc:  # noqa: BLE001
            msg = f"apiRestrictions failed: {exc}"
            if as_json:
                typer.echo(json.dumps({"ok": False, "error": msg, "assist_trade_key": True}))
            else:
                typer.echo(msg, err=True)
            raise typer.Exit(1) from exc
        enable_futures = bool(raw.get("enableFutures"))
        enable_withdrawals = bool(raw.get("enableWithdrawals"))
        ip_restrict = bool(raw.get("ipRestrict"))
        reasons: list[str] = []
        if not enable_futures:
            reasons.append("enableFutures=false")
        if enable_withdrawals:
            reasons.append("enableWithdrawals=true")
        if not ip_restrict:
            reasons.append("ipRestrict=false")
        ok = not reasons
        payload = {
            "ok": ok,
            "assist_trade_key": True,
            "enableFutures": enable_futures,
            "enableWithdrawals": enable_withdrawals,
            "ipRestrict": ip_restrict,
            "block_reasons": reasons,
            "paper_only": True,
        }
        if as_json:
            typer.echo(json.dumps(payload))
        else:
            typer.echo(
                f"key=assist_trade ok={str(ok).lower()} "
                f"futures={str(enable_futures).lower()} "
                f"withdraw={str(enable_withdrawals).lower()} "
                f"ipRestrict={str(ip_restrict).lower()}"
            )
            if reasons:
                typer.echo("block: " + ", ".join(reasons))
        if not ok:
            raise typer.Exit(3)
        return

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
