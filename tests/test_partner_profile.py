"""Partner profile view and protect_content on partner-chat sends."""

from __future__ import annotations

import ast
import logging
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from jarvise_assist import partner_fx as fx
from jarvise_assist import partner_kyc as pk
from jarvise_assist import partner_portal as portal
from jarvise_assist import partner_profile as profile
from jarvise_assist import partner_store as ps
from jarvise_assist.partner_admin import send_to_partner
from jarvise_assist.partner_value import AccountValue
from jarvise_ingest.db import open_db
from jarvise_notify.telegram import send_message

D = Decimal
NOW = 1_700_000_000_000
BANGKOK = timezone(timedelta(hours=7))
PHONE = "0812345678"
ACCOUNT = "1234567890"
REAL_NAME = "สมชาย ใจดี"
HOLDER = "สมหญิง ใจงาม"


def _no_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_TEST_NO_KYC_SEED", "1")


def _form(
    *,
    real_name: str = REAL_NAME,
    phone: str = "081-234-5678",
    account: str = "123-4-56789-0",
    holder: str = HOLDER,
) -> str:
    return f"ข้อมูลรับเงิน\nชื่อจริง: {real_name}\nเบอร์: {phone}\nธนาคาร: กสิกร\nเลขบัญชี: {account}\nชื่อบัญชี: {holder}\n"


def _partner(
    conn: sqlite3.Connection,
    name: str = "Bob",
    *,
    admin: bool = False,
    fee_rate_bps: int = 5000,
    now_ms: int = NOW,
) -> str:
    created = ps.create_partner(conn, name=name, is_admin=admin, now_ms=now_ms, fee_rate_bps=fee_rate_bps)
    assert created["ok"] is True
    return str(created["partner_id"])


def _bind(conn: sqlite3.Connection, pid: str, tg: str, now_ms: int = NOW) -> None:
    invited = ps.create_invite(conn, pid, now_ms=now_ms)
    assert invited["ok"] is True
    bound = ps.bind_invite(conn, invited["code"], tg, admin_chat_id="42", now_ms=now_ms)
    assert bound["ok"] is True


def _value(now_ms: int, wallet: str = "1000") -> AccountValue:
    return AccountValue(
        wallet=D(wallet),
        unrealized=D("0"),
        value=D(wallet),
        open_positions=0,
        read_at_ms=now_ms,
    )


def _text(conn: sqlite3.Connection, pid: str, now_ms: int = NOW, wallet: str | None = None) -> str:
    value = None if wallet is None else D(wallet)
    return profile.render_profile(profile.profile_data(conn, pid, now_ms, account_value=value))


def _callback(data: str, *, mid: int = 3, tg: str = "501", text: str = "card") -> dict[str, Any]:
    return {
        "id": "cq",
        "from": {"id": int(tg)},
        "message": {"message_id": mid, "chat": {"id": int(tg), "type": "private"}, "text": text},
        "data": data,
    }


def _capture() -> tuple[list[dict[str, Any]], Any]:
    sent: list[dict[str, Any]] = []

    def send(
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        chat_id: str | None = None,
        protect_content: bool = False,
        **_k: Any,
    ) -> int:
        sent.append(
            {
                "text": text,
                "reply_markup": reply_markup,
                "chat_id": chat_id,
                "protect_content": protect_content,
            }
        )
        return len(sent)

    return sent, send


