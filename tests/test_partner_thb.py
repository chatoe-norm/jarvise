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
    sent, send = _send_capture()
    text = "p carry nok settle 0.50 33.10\nโอนเศษเป็นเงินบาทตามสลิป"
    out = pa.handle_partner_text(conn, text, message=_msg(text), send=send, now_ms=NOW + 1)
    assert out["reason"] == "carry_mismatch"
    assert sent[-1]["text"].endswith("ยังไม่บันทึกอะไร")
    assert fx.carry_balance(conn, pid) == 980_000
    conn.close()


def test_p_carry_settle_needs_reason(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "creason.db")
    _partner(conn, "nok")
    sent, send = _send_capture()
    text = "p carry nok settle 0.98 33.10"
    out = pa.handle_partner_text(conn, text, message=_msg(text), send=send, now_ms=NOW + 1)
    assert out["reason"] == "reason_required"
    assert "ต้องใส่เหตุผล" in sent[-1]["text"]
    conn.close()
