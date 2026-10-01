"""Shared pytest fixtures for Jarvise tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _disable_market_safety_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing paper/approval tests seed OHLCV only; gate is covered in
    test_market_safety.py with explicit MarketSafetyConfig."""
    monkeypatch.setenv("JARVISE_MARKET_SAFETY", "0")
