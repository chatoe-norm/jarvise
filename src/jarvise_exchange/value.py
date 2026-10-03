"""USD valuation for spot balances via Binance public USDT prices (GET-only)."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

import httpx

from jarvise_exchange.models import SpotBalance, ValuationResult, ValuedBalance
from jarvise_ingest.http import get_json

BINANCE_BASE = "https://api.binance.com"
TICKER_PRICE_PATH = "/api/v3/ticker/price"

# Face-value ~USD (USDT proxy). No ticker fetch.
STABLE_ASSETS = frozenset({"USDT", "USDC", "FDUSD", "BUSD"})

PriceFn = Callable[[str], Decimal | None]


def fetch_usdt_price(
    asset: str,
    *,
    client: httpx.Client | None = None,
    base_url: str = BINANCE_BASE,
) -> Decimal | None:
    """Public GET ticker/price for {ASSET}USDT. Returns None on miss/error."""
    symbol = f"{asset.upper()}USDT"
    try:
        # One retry only: this feeds a UI panel, so stay soft and quick on failure.
        payload = get_json(
            f"{base_url.rstrip('/')}{TICKER_PRICE_PATH}",
            params={"symbol": symbol},
            client=client,
            timeout=15.0,
            retries=1,
            provider="binance_ticker_price",
        )
        raw = payload.get("price") if isinstance(payload, dict) else None
        if raw is None:
            return None
        return Decimal(str(raw))
    except Exception:  # noqa: BLE001
        return None


def value_spot_balances(
    balances: list[SpotBalance],
    *,
    price_fn: PriceFn | None = None,
    http_client: httpx.Client | None = None,
) -> ValuationResult:
    """Attach ~USD to each balance. Unpriced rows have usd=None and are omitted from total."""
    get_price = price_fn
    if get_price is None:

        def get_price(asset: str) -> Decimal | None:
            return fetch_usdt_price(asset, client=http_client)

    rows: list[ValuedBalance] = []
    total = Decimal("0")
    priced = 0
    unpriced = 0
    for bal in balances:
        asset = bal.asset.upper()
        if asset in STABLE_ASSETS:
            unit = Decimal("1")
            usd = bal.total * unit
            rows.append(ValuedBalance(balance=bal, usd=usd, price_usd=unit))
            total += usd
            priced += 1
            continue
        price = get_price(asset)
        if price is None:
            rows.append(ValuedBalance(balance=bal, usd=None, price_usd=None))
            unpriced += 1
            continue
        unit = price
        usd = bal.total * unit
        rows.append(ValuedBalance(balance=bal, usd=usd, price_usd=unit))
        total += usd
        priced += 1
    return ValuationResult(
        rows=rows,
        total_usd=total,
        priced_count=priced,
        unpriced_count=unpriced,
    )
