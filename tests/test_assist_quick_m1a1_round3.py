"""M1a-1 fix round 3 — mutation-backed integration tests."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest import mock

import pytest
from tests.test_assist_execute import _forbid_trade_client
from tests.test_assist_listen import _send_capture
from tests.test_assist_quick_card import (
    _happy_fake,
    _msg,
    _seed_settings,
)
from tests.test_assist_quick_m1a1_round1 import NOW, _flow_to_quoted, _run_qk

from jarvise_assist.listen import process_update
from jarvise_assist.messages import fmt_price
from jarvise_assist.quick import REG_EXPIRE_GRACE_MS, render_quick_menu
from jarvise_assist.quick_card import REFUSAL, quick_mode_tag
from jarvise_assist.quick_m1a1 import (
    MSG_BAD_BUTTON,
    MSG_CARD_TOO_OLD,
    QUICK_CARD_MAX_AGE_MS,
    _stop_step_markup,
)
from jarvise_ingest.db import open_db


def test_full_flow_line2_is_mode_tag(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutant: wrong tag order or missing tag on body line 2."""
    for mode in ("off", "testnet", "live"):
        _forbid_trade_client(monkeypatch)
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
        monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", mode)
        tag = quick_mode_tag()
        conn = open_db(tmp_path / f"ft-{mode}.db")
        _seed_settings(conn)
        fake = _happy_fake()
        sent: list[dict[str, Any]] = []
        edits: list[dict[str, Any]] = []
        answers_total = 0
        cid = None
        for data in ("qk:sd:l",):
            s, ans, _, e = _run_qk(conn, data, fake)
            sent += s
            edits += e
            answers_total += len(ans)
            cid = conn.execute("SELECT id FROM assist_quick_card LIMIT 1").fetchone()["id"]
        for data in (f"qk:sy:{cid}:SUIUSDT", f"qk:sl:{cid}:0.5", f"qk:rg:{cid}"):
            s, ans, _, e = _run_qk(conn, data, fake)
            sent += s
            edits += e
            answers_total += 1
            assert len(ans) == 1
        for block in sent + edits:
            lines = str(block.get("text") or "").split("\n")
            if len(lines) >= 2:
                assert lines[1] == tag
        assert any(str(s.get("text", "")).startswith("✅ ลงทะเบียนไม้ quick") for s in sent)
        assert answers_total == 4
        conn.close()


def test_wrong_side_refresh_exact_text(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutant M17: wrong-side tail text."""
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "wsx.db")
    _seed_settings(conn)
    mark = Decimal("2.0")
    fake = _happy_fake(marks={"SUIUSDT": mark})
    cid = _flow_to_quoted(conn, fake)
    conn.execute(
        "UPDATE assist_quick_card SET stop_kind='price', stop_value='2.5' WHERE id=?",
        (cid,),
    )
    conn.commit()
    _, _, _, edits = _run_qk(conn, f"qk:rf:{cid}", fake)
    want = f"stop 2.5 อยู่ผิดฝั่งของราคาแล้ว (ตอนนี้ {fmt_price(mark)}) — เลือก stop ใหม่\n{quick_mode_tag()}"
    assert edits[-1]["text"] == want
    assert edits[-1]["reply_markup"] == _stop_step_markup(cid)
    assert (
        conn.execute("SELECT status FROM assist_quick_card WHERE id=?", (cid,)).fetchone()["status"]
        == "pick_stop"
    )
    conn.close()


def test_rf_too_old_answers(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "old.db")
    _seed_settings(conn)
    fake = _happy_fake()
    cid = _flow_to_quoted(conn, fake)
    row = conn.execute("SELECT created_at_ms FROM assist_quick_card WHERE id=?", (cid,)).fetchone()
    late = int(row["created_at_ms"]) + QUICK_CARD_MAX_AGE_MS + 1
    _, answers, _, _ = _run_qk(conn, f"qk:rf:{cid}", fake, now_ms=late)
    assert answers == [MSG_CARD_TOO_OLD]


def test_card_after_registration_strips_ttl(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutant M11: _card_text_after_registration no-op."""
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "post.db")
    _seed_settings(conn)
    fake = _happy_fake()
    cid = _flow_to_quoted(conn, fake)
    _, _, _, edits = _run_qk(conn, f"qk:rg:{cid}", fake)
    assert edits
    lines = edits[-1]["text"].split("\n")
    assert not any(ln.startswith("การ์ดนี้ใช้ได้ถึง") for ln in lines)
    assert lines[-1] == "✅ ลงทะเบียนแล้ว"
    conn.close()


def test_malformed_ids_no_db_change(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "bad.db")
    _seed_settings(conn)
    fake = _happy_fake()
    cid = _flow_to_quoted(conn, fake)
    before = tuple(tuple(r) for r in conn.execute("SELECT * FROM assist_quick_card"))
    for data in ("qk:", "qk:sd:x", "qk:sy:zzzzzzzz:BTCUSDT", f"qk:zz:{cid}"):
        _, answers, _, _ = _run_qk(conn, data, fake)
        assert answers == [MSG_BAD_BUTTON]
    after = tuple(tuple(r) for r in conn.execute("SELECT * FROM assist_quick_card"))
    assert before == after


