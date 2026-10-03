"""Shared pytest fixtures for Jarvise tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _disable_market_safety_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing paper/approval tests seed OHLCV only; gate is covered in
    test_market_safety.py with explicit MarketSafetyConfig."""
    monkeypatch.setenv("JARVISE_MARKET_SAFETY", "0")


@pytest.fixture(autouse=True)
def _no_http_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry/backoff must never slow the suite; test_http_retry asserts the delays explicitly."""
    monkeypatch.setattr("jarvise_ingest.http._sleep", lambda seconds: None)
