"""Partner payout KYC (migration 33). Tmp key only; never a real key file."""

from __future__ import annotations

import base64
import logging
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from jarvise_assist import partner_admin as pa
from jarvise_assist import partner_kyc as pk
from jarvise_assist import partner_portal as portal
from jarvise_assist import partner_store as ps
from jarvise_assist.partner_value import AccountValue
from jarvise_ingest.db import SCHEMA_VERSION, migrate, open_db, schema_version

D = Decimal
NOW = 1_700_000_000_000
PHONE = "0812345678"
ACCOUNT = "1234567890"


def _form(
    *,
    real_name: str = "สมชาย ใจดี",
    phone: str = "081-234-5678",
    bank: str = "กสิกร",
    account: str = "123-4-56789-0",
    holder: str = "สมชาย ใจดี",
    real_label: str = "ชื่อจริง",
    phone_label: str = "เบอร์",
    bank_label: str = "ธนาคาร",
    account_label: str = "เลขบัญชี",
    holder_label: str = "ชื่อบัญชี",
    sep: str = ":",
) -> str:
    return (
        "ข้อมูลรับเงิน\n"
        f"{real_label}{sep} {real_name}\n"
        f"{phone_label}{sep} {phone}\n"
        f"{bank_label}{sep} {bank}\n"
        f"{account_label}{sep} {account}\n"
        f"{holder_label}{sep} {holder}\n"
    )


def _no_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_TEST_NO_KYC_SEED", "1")


def _partner(conn: sqlite3.Connection, name: str = "Bob", *, admin: bool = False) -> str:
    created = ps.create_partner(conn, name=name, is_admin=admin, now_ms=NOW)
    assert created["ok"] is True
    return str(created["partner_id"])


def _blob_columns(conn: sqlite3.Connection, table: str) -> list[bytes]:
    rows = conn.execute(
        f"SELECT real_name_enc, phone_enc, account_no_enc, holder_name_enc FROM {table}"
    ).fetchall()
    out: list[bytes] = []
    for row in rows:
        out.extend(bytes(col) for col in row)
    return out


def test_migration_32_to_33_is_additive_and_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_seed(monkeypatch)
    path = tmp_path / "k.db"
    conn = open_db(path)
    assert SCHEMA_VERSION == 33
    assert schema_version(conn) == 33
    conn.execute("DROP TABLE partner_kyc_draft")
    conn.execute("DROP TABLE partner_kyc")
    conn.execute("PRAGMA user_version = 32")
    conn.commit()
    conn.close()

    conn = open_db(path)
    assert schema_version(conn) == 33
    assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='partner_kyc'").fetchone()
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(partner_request)")}
    assert "payout_kyc_id" in cols
    assert "source" in cols
    before = conn.total_changes
    assert migrate(conn) == []
    assert conn.total_changes == before

    pid = _partner(conn)
    saved = pk.admin_set_kyc(conn, pid, _form(), "ตั้งครั้งแรก", NOW)
    assert saved["ok"] is True
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM partner_kyc")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE partner_kyc SET bank_code = 'SCB'")
    assert conn.execute("SELECT COUNT(*) AS n FROM partner_kyc").fetchone()["n"] == 1
    conn.close()


def test_crypto_round_trip_aad_and_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_seed(monkeypatch)
    conn = open_db(tmp_path / "c.db")
    pid = _partner(conn)
    staged = pk.stage_draft(conn, pid, _form(), NOW)
    assert staged["ok"] is True
    for blob in _blob_columns(conn, "partner_kyc_draft"):
        assert PHONE.encode() not in blob
        assert ACCOUNT.encode() not in blob
        assert "สมชาย ใจดี".encode() not in blob
    committed = pk.commit_draft(conn, pid, str(staged["token"]), NOW + 1)
    assert committed["ok"] is True
    for blob in _blob_columns(conn, "partner_kyc"):
        assert PHONE.encode() not in blob
        assert ACCOUNT.encode() not in blob
        assert "สมชาย ใจดี".encode() not in blob
    revealed = pk.reveal_current(conn, pid)
    assert revealed["versions"][0]["phone"] == PHONE
    assert revealed["versions"][0]["account_no"] == ACCOUNT

    master = pk._master_key()
    blob = pk._encrypt(master, pk._aad("pk_aaaaaaaaaaaaaaaa", "phone"), PHONE)
    assert pk._decrypt(master, pk._aad("pk_aaaaaaaaaaaaaaaa", "phone"), blob) == PHONE
    with pytest.raises(pk.KycCryptoError, match=pk.KYC_DECRYPT_MESSAGE):
        pk._decrypt(master, pk._aad("pk_bbbbbbbbbbbbbbbb", "phone"), blob)

    other = tmp_path / "other.key"
    other.write_text(base64.b64encode(b"\x02" * 32).decode("ascii"), encoding="utf-8")
    monkeypatch.setenv("JARVISE_PARTNER_KYC_KEY_PATH", str(other))
    with pytest.raises(pk.KycCryptoError):
        pk.reveal_current(conn, pid)
    fresh = _partner(conn, "Dee")
    monkeypatch.setenv("JARVISE_PARTNER_KYC_KEY_PATH", str(tmp_path / "missing.key"))
    with pytest.raises(pk.KycKeyError, match=pk.KYC_KEY_MESSAGE):
        pk.stage_draft(conn, fresh, _form(phone="0899999999", account="3333333333"), NOW + 2)
    assert "missing.key" not in pk.KYC_KEY_MESSAGE
    conn.close()