def test_profile_uses_masks_even_if_decrypt_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _no_seed(monkeypatch)
    conn = open_db(tmp_path / "mask.db")
    bob = _partner(conn)
    assert pk.admin_set_kyc(conn, bob, _form(), "ตั้งครั้งแรก", NOW)["ok"] is True

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError(f"{PHONE} {ACCOUNT} {REAL_NAME} {HOLDER}")

    monkeypatch.setattr(pk, "_master_key", boom)
    monkeypatch.setattr(pk, "_decrypt", boom)
    masks = pk.masked_profile(conn, bob)
    assert masks is not None
    assert set(masks) == {
        "real_name_mask",
        "bank_code",
        "account_last4",
        "holder_name_mask",
        "phone_last4",
    }
    caplog.set_level(logging.DEBUG)
    text = _text(conn, bob, wallet="1000")
    assert "KBANK" in text
    assert "7890" in text
    assert "สมชาย" in text
    assert REAL_NAME not in text
    assert "ใจดี" not in text
    assert HOLDER not in text
    assert "ใจงาม" not in text
    assert PHONE not in text
    assert "081-234-5678" not in text
    assert ACCOUNT not in text
    assert "123-4-56789-0" not in text
    assert PHONE not in caplog.text
    assert ACCOUNT not in caplog.text
    assert REAL_NAME not in caplog.text
    assert HOLDER not in caplog.text
    conn.close()


def test_profile_renders_masks_with_the_key_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_seed(monkeypatch)
    conn = open_db(tmp_path / "nokey.db")
    bob = _partner(conn)
    assert pk.admin_set_kyc(conn, bob, _form(), "ตั้งครั้งแรก", NOW)["ok"] is True
    monkeypatch.delenv("JARVISE_PARTNER_KYC_KEY_PATH", raising=False)
    text = _text(conn, bob)
    assert "KBANK" in text
    assert "7890" in text
    assert "สมชาย" in text
    assert REAL_NAME not in text
    conn.close()


def test_legacy_partner_without_kyc_shows_the_reminder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_seed(monkeypatch)
    conn = open_db(tmp_path / "legacy.db")
    admin = _partner(conn, "admin", admin=True)
    bob = _partner(conn)
    monkeypatch.setattr(ps, "deposit_kyc_required", lambda *_a, **_k: False)
    assert ps.apply_deposit(conn, admin, D("1000"), D("1000"), NOW)["ok"] is True
    assert ps.apply_deposit(conn, bob, D("100"), D("1100"), NOW + 1)["ok"] is True
    text = _text(conn, bob, now_ms=NOW + 1, wallet="1100")
    assert "ข้อมูลรับเงิน: ยังไม่กรอก" in text
    assert profile._KYC_LEGACY in text
    assert profile._KYC_LEGACY == portal._KYC_LEGACY
    conn.close()


def test_uncommitted_draft_stays_off_the_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_seed(monkeypatch)
    conn = open_db(tmp_path / "draft.db")
    bob = _partner(conn)
    staged = pk.stage_draft(conn, bob, _form(), NOW)
    assert staged["ok"] is True
    assert pk.masked_profile(conn, bob) is None
    text = _text(conn, bob)
    assert "ข้อมูลรับเงิน: ยังไม่กรอก" in text
    assert ACCOUNT not in text
    assert PHONE not in text
    assert REAL_NAME not in text
    assert HOLDER not in text
    assert staged["token"] not in text
    conn.close()


def test_joined_date_is_the_first_deposit_in_bangkok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = open_db(tmp_path / "join.db")
    admin = _partner(conn, "admin", admin=True, now_ms=NOW)
    bare = _partner(conn, "Bare", now_ms=NOW)
    assert "เข้าร่วม: ยังไม่ฝาก" in _text(conn, bare, now_ms=NOW + 5)
    # 03:00 Bangkok on 2 Jan is 20:00 UTC on 1 Jan, so a UTC date would be wrong.
    first = datetime(2024, 1, 2, 3, 0, tzinfo=BANGKOK)
    joined = int(first.timestamp() * 1000)
    utc = datetime.fromtimestamp(joined / 1000, tz=UTC)
    assert utc.strftime("%d/%m/%Y %H:%M") == "01/01/2024 20:00"
    bob = _partner(conn, "Bob", now_ms=NOW)
    assert ps.apply_deposit(conn, admin, D("1000"), D("1000"), NOW + 1)["ok"] is True
    assert ps.apply_deposit(conn, bob, D("100"), D("1100"), joined)["ok"] is True
    text = _text(conn, bob, now_ms=joined + 3_600_000, wallet="1100")
    assert "เข้าร่วม 02/01/2024" in text
    assert "01/01/2024" not in text
    second = joined + 86_400_000
    assert ps.apply_deposit(conn, bob, D("50"), D("1250"), second)["ok"] is True
    later = _text(conn, bob, now_ms=second, wallet="1250")
    assert "เข้าร่วม 02/01/2024" in later
    assert "01/01/2024" not in later
    conn.close()


