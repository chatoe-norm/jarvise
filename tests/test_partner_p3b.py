"""Partner P3b: requests, admin confirm/reject/paid, payout-due guard."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from jarvise_assist import partner_admin as pa
from jarvise_assist import partner_portal as portal
from jarvise_assist import partner_store as ps
from jarvise_assist.partner_value import AccountValue, AccountValueError
from jarvise_ingest.db import open_db

D = Decimal
NOW = 1_700_000_000_000
ADMIN = "42"


@pytest.fixture(autouse=True)
def _clean_transfer_guard(monkeypatch: Any) -> None:
    """P5/B2 guards are covered elsewhere; keep legacy confirms/requests clean."""
    from jarvise_assist.partner_audit import GuardResult

    def _fake(conn: Any, client: Any, *, now_ms: int, req: Any) -> tuple[GuardResult, str]:
        return (
            GuardResult(status="ok", checked_at_ms=int(now_ms), findings=[], warn=False, error=None),
            "",
        )

    monkeypatch.setattr(pa, "_transfer_guard", _fake)
    monkeypatch.setattr(
        "jarvise_assist.partner_unrecorded.guard_unrecorded_status",
        lambda *_a, **_k: "clear",
    )
    monkeypatch.setattr(
        "jarvise_assist.partner_unrecorded.resolve_unrecorded_status",
        lambda *_a, **_k: "clear",
    )


@pytest.fixture(autouse=True)
def _zero_close_min_days(monkeypatch: Any) -> None:
    """Partner rules are covered in test_partner_rules.py; keep legacy flows clean."""
    import jarvise_assist.partner_rules as partner_rules

    monkeypatch.setattr(partner_rules, "CLOSE_MIN_DAYS", 0)


class FakeValueClient:
    def __init__(
        self,
        *,
        wallet: str | None = None,
        unrealized: str = "0",
        error: AccountValueError | None = None,
    ) -> None:
        self.wallet = wallet
        self.unrealized = unrealized
        self.error = error
        self.calls = 0

    def balance_rows(self) -> list[dict[str, Any]]:
        self.calls += 1
        if self.error is not None:
            raise RuntimeError("no")
        assert self.wallet is not None
        return [{"asset": "USDT", "balance": self.wallet}]

    def position_rows(self) -> list[dict[str, Any]]:
        if self.error is not None:
            raise RuntimeError("no")
        if D(self.unrealized) == 0:
            return []
        return [
            {
                "symbol": "BTCUSDT",
                "positionAmt": "1",
                "unRealizedProfit": self.unrealized,
                "marginAsset": "USDT",
            }
        ]


def _patch_read(monkeypatch: Any, client: FakeValueClient) -> None:
    def fake(cli: Any, *, now_ms: int, attempts: int = 3) -> AccountValue | AccountValueError:
        if isinstance(cli, FakeValueClient):
            cli.calls += 1
            if cli.error is not None:
                return cli.error
            if cli.wallet is not None:
                w = D(cli.wallet)
                u = D(cli.unrealized)
                return AccountValue(
                    wallet=w, unrealized=u, value=w + u, open_positions=0 if u == 0 else 1, read_at_ms=now_ms
                )
        return AccountValueError(reason="no_client")

    monkeypatch.setattr(portal, "read_account_value", fake)
    monkeypatch.setattr(pa, "read_account_value", fake)


def _send_capture() -> tuple[list[dict[str, Any]], Any]:
    sent: list[dict[str, Any]] = []

    def send(
        text: str, *, reply_markup: dict[str, Any] | None = None, chat_id: str | None = None, **kw: Any
    ) -> int:
        mid = len(sent) + 1
        entry: dict[str, Any] = {"text": text, "reply_markup": reply_markup, "message_id": mid}
        if chat_id is not None:
            entry["chat_id"] = chat_id
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
            entry["chat_id"] = chat_id
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
        "id": "cq1",
        "data": data,
        "from": {"id": int(ADMIN)},
        "message": {
            "message_id": mid,
            "chat": {"id": int(ADMIN), "type": "private"},
            "text": "preview",
        },
    }


def _pmsg(text: str, *, chat: int = 501) -> dict[str, Any]:
    return {
        "message_id": 1,
        "chat": {"id": chat, "type": "private"},
        "from": {"id": chat, "first_name": "P"},
        "text": text,
    }


def _pcb(data: str, *, chat: int = 501, mid: int = 7) -> dict[str, Any]:
    return {
        "id": "cq1",
        "data": data,
        "from": {"id": chat},
        "message": {
            "message_id": mid,
            "chat": {"id": chat, "type": "private"},
            "text": "card",
        },
    }


def _assert_privacy(sends: list[dict[str, Any]], edits: list[dict[str, Any]], *, actor: str) -> None:
    for item in list(sends) + list(edits):
        text = str(item.get("text") or "")
        markup = item.get("reply_markup")
        if "chat_id" in item:
            assert item["chat_id"] == actor
            for bad in ("NAV", "หน่วย", "wallet", "p init", "p dep"):
                assert bad not in text
            if markup:
                for row in markup.get("inline_keyboard") or []:
                    for btn in row:
                        assert str(btn.get("callback_data") or "").startswith("pp:")
        else:
            if markup:
                for row in markup.get("inline_keyboard") or []:
                    for btn in row:
                        data = str(btn.get("callback_data") or "")
                        if data:
                            assert data.startswith("pa:")


@pytest.fixture
def env_chat(monkeypatch: Any) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", ADMIN)


def _init_and_add(conn: Any, monkeypatch: Any) -> None:
    portal.reset_value_cache()
    client = FakeValueClient(wallet="1000")
    _patch_read(monkeypatch, client)
    answers, answer = _answer_capture()
    edited, edit = _edit_capture()
    pa.handle_partner_callback(
        conn,
        "pa:iy",
        callback=_owner_cb(data="pa:iy"),
        send=_send_capture()[1],
        answer=answer,
        edit=edit,
        futures=client,
        now_ms=NOW,
    )
    for name, t in (("B", NOW + 1), ("C", NOW + 2)):
        pa.handle_partner_text(
            conn,
            f"p add {name}",
            message=_owner_msg(f"p add {name}"),
            send=_send_capture()[1],
            futures=client,
            now_ms=t,
        )


def _bind(conn: Any, name: str, tg: str, *, now_ms: int) -> None:
    sent, send = _send_capture()
    pa.handle_partner_text(
        conn,
        f"p invite {name}",
        message=_owner_msg(f"p invite {name}"),
        send=send,
        futures=FakeValueClient(wallet="1000"),
        now_ms=now_ms,
    )
    code = ""
    for line in sent[0]["text"].splitlines():
        if line.startswith("/start "):
            code = line.split(None, 1)[1].strip()
    assert ps.bind_invite(conn, code, tg, admin_chat_id=ADMIN, now_ms=now_ms + 1)["ok"]


def test_full_abc_sequence(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "abc.db")
    all_sends: list[dict[str, Any]] = []
    all_edits: list[dict[str, Any]] = []
    _init_and_add(conn, monkeypatch)
    _bind(conn, "B", "501", now_ms=NOW + 10)
    _bind(conn, "C", "502", now_ms=NOW + 20)

    # B deposits 600 via portal text
    cli = FakeValueClient(wallet="1800")
    _patch_read(monkeypatch, cli)
    portal.reset_value_cache()
    sent, send = _send_capture()
    portal.handle_portal_text(conn, _pmsg("ฝาก 600", chat=501), send=send, futures=cli, now_ms=NOW + 30)
    all_sends.extend(sent)
    admin_dep = [s for s in sent if "chat_id" not in s and "แจ้งฝาก" in s["text"]]
    assert admin_dep
    rid_b = admin_dep[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[-1]
    answers, answer = _answer_capture()
    edited, edit = _edit_capture()
    sent2, send2 = _send_capture()
    pa.handle_partner_callback(
        conn,
        f"pa:dy:{rid_b}",
        callback=_owner_cb(data=f"pa:dy:{rid_b}"),
        send=send2,
        answer=answer,
        edit=edit,
        futures=cli,
        now_ms=NOW + 31,
    )
    all_sends.extend(sent2)
    all_edits.extend(edited)
    b = conn.execute("SELECT * FROM partner WHERE name='B'").fetchone()
    assert ps.units_from_e8(int(b["units_e8"])) == D("500")
    assert any(s.get("chat_id") == "501" and "บันทึกเงินฝาก" in s["text"] for s in sent2)

    # C deposits 330
    cli_c = FakeValueClient(wallet="1980")
    _patch_read(monkeypatch, cli_c)
    portal.reset_value_cache()
    sent, send = _send_capture()
    portal.handle_portal_text(conn, _pmsg("ฝาก 330", chat=502), send=send, futures=cli_c, now_ms=NOW + 40)
    all_sends.extend(sent)
    rid_c = [s for s in sent if "chat_id" not in s and "แจ้งฝาก" in s["text"]][0]["reply_markup"][
        "inline_keyboard"
    ][0][0]["callback_data"].split(":")[-1]
    answers, answer = _answer_capture()
    edited, edit = _edit_capture()
    sent2, send2 = _send_capture()
    pa.handle_partner_callback(
        conn,
        f"pa:dy:{rid_c}",
        callback=_owner_cb(data=f"pa:dy:{rid_c}"),
        send=send2,
        answer=answer,
        edit=edit,
        futures=cli_c,
        now_ms=NOW + 41,
    )
    all_sends.extend(sent2)
    all_edits.extend(edited)
    c = conn.execute("SELECT * FROM partner WHERE name='C'").fetchone()
    assert ps.units_from_e8(int(c["units_e8"])) == D("300")

    # C convert at V 2160
    cli_v = FakeValueClient(wallet="2160")
    _patch_read(monkeypatch, cli_v)
    portal.reset_value_cache()
    answers, answer = _answer_capture()
    edited, edit = _edit_capture()
    sent, send = _send_capture()
    portal.handle_portal_callback(
        conn,
        "pp:rq:cv",
        callback=_pcb("pp:rq:cv", chat=502),
        send=send,
        answer=answer,
        edit=edit,
        futures=cli_v,
        now_ms=NOW + 50,
    )
    all_sends.extend(sent)
    all_edits.extend(edited)
    answers, answer = _answer_capture()
    edited, edit = _edit_capture()
    sent, send = _send_capture()
    portal.handle_portal_callback(
        conn,
        "pp:go:cv",
        callback=_pcb("pp:go:cv", chat=502),
        send=send,
        answer=answer,
        edit=edit,
        futures=cli_v,
        now_ms=NOW + 51,
    )
    all_sends.extend(sent)
    all_edits.extend(edited)
    admin_rq = [s for s in sent if "chat_id" not in s and "คำขอจาก" in s["text"]]
    assert admin_rq
    assert "ทบกำไร" in admin_rq[0]["text"]
    rid_cv = admin_rq[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[-1]
    assert rid_cv.startswith("pr_")
    answers, answer = _answer_capture()
    edited, edit = _edit_capture()
    sent, send = _send_capture()
    pa.handle_partner_callback(
        conn,
        f"pa:ry:{rid_cv}",
        callback=_owner_cb(data=f"pa:ry:{rid_cv}"),
        send=send,
        answer=answer,
        edit=edit,
        futures=cli_v,
        now_ms=NOW + 52,
    )
    all_sends.extend(sent)
    all_edits.extend(edited)
    c = conn.execute("SELECT * FROM partner WHERE name='C'").fetchone()
    assert ps.units_from_e8(int(c["units_e8"])) == D("287.5")
    assert ps.usdt_from_micro(int(c["cost_micro"])) == D("345")
    admin = ps.admin_partner(conn)
    assert admin is not None
    # admin started 1000, gained 12.5 fee units
    assert ps.units_from_e8(int(admin["units_e8"])) == D("1012.5")
    assert any(s.get("chat_id") == "502" and "ทบกำไรแล้ว" in s["text"] for s in sent)

    # B close at V 1800
    cli_cl = FakeValueClient(wallet="1800")
    _patch_read(monkeypatch, cli_cl)
    portal.reset_value_cache()
    answers, answer = _answer_capture()
    edited, edit = _edit_capture()
    sent, send = _send_capture()
    portal.handle_portal_callback(
        conn,
        "pp:go:cl",
        callback=_pcb("pp:go:cl", chat=501),
        send=send,
        answer=answer,
        edit=edit,
        futures=cli_cl,
        now_ms=NOW + 60,
    )
    all_sends.extend(sent)
    all_edits.extend(edited)
    rid_cl = [s for s in sent if "chat_id" not in s and "คำขอจาก" in s["text"]][0]["reply_markup"][
        "inline_keyboard"
    ][0][0]["callback_data"].split(":")[-1]
    answers, answer = _answer_capture()
    edited, edit = _edit_capture()
    sent, send = _send_capture()
    pa.handle_partner_callback(
        conn,
        f"pa:ry:{rid_cl}",
        callback=_owner_cb(data=f"pa:ry:{rid_cl}"),
        send=send,
        answer=answer,
        edit=edit,
        futures=cli_cl,
        now_ms=NOW + 61,
    )
    all_sends.extend(sent)
    all_edits.extend(edited)
    assert "ค้างจ่าย 500.00" in edited[0]["text"] or "ค้างจ่าย 500.00 USDT" in edited[0]["text"]
    assert any(
        b.get("callback_data", "").startswith("pa:pd:")
        for row in edited[0]["reply_markup"]["inline_keyboard"]
        for b in row
    )
    close_note = [s for s in sent if s.get("chat_id") == "501" and "ยืนยันปิดพอร์ต" in s["text"]]
    assert close_note

    # While due: new confirm refused, V not read
    ps.create_request(conn, str(c["id"]), "deposit", NOW + 70, amount=D("50"), source="admin")
    rid_block = conn.execute(
        "SELECT id FROM partner_request WHERE partner_id=? AND status='pending'", (str(c["id"]),)
    ).fetchone()["id"]
    err_cli = FakeValueClient(wallet="9999")  # would be wrong if read
    _patch_read(monkeypatch, err_cli)
    before_calls = err_cli.calls
    answers, answer = _answer_capture()
    edited, edit = _edit_capture()
    pa.handle_partner_callback(
        conn,
        f"pa:dy:{rid_block}",
        callback=_owner_cb(data=f"pa:dy:{rid_block}"),
        send=_send_capture()[1],
        answer=answer,
        edit=edit,
        futures=err_cli,
        now_ms=NOW + 71,
    )
    assert "ค้างจ่าย" in (answers[0][1] or "")
    assert err_cli.calls == before_calls
    assert (
        conn.execute("SELECT status FROM partner_request WHERE id=?", (rid_block,)).fetchone()["status"]
        == "pending"
    )

    # Also pa:ry blocked
    ps.reject_request(conn, rid_block, NOW + 72)
    ps.create_request(conn, str(c["id"]), "convert", NOW + 73, source="admin")
    rid_ry = conn.execute(
        "SELECT id FROM partner_request WHERE partner_id=? AND status='pending'", (str(c["id"]),)
    ).fetchone()["id"]
    before_calls = err_cli.calls
    answers, answer = _answer_capture()
    pa.handle_partner_callback(
        conn,
        f"pa:ry:{rid_ry}",
        callback=_owner_cb(data=f"pa:ry:{rid_ry}"),
        send=_send_capture()[1],
        answer=answer,
        edit=_edit_capture()[1],
        futures=err_cli,
        now_ms=NOW + 74,
    )
    assert "ค้างจ่าย" in (answers[0][1] or "")
    assert err_cli.calls == before_calls

    # pa:pd
    answers, answer = _answer_capture()
    edited, edit = _edit_capture()
    sent, send = _send_capture()
    pa.handle_partner_callback(
        conn,
        f"pa:pd:{rid_cl}",
        callback=_owner_cb(data=f"pa:pd:{rid_cl}"),
        send=send,
        answer=answer,
        edit=edit,
        futures=cli_cl,
        now_ms=NOW + 80,
    )
    all_sends.extend(sent)
    all_edits.extend(edited)
    assert any(s.get("chat_id") == "501" and "โอน" in s["text"] for s in sent)
    assert ps.partner_by_telegram_id(conn, "501") is not None
    portal.reset_value_cache()
    card_sent, card_send = _send_capture()
    out = portal.handle_portal_text(
        conn, _pmsg("hi", chat=501), send=card_send, futures=cli_cl, now_ms=NOW + 81
    )
    assert out == {"ok": True, "cmd": "card"}
    assert any("ปิดพอร์ตแล้ว" in item["text"] for item in card_sent)
    assert any("กำลังแปลงเป็นเงินบาท" in item["text"] for item in card_sent)

    # At V 1300, C card shows 287.50 red
    cli_n = FakeValueClient(wallet="1300")
    _patch_read(monkeypatch, cli_n)
    portal.reset_value_cache()
    sent, send = _send_capture()
    portal.handle_portal_text(conn, _pmsg("p", chat=502), send=send, futures=cli_n, now_ms=NOW + 90)
    card = sent[0]["text"]
    assert "287.50" in card
    assert "🔴" in card
    assert ps.verify_ledger(conn)["ok"] is True
    for actor in ("501", "502"):
        actor_sends = [s for s in all_sends if s.get("chat_id") == actor]
        actor_edits = [e for e in all_edits if e.get("chat_id") == actor]
        _assert_privacy(actor_sends, actor_edits, actor=actor)
    admin_items = [s for s in all_sends + all_edits if "chat_id" not in s]
    for item in admin_items:
        markup = item.get("reply_markup")
        if markup:
            for row in markup.get("inline_keyboard") or []:
                for btn in row:
                    data = str(btn.get("callback_data") or "")
                    if data:
                        assert data.startswith("pa:")
    conn.close()


def test_request_idempotency_and_refusals(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "idem.db")
    _init_and_add(conn, monkeypatch)
    _bind(conn, "C", "502", now_ms=NOW + 5)
    # fund C
    ps.apply_deposit(
        conn,
        str(conn.execute("SELECT id FROM partner WHERE name='C'").fetchone()["id"]),
        D("330"),
        D("1330"),
        NOW + 6,
    )
    # bump units properly via store at V that matches — simpler: use dep flow
    # Actually apply_deposit with 330 at 1330 after admin 1000 → C gets 330 units at nav 1
    c_id = str(conn.execute("SELECT id FROM partner WHERE name='C'").fetchone()["id"])
    # Fix: after init admin has 1000 at V1000; deposit 330 at V1330 → 330 units
    assert ps.units_from_e8(int(ps.get_partner(conn, c_id)["units_e8"])) == D("330")

    cli = FakeValueClient(wallet="1500")
    _patch_read(monkeypatch, cli)
    portal.reset_value_cache()
    answers, answer = _answer_capture()
    edited, edit = _edit_capture()
    sent, send = _send_capture()
    portal.handle_portal_callback(
        conn,
        "pp:go:cv",
        callback=_pcb("pp:go:cv", chat=502),
        send=send,
        answer=answer,
        edit=edit,
        futures=cli,
        now_ms=NOW + 10,
    )
    n_req = int(
        conn.execute("SELECT COUNT(*) AS n FROM partner_request WHERE kind='convert'").fetchone()["n"]
    )
    answers2, answer2 = _answer_capture()
    portal.handle_portal_callback(
        conn,
        "pp:go:cv",
        callback=_pcb("pp:go:cv", chat=502),
        send=_send_capture()[1],
        answer=answer2,
        edit=_edit_capture()[1],
        futures=cli,
        now_ms=NOW + 11,
    )
    assert (
        int(conn.execute("SELECT COUNT(*) AS n FROM partner_request WHERE kind='convert'").fetchone()["n"])
        == n_req
    )
    assert "ค้างอยู่" in (answers2[0][1] or "")

    rid = conn.execute("SELECT id FROM partner_request WHERE status='pending'").fetchone()["id"]
    answers, answer = _answer_capture()
    edited, edit = _edit_capture()
    sent, send = _send_capture()
    pa.handle_partner_callback(
        conn,
        f"pa:ry:{rid}",
        callback=_owner_cb(data=f"pa:ry:{rid}"),
        send=send,
        answer=answer,
        edit=edit,
        futures=cli,
        now_ms=NOW + 12,
    )
    n_ev = int(conn.execute("SELECT COUNT(*) AS n FROM partner_event WHERE kind='convert'").fetchone()["n"])
    pa.handle_partner_callback(
        conn,
        f"pa:ry:{rid}",
        callback=_owner_cb(data=f"pa:ry:{rid}"),
        send=_send_capture()[1],
        answer=_answer_capture()[1],
        edit=_edit_capture()[1],
        futures=cli,
        now_ms=NOW + 13,
    )
    assert (
        int(conn.execute("SELECT COUNT(*) AS n FROM partner_event WHERE kind='convert'").fetchone()["n"])
        == n_ev
    )

    # forged buttons
    before = int(conn.execute("SELECT COUNT(*) AS n FROM partner_request").fetchone()["n"])
    for data in ("pp:go:zz", "pp:go:"):
        portal.handle_portal_callback(
            conn,
            data,
            callback=_pcb(data, chat=502),
            send=_send_capture()[1],
            answer=_answer_capture()[1],
            edit=_edit_capture()[1],
            futures=cli,
            now_ms=NOW + 14,
        )
    assert int(conn.execute("SELECT COUNT(*) AS n FROM partner_request").fetchone()["n"]) == before
    assert ps.verify_ledger(conn)["ok"] is True
    conn.close()


def test_pa_ry_no_profit_cancels(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "nprof.db")
    _init_and_add(conn, monkeypatch)
    _bind(conn, "C", "502", now_ms=NOW + 5)
    c_id = str(conn.execute("SELECT id FROM partner WHERE name='C'").fetchone()["id"])
    ps.apply_deposit(conn, c_id, D("330"), D("1330"), NOW + 6)
    # create convert while profitable
    cli_hi = FakeValueClient(wallet="1500")
    _patch_read(monkeypatch, cli_hi)
    portal.reset_value_cache()
    portal.handle_portal_callback(
        conn,
        "pp:go:cv",
        callback=_pcb("pp:go:cv", chat=502),
        send=_send_capture()[1],
        answer=_answer_capture()[1],
        edit=_edit_capture()[1],
        futures=cli_hi,
        now_ms=NOW + 10,
    )
    rid = conn.execute("SELECT id FROM partner_request WHERE status='pending'").fetchone()["id"]
    # confirm at loss V
    cli_lo = FakeValueClient(wallet="1200")
    _patch_read(monkeypatch, cli_lo)
    answers, answer = _answer_capture()
    edited, edit = _edit_capture()
    sent, send = _send_capture()
    pa.handle_partner_callback(
        conn,
        f"pa:ry:{rid}",
        callback=_owner_cb(data=f"pa:ry:{rid}"),
        send=send,
        answer=answer,
        edit=edit,
        futures=cli_lo,
        now_ms=NOW + 11,
    )
    assert "ยกเลิกอัตโนมัติ" in edited[0]["text"]
    assert any(s.get("chat_id") == "502" and "ถูกยกเลิก" in s["text"] for s in sent)
    assert (
        conn.execute("SELECT status FROM partner_request WHERE id=?", (rid,)).fetchone()["status"]
        == "cancelled"
    )
    assert ps.verify_ledger(conn)["ok"] is True
    conn.close()


def test_pa_ry_v_error_and_deposit_pending(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "verr.db")
    _init_and_add(conn, monkeypatch)
    _bind(conn, "C", "502", now_ms=NOW + 5)
    c_id = str(conn.execute("SELECT id FROM partner WHERE name='C'").fetchone()["id"])
    ps.apply_deposit(conn, c_id, D("330"), D("1330"), NOW + 6)
    cli = FakeValueClient(wallet="1500")
    _patch_read(monkeypatch, cli)
    portal.reset_value_cache()
    portal.handle_portal_callback(
        conn,
        "pp:go:cv",
        callback=_pcb("pp:go:cv", chat=502),
        send=_send_capture()[1],
        answer=_answer_capture()[1],
        edit=_edit_capture()[1],
        futures=cli,
        now_ms=NOW + 10,
    )
    rid = conn.execute("SELECT id FROM partner_request WHERE status='pending'").fetchone()["id"]
    err = FakeValueClient(error=AccountValueError(reason="read_failed"))
    _patch_read(monkeypatch, err)
    answers, answer = _answer_capture()
    pa.handle_partner_callback(
        conn,
        f"pa:ry:{rid}",
        callback=_owner_cb(data=f"pa:ry:{rid}"),
        send=_send_capture()[1],
        answer=answer,
        edit=_edit_capture()[1],
        futures=err,
        now_ms=NOW + 11,
    )
    assert answers[0][1]
    assert (
        conn.execute("SELECT status FROM partner_request WHERE id=?", (rid,)).fetchone()["status"]
        == "pending"
    )

    # deposit pending blocks convert confirm
    b_id = str(conn.execute("SELECT id FROM partner WHERE name='B'").fetchone()["id"])
    ps.create_request(conn, b_id, "deposit", NOW + 12, amount=D("50"), source="admin")
    _patch_read(monkeypatch, cli)
    answers, answer = _answer_capture()
    pa.handle_partner_callback(
        conn,
        f"pa:ry:{rid}",
        callback=_owner_cb(data=f"pa:ry:{rid}"),
        send=_send_capture()[1],
        answer=answer,
        edit=_edit_capture()[1],
        futures=cli,
        now_ms=NOW + 13,
    )
    assert "ฝากค้าง" in (answers[0][1] or "")
    assert (
        conn.execute("SELECT status FROM partner_request WHERE id=?", (rid,)).fetchone()["status"]
        == "pending"
    )
    conn.close()


def test_reject_and_paid_twice(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "rn.db")
    _init_and_add(conn, monkeypatch)
    _bind(conn, "C", "502", now_ms=NOW + 5)
    c_id = str(conn.execute("SELECT id FROM partner WHERE name='C'").fetchone()["id"])
    ps.apply_deposit(conn, c_id, D("330"), D("1330"), NOW + 6)
    cli = FakeValueClient(wallet="1500")
    _patch_read(monkeypatch, cli)
    portal.reset_value_cache()
    portal.handle_portal_callback(
        conn,
        "pp:go:wd",
        callback=_pcb("pp:go:wd", chat=502),
        send=_send_capture()[1],
        answer=_answer_capture()[1],
        edit=_edit_capture()[1],
        futures=cli,
        now_ms=NOW + 10,
    )
    rid = conn.execute("SELECT id FROM partner_request WHERE status='pending'").fetchone()["id"]
    answers, answer = _answer_capture()
    edited, edit = _edit_capture()
    sent, send = _send_capture()
    pa.handle_partner_callback(
        conn,
        f"pa:rn:{rid}",
        callback=_owner_cb(data=f"pa:rn:{rid}"),
        send=send,
        answer=answer,
        edit=edit,
        futures=cli,
        now_ms=NOW + 11,
    )
    assert edited[0]["text"] == "ปฏิเสธแล้ว"
    assert any(s.get("chat_id") == "502" and "ปฏิเสธ" in s["text"] for s in sent)

    # fresh close → due → paid twice
    portal.handle_portal_callback(
        conn,
        "pp:go:cl",
        callback=_pcb("pp:go:cl", chat=502),
        send=_send_capture()[1],
        answer=_answer_capture()[1],
        edit=_edit_capture()[1],
        futures=cli,
        now_ms=NOW + 20,
    )
    rid2 = conn.execute("SELECT id FROM partner_request WHERE status='pending'").fetchone()["id"]
    pa.handle_partner_callback(
        conn,
        f"pa:ry:{rid2}",
        callback=_owner_cb(data=f"pa:ry:{rid2}"),
        send=_send_capture()[1],
        answer=_answer_capture()[1],
        edit=_edit_capture()[1],
        futures=cli,
        now_ms=NOW + 21,
    )
    pa.handle_partner_callback(
        conn,
        f"pa:pd:{rid2}",
        callback=_owner_cb(data=f"pa:pd:{rid2}"),
        send=_send_capture()[1],
        answer=_answer_capture()[1],
        edit=_edit_capture()[1],
        futures=cli,
        now_ms=NOW + 22,
    )
    state = conn.execute("SELECT payout_state FROM partner_request WHERE id=?", (rid2,)).fetchone()[
        "payout_state"
    ]
    assert state == "paid"
    answers, answer = _answer_capture()
    pa.handle_partner_callback(
        conn,
        f"pa:pd:{rid2}",
        callback=_owner_cb(data=f"pa:pd:{rid2}"),
        send=_send_capture()[1],
        answer=answer,
        edit=_edit_capture()[1],
        futures=cli,
        now_ms=NOW + 23,
    )
    assert "ปิดไปแล้ว" in (answers[0][1] or "")
    assert ps.verify_ledger(conn)["ok"] is True
    conn.close()


def test_deposit_notify_amount_guards(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "dep_amt.db")
    _init_and_add(conn, monkeypatch)
    _bind(conn, "B", "501", now_ms=NOW + 5)
    cli = FakeValueClient(wallet="1000")
    _patch_read(monkeypatch, cli)
    portal.reset_value_cache()
    for text in ("ฝาก 49", "ฝาก ๕๐๐", "ฝาก 1e3"):
        sent, send = _send_capture()
        portal.handle_portal_text(conn, _pmsg(text, chat=501), send=send, futures=cli, now_ms=NOW + 10)
        assert "จำนวนไม่ถูกต้อง" in sent[0]["text"]
        assert int(conn.execute("SELECT COUNT(*) AS n FROM partner_request").fetchone()["n"]) == 0
    sent, send = _send_capture()
    portal.handle_portal_text(conn, _pmsg("ฝาก 500", chat=501), send=send, futures=cli, now_ms=NOW + 20)
    assert "แจ้งฝาก" in sent[0]["text"] or any("แจ้งฝาก" in s["text"] for s in sent)
    row = conn.execute("SELECT * FROM partner_request WHERE status='pending'").fetchone()
    assert row is not None
    assert ps.usdt_from_micro(int(row["quote_gross_micro"])) == D("500")
    assert str(row["kind"]) == "deposit"
    conn.close()


def test_admin_principal_withdrawal(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "admin_wd.db")
    _init_and_add(conn, monkeypatch)
    _bind(conn, "C", "502", now_ms=NOW + 5)
    c_id = str(conn.execute("SELECT id FROM partner WHERE name='C'").fetchone()["id"])
    ps.apply_deposit(conn, c_id, D("300"), D("1300"), NOW + 6)
    # V still 1300, admin cost 1000; withdraw 100 of principal via cost then wd
    cli = FakeValueClient(wallet="1300")
    _patch_read(monkeypatch, cli)
    net_before = Decimal(ps.view_partner(conn, c_id, D("1300"))["net"])
    sent, send = _send_capture()
    pa.handle_partner_text(
        conn,
        "p cost admin 900 reduce admin principal",
        message=_owner_msg("p cost admin 900 reduce admin principal"),
        send=send,
        futures=cli,
        now_ms=NOW + 10,
    )
    sent, send = _send_capture()
    pa.handle_partner_text(
        conn, "p wd admin", message=_owner_msg("p wd admin"), send=send, futures=cli, now_ms=NOW + 11
    )
    rid = [s for s in sent if s.get("reply_markup")][0]["reply_markup"]["inline_keyboard"][0][0][
        "callback_data"
    ].split(":")[-1]
    answers, answer = _answer_capture()
    edited, edit = _edit_capture()
    sent2, send2 = _send_capture()
    pa.handle_partner_callback(
        conn,
        f"pa:ry:{rid}",
        callback=_owner_cb(data=f"pa:ry:{rid}"),
        send=send2,
        answer=answer,
        edit=edit,
        futures=cli,
        now_ms=NOW + 12,
    )
    req = ps.get_request(conn, rid)
    assert req is not None
    assert ps.usdt_from_micro(int(req["payout_micro"])) == D("100")
    # fee 0: look at event
    ev = conn.execute("SELECT fee_micro FROM partner_event WHERE request_id=?", (rid,)).fetchone()
    assert int(ev["fee_micro"]) == 0
    net_after = Decimal(ps.view_partner(conn, c_id, D("1300"))["net"])
    assert abs(net_after - net_before) <= D("0.000001")
    assert ps.verify_ledger(conn)["ok"] is True
    conn.close()


def test_static_guards_allow_mark_paid() -> None:
    admin = (Path(__file__).resolve().parents[1] / "src" / "jarvise_assist" / "partner_admin.py").read_text(
        encoding="utf-8"
    )
    for banned in ("apply_convert", "apply_withdraw_profit", "apply_close"):
        assert banned not in admin
    # mark_paid is allowed in P3b
    assert "mark_paid" in admin


def _owner_cb_stale(data: str, text: str, *, mid: int = 7) -> dict[str, Any]:
    """Callback with a frozen message.text (Telegram snapshot at first tap)."""
    cb = _owner_cb(data, mid=mid)
    cb["message"] = dict(cb["message"])
    cb["message"]["text"] = text
    return cb


def _pd_callback(markup: dict[str, Any] | None) -> str | None:
    if not markup:
        return None
    for row in markup.get("inline_keyboard") or []:
        for btn in row:
            data = str(btn.get("callback_data") or "")
            if data.startswith("pa:pd:"):
                return data
    return None


def _net_line(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("ยอดสุทธิ "):
            return line
    raise AssertionError(f"no net line in {text!r}")


def test_double_tap_pa_ry_keeps_due_button(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "dt_ry.db")
    _init_and_add(conn, monkeypatch)
    _bind(conn, "C", "502", now_ms=NOW + 5)
    c_id = str(conn.execute("SELECT id FROM partner WHERE name='C'").fetchone()["id"])
    ps.apply_deposit(conn, c_id, D("330"), D("1330"), NOW + 6)
    cli = FakeValueClient(wallet="1500")
    _patch_read(monkeypatch, cli)
    portal.reset_value_cache()
    portal.handle_portal_callback(
        conn,
        "pp:go:wd",
        callback=_pcb("pp:go:wd", chat=502),
        send=_send_capture()[1],
        answer=_answer_capture()[1],
        edit=_edit_capture()[1],
        futures=cli,
        now_ms=NOW + 10,
    )
    rid = conn.execute("SELECT id FROM partner_request WHERE status='pending'").fetchone()["id"]
    stale = "📥 คำขอจาก C: ถอนกำไร\nประมาณการตอนนี้: เงินออก 0.00 · ค่าธรรมเนียม 0.00"
    cb = _owner_cb_stale(f"pa:ry:{rid}", stale)
    edited, edit = _edit_capture()
    pa.handle_partner_callback(
        conn,
        f"pa:ry:{rid}",
        callback=cb,
        send=_send_capture()[1],
        answer=_answer_capture()[1],
        edit=edit,
        futures=cli,
        now_ms=NOW + 11,
    )
    assert "ค้างจ่าย" in edited[0]["text"]
    assert _pd_callback(edited[0]["reply_markup"]) == f"pa:pd:{rid}"
    due_after = ps.payouts_due_total(conn)
    n_wd = int(
        conn.execute("SELECT COUNT(*) AS n FROM partner_event WHERE kind='withdraw_profit'").fetchone()["n"]
    )
    assert n_wd == 1

    edited2, edit2 = _edit_capture()
    answers2, answer2 = _answer_capture()
    pa.handle_partner_callback(
        conn,
        f"pa:ry:{rid}",
        callback=cb,
        send=_send_capture()[1],
        answer=answer2,
        edit=edit2,
        futures=cli,
        now_ms=NOW + 12,
    )
    assert "ปิดไปแล้ว" in (answers2[0][1] or "")
    assert "ค้างจ่าย" in edited2[0]["text"]
    assert "📥" not in edited2[0]["text"]
    assert _pd_callback(edited2[0]["reply_markup"]) == f"pa:pd:{rid}"
    assert ps.payouts_due_total(conn) == due_after
    assert (
        int(
            conn.execute("SELECT COUNT(*) AS n FROM partner_event WHERE kind='withdraw_profit'").fetchone()[
                "n"
            ]
        )
        == 1
    )
    assert ps.verify_ledger(conn)["ok"] is True
    conn.close()


def test_double_tap_pa_pd_keeps_paid_text(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "dt_pd.db")
    _init_and_add(conn, monkeypatch)
    _bind(conn, "C", "502", now_ms=NOW + 5)
    c_id = str(conn.execute("SELECT id FROM partner WHERE name='C'").fetchone()["id"])
    ps.apply_deposit(conn, c_id, D("330"), D("1330"), NOW + 6)
    cli = FakeValueClient(wallet="1500")
    _patch_read(monkeypatch, cli)
    portal.reset_value_cache()
    portal.handle_portal_callback(
        conn,
        "pp:go:wd",
        callback=_pcb("pp:go:wd", chat=502),
        send=_send_capture()[1],
        answer=_answer_capture()[1],
        edit=_edit_capture()[1],
        futures=cli,
        now_ms=NOW + 10,
    )
    rid = conn.execute("SELECT id FROM partner_request WHERE status='pending'").fetchone()["id"]
    edited, edit = _edit_capture()
    pa.handle_partner_callback(
        conn,
        f"pa:ry:{rid}",
        callback=_owner_cb(data=f"pa:ry:{rid}"),
        send=_send_capture()[1],
        answer=_answer_capture()[1],
        edit=edit,
        futures=cli,
        now_ms=NOW + 11,
    )
    due_text = edited[0]["text"]
    assert "ค้างจ่าย" in due_text
    cb = _owner_cb_stale(f"pa:pd:{rid}", due_text)
    edited1, edit1 = _edit_capture()
    pa.handle_partner_callback(
        conn,
        f"pa:pd:{rid}",
        callback=cb,
        send=_send_capture()[1],
        answer=_answer_capture()[1],
        edit=edit1,
        futures=cli,
        now_ms=NOW + 12,
    )
    assert "USDT ออกจากพอร์ตแล้ว" in edited1[0]["text"]
    assert (edited1[0]["reply_markup"] or {}).get("inline_keyboard") == []

    edited2, edit2 = _edit_capture()
    answers2, answer2 = _answer_capture()
    pa.handle_partner_callback(
        conn,
        f"pa:pd:{rid}",
        callback=cb,
        send=_send_capture()[1],
        answer=answer2,
        edit=edit2,
        futures=cli,
        now_ms=NOW + 13,
    )
    assert "ปิดไปแล้ว" in (answers2[0][1] or "")
    assert "USDT ออกจากพอร์ตแล้ว" in edited2[0]["text"]
    assert "ค้างจ่าย" not in edited2[0]["text"]
    assert (edited2[0]["reply_markup"] or {}).get("inline_keyboard") == []
    assert (
        conn.execute("SELECT payout_state FROM partner_request WHERE id=?", (rid,)).fetchone()["payout_state"]
        == "paid"
    )
    assert ps.verify_ledger(conn)["ok"] is True
    conn.close()


def test_double_tap_pa_dy_keeps_deposit_text(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "dt_dy.db")
    _init_and_add(conn, monkeypatch)
    _bind(conn, "B", "501", now_ms=NOW + 5)
    cli = FakeValueClient(wallet="1600")
    _patch_read(monkeypatch, cli)
    portal.reset_value_cache()
    sent, send = _send_capture()
    portal.handle_portal_text(conn, _pmsg("ฝาก 600", chat=501), send=send, futures=cli, now_ms=NOW + 10)
    admin_dep = [s for s in sent if "chat_id" not in s and "แจ้งฝาก" in s["text"]][0]
    rid = admin_dep["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[-1]
    cb = _owner_cb_stale(f"pa:dy:{rid}", admin_dep["text"])
    edited, edit = _edit_capture()
    pa.handle_partner_callback(
        conn,
        f"pa:dy:{rid}",
        callback=cb,
        send=_send_capture()[1],
        answer=_answer_capture()[1],
        edit=edit,
        futures=cli,
        now_ms=NOW + 11,
    )
    assert "บันทึกฝากแล้ว" in edited[0]["text"]
    n_dep = int(conn.execute("SELECT COUNT(*) AS n FROM partner_event WHERE kind='deposit'").fetchone()["n"])
    # admin init deposit + B deposit
    assert n_dep == 2

    edited2, edit2 = _edit_capture()
    answers2, answer2 = _answer_capture()
    pa.handle_partner_callback(
        conn,
        f"pa:dy:{rid}",
        callback=cb,
        send=_send_capture()[1],
        answer=answer2,
        edit=edit2,
        futures=cli,
        now_ms=NOW + 12,
    )
    assert "ปิดไปแล้ว" in (answers2[0][1] or "")
    assert "บันทึกฝากแล้ว" in edited2[0]["text"]
    assert (
        int(conn.execute("SELECT COUNT(*) AS n FROM partner_event WHERE kind='deposit'").fetchone()["n"]) == 2
    )
    assert ps.verify_ledger(conn)["ok"] is True
    conn.close()


def test_pa_rn_after_pa_ry_keeps_pd_button(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "rn_after_ry.db")
    _init_and_add(conn, monkeypatch)
    _bind(conn, "C", "502", now_ms=NOW + 5)
    c_id = str(conn.execute("SELECT id FROM partner WHERE name='C'").fetchone()["id"])
    ps.apply_deposit(conn, c_id, D("330"), D("1330"), NOW + 6)
    cli = FakeValueClient(wallet="1500")
    _patch_read(monkeypatch, cli)
    portal.reset_value_cache()
    portal.handle_portal_callback(
        conn,
        "pp:go:wd",
        callback=_pcb("pp:go:wd", chat=502),
        send=_send_capture()[1],
        answer=_answer_capture()[1],
        edit=_edit_capture()[1],
        futures=cli,
        now_ms=NOW + 10,
    )
    rid = conn.execute("SELECT id FROM partner_request WHERE status='pending'").fetchone()["id"]
    edited, edit = _edit_capture()
    pa.handle_partner_callback(
        conn,
        f"pa:ry:{rid}",
        callback=_owner_cb(data=f"pa:ry:{rid}"),
        send=_send_capture()[1],
        answer=_answer_capture()[1],
        edit=edit,
        futures=cli,
        now_ms=NOW + 11,
    )
    assert _pd_callback(edited[0]["reply_markup"]) == f"pa:pd:{rid}"
    stale = "📥 คำขอจาก C: ถอนกำไร"
    answers, answer = _answer_capture()
    edited2, edit2 = _edit_capture()
    pa.handle_partner_callback(
        conn,
        f"pa:rn:{rid}",
        callback=_owner_cb_stale(f"pa:rn:{rid}", stale),
        send=_send_capture()[1],
        answer=answer,
        edit=edit2,
        futures=cli,
        now_ms=NOW + 12,
    )
    assert "ปิดไปแล้ว" in (answers[0][1] or "")
    assert "ค้างจ่าย" in edited2[0]["text"]
    assert _pd_callback(edited2[0]["reply_markup"]) == f"pa:pd:{rid}"
    assert ps.verify_ledger(conn)["ok"] is True
    conn.close()


def test_portal_v_cache_invalidates_when_due_clears(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "cache_due.db")
    _init_and_add(conn, monkeypatch)
    _bind(conn, "B", "501", now_ms=NOW + 5)
    _bind(conn, "C", "502", now_ms=NOW + 6)
    b_id = str(conn.execute("SELECT id FROM partner WHERE name='B'").fetchone()["id"])
    c_id = str(conn.execute("SELECT id FROM partner WHERE name='C'").fetchone()["id"])
    ps.apply_deposit(conn, b_id, D("600"), D("1600"), NOW + 7)
    ps.apply_deposit(conn, c_id, D("330"), D("1930"), NOW + 8)
    cli = FakeValueClient(wallet="2400")
    _patch_read(monkeypatch, cli)
    portal.reset_value_cache()
    portal.handle_portal_callback(
        conn,
        "pp:go:wd",
        callback=_pcb("pp:go:wd", chat=502),
        send=_send_capture()[1],
        answer=_answer_capture()[1],
        edit=_edit_capture()[1],
        futures=cli,
        now_ms=NOW + 20,
    )
    rid = conn.execute("SELECT id FROM partner_request WHERE status='pending'").fetchone()["id"]
    pa.handle_partner_callback(
        conn,
        f"pa:ry:{rid}",
        callback=_owner_cb(data=f"pa:ry:{rid}"),
        send=_send_capture()[1],
        answer=_answer_capture()[1],
        edit=_edit_capture()[1],
        futures=cli,
        now_ms=NOW + 21,
    )
    x = ps.payouts_due_total(conn)
    assert x > 0
    portal.reset_value_cache()
    cli.calls = 0
    sent, send = _send_capture()
    portal.handle_portal_text(conn, _pmsg("hi", chat=501), send=send, futures=cli, now_ms=NOW + 30)
    assert cli.calls == 1
    net_before = _net_line(sent[0]["text"])

    cli.wallet = str(D("2400") - x)
    pa.handle_partner_callback(
        conn,
        f"pa:pd:{rid}",
        callback=_owner_cb(data=f"pa:pd:{rid}"),
        send=_send_capture()[1],
        answer=_answer_capture()[1],
        edit=_edit_capture()[1],
        futures=cli,
        now_ms=NOW + 31,
    )
    assert ps.payouts_due_total(conn) == 0
    sent2, send2 = _send_capture()
    portal.handle_portal_text(conn, _pmsg("hi", chat=501), send=send2, futures=cli, now_ms=NOW + 30 + 4000)
    assert cli.calls == 2
    assert _net_line(sent2[0]["text"]) == net_before
    assert ps.verify_ledger(conn)["ok"] is True
    conn.close()
