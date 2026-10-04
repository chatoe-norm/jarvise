"""CoinMarketCap Altcoin Season — best-effort when CMC_API_KEY is set.

There is no stable public CMC field named altcoin_season_index. We look for
documented-adjacent keys on global metrics and leave NULL when absent.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from jarvise_ingest.http import ProviderError, get_json

CMC_GLOBAL_URL = "https://pro-api.coinmarketcap.com/v1/global-metrics/quotes/latest"


def parse_altseason(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    for key in ("altcoin_season_index", "altcoinSeasonIndex"):
        raw = data.get(key)
        if raw is None:
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if 0 <= value <= 100:
            return value
    return None


def fetch_altseason(*, client: httpx.Client | None = None) -> int | None:
    key = (os.environ.get("CMC_API_KEY") or "").strip()
    if not key:
        return None
    headers = {"X-CMC_PRO_API_KEY": key, "Accept": "application/json"}
    try:
        payload = get_json(CMC_GLOBAL_URL, headers=headers, client=client, provider="cmc_altseason")
    except ProviderError:
        return None
    return parse_altseason(payload)
