"""Partner invite / bind / unbind, and close-access (PR B-2)."""

from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from jarvise_assist import partner_admin as pa
from jarvise_assist import partner_portal as portal
from jarvise_assist import partner_rules
from jarvise_assist import partner_store as ps
from jarvise_assist.partner_audit import GuardResult
from jarvise_assist.partner_value import AccountValue, AccountValueError
from jarvise_assist.pause import BANGKOK
from jarvise_ingest.db import open_db

D = Decimal
NOW = 1_700_000_000_000


def _setup(conn: Any) -> tuple[str, str]:
    a = ps.create_partner(conn, name="admin", is_admin=True, now_ms=NOW)
    b = ps.create_partner(conn, name="B", is_admin=False, now_ms=NOW + 1)
    ps.apply_deposit(conn, a["partner_id"], D("1000"), D("1000"), NOW + 2)
    return a["partner_id"], b["partner_id"]


def test_create_invite_stores_hash_not_code(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "inv.db")
    _, b_id = _setup(conn)
    out = ps.create_invite(conn, b_id, now_ms=NOW + 10)
    assert out["ok"] is True
    code = out["code"]
    assert len(code) >= 20
    row = ps.get_partner(conn, b_id)
    assert row["invite_hash"] == hashlib.sha256(code.encode()).hexdigest()
    assert row["invite_expires_at_ms"] == NOW + 10 + ps.INVITE_TTL_MS
    cols = conn.execute("SELECT * FROM partner WHERE id = ?", (b_id,)).fetchone()
    for k in cols.keys():
        assert code not in str(cols[k])
    assert ps.verify_ledger(conn)["ok"] is True
    conn.close()


def test_create_invite_refusals(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "inv_ref.db")
    a_id, b_id = _setup(conn)
    assert ps.create_invite(conn, "missing", now_ms=NOW)["reason"] == "not_found"
    assert ps.create_invite(conn, a_id, now_ms=NOW)["reason"] == "admin_no_invite"
    ps.apply_close(conn, b_id, D("1000"), NOW + 20)
    assert ps.create_invite(conn, b_id, now_ms=NOW + 21)["reason"] == "closed"
    c = ps.create_partner(conn, name="C", is_admin=False, now_ms=NOW + 30)
    inv = ps.create_invite(conn, c["partner_id"], now_ms=NOW + 31)
    assert inv["ok"]
    bind = ps.bind_invite(conn, inv["code"], "501", admin_chat_id="42", now_ms=NOW + 32)
    assert bind["ok"]
    assert ps.create_invite(conn, c["partner_id"], now_ms=NOW + 33)["reason"] == "already_bound"
    assert ps.verify_ledger(conn)["ok"] is True
    conn.close()


def test_bind_ok_and_second_code_fails(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "bind.db")
    _, b_id = _setup(conn)
    inv = ps.create_invite(conn, b_id, now_ms=NOW + 10)
    code = inv["code"]
    ok = ps.bind_invite(conn, code, "501", admin_chat_id="42", now_ms=NOW + 11)
    assert ok == {"ok": True, "partner_id": b_id, "name": "B"}
    row = ps.get_partner(conn, b_id)
    assert row["telegram_user_id"] == "501"
    assert row["invite_hash"] is None
    assert row["invite_expires_at_ms"] is None
    again = ps.bind_invite(conn, code, "502", admin_chat_id="42", now_ms=NOW + 12)
    assert again["ok"] is False
    assert again["reason"] == "invite_invalid"
    assert ps.partner_by_telegram_id(conn, "501")["id"] == b_id
    assert ps.verify_ledger(conn)["ok"] is True
    conn.close()


def test_bind_at_exact_expiry_fails(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "bind_exp.db")
    _, b_id = _setup(conn)
    inv = ps.create_invite(conn, b_id, now_ms=NOW + 10)
    expires = inv["expires_at_ms"]
    out = ps.bind_invite(conn, inv["code"], "501", admin_chat_id="42", now_ms=expires)
    assert out["reason"] == "invite_invalid"
    assert ps.get_partner(conn, b_id)["telegram_user_id"] is None
    conn.close()


def test_bind_admin_chat_id_and_closed_and_admin_row(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "bind_bad.db")
    a_id, b_id = _setup(conn)
    inv = ps.create_invite(conn, b_id, now_ms=NOW + 10)
    assert (
        ps.bind_invite(conn, inv["code"], "42", admin_chat_id="42", now_ms=NOW + 11)["reason"] == "admin_id"
    )
    c = ps.create_partner(conn, name="C", is_admin=False, now_ms=NOW + 20)
    inv_c = ps.create_invite(conn, c["partner_id"], now_ms=NOW + 21)
    conn.execute(
        "UPDATE partner SET status='closed', updated_at_ms=? WHERE id=?",
        (NOW + 22, c["partner_id"]),
    )
    conn.commit()
    assert (
        ps.bind_invite(conn, inv_c["code"], "503", admin_chat_id="42", now_ms=NOW + 23)["reason"]
        == "invite_invalid"
    )
    inv2 = ps.create_invite(conn, b_id, now_ms=NOW + 30)
    h = hashlib.sha256(inv2["code"].encode()).hexdigest()
    conn.execute(
        "UPDATE partner SET invite_hash=?, invite_expires_at_ms=? WHERE id=?",
        (h, NOW + 30 + ps.INVITE_TTL_MS, a_id),
    )
    conn.execute(
        "UPDATE partner SET invite_hash=NULL, invite_expires_at_ms=NULL WHERE id=?",
        (b_id,),
    )
    conn.commit()
    assert (
        ps.bind_invite(conn, inv2["code"], "504", admin_chat_id="42", now_ms=NOW + 31)["reason"]
        == "invite_invalid"
    )
    conn.close()


def test_two_partners_cannot_share_telegram_id(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "tg_dup.db")
    _, b_id = _setup(conn)
    c = ps.create_partner(conn, name="C", is_admin=False, now_ms=NOW + 5)
    inv_b = ps.create_invite(conn, b_id, now_ms=NOW + 10)
    assert ps.bind_invite(conn, inv_b["code"], "501", admin_chat_id="42", now_ms=NOW + 11)["ok"]
    inv_c = ps.create_invite(conn, c["partner_id"], now_ms=NOW + 12)
    out = ps.bind_invite(conn, inv_c["code"], "501", admin_chat_id="42", now_ms=NOW + 13)
    assert out["reason"] == "tg_in_use"
    assert ps.get_partner(conn, c["partner_id"])["telegram_user_id"] is None
    assert ps.verify_ledger(conn)["ok"] is True
    conn.close()


def test_unbind_clears_fields(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "unbind.db")
    _, b_id = _setup(conn)
    inv = ps.create_invite(conn, b_id, now_ms=NOW + 10)
    ps.bind_invite(conn, inv["code"], "501", admin_chat_id="42", now_ms=NOW + 11)
    out = ps.unbind_partner(conn, b_id, now_ms=NOW + 20)
    assert out["ok"] is True
    row = ps.get_partner(conn, b_id)
    assert row["telegram_user_id"] is None
    assert row["invite_hash"] is None
    assert row["invite_expires_at_ms"] is None
    assert ps.partner_by_telegram_id(conn, "501") is None
    assert ps.verify_ledger(conn)["ok"] is True
    conn.close()


