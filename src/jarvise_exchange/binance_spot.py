"""Binance spot account read — signed GET /api/v3/account only. No orders."""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import httpx

from jarvise_exchange.models import SpotBalance

BINANCE_BASE = "https://api.binance.com"
ACCOUNT_PATH = "/api/v3/account"


def resolve_binance_credentials() -> tuple[str, str] | None:
    key = os.environ.get("BINANCE_API_KEY") or ""
    secret = os.environ.get("BINANCE_API_SECRET") or ""
    if not key or not secret:
        return None
    return key, secret


def sign_query(secret: str, query_string: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def balances_from_account_payload(payload: dict[str, Any]) -> list[SpotBalance]:
    out: list[SpotBalance] = []
    for row in payload.get("balances") or []:
        free = Decimal(str(row["free"]))
        locked = Decimal(str(row["locked"]))
        if free == 0 and locked == 0:
            continue
        out.append(
            SpotBalance(
                venue="binance",
                asset=str(row["asset"]),
                free=free,
                locked=locked,
                total=free + locked,
            )
        )
    return out


class BinanceSpotClient:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        client: httpx.Client | None = None,
        base_url: str = BINANCE_BASE,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._client = client
        self._base_url = base_url.rstrip("/")

    def list_spot_balances(self) -> list[SpotBalance]:
        params = {"timestamp": int(time.time() * 1000)}
        query = urlencode(params)
        signature = sign_query(self._api_secret, query)
        headers = {"X-MBX-APIKEY": self._api_key}
        url = f"{self._base_url}{ACCOUNT_PATH}?{query}&signature={signature}"
        own = self._client is None
        http = self._client or httpx.Client(timeout=30.0)
        try:
            resp = http.get(url, headers=headers)
            resp.raise_for_status()
            return balances_from_account_payload(resp.json())
        finally:
            if own:
                http.close()