def test_fee_percent_hides_a_zero_fraction(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "fee.db")
    even = _partner(conn, "Even", fee_rate_bps=2000)
    odd = _partner(conn, "Odd", fee_rate_bps=2050)
    assert "ค่าธรรมเนียม 20%" in _text(conn, even)
    assert "ค่าธรรมเนียม 20.0%" not in _text(conn, even)
    assert "ค่าธรรมเนียม 20.5%" in _text(conn, odd)
    conn.close()


def test_due_line_follows_the_payout_and_matches_the_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = open_db(tmp_path / "due.db")
    admin = _partner(conn, "admin", admin=True)
    bob = _partner(conn)
    assert ps.apply_deposit(conn, admin, D("1000"), D("1000"), NOW + 1)["ok"] is True
    assert ps.apply_deposit(conn, bob, D("200"), D("1200"), NOW + 2)["ok"] is True
    withdrawn = ps.apply_withdraw_profit(conn, bob, D("1440"), NOW + 3)
    assert withdrawn["ok"] is True
    assert D(withdrawn["cash"]) > 0
    text = _text(conn, bob, now_ms=NOW + 3, wallet="1440")
    assert "รอโอน " in text
    row = ps.get_partner(conn, bob)
    assert row is not None
    card, _markup = portal._build_card(conn, row, _value(NOW + 3, "1440"), now_ms=NOW + 3)
    card_due = [line for line in card.splitlines() if line.startswith("รอโอน ")]
    profile_due = [line for line in text.splitlines() if line.startswith("รอโอน ")]
    assert profile_due == card_due
    assert ps.mark_paid(conn, str(withdrawn["request_id"]), NOW + 4)["ok"] is True
    paid = _text(conn, bob, now_ms=NOW + 4, wallet="1440")
    assert "รอโอน" not in paid
    conn.close()