def test_get_request_and_list_helpers(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lists.db")
    _, b_id = _setup(conn)
    req = ps.create_request(conn, b_id, "deposit", NOW + 10, amount=D("100"), source="admin")
    assert req["ok"]
    rid = req["request_id"]
    got = ps.get_request(conn, rid)
    assert got is not None and got["id"] == rid
    pending = ps.list_pending_requests(conn, now_ms=NOW + 11)
    assert len(pending) == 1
    assert pending[0]["id"] == rid
    empty = ps.list_pending_requests(conn, now_ms=NOW + 10 + ps.REQUEST_TTL_MS + 1)
    assert empty == []
    assert ps.list_due_payouts(conn) == []
    ps.confirm_request(conn, rid, D("1100"), NOW + 12, amount_received=D("100"))
    assert ps.list_due_payouts(conn) == []
    assert ps.verify_ledger(conn)["ok"] is True
    conn.close()


def test_new_invite_replaces_old(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "repl.db")
    _, b_id = _setup(conn)
    inv1 = ps.create_invite(conn, b_id, now_ms=NOW + 10)
    inv2 = ps.create_invite(conn, b_id, now_ms=NOW + 11)
    assert inv1["code"] != inv2["code"]
    assert (
        ps.bind_invite(conn, inv1["code"], "501", admin_chat_id="42", now_ms=NOW + 12)["reason"]
        == "invite_invalid"
    )
    assert ps.bind_invite(conn, inv2["code"], "501", admin_chat_id="42", now_ms=NOW + 13)["ok"]
    conn.close()


# --- PR B-2: close access ----------------------------------------------------

ADMIN = "42"
TG = "501"


def _ms(y: int, m: int, d: int, hh: int = 12, mm: int = 0) -> int:
    return int(datetime(y, m, d, hh, mm, tzinfo=BANGKOK).timestamp() * 1000)


@pytest.fixture
def env_chat(monkeypatch: Any) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", ADMIN)


def _patch_guard(monkeypatch: Any) -> None:
    def _fake(conn: Any, client: Any, *, now_ms: int, req: Any) -> tuple[GuardResult, str]:
        result = GuardResult(status="ok", checked_at_ms=int(now_ms), findings=[], warn=False, error=None)
        return result, ""

    monkeypatch.setattr(pa, "_transfer_guard", _fake)


def _patch_value(monkeypatch: Any, wallet: str = "1000") -> dict[str, str]:
    state = {"wallet": wallet}

    def fake(cli: Any, *, now_ms: int, attempts: int = 3) -> AccountValue:
        w = D(state["wallet"])
        return AccountValue(wallet=w, unrealized=D("0"), value=w, open_positions=0, read_at_ms=now_ms)

    monkeypatch.setattr(pa, "read_account_value", fake)
    monkeypatch.setattr(portal, "read_account_value", fake)
    return state


def _send_capture() -> tuple[list[dict[str, Any]], Any]:
    sent: list[dict[str, Any]] = []

    def send(
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        chat_id: str | None = None,
        **kw: Any,
    ) -> int:
        mid = len(sent) + 1
        entry: dict[str, Any] = {"text": text, "reply_markup": reply_markup, "message_id": mid}
        if chat_id is not None:
            entry["chat_id"] = str(chat_id)
        sent.append(entry)
        return mid

    return sent, send


def _edit_capture() -> tuple[list[dict[str, Any]], Any]:
    edited: list[dict[str, Any]] = []

    def edit(
        message_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        chat_id: str | None = None,
        **kw: Any,
    ) -> bool:
        entry: dict[str, Any] = {"message_id": message_id, "text": text, "reply_markup": reply_markup}
        if chat_id is not None:
            entry["chat_id"] = str(chat_id)
        edited.append(entry)
        return True

    return edited, edit


def _answer_capture() -> tuple[list[Any], Any]:
    answers: list[Any] = []

    def answer(cq_id: str, text: str | None = None) -> bool:
        answers.append((cq_id, text))
        return True

    return answers, answer


def _owner_msg(text: str) -> dict[str, Any]:
    return {
        "message_id": 1,
        "chat": {"id": int(ADMIN), "type": "private"},
        "from": {"id": int(ADMIN)},
        "text": text,
    }


def _owner_cb(data: str, *, mid: int = 7) -> dict[str, Any]:
    return {
        "id": "cq-admin",
        "data": data,
        "from": {"id": int(ADMIN)},
        "message": {
            "message_id": mid,
            "chat": {"id": int(ADMIN), "type": "private"},
            "text": "card",
        },
    }


def _pmsg(text: str, chat: str = TG) -> dict[str, Any]:
    return {
        "message_id": 3,
        "chat": {"id": int(chat), "type": "private"},
        "from": {"id": int(chat), "first_name": "Bob"},
        "text": text,
    }


def _pcb(data: str, *, chat: str = TG, mid: int = 9) -> dict[str, Any]:
    return {
        "id": "cq-p",
        "data": data,
        "from": {"id": int(chat)},
        "message": {
            "message_id": mid,
            "chat": {"id": int(chat), "type": "private"},
            "text": "card",
        },
    }


def _admin_and_partner(conn: Any, name: str = "B", *, now_ms: int = NOW) -> tuple[str, str]:
    admin = ps.create_partner(conn, name="admin", is_admin=True, now_ms=now_ms)
    assert ps.apply_deposit(conn, admin["partner_id"], D("1000"), D("1000"), now_ms)["ok"]
    partner = ps.create_partner(conn, name=name, now_ms=now_ms + 1)
    return str(admin["partner_id"]), str(partner["partner_id"])


def _bind(conn: Any, pid: str, tg: str = TG, *, now_ms: int) -> None:
    inv = ps.create_invite(conn, pid, now_ms=now_ms)
    assert inv["ok"] is True
    assert ps.bind_invite(conn, inv["code"], tg, admin_chat_id=ADMIN, now_ms=now_ms + 1)["ok"]


def _confirm_close(conn: Any, pid: str, *, send: Any, edit: Any, now_ms: int) -> dict[str, Any]:
    created = ps.create_request(conn, pid, "close", now_ms, source="admin")
    assert created["ok"] is True
    rid = str(created["request_id"])
    return pa.handle_partner_callback(
        conn,
        f"pa:ry:{rid}",
        callback=_owner_cb(data=f"pa:ry:{rid}"),
        send=send,
        answer=_answer_capture()[1],
        edit=edit,
        futures=object(),
        now_ms=now_ms + 1,
    )


def _partner_texts(sent: list[dict[str, Any]], chat: str = TG) -> list[str]:
    return [str(item["text"]) for item in sent if item.get("chat_id") == chat]


def test_closed_card_lists_every_due_when_value_unreadable(
    tmp_path: Path, monkeypatch: Any, env_chat: None
) -> None:
    conn = open_db(tmp_path / "card.db")
    _, b_id = _admin_and_partner(conn)
    _bind(conn, b_id, now_ms=NOW + 2)
    join_ms = NOW + 5
    assert ps.apply_deposit(conn, b_id, D("1000"), D("2000"), join_ms)["ok"]
    wd = ps.apply_withdraw_profit(conn, b_id, D("4000"), join_ms + 10)
    assert wd["ok"] is True and D(wd["cash"]) > 0
    closed = ps.apply_close(conn, b_id, D("4000"), join_ms + 20)
    assert closed["ok"] is True and D(closed["cash"]) > 0
    summary = ps.close_summary(conn, b_id)
    assert summary["ok"] is True
    assert summary["joined_at_ms"] == join_ms
    assert summary["closed_at_ms"] == join_ms + 20
    assert {d["kind"] for d in summary["dues"]} == {"withdraw_profit", "close"}
    row = ps.get_partner(conn, b_id)
    assert row is not None
    text, buttons = portal._build_card(
        conn, row, AccountValueError(reason="read_failed"), now_ms=join_ms + 30
    )
    assert buttons is None
    assert "ปิดพอร์ตแล้ว" in text
    assert partner_rules.fmt_date(join_ms) in text
    assert partner_rules.fmt_date(join_ms + 20) in text
    assert f"ยอดปิด {pa.fmt_usdt(summary['close_cash'])} USDT" in text
    assert text.count("รอโอน") == len(summary["dues"])
    for due in summary["dues"]:
        assert f"รอโอน {pa.fmt_usdt(due['cash'])} USDT" in text

    monkeypatch.setattr(portal, "read_account_value", lambda *a, **k: AccountValueError(reason="no_client"))
    portal.reset_value_cache()
    sent, send = _send_capture()
    out = portal.handle_portal_text(conn, _pmsg("ยอด"), send=send, futures=object(), now_ms=join_ms + 40)
    assert out["ok"] is True
    assert sent and sent[0]["reply_markup"] is None
    body = sent[0]["text"]
    assert "เข้าร่วม" in body and "ปิดเมื่อ" in body and "ยอดปิด" in body
    assert body.count("รอโอน") == 2
    conn.close()


def test_zero_cash_close_sends_then_unbinds(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "zero.db")
    _patch_guard(monkeypatch)
    _patch_value(monkeypatch, "1000")
    _, b_id = _admin_and_partner(conn)
    _bind(conn, b_id, now_ms=NOW + 2)
    order: list[str] = []
    sent, send = _send_capture()
    real_send = send

    def tracking_send(
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        chat_id: str | None = None,
        **kw: Any,
    ) -> int:
        if chat_id is not None:
            order.append("send")
        return real_send(text, reply_markup=reply_markup, chat_id=chat_id, **kw)

    real_unbind = ps.unbind_partner

    def tracking_unbind(conn: Any, partner_id: str, *, now_ms: int) -> dict[str, Any]:
        order.append("unbind")
        return real_unbind(conn, partner_id, now_ms=now_ms)

    monkeypatch.setattr(ps, "unbind_partner", tracking_unbind)
    edited, edit = _edit_capture()
    out = _confirm_close(conn, b_id, send=tracking_send, edit=edit, now_ms=NOW + 10)
    assert out["ok"] is True
    assert order == ["send", "unbind"]
    note = _partner_texts(sent)[0]
    assert "ยอดปิด 0.00 USDT" in note
    assert "ไม่มีเงินออก" in note
    assert ps.get_partner(conn, b_id)["telegram_user_id"] is None
    assert edited
    assert "ไม่มีเงินออก" in edited[0]["text"]
    assert "แจ้งพาร์ทเนอร์ไม่สำเร็จ" not in edited[0]["text"]
    conn.close()


def test_failed_close_send_keeps_binding(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "fail-send.db")
    _patch_guard(monkeypatch)
    _patch_value(monkeypatch, "1000")
    _, b_id = _admin_and_partner(conn)
    _bind(conn, b_id, now_ms=NOW + 2)
    sent, _base = _send_capture()

    def send(
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        chat_id: str | None = None,
        **kw: Any,
    ) -> int | None:
        if chat_id is not None:
            return None
        mid = len(sent) + 1
        sent.append({"text": text, "reply_markup": reply_markup, "message_id": mid})
        return mid

    edited, edit = _edit_capture()
    out = _confirm_close(conn, b_id, send=send, edit=edit, now_ms=NOW + 10)
    assert out["ok"] is True
    assert ps.get_partner(conn, b_id)["telegram_user_id"] == TG
    assert edited
    assert "⚠️ แจ้งพาร์ทเนอร์ไม่สำเร็จ — ยังไม่ตัดการผูก (p unbind B)" in edited[0]["text"]
    sent_u, send_u = _send_capture()
    unbound = pa.handle_partner_text(
        conn,
        "p unbind B",
        message=_owner_msg("p unbind B"),
        send=send_u,
        futures=object(),
        now_ms=NOW + 20,
    )
    assert unbound["ok"] is True
    assert "ยกเลิกการผูก B แล้ว" in sent_u[0]["text"]
    assert ps.get_partner(conn, b_id)["telegram_user_id"] is None
    conn.close()


def test_zero_cash_close_keeps_binding_while_withdraw_due(
    tmp_path: Path, monkeypatch: Any, env_chat: None
) -> None:
    """The confirm gate blocks this; the unbind check is what must hold if it is reached."""
    conn = open_db(tmp_path / "zero-due.db")
    _patch_guard(monkeypatch)
    _patch_value(monkeypatch, "1000")
    _, b_id = _admin_and_partner(conn)
    _bind(conn, b_id, now_ms=NOW + 2)
    conn.execute(
        """
        INSERT INTO partner_request (
            id, partner_id, kind, status, payout_state, payout_micro,
            created_at_ms, expires_at_ms, decided_at_ms, source
        ) VALUES (?, ?, 'withdraw_profit', 'confirmed', 'due', ?, ?, ?, ?, 'admin')
        """,
        ("pr_aabbccddeeff0011", b_id, ps.usdt_to_micro(D("10")), NOW + 3, NOW + 4, NOW + 3),
    )
    conn.commit()
    monkeypatch.setattr(ps, "payouts_due_total", lambda _conn: D("0"))
    order: list[str] = []
    sent, real_send = _send_capture()

    def send(
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        chat_id: str | None = None,
        **kw: Any,
    ) -> int:
        if chat_id is not None:
            order.append("send")
        return real_send(text, reply_markup=reply_markup, chat_id=chat_id, **kw)

    real_unbind = ps.unbind_partner

    def tracking_unbind(conn: Any, partner_id: str, *, now_ms: int) -> dict[str, Any]:
        order.append("unbind")
        return real_unbind(conn, partner_id, now_ms=now_ms)

    monkeypatch.setattr(ps, "unbind_partner", tracking_unbind)
    edited, edit = _edit_capture()
    out = _confirm_close(conn, b_id, send=send, edit=edit, now_ms=NOW + 10)
    assert out["ok"] is True
    assert order == ["send"]
    assert ps.get_partner(conn, b_id)["telegram_user_id"] == TG
    assert "รอโอน 10.00 USDT" in _partner_texts(sent)[0]
    assert edited
    assert "⚠️ ยังมียอดค้างจ่าย — ยังไม่ตัดการผูก (โอนให้ครบก่อน)" in edited[0]["text"]
    assert "แจ้งพาร์ทเนอร์ไม่สำเร็จ" not in edited[0]["text"]
    conn.close()


def test_paid_button_unbinds_only_the_last_due(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "paid.db")
    _, b_id = _admin_and_partner(conn)
    _bind(conn, b_id, now_ms=NOW + 2)
    assert ps.apply_deposit(conn, b_id, D("1000"), D("2000"), NOW + 5)["ok"]
    wd = ps.apply_withdraw_profit(conn, b_id, D("4000"), NOW + 6)
    closed = ps.apply_close(conn, b_id, D("4000"), NOW + 7)
    assert wd["ok"] and closed["ok"]
    dues = ps.list_due_payouts(conn)
    wd_id = next(str(d["id"]) for d in dues if str(d["kind"]) == "withdraw_profit")
    cl_id = next(str(d["id"]) for d in dues if str(d["kind"]) == "close")

    sent, send = _send_capture()
    edited, edit = _edit_capture()
    out = pa.handle_partner_callback(
        conn,
        f"pa:pd:{wd_id}",
        callback=_owner_cb(data=f"pa:pd:{wd_id}"),
        send=send,
        answer=_answer_capture()[1],
        edit=edit,
        futures=None,
        now_ms=NOW + 8,
    )
    assert out == {"ok": True, "cmd": "paid"}
    assert ps.get_partner(conn, b_id)["telegram_user_id"] == TG
    note = _partner_texts(sent)[0]
    assert "โอน" in note
    assert "บอทจะไม่แสดงยอดอีก" not in note
    assert "⚠️ ยังมียอดค้างจ่าย — ยังไม่ตัดการผูก (โอนให้ครบก่อน)" in edited[0]["text"]

    sent2, send2 = _send_capture()
    edited2, edit2 = _edit_capture()
    out2 = pa.handle_partner_callback(
        conn,
        f"pa:pd:{cl_id}",
        callback=_owner_cb(data=f"pa:pd:{cl_id}"),
        send=send2,
        answer=_answer_capture()[1],
        edit=edit2,
        futures=None,
        now_ms=NOW + 9,
    )
    assert out2 == {"ok": True, "cmd": "paid"}
    assert ps.get_partner(conn, b_id)["telegram_user_id"] is None
    assert "บอทจะไม่แสดงยอดอีก" in _partner_texts(sent2)[0]
    assert "ยังไม่ตัดการผูก" not in edited2[0]["text"]
    conn.close()


def test_closed_start_with_spent_invite_shows_the_card(
    tmp_path: Path, monkeypatch: Any, env_chat: None
) -> None:
    conn = open_db(tmp_path / "spent-start.db")
    _patch_value(monkeypatch, "1100")
    _, b_id = _admin_and_partner(conn, "Bob")
    inv = ps.create_invite(conn, b_id, now_ms=NOW + 2)
    assert inv["ok"] is True
    code = str(inv["code"])
    assert ps.bind_invite(conn, code, TG, admin_chat_id=ADMIN, now_ms=NOW + 3)["ok"]
    assert ps.apply_deposit(conn, b_id, D("100"), D("1100"), NOW + 5)["ok"]
    closed = ps.apply_close(conn, b_id, D("1100"), NOW + 6)
    assert closed["ok"] is True and D(closed["cash"]) > 0
    before = ps.get_partner(conn, b_id)
    assert before is not None
    dues = [(str(d["id"]), int(d["payout_micro"] or 0)) for d in ps.list_due_payouts(conn)]
    assert dues
    portal.reset_value_cache()
    sent, send = _send_capture()
    out = portal.handle_portal_text(
        conn,
        _pmsg(f"/start {code}"),
        send=send,
        futures=object(),
        now_ms=NOW + 9,
    )
    assert out == {"ok": True, "cmd": "card"}
    note = _partner_texts(sent)[0]
    assert "ปิดพอร์ตแล้ว" in note
    assert "รอโอน" in note
    assert "ลิงก์เชิญไม่ถูกต้อง" not in note
    after = ps.get_partner(conn, b_id)
    assert after is not None
    assert after["telegram_user_id"] == TG
    assert after["invite_hash"] is None
    assert after["status"] == "closed"
    assert int(after["units_e8"]) == int(before["units_e8"])
    assert int(after["cost_micro"]) == int(before["cost_micro"])
    assert [(str(d["id"]), int(d["payout_micro"] or 0)) for d in ps.list_due_payouts(conn)] == dues
    conn.close()


def test_start_while_due_keeps_invite_then_binds_after_paid(
    tmp_path: Path, monkeypatch: Any, env_chat: None
) -> None:
    conn = open_db(tmp_path / "start-due.db")
    _patch_value(monkeypatch, "1000")
    _, b_id = _admin_and_partner(conn, "Bob")
    _bind(conn, b_id, now_ms=NOW + 2)
    assert ps.apply_deposit(conn, b_id, D("100"), D("1100"), NOW + 5)["ok"]
    closed = ps.apply_close(conn, b_id, D("1100"), NOW + 6)
    assert closed["ok"] is True and D(closed["cash"]) > 0
    bob2 = ps.create_partner(conn, name="Bob2", now_ms=NOW + 7)
    b2 = str(bob2["partner_id"])
    inv = ps.create_invite(conn, b2, now_ms=NOW + 8)
    assert inv["ok"] is True
    code = str(inv["code"])
    before = ps.get_partner(conn, b2)
    assert before is not None
    hash_before = before["invite_hash"]
    exp_before = before["invite_expires_at_ms"]

    portal.reset_value_cache()
    sent, send = _send_capture()
    out = portal.handle_portal_text(
        conn,
        _pmsg(f"/start {code}"),
        send=send,
        futures=object(),
        now_ms=NOW + 9,
    )
    assert out == {"ok": False, "reason": "closed_payout_due"}
    assert any("ผูกบัญชีใหม่ไม่ได้" in t for t in _partner_texts(sent))
    after = ps.get_partner(conn, b2)
    assert after is not None
    assert after["invite_hash"] == hash_before
    assert after["invite_expires_at_ms"] == exp_before
    assert after["telegram_user_id"] is None
    assert ps.get_partner(conn, b_id)["telegram_user_id"] == TG

    rid = next(str(d["id"]) for d in ps.list_due_payouts(conn) if str(d["partner_id"]) == b_id)
    paid = pa.handle_partner_callback(
        conn,
        f"pa:pd:{rid}",
        callback=_owner_cb(data=f"pa:pd:{rid}"),
        send=_send_capture()[1],
        answer=_answer_capture()[1],
        edit=_edit_capture()[1],
        futures=None,
        now_ms=NOW + 10,
    )
    assert paid["ok"] is True
    assert ps.get_partner(conn, b_id)["telegram_user_id"] is None

    portal.reset_value_cache()
    sent2, send2 = _send_capture()
    bound = portal.handle_portal_text(
        conn,
        _pmsg(f"/start {code}"),
        send=send2,
        futures=object(),
        now_ms=NOW + 11,
    )
    assert bound["ok"] is True
    assert bound["partner_id"] == b2
    bound_row = ps.partner_by_telegram_id(conn, TG)
    assert bound_row is not None and bound_row["id"] == b2
    assert ps.get_partner(conn, b2)["invite_hash"] is None
    conn.close()


def test_start_rebinds_closed_row_when_nothing_is_due(
    tmp_path: Path, monkeypatch: Any, env_chat: None
) -> None:
    conn = open_db(tmp_path / "rebind.db")
    _patch_guard(monkeypatch)
    _patch_value(monkeypatch, "1000")
    _, b_id = _admin_and_partner(conn, "Bob")
    _bind(conn, b_id, now_ms=NOW + 2)
    edited, edit = _edit_capture()

    def send(
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        chat_id: str | None = None,
        **kw: Any,
    ) -> int:
        if chat_id is not None:
            raise RuntimeError("telegram down")
        return 1

    assert _confirm_close(conn, b_id, send=send, edit=edit, now_ms=NOW + 10)["ok"]
    assert ps.get_partner(conn, b_id)["telegram_user_id"] == TG
    bob2 = ps.create_partner(conn, name="Bob2", now_ms=NOW + 12)
    b2 = str(bob2["partner_id"])
    inv = ps.create_invite(conn, b2, now_ms=NOW + 13)
    portal.reset_value_cache()
    sent, send_ok = _send_capture()
    out = portal.handle_portal_text(
        conn,
        _pmsg(f"/start {inv['code']}"),
        send=send_ok,
        futures=object(),
        now_ms=NOW + 14,
    )
    assert out["ok"] is True and out["partner_id"] == b2
    assert ps.get_partner(conn, b_id)["telegram_user_id"] is None
    assert ps.get_partner(conn, b_id)["status"] == "closed"
    assert ps.get_partner(conn, b2)["telegram_user_id"] == TG
    assert any("ผูกบัญชีแล้ว" in t for t in _partner_texts(sent))
    conn.close()


def test_active_start_does_not_consume_a_new_code(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "active-start.db")
    _patch_value(monkeypatch, "1000")
    _, b_id = _admin_and_partner(conn)
    _bind(conn, b_id, now_ms=NOW + 2)
    other = ps.create_partner(conn, name="C", now_ms=NOW + 3)
    cid = str(other["partner_id"])
    inv = ps.create_invite(conn, cid, now_ms=NOW + 4)
    before = (ps.get_partner(conn, cid)["invite_hash"], ps.get_partner(conn, cid)["invite_expires_at_ms"])
    portal.reset_value_cache()
    out = portal.handle_portal_text(
        conn,
        _pmsg(f"/start {inv['code']}"),
        send=_send_capture()[1],
        futures=object(),
        now_ms=NOW + 5,
    )
    assert out.get("cmd") == "card"
    assert ps.get_partner(conn, b_id)["telegram_user_id"] == TG
    assert ps.get_partner(conn, cid)["telegram_user_id"] is None
    assert (
        ps.get_partner(conn, cid)["invite_hash"],
        ps.get_partner(conn, cid)["invite_expires_at_ms"],
    ) == before
    conn.close()


def test_unbind_refuses_closed_partner_with_payout_due(
    tmp_path: Path, monkeypatch: Any, env_chat: None
) -> None:
    conn = open_db(tmp_path / "unbind-due.db")
    _, b_id = _admin_and_partner(conn, "Bob")
    _bind(conn, b_id, now_ms=NOW + 2)
    assert ps.apply_deposit(conn, b_id, D("100"), D("1100"), NOW + 5)["ok"]
    assert ps.apply_close(conn, b_id, D("1100"), NOW + 6)["ok"]
    sent, send = _send_capture()
    out = pa.handle_partner_text(
        conn,
        "p unbind Bob",
        message=_owner_msg("p unbind Bob"),
        send=send,
        futures=object(),
        now_ms=NOW + 7,
    )
    assert out == {"ok": False, "reason": "payout_due"}
    assert "ปิดแล้วและยังมียอดรอโอน" in sent[0]["text"]
    assert "ยกเลิกการผูกไม่ได้" in sent[0]["text"]
    assert ps.get_partner(conn, b_id)["telegram_user_id"] == TG
    rid = next(str(d["id"]) for d in ps.list_due_payouts(conn) if str(d["partner_id"]) == b_id)
    assert ps.mark_paid(conn, rid, NOW + 8)["ok"]
    sent2, send2 = _send_capture()
    out2 = pa.handle_partner_text(
        conn,
        "p unbind Bob",
        message=_owner_msg("p unbind Bob"),
        send=send2,
        futures=object(),
        now_ms=NOW + 9,
    )
    assert out2["ok"] is True
    assert ps.get_partner(conn, b_id)["telegram_user_id"] is None
    conn.close()


def test_p_closed_cap_bangkok_dates_and_empty(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "closed-list.db")
    assert pa.parse_partner_command("p closed") == {"cmd": "closed"}
    assert pa.parse_partner_command("p closed Bob") == {"error": "usage"}
    assert "p closed" in pa.HELP_TEXT
    sent, send = _send_capture()
    empty = pa.handle_partner_text(
        conn, "p closed", message=_owner_msg("p closed"), send=send, futures=None, now_ms=NOW
    )
    assert empty == {"ok": True, "cmd": "closed", "n": 0}
    assert sent[0]["text"] == "ยังไม่มีพาร์ทเนอร์ที่ปิด"

    admin = ps.create_partner(conn, name="admin", is_admin=True, now_ms=NOW)
    assert ps.apply_deposit(conn, admin["partner_id"], D("1000"), D("1000"), NOW)["ok"]
    join = _ms(2026, 10, 1, 0, 30)
    dated = ps.create_partner(conn, name="Dawn", now_ms=join - 5)
    did = str(dated["partner_id"])
    assert ps.apply_deposit(conn, did, D("100"), D("1100"), join)["ok"]
    assert ps.apply_close(conn, did, D("1100"), join + 60_000)["ok"]
    sent_d, send_d = _send_capture()
    pa.handle_partner_text(
        conn, "p closed", message=_owner_msg("p closed"), send=send_d, futures=None, now_ms=join + 1
    )
    body = sent_d[0]["text"]
    assert body.startswith("พาร์ทเนอร์ที่ปิดแล้ว (1)")
    assert "· Dawn · ปิด 01/10/2026 · 100.00 USDT · รอโอน 100.00 USDT" in body
    assert "30/09/2026" not in body
    rid = next(str(d["id"]) for d in ps.list_due_payouts(conn) if str(d["partner_id"]) == did)
    assert ps.mark_paid(conn, rid, join + 120_000)["ok"]
    sent_p, send_p = _send_capture()
    pa.handle_partner_text(
        conn,
        "p closed",
        message=_owner_msg("p closed"),
        send=send_p,
        futures=None,
        now_ms=join + 130_000,
    )
    assert "· Dawn · ปิด 01/10/2026 · 100.00 USDT · โอนแล้ว" in sent_p[0]["text"]
    assert "รอโอน" not in sent_p[0]["text"]

    # 40 then 41. Dawn stays; the extra rows are zero-cash closes.
    base = join + 200_000
    for i in range(39):
        made = ps.create_partner(conn, name=f"N{i:02d}", now_ms=base + i)
        pid = str(made["partner_id"])
        assert ps.apply_close(conn, pid, D("1000"), base + 1000 + i)["ok"]
    sent40, send40 = _send_capture()
    out40 = pa.handle_partner_text(
        conn, "p closed", message=_owner_msg("p closed"), send=send40, futures=None, now_ms=base + 5000
    )
    assert out40["n"] == 40
    assert "more" not in sent40[0]["text"]
    assert "N38" in sent40[0]["text"]
    assert "Dawn" in sent40[0]["text"]
    made_last = ps.create_partner(conn, name="N39", now_ms=base + 6000)
    assert ps.apply_close(conn, str(made_last["partner_id"]), D("1000"), base + 7000)["ok"]
    sent41, send41 = _send_capture()
    out41 = pa.handle_partner_text(
        conn, "p closed", message=_owner_msg("p closed"), send=send41, futures=None, now_ms=base + 8000
    )
    assert out41["n"] == 40
    text41 = sent41[0]["text"]
    assert text41.splitlines()[1].startswith("· N39 ")
    assert "อีก 1 คน" in text41
    assert "Dawn" not in text41
    conn.close()


def test_rejoin_new_account_restarts_close_clock(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "rejoin.db")
    state = _patch_value(monkeypatch, "1100")
    _patch_guard(monkeypatch)
    portal.reset_value_cache()
    join_old = _ms(2026, 9, 1, 12, 0)
    _, bob = _admin_and_partner(conn, "Bob", now_ms=join_old - 10_000)
    _bind(conn, bob, now_ms=join_old - 5_000)
    assert ps.apply_deposit(conn, bob, D("100"), D("1100"), join_old)["ok"]
    closed = ps.apply_close(conn, bob, D("1100"), join_old + 86_400_000)
    assert closed["ok"] is True and D(closed["cash"]) > 0
    old_join = partner_rules.joined_at_ms(conn, bob)
    old_close = ps.close_summary(conn, bob)["closed_at_ms"]
    assert old_join == join_old
    rid = next(str(d["id"]) for d in ps.list_due_payouts(conn) if str(d["partner_id"]) == bob)
    paid = pa.handle_partner_callback(
        conn,
        f"pa:pd:{rid}",
        callback=_owner_cb(data=f"pa:pd:{rid}"),
        send=_send_capture()[1],
        answer=_answer_capture()[1],
        edit=_edit_capture()[1],
        futures=object(),
        now_ms=join_old + 86_400_000 + 1,
    )
    assert paid["ok"] is True
    assert ps.get_partner(conn, bob)["telegram_user_id"] is None

    added = pa.handle_partner_text(
        conn,
        "p add Bob2",
        message=_owner_msg("p add Bob2"),
        send=_send_capture()[1],
        futures=object(),
        now_ms=join_old + 90_000_000,
    )
    assert added["ok"] is True
    sent_i, send_i = _send_capture()
    invited = pa.handle_partner_text(
        conn,
        "p invite Bob2",
        message=_owner_msg("p invite Bob2"),
        send=send_i,
        futures=object(),
        now_ms=join_old + 90_000_001,
    )
    assert invited["ok"] is True
    code = ""
    for line in sent_i[0]["text"].splitlines():
        if line.startswith("/start "):
            code = line.split(None, 1)[1].strip()
    assert code
    portal.reset_value_cache()
    bound = portal.handle_portal_text(
        conn,
        _pmsg(f"/start {code}"),
        send=_send_capture()[1],
        futures=object(),
        now_ms=join_old + 90_000_002,
    )
    assert bound["ok"] is True
    bob2 = str(bound["partner_id"])
    assert ps.get_partner(conn, bob2)["name"] == "Bob2"

    new_join = _ms(2026, 11, 1, 12, 0)
    assert ps.apply_deposit(conn, bob2, D("100"), D("1100"), new_join)["ok"]
    assert partner_rules.joined_at_ms(conn, bob2) == new_join
    assert partner_rules.joined_at_ms(conn, bob) == old_join
    assert ps.close_summary(conn, bob)["closed_at_ms"] == old_close

    row2 = ps.get_partner(conn, bob2)
    assert row2 is not None
    day0 = partner_rules.block(conn, row2, "close", nav=D("1"), now_ms=new_join)
    assert day0 is not None and day0.code == "close_too_soon"
    day30 = _ms(2026, 12, 1, 0, 0)
    assert partner_rules.block(conn, row2, "close", nav=D("1"), now_ms=day30) is None

    portal.reset_value_cache()
    opened = portal.handle_portal_callback(
        conn,
        "pp:rq:cl",
        callback=_pcb("pp:rq:cl"),
        send=_send_capture()[1],
        answer=_answer_capture()[1],
        edit=_edit_capture()[1],
        futures=object(),
        now_ms=day30,
    )
    assert opened["ok"] is True

    portal.reset_value_cache()
    answers, answer = _answer_capture()
    tap = portal.handle_portal_callback(
        conn,
        "pp:rq:cl",
        callback=_pcb("pp:rq:cl"),
        send=_send_capture()[1],
        answer=answer,
        edit=_edit_capture()[1],
        futures=object(),
        now_ms=new_join,
    )
    assert tap == {"ok": False, "reason": "close_too_soon"}
    assert answers[0][1] == day0.toast

    state["wallet"] = "1210"
    portal.reset_value_cache()
    for data, code_name in (("pp:rq:cv", "convert_min"), ("pp:rq:wd", "withdraw_min")):
        answers_m, answer_m = _answer_capture()
        out_m = portal.handle_portal_callback(
            conn,
            data,
            callback=_pcb(data),
            send=_send_capture()[1],
            answer=answer_m,
            edit=_edit_capture()[1],
            futures=object(),
            now_ms=new_join + 1,
        )
        assert out_m["reason"] == code_name
        assert answers_m[0][1]

    state["wallet"] = "0"
    portal.reset_value_cache()
    before_req = int(conn.execute("SELECT COUNT(*) AS n FROM partner_request").fetchone()["n"])
    for data in ("pp:go:cv", "pp:go:wd"):
        answers_n, answer_n = _answer_capture()
        out_n = portal.handle_portal_callback(
            conn,
            data,
            callback=_pcb(data),
            send=_send_capture()[1],
            answer=answer_n,
            edit=_edit_capture()[1],
            futures=object(),
            now_ms=new_join + 2,
        )
        assert out_n == {"ok": False, "reason": "nav_not_positive"}
        assert answers_n[0][1] == "อ่านยอดไม่ได้ตอนนี้ ลองใหม่อีกครั้ง"
    assert int(conn.execute("SELECT COUNT(*) AS n FROM partner_request").fetchone()["n"]) == before_req

    sent_dep, send_dep = _send_capture()
    low = portal.handle_portal_text(
        conn, _pmsg("ฝาก 10"), send=send_dep, futures=object(), now_ms=new_join + 3
    )
    assert low["reason"] == "bad_amount"
    assert "50" in _partner_texts(sent_dep)[0]
    assert (
        int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM partner_request WHERE partner_id=? AND status='pending'",
                (bob2,),
            ).fetchone()["n"]
        )
        == 0
    )
    sent_ok, send_ok = _send_capture()
    good = portal.handle_portal_text(
        conn, _pmsg("ฝาก 50"), send=send_ok, futures=object(), now_ms=new_join + 4
    )
    assert good["ok"] is True
    src = conn.execute(
        "SELECT source FROM partner_request WHERE partner_id=? AND status='pending'",
        (bob2,),
    ).fetchone()
    assert src["source"] == "portal"
    again = portal.handle_portal_text(
        conn, _pmsg("ฝาก 60"), send=_send_capture()[1], futures=object(), now_ms=new_join + 5
    )
    assert again["reason"] == "request_pending"

    for cmd in ("p dep Bob 100", "p cv Bob", "p wd Bob", "p close Bob"):
        sent_m, send_m = _send_capture()
        refused = pa.handle_partner_text(
            conn,
            cmd,
            message=_owner_msg(cmd),
            send=send_m,
            futures=object(),
            now_ms=new_join + 6,
        )
        assert refused["reason"] == "closed"
        assert "ปิดแล้ว" in sent_m[0]["text"]

    assert partner_rules.joined_at_ms(conn, bob) == old_join
    assert ps.close_summary(conn, bob)["closed_at_ms"] == old_close
    fresh = ps.get_partner(conn, bob2)
    assert fresh is not None
    assert partner_rules.block(conn, fresh, "close", nav=D("1"), now_ms=day30) is None
    conn.close()


