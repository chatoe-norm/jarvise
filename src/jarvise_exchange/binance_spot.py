"""Binance spot account read — signed GET /api/v3/account only. No orders.

Supports HMAC-SHA256, Ed25519, and RSA (PKCS#8) signing per Binance Spot REST docs.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
from cryptography.hazmat.primitives.asymmetric.types import PrivateKeyTypes

from jarvise_exchange.models import SpotBalance

BINANCE_BASE = "https://api.binance.com"
ACCOUNT_PATH = "/api/v3/account"


@dataclass
class BinanceAuth:
    """Auth material for signed spot reads. Exactly one of hmac_secret / private_key."""

    api_key: str
    hmac_secret: str | None = None
    private_key: PrivateKeyTypes | None = None

    def __post_init__(self) -> None:
        has_hmac = bool(self.hmac_secret)
        has_asym = self.private_key is not None
        if has_hmac == has_asym:
            raise ValueError("BinanceAuth requires exactly one of hmac_secret or private_key")


def load_private_key_pem(
    path: Path, *, passphrase: str | None = None
) -> PrivateKeyTypes:
    raw = path.read_bytes()
    password = passphrase.encode("utf-8") if passphrase else None
    return serialization.load_pem_private_key(raw, password=password)


def resolve_binance_auth() -> BinanceAuth | None:
    """Resolve API key + HMAC secret or PEM private key from env.

    Preference: BINANCE_API_PRIVATE_KEY_PATH over BINANCE_API_SECRET when both set.
    """
    api_key = os.environ.get("BINANCE_API_KEY") or ""
    if not api_key:
        return None

    key_path = (os.environ.get("BINANCE_API_PRIVATE_KEY_PATH") or "").strip()
    if key_path:
        path = Path(key_path)
        if not path.is_file():
            raise FileNotFoundError(
                f"BINANCE_API_PRIVATE_KEY_PATH not found or not a file: {key_path}"
            )
        passphrase = os.environ.get("BINANCE_API_PRIVATE_KEY_PASSPHRASE") or None
        private_key = load_private_key_pem(path, passphrase=passphrase)
        return BinanceAuth(api_key=api_key, private_key=private_key)

    secret = os.environ.get("BINANCE_API_SECRET") or ""
    if secret:
        return BinanceAuth(api_key=api_key, hmac_secret=secret)
    return None


def resolve_binance_credentials() -> tuple[str, str] | None:
    """HMAC-only helper kept for callers that expect (key, secret). Prefer resolve_binance_auth."""
    auth = resolve_binance_auth()
    if auth is None or auth.hmac_secret is None:
        return None
    return auth.api_key, auth.hmac_secret


def sign_query(secret: str, query_string: str) -> str:
    """HMAC-SHA256 hex digest (Binance HMAC keys)."""
    return hmac.new(
        secret.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def sign_payload(auth: BinanceAuth, query_string: str) -> str:
    """Return signature string for the query payload (hex for HMAC, Base64 for asym)."""
    if auth.hmac_secret is not None:
        return sign_query(auth.hmac_secret, query_string)

    assert auth.private_key is not None
    payload = query_string.encode("ASCII")
    key = auth.private_key
    if isinstance(key, ed25519.Ed25519PrivateKey):
        sig = key.sign(payload)
    elif isinstance(key, rsa.RSAPrivateKey):
        sig = key.sign(payload, padding.PKCS1v15(), hashes.SHA256())
    else:
        raise TypeError(
            f"Unsupported private key type for Binance signing: {type(key).__name__}"
        )
    return base64.b64encode(sig).decode("ascii")


def signature_for_query(auth: BinanceAuth, query_string: str) -> str:
    """Signature value safe to append as a query parameter."""
    raw = sign_payload(auth, query_string)
    if auth.hmac_secret is not None:
        return raw
    return quote(raw, safe="")


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
        auth: BinanceAuth,
        *,
        client: httpx.Client | None = None,
        base_url: str = BINANCE_BASE,
    ) -> None:
        self._auth = auth
        self._client = client
        self._base_url = base_url.rstrip("/")

    def list_spot_balances(self) -> list[SpotBalance]:
        params = {"timestamp": int(time.time() * 1000)}
        query = urlencode(params)
        signature = signature_for_query(self._auth, query)
        headers = {"X-MBX-APIKEY": self._auth.api_key}
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