def test_closed_card_offers_only_the_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "closed.db")
    admin = _partner(conn, "admin", admin=True)
    bob = _partner(conn)
    _bind(conn, bob, "501")
    assert ps.apply_deposit(conn, admin, D("1000"), D("1000"), NOW + 1)["ok"] is True
    assert ps.apply_deposit(conn, bob, D("1000"), D("2000"), NOW + 2)["ok"] is True
    closed = ps.apply_close(conn, bob, D("2000"), NOW + 3)
    assert closed["ok"] is True
    assert D(closed["cash"]) > 0
    row = ps.get_partner(conn, bob)
    assert row is not None
    _card, markup = portal._build_card(conn, row, _value(NOW + 3, "2000"), now_ms=NOW + 3)
    assert markup == {"inline_keyboard": [[{"text": "👤 โปรไฟล์", "callback_data": "pp:pf"}]]}
    edited: list[tuple[str, dict[str, Any] | None]] = []
    sent, send = _capture()
    reads: list[int] = []

    def edit(
        mid: int, text: str, *, reply_markup: dict[str, Any] | None = None, chat_id: str | None = None
    ) -> bool:
        edited.append((text, reply_markup))
        return True

    def _must_not_read(*_a: Any, **_k: Any) -> AccountValue:
        reads.append(1)
        raise AssertionError("closed profile must not read the wallet")

    monkeypatch.setattr(portal, "_read_v_cached", _must_not_read)
    out = portal.handle_portal_callback(
        conn,
        "pp:pf",
        callback=_callback("pp:pf"),
        send=send,
        answer=lambda *_a, **_k: True,
        edit=edit,
        now_ms=NOW + 4,
    )
    assert out["cmd"] == "profile"
    assert reads == []
    assert sent == []
    body, buttons = edited[-1]
    assert "โปรไฟล์พาร์ทเนอร์" in body
    assert "สถานะ: ปิดแล้ว" in body
    assert "ยอดปิด" in body
    assert "รอโอน" in body
    assert "ทุน" not in body
    assert "ยอดสุทธิ" not in body
    assert "กำไร" not in body
    assert buttons == {"inline_keyboard": [[{"text": "⬅️ กลับ", "callback_data": "pp:v"}]]}
    monkeypatch.setattr(portal, "_read_v_cached", lambda _c, _f, *, now_ms: _value(now_ms, "2000"))
    back = portal.handle_portal_callback(
        conn,
        "pp:v",
        callback=_callback("pp:v", text=body),
        send=send,
        answer=lambda *_a, **_k: True,
        edit=edit,
        now_ms=NOW + 5,
    )
    assert back["cmd"] == "refresh"
    assert "ปิดพอร์ตแล้ว" in edited[-1][0]
    assert edited[-1][1] == {"inline_keyboard": [[{"text": "👤 โปรไฟล์", "callback_data": "pp:pf"}]]}
    conn.close()


def test_rebound_telegram_shows_only_the_new_partner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setattr(portal, "_read_v_cached", lambda _c, _f, *, now_ms: _value(now_ms, "2000"))
    conn = open_db(tmp_path / "rebind.db")
    admin = _partner(conn, "admin", admin=True)
    bob = _partner(conn, "Bob")
    _bind(conn, bob, "501", NOW)
    assert ps.apply_deposit(conn, admin, D("1000"), D("1000"), NOW + 1)["ok"] is True
    assert ps.apply_deposit(conn, bob, D("1000"), D("2000"), NOW + 2)["ok"] is True
    closed = ps.apply_close(conn, bob, D("2000"), NOW + 3)
    assert closed["ok"] is True
    rid = str(closed["request_id"])
    assert ps.mark_paid(conn, rid, NOW + 4)["ok"] is True
    micro = int(ps.get_request(conn, rid)["payout_micro"])
    assert micro % 1_000_000 == 0
    sold = micro
    gross = (sold * 200_000) // 100_000_000
    kyc = pk.current_kyc(conn, bob)
    assert kyc is not None
    saved = fx.record_payout_fx(
        conn,
        request_id=rid,
        carry_in_micro=0,
        chain_fee_micro=0,
        usdt_sold_micro=sold,
        rate=D("20"),
        trade_fee_satang=0,
        thb_wd_fee_satang=0,
        bank_fee_satang=0,
        net_satang=gross,
        ref_status="off",
        paid_kyc_id=str(kyc["id"]),
        entered_by="42",
        now_ms=NOW + 4,
    )
    assert saved["ok"] is True, saved
    assert fx.open_settlements(conn, bob) == []
    bob2 = _partner(conn, "Bob2", now_ms=NOW + 5)
    invited = ps.create_invite(conn, bob2, now_ms=NOW + 6)
    assert invited["ok"] is True
    bound = ps.bind_invite(conn, invited["code"], "501", admin_chat_id="42", now_ms=NOW + 7)
    assert bound["ok"] is True
    edited: list[str] = []
    out = portal.handle_portal_callback(
        conn,
        "pp:pf",
        callback=_callback("pp:pf", tg="501"),
        send=_capture()[1],
        answer=lambda *_a, **_k: True,
        edit=lambda _m, text, **_k: edited.append(text) or True,
        now_ms=NOW + 8,
    )
    assert out["cmd"] == "profile"
    assert "Bob2 · อัปเดต" in edited[-1]
    assert "Bob · อัปเดต" not in edited[-1]
    conn.close()