def test_send_to_partner_treats_none_as_failure(env_chat: None) -> None:
    row = {"telegram_user_id": TG, "is_admin": 0}
    assert pa.send_to_partner(lambda *a, **k: None, row, "hi") is False
    assert pa.send_to_partner(lambda *a, **k: False, row, "hi") is False
    assert pa.send_to_partner(lambda *a, **k: 9, row, "hi") is True

    def boom(*_a: Any, **_k: Any) -> int:
        raise RuntimeError("telegram down")

    assert pa.send_to_partner(boom, row, "hi") is False


def test_paid_last_due_send_none_keeps_binding(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "paid-none.db")
    _, b_id = _admin_and_partner(conn)
    _bind(conn, b_id, now_ms=NOW + 2)
    assert ps.apply_deposit(conn, b_id, D("100"), D("1100"), NOW + 5)["ok"]
    closed = ps.apply_close(conn, b_id, D("1100"), NOW + 6)
    assert closed["ok"] is True and D(closed["cash"]) > 0
    rid = next(str(d["id"]) for d in ps.list_due_payouts(conn) if str(d["partner_id"]) == b_id)
    sent, real_send = _send_capture()

    def send(
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        chat_id: str | None = None,
        **kw: Any,
    ) -> int | None:
        if chat_id is not None:
            return None
        return real_send(text, reply_markup=reply_markup, chat_id=chat_id, **kw)

    edited, edit = _edit_capture()
    out = pa.handle_partner_callback(
        conn,
        f"pa:pd:{rid}",
        callback=_owner_cb(data=f"pa:pd:{rid}"),
        send=send,
        answer=_answer_capture()[1],
        edit=edit,
        futures=None,
        now_ms=NOW + 7,
    )
    assert out == {"ok": True, "cmd": "paid"}
    assert ps.get_partner(conn, b_id)["telegram_user_id"] == TG
    assert "⚠️ แจ้งพาร์ทเนอร์ไม่สำเร็จ — ยังไม่ตัดการผูก (p unbind B)" in edited[0]["text"]
    conn.close()


