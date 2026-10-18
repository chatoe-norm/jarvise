"""Quiet mode gates pending alerts and leaves critical alerts alone."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from jarvise_notify.telegram import (
    notify_ingest_health,
    notify_kill_switch,
    notify_pending_digest,
    notify_pending_enqueue,
    notify_risk_halt,
)


def _client() -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    client = MagicMock(spec=httpx.Client)
    client.post.return_value = mock_resp
    return client


def _quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setattr("jarvise_notify.quiet.quiet_mode_enabled", lambda: True)


def test_quiet_mode_suppresses_normal_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    _quiet(monkeypatch)
    client = _client()
    row = {"symbol": "BTCUSDT", "action": "long", "confidence_score": 0.8}
    assert notify_pending_enqueue(row, open_symbols={"BTCUSDT"}, client=client) is False
    digest = notify_pending_digest([row], open_symbols={"BTCUSDT"}, client=client)
    assert digest["sent"] is False
    assert digest["reason"] == "quiet_mode"
    assert client.post.call_count == 0


def test_quiet_mode_sends_flat_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    _quiet(monkeypatch)
    client = _client()
    row = {"symbol": "ETHUSDT", "action": "flat", "confidence_score": 0.4}
    assert notify_pending_enqueue(row, open_symbols={"ETHUSDT"}, client=client) is True
    assert client.post.call_count == 1


def test_quiet_mode_still_sends_critical(monkeypatch: pytest.MonkeyPatch) -> None:
    _quiet(monkeypatch)
    client = _client()
    assert notify_kill_switch("owner", engaged=True, client=client) is True
    assert notify_risk_halt("daily loss", client=client) is True
    sent = notify_ingest_health({"alerts": ["stale BTCUSDT"], "symbols": {}}, client=client)
    assert sent is True
    assert client.post.call_count == 3