def test_parser_aliases_masks_and_flags() -> None:
    parsed = pk.parse_form(
        _form(
            real_label="ชื่อ-นามสกุล",
            phone_label="มือถือ",
            bank_label="แบงก์",
            account_label="บัญชี",
            holder_label="ชื่อบัญชี",
            sep="：",
            phone="+66 81 234 5678",
            bank="ไทยพาณิชย์",
            account="123456789012",
            holder="นาย สมชาย ใจดี",
        )
    )
    assert parsed["ok"] is True
    assert parsed["bank_code"] == "SCB"
    assert parsed["phone"] == PHONE
    assert parsed["account_no"] == "123456789012"
    assert parsed["holder_matches"] is True
    assert parsed["account_length_warn"] is True

    gsb = pk.parse_form(_form(bank="ออมสิน", account="123456789012"))
    assert gsb["ok"] is True
    assert gsb["bank_code"] == "GSB"
    assert gsb["account_length_warn"] is False
    short_gsb = pk.parse_form(_form(bank="GSB", account="1234567890"))
    assert short_gsb["account_length_warn"] is True

    landline = pk.parse_form(_form(phone="021234567"))
    assert landline["ok"] is False
    assert "เบอร์ต้องเป็นมือถือไทย" in landline["errors"]

    mismatch = pk.parse_form(_form(holder="สมหญิง ใจดี"))
    assert mismatch["ok"] is True
    assert mismatch["holder_matches"] is False

    zw = pk.parse_form(_form(real_name="สมชาย\u200b ใจดี"))
    assert zw["ok"] is True
    assert zw["real_name"] == "สมชาย ใจดี"

    masked = pk.mask_name("สมชาย ก\u0e49า")
    assert masked == "สมชาย ก\u0e49***"
    assert pk.mask_phone("5678") == "xxx-xxx-5678"
    assert pk.mask_account("KBANK", "7890") == "KBANK ••••7890"

    promptpay = pk.parse_form(_form(bank="พร้อมเพย์"))
    assert promptpay["ok"] is False
    assert "ไม่รู้จักธนาคาร" in promptpay["errors"]

    both = pk.parse_form("ข้อมูลรับเงิน\nชื่อจริง: สมชาย ใจดี\nชื่อ: สมชาย ใจดี\n")
    assert both["ok"] is False
    assert "มีช่องซ้ำ" in both["errors"]
    assert "ขาดเบอร์มือถือ" in both["errors"]
    assert PHONE not in "".join(both["errors"])


