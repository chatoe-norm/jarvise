"""Scrub full KYC text from the partner chat. Tmp key only."""

from __future__ import annotations

import logging
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from jarvise_assist import partner_kyc as pk
from jarvise_assist import partner_portal as portal
from jarvise_assist import partner_store as ps
from jarvise_ingest.db import open_db

D = Decimal
NOW = 1_700_000_000_000
PHONE = "0812345678"
ACCOUNT = "1234567890"
REAL_NAME = "สมชาย ใจดี"


def _form(
    *,
    real_name: str = REAL_NAME,
    phone: str = "081-234-5678",
    account: str = "123-4-56789-0",
) -> str:
    return (
        f"ข้อมูลรับเงิน\nชื่อจริง: {real_name}\nเบอร์: {phone}\nธนาคาร: กสิกร\nเลขบัญชี: {account}\nชื่อบัญชี: {real_name}\n"
    )


def _no_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_TEST_NO_KYC_SEED", "1")


def _partner(conn: sqlite3.Connection, name: str = "Bob", *, admin: bool = False) -> str:
    created = ps.create_partner(conn, name=name, is_admin=admin, now_ms=NOW)
    assert created["ok"] is True
    return str(created["partner_id"])


def _bind(conn: sqlite3.Connection, pid: str, tg: str = "501") -> None:
    invited = ps.create_invite(conn, pid, now_ms=NOW)
    assert invited["ok"] is True
    bound = ps.bind_invite(conn, invited["code"], tg, admin_chat_id="42", now_ms=NOW)
    assert bound["ok"] is True


def _send_capture() -> tuple[list[dict[str, Any]], Any]:
    sent: list[dict[str, Any]] = []

    def send(
        text: str, *, reply_markup: dict[str, Any] | None = None, chat_id: str | None = None, **_k: Any
    ) -> int:
        sent.append({"text": text, "reply_markup": reply_markup, "chat_id": chat_id})
        return len(sent)

    return sent, send


def _form_message(text: str, *, mid: int = 9, tg: str = "501") -> dict[str, Any]:
    return {
        "message_id": mid,
        "chat": {"id": int(tg), "type": "private"},
        "from": {"id": int(tg)},
        "text": text,
    }


def _callback(data: str, *, mid: int = 3, tg: str = "501") -> dict[str, Any]:
    return {
        "id": "cq",
        "from": {"id": int(tg)},
        "message": {"message_id": mid, "chat": {"id": int(tg), "type": "private"}, "text": "confirm"},
        "data": data,
    }


def test_raw_form_is_deleted_on_invalid_locked_and_key_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _no_seed(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "scrub-form.db")
    bob = _partner(conn)
    _bind(conn, bob)
    deleted: list[tuple[str, int]] = []
    sent, send = _send_capture()

    def delete(chat_id: str, message_id: int) -> None:
        deleted.append((str(chat_id), int(message_id)))
        raise RuntimeError(f"{PHONE} {ACCOUNT} {REAL_NAME}")

    caplog.set_level(logging.INFO)
    invalid = portal.handle_portal_text(
        conn,
        _form_message(_form(phone="021234567")),
        send=send,
        now_ms=NOW + 1,
        delete=delete,
    )
    assert invalid["reason"] == "kyc_invalid"
    assert deleted == [("501", 9)]
    assert PHONE not in caplog.text
    assert ACCOUNT not in caplog.text
    assert REAL_NAME not in caplog.text

    assert pk.admin_set_kyc(conn, bob, _form(), "ตั้งครั้งแรก", NOW + 2)["ok"] is True
    deleted.clear()
    locked = portal.handle_portal_text(
        conn,
        _form_message(_form(), mid=10),
        send=send,
        now_ms=NOW + 3,
        delete=lambda chat_id, message_id: deleted.append((str(chat_id), int(message_id))),
    )
    assert locked["reason"] == "kyc_locked"
    assert deleted == [("501", 10)]

    monkeypatch.setenv("JARVISE_PARTNER_KYC_KEY_PATH", str(tmp_path / "missing.key"))
    cara = _partner(conn, "Cara")
    _bind(conn, cara, "502")
    deleted.clear()
    missing = portal.handle_portal_text(
        conn,
        _form_message(_form(phone="0890000000", account="2222222222"), mid=11, tg="502"),
        send=send,
        now_ms=NOW + 4,
        delete=lambda chat_id, message_id: deleted.append((str(chat_id), int(message_id))),
    )
    assert missing["reason"] == "kyc_key"
    assert deleted == [("502", 11)]
    assert "2222222222" not in caplog.text
    conn.close()


