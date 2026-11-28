"""M1a-1 quick sync Telegram notices."""

from __future__ import annotations

from typing import Any

import pytest

from jarvise_assist.quick import _expire_window_phrase, _send_reg_expire_notice, sync_quick_trades
from jarvise_assist.quick_card import quick_mode_tag
from jarvise_ingest.db import open_db


def _no_fill_notice() -> str:
    sent: list[str] = []

    def send(msg: str, **_kw: Any) -> None:
        sent.append(msg)

    _send_reg_expire_notice(send, sym="BTCUSDT", side="long", kind="no_fill")
    assert len(sent) == 1
    return sent[0]


def test_expire_no_fill_notice(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVISE_QUICK_REG_TTL_MS", raising=False)
    text = _no_fill_notice()
    assert "ไม่พบไม้ภายใน 10 นาที" in text
    assert quick_mode_tag() in text


def test_no_fill_notice_window_follows_reg_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """window_ms omitted uses reg_ttl_ms(), not a hardcoded 10 minutes."""
    monkeypatch.delenv("JARVISE_QUICK_REG_TTL_MS", raising=False)
    default = _no_fill_notice()
    assert _expire_window_phrase(10 * 60_000) in default
    assert "10 นาที" in default

    monkeypatch.setenv("JARVISE_QUICK_REG_TTL_MS", "900000")
    overridden = _no_fill_notice()
    phrase = _expire_window_phrase(900_000)
    assert phrase == "15 นาที"
    assert phrase in overridden
    assert "10 นาที" not in overridden


def test_sync_idle_returns_empty(tmp_path: Any) -> None:
    conn = open_db(tmp_path / "n.db")
    try:
        assert sync_quick_trades(conn, None, now_ms=1, send=lambda *_a, **_k: None) == {}
    finally:
        conn.close()