def test_confirm_flow_locks_after_save(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_seed(monkeypatch)
    conn = open_db(tmp_path / "f.db")
    pid = _partner(conn)
    staged = pk.stage_draft(conn, pid, _form(), NOW)
    assert staged["ok"] is True
    assert len(str(staged["token"])) == 8
    assert conn.execute("SELECT COUNT(*) AS n FROM partner_kyc").fetchone()["n"] == 0
    assert pk.kyc_state(conn, pid) == "none"

    redo = pk.discard_draft(conn, pid)
    assert redo["ok"] is True
    assert conn.execute("SELECT COUNT(*) AS n FROM partner_kyc_draft").fetchone()["n"] == 0
    staged = pk.stage_draft(conn, pid, _form(), NOW)
    token = str(staged["token"])
    expired = pk.commit_draft(conn, pid, token, NOW + pk.DRAFT_TTL_MS)
    assert expired == {"ok": False, "reason": "kyc_draft_expired"}
    assert conn.execute("SELECT COUNT(*) AS n FROM partner_kyc").fetchone()["n"] == 0

    staged = pk.stage_draft(conn, pid, _form(), NOW + 1)
    saved = pk.commit_draft(conn, pid, str(staged["token"]), NOW + 2)
    assert saved["ok"] is True
    assert saved["kyc_id"].startswith("pk_")
    assert conn.execute("SELECT COUNT(*) AS n FROM partner_kyc_draft").fetchone()["n"] == 0
    row = conn.execute("SELECT version, source FROM partner_kyc").fetchone()
    assert row["version"] == 1
    assert row["source"] == "portal"
    again = pk.stage_draft(conn, pid, _form(phone="0890000000"), NOW + 3)
    assert again["reason"] == "kyc_locked"
    assert conn.execute("SELECT COUNT(*) AS n FROM partner_kyc").fetchone()["n"] == 1

    closed = _partner(conn, "Cara")
    conn.execute("UPDATE partner SET status = 'closed' WHERE id = ?", (closed,))
    refused = pk.stage_draft(conn, closed, _form(phone="0890000001", account="2222222222"), NOW + 5)
    assert refused["reason"] == "closed"
    admin = _partner(conn, "admin", admin=True)
    assert pk.stage_draft(conn, admin, _form(), NOW + 6)["reason"] == "admin_exempt"
    conn.close()


def test_gate_blocks_only_the_first_deposit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_seed(monkeypatch)
    conn = open_db(tmp_path / "g.db")
    admin = _partner(conn, "admin", admin=True)
    assert ps.apply_deposit(conn, admin, D("1000"), D("1000"), NOW)["ok"] is True
    bob = _partner(conn)
    refused = ps.apply_deposit(conn, bob, D("100"), D("1100"), NOW + 1)
    assert refused == {"ok": False, "reason": "kyc_required"}
    created = ps.create_request(conn, bob, "deposit", NOW + 2, amount=D("100"), source="portal")
    assert created == {"ok": False, "reason": "kyc_required"}

    real = ps.deposit_kyc_required
    monkeypatch.setattr(ps, "deposit_kyc_required", lambda *_a, **_k: False)
    pending = ps.create_request(conn, bob, "deposit", NOW + 3, amount=D("100"), source="admin")
    monkeypatch.setattr(ps, "deposit_kyc_required", real)
    assert pending["ok"] is True
    confirmed = ps.confirm_request(
        conn, str(pending["request_id"]), D("1100"), NOW + 4, amount_received=D("100")
    )
    assert confirmed == {"ok": False, "reason": "kyc_required"}
    status = conn.execute(
        "SELECT status FROM partner_request WHERE id = ?",
        (pending["request_id"],),
    ).fetchone()
    assert status["status"] == "pending"

    still = ps.apply_deposit(conn, bob, D("100"), D("1100"), NOW + 5)
    assert still == {"ok": False, "reason": "kyc_required"}
    monkeypatch.setattr(ps, "deposit_kyc_required", lambda *_a, **_k: False)
    assert ps.apply_deposit(conn, bob, D("100"), D("1100"), NOW + 6)["ok"] is True
    monkeypatch.setattr(ps, "deposit_kyc_required", real)
    assert ps.deposit_kyc_required(conn, ps.get_partner(conn, bob)) is False
    second = ps.apply_deposit(conn, bob, D("50"), D("1250"), NOW + 7)
    assert second["ok"] is True
    conn.close()


def _bind(conn: sqlite3.Connection, pid: str, tg: str = "501") -> None:
    invited = ps.create_invite(conn, pid, now_ms=NOW)
    assert invited["ok"] is True
    bound = ps.bind_invite(conn, invited["code"], tg, admin_chat_id="42", now_ms=NOW)
    assert bound["ok"] is True


def _portal_send() -> tuple[list[dict[str, Any]], Any]:
    sent: list[dict[str, Any]] = []

    def send(
        text: str, *, reply_markup: dict[str, Any] | None = None, chat_id: str | None = None, **_k: Any
    ) -> int:
        sent.append({"text": text, "reply_markup": reply_markup, "chat_id": chat_id})
        return len(sent)

    return sent, send


def test_portal_confirm_masks_admin_notice_and_blocks_deposit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_seed(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "p.db")
    admin = _partner(conn, "admin", admin=True)
    ps.apply_deposit(conn, admin, D("1000"), D("1000"), NOW)
    bob = _partner(conn, "Bob")
    _bind(conn, bob)
    deleted: list[tuple[str, int]] = []
    sent, send = _portal_send()
    portal.handle_portal_text(
        conn,
        {
            "message_id": 9,
            "chat": {"id": 501, "type": "private"},
            "from": {"id": 501, "first_name": "Bob"},
            "text": _form(),
        },
        send=send,
        now_ms=NOW + 1,
        delete=lambda chat_id, message_id: deleted.append((str(chat_id), int(message_id))),
    )
    assert deleted == [("501", 9)]
    assert conn.execute("SELECT COUNT(*) AS n FROM partner_kyc").fetchone()["n"] == 0
    partner_msgs = [row for row in sent if row["chat_id"] == "501"]
    assert partner_msgs
    assert PHONE in partner_msgs[-1]["text"]
    assert ACCOUNT in partner_msgs[-1]["text"]
    assert pk.KYC_WARNING in partner_msgs[-1]["text"]
    buttons = partner_msgs[-1]["reply_markup"]["inline_keyboard"][0]
    ok_data = buttons[0]["callback_data"]
    assert ok_data.startswith("pp:kyc:ok:")
    assert len(ok_data) <= 64
    token = ok_data.rsplit(":", 1)[1]
    edited: list[str] = []

    def edit(
        message_id: int, text: str, *, reply_markup: dict[str, Any] | None = None, chat_id: str | None = None
    ) -> bool:
        edited.append(text)
        return True

    portal.handle_portal_callback(
        conn,
        ok_data,
        callback={
            "id": "cq",
            "from": {"id": 501},
            "message": {"message_id": 3, "chat": {"id": 501, "type": "private"}, "text": "confirm"},
        },
        send=send,
        answer=lambda *_a, **_k: True,
        edit=edit,
        now_ms=NOW + 2,
    )
    assert edited
    assert "บันทึกแล้ว แก้ไขเองไม่ได้" in edited[0]
    assert PHONE not in edited[0]
    assert ACCOUNT not in edited[0]
    assert token
    admin_msgs = [row["text"] for row in sent if row["chat_id"] is None]
    assert admin_msgs
    assert PHONE not in admin_msgs[-1]
    assert ACCOUNT not in admin_msgs[-1]
    assert "ดูเต็ม: p kyc Bob" in admin_msgs[-1]
    assert pk.kyc_state(conn, bob) == "saved"

    sent.clear()
    portal.handle_portal_text(
        conn,
        {
            "message_id": 10,
            "chat": {"id": 501, "type": "private"},
            "from": {"id": 501},
            "text": "ฝาก 100",
        },
        send=send,
        now_ms=NOW + 3,
    )
    assert any("แจ้งฝาก" in row["text"] for row in sent)

    cara = _partner(conn, "Cara")
    _bind(conn, cara, "502")
    sent.clear()
    portal.handle_portal_text(
        conn,
        {
            "message_id": 11,
            "chat": {"id": 502, "type": "private"},
            "from": {"id": 502},
            "text": "ฝาก 100",
        },
        send=send,
        now_ms=NOW + 4,
    )
    assert any("อย่าโอนเงินก่อน" in row["text"] for row in sent)
    conn.close()


def test_admin_set_notifies_masked_and_has_no_verify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_seed(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "a.db")
    _partner(conn, "admin", admin=True)
    bob = _partner(conn, "Bob")
    _bind(conn, bob)
    sent: list[dict[str, Any]] = []

    def send(
        text: str, *, reply_markup: dict[str, Any] | None = None, chat_id: str | None = None, **_k: Any
    ) -> int:
        sent.append({"text": text, "chat_id": chat_id, "reply_markup": reply_markup})
        return len(sent)

    message = {"chat": {"id": 42, "type": "private"}, "from": {"id": 42}}
    bad = pa.handle_partner_text(conn, "p kyc Bob ok", message=message, send=send, now_ms=NOW)
    assert bad["reason"] == "usage"
    assert pk.kyc_state(conn, bob) == "none"
    text = "p kyc Bob set แก้ชื่อตามคำขอ\n" + _form(holder="สมหญิง ใจดี")
    out = pa.handle_partner_text(conn, text, message=message, send=send, now_ms=NOW + 1)
    assert out["ok"] is True
    assert out["version"] == 1
    partner_note = [row["text"] for row in sent if row["chat_id"] == "501"]
    assert partner_note
    assert "แอดมินแก้ไขข้อมูลรับเงินของคุณแล้ว" in partner_note[-1]
    assert PHONE not in partner_note[-1]
    assert ACCOUNT not in partner_note[-1]
    short = pa.handle_partner_text(
        conn,
        "p kyc Bob set สั้น\n" + _form(phone="0891111111", account="1111111111"),
        message=message,
        send=send,
        now_ms=NOW + 2,
    )
    assert short["reason"] == "reason_short"
    shown = pa.handle_partner_text(conn, "p kyc Bob", message=message, send=send, now_ms=NOW + 3)
    assert shown["ok"] is True
    full = sent[-1]["text"]
    assert PHONE in full
    assert ACCOUNT in full
    listed = pa.handle_partner_text(conn, "p kyc", message=message, send=send, now_ms=NOW + 4)
    assert listed["ok"] is True
    overview = sent[-1]["text"]
    assert "บันทึกแล้ว" in overview
    assert "⚠️ ชื่อบัญชีไม่ตรง" in overview
    assert PHONE not in overview
    assert ACCOUNT not in overview
    assert "แอดมินไม่ต้องกรอก" in overview
    conn.close()


def test_payout_snapshot_and_portal_withdraw_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_seed(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "w.db")
    admin = _partner(conn, "admin", admin=True)
    ps.apply_deposit(conn, admin, D("1000"), D("1000"), NOW)
    bob = _partner(conn, "Bob")
    pk.admin_set_kyc(conn, bob, _form(), "ตั้งครั้งแรก", NOW)
    real = ps.deposit_kyc_required
    monkeypatch.setattr(ps, "deposit_kyc_required", lambda *_a, **_k: False)
    assert ps.apply_deposit(conn, bob, D("1000"), D("2000"), NOW + 1)["ok"] is True
    monkeypatch.setattr(ps, "deposit_kyc_required", real)
    _bind(conn, bob)
    created = ps.create_request(conn, bob, "withdraw_profit", NOW + 2, source="portal")
    # May refuse on profit rules; drive confirm through a request we insert after a profitable value.
    if not created.get("ok"):
        created = ps.create_request(conn, bob, "withdraw_profit", NOW + 2, source="admin")
    assert created["ok"] is True
    confirmed = ps.confirm_request(conn, str(created["request_id"]), D("4000"), NOW + 3)
    assert confirmed["ok"] is True
    assert D(confirmed["cash"]) > 0
    stored = conn.execute(
        "SELECT payout_kyc_id FROM partner_request WHERE id = ?",
        (created["request_id"],),
    ).fetchone()
    current = pk.current_kyc(conn, bob)
    assert stored["payout_kyc_id"] == current["id"]
    line = pk.payout_account_line(
        conn, partner_id=bob, payout_kyc_id=stored["payout_kyc_id"], partner_name="Bob"
    )
    assert "KBANK ••••7890" in line
    assert "เปลี่ยนหลังยืนยัน" not in line
    pk.admin_set_kyc(conn, bob, _form(account="9999999999", phone="0892222222"), "เปลี่ยนบัญชีตามคำขอ", NOW + 4)
    changed = pk.payout_account_line(
        conn, partner_id=bob, payout_kyc_id=stored["payout_kyc_id"], partner_name="Bob"
    )
    assert "เปลี่ยนหลังยืนยัน" in changed
    assert "p kyc Bob" in changed

    cara = _partner(conn, "Cara")
    monkeypatch.setattr(ps, "deposit_kyc_required", lambda *_a, **_k: False)
    ps.apply_deposit(conn, cara, D("100"), D("4100"), NOW + 5)
    monkeypatch.setattr(ps, "deposit_kyc_required", real)
    row = ps.get_partner(conn, cara)
    assert row is not None
    from jarvise_assist.partner_rules import block

    blocked = block(conn, row, "withdraw_profit", nav=D("1"), now_ms=NOW + 6)
    assert blocked is not None
    assert blocked.code == "kyc_missing"
    closed = block(conn, row, "close", nav=D("1"), now_ms=NOW + 6)
    assert closed is not None and closed.code == "kyc_missing"
    convert = block(conn, row, "convert", nav=D("2"), now_ms=NOW + 6)
    assert convert is None or convert.code != "kyc_missing"
    admin_req = ps.create_request(conn, cara, "withdraw_profit", NOW + 7, source="admin")
    assert admin_req["ok"] is True
    conn.close()


def test_logs_and_web_do_not_carry_pii(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _no_seed(monkeypatch)
    conn = open_db(tmp_path / "l.db")
    pid = _partner(conn)
    caplog.set_level(logging.INFO)
    pk.stage_draft(conn, pid, _form(phone="021234567"), NOW)
    assert PHONE not in caplog.text
    assert "021234567" not in caplog.text

    def _boom(*_a: Any, **_k: Any) -> bytes:
        raise RuntimeError(PHONE)

    monkeypatch.setattr(pk, "_encrypt", _boom)
    pk.stage_draft(conn, pid, _form(), NOW + 1)
    assert PHONE not in caplog.text
    assert ACCOUNT not in caplog.text
    web = Path(__file__).resolve().parents[1] / "src" / "jarvise_web"
    for path in web.rglob("*.py"):
        assert "partner_kyc" not in path.read_text(encoding="utf-8")
    conn.close()


def test_new_partner_row_does_not_inherit_kyc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_seed(monkeypatch)
    conn = open_db(tmp_path / "n.db")
    first = _partner(conn, "Bob")
    assert pk.admin_set_kyc(conn, first, _form(), "ตั้งครั้งแรก", NOW)["ok"] is True
    second = _partner(conn, "Bob2")
    assert pk.kyc_state(conn, second) == "none"
    assert ps.create_request(conn, second, "deposit", NOW + 1, amount=D("100"), source="portal")[
        "reason"
    ] == ("kyc_required")
    conn.close()


def _value(now_ms: int, amount: str = "4000") -> AccountValue:
    return AccountValue(
        wallet=D(amount),
        unrealized=D("0"),
        value=D(amount),
        open_positions=0,
        read_at_ms=now_ms,
    )


def _admin_message(text: str = "p") -> dict[str, Any]:
    return {"chat": {"id": 42, "type": "private"}, "from": {"id": 42}, "text": text}


def _portal_cb(data: str, tg: str = "501", mid: int = 3) -> dict[str, Any]:
    return {
        "id": "cq",
        "from": {"id": int(tg)},
        "message": {
            "message_id": mid,
            "chat": {"id": int(tg), "type": "private"},
            "text": "card",
        },
        "data": data,
    }


def test_thai_digits_match_ascii_and_titles_stop_at_a_letter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_seed(monkeypatch)
    thai_phone = "๐๘๑๒๓๔๕๖๗๘"
    thai_account = "๑๒๓๔๕๖๗๘๙๐"
    parsed = pk.parse_form(_form(phone=thai_phone, account=thai_account))
    assert parsed["ok"] is True
    assert parsed["phone"] == PHONE
    assert parsed["account_no"] == ACCOUNT
    assert parsed["phone_last4"] == "5678"
    assert parsed["account_last4"] == "7890"

    arabic = pk.parse_form(_form(phone="٠٨١٢٣٤٥٦٧٨"))
    assert arabic["ok"] is False
    assert "เบอร์ต้องเป็นมือถือไทย" in arabic["errors"]
    assert "٠" not in "".join(arabic["errors"])

    named = pk.parse_form(_form(real_name="สมชาย ใจด๐"))
    assert named["ok"] is False
    assert "ชื่อจริงต้องเป็นชื่อและนามสกุล" in named["errors"]
    baht = pk.parse_form(_form(real_name="สมชาย ใจ฿"))
    assert baht["ok"] is False

    assert pk._strip_titles("Mrinal Shah") == "Mrinal Shah"
    assert pk.names_match("Mr. John Smith", "John Smith") is True
    assert pk.names_match("นางนวล สวย", "นวล สวย") is True

    conn = open_db(tmp_path / "digits.db")
    first = _partner(conn, "Ann")
    second = _partner(conn, "Bea")
    saved = pk.admin_set_kyc(conn, first, _form(), "ตั้งครั้งแรก", NOW)
    assert saved["ok"] is True
    again = pk.admin_set_kyc(conn, second, _form(phone=thai_phone, account=thai_account), "ตั้งครั้งแรก", NOW)
    assert again["ok"] is True
    assert first in again["duplicate_partner_ids"]
    conn.close()


def test_portal_gates_callbacks_and_legacy_card_still_deposits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_seed(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setattr(
        portal,
        "_read_v_cached",
        lambda _conn, _futures, *, now_ms: _value(now_ms),
    )
    conn = open_db(tmp_path / "cb.db")
    admin = _partner(conn, "admin", admin=True)
    assert ps.apply_deposit(conn, admin, D("1000"), D("1000"), NOW)["ok"] is True
    bare = _partner(conn, "Cara")
    _bind(conn, bare, "502")
    sent, send = _portal_send()

    def _tap(data: str, tg: str, now_ms: int = NOW) -> dict[str, Any]:
        return portal.handle_portal_callback(
            conn,
            data,
            callback=_portal_cb(data, tg),
            send=send,
            answer=lambda *_a, **_k: True,
            edit=lambda *_a, **_k: True,
            now_ms=now_ms,
        )

    before = conn.execute("SELECT COUNT(*) AS n FROM partner_request").fetchone()["n"]
    for data in ("pp:rq:wd", "pp:go:wd", "pp:rq:cl", "pp:go:cl"):
        refused = _tap(data, "502")
        assert refused["reason"] == "kyc_missing"
    assert conn.execute("SELECT COUNT(*) AS n FROM partner_request").fetchone()["n"] == before

    sent.clear()
    dep = _tap("pp:dep", "502")
    assert dep["reason"] == "kyc_required"
    assert any("อย่าโอนเงินก่อน" in row["text"] for row in sent)

    legacy = _partner(conn, "Dee")
    _bind(conn, legacy, "503")
    real = ps.deposit_kyc_required
    monkeypatch.setattr(ps, "deposit_kyc_required", lambda *_a, **_k: False)
    assert ps.apply_deposit(conn, legacy, D("100"), D("1100"), NOW + 1)["ok"] is True
    monkeypatch.setattr(ps, "deposit_kyc_required", real)
    sent.clear()
    portal.handle_portal_text(
        conn,
        {"chat": {"id": 503, "type": "private"}, "from": {"id": 503}, "text": "p"},
        send=send,
        now_ms=NOW + 2,
    )
    card = sent[-1]["text"]
    assert "ยังไม่ได้กรอกข้อมูลรับเงิน" in card
    sent.clear()
    portal.handle_portal_text(
        conn,
        {"chat": {"id": 503, "type": "private"}, "from": {"id": 503}, "text": "ฝาก 100"},
        send=send,
        now_ms=NOW + 3,
    )
    assert any("แจ้งฝาก" in row["text"] for row in sent)
    assert all("อย่าโอนเงินก่อน" not in row["text"] for row in sent)

    conn.execute("UPDATE partner SET status = 'closed' WHERE id = ?", (legacy,))
    row = ps.get_partner(conn, legacy)
    assert row is not None
    closed_text, markup = portal._build_card(conn, row, _value(NOW + 4), now_ms=NOW + 4)
    assert "แก้ไขข้อมูลรับเงิน: ติดต่อแอดมิน" in closed_text
    assert "ยังไม่ได้กรอกข้อมูลรับเงิน" not in closed_text
    assert markup is None

    bob = _partner(conn, "Bob")
    _bind(conn, bob, "501")
    assert pk.admin_set_kyc(conn, bob, _form(), "ตั้งครั้งแรก", NOW + 5)["ok"] is True
    monkeypatch.setattr(ps, "deposit_kyc_required", lambda *_a, **_k: False)
    assert ps.apply_deposit(conn, bob, D("1000"), D("2100"), NOW + 6)["ok"] is True
    monkeypatch.setattr(ps, "deposit_kyc_required", real)
    conn.execute(
        "UPDATE partner_event SET created_at_ms = ? WHERE partner_id = ? AND kind = 'deposit'",
        (NOW - 40 * 86_400_000, bob),
    )
    edited: list[str] = []

    def edit(
        _mid: int, text: str, *, reply_markup: dict[str, Any] | None = None, chat_id: str | None = None
    ) -> bool:
        edited.append(text)
        return True

    for data in ("pp:rq:wd", "pp:rq:cl"):
        edited.clear()
        shown = portal.handle_portal_callback(
            conn,
            data,
            callback=_portal_cb(data, "501"),
            send=send,
            answer=lambda *_a, **_k: True,
            edit=edit,
            now_ms=NOW + 7,
        )
        assert shown.get("ok") is True
        assert edited
        assert "โอนเข้า: KBANK ••••7890" in edited[-1]
    conn.close()


def test_admin_withdraw_without_kyc_and_partner_note_stays_masked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_seed(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setattr(pa, "_read_v", lambda *_a, **_k: _value(NOW, "1000"))
    conn = open_db(tmp_path / "adm.db")
    admin = _partner(conn, "admin", admin=True)
    assert ps.apply_deposit(conn, admin, D("1000"), D("1000"), NOW)["ok"] is True
    bob = _partner(conn, "Bob")
    cara = _partner(conn, "Cara")
    sent: list[dict[str, Any]] = []

    def send(
        text: str, *, reply_markup: dict[str, Any] | None = None, chat_id: str | None = None, **_k: Any
    ) -> int:
        sent.append({"text": text, "chat_id": chat_id})
        return len(sent)

    message = _admin_message()
    wd = pa.handle_partner_text(conn, "p wd Bob", message=message, send=send, now_ms=NOW + 1)
    assert wd["ok"] is True
    close = pa.handle_partner_text(conn, "p close Cara", message=message, send=send, now_ms=NOW + 2)
    assert close["ok"] is True
    assert pk.kyc_state(conn, bob) == "none"
    assert pk.kyc_state(conn, cara) == "none"

    conn.close()
    conn = open_db(tmp_path / "pay.db")
    admin = _partner(conn, "admin", admin=True)
    assert ps.apply_deposit(conn, admin, D("1000"), D("1000"), NOW)["ok"] is True
    bob = _partner(conn, "Bob")
    _bind(conn, bob)
    assert pk.admin_set_kyc(conn, bob, _form(), "ตั้งครั้งแรก", NOW + 1)["ok"] is True
    bare = pk.payout_account_line(conn, partner_id=bob, payout_kyc_id=None, partner_name="Bob")
    assert "ไม่มีข้อมูลรับเงินตอนยืนยัน" in bare
    assert "KBANK ••••7890" in bare
    assert "p kyc Bob" in bare
    real = ps.deposit_kyc_required
    monkeypatch.setattr(ps, "deposit_kyc_required", lambda *_a, **_k: False)
    assert ps.apply_deposit(conn, bob, D("1000"), D("2000"), NOW + 2)["ok"] is True
    monkeypatch.setattr(ps, "deposit_kyc_required", real)
    created = ps.create_request(conn, bob, "withdraw_profit", NOW + 3, source="admin")
    assert created["ok"] is True
    confirmed = ps.confirm_request(conn, str(created["request_id"]), D("4000"), NOW + 4)
    assert confirmed["ok"] is True
    assert D(confirmed["cash"]) > 0
    rid = str(created["request_id"])
    body, _markup = pa._settled_view(conn, rid)
    assert "KBANK ••••7890" in body
    sent.clear()
    due = pa.handle_partner_text(conn, "p due", message=message, send=send, now_ms=NOW + 5)
    assert due["ok"] is True
    assert any("KBANK ••••7890" in row["text"] for row in sent)
    zero = pa._closing_note(conn, bob, cash=D("0"), fee=D("0"))
    assert "ไม่มีข้อมูลรับเงิน" not in zero
    sent.clear()
    paid = pa.handle_partner_callback(
        conn,
        f"pa:pd:{rid}",
        callback={
            "id": "cq",
            "from": {"id": 42},
            "message": {"message_id": 8, "chat": {"id": 42, "type": "private"}, "text": "due"},
        },
        send=send,
        answer=lambda *_a, **_k: True,
        edit=lambda *_a, **_k: True,
        now_ms=NOW + 6,
    )
    assert paid["ok"] is True
    notes = [row["text"] for row in sent if row["chat_id"] == "501"]
    assert notes
    assert "โอนเข้า: KBANK ••••7890" in notes[-1]
    assert "p kyc" not in notes[-1]

    unsaved = _partner(conn, "Cara")
    called = {"n": 0}

    def _no_wallet(*_a: Any, **_k: Any) -> Any:
        called["n"] += 1
        raise AssertionError("wallet read")

    monkeypatch.setattr(ps, "deposit_kyc_required", lambda *_a, **_k: False)
    pending = ps.create_request(conn, unsaved, "deposit", NOW + 7, amount=D("100"), source="admin")
    monkeypatch.setattr(ps, "deposit_kyc_required", real)
    assert pending["ok"] is True
    monkeypatch.setattr(pa, "_read_v", _no_wallet)
    blocked = pa.handle_partner_callback(
        conn,
        f"pa:dy:{pending['request_id']}",
        callback={
            "id": "cq2",
            "from": {"id": 42},
            "message": {"message_id": 9, "chat": {"id": 42, "type": "private"}, "text": "dep"},
        },
        send=send,
        answer=lambda *_a, **_k: True,
        edit=lambda *_a, **_k: True,
        now_ms=NOW + 8,
    )
    assert blocked["reason"] == "kyc_required"
    assert called["n"] == 0
    assert any("แล้วขึ้นบรรทัดใหม่ใส่ฟอร์ม" in row["text"] for row in sent)
    conn.close()


def test_draft_buttons_keep_the_newer_token_and_refuse_a_closed_partner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_seed(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "draft.db")
    bob = _partner(conn, "Bob")
    _bind(conn, bob)
    first = pk.stage_draft(conn, bob, _form(), NOW)
    second = pk.stage_draft(conn, bob, _form(phone="0890000000", account="2222222222"), NOW + 1)
    assert first["ok"] is True and second["ok"] is True
    sent, send = _portal_send()
    edited: list[str] = []

    def edit_ok(
        _mid: int, text: str, *, reply_markup: dict[str, Any] | None = None, chat_id: str | None = None
    ) -> bool:
        edited.append(text)
        return True

    redo = portal.handle_portal_callback(
        conn,
        f"pp:kyc:re:{first['token']}",
        callback=_portal_cb(f"pp:kyc:re:{first['token']}"),
        send=send,
        answer=lambda *_a, **_k: True,
        edit=edit_ok,
        now_ms=NOW + 2,
    )
    assert redo["ok"] is True
    kept = conn.execute("SELECT token FROM partner_kyc_draft WHERE partner_id = ?", (bob,)).fetchone()
    assert kept is not None
    assert kept["token"] == second["token"]
    assert edited
    assert "ข้อมูลรับเงิน" in edited[-1]

    sent.clear()
    expired = portal.handle_portal_callback(
        conn,
        f"pp:kyc:ok:{second['token']}",
        callback=_portal_cb(f"pp:kyc:ok:{second['token']}"),
        send=send,
        answer=lambda *_a, **_k: True,
        edit=lambda *_a, **_k: True,
        now_ms=NOW + 1 + pk.DRAFT_TTL_MS,
    )
    assert expired["reason"] == "kyc_draft_expired"
    assert any("ฟอร์มหมดอายุ" in row["text"] for row in sent)
    assert conn.execute("SELECT COUNT(*) AS n FROM partner_kyc").fetchone()["n"] == 0

    fresh = pk.stage_draft(conn, bob, _form(), NOW + 3)
    assert fresh["ok"] is True
    notices = {"n": 0}

    def send_count(
        text: str, *, reply_markup: dict[str, Any] | None = None, chat_id: str | None = None, **_k: Any
    ) -> int:
        if chat_id is None and "ดูเต็ม" in text:
            notices["n"] += 1
        sent.append({"text": text, "chat_id": chat_id, "reply_markup": reply_markup})
        return len(sent)

    sent.clear()
    saved = portal.handle_portal_callback(
        conn,
        f"pp:kyc:ok:{fresh['token']}",
        callback=_portal_cb(f"pp:kyc:ok:{fresh['token']}"),
        send=send_count,
        answer=lambda *_a, **_k: True,
        edit=lambda *_a, **_k: True,
        now_ms=NOW + 4,
    )
    assert saved["ok"] is True
    again = portal.handle_portal_callback(
        conn,
        f"pp:kyc:ok:{fresh['token']}",
        callback=_portal_cb(f"pp:kyc:ok:{fresh['token']}", mid=4),
        send=send_count,
        answer=lambda *_a, **_k: True,
        edit=lambda *_a, **_k: True,
        now_ms=NOW + 5,
    )
    assert again["reason"] == "kyc_locked"
    assert notices["n"] == 1

    cara = _partner(conn, "Cara")
    _bind(conn, cara, "502")
    staged = pk.stage_draft(conn, cara, _form(phone="0891111111", account="3333333333"), NOW + 6)
    conn.execute("UPDATE partner SET status = 'closed' WHERE id = ?", (cara,))
    sent.clear()
    closed = portal.handle_portal_callback(
        conn,
        f"pp:kyc:ok:{staged['token']}",
        callback=_portal_cb(f"pp:kyc:ok:{staged['token']}", tg="502"),
        send=send,
        answer=lambda *_a, **_k: True,
        edit=lambda *_a, **_k: True,
        now_ms=NOW + 7,
    )
    assert closed["reason"] == "closed"
    assert any("ปิดพอร์ตแล้ว — ติดต่อแอดมิน" in row["text"] for row in sent)
    assert (
        conn.execute("SELECT COUNT(*) AS n FROM partner_kyc WHERE partner_id = ?", (cara,)).fetchone()["n"]
        == 0
    )

    def edit_fail(*_a: Any, **_k: Any) -> bool:
        return False

    eve = _partner(conn, "Eve")
    _bind(conn, eve, "504")
    eve_draft = pk.stage_draft(conn, eve, _form(phone="0892222222", account="4444444444"), NOW + 8)
    sent.clear()
    fallback = portal.handle_portal_callback(
        conn,
        f"pp:kyc:ok:{eve_draft['token']}",
        callback=_portal_cb(f"pp:kyc:ok:{eve_draft['token']}", tg="504"),
        send=send,
        answer=lambda *_a, **_k: True,
        edit=edit_fail,
        now_ms=NOW + 9,
    )
    assert fallback["ok"] is True
    partner_copy = [row["text"] for row in sent if row["chat_id"] == "504"]
    assert partner_copy
    assert "บันทึกแล้ว แก้ไขเองไม่ได้" in partner_copy[-1]
    assert "4444444444" not in partner_copy[-1]

    real_reveal = pk.reveal_current

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise pk.KycCryptoError()

    monkeypatch.setattr(pk, "reveal_current", _boom)
    shown = pa.handle_partner_text(
        conn, "p kyc Bob", message=_admin_message("p kyc Bob"), send=send, now_ms=NOW + 10
    )
    assert shown["reason"] == "kyc_crypto"
    assert sent[-1]["text"] == "อ่านข้อมูลรับเงินไม่ได้ตอนนี้"
    assert PHONE not in sent[-1]["text"]

    monkeypatch.setattr(pk, "reveal_current", real_reveal)
    conn.execute("UPDATE partner SET status = 'closed' WHERE id = ?", (bob,))
    updated = pa.handle_partner_text(
        conn,
        "p kyc Bob set แก้ไขข้อมูลหลังปิดพอร์ตตามคำขอของพาร์ทเนอร์\n"
        + _form(phone="0893333333", account="5555555555"),
        message=_admin_message(),
        send=send,
        now_ms=NOW + 11,
    )
    assert updated["ok"] is True
    assert updated["version"] == 2
    conn.close()