def test_never_bound_close_has_no_warning(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "unbound-close.db")
    _patch_guard(monkeypatch)
    _patch_value(monkeypatch, "1000")
    _, b_id = _admin_and_partner(conn)
    assert ps.get_partner(conn, b_id)["telegram_user_id"] is None
    sent: list[str] = []

    def send(
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        chat_id: str | None = None,
        **kw: Any,
    ) -> int:
        assert chat_id is None
        sent.append(text)
        return 1

    edited, edit = _edit_capture()
    out = _confirm_close(conn, b_id, send=send, edit=edit, now_ms=NOW + 10)
    assert out["ok"] is True
    assert sent == []
    assert "แจ้งพาร์ทเนอร์ไม่สำเร็จ" not in edited[0]["text"]
    assert "p unbind" not in edited[0]["text"]
    conn.close()


def test_send_fail_with_other_due_omits_unbind_hint(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "fail-due.db")
    _, b_id = _admin_and_partner(conn)
    _bind(conn, b_id, now_ms=NOW + 2)
    assert ps.apply_deposit(conn, b_id, D("1000"), D("2000"), NOW + 5)["ok"]
    assert ps.apply_withdraw_profit(conn, b_id, D("4000"), NOW + 6)["ok"]
    assert ps.apply_close(conn, b_id, D("4000"), NOW + 7)["ok"]
    wd_id = next(str(d["id"]) for d in ps.list_due_payouts(conn) if str(d["kind"]) == "withdraw_profit")

    def send(
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        chat_id: str | None = None,
        **kw: Any,
    ) -> int | None:
        return None if chat_id is not None else 1

    edited, edit = _edit_capture()
    out = pa.handle_partner_callback(
        conn,
        f"pa:pd:{wd_id}",
        callback=_owner_cb(data=f"pa:pd:{wd_id}"),
        send=send,
        answer=_answer_capture()[1],
        edit=edit,
        futures=None,
        now_ms=NOW + 8,
    )
    assert out == {"ok": True, "cmd": "paid"}
    text = edited[0]["text"]
    assert "⚠️ แจ้งพาร์ทเนอร์ไม่สำเร็จ — ยังไม่ตัดการผูก" in text
    assert "p unbind" not in text
    assert "⚠️ ยังมียอดค้างจ่าย — ยังไม่ตัดการผูก (โอนให้ครบก่อน)" in text
    assert ps.get_partner(conn, b_id)["telegram_user_id"] == TG
    conn.close()