def test_card_keyboard_switches_row_five_when_kyc_is_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plain = portal._card_buttons()["inline_keyboard"]
    saved_rows = portal._card_buttons(kyc_saved=True)["inline_keyboard"]
    assert len(plain) == 5
    assert len(saved_rows) == 5
    assert plain[4] == [{"text": "ข้อมูลรับเงิน", "callback_data": "pp:kyc"}]
    assert saved_rows[4] == [{"text": "👤 โปรไฟล์", "callback_data": "pp:pf"}]
    assert plain[:4] == saved_rows[:4]
    for rows in (plain, saved_rows):
        for row in rows:
            for btn in row:
                data = str(btn["callback_data"])
                assert data.startswith("pp:")
                assert len(data.encode()) <= 64

    _no_seed(monkeypatch)
    conn = open_db(tmp_path / "keys.db")
    bare = _partner(conn, "Bare")
    _bind(conn, bare, "501")
    row = ps.get_partner(conn, bare)
    assert row is not None
    _text_card, markup = portal._build_card(conn, row, _value(NOW), now_ms=NOW)
    assert markup is not None
    assert markup["inline_keyboard"][4][0]["callback_data"] == "pp:kyc"
    assert pk.admin_set_kyc(conn, bare, _form(), "ตั้งครั้งแรก", NOW + 1)["ok"] is True
    row = ps.get_partner(conn, bare)
    assert row is not None
    _text_card, markup = portal._build_card(conn, row, _value(NOW + 1), now_ms=NOW + 1)
    assert markup is not None
    assert markup["inline_keyboard"][4][0]["callback_data"] == "pp:pf"
    assert len(markup["inline_keyboard"]) == 5
    conn.close()


def test_profile_edits_the_card_and_back_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _no_seed(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setattr(portal, "_read_v_cached", lambda _c, _f, *, now_ms: _value(now_ms, "1200"))
    conn = open_db(tmp_path / "edit.db")
    admin = _partner(conn, "admin", admin=True)
    bob = _partner(conn)
    assert pk.admin_set_kyc(conn, bob, _form(), "ตั้งครั้งแรก", NOW)["ok"] is True
    _bind(conn, bob, "501")
    assert ps.apply_deposit(conn, admin, D("1000"), D("1000"), NOW)["ok"] is True
    assert ps.apply_deposit(conn, bob, D("200"), D("1200"), NOW + 1)["ok"] is True
    edited: list[tuple[str, dict[str, Any] | None]] = []
    sent, send = _capture()
    caplog.set_level(logging.DEBUG)

    def edit(
        _mid: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        chat_id: str | None = None,
    ) -> bool:
        edited.append((text, reply_markup))
        return True

    opened = portal.handle_portal_callback(
        conn,
        "pp:pf",
        callback=_callback("pp:pf", text="พอร์ตของ Bob"),
        send=send,
        answer=lambda *_a, **_k: True,
        edit=edit,
        now_ms=NOW + 2,
    )
    assert opened == {"ok": True, "cmd": "profile"}
    assert sent == []
    assert edited[-1][1] == {"inline_keyboard": [[{"text": "⬅️ กลับ", "callback_data": "pp:v"}]]}
    profile_text = edited[-1][0]
    assert "โปรไฟล์พาร์ทเนอร์" in profile_text
    back = portal.handle_portal_callback(
        conn,
        "pp:v",
        callback=_callback("pp:v", text=profile_text),
        send=send,
        answer=lambda *_a, **_k: True,
        edit=edit,
        now_ms=NOW + 3,
    )
    assert back["cmd"] == "refresh"
    assert sent == []
    assert "พอร์ตของ Bob" in edited[-1][0]
    assert "KBANK" in profile_text
    assert "7890" in profile_text
    for secret in (PHONE, "081-234-5678", ACCOUNT, "123-4-56789-0", REAL_NAME, "ใจดี", HOLDER, "ใจงาม"):
        assert secret not in caplog.text
        assert secret not in profile_text
    conn.close()


def test_profile_module_does_not_touch_ciphertext() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "jarvise_assist" / "partner_profile.py"
    src = root.read_text(encoding="utf-8")
    for banned in ("_decrypt", "_master_key", "reveal_current", "_enc"):
        assert banned not in src
    tree = ast.parse(src)
    allowed = {"__future__", "logging", "sqlite3", "datetime", "decimal", "typing", "jarvise_assist"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] in allowed
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] in allowed
        elif isinstance(node, ast.Attribute):
            assert node.attr not in {"_decrypt", "_master_key", "reveal_current"}
        elif isinstance(node, ast.Name):
            assert node.id not in {"_decrypt", "_master_key", "reveal_current"}


