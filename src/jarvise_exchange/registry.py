"""VenueClient factory — Binance first, Eterna read-only stub second (T2.5)."""

from __future__ import annotations

import os
from typing import Any

from jarvise_exchange.binance_spot import BinanceSpotClient, resolve_binance_auth
from jarvise_exchange.eterna_spot import EternaSpotClient
from jarvise_exchange.protocol import VenueClient

SUPPORTED_VENUES = ("binance", "eterna")


def default_venue() -> str:
    return (os.environ.get("JARVISE_EXCHANGE_VENUE") or "binance").strip().lower()


def resolve_venue_client(venue: str | None = None) -> VenueClient:
    """Return a read-only VenueClient for ``venue`` (or JARVISE_EXCHANGE_VENUE)."""
    name = (venue or default_venue()).strip().lower()
    if name == "binance":
        auth = resolve_binance_auth()
        if auth is None:
            raise ValueError("Missing Binance auth (BINANCE_API_KEY + SECRET or PRIVATE_KEY_PATH)")
        return BinanceSpotClient(auth)
    if name == "eterna":
        return EternaSpotClient()
    raise ValueError(f"Unknown venue {name!r}; supported: {', '.join(SUPPORTED_VENUES)}")


def list_supported_venues() -> list[dict[str, Any]]:
    return [
        {"venue": "binance", "role": "primary", "status": "live_readonly"},
        {
            "venue": "eterna",
            "role": "candidate",
            "status": "fixture_or_blocked",
            "note": "No GET-only REST yet; ETERNA_SPOT_FIXTURE for offline",
        },
    ]