def test_edit_false_sends_the_warning_body(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "edit-false.db")
    _patch_guard(monkeypatch)
    _patch_value(monkeypatch, "1000")
    _, b_id = _admin_and_partner(conn)
    _bind(conn, b_id, now_ms=NOW + 2)
    admin_sent: list[str] = []

    def send(
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        chat_id: str | None = None,
        **kw: Any,
    ) -> int | None:
        if chat_id is not None:
            return None
        admin_sent.append(text)
        return 1

    def edit(*_a: Any, **_k: Any) -> bool:
        return False

    out = _confirm_close(conn, b_id, send=send, edit=edit, now_ms=NOW + 10)
    assert out["ok"] is True
    assert ps.get_partner(conn, b_id)["telegram_user_id"] == TG
    assert admin_sent
    assert "⚠️ แจ้งพาร์ทเนอร์ไม่สำเร็จ — ยังไม่ตัดการผูก (p unbind B)" in admin_sent[0]
    conn.close()


def test_bind_invite_rolls_back_when_the_new_bind_aborts(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "bind-abort.db")
    _, bob = _admin_and_partner(conn)
    _bind(conn, bob, now_ms=NOW + 2)
    assert ps.apply_close(conn, bob, D("1000"), NOW + 10)["ok"]
    assert ps.list_due_payouts(conn) == []
    bob2 = ps.create_partner(conn, name="Bob2", now_ms=NOW + 11)
    b2 = str(bob2["partner_id"])
    inv = ps.create_invite(conn, b2, now_ms=NOW + 12)
    assert inv["ok"] is True
    before = ps.get_partner(conn, b2)
    assert before is not None
    hash_before = before["invite_hash"]
    exp_before = before["invite_expires_at_ms"]
    conn.execute(
        """
        CREATE TEMP TRIGGER partner_bind_abort
        BEFORE UPDATE OF telegram_user_id ON partner
        WHEN NEW.telegram_user_id IS NOT NULL AND OLD.telegram_user_id IS NULL
        BEGIN
            SELECT RAISE(ABORT, 'bind abort');
        END
        """
    )
    result = ps.bind_invite(conn, str(inv["code"]), TG, admin_chat_id=ADMIN, now_ms=NOW + 20)
    assert result == {"ok": False, "reason": "tg_in_use"}
    assert ps.get_partner(conn, bob)["telegram_user_id"] == TG
    after = ps.get_partner(conn, b2)
    assert after is not None
    assert after["telegram_user_id"] is None
    assert after["invite_hash"] == hash_before
    assert after["invite_expires_at_ms"] == exp_before
    conn.close()


