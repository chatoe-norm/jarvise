"""CoinGecko global macro — GET only. No order placement."""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

from jarvise_ingest.http import ProviderError, get_json

COINGECKO_PUBLIC_GLOBAL = "https://api.coingecko.com/api/v3/global"
COINGECKO_PRO_GLOBAL = "https://pro-api.coingecko.com/api/v3/global"


def _env_key(*names: str) -> str:
    for name in names:
        raw = os.environ.get(name)
        if raw is not None and raw.strip():
            return raw.strip()
    return ""


def resolve_coingecko_global_request() -> tuple[str, dict[str, str]]:
    """Return (url, headers) for GET /global.

    Priority: Pro key → Demo / generic / legacy key → keyless public.
    """
    headers: dict[str, str] = {"Accept": "application/json"}
    pro = _env_key("COINGECKO_PRO_API_KEY")
    if pro:
        headers["x-cg-pro-api-key"] = pro
        return COINGECKO_PRO_GLOBAL, headers
    demo = _env_key("COINGECKO_DEMO_API_KEY", "COINGECKO_API_KEY", "CoinGecko_API_KEY")
    if demo:
        headers["x-cg-demo-api-key"] = demo
        return COINGECKO_PUBLIC_GLOBAL, headers
    return COINGECKO_PUBLIC_GLOBAL, headers


def fetch_global_macro(
    *,
    client: httpx.Client | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """BTC dominance + total crypto market cap → macro_onchain_sentiment row."""
    url, headers = resolve_coingecko_global_request()
    try:
        payload = get_json(url, headers=headers, client=client, provider="coingecko_global")
    except ProviderError as exc:
        raise RuntimeError(
            f"coingecko global failed: {exc}. "
            "Retry: jarvise ingest --skip-macro"
        ) from exc

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise RuntimeError("coingecko global: missing data object")

    mcap_pct = data.get("market_cap_percentage") or {}
    btc_dom = mcap_pct.get("btc") if isinstance(mcap_pct, dict) else None
    total = data.get("total_market_cap") or {}
    global_usd = total.get("usd") if isinstance(total, dict) else None

    if btc_dom is None and global_usd is None:
        raise RuntimeError("coingecko global: empty dominance and market cap")

    ts = int(now_ms if now_ms is not None else time.time() * 1000)
    return {
        "timestamp": ts,
        "fear_greed_index": None,
        "altcoin_season_index": None,
        "btc_dominance_pct": float(btc_dom) if btc_dom is not None else None,
        "exchange_netflow_btc": None,
        "exchange_reserve_btc": None,
        "etf_net_flow_usd": None,
        "global_market_cap_usd": float(global_usd) if global_usd is not None else None,
    }