def test_confirm_message_is_retired_when_the_edit_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_seed(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "scrub-cb.db")
    bob = _partner(conn)
    _bind(conn, bob)
    staged = pk.stage_draft(conn, bob, _form(), NOW)
    assert staged["ok"] is True
    token = str(staged["token"])
    edited: list[str] = []
    deleted: list[tuple[str, int]] = []
    sent, send = _send_capture()

    def edit_ok(
        _mid: int, text: str, *, reply_markup: dict[str, Any] | None = None, chat_id: str | None = None
    ) -> bool:
        edited.append(text)
        return True

    expired = portal.handle_portal_callback(
        conn,
        f"pp:kyc:ok:{token}",
        callback=_callback(f"pp:kyc:ok:{token}", mid=7),
        send=send,
        answer=lambda *_a, **_k: True,
        edit=edit_ok,
        delete=lambda chat_id, message_id: deleted.append((str(chat_id), int(message_id))),
        now_ms=NOW + pk.DRAFT_TTL_MS,
    )
    assert expired["reason"] == "kyc_draft_expired"
    assert edited
    assert ACCOUNT not in edited[-1]
    assert PHONE not in edited[-1]
    assert deleted == []

    again = pk.stage_draft(conn, bob, _form(), NOW + 1)
    assert again["ok"] is True
    edited.clear()
    deleted.clear()
    sent.clear()

    def edit_fail(*_a: Any, **_k: Any) -> bool:
        return False

    failed = portal.handle_portal_callback(
        conn,
        f"pp:kyc:ok:{again['token']}",
        callback=_callback(f"pp:kyc:ok:{again['token']}", mid=8),
        send=send,
        answer=lambda *_a, **_k: True,
        edit=edit_fail,
        delete=lambda chat_id, message_id: deleted.append((str(chat_id), int(message_id))),
        now_ms=NOW + 1 + pk.DRAFT_TTL_MS,
    )
    assert failed["reason"] == "kyc_draft_expired"
    assert deleted == [("501", 8)]
    assert sent
    assert ACCOUNT not in sent[-1]["text"]
    assert PHONE not in sent[-1]["text"]

    fresh = pk.stage_draft(conn, bob, _form(), NOW + 2)
    assert fresh["ok"] is True
    order: list[str] = []
    deleted.clear()
    sent.clear()

    def edit_redo(*_a: Any, **_k: Any) -> bool:
        order.append("edit")
        return False

    def delete_redo(chat_id: str, message_id: int) -> None:
        order.append("delete")
        deleted.append((str(chat_id), int(message_id)))

    def send_redo(
        text: str, *, reply_markup: dict[str, Any] | None = None, chat_id: str | None = None, **_k: Any
    ) -> int:
        order.append("send")
        sent.append({"text": text, "chat_id": chat_id, "reply_markup": reply_markup})
        return len(sent)

    redo = portal.handle_portal_callback(
        conn,
        f"pp:kyc:re:{fresh['token']}",
        callback=_callback(f"pp:kyc:re:{fresh['token']}", mid=9),
        send=send_redo,
        answer=lambda *_a, **_k: True,
        edit=edit_redo,
        delete=delete_redo,
        now_ms=NOW + 3,
    )
    assert redo["ok"] is True
    assert deleted == [("501", 9)]
    assert order == ["edit", "delete", "send"]
    assert "ข้อมูลรับเงิน" in sent[-1]["text"]
    kept = conn.execute("SELECT token FROM partner_kyc_draft WHERE partner_id = ?", (bob,)).fetchone()
    assert kept is None

    saved_draft = pk.stage_draft(conn, bob, _form(), NOW + 4)
    assert saved_draft["ok"] is True
    saved_order: list[str] = []
    deleted.clear()
    sent.clear()

    def delete_saved(chat_id: str, message_id: int) -> None:
        saved_order.append("delete")
        deleted.append((str(chat_id), int(message_id)))

    def send_saved(
        text: str, *, reply_markup: dict[str, Any] | None = None, chat_id: str | None = None, **_k: Any
    ) -> int:
        saved_order.append("send")
        sent.append({"text": text, "chat_id": chat_id, "reply_markup": reply_markup})
        return len(sent)

    saved = portal.handle_portal_callback(
        conn,
        f"pp:kyc:ok:{saved_draft['token']}",
        callback=_callback(f"pp:kyc:ok:{saved_draft['token']}", mid=10),
        send=send_saved,
        answer=lambda *_a, **_k: True,
        edit=edit_fail,
        delete=delete_saved,
        now_ms=NOW + 5,
    )
    assert saved["ok"] is True
    assert deleted == [("501", 10)]
    assert saved_order[:2] == ["delete", "send"]
    partner_sends = [row["text"] for row in sent if row["chat_id"] == "501"]
    assert partner_sends
    assert "บันทึกแล้ว แก้ไขเองไม่ได้" in partner_sends[-1]
    for text in partner_sends:
        assert ACCOUNT not in text
        assert PHONE not in text
    conn.close()