def test_refusal_mapping_open_registration(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutant M15: identity refusal mapping."""
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "map.db")
    _seed_settings(conn)
    fake = _happy_fake()
    cid = _flow_to_quoted(conn, fake)
    with mock.patch(
        "jarvise_assist.quick_m1a1.create_quick_registration_from_card",
        return_value=(None, "open_registration"),
    ):
        sent, _, _, _ = _run_qk(conn, f"qk:rg:{cid}", fake)
    assert REFUSAL["quick_open"] in sent[0]["text"]
    assert (
        conn.execute("SELECT status FROM assist_quick_card WHERE id=?", (cid,)).fetchone()["status"]
        == "quoted"
    )
    conn.close()


def test_qk_rg_success_message_raises_still_registered(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "rsm.db")
    _seed_settings(conn)
    fake = _happy_fake()
    cid = _flow_to_quoted(conn, fake)
    with mock.patch(
        "jarvise_assist.quick_m1a1.registration_success_message",
        side_effect=RuntimeError("send fail"),
    ):
        _, answers, out, _ = _run_qk(conn, f"qk:rg:{cid}", fake)
    assert conn.execute("SELECT COUNT(*) c FROM assist_quick_registration").fetchone()["c"] == 1
    row = conn.execute("SELECT status, registration_id FROM assist_quick_card WHERE id=?", (cid,)).fetchone()
    assert row["status"] == "registered"
    assert row["registration_id"] is not None
    assert answers == [None]
    assert out == {"ok": True, "registration_id": row["registration_id"]}
    conn.close()


def test_qk_rg_answer_raises_no_double(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "ans.db")
    _seed_settings(conn)
    fake = _happy_fake()
    cid = _flow_to_quoted(conn, fake)
    calls: list[Any] = []

    def boom_answer(cq_id: str, *, text: str | None = None) -> bool:
        calls.append(text)
        raise RuntimeError("answer boom")

    chat_id = 1
    upd = {
        "callback_query": {
            "id": "cq-rg",
            "data": f"qk:rg:{cid}",
            "message": {"chat": {"id": chat_id}, "message_id": 1},
        }
    }
    out = process_update(
        conn,
        upd,
        send=lambda t, **kw: 1,
        answer=boom_answer,
        edit=lambda *_a, **_k: True,
        futures=fake,
        now_ms_value=NOW,
    )
    assert out.get("registration_id") or out.get("ok")
    assert conn.execute("SELECT COUNT(*) c FROM assist_quick_registration").fetchone()["c"] == 1
    assert len(calls) == 1


def test_typed_stepper_btc_and_pct(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "tst.db")
    _seed_settings(conn)
    fake = _happy_fake()
    _run_qk(conn, "qk:sd:l", fake)
    edits: list[dict[str, Any]] = []

    def edit_fn(mid: int, text: str, **kw: Any) -> bool:
        edits.append({"text": text, **kw})
        return True

    sent, send = _send_capture()
    process_update(conn, _msg("BTC"), send=send, edit=edit_fn, futures=fake, now_ms_value=NOW)
    assert edits
    assert conn.execute("SELECT status FROM assist_quick_card LIMIT 1").fetchone()["status"] == "pick_stop"
    process_update(conn, _msg("0.4%"), send=send, edit=edit_fn, futures=fake, now_ms_value=NOW)
    row = conn.execute("SELECT status, stop_kind, stop_value FROM assist_quick_card LIMIT 1").fetchone()
    assert row["status"] == "quoted"
    assert row["stop_kind"] == "pct"
    assert row["stop_value"] == "0.4"
    conn.close()


def test_menu_grace_line(tmp_path: Any) -> None:
    conn = open_db(tmp_path / "grace.db")
    t0 = NOW
    conn.execute(
        """
        INSERT INTO assist_quick_registration (
            id, symbol, side, planned_entry, planned_stop, qty,
            status, created_at_ms, expires_at_ms, updated_at_ms
        ) VALUES ('r1', 'BTCUSDT', 'long', 1, 0.9, 0.01, 'open', ?, ?, ?)
        """,
        (t0, t0 + 60_000, t0),
    )
    conn.commit()
    text, _ = render_quick_menu(conn, now_ms=t0 + 60_000 + REG_EXPIRE_GRACE_MS - 1)
    assert "⏳ ลงทะเบียน BTCUSDT LONG — หมดเวลาแล้ว รอตรวจรอบสุดท้าย" in text.split("\n")


def test_sync_tag_notice_select_failure_continues(tmp_path: Any) -> None:
    from jarvise_assist.quick import _send_tag_notice

    conn = open_db(tmp_path / "sel.db")
    t0 = NOW
    conn.execute(
        """
        INSERT INTO assist_quick_registration (
            id, symbol, side, planned_entry, planned_stop, qty, notice_sent_at_ms,
            status, created_at_ms, expires_at_ms, updated_at_ms
        ) VALUES ('r1', 'BTCUSDT', 'long', 1, 0.9, 0.1, NULL, 'tagged', ?, ?, ?)
        """,
        (t0, t0 + 600_000, t0),
    )
    conn.commit()

    class ConnWrap:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def execute(self, sql: str, *a: Any) -> Any:
            if "FROM assist_quick_trade" in sql:
                raise RuntimeError("select boom")
            return self._inner.execute(sql, *a)

        def commit(self) -> None:
            self._inner.commit()

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    wrap = ConnWrap(conn)
    _send_tag_notice(wrap, lambda *_a, **_k: None, registration_id="r1", client=None, now_ms=t0)
    assert (
        conn.execute("SELECT notice_sent_at_ms FROM assist_quick_registration WHERE id='r1'").fetchone()[0]
        == t0
    )