def test_p_closed_long_names_stay_within_telegram_limit(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "closed-len.db")
    admin = ps.create_partner(conn, name="admin", is_admin=True, now_ms=NOW)
    assert ps.apply_deposit(conn, admin["partner_id"], D("1000"), D("1000"), NOW)["ok"]
    stamp = _ms(2026, 10, 2, 12, 0)
    for i in range(41):
        name = f"P{i:02d}{'X' * 21}"
        assert len(name) == 24
        made = ps.create_partner(conn, name=name, now_ms=stamp + i)
        assert ps.apply_close(conn, str(made["partner_id"]), D("1000"), stamp + 1000 + i)["ok"]
    sent, send = _send_capture()
    out = pa.handle_partner_text(
        conn, "p closed", message=_owner_msg("p closed"), send=send, futures=None, now_ms=stamp + 5000
    )
    text = sent[0]["text"]
    assert out["n"] == 40
    assert text.startswith("พาร์ทเนอร์ที่ปิดแล้ว (41)")
    assert "อีก 1 คน" in text
    assert len(text) <= 4096
    conn.close()


def test_close_summary_is_in_the_cash_out_and_final_paid_notes(
    tmp_path: Path, monkeypatch: Any, env_chat: None
) -> None:
    conn = open_db(tmp_path / "summary-notes.db")
    _patch_guard(monkeypatch)
    _patch_value(monkeypatch, "1100")
    _, b_id = _admin_and_partner(conn)
    _bind(conn, b_id, now_ms=NOW + 2)
    join = _ms(2026, 10, 1, 0, 30)
    assert ps.apply_deposit(conn, b_id, D("100"), D("1100"), join)["ok"]
    created = ps.create_request(conn, b_id, "close", join + 60_000, source="admin")
    assert created["ok"] is True
    rid = str(created["request_id"])
    sent, send = _send_capture()
    edited, edit = _edit_capture()
    out = pa.handle_partner_callback(
        conn,
        f"pa:ry:{rid}",
        callback=_owner_cb(data=f"pa:ry:{rid}"),
        send=send,
        answer=_answer_capture()[1],
        edit=edit,
        futures=object(),
        now_ms=join + 61_000,
    )
    assert out["ok"] is True
    note = _partner_texts(sent)[0]
    assert "ยืนยันปิดพอร์ต" in note
    assert "เข้าร่วม 01/10/2026" in note
    assert "ปิดเมื่อ 01/10/2026" in note
    assert "ยอดปิด" in note
    summary = ps.close_summary(conn, b_id)
    assert summary["close_payout_state"] == "due"
    assert ps.get_partner(conn, b_id)["telegram_user_id"] == TG

    sent2, send2 = _send_capture()
    paid = pa.handle_partner_callback(
        conn,
        f"pa:pd:{rid}",
        callback=_owner_cb(data=f"pa:pd:{rid}"),
        send=send2,
        answer=_answer_capture()[1],
        edit=_edit_capture()[1],
        futures=None,
        now_ms=join + 120_000,
    )
    assert paid == {"ok": True, "cmd": "paid"}
    final = _partner_texts(sent2)[0]
    assert "โอน" in final
    assert "เข้าร่วม 01/10/2026" in final
    assert "ปิดเมื่อ 01/10/2026" in final
    assert "ยอดปิด" in final
    assert ps.close_summary(conn, b_id)["close_payout_state"] == "paid"
    assert ps.get_partner(conn, b_id)["telegram_user_id"] is None
    conn.close()


