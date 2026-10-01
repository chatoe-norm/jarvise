"""Derivatives router — CoinGlass when keyed, else Binance Futures public."""

from __future__ import annotations

import httpx

from jarvise_ingest.providers import binance_futures_deriv, coinglass

PROVIDER_COINGLASS = "coinglass"
PROVIDER_BINANCE_FUTURES = "binance_futures"


def resolve_api_key() -> str | None:
    return coinglass.resolve_api_key()


def select_provider(*, api_key: str | None = None) -> str:
    key = api_key if api_key is not None else resolve_api_key()
    return PROVIDER_COINGLASS if key else PROVIDER_BINANCE_FUTURES


def fetch_derivatives(
    pair_symbol: str,
    interval: str,
    limit: int = 30,
    *,
    api_key: str | None = None,
    client: httpx.Client | None = None,
) -> tuple[list[dict], str]:
    """Return (rows, provider_name)."""
    provider = select_provider(api_key=api_key)
    if provider == PROVIDER_COINGLASS:
        rows = coinglass.fetch_derivatives(
            pair_symbol,
            interval,
            limit,
            api_key=api_key or resolve_api_key(),
            client=client,
        )
        return rows, PROVIDER_COINGLASS
    rows = binance_futures_deriv.fetch_derivatives(
        pair_symbol, interval, limit, client=client
    )
    return rows, PROVIDER_BINANCE_FUTURES
