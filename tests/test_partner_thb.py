"""C2b PR-2 THB command, preview, and confirm. No network."""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from jarvise_assist import listen, partner_profile
from jarvise_assist import partner_admin as pa
from jarvise_assist import partner_fx as fx
from jarvise_assist import partner_kyc as pk
from jarvise_assist import partner_store as ps
from jarvise_assist import partner_thb as thb
from jarvise_assist.partner_audit import GuardResult
from jarvise_assist.partner_value import AccountValue
from jarvise_assist.pause import BANGKOK
from jarvise_ingest.db import open_db

D = Decimal
ADMIN = "42"
TG = "501"
NOW = 1_700_000_000_000
PAID_AT = int(datetime(2026, 11, 18, 9, 2, tzinfo=BANGKOK).timestamp() * 1000)
RECORDED_AT = int(datetime(2026, 11, 18, 14, 5, tzinfo=BANGKOK).timestamp() * 1000)
RID = "pr_0000000000a1b2c3"
KYC_FORM = (
    "ข้อมูลรับเงิน\nชื่อจริง: สมชาย ใจดี\nเบอร์: 0812345678\nธนาคาร: KBANK\nเลขบัญชี: 0000001234\nชื่อบัญชี: สมชาย ใจดี\n"
)
PREVIEW = """ตรวจก่อนบันทึกโอนเงินบาท · som · ถอนกำไร #a1b2c3
ยอดถอน 500.00 USDT · USDT ออก 18/11 09:02
เศษยกมา 0.00 · ค่าเครือข่าย −0.01 → มีให้ขาย 499.99 USDT
ขาย 499 USDT × 33.90 = 16,916.10 บาท
ค่าซื้อขาย −42.29 (0.25%) · ค่าถอนบาท −20.00 · ค่าโอนธนาคาร 0.00
คำนวณได้ 16,853.81 · ตามสลิป 16,853.81 · ส่วนต่าง 0.00 (เกณฑ์ ±1.00)
เฉลี่ยสุทธิ 33.7744 บาท/USDT
เศษ 0.99 USDT ยกไปครั้งหน้า → ยอดเศษหลังบันทึก 0.99 USDT
โอนเข้า: KBANK ••••1234 (สมชาย ใ***)
เรทอ้างอิง: ยังไม่เปิดใช้
กดยืนยันภายใน 15 นาที"""
NOTICE = """💸 โอนเงินบาทเข้าบัญชีของคุณแล้ว · ถอนกำไร
ยอดถอน 500.00 USDT
ค่าธรรมเนียมเครือข่าย −0.01 USDT
ขาย 499 USDT × 33.90 = 16,916.10 บาท
ค่าธรรมเนียมซื้อขาย −42.29 บาท
ค่าถอนเงินบาท −20.00 บาท
รับสุทธิ 16,853.81 บาท (เฉลี่ย 33.7744 บาท/USDT)
เศษ 0.99 USDT ยังไม่ได้ขาย (ขายได้ขั้นละ 1 USDT) — ยกไปรวมกับการถอนครั้งถัดไป
โอนเข้า: KBANK ••••1234 (สมชาย ใ***)
บันทึก 18/11/2026 14:05 · อ้างอิงสลิป 0123ABC"""


@pytest.fixture
def env_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", ADMIN)


def _partner(conn: Any, name: str = "som", *, now_ms: int = NOW) -> str:
    made = ps.create_partner(conn, name=name, now_ms=now_ms)
    assert made["ok"] is True
    pid = str(made["partner_id"])
    saved = pk.admin_set_kyc(conn, pid, KYC_FORM, "ตั้งบัญชีตัวอย่าง", now_ms + 1)
    assert saved["ok"] is True
    return pid


def _paid_request(
    conn: Any,
    partner_id: str,
    *,
    rid: str = RID,
    kind: str = "withdraw_profit",
    micro: int = 500_000_000,
    paid_at: int = PAID_AT,
) -> str:
    conn.execute(
        """
        INSERT INTO partner_request (
            id, partner_id, kind, status, payout_state, payout_micro,
            created_at_ms, expires_at_ms, decided_at_ms, paid_at_ms, source
        ) VALUES (?, ?, ?, 'confirmed', 'paid', ?, ?, ?, ?, ?, 'admin')
        """,
        (rid, partner_id, kind, micro, paid_at, paid_at + 1, paid_at, paid_at),
    )
    conn.commit()
    return rid


def _bind(conn: Any, partner_id: str, tg: str = TG) -> None:
    conn.execute(
        "UPDATE partner SET telegram_user_id = ?, updated_at_ms = ? WHERE id = ?",
        (tg, NOW, partner_id),
    )
    conn.commit()


def _send_capture() -> tuple[list[dict[str, Any]], Any]:
    sent: list[dict[str, Any]] = []

    def send(
        text: str, *, reply_markup: dict[str, Any] | None = None, chat_id: str | None = None, **kw: Any
    ) -> int:
        mid = len(sent) + 1
        sent.append(
            {
                "text": text,
                "reply_markup": reply_markup,
                "chat_id": None if chat_id is None else str(chat_id),
                "message_id": mid,
                **kw,
            }
        )
        return mid

    return sent, send


def _edit_capture() -> tuple[list[dict[str, Any]], Any]:
    edited: list[dict[str, Any]] = []

    def edit(mid: int, text: str, *, reply_markup: dict[str, Any] | None = None, **kw: Any) -> bool:
        edited.append({"message_id": mid, "text": text, "reply_markup": reply_markup})
        return True

    return edited, edit


def _msg(text: str) -> dict[str, Any]:
    return {
        "message_id": 1,
        "chat": {"id": int(ADMIN), "type": "private"},
        "from": {"id": int(ADMIN)},
        "text": text,
    }


def _cb(data: str, *, mid: int = 7) -> dict[str, Any]:
    return {
        "id": "cq",
        "data": data,
        "from": {"id": int(ADMIN)},
        "message": {"message_id": mid, "chat": {"id": int(ADMIN), "type": "private"}, "text": "card"},
    }


def _paid_cmd(target: str = "#a1b2c3", **over: str) -> str:
    bits = {
        "sold": "499",
        "rate": "33.90",
        "trade": "42.29",
        "net": "16853.81",
        "slip": "0123ABC",
    }
    bits.update(over)
    tail = " ".join(f"{key}={value}" for key, value in bits.items())
    return f"p paid {target} {tail}"


def test_p_paid_preview_snapshot(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "snap.db")
    pid = _partner(conn)
    _paid_request(conn, pid)
    sent, send = _send_capture()
    out = pa.handle_partner_text(conn, _paid_cmd(), message=_msg(_paid_cmd()), send=send, now_ms=RECORDED_AT)
    assert out["ok"] is True
    assert sent[0]["text"] == PREVIEW
    buttons = sent[0]["reply_markup"]["inline_keyboard"][0]
    assert buttons[0]["text"] == "✅ บันทึกโอนเงินบาท"
    assert buttons[1]["text"] == "✖️ ยกเลิก"
    assert len(buttons[0]["callback_data"].encode()) == 34
    conn.close()


def test_p_paid_notice_snapshot_and_second_carry(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "note.db")
    pid = _partner(conn)
    _bind(conn, pid)
    _paid_request(conn, pid)
    sent, send = _send_capture()
    preview = pa.handle_partner_text(conn, _paid_cmd(), message=_msg("p paid"), send=send, now_ms=RECORDED_AT)
    token = preview["token"]
    edited, edit = _edit_capture()
    answers: list[Any] = []
    saved = pa.handle_partner_callback(
        conn,
        f"pa:fy:{RID}:{token}",
        callback=_cb(f"pa:fy:{RID}:{token}"),
        send=send,
        answer=lambda *_a, **k: answers.append(k.get("text")),
        edit=edit,
        now_ms=RECORDED_AT,
    )
    assert saved["ok"] is True
    note = next(item["text"] for item in sent if item["chat_id"] == TG)
    assert note == NOTICE
    assert "เรทอ้างอิง" not in note
    row = fx.current_fx(conn, RID)
    assert row is not None and row["ref_status"] == "off" and row["ref_rate_e4"] is None
    assert "✅ แจ้งพาร์ทเนอร์แล้ว" in edited[-1]["text"]

    second = "pr_0000000000d4e5f6"
    _paid_request(conn, pid, rid=second, micro=300_000_000, paid_at=PAID_AT + 3_600_000)
    sent2, send2 = _send_capture()
    cmd = _paid_cmd(f"#{second[-6:]}", sold="300", rate="33.85", trade="25.39", net="10109.61", slip="NEXT")
    preview2 = pa.handle_partner_text(conn, cmd, message=_msg(cmd), send=send2, now_ms=RECORDED_AT + 1000)
    pa.handle_partner_callback(
        conn,
        f"pa:fy:{second}:{preview2['token']}",
        callback=_cb(f"pa:fy:{second}:{preview2['token']}"),
        send=send2,
        answer=lambda *_a, **_k: None,
        edit=_edit_capture()[1],
        now_ms=RECORDED_AT + 1000,
    )
    note2 = next(item["text"] for item in sent2 if item["chat_id"] == TG)
    assert "เศษยกมาจากครั้งก่อน +0.99 USDT" in note2
    assert "รับสุทธิ 10,109.61 บาท (เฉลี่ย 33.6975 บาท/USDT)" in note2
    assert "เศษ 0.98 USDT" in note2
    conn.close()


def test_parse_refuses_unknown_duplicate_and_extra_decimals(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "parse.db")
    sent, send = _send_capture()
    bad = "p paid som sold=499 rate=33.90 trade=42.29 net=1 fee=1"
    pa.handle_partner_text(conn, bad, message=_msg(bad), send=send, now_ms=NOW)
    assert sent[-1]["text"].startswith("p paid")
    dup = "p paid som sold=1 sold=2 rate=33.90 trade=1 net=1"
    pa.handle_partner_text(conn, dup, message=_msg(dup), send=send, now_ms=NOW)
    assert sent[-1]["text"].startswith("p paid")
    wide = "p paid som sold=499 rate=33.90123 trade=42.29 net=16853.81"
    pa.handle_partner_text(conn, wide, message=_msg(wide), send=send, now_ms=NOW)
    assert "ไม่ปัดให้" in sent[-1]["text"]
    missing = "p paid som rate=33.90 trade=42.29 net=16853.81"
    assert thb.parse_paid_command(missing) == "usage"
    upper = thb.parse_paid_command("p paid som SOLD=499 RATE=33.90 TRADE=42.29 NET=16853.81")
    assert isinstance(upper, dict) and upper["sold"] == 499_000_000
    commas = thb.parse_paid_command("p paid som sold=1,499 rate=33.90 trade=1,000.00 net=16,853.81")
    assert isinstance(commas, dict) and commas["sold"] == 1_499_000_000 and commas["trade"] == 100_000
    reason = thb.parse_paid_command(
        "p paid som sold=499 rate=33.90 trade=42.29 net=16853.81\nเหตุผลสิบตัวอักษรนะ"
    )
    assert isinstance(reason, dict) and reason["reason"] == "เหตุผลสิบตัวอักษรนะ"
    long_slip = "p paid som sold=499 rate=33.90 trade=42.29 net=16853.81 slip=" + ("A" * 41)
    assert thb.parse_paid_command(long_slip) == "usage"
    conn.close()


