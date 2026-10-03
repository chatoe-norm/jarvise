"""Binance API key permission probe (signed GET). No order placement."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlencode

import httpx

from jarvise_exchange.binance_spot import (
    BINANCE_BASE,
    BinanceAuth,
    signature_for_query,
)
from jarvise_ingest.http import get_json

RESTRICTIONS_PATH = "/sapi/v1/account/apiRestrictions"


def fetch_api_restrictions(
    auth: BinanceAuth,
    *,
    client: httpx.Client | None = None,
    base_url: str = BINANCE_BASE,
    timestamp_ms: int | None = None,
) -> dict[str, Any]:
    """GET /sapi/v1/account/apiRestrictions for the calling API key."""

    def signed_url() -> str:
        params = {"timestamp": int(timestamp_ms if timestamp_ms is not None else time.time() * 1000)}
        query = urlencode(params)
        signature = signature_for_query(auth, query)
        return f"{base_url.rstrip('/')}{RESTRICTIONS_PATH}?{query}&signature={signature}"

    headers = {"X-MBX-APIKEY": auth.api_key}
    payload = get_json(signed_url, headers=headers, client=client, provider="binance_restrictions")
    if not isinstance(payload, dict):
        raise ValueError("unexpected apiRestrictions response type")
    return payload


def audit_key_permissions(perms: dict[str, Any]) -> dict[str, Any]:
    """Classify key safety for Jarvise live trade (spot ok, withdraw forbidden)."""
    withdraw = bool(perms.get("enableWithdrawals"))
    internal = bool(perms.get("enableInternalTransfer"))
    universal = bool(perms.get("permitsUniversalTransfer"))
    spot = bool(
        perms.get("enableSpotAndMarginTrading")
        or perms.get("enableSpotTrading")
    )
    reading = bool(perms.get("enableReading", True))
    futures = bool(perms.get("enableFutures"))
    margin = bool(perms.get("enableMargin"))

    ok_for_read = reading and not withdraw
    ok_for_trade = spot and not withdraw and not internal and not universal
    reasons: list[str] = []
    if withdraw:
        reasons.append("enableWithdrawals=true")
    if internal:
        reasons.append("enableInternalTransfer=true")
    if universal:
        reasons.append("permitsUniversalTransfer=true")
    if not spot:
        reasons.append("spot trading disabled")

    return {
        "ok_for_read": ok_for_read,
        "ok_for_trade": ok_for_trade,
        "enableWithdrawals": withdraw,
        "enableInternalTransfer": internal,
        "permitsUniversalTransfer": universal,
        "enableSpotAndMarginTrading": spot,
        "enableReading": reading,
        "enableFutures": futures,
        "enableMargin": margin,
        "block_reasons": reasons,
        "raw": perms,
    }
