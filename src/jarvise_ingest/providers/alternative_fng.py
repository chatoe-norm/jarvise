"""Alternative.me Fear & Greed — free GET, context only (not a trade gate)."""

from __future__ import annotations

from typing import Any

import httpx

from jarvise_ingest.http import ProviderError, get_json

FNG_URL = "https://api.alternative.me/fng/?limit=1"


def parse_fear_greed(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        return None
    first = data[0]
    if not isinstance(first, dict):
        return None
    raw = first.get("value")
    if not isinstance(raw, (int, str)):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if value < 0 or value > 100:
        return None
    return value


def fetch_fear_greed(*, client: httpx.Client | None = None) -> int | None:
    try:
        payload = get_json(FNG_URL, client=client, provider="alternative_fng")
    except ProviderError as exc:
        raise RuntimeError(f"alternative.me fear-greed failed: {exc}") from exc
    return parse_fear_greed(payload)