def test_target_refusals(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "tgt.db")
    pid = _partner(conn)
    due = "pr_0000000000aaaaaa"
    conn.execute(
        """
        INSERT INTO partner_request (
            id, partner_id, kind, status, payout_state, payout_micro,
            created_at_ms, expires_at_ms, decided_at_ms, source
        ) VALUES (?, ?, 'withdraw_profit', 'confirmed', 'due', 500000000, ?, ?, ?, 'admin')
        """,
        (due, pid, NOW, NOW + 1, NOW),
    )
    conn.commit()
    sent, send = _send_capture()
    cmd = _paid_cmd(f"#{due[-6:]}")
    pa.handle_partner_text(conn, cmd, message=_msg(cmd), send=send, now_ms=NOW)
    assert "ยังไม่ได้กด 📤" in sent[-1]["text"]
    assert sent[-1]["text"].endswith("ยังไม่บันทึกอะไร")
    conn.execute("UPDATE partner_request SET payout_state='paid', paid_at_ms=? WHERE id=?", (PAID_AT, due))
    conn.commit()
    _paid_request(conn, pid, rid="pr_0000000000bbbbbb")
    many = _paid_cmd("som")
    pa.handle_partner_text(conn, many, message=_msg(many), send=send, now_ms=NOW)
    assert "มีหลายรายการรอบันทึก" in sent[-1]["text"]
    conn.close()


def test_residual_over_tolerance_needs_a_reason(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "res.db")
    pid = _partner(conn)
    _paid_request(conn, pid)
    sent, send = _send_capture()
    cmd = _paid_cmd(net="16851.31")
    pa.handle_partner_text(conn, cmd, message=_msg(cmd), send=send, now_ms=NOW)
    assert "ส่วนต่าง −2.50 (เกินเกณฑ์ ±1.00)" in sent[-1]["text"]
    assert "ต้องใส่เหตุผลบรรทัดที่ 2" in sent[-1]["text"]
    assert sent[-1]["reply_markup"] is None
    conn.close()


def test_hard_rate_refusal_has_no_buttons(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "hard.db")
    pid = _partner(conn)
    _paid_request(conn, pid)
    sent, send = _send_capture()
    cmd = _paid_cmd(rate="61")
    pa.handle_partner_text(conn, cmd, message=_msg(cmd), send=send, now_ms=NOW)
    assert "เรทต้องอยู่ระหว่าง 20–60" in sent[-1]["text"]
    assert sent[-1]["text"].endswith("ยังไม่บันทึกอะไร")
    assert fx.fx_draft_row(conn, RID) is None
    conn.close()


def test_double_tap_and_replayed_update(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "tap.db")
    pid = _partner(conn)
    _bind(conn, pid)
    _paid_request(conn, pid)
    sent, send = _send_capture()
    preview = pa.handle_partner_text(conn, _paid_cmd(), message=_msg("x"), send=send, now_ms=RECORDED_AT)
    data = f"pa:fy:{RID}:{preview['token']}"
    update = {"update_id": 1, "callback_query": _cb(data)}
    first = listen.process_update(
        conn,
        update,
        send=send,
        answer=lambda *_a, **_k: None,
        edit=_edit_capture()[1],
        now_ms_value=RECORDED_AT,
    )
    second = listen.process_update(
        conn,
        update,
        send=send,
        answer=lambda *_a, **_k: None,
        edit=_edit_capture()[1],
        now_ms_value=RECORDED_AT,
    )
    assert first["ok"] is True
    assert second["reason"] == "already"
    assert conn.execute("SELECT COUNT(*) AS n FROM partner_payout_fx").fetchone()["n"] == 1
    assert sum(1 for item in sent if item["chat_id"] == TG) == 1
    conn.close()


