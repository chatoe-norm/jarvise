"""Eterna spot balances — read-only VenueClient stub (T2.5).

Eterna's public surface today is MCP ``execute_code`` (includes trading/withdraw).
Jarvise adapters must not call that path. Until a documented GET-only spot-balance
REST exists, this client:

* loads balances from ``ETERNA_SPOT_FIXTURE`` (JSON) for offline tests / dry demos
* otherwise raises ``EternaReadApiBlocked`` with a clear owner-facing reason

Never place orders or move funds.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path

from jarvise_exchange.models import SpotBalance

VENUE = "eterna"


class EternaReadApiBlocked(RuntimeError):
    """No safe GET-only spot balance API available for VenueClient."""


def _dec(value: object) -> Decimal:
    return Decimal(str(value))


def load_balances_fixture(path: Path) -> list[SpotBalance]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    rows = raw.get("balances") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        raise ValueError(f"Eterna fixture must be a list or {{balances: [...]}}: {path}")
    out: list[SpotBalance] = []
    for row in rows:
        asset = str(row["asset"]).upper()
        free = _dec(row.get("free", 0))
        locked = _dec(row.get("locked", 0))
        total = _dec(row["total"]) if row.get("total") is not None else free + locked
        out.append(SpotBalance(venue=VENUE, asset=asset, free=free, locked=locked, total=total))
    return out


class EternaSpotClient:
    """Read-only spot balances. Fixture-backed until a GET-only REST exists."""

    venue = VENUE

    def __init__(self, *, fixture_path: Path | None = None) -> None:
        self.fixture_path = fixture_path

    def list_spot_balances(self) -> list[SpotBalance]:
        path = self.fixture_path
        if path is None:
            raw = (os.environ.get("ETERNA_SPOT_FIXTURE") or "").strip()
            path = Path(raw) if raw else None
        if path is not None:
            if not path.is_file():
                raise FileNotFoundError(f"ETERNA_SPOT_FIXTURE not found: {path}")
            return load_balances_fixture(path)
        raise EternaReadApiBlocked(
            "Eterna has no documented GET-only spot-balance REST for VenueClient. "
            "MCP execute_code is forbidden (includes trading/withdraw). "
            "Set ETERNA_SPOT_FIXTURE=/path/to/balances.json for offline reads, "
            "or wait for a read-only HTTP account API. See docs/exchange/eterna-for-jarvise.md."
        )
