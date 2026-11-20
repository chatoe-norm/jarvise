"""M1a-1 quick sync Telegram notices."""

from __future__ import annotations

from typing import Any

from jarvise_assist.quick import _send_reg_expire_notice, sync_quick_trades
from jarvise_assist.quick_card import quick_mode_tag
from jarvise_ingest.db import open_db


def test_expire_no_fill_notice(tmp_path: Any) -> None:
    sent: list[str] = []

    def send(msg: str, **_kw: Any) -> None:
        sent.append(msg)

    _send_reg_expire_notice(send, sym="BTCUSDT", side="long", kind="no_fill")
    assert sent
    assert "ไม่พบไม้ภายใน 10 นาที" in sent[0]
    assert quick_mode_tag() in sent[0]


def test_sync_idle_returns_empty(tmp_path: Any) -> None:
    conn = open_db(tmp_path / "n.db")
    try:
        assert sync_quick_trades(conn, None, now_ms=1, send=lambda *_a, **_k: None) == {}
    finally:
        conn.close()