def test_cancel_expired_and_replaced(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "life.db")
    pid = _partner(conn)
    _paid_request(conn, pid)
    sent, send = _send_capture()
    edited, edit = _edit_capture()
    first = pa.handle_partner_text(conn, _paid_cmd(), message=_msg("x"), send=send, edit=edit, now_ms=NOW)
    second = pa.handle_partner_text(
        conn, _paid_cmd(net="16853.81"), message=_msg("x"), send=send, edit=edit, now_ms=NOW + 1
    )
    stale = pa.handle_partner_callback(
        conn,
        f"pa:fy:{RID}:{first['token']}",
        callback=_cb(f"pa:fy:{RID}:{first['token']}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=edit,
        now_ms=NOW + 2,
    )
    assert stale["reason"] == "replaced"
    assert "มีรายการใหม่แทนแล้ว" in edited[-1]["text"]
    cancelled = pa.handle_partner_callback(
        conn,
        f"pa:fn:{RID}:{second['token']}",
        callback=_cb(f"pa:fn:{RID}:{second['token']}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=edit,
        now_ms=NOW + 3,
    )
    assert cancelled["ok"] is True
    assert fx.fx_draft_row(conn, RID) is None
    assert "ยกเลิกแล้ว" in edited[-1]["text"]
    fx.stage_fx_draft(conn, request_id=RID, mode="paid", payload={"op": "paid"}, created_by="42", now_ms=NOW)
    token = fx.fx_draft_row(conn, RID)["token"]
    expired = pa.handle_partner_callback(
        conn,
        f"pa:fy:{RID}:{token}",
        callback=_cb(f"pa:fy:{RID}:{token}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=edit,
        now_ms=NOW + fx.DRAFT_TTL_MS + 5,
    )
    assert expired["reason"] == "expired"
    conn.close()


def test_kyc_changed_and_carry_changed(
    tmp_path: Path, env_chat: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = open_db(tmp_path / "chg.db")
    pid = _partner(conn)
    _paid_request(conn, pid)
    sent, send = _send_capture()
    preview = pa.handle_partner_text(conn, _paid_cmd(), message=_msg("x"), send=send, now_ms=NOW)
    pk.admin_set_kyc(
        conn,
        pid,
        "ข้อมูลรับเงิน\nชื่อจริง: สมหญิง ใจดี\nเบอร์: 0899999999\nธนาคาร: SCB\nเลขบัญชี: 9999999999\nชื่อบัญชี: สมหญิง ใจดี\n",
        "เปลี่ยนบัญชี",
        NOW + 2,
    )
    edited, edit = _edit_capture()
    out = pa.handle_partner_callback(
        conn,
        f"pa:fy:{RID}:{preview['token']}",
        callback=_cb(f"pa:fy:{RID}:{preview['token']}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=edit,
        now_ms=NOW + 3,
    )
    assert out["reason"] == "kyc_changed"
    assert fx.current_fx(conn, RID) is None

    preview2 = pa.handle_partner_text(conn, _paid_cmd(), message=_msg("x"), send=send, now_ms=NOW + 4)
    monkeypatch.setattr(fx, "carry_balance", lambda *_a, **_k: 1_000)
    moved = pa.handle_partner_callback(
        conn,
        f"pa:fy:{RID}:{preview2['token']}",
        callback=_cb(f"pa:fy:{RID}:{preview2['token']}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=edit,
        now_ms=NOW + 6,
    )
    assert moved["reason"] == "carry_mismatch"
    assert "ยอดเศษยกมาเปลี่ยน" in edited[-1]["text"]
    conn.close()


def test_send_failure_then_resend_and_edit_failure(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "send.db")
    pid = _partner(conn)
    _bind(conn, pid)
    _paid_request(conn, pid)
    sent, send = _send_capture()
    preview = pa.handle_partner_text(conn, _paid_cmd(), message=_msg("x"), send=send, now_ms=RECORDED_AT)

    def fail_partner(
        text: str, *, reply_markup: dict[str, Any] | None = None, chat_id: str | None = None, **kw: Any
    ) -> int | None:
        if chat_id is not None:
            return None
        return send(text, reply_markup=reply_markup, chat_id=chat_id, **kw)

    edited, edit = _edit_capture()
    saved = pa.handle_partner_callback(
        conn,
        f"pa:fy:{RID}:{preview['token']}",
        callback=_cb(f"pa:fy:{RID}:{preview['token']}"),
        send=fail_partner,
        answer=lambda *_a, **_k: None,
        edit=edit,
        now_ms=RECORDED_AT,
    )
    assert saved["notified"] is False
    assert "แจ้งพาร์ทเนอร์ไม่สำเร็จ" in edited[-1]["text"]
    again = pa.handle_partner_callback(
        conn,
        f"pa:fs:{RID}",
        callback=_cb(f"pa:fs:{RID}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=edit,
        now_ms=RECORDED_AT + 1,
    )
    assert again["notified"] is True
    assert any(item["chat_id"] == TG for item in sent)

    def edit_fails(*_a: Any, **_k: Any) -> bool:
        return False

    pid2 = _partner(conn, "nok", now_ms=NOW + 50)
    _bind(conn, pid2, "502")
    rid2 = "pr_0000000000112233"
    _paid_request(conn, pid2, rid=rid2)
    cmd = _paid_cmd(f"#{rid2[-6:]}")
    preview2 = pa.handle_partner_text(conn, cmd, message=_msg(cmd), send=send, now_ms=RECORDED_AT)
    before = len(sent)
    pa.handle_partner_callback(
        conn,
        f"pa:fy:{rid2}:{preview2['token']}",
        callback=_cb(f"pa:fy:{rid2}:{preview2['token']}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=edit_fails,
        now_ms=RECORDED_AT,
    )
    assert len(sent) > before
    assert "แจ้งพาร์ทเนอร์แล้ว" in sent[-1]["text"]
    conn.close()


def test_exception_after_commit_does_not_say_nothing_saved(
    tmp_path: Path, env_chat: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = open_db(tmp_path / "boom.db")
    pid = _partner(conn)
    _bind(conn, pid)
    _paid_request(conn, pid)
    sent, send = _send_capture()
    preview = pa.handle_partner_text(conn, _paid_cmd(), message=_msg("x"), send=send, now_ms=RECORDED_AT)

    def _boom(*_a: Any, **_k: Any) -> str:
        raise RuntimeError("render")

    monkeypatch.setattr(thb, "partner_notice", _boom)
    edited, edit = _edit_capture()
    answers: list[Any] = []
    out = pa.handle_partner_callback(
        conn,
        f"pa:fy:{RID}:{preview['token']}",
        callback=_cb(f"pa:fy:{RID}:{preview['token']}"),
        send=send,
        answer=lambda *_a, **k: answers.append(k.get("text")),
        edit=edit,
        now_ms=RECORDED_AT,
    )
    assert out["cmd"] == "fx_saved_partial"
    assert fx.current_fx(conn, RID) is not None
    blob = " ".join(str(item) for item in answers) + edited[-1]["text"]
    assert "ยังไม่บันทึกอะไร" not in blob
    assert "ส่งข้อความไม่ครบ" in blob
    conn.close()


def test_closed_partner_stays_bound_until_thb_notice(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "bound.db")
    admin = ps.create_partner(conn, name="admin", is_admin=True, now_ms=NOW)
    assert ps.apply_deposit(conn, admin["partner_id"], D("1000"), D("1000"), NOW)["ok"]
    pid = _partner(conn, now_ms=NOW + 1)
    _bind(conn, pid)
    assert ps.apply_deposit(conn, pid, D("100"), D("1100"), NOW + 2)["ok"]
    closed = ps.apply_close(conn, pid, D("1100"), NOW + 3)
    assert closed["ok"] is True
    rid = next(str(row["id"]) for row in ps.list_due_payouts(conn) if str(row["partner_id"]) == pid)
    sent, send = _send_capture()
    paid = pa.handle_partner_callback(
        conn,
        f"pa:pd:{rid}",
        callback=_cb(f"pa:pd:{rid}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=_edit_capture()[1],
        now_ms=NOW + 4,
    )
    assert paid["ok"] is True
    note = next(item["text"] for item in sent if item["chat_id"] == TG)
    assert "ให้คุณแล้ว" not in note and "โอนเข้า:" not in note
    assert "บัญชีที่จะรับเงิน" in note
    assert ps.get_partner(conn, pid)["telegram_user_id"] == TG
    nxt = ps.create_partner(conn, name="next", now_ms=NOW + 5)
    invited = ps.create_invite(conn, nxt["partner_id"], now_ms=NOW + 6)
    refused = ps.bind_invite(conn, invited["code"], TG, admin_chat_id=ADMIN, now_ms=NOW + 7)
    assert refused["reason"] == "closed_payout_due"
    unbind = pa.handle_partner_text(
        conn, "p unbind som", message=_msg("p unbind som"), send=send, now_ms=NOW + 8
    )
    assert unbind["reason"] == "payout_due"
    assert "ยังรอบันทึกเงินบาท" in sent[-1]["text"]
    conn.close()


def test_step1_relabel_and_old_button(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "step.db")
    pid = _partner(conn)
    _bind(conn, pid)
    conn.execute(
        """
        INSERT INTO partner_request (
            id, partner_id, kind, status, payout_state, payout_micro,
            created_at_ms, expires_at_ms, decided_at_ms, source
        ) VALUES (?, ?, 'withdraw_profit', 'confirmed', 'due', 500000000, ?, ?, ?, 'admin')
        """,
        (RID, pid, NOW, NOW + 1, NOW),
    )
    conn.commit()
    sent, send = _send_capture()
    pa.handle_partner_text(conn, "p due", message=_msg("p due"), send=send, now_ms=PAID_AT)
    assert "ค้างโอน USDT" in sent[0]["text"]
    assert sent[0]["reply_markup"]["inline_keyboard"][0][0]["text"] == "📤 USDT ออกจากพอร์ตแล้ว"
    edited, edit = _edit_capture()
    pa.handle_partner_callback(
        conn,
        f"pa:pd:{RID}",
        callback=_cb(f"pa:pd:{RID}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=edit,
        now_ms=PAID_AT,
    )
    assert "USDT ออกจากพอร์ตแล้ว" in edited[-1]["text"]
    assert "p paid #a1b2c3" in edited[-1]["text"]
    conn.close()


def test_p_due_sections_overview_and_confirm_not_blocked(
    tmp_path: Path, env_chat: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = open_db(tmp_path / "due.db")
    admin = ps.create_partner(conn, name="admin", is_admin=True, now_ms=NOW)
    assert ps.apply_deposit(conn, str(admin["partner_id"]), D("1000"), D("1000"), NOW)["ok"]
    pid = _partner(conn, now_ms=NOW + 1)
    _paid_request(conn, pid)
    sent, send = _send_capture()
    pa.handle_partner_text(conn, "p due", message=_msg("p due"), send=send, now_ms=PAID_AT)
    assert sent[0]["text"].startswith("รอบันทึกเงินบาท (1)")
    assert "#a1b2c3" in sent[0]["text"]
    assert sent[0]["reply_markup"] is None
    monkeypatch.setattr(
        pa,
        "_read_v",
        lambda *_a, **_k: AccountValue(
            value=D("1000"), wallet=D("1000"), unrealized=D("0"), open_positions=0, read_at_ms=PAID_AT
        ),
    )
    pa.handle_partner_text(conn, "p", message=_msg("p"), send=send, futures=object(), now_ms=PAID_AT)
    assert "รอบันทึกเงินบาท 1 รายการ" in sent[-1]["text"]
    profile = partner_profile.profile_data(conn, pid, PAID_AT, account_value=D("1000"))
    rendered = partner_profile.render_profile(profile)
    assert "กำลังแปลงเป็นเงินบาท 500.00 USDT" in rendered
    assert ps.payouts_due_total(conn) == D("0")
    conn.close()


def test_p_fix_partial_and_null_kyc(tmp_path: Path, env_chat: None, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = open_db(tmp_path / "fix.db")
    pid = _partner(conn)
    _bind(conn, pid)
    _paid_request(conn, pid)
    sent, send = _send_capture()
    preview = pa.handle_partner_text(conn, _paid_cmd(), message=_msg("x"), send=send, now_ms=RECORDED_AT)
    pa.handle_partner_callback(
        conn,
        f"pa:fy:{RID}:{preview['token']}",
        callback=_cb(f"pa:fy:{RID}:{preview['token']}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=_edit_capture()[1],
        now_ms=RECORDED_AT,
    )
    same = "p fix #a1b2c3 bank=0\nไม่มีอะไรเปลี่ยนจริง"
    pa.handle_partner_text(conn, same, message=_msg(same), send=send, now_ms=RECORDED_AT + 1)
    assert "ไม่มีอะไรเปลี่ยน" in sent[-1]["text"]
    fix = "p fix #a1b2c3 bank=10 net=16843.81\nธนาคารหักค่าโอน 10 บาท ตามสลิปจริง"
    fixed = pa.handle_partner_text(conn, fix, message=_msg(fix), send=send, now_ms=RECORDED_AT + 2)
    assert fixed["ok"] is True
    edited, edit = _edit_capture()
    pa.handle_partner_callback(
        conn,
        f"pa:fy:{RID}:{fixed['token']}",
        callback=_cb(f"pa:fy:{RID}:{fixed['token']}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=edit,
        now_ms=RECORDED_AT + 3,
    )
    note = next(item["text"] for item in reversed(sent) if item["chat_id"] == TG)
    assert "แก้ครั้งที่ 1" in note
    assert "16,843.81" in note
    assert "33.7544" in note
    assert fx.current_fx(conn, RID)["paid_kyc_id"]

    monkeypatch.setenv("JARVISE_TEST_NO_KYC_SEED", "1")
    bare = ps.create_partner(conn, name="bare", now_ms=NOW + 20)
    bid = str(bare["partner_id"])
    brid = "pr_0000000000abc123"
    _paid_request(conn, bid, rid=brid)
    warned = _paid_cmd(f"#{brid[-6:]}")
    card = pa.handle_partner_text(conn, warned, message=_msg(warned), send=send, now_ms=RECORDED_AT)
    assert "ยังไม่มีข้อมูลรับเงิน" in sent[-1]["text"]
    pa.handle_partner_callback(
        conn,
        f"pa:fy:{brid}:{card['token']}",
        callback=_cb(f"pa:fy:{brid}:{card['token']}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=edit,
        now_ms=RECORDED_AT,
    )
    assert fx.current_fx(conn, brid)["paid_kyc_id"] is None
    conn.close()


def test_zero_fee_lines_hidden_and_logs_skip_slip(
    tmp_path: Path, env_chat: None, caplog: pytest.LogCaptureFixture
) -> None:
    conn = open_db(tmp_path / "hide.db")
    pid = _partner(conn)
    _bind(conn, pid)
    _paid_request(conn, pid, micro=1_010_000)
    sent, send = _send_capture()
    cmd = "p paid #a1b2c3 sold=1 rate=33.90 trade=0.00 wd=0 bank=0 net=33.90 slip=SECRET-SLIP"
    preview = pa.handle_partner_text(conn, cmd, message=_msg(cmd), send=send, now_ms=RECORDED_AT)
    caplog.set_level(logging.INFO)
    pa.handle_partner_callback(
        conn,
        f"pa:fy:{RID}:{preview['token']}",
        callback=_cb(f"pa:fy:{RID}:{preview['token']}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=_edit_capture()[1],
        now_ms=RECORDED_AT,
    )
    note = next(item["text"] for item in sent if item["chat_id"] == TG)
    assert "ค่าโอนธนาคาร" not in note
    assert "ค่าถอนเงินบาท" not in note
    assert "SECRET-SLIP" not in caplog.text
    assert "KBANK" not in caplog.text
    conn.close()


def test_legacy_paid_before_cutoff_not_listed(
    tmp_path: Path, env_chat: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = open_db(tmp_path / "old.db")
    pid = _partner(conn)
    _paid_request(conn, pid, paid_at=100)
    monkeypatch.setattr(fx, "THB_FLOW_SINCE_MS", 150)
    monkeypatch.setattr(thb.fx, "THB_FLOW_SINCE_MS", 150)
    sent, send = _send_capture()
    pa.handle_partner_text(conn, "p due", message=_msg("p due"), send=send, now_ms=NOW)
    assert sent[-1]["text"] == "ไม่มีรายการค้าง"
    cmd = _paid_cmd("#a1b2c3")
    pa.handle_partner_text(conn, cmd, message=_msg(cmd), send=send, now_ms=NOW)
    assert "จ่ายก่อนเริ่มระบบเงินบาท" in sent[-1]["text"]
    conn.close()


def test_p_carry_settle_happy_path_unbinds_closed(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "carry.db")
    pid = _partner(conn, "nok")
    _bind(conn, pid)
    conn.execute("UPDATE partner SET status = 'closed', updated_at_ms = ? WHERE id = ?", (NOW + 2, pid))
    conn.commit()
    rid = "pr_a0c10ce000000001"
    _paid_request(conn, pid, rid=rid, kind="close", micro=1_990_000, paid_at=NOW + 3)
    saved = fx.record_payout_fx(
        conn,
        request_id=rid,
        carry_in_micro=0,
        chain_fee_micro=10_000,
        usdt_sold_micro=1_000_000,
        rate=D("33.10"),
        trade_fee_satang=0,
        thb_wd_fee_satang=0,
        bank_fee_satang=0,
        net_satang=3_310,
        ref_status="off",
        paid_kyc_id=pk.current_kyc(conn, pid)["id"],
        entered_by="42",
        now_ms=NOW + 4,
    )
    assert saved["ok"] is True
    assert fx.carry_balance(conn, pid) == 980_000
    conn.execute("UPDATE partner SET status = 'closed', updated_at_ms = ? WHERE id = ?", (NOW + 5, pid))
    conn.commit()
    sent, send = _send_capture()
    text = "p carry nok settle 0.98 33.10\nโอนเศษเป็นเงินบาทตามสลิป"
    preview = pa.handle_partner_text(conn, text, message=_msg(text), send=send, now_ms=NOW + 8)
    assert preview["thb_satang"] == 3244
    saved_carry = pa.handle_partner_callback(
        conn,
        f"pa:fy:{preview['request_id']}:{preview['token']}",
        callback=_cb(f"pa:fy:{preview['request_id']}:{preview['token']}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=_edit_capture()[1],
        now_ms=NOW + 9,
    )
    assert saved_carry["ok"] is True, saved_carry
    note = next(item["text"] for item in sent if item["chat_id"] == TG)
    assert "💸 ชำระเศษ 0.98 USDT เป็นเงินบาท 32.44 บาท" in note
    assert "ปิดพอร์ตเรียบร้อย" in note
    assert ps.get_partner(conn, pid)["telegram_user_id"] is None
    assert fx.carry_balance(conn, pid) == 0
    conn.close()


def test_p_carry_settle_amount_mismatch_refused(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "cmis.db")
    pid = _partner(conn, "nok")
    rid = "pr_nokclose0000002"
    _paid_request(conn, pid, rid=rid, kind="close", micro=1_990_000)
    assert fx.record_payout_fx(
        conn,
        request_id=rid,
        carry_in_micro=0,
        chain_fee_micro=10_000,
        usdt_sold_micro=1_000_000,
        rate=D("33.10"),
        trade_fee_satang=0,
        thb_wd_fee_satang=0,
        bank_fee_satang=0,
        net_satang=3_310,
        ref_status="off",
        paid_kyc_id=pk.current_kyc(conn, pid)["id"],
        entered_by="42",
        now_ms=NOW + 2,
    )["ok"]
    conn.execute("UPDATE partner SET status = 'closed', updated_at_ms = ? WHERE id = ?", (NOW + 3, pid))
    conn.commit()
    sent, send = _send_capture()
    text = "p carry nok settle 0.50 33.10\nโอนเศษเป็นเงินบาทตามสลิป"
    out = pa.handle_partner_text(conn, text, message=_msg(text), send=send, now_ms=NOW + 1)
    assert out["reason"] == "carry_mismatch"
    assert sent[-1]["text"].endswith("ยังไม่บันทึกอะไร")
    assert fx.carry_balance(conn, pid) == 980_000
    conn.close()


def test_p_carry_settle_needs_reason(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "creason.db")
    pid = _partner(conn, "nok")
    conn.execute("UPDATE partner SET status = 'closed', updated_at_ms = ? WHERE id = ?", (NOW, pid))
    conn.commit()
    sent, send = _send_capture()
    text = "p carry nok settle 0.98 33.10"
    out = pa.handle_partner_text(conn, text, message=_msg(text), send=send, now_ms=NOW + 1)
    assert out["reason"] == "reason_required"
    assert "ต้องใส่เหตุผล" in sent[-1]["text"]
    conn.close()


FIX_NOTICE = """✏️ แก้ไขรายการโอนเงินบาท (แก้ครั้งที่ 1) · ถอนกำไร 500.00 USDT
ค่าโอนธนาคาร: เดิม 0.00 → ใหม่ 10.00 บาท
รับสุทธิ: เดิม 16,853.81 → ใหม่ 16,843.81 บาท
เหตุผล: ธนาคารหักค่าโอน 10 บาท ตามสลิปจริง
— รายละเอียดใหม่ —
💸 โอนเงินบาทเข้าบัญชีของคุณแล้ว · ถอนกำไร
ยอดถอน 500.00 USDT
ค่าธรรมเนียมเครือข่าย −0.01 USDT
ขาย 499 USDT × 33.90 = 16,916.10 บาท
ค่าธรรมเนียมซื้อขาย −42.29 บาท
ค่าถอนเงินบาท −20.00 บาท
ค่าโอนธนาคาร −10.00 บาท
รับสุทธิ 16,843.81 บาท (เฉลี่ย 33.7544 บาท/USDT)
เศษ 0.99 USDT ยังไม่ได้ขาย (ขายได้ขั้นละ 1 USDT) — ยกไปรวมกับการถอนครั้งถัดไป
โอนเข้า: KBANK ••••1234 (สมชาย ใ***)
บันทึก 18/11/2026 14:05 · อ้างอิงสลิป 0123ABC"""


def test_correction_notice_snapshot(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "fixsnap.db")
    pid = _partner(conn)
    _bind(conn, pid)
    _paid_request(conn, pid)
    sent, send = _send_capture()
    preview = pa.handle_partner_text(conn, _paid_cmd(), message=_msg("x"), send=send, now_ms=RECORDED_AT)
    pa.handle_partner_callback(
        conn,
        f"pa:fy:{RID}:{preview['token']}",
        callback=_cb(f"pa:fy:{RID}:{preview['token']}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=_edit_capture()[1],
        now_ms=RECORDED_AT,
    )
    fix = "p fix #a1b2c3 bank=10 net=16843.81\nธนาคารหักค่าโอน 10 บาท ตามสลิปจริง"
    fixed = pa.handle_partner_text(
        conn, fix, message=_msg(fix), send=send, edit=_edit_capture()[1], now_ms=RECORDED_AT
    )
    assert "ค่าโอนธนาคาร: เดิม 0.00 → ใหม่ 10.00 บาท" in sent[-1]["text"]
    pa.handle_partner_callback(
        conn,
        f"pa:fy:{RID}:{fixed['token']}",
        callback=_cb(f"pa:fy:{RID}:{fixed['token']}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=_edit_capture()[1],
        now_ms=RECORDED_AT + 3,
    )
    note = next(item["text"] for item in reversed(sent) if item["chat_id"] == TG)
    assert note == FIX_NOTICE
    conn.close()


def test_kyc_changed_warning_on_preview(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "kycw.db")
    pid = _partner(conn)
    _paid_request(conn, pid)
    first = pk.current_kyc(conn, pid)
    assert first is not None
    conn.execute("UPDATE partner_request SET payout_kyc_id = ? WHERE id = ?", (first["id"], RID))
    conn.commit()
    saved = pk.admin_set_kyc(
        conn,
        pid,
        "ข้อมูลรับเงิน\nชื่อจริง: สมหญิง ใจดี\nเบอร์: 0899999999\nธนาคาร: SCB\nเลขบัญชี: 9999999999\nชื่อบัญชี: สมหญิง ใจดี\n",
        "เปลี่ยนบัญชีรับเงิน",
        NOW + 5,
    )
    assert saved["ok"] is True
    sent, send = _send_capture()
    out = pa.handle_partner_text(conn, _paid_cmd(), message=_msg("x"), send=send, now_ms=NOW + 6)
    assert out["ok"] is True
    assert "⚠️ บัญชีรับเงินเปลี่ยนหลังยืนยัน — จะบันทึกบัญชีปัจจุบัน" in sent[-1]["text"]
    conn.close()


def test_unsellable_close_unbinds_at_step1(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "tiny.db")
    pid = _partner(conn, "tiny")
    _bind(conn, pid)
    conn.execute("UPDATE partner SET status = 'closed', updated_at_ms = ? WHERE id = ?", (NOW, pid))
    rid = "pr_0000000000000050"
    conn.execute(
        """
        INSERT INTO partner_request (
            id, partner_id, kind, status, payout_state, payout_micro,
            created_at_ms, expires_at_ms, decided_at_ms, source
        ) VALUES (?, ?, 'close', 'confirmed', 'due', 500000, ?, ?, ?, 'admin')
        """,
        (rid, pid, NOW, NOW + 1, NOW),
    )
    conn.commit()
    sent, send = _send_capture()
    edited, edit = _edit_capture()
    out = pa.handle_partner_callback(
        conn,
        f"pa:pd:{rid}",
        callback=_cb(f"pa:pd:{rid}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=edit,
        now_ms=NOW + 2,
    )
    assert out["ok"] is True
    note = next(item["text"] for item in sent if item["chat_id"] == TG)
    assert "💸 แอดมินแจ้งว่าโอน 0.50 USDT ให้คุณแล้ว" in note
    assert "บอทจะไม่แสดงยอดอีก" in note
    assert "ยอดน้อยกว่า 1 USDT" in edited[-1]["text"]
    assert "p paid" not in edited[-1]["text"]
    assert ps.get_partner(conn, pid)["telegram_user_id"] is None
    listed = pa.handle_partner_text(conn, "p due", message=_msg("p due"), send=send, now_ms=NOW + 3)
    assert listed["n"] == 0
    conn.close()


def _record_simple(
    conn: Any,
    pid: str,
    rid: str,
    *,
    payout: int,
    sold: int,
    carry: int = 0,
    chain: int = 10_000,
    now_ms: int = NOW,
) -> None:
    kyc = pk.current_kyc(conn, pid)
    assert kyc is not None
    gross = sold * 331_000 // 100_000_000
    saved = fx.record_payout_fx(
        conn,
        request_id=rid,
        carry_in_micro=carry,
        chain_fee_micro=chain,
        usdt_sold_micro=sold,
        rate=D("33.10"),
        trade_fee_satang=0,
        thb_wd_fee_satang=0,
        bank_fee_satang=0,
        net_satang=gross,
        ref_status="off",
        paid_kyc_id=str(kyc["id"]),
        entered_by="42",
        now_ms=now_ms,
    )
    assert saved["ok"] is True, saved


def test_unsellable_close_with_carry_is_not_awaiting(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "tinycarry.db")
    pid = _partner(conn, "nok")
    _bind(conn, pid)
    first = "pr_00000000000000a1"
    _paid_request(conn, pid, rid=first, micro=2_000_000)
    _record_simple(conn, pid, first, payout=2_000_000, sold=1_000_000, now_ms=NOW + 2)
    assert fx.carry_balance(conn, pid) == 990_000
    close = "pr_0000000000000051"
    _paid_request(conn, pid, rid=close, kind="close", micro=500_000, paid_at=NOW + 3)
    conn.execute("UPDATE partner SET status = 'closed', updated_at_ms = ? WHERE id = ?", (NOW + 4, pid))
    conn.commit()
    assert [row["id"] for row in fx.awaiting_thb(conn, pid)] == []
    assert any(row.get("stage") == "carry_closed" for row in fx.open_settlements(conn, pid))
    assert ps.get_partner(conn, pid)["telegram_user_id"] == TG
    sent, send = _send_capture()
    refused = "p paid #000051 sold=1 rate=33.10 trade=0 net=33.10"
    out = pa.handle_partner_text(conn, refused, message=_msg(refused), send=send, now_ms=NOW + 5)
    assert out["reason"] == "target"
    assert "รายการนี้จบที่ขั้น 📤 แล้ว" in sent[-1]["text"]
    assert "ยังไม่บันทึกอะไร" in sent[-1]["text"]
    assert fx.fx_draft_row(conn, close) is None
    text = "p carry nok settle 0.99 33.10\nโอนเศษเป็นเงินบาทตามสลิป"
    preview = pa.handle_partner_text(conn, text, message=_msg(text), send=send, now_ms=NOW + 6)
    assert preview["ok"] is True, preview
    saved = pa.handle_partner_callback(
        conn,
        f"pa:fy:{preview['request_id']}:{preview['token']}",
        callback=_cb(f"pa:fy:{preview['request_id']}:{preview['token']}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=_edit_capture()[1],
        now_ms=NOW + 7,
    )
    assert saved["ok"] is True, saved
    assert ps.get_partner(conn, pid)["telegram_user_id"] is None
    assert fx.carry_balance(conn, pid) == 0
    conn.close()


def test_expired_fix_and_carry_drafts(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "exp.db")
    pid = _partner(conn, "nok")
    _bind(conn, pid)
    _paid_request(conn, pid)
    sent, send = _send_capture()
    edited, edit = _edit_capture()
    preview = pa.handle_partner_text(conn, _paid_cmd(), message=_msg("x"), send=send, now_ms=RECORDED_AT)
    pa.handle_partner_callback(
        conn,
        f"pa:fy:{RID}:{preview['token']}",
        callback=_cb(f"pa:fy:{RID}:{preview['token']}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=edit,
        now_ms=RECORDED_AT,
    )
    fix = "p fix #a1b2c3 bank=10 net=16843.81\nธนาคารหักค่าโอน 10 บาท ตามสลิปจริง"
    fixed = pa.handle_partner_text(conn, fix, message=_msg(fix), send=send, edit=edit, now_ms=RECORDED_AT + 1)
    expired = pa.handle_partner_callback(
        conn,
        f"pa:fy:{RID}:{fixed['token']}",
        callback=_cb(f"pa:fy:{RID}:{fixed['token']}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=edit,
        now_ms=RECORDED_AT + fx.DRAFT_TTL_MS + 5,
    )
    assert expired["reason"] == "expired"
    assert "พิมพ์คำสั่งใหม่" in edited[-1]["text"]
    assert fx.current_fx(conn, RID)["version"] == 1

    conn.execute("UPDATE partner SET status = 'closed', updated_at_ms = ? WHERE id = ?", (NOW, pid))
    conn.commit()
    # A second close with leftover so p carry has a balance, anchored on a row that already has fx.
    close_id = "pr_a0c10ce0000000c1"
    _paid_request(conn, pid, rid=close_id, kind="close", micro=1_990_000, paid_at=NOW + 3)
    kyc = pk.current_kyc(conn, pid)
    assert fx.record_payout_fx(
        conn,
        request_id=close_id,
        carry_in_micro=fx.carry_balance(conn, pid),
        chain_fee_micro=10_000,
        usdt_sold_micro=1_000_000,
        rate=D("33.10"),
        trade_fee_satang=0,
        thb_wd_fee_satang=0,
        bank_fee_satang=0,
        net_satang=3_310,
        ref_status="off",
        paid_kyc_id=str(kyc["id"]),
        entered_by="42",
        now_ms=NOW + 4,
    )["ok"]
    before = fx.carry_balance(conn, pid)
    text = f"p carry nok settle {_usdt(before)} 33.10\nโอนเศษเป็นเงินบาทตามสลิป"
    carry = pa.handle_partner_text(conn, text, message=_msg(text), send=send, edit=edit, now_ms=NOW + 8)
    assert carry["ok"] is True, carry
    late = pa.handle_partner_callback(
        conn,
        f"pa:fy:{carry['request_id']}:{carry['token']}",
        callback=_cb(f"pa:fy:{carry['request_id']}:{carry['token']}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=edit,
        now_ms=NOW + 8 + fx.DRAFT_TTL_MS + 5,
    )
    assert late["reason"] == "expired"
    assert fx.carry_balance(conn, pid) == before
    conn.close()


def _usdt(micro: int) -> str:
    return f"{Decimal(micro) / Decimal(1_000_000):f}"


def test_admin_due_is_listed(tmp_path: Path, env_chat: None, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = open_db(tmp_path / "admindue.db")
    admin = ps.create_partner(conn, name="admin", is_admin=True, now_ms=NOW)
    assert ps.apply_deposit(conn, str(admin["partner_id"]), D("1000"), D("1000"), NOW)["ok"]
    monkeypatch.setattr(
        pa,
        "_transfer_guard",
        lambda *_a, **_k: (
            GuardResult(status="ok", checked_at_ms=NOW, findings=[], warn=False, error=None),
            "",
        ),
    )
    monkeypatch.setattr(
        pa,
        "read_account_value",
        lambda *_a, **_k: AccountValue(
            wallet=D("2000"), unrealized=D("0"), value=D("2000"), open_positions=0, read_at_ms=NOW + 2
        ),
    )
    sent, send = _send_capture()
    opened = pa.handle_partner_text(
        conn, "p wd admin", message=_msg("p wd admin"), send=send, futures=object(), now_ms=NOW + 2
    )
    assert opened["ok"] is True, opened
    button = sent[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    pa.handle_partner_callback(
        conn,
        button,
        callback=_cb(button),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=_edit_capture()[1],
        futures=object(),
        now_ms=NOW + 3,
    )
    due = pa.handle_partner_text(conn, "p due", message=_msg("p due"), send=send, now_ms=NOW + 4)
    assert due["n"] >= 1
    card = next(item for item in reversed(sent) if item["reply_markup"])
    assert "ค้างโอน USDT" in card["text"]
    assert card["reply_markup"]["inline_keyboard"][0][0]["text"] == "📤 USDT ออกจากพอร์ตแล้ว"
    conn.close()


def test_carry_reason_stores_thb_and_resend_unbinds(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "creason2.db")
    pid = _partner(conn, "nok")
    _bind(conn, pid)
    conn.execute("UPDATE partner SET status = 'closed', updated_at_ms = ? WHERE id = ?", (NOW, pid))
    conn.commit()
    rid = "pr_a0c10ce0000000d1"
    _paid_request(conn, pid, rid=rid, kind="close", micro=1_990_000, paid_at=NOW + 3)
    kyc = pk.current_kyc(conn, pid)
    assert fx.record_payout_fx(
        conn,
        request_id=rid,
        carry_in_micro=0,
        chain_fee_micro=10_000,
        usdt_sold_micro=1_000_000,
        rate=D("33.10"),
        trade_fee_satang=0,
        thb_wd_fee_satang=0,
        bank_fee_satang=0,
        net_satang=3_310,
        ref_status="off",
        paid_kyc_id=str(kyc["id"]),
        entered_by="42",
        now_ms=NOW + 4,
    )["ok"]
    sent, send = _send_capture()

    def fail_partner(
        text: str, *, reply_markup: dict[str, Any] | None = None, chat_id: str | None = None, **kw: Any
    ) -> int | None:
        if chat_id is not None:
            return None
        return send(text, reply_markup=reply_markup, chat_id=chat_id, **kw)

    text = "p carry nok settle 0.98 33.10\nโอนเศษเป็นเงินบาทตามสลิป"
    preview = pa.handle_partner_text(conn, text, message=_msg(text), send=send, now_ms=NOW + 8)
    saved = pa.handle_partner_callback(
        conn,
        f"pa:fy:{preview['request_id']}:{preview['token']}",
        callback=_cb(f"pa:fy:{preview['request_id']}:{preview['token']}"),
        send=fail_partner,
        answer=lambda *_a, **_k: None,
        edit=_edit_capture()[1],
        now_ms=NOW + 9,
    )
    assert saved["ok"] is True, saved
    row = conn.execute(
        "SELECT reason FROM partner_usdt_carry WHERE partner_id = ? AND kind = 'settle'",
        (pid,),
    ).fetchone()
    assert row["reason"] == "THB 32.44 @33.10 · โอนเศษเป็นเงินบาทตามสลิป"
    assert ps.get_partner(conn, pid)["telegram_user_id"] == TG
    resent = pa.handle_partner_callback(
        conn,
        f"pa:fc:{preview['request_id']}",
        callback=_cb(f"pa:fc:{preview['request_id']}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=_edit_capture()[1],
        now_ms=NOW + 10,
    )
    assert resent["ok"] is True, resent
    note = next(item["text"] for item in reversed(sent) if item["chat_id"] == TG)
    assert note.startswith("💸 ชำระเศษ 0.98 USDT เป็นเงินบาท 32.44 บาท")
    assert ps.get_partner(conn, pid)["telegram_user_id"] is None
    conn.close()


def test_p_carry_refuses_while_close_awaits_thb(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "copen.db")
    pid = _partner(conn, "nok")
    conn.execute("UPDATE partner SET status = 'closed', updated_at_ms = ? WHERE id = ?", (NOW, pid))
    conn.commit()
    rid = "pr_a0c10ce0000000e1"
    _paid_request(conn, pid, rid=rid, kind="close", micro=2_000_000)
    sent, send = _send_capture()
    paid = "p paid #0000e1 sold=1 rate=33.10 chain=0.01 trade=0 wd=0 bank=0 net=33.10"
    staged = pa.handle_partner_text(conn, paid, message=_msg(paid), send=send, now_ms=NOW + 2)
    assert staged["ok"] is True, staged
    token = fx.fx_draft_row(conn, rid)["token"]
    text = "p carry nok settle 0.99 33.10\nโอนเศษเป็นเงินบาทตามสลิป"
    refused = pa.handle_partner_text(conn, text, message=_msg(text), send=send, now_ms=NOW + 3)
    assert refused["reason"] == "open_settlement"
    assert "บันทึก p paid ให้ครบก่อน" in sent[-1]["text"]
    assert fx.fx_draft_row(conn, rid)["token"] == token
    conn.close()


def test_two_awaiting_unbinds_only_after_the_second(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "two.db")
    pid = _partner(conn, "nok")
    _bind(conn, pid)
    conn.execute("UPDATE partner SET status = 'closed', updated_at_ms = ? WHERE id = ?", (NOW, pid))
    conn.commit()
    first = "pr_0000000000000aa1"
    second = "pr_0000000000000aa2"
    _paid_request(conn, pid, rid=first, kind="withdraw_profit", micro=2_000_000, paid_at=NOW + 1)
    _paid_request(conn, pid, rid=second, kind="close", micro=2_000_000, paid_at=NOW + 2)
    sent, send = _send_capture()
    for rid, when in ((first, NOW + 3), (second, NOW + 6)):
        cmd = f"p paid #{rid[-6:]} sold=2 rate=33.10 chain=0 trade=0 wd=0 bank=0 net=66.20"
        preview = pa.handle_partner_text(conn, cmd, message=_msg(cmd), send=send, now_ms=when)
        assert preview["ok"] is True, preview
        saved = pa.handle_partner_callback(
            conn,
            f"pa:fy:{rid}:{preview['token']}",
            callback=_cb(f"pa:fy:{rid}:{preview['token']}"),
            send=send,
            answer=lambda *_a, **_k: None,
            edit=_edit_capture()[1],
            now_ms=when + 1,
        )
        assert saved["ok"] is True, saved
        if rid == first:
            assert ps.get_partner(conn, pid)["telegram_user_id"] == TG
    assert ps.get_partner(conn, pid)["telegram_user_id"] is None
    conn.close()


def test_close_leftover_stays_bound_and_blocks_rebind(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "left.db")
    pid = _partner(conn, "nok")
    _bind(conn, pid)
    conn.execute("UPDATE partner SET status = 'closed', updated_at_ms = ? WHERE id = ?", (NOW, pid))
    conn.commit()
    rid = "pr_0000000000000bb1"
    _paid_request(conn, pid, rid=rid, kind="close", micro=2_000_000)
    sent, send = _send_capture()
    cmd = "p paid #000bb1 sold=1 rate=33.10 chain=0.01 trade=0 wd=0 bank=0 net=33.10"
    preview = pa.handle_partner_text(conn, cmd, message=_msg(cmd), send=send, now_ms=NOW + 3)
    saved = pa.handle_partner_callback(
        conn,
        f"pa:fy:{rid}:{preview['token']}",
        callback=_cb(f"pa:fy:{rid}:{preview['token']}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=_edit_capture()[1],
        now_ms=NOW + 4,
    )
    assert saved["ok"] is True, saved
    note = next(item["text"] for item in sent if item["chat_id"] == TG)
    assert "ยกไปรวมกับการถอนครั้งถัดไป" not in note
    assert "แอดมินจะแจ้งการชำระเศษนี้แยกต่างหาก" in note
    assert ps.get_partner(conn, pid)["telegram_user_id"] == TG
    other = ps.create_partner(conn, name="neo", now_ms=NOW + 5)
    invited = ps.create_invite(conn, str(other["partner_id"]), now_ms=NOW + 6)
    bound = ps.bind_invite(conn, invited["code"], TG, admin_chat_id=ADMIN, now_ms=NOW + 7)
    assert bound == {"ok": False, "reason": "closed_payout_due"}
    due = pa.handle_partner_text(conn, "p due", message=_msg("p due"), send=send, now_ms=NOW + 8)
    assert due["ok"] is True
    assert any("เศษค้าง" in item["text"] for item in sent)
    conn.close()


def test_p_fix_carry_locked_refused_upfront(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "locked.db")
    pid = _partner(conn)
    first = "pr_0000000000000cc1"
    second = "pr_0000000000000cc2"
    _paid_request(conn, pid, rid=first, micro=2_000_000, paid_at=NOW + 1)
    kyc = pk.current_kyc(conn, pid)
    assert fx.record_payout_fx(
        conn,
        request_id=first,
        carry_in_micro=0,
        chain_fee_micro=10_000,
        usdt_sold_micro=1_000_000,
        rate=D("33.10"),
        trade_fee_satang=0,
        thb_wd_fee_satang=0,
        bank_fee_satang=0,
        net_satang=3_310,
        ref_status="off",
        paid_kyc_id=str(kyc["id"]),
        entered_by="42",
        now_ms=NOW + 2,
    )["ok"]
    _paid_request(conn, pid, rid=second, micro=1_990_000, paid_at=NOW + 3)
    assert fx.record_payout_fx(
        conn,
        request_id=second,
        carry_in_micro=fx.carry_balance(conn, pid),
        chain_fee_micro=10_000,
        usdt_sold_micro=1_000_000,
        rate=D("33.10"),
        trade_fee_satang=0,
        thb_wd_fee_satang=0,
        bank_fee_satang=0,
        net_satang=3_310,
        ref_status="off",
        paid_kyc_id=str(kyc["id"]),
        entered_by="42",
        now_ms=NOW + 4,
    )["ok"]
    sent, send = _send_capture()
    cmd = "p fix #000cc1 chain=0\nเปลี่ยนค่าเครือข่ายหลังเศษถูกใช้แล้ว"
    out = pa.handle_partner_text(conn, cmd, message=_msg(cmd), send=send, now_ms=NOW + 5)
    assert out["reason"] == "carry_consumed"
    assert "แก้ได้เฉพาะ rate/trade/wd/bank/net" in sent[-1]["text"]
    assert fx.fx_draft_row(conn, first) is None
    conn.close()


def test_stale_cancel_keeps_the_newer_draft(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "stale.db")
    pid = _partner(conn)
    _paid_request(conn, pid)
    sent, send = _send_capture()
    edited, edit = _edit_capture()
    first = pa.handle_partner_text(conn, _paid_cmd(), message=_msg("x"), send=send, edit=edit, now_ms=NOW)
    second = pa.handle_partner_text(
        conn, _paid_cmd(), message=_msg("x"), send=send, edit=edit, now_ms=NOW + 1
    )
    cancelled = pa.handle_partner_callback(
        conn,
        f"pa:fn:{RID}:{first['token']}",
        callback=_cb(f"pa:fn:{RID}:{first['token']}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=edit,
        now_ms=NOW + 2,
    )
    assert cancelled["reason"] == "replaced"
    assert "ยกเลิกแล้ว" not in edited[-1]["text"]
    assert fx.fx_draft_row(conn, RID)["token"] == second["token"]
    conn.close()


def test_deposit_confirm_while_awaiting_thb(
    tmp_path: Path, env_chat: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = open_db(tmp_path / "depwait.db")
    admin = ps.create_partner(conn, name="admin", is_admin=True, now_ms=NOW)
    assert ps.apply_deposit(conn, str(admin["partner_id"]), D("1000"), D("1000"), NOW)["ok"]
    pid = _partner(conn, now_ms=NOW + 1)
    _paid_request(conn, pid)
    created = ps.create_request(conn, pid, "deposit", NOW + 2, amount=D("100"), source="admin")
    assert created["ok"] is True
    monkeypatch.setattr(
        pa,
        "_transfer_guard",
        lambda *_a, **_k: (
            GuardResult(status="ok", checked_at_ms=NOW, findings=[], warn=False, error=None),
            "",
        ),
    )
    monkeypatch.setattr(
        pa,
        "read_account_value",
        lambda *_a, **_k: AccountValue(
            wallet=D("1100"), unrealized=D("0"), value=D("1100"), open_positions=0, read_at_ms=NOW + 3
        ),
    )
    sent, send = _send_capture()
    out = pa.handle_partner_callback(
        conn,
        f"pa:dy:{created['request_id']}",
        callback=_cb(f"pa:dy:{created['request_id']}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=_edit_capture()[1],
        futures=object(),
        now_ms=NOW + 3,
    )
    assert out["ok"] is True, out
    assert out.get("reason") != "payout_due"
    conn.close()


def test_resend_after_fix_sends_the_correction(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "resendfix.db")
    pid = _partner(conn)
    _bind(conn, pid)
    _paid_request(conn, pid)
    sent, send = _send_capture()

    def drop_partner(
        text: str, *, reply_markup: dict[str, Any] | None = None, chat_id: str | None = None, **kw: Any
    ) -> int | None:
        if chat_id is not None:
            return None
        return send(text, reply_markup=reply_markup, chat_id=chat_id, **kw)

    preview = pa.handle_partner_text(conn, _paid_cmd(), message=_msg("x"), send=send, now_ms=RECORDED_AT)
    pa.handle_partner_callback(
        conn,
        f"pa:fy:{RID}:{preview['token']}",
        callback=_cb(f"pa:fy:{RID}:{preview['token']}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=_edit_capture()[1],
        now_ms=RECORDED_AT,
    )
    fix = "p fix #a1b2c3 bank=10 net=16843.81\nธนาคารหักค่าโอน 10 บาท ตามสลิปจริง"
    fixed = pa.handle_partner_text(conn, fix, message=_msg(fix), send=send, now_ms=RECORDED_AT + 1)
    pa.handle_partner_callback(
        conn,
        f"pa:fy:{RID}:{fixed['token']}",
        callback=_cb(f"pa:fy:{RID}:{fixed['token']}"),
        send=drop_partner,
        answer=lambda *_a, **_k: None,
        edit=_edit_capture()[1],
        now_ms=RECORDED_AT + 2,
    )
    resent = pa.handle_partner_callback(
        conn,
        f"pa:fs:{RID}",
        callback=_cb(f"pa:fs:{RID}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=_edit_capture()[1],
        now_ms=RECORDED_AT + 3,
    )
    assert resent["ok"] is True, resent
    note = next(item["text"] for item in reversed(sent) if item["chat_id"] == TG)
    assert note.startswith("✏️ แก้ไขรายการโอนเงินบาท")
    assert "ค่าโอนธนาคาร: เดิม 0.00 → ใหม่ 10.00 บาท" in note
    conn.close()


def test_fix_refuses_a_long_slip_before_staging(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "slip.db")
    pid = _partner(conn)
    _paid_request(conn, pid)
    sent, send = _send_capture()
    preview = pa.handle_partner_text(
        conn, _paid_cmd(slip="ok"), message=_msg("x"), send=send, now_ms=RECORDED_AT
    )
    pa.handle_partner_callback(
        conn,
        f"pa:fy:{RID}:{preview['token']}",
        callback=_cb(f"pa:fy:{RID}:{preview['token']}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=_edit_capture()[1],
        now_ms=RECORDED_AT,
    )
    cmd = "p fix #a1b2c3 slip=" + ("Z" * 41) + "\nสลิปยาวเกินที่ระบบรับได้จริง"
    out = pa.handle_partner_text(conn, cmd, message=_msg(cmd), send=send, now_ms=RECORDED_AT + 1)
    assert out["reason"] == "slip_long"
    assert fx.fx_draft_row(conn, RID) is None
    conn.close()


def test_unsellable_stays_out_after_carry_returns(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "caseb.db")
    pid = _partner(conn)
    close = "pr_00000000000000b1"
    _paid_request(conn, pid, rid=close, kind="close", micro=500_000)
    assert [row["id"] for row in fx.awaiting_thb(conn, pid)] == []
    withdraw = "pr_00000000000000b2"
    _paid_request(conn, pid, rid=withdraw, micro=2_000_000, paid_at=NOW + 2)
    _record_simple(conn, pid, withdraw, payout=2_000_000, sold=1_000_000, now_ms=NOW + 3)
    assert fx.carry_balance(conn, pid) == 990_000
    assert close not in [row["id"] for row in fx.awaiting_thb(conn, pid)]
    conn.close()


def test_sellable_close_paid_after_carry_used(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "sellable.db")
    pid = _partner(conn, "nok")
    _bind(conn, pid)
    first = "pr_0000000000000c01"
    _paid_request(conn, pid, rid=first, micro=2_000_000)
    _record_simple(conn, pid, first, payout=2_000_000, sold=1_000_000, now_ms=NOW + 2)
    close = "pr_0000000000000c03"
    conn.execute(
        """
        INSERT INTO partner_request (
            id, partner_id, kind, status, payout_state, payout_micro,
            created_at_ms, expires_at_ms, decided_at_ms, source
        ) VALUES (?, ?, 'close', 'confirmed', 'due', 1200000, ?, ?, ?, 'admin')
        """,
        (close, pid, NOW + 3, NOW + 4, NOW + 3),
    )
    conn.commit()
    sent, send = _send_capture()
    edited, edit = _edit_capture()
    step = pa.handle_partner_callback(
        conn,
        f"pa:pd:{close}",
        callback=_cb(f"pa:pd:{close}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=edit,
        now_ms=NOW + 4,
    )
    assert step["ok"] is True
    assert [row["id"] for row in fx.awaiting_thb(conn, pid)] == [close]
    assert "p paid" in edited[-1]["text"]
    assert "ยอดน้อยกว่า 1 USDT" not in edited[-1]["text"]
    second = "pr_0000000000000c02"
    _paid_request(conn, pid, rid=second, micro=1_020_000, paid_at=NOW + 5)
    _record_simple(
        conn,
        pid,
        second,
        payout=1_020_000,
        sold=2_000_000,
        carry=990_000,
        now_ms=NOW + 6,
    )
    assert fx.carry_balance(conn, pid) == 0
    cmd = "p paid #000c03 sold=1 rate=33.10 chain=0.01 trade=0 wd=0 bank=0 net=33.10"
    preview = pa.handle_partner_text(conn, cmd, message=_msg(cmd), send=send, now_ms=NOW + 7)
    assert preview["ok"] is True, preview
    saved = pa.handle_partner_callback(
        conn,
        f"pa:fy:{close}:{preview['token']}",
        callback=_cb(f"pa:fy:{close}:{preview['token']}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=edit,
        now_ms=NOW + 8,
    )
    assert saved["ok"] is True, saved
    assert fx.current_fx(conn, close) is not None
    conn.close()


def _button(card: dict[str, Any]) -> str:
    return str(card["reply_markup"]["inline_keyboard"][0][0]["callback_data"])


def test_carry_retap_shows_fc_with_and_without_anchor_fx(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "retap.db")
    pid = _partner(conn, "nok")
    _bind(conn, pid)
    conn.execute("UPDATE partner SET status = 'closed', updated_at_ms = ? WHERE id = ?", (NOW, pid))
    conn.commit()
    with_fx = "pr_a0c10ce0000000f1"
    _paid_request(conn, pid, rid=with_fx, kind="close", micro=1_990_000, paid_at=NOW + 1)
    _record_simple(conn, pid, with_fx, payout=1_990_000, sold=1_000_000, now_ms=NOW + 2)
    sent, send = _send_capture()
    edited, edit = _edit_capture()
    text = "p carry nok settle 0.98 33.10\nโอนเศษเป็นเงินบาทตามสลิป"
    preview = pa.handle_partner_text(conn, text, message=_msg(text), send=send, edit=edit, now_ms=NOW + 3)
    assert preview["ok"] is True, preview
    token = str(preview["token"])
    rid = str(preview["request_id"])
    pa.handle_partner_callback(
        conn,
        f"pa:fy:{rid}:{token}",
        callback=_cb(f"pa:fy:{rid}:{token}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=edit,
        now_ms=NOW + 4,
    )

    def retap(data: str, when: int) -> dict[str, Any]:
        return pa.handle_partner_callback(
            conn,
            data,
            callback=_cb(data),
            send=send,
            answer=lambda *_a, **_k: None,
            edit=edit,
            now_ms=when,
        )

    again = retap(f"pa:fy:{rid}:{token}", NOW + 5)
    assert again["reason"] == "already"
    assert "ชำระเศษแล้ว" in edited[-1]["text"]
    assert _button(edited[-1]).startswith("pa:fc:")
    assert "pa:fs:" not in _button(edited[-1])
    cancelled = retap(f"pa:fn:{rid}:{token}", NOW + 6)
    assert cancelled["reason"] == "already"
    assert "ชำระเศษแล้ว" in edited[-1]["text"]
    assert _button(edited[-1]).startswith("pa:fc:")
    assert "pa:fs:" not in _button(edited[-1])

    bare = "pr_00000000000000f2"
    _paid_request(conn, pid, rid=bare, kind="close", micro=500_000, paid_at=NOW + 7)
    topup = "pr_00000000000000f3"
    _paid_request(conn, pid, rid=topup, micro=2_000_000, paid_at=NOW + 8)
    _record_simple(conn, pid, topup, payout=2_000_000, sold=1_000_000, now_ms=NOW + 9)
    conn.execute(
        "UPDATE partner SET telegram_user_id = ?, status = 'closed', updated_at_ms = ? WHERE id = ?",
        (TG, NOW + 9, pid),
    )
    conn.commit()
    text2 = "p carry nok settle 0.99 33.10\nโอนเศษเป็นเงินบาทตามสลิป"
    preview2 = pa.handle_partner_text(conn, text2, message=_msg(text2), send=send, edit=edit, now_ms=NOW + 10)
    assert preview2["request_id"] == bare
    token2 = str(preview2["token"])
    pa.handle_partner_callback(
        conn,
        f"pa:fy:{bare}:{token2}",
        callback=_cb(f"pa:fy:{bare}:{token2}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=edit,
        now_ms=NOW + 11,
    )
    bare_again = retap(f"pa:fy:{bare}:{token2}", NOW + 12)
    assert bare_again["reason"] == "already"
    assert "ชำระเศษแล้ว" in edited[-1]["text"]
    assert _button(edited[-1]).startswith("pa:fc:")
    assert "pa:fs:" not in _button(edited[-1])
    bare_cancel = retap(f"pa:fn:{bare}:{token2}", NOW + 13)
    assert bare_cancel["reason"] == "already"
    assert "ชำระเศษแล้ว" in edited[-1]["text"]
    assert _button(edited[-1]).startswith("pa:fc:")
    assert "pa:fs:" not in _button(edited[-1])
    conn.close()


def test_failed_carry_card_button_is_fc(
    tmp_path: Path, env_chat: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = open_db(tmp_path / "fcfail.db")
    pid = _partner(conn, "nok")
    conn.execute("UPDATE partner SET status = 'closed', updated_at_ms = ? WHERE id = ?", (NOW, pid))
    conn.commit()
    rid = "pr_a0c10ce0000000f4"
    _paid_request(conn, pid, rid=rid, kind="close", micro=1_990_000)
    _record_simple(conn, pid, rid, payout=1_990_000, sold=1_000_000, now_ms=NOW + 2)
    sent, send = _send_capture()
    edited, edit = _edit_capture()
    text = "p carry nok settle 0.98 33.10\nโอนเศษเป็นเงินบาทตามสลิป"
    preview = pa.handle_partner_text(conn, text, message=_msg(text), send=send, now_ms=NOW + 3)

    def boom(*_a: Any, **_k: Any) -> str:
        raise RuntimeError("render")

    monkeypatch.setattr(thb, "carry_notice_from_db", boom)
    out = pa.handle_partner_callback(
        conn,
        f"pa:fy:{preview['request_id']}:{preview['token']}",
        callback=_cb(f"pa:fy:{preview['request_id']}:{preview['token']}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=edit,
        now_ms=NOW + 4,
    )
    assert out["cmd"] == "fx_saved_partial"
    assert _button(edited[-1]).startswith("pa:fc:")
    assert "pa:fs:" not in _button(edited[-1])
    conn.close()


def test_carry_rounds_091_up_to_3013(tmp_path: Path, env_chat: None, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = open_db(tmp_path / "roundup.db")
    pid = _partner(conn, "nok")
    conn.execute("UPDATE partner SET status = 'closed', updated_at_ms = ? WHERE id = ?", (NOW, pid))
    conn.commit()
    rid = "pr_a0c10ce000000091"
    _paid_request(conn, pid, rid=rid, kind="close", micro=1_920_000)
    _record_simple(conn, pid, rid, payout=1_920_000, sold=1_000_000, now_ms=NOW + 2)
    assert fx.carry_balance(conn, pid) == 910_000
    sent, send = _send_capture()
    edited, edit = _edit_capture()
    text = "p carry nok settle 0.91 33.10\nโอนเศษเป็นเงินบาทตามสลิป"
    preview = pa.handle_partner_text(conn, text, message=_msg(text), send=send, now_ms=NOW + 3)
    assert preview["thb_satang"] == 3013
    real_balance = fx.carry_balance
    monkeypatch.setattr(fx, "carry_balance", lambda *_a, **_k: 1)
    moved = pa.handle_partner_callback(
        conn,
        f"pa:fy:{preview['request_id']}:{preview['token']}",
        callback=_cb(f"pa:fy:{preview['request_id']}:{preview['token']}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=edit,
        now_ms=NOW + 4,
    )
    assert moved["reason"] == "carry_mismatch"
    assert "พิมพ์ p carry ใหม่" in edited[-1]["text"]
    assert "p paid" not in edited[-1]["text"]
    monkeypatch.setattr(fx, "carry_balance", real_balance)
    preview2 = pa.handle_partner_text(conn, text, message=_msg(text), send=send, now_ms=NOW + 5)
    saved = pa.handle_partner_callback(
        conn,
        f"pa:fy:{preview2['request_id']}:{preview2['token']}",
        callback=_cb(f"pa:fy:{preview2['request_id']}:{preview2['token']}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=edit,
        now_ms=NOW + 6,
    )
    assert saved["ok"] is True, saved
    row = conn.execute(
        "SELECT reason FROM partner_usdt_carry WHERE partner_id = ? AND kind = 'settle'",
        (pid,),
    ).fetchone()
    assert str(row["reason"]).startswith("THB 30.13 @33.10 · ")
    conn.close()


def test_double_tap_unsellable_has_no_paid_hint(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "dbl.db")
    pid = _partner(conn, "tiny")
    _bind(conn, pid)
    conn.execute("UPDATE partner SET status = 'closed', updated_at_ms = ? WHERE id = ?", (NOW, pid))
    rid = "pr_00000000000000d0"
    conn.execute(
        """
        INSERT INTO partner_request (
            id, partner_id, kind, status, payout_state, payout_micro,
            created_at_ms, expires_at_ms, decided_at_ms, source
        ) VALUES (?, ?, 'close', 'confirmed', 'due', 500000, ?, ?, ?, 'admin')
        """,
        (rid, pid, NOW, NOW + 1, NOW),
    )
    conn.commit()
    sent, send = _send_capture()
    edited, edit = _edit_capture()
    first = pa.handle_partner_callback(
        conn,
        f"pa:pd:{rid}",
        callback=_cb(f"pa:pd:{rid}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=edit,
        now_ms=NOW + 2,
    )
    assert first["ok"] is True
    assert "p paid" not in edited[-1]["text"]
    assert "ถ้าค่าเครือข่าย" not in edited[-1]["text"]
    second = pa.handle_partner_callback(
        conn,
        f"pa:pd:{rid}",
        callback=_cb(f"pa:pd:{rid}", mid=8),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=edit,
        now_ms=NOW + 3,
    )
    assert second["reason"] == "not_due"
    assert "p paid" not in edited[-1]["text"]
    assert edited[-1]["text"] == "💸 โอนแล้ว 0.50 USDT ให้ tiny"
    conn.close()


def test_release_send_fail_card_has_unbind(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "relfail.db")
    pid = _partner(conn, "tiny")
    _bind(conn, pid)
    conn.execute("UPDATE partner SET status = 'closed', updated_at_ms = ? WHERE id = ?", (NOW, pid))
    rid = "pr_00000000000000d1"
    conn.execute(
        """
        INSERT INTO partner_request (
            id, partner_id, kind, status, payout_state, payout_micro,
            created_at_ms, expires_at_ms, decided_at_ms, source
        ) VALUES (?, ?, 'close', 'confirmed', 'due', 500000, ?, ?, ?, 'admin')
        """,
        (rid, pid, NOW, NOW + 1, NOW),
    )
    conn.commit()
    sent, real = _send_capture()

    def send(
        text: str, *, reply_markup: dict[str, Any] | None = None, chat_id: str | None = None, **kw: Any
    ) -> int | None:
        if chat_id is not None:
            return None
        return real(text, reply_markup=reply_markup, chat_id=chat_id, **kw)

    edited, edit = _edit_capture()
    out = pa.handle_partner_callback(
        conn,
        f"pa:pd:{rid}",
        callback=_cb(f"pa:pd:{rid}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=edit,
        now_ms=NOW + 2,
    )
    assert out["ok"] is True
    assert "p unbind tiny" in edited[-1]["text"]
    assert ps.get_partner(conn, pid)["telegram_user_id"] == TG
    conn.close()


def test_closed_withdraw_notice_has_no_carry_forward(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "wdclosed.db")
    pid = _partner(conn)
    _bind(conn, pid)
    _paid_request(conn, pid)
    conn.execute("UPDATE partner SET status = 'closed', updated_at_ms = ? WHERE id = ?", (NOW, pid))
    conn.commit()
    sent, send = _send_capture()
    preview = pa.handle_partner_text(conn, _paid_cmd(), message=_msg("x"), send=send, now_ms=RECORDED_AT)
    pa.handle_partner_callback(
        conn,
        f"pa:fy:{RID}:{preview['token']}",
        callback=_cb(f"pa:fy:{RID}:{preview['token']}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=_edit_capture()[1],
        now_ms=RECORDED_AT,
    )
    note = next(item["text"] for item in sent if item["chat_id"] == TG)
    assert "ยกไปรวมกับการถอนครั้งถัดไป" not in note
    assert "แอดมินจะแจ้งการชำระเศษนี้แยกต่างหาก" in note
    conn.close()


def test_closed_without_telegram_shows_warning_and_resend(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "notg.db")
    pid = _partner(conn, "quiet")
    conn.execute("UPDATE partner SET status = 'closed', updated_at_ms = ? WHERE id = ?", (NOW, pid))
    rid = "pr_00000000000000e2"
    _paid_request(conn, pid, rid=rid, micro=2_000_000)
    conn.commit()
    sent, send = _send_capture()
    edited, edit = _edit_capture()
    cmd = "p paid #0000e2 sold=2 rate=33.10 chain=0 trade=0 wd=0 bank=0 net=66.20"
    preview = pa.handle_partner_text(conn, cmd, message=_msg(cmd), send=send, now_ms=NOW + 3)
    assert preview["ok"] is True, preview
    saved = pa.handle_partner_callback(
        conn,
        f"pa:fy:{rid}:{preview['token']}",
        callback=_cb(f"pa:fy:{rid}:{preview['token']}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=edit,
        now_ms=NOW + 4,
    )
    assert saved["notified"] is False
    assert "⚠️ แจ้งพาร์ทเนอร์ไม่สำเร็จ" in edited[-1]["text"]
    assert "✅" not in edited[-1]["text"]
    assert _button(edited[-1]).startswith("pa:fs:")
    again = pa.handle_partner_callback(
        conn,
        f"pa:fs:{rid}",
        callback=_cb(f"pa:fs:{rid}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=edit,
        now_ms=NOW + 5,
    )
    assert again["notified"] is False
    assert "ℹ️ พาร์ทเนอร์ไม่ได้ผูกแชทแล้ว — ไม่ต้องส่งซ้ำ" in edited[-1]["text"]
    assert "⚠️ แจ้งพาร์ทเนอร์ไม่สำเร็จ" not in edited[-1]["text"]
    assert edited[-1]["reply_markup"] == {"inline_keyboard": []}
    conn.close()


def test_cancel_after_paid_save_shows_settled_view(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "xafter.db")
    pid = _partner(conn)
    _bind(conn, pid)
    _paid_request(conn, pid)
    sent, send = _send_capture()
    edited, edit = _edit_capture()
    preview = pa.handle_partner_text(conn, _paid_cmd(), message=_msg("x"), send=send, now_ms=RECORDED_AT)
    pa.handle_partner_callback(
        conn,
        f"pa:fy:{RID}:{preview['token']}",
        callback=_cb(f"pa:fy:{RID}:{preview['token']}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=edit,
        now_ms=RECORDED_AT,
    )
    cancelled = pa.handle_partner_callback(
        conn,
        f"pa:fn:{RID}:{preview['token']}",
        callback=_cb(f"pa:fn:{RID}:{preview['token']}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=edit,
        now_ms=RECORDED_AT + 1,
    )
    assert cancelled["reason"] == "already"
    assert edited[-1]["text"].startswith("💸 บันทึกโอนเงินบาทแล้ว")
    conn.close()


def test_awaiting_due_text_caps_at_3500(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "cap.db")
    pid = _partner(conn)
    rows = [
        {
            "id": f"pr_{index:016x}",
            "partner_id": pid,
            "partner_name": "som",
            "kind": "withdraw_profit",
            "payout_micro": 1_200_000,
            "paid_at_ms": PAID_AT,
        }
        for index in range(80)
    ]
    text = pa._awaiting_due_text(conn, rows, PAID_AT)
    assert len(text) <= 3500
    assert "… อีก " in text
    assert text.endswith("บันทึก: p paid #รหัส sold= rate= trade= net=")
    conn.close()


def test_unbind_carry_only_asks_for_p_carry(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "unb.db")
    pid = _partner(conn, "nok")
    _bind(conn, pid)
    rid = "pr_00000000000000e3"
    _paid_request(conn, pid, rid=rid, micro=2_000_000)
    _record_simple(conn, pid, rid, payout=2_000_000, sold=1_000_000, now_ms=NOW + 2)
    conn.execute("UPDATE partner SET status = 'closed', updated_at_ms = ? WHERE id = ?", (NOW + 3, pid))
    conn.commit()
    sent, send = _send_capture()
    out = pa.handle_partner_text(
        conn, "p unbind nok", message=_msg("p unbind nok"), send=send, now_ms=NOW + 4
    )
    assert out["reason"] == "payout_due"
    assert "ปิดแล้วและยังมีเศษค้าง — ใช้ p carry ก่อน" in sent[-1]["text"]
    assert ps.get_partner(conn, pid)["telegram_user_id"] == TG
    conn.close()


def test_replaced_fix_and_carry_preview_says_replaced(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "repl.db")
    pid = _partner(conn, "nok")
    _bind(conn, pid)
    _paid_request(conn, pid)
    sent, send = _send_capture()
    edited, edit = _edit_capture()
    preview = pa.handle_partner_text(conn, _paid_cmd(), message=_msg("x"), send=send, now_ms=RECORDED_AT)
    pa.handle_partner_callback(
        conn,
        f"pa:fy:{RID}:{preview['token']}",
        callback=_cb(f"pa:fy:{RID}:{preview['token']}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=edit,
        now_ms=RECORDED_AT,
    )
    fix = "p fix #a1b2c3 bank=10 net=16843.81\nธนาคารหักค่าโอน 10 บาท ตามสลิปจริง"
    pa.handle_partner_text(conn, fix, message=_msg(fix), send=send, edit=edit, now_ms=RECORDED_AT + 1)
    pa.handle_partner_text(conn, fix, message=_msg(fix), send=send, edit=edit, now_ms=RECORDED_AT + 2)
    assert any(item["text"] == "ถูกแทนด้วยรายการใหม่" for item in edited)
    conn.execute("UPDATE partner SET status = 'closed', updated_at_ms = ? WHERE id = ?", (NOW, pid))
    conn.commit()
    text = "p carry nok settle 0.99 33.10\nโอนเศษเป็นเงินบาทตามสลิป"
    pa.handle_partner_text(conn, text, message=_msg(text), send=send, edit=edit, now_ms=NOW + 8)
    before = len(edited)
    pa.handle_partner_text(conn, text, message=_msg(text), send=send, edit=edit, now_ms=NOW + 9)
    assert any(item["text"] == "ถูกแทนด้วยรายการใหม่" for item in edited[before:])
    conn.close()


def test_earlier_withdraw_keeps_carry_forward_after_close(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "w1w2.db")
    pid = _partner(conn, "nok")
    _bind(conn, pid)
    first = "pr_0000000000000a11"
    second = "pr_0000000000000a12"
    _paid_request(conn, pid, rid=first, micro=1_310_000)
    _record_simple(conn, pid, first, payout=1_310_000, sold=1_000_000, now_ms=NOW + 2)
    assert fx.carry_balance(conn, pid) == 300_000
    _paid_request(conn, pid, rid=second, micro=710_000, paid_at=NOW + 3)
    _record_simple(conn, pid, second, payout=710_000, sold=1_000_000, carry=300_000, now_ms=NOW + 4)
    assert fx.carry_balance(conn, pid) == 0
    conn.execute("UPDATE partner SET status = 'closed', updated_at_ms = ? WHERE id = ?", (NOW + 5, pid))
    conn.commit()
    sent, send = _send_capture()
    cmd = "p fix #000a11 rate=34.00 net=34.00\nปรับเรทตามสลิปที่ได้รับ"
    preview = pa.handle_partner_text(conn, cmd, message=_msg(cmd), send=send, now_ms=NOW + 6)
    assert preview["ok"] is True, preview
    saved = pa.handle_partner_callback(
        conn,
        f"pa:fy:{first}:{preview['token']}",
        callback=_cb(f"pa:fy:{first}:{preview['token']}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=_edit_capture()[1],
        now_ms=NOW + 7,
    )
    assert saved["ok"] is True, saved
    note = next(item["text"] for item in sent if item["chat_id"] == TG)
    assert "ยกไปรวมกับการถอนครั้งถัดไป" in note
    assert "แยกต่างหาก" not in note
    conn.close()


def test_fix_cancel_on_non_anchor_is_not_a_carry_card(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "nonanchor.db")
    pid = _partner(conn, "nok")
    _bind(conn, pid)
    withdraw = "pr_0000000000000b11"
    _paid_request(conn, pid, rid=withdraw, micro=1_990_000)
    _record_simple(conn, pid, withdraw, payout=1_990_000, sold=1_000_000, now_ms=NOW + 2)
    close = "pr_0000000000000b12"
    _paid_request(conn, pid, rid=close, kind="close", micro=500_000, paid_at=NOW + 3)
    conn.execute("UPDATE partner SET status = 'closed', updated_at_ms = ? WHERE id = ?", (NOW + 4, pid))
    conn.commit()
    sent, send = _send_capture()
    edited, edit = _edit_capture()
    text = "p carry nok settle 0.98 33.10\nโอนเศษเป็นเงินบาทตามสลิป"
    preview = pa.handle_partner_text(conn, text, message=_msg(text), send=send, now_ms=NOW + 5)
    assert preview["request_id"] == close
    pa.handle_partner_callback(
        conn,
        f"pa:fy:{close}:{preview['token']}",
        callback=_cb(f"pa:fy:{close}:{preview['token']}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=edit,
        now_ms=NOW + 6,
    )
    fix = "p fix #000b11 rate=33.20 net=33.20\nปรับเรทตามสลิปที่ได้รับ"
    fixed = pa.handle_partner_text(conn, fix, message=_msg(fix), send=send, edit=edit, now_ms=NOW + 7)
    assert fixed["ok"] is True, fixed
    token = str(fixed["token"])
    first = pa.handle_partner_callback(
        conn,
        f"pa:fn:{withdraw}:{token}",
        callback=_cb(f"pa:fn:{withdraw}:{token}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=edit,
        now_ms=NOW + 8,
    )
    assert first["cmd"] == "fx_cancel"
    assert "ชำระเศษแล้ว" not in edited[-1]["text"]
    second = pa.handle_partner_callback(
        conn,
        f"pa:fn:{withdraw}:{token}",
        callback=_cb(f"pa:fn:{withdraw}:{token}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=edit,
        now_ms=NOW + 9,
    )
    assert second["reason"] == "already"
    assert "ชำระเศษแล้ว" not in edited[-1]["text"]
    assert edited[-1]["text"].startswith("💸 บันทึกโอนเงินบาทแล้ว")
    assert _button(edited[-1]).startswith("pa:fs:")
    conn.close()


def test_p_carry_on_active_partner_says_not_closed(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "active.db")
    _partner(conn, "nok")
    sent, send = _send_capture()
    text = "p carry nok settle 0.98 33.10\nโอนเศษเป็นเงินบาทตามสลิป"
    out = pa.handle_partner_text(conn, text, message=_msg(text), send=send, now_ms=NOW + 1)
    assert out["reason"] == "not_closed"
    assert "nok ยังไม่ได้ปิดพอร์ต — p carry ใช้กับพาร์ทเนอร์ที่ปิดแล้ว" in sent[-1]["text"]
    conn.close()


def test_fix_sold_change_line_uses_whole_usdt(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "soldfix.db")
    pid = _partner(conn)
    _bind(conn, pid)
    _paid_request(conn, pid)
    sent, send = _send_capture()
    preview = pa.handle_partner_text(conn, _paid_cmd(), message=_msg("x"), send=send, now_ms=RECORDED_AT)
    pa.handle_partner_callback(
        conn,
        f"pa:fy:{RID}:{preview['token']}",
        callback=_cb(f"pa:fy:{RID}:{preview['token']}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=_edit_capture()[1],
        now_ms=RECORDED_AT,
    )
    cmd = "p fix #a1b2c3 sold=498 net=16819.91\nปรับจำนวนขายตามสลิปจริง"
    fixed = pa.handle_partner_text(conn, cmd, message=_msg(cmd), send=send, now_ms=RECORDED_AT + 1)
    assert fixed["ok"] is True, fixed
    assert "จำนวนขาย: เดิม 499 → ใหม่ 498 USDT" in sent[-1]["text"]
    saved = pa.handle_partner_callback(
        conn,
        f"pa:fy:{RID}:{fixed['token']}",
        callback=_cb(f"pa:fy:{RID}:{fixed['token']}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=_edit_capture()[1],
        now_ms=RECORDED_AT + 2,
    )
    assert saved["ok"] is True, saved
    note = next(item["text"] for item in reversed(sent) if item["chat_id"] == TG)
    assert "จำนวนขาย: เดิม 499 → ใหม่ 498 USDT" in note
    conn.close()


def test_settle_reason_with_embedded_at_keeps_thb(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "embed.db")
    pid = _partner(conn, "nok")
    conn.execute("UPDATE partner SET status = 'closed', updated_at_ms = ? WHERE id = ?", (NOW, pid))
    conn.commit()
    rid = "pr_a0c10ce0000000e9"
    _paid_request(conn, pid, rid=rid, kind="close", micro=1_990_000)
    _record_simple(conn, pid, rid, payout=1_990_000, sold=1_000_000, now_ms=NOW + 2)
    sent, send = _send_capture()
    text = "p carry nok settle 0.98 33.10\nโอน @12 · ตามสลิป"
    preview = pa.handle_partner_text(conn, text, message=_msg(text), send=send, now_ms=NOW + 3)
    assert preview["ok"] is True, preview
    saved = pa.handle_partner_callback(
        conn,
        f"pa:fy:{preview['request_id']}:{preview['token']}",
        callback=_cb(f"pa:fy:{preview['request_id']}:{preview['token']}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=_edit_capture()[1],
        now_ms=NOW + 4,
    )
    assert saved["ok"] is True, saved
    note = thb.carry_notice_from_db(conn, pid)
    assert note.splitlines()[0] == "💸 ชำระเศษ 0.98 USDT เป็นเงินบาท 32.44 บาท"
    assert note.splitlines()[1] == "โอน @12 · ตามสลิป"
    conn.close()
