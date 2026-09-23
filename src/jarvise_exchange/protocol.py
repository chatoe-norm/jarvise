from __future__ import annotations

from typing import Protocol

from jarvise_exchange.models import SpotBalance


class VenueClient(Protocol):
    def list_spot_balances(self) -> list[SpotBalance]: ...
