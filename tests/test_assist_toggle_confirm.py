"""Assist toggle confirm — button asks first; ✅ applies via toggle_enabled."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from tests.assist_helpers import assist_toggle_confirm
from tests.test_assist_day_loss import NOW, _futures, _trade

from jarvise_assist.keyboard import (
    ASSIST_TOGGLE_PREFIX,
    BTN_TOGGLE,
    LEGACY_BTN_TOGGLE,
    toggle_confirm_keyboard,
)
from jarvise_assist.listen import process_update
from jarvise_assist.menu import (
    TOGGLE_CONFIRM_TTL_S,
    handle_menu_text,
    handle_toggle_confirm_callback,
    render_toggle_confirm,
    toggle_enabled,
)
from jarvise_assist.pause import (
    BOT_ENTRIES_ONLY_TH,
    bangkok_day_id,
    bangkok_resume_label,
    evaluate_pause,
    next_0820_bangkok_ms,
    pause_clear_warning,
)
from jarvise_assist.store import ensure_settings, get_edge, write_edge, write_settings
from jarvise_ingest.db import open_db

_TOGGLE_DATA_RE = re.compile(r"^at:(on|onp|off|no):([0-9]{10})$")


def _send_capture() -> tuple[list[dict[str, Any]], Any]:
    sent: list[dict[str, Any]] = []

    def send(text: str, *, reply_markup: dict[str, Any] | None = None) -> int:
        mid = len(sent) + 1
        sent.append({"text": text, "reply_markup": reply_markup, "message_id": mid})
        return mid

    return sent, send


def _snap(conn: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    return dict(ensure_settings(conn)), dict(get_edge(conn))


def _confirm_data_from_sent(sent: list[dict[str, Any]]) -> str:
    markup = sent[-1]["reply_markup"]
    return str(markup["inline_keyboard"][0][0]["callback_data"])


def test_button_tap_only_does_not_mutate(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "tap.db")
    write_settings(conn, enabled=True, paused_until_ms=NOW + 3_600_000, pause_reason="streak")
    before_s, before_e = _snap(conn)
    sent, send = _send_capture()
    out = handle_menu_text(conn, BTN_TOGGLE, now_ms=NOW, send=send)
    assert out.get("confirm") is True
    after_s, after_e = _snap(conn)
    assert after_s == before_s
    assert after_e == before_e
    assert after_s["enabled"] is True
    assert after_s["paused_until_ms"] == NOW + 3_600_000
    assert after_s["pause_reason"] == "streak"
    assert len(sent) == 1
    markup = sent[0]["reply_markup"]
    assert markup is not None
    buttons = markup["inline_keyboard"][0]
    assert len(buttons) == 2
    for btn in buttons:
        assert _TOGGLE_DATA_RE.match(btn["callback_data"])
    conn.close()


def test_confirm_same_side_effects_as_toggle_enabled(tmp_path: Path) -> None:
    # ON direction: disabled + active day_loss pause today + -3.2R closed bot trade
    path_a = tmp_path / "a.db"
    path_b = tmp_path / "b.db"
    for path in (path_a, path_b):
        conn = open_db(path)
        write_settings(conn, enabled=True)
        _trade(conn, oid="t", exit_ms=NOW - 60_000, net=-3.2)
        first = evaluate_pause(conn, futures=_futures(), now_ms=NOW, send=lambda t, **_k: True)
        assert first["reason"] == "day_loss"
        write_settings(conn, enabled=False)
        conn.close()

    conn_a = open_db(path_a)
    text_a = toggle_enabled(conn_a, now_ms_value=NOW)
    snap_a = _snap(conn_a)
    conn_a.close()

    conn_b = open_db(path_b)
    sent_b, send_b = _send_capture()
    out_b = assist_toggle_confirm(conn_b, now_ms=NOW, send=send_b)
    snap_b = _snap(conn_b)
    assert out_b.get("applied") is True
    assert out_b.get("text") == text_a
    assert snap_b == snap_a
    conn_b.close()

    # OFF direction
    path_c = tmp_path / "c.db"
    path_d = tmp_path / "d.db"
    for path in (path_c, path_d):
        conn = open_db(path)
        write_settings(conn, enabled=True)
        conn.close()

    conn_c = open_db(path_c)
    text_c = toggle_enabled(conn_c, now_ms_value=NOW)
    snap_c = _snap(conn_c)
    conn_c.close()

    conn_d = open_db(path_d)
    sent_d, send_d = _send_capture()
    out_d = assist_toggle_confirm(conn_d, now_ms=NOW, send=send_d)
    snap_d = _snap(conn_d)
    assert out_d.get("applied") is True
    assert out_d.get("text") == text_c
    assert snap_d == snap_c
    assert sent_d  # result message sent
    del sent_b
    conn_d.close()


def test_ttl_expiry_and_boundary(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "ttl.db")
    write_settings(conn, enabled=True)
    sent, send = _send_capture()
    handle_menu_text(conn, BTN_TOGGLE, now_ms=NOW, send=send)
    data = _confirm_data_from_sent(sent)
    ts_s = int(data.rsplit(":", 1)[-1])
    before = _snap(conn)

    answers: list[str | None] = []
    edits: list[tuple[Any, ...]] = []

    def answer(text: str | None = None) -> None:
        answers.append(text)

    def edit(mid: int, text: str, *, reply_markup: Any = None) -> bool:
        edits.append((mid, text, reply_markup))
        return True

    expired = handle_toggle_confirm_callback(
        conn,
        data,
        send=send,
        answer=answer,
        edit=edit,
        message={"message_id": 9},
        now_ms=(ts_s + 61) * 1000,
    )
    assert expired["reason"] == "expired"
    assert _snap(conn) == before
    assert any(a and "หมดเวลา" in a for a in answers)

    answers.clear()
    edits.clear()
    n_sent = len(sent)
    ok = handle_toggle_confirm_callback(
        conn,
        f"at:off:{ts_s}",
        send=send,
        answer=answer,
        edit=edit,
        message={"message_id": 10},
        now_ms=(ts_s + TOGGLE_CONFIRM_TTL_S) * 1000,
    )
    assert ok.get("applied") is True
    assert ensure_settings(conn)["enabled"] is False
    assert len(sent) == n_sent + 1

    write_settings(conn, enabled=True)
    before2 = _snap(conn)
    future_ts = (NOW // 1000) + 10
    future = handle_toggle_confirm_callback(
        conn,
        f"at:off:{future_ts}",
        send=send,
        answer=answer,
        edit=edit,
        message={"message_id": 11},
        now_ms=NOW,
    )
    assert future["reason"] == "expired"
    assert _snap(conn) == before2
    conn.close()


def test_cancel_no_change_and_menu_style(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "cancel.db")
    write_settings(conn, enabled=True)
    sent, send = _send_capture()
    handle_menu_text(conn, BTN_TOGGLE, now_ms=NOW, send=send)
    data = _confirm_data_from_sent(sent)
    ts_s = data.rsplit(":", 1)[-1]
    cancel_data = f"at:no:{ts_s}"
    before = _snap(conn)
    edits: list[tuple[Any, ...]] = []

    def answer(text: str | None = None) -> None:
        return None

    def edit(mid: int, text: str, *, reply_markup: Any = None) -> bool:
        edits.append((mid, text, reply_markup))
        return True

    out = handle_toggle_confirm_callback(
        conn,
        cancel_data,
        send=send,
        answer=answer,
        edit=edit,
        message={"message_id": 3},
        now_ms=NOW,
    )
    assert out.get("cancelled") is True
    assert _snap(conn) == before
    assert edits and edits[0][2] == {"inline_keyboard": []}
    status = sent[-1]
    assert status["text"] == "ผู้ช่วยยังเปิดอยู่"
    assert status["reply_markup"]["keyboard"][2][1].get("style") == "success"

    write_settings(conn, enabled=False)
    sent2, send2 = _send_capture()
    handle_menu_text(conn, BTN_TOGGLE, now_ms=NOW, send=send2)
    ts2 = _confirm_data_from_sent(sent2).rsplit(":", 1)[-1]
    handle_toggle_confirm_callback(
        conn,
        f"at:no:{ts2}",
        send=send2,
        answer=answer,
        edit=edit,
        message={"message_id": 4},
        now_ms=NOW,
    )
    assert sent2[-1]["text"] == "ผู้ช่วยยังปิดอยู่"
    assert "style" not in sent2[-1]["reply_markup"]["keyboard"][2][1]
    conn.close()


def test_stale_second_off_confirm(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "stale.db")
    write_settings(conn, enabled=True)
    sent, send = _send_capture()
    # Issue two OFF confirms before either is applied
    handle_menu_text(conn, BTN_TOGGLE, now_ms=NOW, send=send)
    data1 = _confirm_data_from_sent(sent)
    handle_menu_text(conn, BTN_TOGGLE, now_ms=NOW, send=send)
    data2 = _confirm_data_from_sent(sent)
    assert data1 == data2

    answers: list[str | None] = []

    def answer(text: str | None = None) -> None:
        answers.append(text)

    def edit(mid: int, text: str, *, reply_markup: Any = None) -> bool:
        return True

    first = handle_toggle_confirm_callback(
        conn, data1, send=send, answer=answer, edit=edit, message={"message_id": 1}, now_ms=NOW
    )
    assert first.get("applied") is True
    assert ensure_settings(conn)["enabled"] is False
    n = len(sent)
    answers.clear()
    second = handle_toggle_confirm_callback(
        conn, data2, send=send, answer=answer, edit=edit, message={"message_id": 2}, now_ms=NOW
    )
    assert second.get("reason") == "stale"
    assert ensure_settings(conn)["enabled"] is False
    assert len(sent) == n
    conn.close()


def test_double_tap_confirm_idempotent_with_pause(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "dbl.db")
    write_settings(conn, enabled=True)
    _trade(conn, oid="t", exit_ms=NOW - 60_000, net=-3.2)
    evaluate_pause(
        conn,
        futures=_futures(),
        now_ms=NOW,
        send=lambda t, **_k: True,
    )
    write_settings(conn, enabled=False)
    sent, send = _send_capture()
    handle_menu_text(conn, BTN_TOGGLE, now_ms=NOW, send=send)
    data = _confirm_data_from_sent(sent)
    assert data.startswith("at:onp:")

    def answer(text: str | None = None) -> None:
        return None

    def edit(mid: int, text: str, *, reply_markup: Any = None) -> bool:
        return True

    first = handle_toggle_confirm_callback(
        conn, data, send=send, answer=answer, edit=edit, message={"message_id": 1}, now_ms=NOW
    )
    assert first.get("applied") is True
    edge1 = dict(get_edge(conn))
    settings1 = dict(ensure_settings(conn))
    n = len([s for s in sent if s.get("reply_markup") and "keyboard" in (s["reply_markup"] or {})])
    second = handle_toggle_confirm_callback(
        conn, data, send=send, answer=answer, edit=edit, message={"message_id": 2}, now_ms=NOW
    )
    assert second.get("reason") == "stale"
    assert dict(ensure_settings(conn)) == settings1
    assert dict(get_edge(conn)) == edge1
    n2 = len([s for s in sent if s.get("reply_markup") and "keyboard" in (s["reply_markup"] or {})])
    assert n2 == n == 1
    conn.close()


@pytest.mark.parametrize(
    "setup,expect_warn,action,has_1r,has_week",
    [
        ("day_loss_today", True, "onp", True, False),
        ("expired_pause", False, "on", False, False),
        ("no_pause", False, "on", False, False),
        ("week_loss", True, "onp", False, True),
        ("streak", True, "onp", False, False),
        ("null_reason", True, "onp", False, False),
        ("enabled_paused", False, "off", False, False),
    ],
)
def test_warning_only_when_real(
    tmp_path: Path,
    setup: str,
    expect_warn: bool,
    action: str,
    has_1r: bool,
    has_week: bool,
) -> None:
    conn = open_db(tmp_path / f"warn_{setup}.db")
    until = NOW + 3_600_000
    if setup == "day_loss_today":
        write_settings(conn, enabled=False, paused_until_ms=until, pause_reason="day_loss")
        write_edge(conn, day_pause_id=bangkok_day_id(NOW))
    elif setup == "expired_pause":
        write_settings(conn, enabled=False, paused_until_ms=NOW - 1, pause_reason="day_loss")
        write_edge(conn, day_pause_id=bangkok_day_id(NOW))
    elif setup == "no_pause":
        write_settings(conn, enabled=False, paused_until_ms=None)
    elif setup == "week_loss":
        write_settings(conn, enabled=False, paused_until_ms=until, pause_reason="week_loss")
        write_edge(conn, day_pause_id=None)
    elif setup == "streak":
        write_settings(conn, enabled=False, paused_until_ms=until, pause_reason="streak")
    elif setup == "null_reason":
        write_settings(conn, enabled=False, paused_until_ms=until, pause_reason=None)
    else:
        write_settings(conn, enabled=True, paused_until_ms=until, pause_reason="day_loss")
        write_edge(conn, day_pause_id=bangkok_day_id(NOW))

    body, markup = render_toggle_confirm(conn, now_ms=NOW)
    data = markup["inline_keyboard"][0][0]["callback_data"]
    assert data.startswith(f"at:{action}:")
    if expect_warn:
        assert "⚠️" in body
    else:
        assert "⚠️" not in body
    if has_1r:
        assert "1R" in body and BOT_ENTRIES_ONLY_TH in body
    if has_week:
        assert "สัปดาห์นี้จะไม่หยุดพักเพราะขาดทุนรายสัปดาห์อีก" in body
    if setup == "streak":
        assert "แพ้ติดกัน 3 ไม้" in body
        assert "1R" not in body or "ขาดทุนเพิ่มอีก 1R" not in body
    if setup == "null_reason":
        assert "จะยกเลิกการหยุดพัก ที่ตั้งไว้ถึง" in body
        assert "(" not in body.split("จะยกเลิกการหยุดพัก", 1)[1].split("ที่ตั้งไว้ถึง", 1)[0]
    if setup == "enabled_paused":
        assert body.startswith("ยืนยันปิดผู้ช่วย?")
    conn.close()


def test_on_stale_pause_when_pause_appears_after_prompt(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "stale_pause.db")
    write_settings(conn, enabled=False)
    sent, send = _send_capture()
    handle_menu_text(conn, BTN_TOGGLE, now_ms=NOW, send=send)
    data = _confirm_data_from_sent(sent)
    assert data.startswith("at:on:")
    write_settings(conn, paused_until_ms=NOW + 3_600_000, pause_reason="streak")
    before = _snap(conn)

    def answer(text: str | None = None) -> None:
        return None

    def edit(mid: int, text: str, *, reply_markup: Any = None) -> bool:
        return True

    out = handle_toggle_confirm_callback(
        conn, data, send=send, answer=answer, edit=edit, message={"message_id": 1}, now_ms=NOW
    )
    assert out.get("reason") == "stale_pause"
    assert _snap(conn) == before
    assert ensure_settings(conn)["paused_until_ms"] == NOW + 3_600_000
    assert ensure_settings(conn)["pause_reason"] == "streak"
    conn.close()


def test_render_and_warning_are_read_only(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "ro.db")
    write_settings(conn, enabled=False, paused_until_ms=NOW + 1_000_000, pause_reason="week_loss")
    write_edge(conn, week_pause_id="2026-11-09")
    before = _snap(conn)
    pause_clear_warning(conn, now_ms=NOW)
    render_toggle_confirm(conn, now_ms=NOW)
    assert _snap(conn) == before
    conn.close()


def test_process_update_owner_vs_foreign(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "route.db")
    write_settings(conn, enabled=True)
    before = _snap(conn)
    ts = NOW // 1000
    data = f"at:off:{ts}"
    sent, send = _send_capture()
    answers: list[Any] = []
    edits: list[Any] = []

    def answer(cq_id: str, *, text: str | None = None) -> bool:
        answers.append((cq_id, text))
        return True

    def edit(mid: int, text: str, *, reply_markup: Any = None) -> bool:
        edits.append((mid, text, reply_markup))
        return True

    foreign = {
        "update_id": 1,
        "callback_query": {
            "id": "cq-f",
            "data": data,
            "message": {"message_id": 1, "chat": {"id": 999}, "text": "x"},
        },
    }
    out_f = process_update(conn, foreign, send=send, answer=answer, edit=edit, now_ms_value=NOW)
    assert out_f.get("ignored") == "foreign_chat"
    assert _snap(conn) == before
    assert answers == [] and edits == [] and sent == []

    owner = {
        "update_id": 2,
        "callback_query": {
            "id": "cq-o",
            "data": data,
            "message": {"message_id": 2, "chat": {"id": 42}, "text": "x"},
        },
    }
    out_o = process_update(conn, owner, send=send, answer=answer, edit=edit, now_ms_value=NOW)
    assert "unhandled_callback" not in out_o
    assert out_o["ok"] is True
    assert out_o["applied"] is True
    assert out_o["enabled"] is False
    assert ensure_settings(conn)["enabled"] is False
    assert answers == [("cq-o", None)]
    assert edits == [(2, "ปิดผู้ช่วย — ✅ ยืนยันแล้ว", {"inline_keyboard": []})]
    assert [s["text"] for s in sent] == [out_o["text"]]
    assert "style" not in sent[-1]["reply_markup"]["keyboard"][2][1]
    conn.close()


def test_bad_callback_data(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "bad.db")
    write_settings(conn, enabled=True)
    before = _snap(conn)
    sent, send = _send_capture()

    def answer(text: str | None = None) -> None:
        return None

    def edit(mid: int, text: str, *, reply_markup: Any = None) -> bool:
        return True

    for data in ("at:xx:1763280000", "at:on:abc", "at:on:"):
        out = handle_toggle_confirm_callback(
            conn, data, send=send, answer=answer, edit=edit, message={"message_id": 1}, now_ms=NOW
        )
        assert out.get("reason") == "bad_data"
        assert _snap(conn) == before
    conn.close()


def test_legacy_toggle_asks_confirm_no_refresh(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "legacy.db")
    write_settings(conn, enabled=True)
    before = _snap(conn)
    sent, send = _send_capture()
    out = handle_menu_text(conn, LEGACY_BTN_TOGGLE, now_ms=NOW, send=send)
    assert out.get("confirm") is True
    assert out.get("menu_refreshed") is not True
    assert all(s["text"] != "เมนูอัปเดตแล้ว" for s in sent)
    assert _snap(conn) == before
    conn.close()


def test_pause_clear_warning_space_before_numeric_date(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "thru.db")
    far = NOW + 3 * 86_400_000
    write_settings(conn, enabled=False, paused_until_ms=far, pause_reason="streak")
    label = bangkok_resume_label(far, now_ms=NOW)
    assert label[:1].isdigit()
    far_line = pause_clear_warning(conn, now_ms=NOW)[0]
    assert f"ที่ตั้งไว้ถึง {label}" in far_line

    near = NOW + 3_600_000
    write_settings(conn, paused_until_ms=near, pause_reason="streak")
    near_line = pause_clear_warning(conn, now_ms=NOW)[0]
    assert "ที่ตั้งไว้ถึงวันนี้" in near_line
    conn.close()


def test_legacy_toggle_confirm_menu_style_follows_enabled(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "legacy-style.db")
    write_settings(conn, enabled=True)
    sent, send = _send_capture()

    out_off = assist_toggle_confirm(conn, now_ms=NOW, send=send, button=LEGACY_BTN_TOGGLE)
    assert out_off.get("applied") is True
    assert ensure_settings(conn)["enabled"] is False
    assert sent[-1]["reply_markup"]["keyboard"][2][1] == {"text": BTN_TOGGLE}

    out_on = assist_toggle_confirm(conn, now_ms=NOW, send=send, button=LEGACY_BTN_TOGGLE)
    assert out_on.get("applied") is True
    assert ensure_settings(conn)["enabled"] is True
    assert sent[-1]["reply_markup"]["keyboard"][2][1] == {"text": BTN_TOGGLE, "style": "success"}
    conn.close()


def test_callback_data_at_most_64_bytes() -> None:
    for action in ("on", "onp", "off", "no"):
        kb = toggle_confirm_keyboard(action, 1763280000)
        for row in kb["inline_keyboard"]:
            for btn in row:
                raw = btn["callback_data"].encode("utf-8")
                assert len(raw) <= 64
                assert raw.decode().startswith(ASSIST_TOGGLE_PREFIX)
    # also cover next_0820 stamp size used in real pauses
    until = next_0820_bangkok_ms(NOW)
    kb2 = toggle_confirm_keyboard("onp", until // 1000)
    for row in kb2["inline_keyboard"]:
        for btn in row:
            assert len(btn["callback_data"].encode("utf-8")) <= 64
