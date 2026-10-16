"""Altcoin Season Index Provider.

Sources in priority order:
1. CoinMarketCap dedicated endpoint (when CMC_API_KEY is set)
2. CoinGlass dedicated endpoint (when COINGLASS_API_KEY / CG-API-KEY is set)
3. Dynamic computation via Binance public 90-day returns of top altcoins vs BTC.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from jarvise_ingest.http import ProviderError, get_json

CMC_PRO_URL = "https://pro-api.coinmarketcap.com/v1/altcoin-season-index/latest"
COINGLASS_ALTSEASON_URL = "https://open-api-v4.coinglass.com/api/index/altcoin-season"

BENCHMARK_SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "AVAXUSDT",
    "LINKUSDT",
    "SUIUSDT",
    "NEARUSDT",
    "DOTUSDT",
    "LTCUSDT",
    "BCHUSDT",
    "UNIUSDT",
    "APTUSDT",
    "FETUSDT",
    "RENDERUSDT",
    "AAVEUSDT",
    "TAOUSDT",
)


def parse_altseason(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if isinstance(data, list) and data:
        data = data[-1] if isinstance(data[-1], dict) else data[0]
    if not isinstance(data, dict):
        return None
    for key in ("altcoin_index", "altcoin_season_index", "altcoinSeasonIndex", "index"):
        raw = data.get(key)
        if raw is None:
            continue
        try:
            value = int(round(float(raw)))
        except (TypeError, ValueError):
            continue
        if 0 <= value <= 100:
            return value
    return None


def fetch_altseason_from_cmc(*, client: httpx.Client | None = None) -> int | None:
    key = (os.environ.get("CMC_API_KEY") or "").strip()
    if not key:
        return None
    headers = {"X-CMC_PRO_API_KEY": key, "Accept": "application/json"}
    try:
        payload = get_json(CMC_PRO_URL, headers=headers, client=client, provider="cmc_altseason")
        return parse_altseason(payload)
    except ProviderError:
        return None


def fetch_altseason_from_coinglass(*, client: httpx.Client | None = None) -> int | None:
    key = (os.environ.get("COINGLASS_API_KEY") or os.environ.get("CG-API-KEY") or "").strip()
    if not key:
        return None
    headers = {"CG-API-KEY": key, "Accept": "application/json"}
    try:
        payload = get_json(
            COINGLASS_ALTSEASON_URL,
            headers=headers,
            client=client,
            provider="coinglass_altseason",
        )
        return parse_altseason(payload)
    except ProviderError:
        return None


def fetch_altseason_from_binance_90d(*, client: httpx.Client | None = None) -> int | None:
    """Computes Altcoin Season Index based on 90-day return of top alts vs BTC."""
    local_client = client or httpx.Client(timeout=4.0)
    rets: dict[str, float] = {}
    try:
        for s in BENCHMARK_SYMBOLS:
            try:
                res = local_client.get(
                    "https://api.binance.com/api/v3/klines",
                    params={"symbol": s, "interval": "1d", "limit": 90},
                )
                if res.status_code == 200:
                    raw = res.json()
                    if isinstance(raw, list) and len(raw) >= 90:
                        open_90 = float(raw[0][1])
                        close_now = float(raw[-1][4])
                        if open_90 > 0:
                            rets[s] = (close_now - open_90) / open_90
            except Exception:
                continue
    finally:
        if client is None:
            local_client.close()

    btc_ret = rets.get("BTCUSDT")
    if btc_ret is None:
        return None

    alts = {s: r for s, r in rets.items() if s != "BTCUSDT"}
    if not alts:
        return None

    outperformed = sum(1 for _, r in alts.items() if r > btc_ret)
    score = int(round((outperformed / len(alts)) * 100))
    return max(0, min(100, score))


def fetch_altseason(*, client: httpx.Client | None = None) -> int | None:
    # 1. Official CoinMarketCap
    score = fetch_altseason_from_cmc(client=client)
    if score is not None:
        return score

    # 2. CoinGlass
    score = fetch_altseason_from_coinglass(client=client)
    if score is not None:
        return score

    # 3. Dynamic Binance 90-Day Top Alts vs BTC
    try:
        score = fetch_altseason_from_binance_90d(client=client)
        if score is not None:
            return score
    except Exception:
        pass

    return None