def test_p_closed_paid_label_ignores_an_older_withdrawal(tmp_path: Path, env_chat: None) -> None:
    """A paid profit withdrawal must not label a zero-cash close as โอนแล้ว."""
    conn = open_db(tmp_path / "close-state.db")
    _, b_id = _admin_and_partner(conn)
    closed = ps.apply_close(conn, b_id, D("1000"), NOW + 5)
    assert closed["ok"] is True and D(closed["cash"]) == 0
    assert ps.close_summary(conn, b_id)["close_payout_state"] == "none"
    conn.execute(
        """
        INSERT INTO partner_request (
            id, partner_id, kind, status, payout_state, payout_micro,
            created_at_ms, expires_at_ms, decided_at_ms, paid_at_ms, source
        ) VALUES (?, ?, 'withdraw_profit', 'confirmed', 'paid', ?, ?, ?, ?, ?, 'admin')
        """,
        (
            "pr_aabbccddeeff0011",
            b_id,
            ps.usdt_to_micro(D("10")),
            NOW + 6,
            NOW + 7,
            NOW + 6,
            NOW + 8,
        ),
    )
    conn.commit()
    sent, send = _send_capture()
    pa.handle_partner_text(
        conn, "p closed", message=_owner_msg("p closed"), send=send, futures=None, now_ms=NOW + 9
    )
    text = sent[0]["text"]
    assert "ไม่มีเงินออก" in text
    assert "โอนแล้ว" not in text
    conn.close()


def test_close_payout_paid_flips_label_and_sends_the_summary(tmp_path: Path, env_chat: None) -> None:
    """รอโอน becomes โอนแล้ว only when the close payout itself is paid."""
    conn = open_db(tmp_path / "close-paid.db")
    join = _ms(2026, 10, 1, 0, 30)
    _, b_id = _admin_and_partner(conn, now_ms=join - 60_000)
    _bind(conn, b_id, now_ms=join - 30_000)
    assert ps.apply_deposit(conn, b_id, D("1000"), D("2000"), join)["ok"]
    assert ps.apply_withdraw_profit(conn, b_id, D("4000"), join + 60_000)["ok"]
    closed = ps.apply_close(conn, b_id, D("4000"), join + 120_000)
    assert closed["ok"] is True and D(closed["cash"]) > 0
    close_cash = pa.fmt_usdt(D(closed["cash"]))
    assert ps.close_summary(conn, b_id)["close_payout_state"] == "due"
    dues = ps.list_due_payouts(conn)
    wd_id = next(str(d["id"]) for d in dues if str(d["kind"]) == "withdraw_profit")
    cl_id = next(str(d["id"]) for d in dues if str(d["kind"]) == "close")

    sent, send = _send_capture()
    pa.handle_partner_text(
        conn, "p closed", message=_owner_msg("p closed"), send=send, futures=None, now_ms=join + 130_000
    )
    assert "รอโอน" in sent[0]["text"]
    assert "โอนแล้ว" not in sent[0]["text"]

    sent_wd, send_wd = _send_capture()
    paid_wd = pa.handle_partner_callback(
        conn,
        f"pa:pd:{wd_id}",
        callback=_owner_cb(data=f"pa:pd:{wd_id}"),
        send=send_wd,
        answer=_answer_capture()[1],
        edit=_edit_capture()[1],
        futures=None,
        now_ms=join + 140_000,
    )
    assert paid_wd == {"ok": True, "cmd": "paid"}
    assert ps.close_summary(conn, b_id)["close_payout_state"] == "due"
    assert "บอทจะไม่แสดงยอดอีก" not in _partner_texts(sent_wd)[0]
    sent_mid, send_mid = _send_capture()
    pa.handle_partner_text(
        conn, "p closed", message=_owner_msg("p closed"), send=send_mid, futures=None, now_ms=join + 150_000
    )
    assert "รอโอน" in sent_mid[0]["text"]
    assert "โอนแล้ว" not in sent_mid[0]["text"]

    sent_cl, send_cl = _send_capture()
    paid_cl = pa.handle_partner_callback(
        conn,
        f"pa:pd:{cl_id}",
        callback=_owner_cb(data=f"pa:pd:{cl_id}"),
        send=send_cl,
        answer=_answer_capture()[1],
        edit=_edit_capture()[1],
        futures=None,
        now_ms=join + 160_000,
    )
    assert paid_cl == {"ok": True, "cmd": "paid"}
    assert ps.close_summary(conn, b_id)["close_payout_state"] == "paid"
    final = _partner_texts(sent_cl)[0]
    assert "เข้าร่วม 01/10/2026" in final
    assert "ปิดเมื่อ 01/10/2026" in final
    assert f"ยอดปิด {close_cash} USDT" in final
    assert f"โอน {close_cash} USDT" in final
    sent_done, send_done = _send_capture()
    pa.handle_partner_text(
        conn, "p closed", message=_owner_msg("p closed"), send=send_done, futures=None, now_ms=join + 170_000
    )
    assert "โอนแล้ว" in sent_done[0]["text"]
    assert "รอโอน" not in sent_done[0]["text"]
    conn.close()


