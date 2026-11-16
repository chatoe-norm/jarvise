"""Shared pytest fixtures for Jarvise tests."""

from __future__ import annotations

from typing import Any

import pytest

from jarvise_assist.keyboard import BTN_TOGGLE
from jarvise_assist.menu import handle_menu_text, handle_toggle_confirm_callback


@pytest.fixture(autouse=True)
def _default_live_equity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Live sizing must not fall back to paper equity; tests that need fail-closed
    can ``delenv("JARVISE_LIVE_EQUITY_USD")``."""
    monkeypatch.setenv("JARVISE_LIVE_EQUITY_USD", "10000")


@pytest.fixture(autouse=True)
def _disable_market_safety_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing paper/approval tests seed OHLCV only; gate is covered in
    test_market_safety.py with explicit MarketSafetyConfig."""
    monkeypatch.setenv("JARVISE_MARKET_SAFETY", "0")


@pytest.fixture(autouse=True)
def _no_http_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry/backoff must never slow the suite; test_http_retry asserts the delays explicitly.
    Circuit-breaker state is in-process here (no REDIS_URL) and reset per test."""
    monkeypatch.setattr("jarvise_ingest.http._sleep", lambda seconds: None)
    monkeypatch.setattr("jarvise_ingest.http._LOCAL_FAILURES", {})
    monkeypatch.setattr("jarvise_ingest.http._LOCAL_OPEN_UNTIL", {})


@pytest.fixture(autouse=True)
def _no_entry_bar_15m_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Assist trail advice must not hit Binance 15m klines during the suite."""
    import jarvise_assist.trail_advice as trail_advice

    trail_advice._ENTRY_BAR_CACHE.clear()
    trail_advice._ENTRY_BAR_FAIL_MEMO.clear()
    monkeypatch.setattr(trail_advice, "_ENTRY_BAR_FETCH", lambda *_a, **_k: [])


def assist_toggle_confirm(
    conn: Any,
    *,
    now_ms: int,
    send: Any,
    button: str = BTN_TOGGLE,
    message_id: int = 1,
) -> dict[str, Any]:
    """Tap ผู้ช่วย (or legacy), then ✅. Returns the confirm-callback result."""
    prompts: list[dict[str, Any]] = []

    def wrap_send(text: str, reply_markup: Any = None, **kwargs: Any) -> Any:
        prompts.append({"text": text, "reply_markup": reply_markup})
        try:
            return send(text, reply_markup=reply_markup, **kwargs)
        except TypeError:
            return send(text, reply_markup=reply_markup)

    prompt = handle_menu_text(conn, button, now_ms=now_ms, send=wrap_send)
    assert prompt.get("confirm") is True
    assert prompts, "confirm prompt was not sent"
    markup = prompts[-1]["reply_markup"]
    assert markup and markup.get("inline_keyboard")
    confirm_data = markup["inline_keyboard"][0][0]["callback_data"]
    assert str(confirm_data).startswith("at:")

    def answer(text: str | None = None) -> None:
        return None

    def edit(mid: int, text: str, *, reply_markup: Any = None) -> bool:
        del mid, text, reply_markup
        return True

    return handle_toggle_confirm_callback(
        conn,
        str(confirm_data),
        send=send,
        answer=answer,
        edit=edit,
        message={"message_id": message_id},
        now_ms=now_ms,
    )