def test_partner_sends_protect_content_and_admin_sends_do_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_seed(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setattr(portal, "_read_v_cached", lambda _c, _f, *, now_ms: _value(now_ms))
    conn = open_db(tmp_path / "protect.db")
    _partner(conn, "admin", admin=True)
    bob = _partner(conn, "Bob")
    invited = ps.create_invite(conn, bob, now_ms=NOW)
    assert invited["ok"] is True
    sent, send = _capture()
    started = portal.handle_portal_text(
        conn,
        {
            "chat": {"id": 501, "type": "private"},
            "from": {"id": 501, "first_name": "Bee"},
            "text": f"/start {invited['code']}",
        },
        send=send,
        now_ms=NOW + 1,
    )
    assert started["ok"] is True
    partner_start = [row for row in sent if row["chat_id"] == "501"]
    admin_start = [row for row in sent if row["chat_id"] is None]
    assert any("ผูกบัญชีแล้ว" in row["text"] for row in partner_start)
    assert any("พอร์ตของ Bob" in row["text"] for row in partner_start)
    assert partner_start
    assert all(row["protect_content"] is True for row in partner_start)
    assert admin_start
    assert all(row["protect_content"] is False for row in admin_start)

    sent.clear()
    portal.handle_portal_text(
        conn,
        {"chat": {"id": 501, "type": "private"}, "from": {"id": 501}, "text": "ดูยอด"},
        send=send,
        now_ms=NOW + 2,
    )
    assert sent
    assert sent[-1]["chat_id"] == "501"
    assert "พอร์ตของ Bob" in sent[-1]["text"]
    assert sent[-1]["protect_content"] is True

    sent.clear()
    portal.handle_portal_text(
        conn,
        {
            "message_id": 9,
            "chat": {"id": 501, "type": "private"},
            "from": {"id": 501},
            "text": _form(),
        },
        send=send,
        delete=lambda *_a, **_k: True,
        now_ms=NOW + 3,
    )
    confirm = [row for row in sent if row["chat_id"] == "501"]
    assert confirm
    buttons = confirm[-1]["reply_markup"]["inline_keyboard"][0]
    assert any(str(btn["callback_data"]).startswith("pp:kyc:ok:") for btn in buttons)
    assert confirm[-1]["protect_content"] is True

    sent.clear()
    portal.handle_portal_callback(
        conn,
        "pp:v",
        callback=_callback("pp:v"),
        send=send,
        answer=lambda *_a, **_k: True,
        edit=lambda *_a, **_k: False,
        now_ms=NOW + 4,
    )
    assert sent
    assert sent[-1]["chat_id"] == "501"
    assert sent[-1]["protect_content"] is True

    sent.clear()
    row = ps.partner_by_telegram_id(conn, "501")
    assert row is not None
    assert send_to_partner(send, row, "แจ้งจากแอดมิน") is True
    assert sent[-1]["text"] == "แจ้งจากแอดมิน"
    assert sent[-1]["protect_content"] is True
    conn.close()


def test_send_message_omits_protect_content_unless_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")

    class _Resp:
        status_code = 200

        def json(self) -> dict[str, Any]:
            return {"ok": True, "result": {"message_id": 9}}

    class _Http:
        def __init__(self) -> None:
            self.bodies: list[dict[str, Any]] = []

        def post(self, _url: str, json: dict[str, Any]) -> _Resp:
            self.bodies.append(json)
            return _Resp()

    http = _Http()
    assert send_message("admin", client=http) == 9  # type: ignore[arg-type]
    assert "protect_content" not in http.bodies[-1]
    assert send_message("partner", client=http, chat_id="501", protect_content=True) == 9  # type: ignore[arg-type]
    assert http.bodies[-1]["protect_content"] is True


def test_zero_balance_and_unreadable_value_match_the_card(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "empty.db")
    admin = _partner(conn, "admin", admin=True)
    bob = _partner(conn)
    assert ps.apply_deposit(conn, admin, D("1000"), D("1000"), NOW)["ok"] is True
    empty = _text(conn, bob, wallet="1000")
    assert "ยังไม่มีเงินในพอร์ต" in empty
    assert "ยอดสุทธิ" not in empty
    assert "กำไร" not in empty
    assert ps.apply_deposit(conn, bob, D("200"), D("1200"), NOW + 1)["ok"] is True
    unread = _text(conn, bob, now_ms=NOW + 1)
    assert "อ่านยอดไม่ได้ตอนนี้ ลองใหม่อีกครั้ง" in unread
    assert "ยอดสุทธิ" not in unread
    assert "กำไร" not in unread
    assert "ทุน" in unread

    def refused(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"ok": False, "reason": "nav_unavailable"}

    real = ps.view_partner
    ps.view_partner = refused  # type: ignore[method-assign]
    try:
        refused_text = _text(conn, bob, now_ms=NOW + 1, wallet="1200")
    finally:
        ps.view_partner = real  # type: ignore[method-assign]
    assert "อ่านยอดไม่ได้ตอนนี้ ลองใหม่อีกครั้ง" in refused_text
    assert "ยอดสุทธิ" not in refused_text
    conn.close()


def test_direct_portal_sends_protect_the_partner_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "direct.db")
    sent, send = _capture()
    invalid = portal.handle_portal_text(
        conn,
        {
            "chat": {"id": 509, "type": "private"},
            "from": {"id": 509},
            "text": "/start " + ("a" * 24),
        },
        send=send,
        now_ms=NOW,
    )
    assert invalid["reason"] == "invite_invalid"
    assert sent[-1]["chat_id"] == "509"
    assert sent[-1]["protect_content"] is True

    admin = _partner(conn, "admin", admin=True)
    bob = _partner(conn, "Bob")
    _bind(conn, bob, "501", NOW + 1)
    assert ps.apply_deposit(conn, admin, D("1000"), D("1000"), NOW + 2)["ok"] is True
    assert ps.apply_deposit(conn, bob, D("1000"), D("2000"), NOW + 3)["ok"] is True
    closed = ps.apply_close(conn, bob, D("2000"), NOW + 4)
    assert closed["ok"] is True
    assert D(closed["cash"]) > 0
    nxt = _partner(conn, "Cara", now_ms=NOW + 5)
    invited = ps.create_invite(conn, nxt, now_ms=NOW + 6)
    assert invited["ok"] is True
    sent.clear()
    refused = portal.handle_portal_text(
        conn,
        {
            "chat": {"id": 501, "type": "private"},
            "from": {"id": 501},
            "text": f"/start {invited['code']}",
        },
        send=send,
        now_ms=NOW + 7,
    )
    assert refused["reason"] == "closed_payout_due"
    assert sent[-1]["chat_id"] == "501"
    assert sent[-1]["protect_content"] is True
    conn.close()