def test_create_partner_rejects_case_and_space_variants(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "names.db")
    admin = ps.create_partner(conn, name="admin", is_admin=True, now_ms=NOW)
    assert admin["ok"] is True
    assert ps.apply_deposit(conn, admin["partner_id"], D("1000"), D("1000"), NOW)["ok"]
    first = ps.create_partner(conn, name="Bob", now_ms=NOW + 1)
    assert first["ok"] is True
    for variant in ("bob", "BOB", " bob ", "Bob "):
        again = ps.create_partner(conn, name=variant, now_ms=NOW + 2)
        assert again == {"ok": False, "reason": "duplicate_name"}
    assert ps.create_partner(conn, name="   ", now_ms=NOW + 3) == {"ok": False, "reason": "bad_name"}
    assert [str(row["name"]) for row in ps.list_partners(conn)] == ["admin", "Bob"]

    assert ps.apply_close(conn, first["partner_id"], D("1000"), NOW + 4)["ok"]
    closed = ps.create_partner(conn, name="  bOb", now_ms=NOW + 5)
    assert closed == {"ok": False, "reason": "duplicate_name"}
    fresh = ps.create_partner(conn, name="Cara", now_ms=NOW + 6)
    assert fresh["ok"] is True
    spaced = ps.create_partner(conn, name="  Dee ", now_ms=NOW + 7)
    assert spaced["ok"] is True
    assert ps.get_partner(conn, spaced["partner_id"])["name"] == "Dee"
    assert ps.create_partner(conn, name="dee", now_ms=NOW + 8) == {"ok": False, "reason": "duplicate_name"}
    assert ps.get_partner(conn, first["partner_id"])["status"] == "closed"
    assert ps.get_partner(conn, fresh["partner_id"])["name"] == "Cara"
    composed = "\u0e04\u0e33"
    decomposed = "\u0e04\u0e4d\u0e32"
    thai = ps.create_partner(conn, name=composed, now_ms=NOW + 9)
    assert thai["ok"] is True
    assert ps.get_partner(conn, thai["partner_id"])["name"] == composed
    assert ps.create_partner(conn, name=decomposed, now_ms=NOW + 10) == {
        "ok": False,
        "reason": "duplicate_name",
    }
    conn.close()


def test_create_partner_duplicate_is_visible_to_a_second_connection(tmp_path: Path) -> None:
    path = tmp_path / "two-conn.db"
    conn_a = open_db(path)
    conn_b = open_db(path)
    try:
        assert ps.create_partner(conn_a, name="Bob", now_ms=NOW)["ok"] is True
        assert ps.create_partner(conn_b, name="bob", now_ms=NOW + 1) == {
            "ok": False,
            "reason": "duplicate_name",
        }
        assert [str(row["name"]) for row in ps.list_partners(conn_b)] == ["Bob"]
    finally:
        conn_b.close()
        conn_a.close()


def test_p_add_and_invite_refuse_a_closed_partner(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "closed-cmd.db")
    _patch_value(monkeypatch, "1100")
    _, bob = _admin_and_partner(conn, "Bob")
    _bind(conn, bob, now_ms=NOW + 2)
    assert ps.apply_deposit(conn, bob, D("100"), D("1100"), NOW + 5)["ok"]
    closed = ps.apply_close(conn, bob, D("1100"), NOW + 6)
    assert closed["ok"] is True and D(closed["cash"]) > 0
    before = ps.get_partner(conn, bob)
    assert before is not None
    dues = [
        (str(d["id"]), str(d["payout_state"]), int(d["payout_micro"] or 0)) for d in ps.list_due_payouts(conn)
    ]
    events = int(conn.execute("SELECT COUNT(*) AS n FROM partner_event").fetchone()["n"])

    sent_add, send_add = _send_capture()
    added = pa.handle_partner_text(
        conn,
        "p add bob",
        message=_owner_msg("p add bob"),
        send=send_add,
        futures=object(),
        now_ms=NOW + 7,
    )
    assert added == {"ok": False, "reason": "duplicate_name"}
    assert "Bob ปิดแล้ว" in sent_add[0]["text"]
    assert "ชื่อใหม่" in sent_add[0]["text"]
    assert sum(1 for row in ps.list_partners(conn) if not row["is_admin"]) == 1

    sent_inv, send_inv = _send_capture()
    invited = pa.handle_partner_text(
        conn,
        "p invite Bob",
        message=_owner_msg("p invite Bob"),
        send=send_inv,
        futures=object(),
        now_ms=NOW + 8,
    )
    assert invited == {"ok": False, "reason": "payout_due"}
    assert "Bob ปิดแล้วและยังรอโอน" in sent_inv[0]["text"]
    assert "ออกลิงก์ให้บัญชีเดิมไม่ได้" in sent_inv[0]["text"]
    after = ps.get_partner(conn, bob)
    assert after is not None
    assert after["status"] == "closed"
    assert after["telegram_user_id"] == TG
    assert after["invite_hash"] is None
    assert int(after["units_e8"]) == int(before["units_e8"])
    assert int(after["cost_micro"]) == int(before["cost_micro"])
    assert [
        (str(d["id"]), str(d["payout_state"]), int(d["payout_micro"] or 0)) for d in ps.list_due_payouts(conn)
    ] == dues
    assert int(conn.execute("SELECT COUNT(*) AS n FROM partner_event").fetchone()["n"]) == events

    cara = ps.create_partner(conn, name="Cara", now_ms=NOW + 9)
    assert cara["ok"] is True
    cid = str(cara["partner_id"])
    issued = ps.create_invite(conn, cid, now_ms=NOW + 10)
    assert issued["ok"] is True
    quiet = ps.apply_close(conn, cid, D("1100"), NOW + 11)
    assert quiet["ok"] is True and D(quiet["cash"]) == 0
    held = ps.get_partner(conn, cid)
    assert held is not None
    hash_before = held["invite_hash"]
    exp_before = held["invite_expires_at_ms"]
    sent_q, send_q = _send_capture()
    quiet_invite = pa.handle_partner_text(
        conn,
        "p invite Cara",
        message=_owner_msg("p invite Cara"),
        send=send_q,
        futures=object(),
        now_ms=NOW + 12,
    )
    assert quiet_invite == {"ok": False, "reason": "closed"}
    assert "ออกลิงก์ให้บัญชีเดิมไม่ได้" in sent_q[0]["text"]
    assert "ชื่อใหม่" in sent_q[0]["text"]
    still = ps.get_partner(conn, cid)
    assert still is not None
    assert still["status"] == "closed"
    assert still["invite_hash"] == hash_before
    assert still["invite_expires_at_ms"] == exp_before
    assert still["telegram_user_id"] is None
    assert ps.list_due_payouts(conn)  # Bob's close payout is still due
    conn.close()
