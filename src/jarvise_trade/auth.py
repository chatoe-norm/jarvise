"""Trade-key auth — distinct from read-only BINANCE_API_* credentials."""

from __future__ import annotations

import os
from pathlib import Path

from jarvise_exchange.binance_spot import BinanceAuth, load_private_key_pem


def resolve_trade_auth() -> BinanceAuth | None:
    """Resolve trade API key + HMAC secret or PEM from BINANCE_TRADE_* env.

    Preference: BINANCE_TRADE_PRIVATE_KEY_PATH over BINANCE_TRADE_API_SECRET.
    """
    api_key = os.environ.get("BINANCE_TRADE_API_KEY") or ""
    if not api_key:
        return None

    key_path = (os.environ.get("BINANCE_TRADE_PRIVATE_KEY_PATH") or "").strip()
    if key_path:
        path = Path(key_path)
        if not path.is_file():
            raise FileNotFoundError(
                f"BINANCE_TRADE_PRIVATE_KEY_PATH not found or not a file: {key_path}"
            )
        passphrase = os.environ.get("BINANCE_TRADE_PRIVATE_KEY_PASSPHRASE") or None
        private_key = load_private_key_pem(path, passphrase=passphrase)
        return BinanceAuth(api_key=api_key, private_key=private_key)

    secret = os.environ.get("BINANCE_TRADE_API_SECRET") or ""
    if secret:
        return BinanceAuth(api_key=api_key, hmac_secret=secret)
    return None