def test_callback_without_delete_still_works(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_seed(monkeypatch)
    conn = open_db(tmp_path / "scrub-old.db")
    bob = _partner(conn)
    _bind(conn, bob)
    staged = pk.stage_draft(conn, bob, _form(), NOW)
    assert staged["ok"] is True
    sent, send = _send_capture()
    out = portal.handle_portal_callback(
        conn,
        f"pp:kyc:ok:{staged['token']}",
        callback=_callback(f"pp:kyc:ok:{staged['token']}"),
        send=send,
        answer=lambda *_a, **_k: True,
        edit=lambda *_a, **_k: True,
        now_ms=NOW + pk.DRAFT_TTL_MS,
    )
    assert out["reason"] == "kyc_draft_expired"
    assert sent == []
    conn.close()


def test_direct_payout_stores_the_current_kyc_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_seed(monkeypatch)
    conn = open_db(tmp_path / "scrub-pay.db")
    admin = _partner(conn, "admin", admin=True)
    bob = _partner(conn)
    assert pk.admin_set_kyc(conn, bob, _form(), "ตั้งครั้งแรก", NOW)["ok"] is True
    current = pk.current_kyc(conn, bob)
    assert current is not None
    assert ps.apply_deposit(conn, admin, D("1000"), D("1000"), NOW + 1)["ok"] is True
    assert ps.apply_deposit(conn, bob, D("200"), D("1200"), NOW + 2)["ok"] is True
    withdrawn = ps.apply_withdraw_profit(conn, bob, D("1440"), NOW + 3)
    assert withdrawn["ok"] is True
    assert D(withdrawn["cash"]) > 0
    wd_row = conn.execute(
        "SELECT payout_kyc_id FROM partner_request WHERE id = ?",
        (withdrawn["request_id"],),
    ).fetchone()
    assert wd_row["payout_kyc_id"] == current["id"]
    conn.close()

    conn = open_db(tmp_path / "scrub-close.db")
    admin = _partner(conn, "admin", admin=True)
    cara = _partner(conn, "Cara")
    assert pk.admin_set_kyc(conn, cara, _form(phone="0891111111", account="3333333333"), "ตั้งครั้งแรก", NOW)[
        "ok"
    ]
    cara_kyc = pk.current_kyc(conn, cara)
    assert cara_kyc is not None
    assert ps.apply_deposit(conn, admin, D("1000"), D("1000"), NOW + 1)["ok"] is True
    assert ps.apply_deposit(conn, cara, D("1000"), D("2000"), NOW + 2)["ok"] is True
    closed = ps.apply_close(conn, cara, D("2000"), NOW + 3)
    assert closed["ok"] is True
    assert D(closed["cash"]) > 0
    cl_row = conn.execute(
        "SELECT payout_kyc_id FROM partner_request WHERE id = ?",
        (closed["request_id"],),
    ).fetchone()
    assert cl_row["payout_kyc_id"] == cara_kyc["id"]
    conn.close()
