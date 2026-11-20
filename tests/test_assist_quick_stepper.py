"""M1a-1 quick stepper (qk:) and stored cards."""

from __future__ import annotations

from typing import Any

import pytest
from tests.test_assist import _ms_bangkok
from tests.test_assist_quick import _answers
from tests.test_assist_quick_card import QuickFake

from jarvise_assist.listen import process_update
from jarvise_assist.quick import QUICK_SAMPLE_TARGET, render_quick_menu
from jarvise_assist.quick_card import quick_mode_tag
from jarvise_ingest.db import open_db

NOW = _ms_bangkok(2026, 11, 20, 12, 0)


@pytest.fixture
def conn(tmp_path: Any) -> Any:
    c = open_db(tmp_path / "q.db")
    yield c
    c.close()


def test_quick_menu_long_short_first_row(conn: Any) -> None:
    text, kb = render_quick_menu(conn, now_ms=NOW)
    assert text.split("\n")[0] == "⚡ Quick (scalping) — แยกจากระบบหลัก"
    assert quick_mode_tag() in text
    row0 = kb["inline_keyboard"][0]
    assert row0[0]["callback_data"] == "qk:sd:l"
    assert row0[1]["callback_data"] == "qk:sd:s"


def test_qk_sd_long_symbol_step(conn: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    fake = QuickFake()
    sent: list[dict[str, Any]] = []
    _seen, answer = _answers()
    upd = {
        "callback_query": {
            "id": "1",
            "data": "qk:sd:l",
            "message": {"message_id": 10, "chat": {"id": 1}, "text": ""},
        }
    }
    out = process_update(
        conn,
        upd,
        send=lambda t, **kw: sent.append({"text": t, **kw}) or 11,
        answer=answer,
        futures=fake,
        now_ms_value=NOW,
    )
    assert out.get("step") == "pick_symbol"
    assert quick_mode_tag() in sent[0]["text"]
    assert len(_seen) == 1


def test_sample_target_25(conn: Any) -> None:
    assert QUICK_SAMPLE_TARGET == 25
    text, _ = render_quick_menu(conn, now_ms=NOW)
    assert f"ตัวอย่าง 0/{QUICK_SAMPLE_TARGET} ไม้" in text
