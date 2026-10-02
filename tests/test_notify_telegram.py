"""Tests for soft-fail Telegram notify."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from jarvise_notify.telegram import (
    format_digest_message,
    format_enqueue_message,
    format_ingest_health_message,
    notify_ingest_health,
    notify_pending_digest,
    notify_pending_enqueue,
    send_telegram_message,
)


def test_send_noop_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    client = MagicMock(spec=httpx.Client)
    assert send_telegram_message("hi", client=client) is False
    assert client.post.call_count == 0


def test_send_telegram_mocked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    client = MagicMock(spec=httpx.Client)
    client.post.return_value = mock_resp
    assert send_telegram_message("hello", client=client) is True
    assert client.post.call_count == 1
    url = client.post.call_args.args[0]
    assert "/bottok/sendMessage" in url
    assert "api.telegram.org" in url


def test_notify_enqueue_formats(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    client = MagicMock(spec=httpx.Client)
    client.post.return_value = mock_resp
    row = {
        "id": "abc",
        "symbol": "BTCUSDT",
        "timeframe": "4h",
        "action": "long",
        "size_pct_equity": 5.0,
        "expires_at_ms": 99,
    }
    assert notify_pending_enqueue(row, client=client) is True
    text = client.post.call_args.kwargs["json"]["text"]
    assert "BTCUSDT" in text
    assert "abc" in text


def test_digest_empty_no_send(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    client = MagicMock(spec=httpx.Client)
    out = notify_pending_digest([], client=client)
    assert out["sent"] is False
    assert out["reason"] == "empty"
    assert client.post.call_count == 0


def test_digest_with_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    client = MagicMock(spec=httpx.Client)
    client.post.return_value = mock_resp
    rows = [
        {
            "id": "1",
            "symbol": "ETHUSDT",
            "timeframe": "4h",
            "action": "long",
            "size_pct_equity": 2,
        }
    ]
    out = notify_pending_digest(rows, client=client)
    assert out["sent"] is True
    assert out["count"] == 1
    assert "ETHUSDT" in format_digest_message(rows, total=1)
    assert "pending approval" in format_enqueue_message(rows[0]).lower()


def test_ingest_health_message_and_notify(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "ok": False,
        "timeframe": "4h",
        "symbols": {"BTCUSDT": {"rows": 217, "ema200_ready": 0, "newest_age_min": 12.0, "gaps": 0}},
        "alerts": ["BTCUSDT: ema_200 not ready (217 rows) — analyze stays flat"],
        "backfill_hint": "jarvise ingest --symbol BTCUSDT --timeframe 4h --since 2021-01-01 --skip-derivatives --json",
    }
    text = format_ingest_health_message(payload)
    assert "Jarvise ingest health (4h)" in text
    assert "ema_200 not ready" in text
    assert "--since 2021-01-01" in text

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert notify_ingest_health(payload) is False

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    client = MagicMock(spec=httpx.Client)
    client.post.return_value = mock_resp
    assert notify_ingest_health(payload, client=client) is True
    assert notify_ingest_health({**payload, "ok": True, "alerts": []}, client=client) is False
    assert client.post.call_count == 1
