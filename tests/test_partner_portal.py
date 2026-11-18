"""Partner portal: bind, private card, isolation (P3a)."""

from __future__ import annotations

import re
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
    """P5 guard is covered in test_partner_guard.py; keep legacy confirms clean."""
    from jarvise_assist.partner_audit import GuardResult

    def _fake(conn: Any, client: Any, *, now_ms: int, req: Any) -> tuple[GuardResult, str]:
        return (
            GuardResult(status="ok", checked_at_ms=int(now_ms), findings=[], warn=False, error=None),
            "",
        )

    monkeypatch.setattr(pa, "_transfer_guard", _fake)


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
        u = self.unrealized
        if D(u) == 0:
            return []
        return [
            {
                "symbol": "BTCUSDT",
                "positionAmt": "1",
                "unRealizedProfit": u,
                "marginAsset": "USDT",
            }
        ]


def _patch_read(monkeypatch: Any, client: FakeValueClient) -> None:
    def fake(cli: Any, *, now_ms: int, attempts: int = 3) -> AccountValue | AccountValueError:
        if isinstance(cli, FakeValueClient) and cli.error is not None:
            return cli.error
        if isinstance(cli, FakeValueClient) and cli.wallet is not None:
            w = D(cli.wallet)
            u = D(cli.unrealized)
            open_n = 0 if u == 0 else 1
            return AccountValue(wallet=w, unrealized=u, value=w + u, open_positions=open_n, read_at_ms=now_ms)
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


def _msg(
    text: str,
    *,
    chat: int = 501,
    from_id: int | None = None,
    chat_type: str = "private",
) -> dict[str, Any]:
    fid = from_id if from_id is not None else chat
    return {
        "message_id": 1,
        "chat": {"id": chat, "type": chat_type},
        "from": {"id": fid, "first_name": "Bob", "username": "bob"},
        "text": text,
    }


def _cb(
    data: str,
    *,
    chat: int = 501,
    from_id: int | None = None,
    chat_type: str = "private",
    mid: int = 7,
    text: str = "card",
) -> dict[str, Any]:
    fid = from_id if from_id is not None else chat
    return {
        "id": "cq1",
        "data": data,
        "from": {"id": fid},
        "message": {
            "message_id": mid,
            "chat": {"id": chat, "type": chat_type},
            "text": text,
        },
    }


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


def _init_abc(conn: Any, monkeypatch: Any) -> FakeValueClient:
    """Init at 1000, add B/C, deposit B 600@1800, C 330@1980 → A/B/C units 1000/500/300."""
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
    for name, amount, wallet, t in (
        ("B", "600", "1800", NOW + 10),
        ("C", "330", "1980", NOW + 20),
    ):
        cli = FakeValueClient(wallet=wallet)
        _patch_read(monkeypatch, cli)
        sent, send = _send_capture()
        pa.handle_partner_text(
            conn,
            f"p dep {name} {amount}",
            message=_owner_msg(f"p dep {name} {amount}"),
            send=send,
            futures=cli,
            now_ms=t,
        )
        rid = sent[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[-1]
        answers, answer = _answer_capture()
        edited, edit = _edit_capture()
        pa.handle_partner_callback(
            conn,
            f"pa:dy:{rid}",
            callback=_owner_cb(data=f"pa:dy:{rid}"),
            send=_send_capture()[1],
            answer=answer,
            edit=edit,
            futures=cli,
            now_ms=t + 1,
        )
    return FakeValueClient(wallet="2160")


def _invite_bind(conn: Any, name: str, tg: str, *, now_ms: int) -> str:
    sent, send = _send_capture()
    pa.handle_partner_text(
        conn,
        f"p invite {name}",
        message=_owner_msg(f"p invite {name}"),
        send=send,
        futures=FakeValueClient(wallet="2160"),
        now_ms=now_ms,
    )
    text = sent[0]["text"]
    # extract /start <code>
    code = ""
    for line in text.splitlines():
        if line.startswith("/start "):
            code = line.split(None, 1)[1].strip()
            break
    assert code
    out = ps.bind_invite(conn, code, tg, admin_chat_id=ADMIN, now_ms=now_ms + 1)
    assert out["ok"]
    return code


@pytest.fixture
def env_chat(monkeypatch: Any) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", ADMIN)


def test_unbound_private_silent(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "ub.db")
    portal.reset_value_cache()
    sent, send = _send_capture()
    for text in ("p", "hi", "/start"):
        out = portal.handle_portal_text(conn, _msg(text), send=send, now_ms=NOW)
        assert out == {"ignored": "foreign_chat"}
    assert sent == []
    conn.close()


def test_unbound_bad_start_one_send(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "badstart.db")
    portal.reset_value_cache()
    code = "x" * 32
    sent, send = _send_capture()
    out = portal.handle_portal_text(conn, _msg(f"/start {code}"), send=send, now_ms=NOW)
    assert out.get("ok") is False or "invite" in str(out.get("reason", "invite"))
    assert len(sent) == 1
    assert sent[0]["chat_id"] == "501"
    assert "ลิงก์เชิญไม่ถูกต้อง" in sent[0]["text"]
    assert "แอดมิน" in sent[0]["text"]
    conn.close()


def test_group_and_mismatched_from_ignored(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "group.db")
    _init_abc(conn, monkeypatch)
    _invite_bind(conn, "B", "501", now_ms=NOW + 50)
    sent, send = _send_capture()
    out_g = portal.handle_portal_text(
        conn, _msg("p", chat=501, chat_type="group"), send=send, now_ms=NOW + 60
    )
    out_m = portal.handle_portal_text(conn, _msg("p", chat=501, from_id=999), send=send, now_ms=NOW + 61)
    assert out_g == {"ignored": "foreign_chat"}
    assert out_m == {"ignored": "foreign_chat"}
    assert sent == []
    conn.close()


def test_bind_ok_partner_and_admin_notice(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "bindok.db")
    _init_abc(conn, monkeypatch)
    sent_inv, send_inv = _send_capture()
    pa.handle_partner_text(
        conn,
        "p invite B",
        message=_owner_msg("p invite B"),
        send=send_inv,
        futures=FakeValueClient(wallet="2160"),
        now_ms=NOW + 50,
    )
    code = ""
    for line in sent_inv[0]["text"].splitlines():
        if line.startswith("/start "):
            code = line.split(None, 1)[1].strip()
    cli = FakeValueClient(wallet="2160")
    _patch_read(monkeypatch, cli)
    portal.reset_value_cache()
    sent, send = _send_capture()
    out = portal.handle_portal_text(
        conn,
        _msg(f"/start {code}", chat=501),
        send=send,
        futures=cli,
        now_ms=NOW + 51,
    )
    assert out.get("ok") is True
    partner_sends = [s for s in sent if s.get("chat_id") == "501"]
    admin_sends = [s for s in sent if "chat_id" not in s]
    assert partner_sends
    assert any("ผูกบัญชีแล้ว" in s["text"] for s in partner_sends)
    assert admin_sends
    assert "🔗" in admin_sends[0]["text"]
    assert "501" not in admin_sends[0]["text"]
    assert ps.verify_ledger(conn)["ok"] is True
    conn.close()


def test_cards_at_v_2160_privacy(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "card.db")
    _init_abc(conn, monkeypatch)
    _invite_bind(conn, "B", "501", now_ms=NOW + 50)
    _invite_bind(conn, "C", "502", now_ms=NOW + 60)
    cli = FakeValueClient(wallet="2160")
    _patch_read(monkeypatch, cli)
    portal.reset_value_cache()

    sent_c, send_c = _send_capture()
    portal.handle_portal_text(conn, _msg("p", chat=502), send=send_c, futures=cli, now_ms=NOW + 70)
    card_c = sent_c[0]["text"]
    assert "345.00" in card_c
    assert "🟢 +15.00 (+4.54%)" in card_c
    for bad in ("NAV", "หน่วย", "wallet", "ยังไม่ปิด", "2,160", "admin"):
        assert bad not in card_c
    assert "พอร์ตของ B" not in card_c
    assert not re.search(r"(?<![A-Za-z])B(?![A-Za-z])", card_c)

    portal.reset_value_cache()
    sent_b, send_b = _send_capture()
    portal.handle_portal_text(conn, _msg("p", chat=501), send=send_b, futures=cli, now_ms=NOW + 71)
    card_b = sent_b[0]["text"]
    assert "600.00" in card_b
    assert "⚪" in card_b
    for bad in ("NAV", "หน่วย", "wallet", "ยังไม่ปิด", "2,160", "admin"):
        assert bad not in card_b
    assert "พอร์ตของ C" not in card_b
    assert not re.search(r"(?<![A-Za-z])C(?![A-Za-z])", card_b)
    assert ps.verify_ledger(conn)["ok"] is True
    conn.close()


def test_v_error_card_no_reason(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "verr.db")
    _init_abc(conn, monkeypatch)
    _invite_bind(conn, "B", "501", now_ms=NOW + 50)
    err = FakeValueClient(error=AccountValueError(reason="read_failed", detail="TimeoutError"))
    _patch_read(monkeypatch, err)
    portal.reset_value_cache()
    sent, send = _send_capture()
    portal.handle_portal_text(conn, _msg("p", chat=501), send=send, futures=err, now_ms=NOW + 60)
    text = sent[0]["text"]
    assert "อ่านยอดไม่ได้" in text
    assert "TimeoutError" not in text
    assert "read_failed" not in text
    assert "ทุน" in text
    conn.close()


def test_value_cache_30s(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "cache.db")
    _init_abc(conn, monkeypatch)
    _invite_bind(conn, "B", "501", now_ms=NOW + 50)
    cli = FakeValueClient(wallet="2160")
    reads = {"n": 0}

    def fake(cli2: Any, *, now_ms: int, attempts: int = 3) -> AccountValue:
        reads["n"] += 1
        return AccountValue(
            wallet=D("2160"), unrealized=D("0"), value=D("2160"), open_positions=0, read_at_ms=now_ms
        )

    monkeypatch.setattr(portal, "read_account_value", fake)
    portal.reset_value_cache()
    sent, send = _send_capture()
    portal.handle_portal_text(conn, _msg("p", chat=501), send=send, futures=cli, now_ms=NOW + 100)
    portal.handle_portal_text(conn, _msg("p", chat=501), send=send, futures=cli, now_ms=NOW + 100 + 10_000)
    assert reads["n"] == 1
    portal.handle_portal_text(conn, _msg("p", chat=501), send=send, futures=cli, now_ms=NOW + 100 + 31_000)
    assert reads["n"] == 2
    conn.close()


def test_bound_adminish_text_only_card(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "onlycard.db")
    _init_abc(conn, monkeypatch)
    _invite_bind(conn, "B", "501", now_ms=NOW + 50)
    cli = FakeValueClient(wallet="2160")
    _patch_read(monkeypatch, cli)
    portal.reset_value_cache()
    ev0 = int(conn.execute("SELECT COUNT(*) AS n FROM partner_event").fetchone()["n"])
    rq0 = int(conn.execute("SELECT COUNT(*) AS n FROM partner_request").fetchone()["n"])
    for text in ("p", "p init", "p dep C 100", "q BTC long 1%"):
        portal.reset_value_cache()
        sent, send = _send_capture()
        portal.handle_portal_text(conn, _msg(text, chat=501), send=send, futures=cli, now_ms=NOW + 70)
        assert len(sent) == 1
        assert sent[0].get("chat_id") == "501"
        assert "พอร์ตของ" in sent[0]["text"] or "ทุน" in sent[0]["text"]
    assert int(conn.execute("SELECT COUNT(*) AS n FROM partner_event").fetchone()["n"]) == ev0
    assert int(conn.execute("SELECT COUNT(*) AS n FROM partner_request").fetchone()["n"]) == rq0
    assert ps.verify_ledger(conn)["ok"] is True
    conn.close()


def test_callback_edit_passes_partner_chat_id(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "cb.db")
    _init_abc(conn, monkeypatch)
    _invite_bind(conn, "B", "501", now_ms=NOW + 50)
    cli = FakeValueClient(wallet="2160")
    _patch_read(monkeypatch, cli)
    portal.reset_value_cache()
    edited, edit = _edit_capture()
    answers, answer = _answer_capture()
    portal.handle_portal_callback(
        conn,
        "pp:v",
        callback=_cb("pp:v", chat=501),
        send=_send_capture()[1],
        answer=answer,
        edit=edit,
        futures=cli,
        now_ms=NOW + 70,
    )
    assert len(answers) == 1
    assert edited
    assert edited[0]["chat_id"] == "501"
    conn.close()


def test_pp_v_unchanged_no_duplicate(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "ppv.db")
    _init_abc(conn, monkeypatch)
    _invite_bind(conn, "B", "501", now_ms=NOW + 50)
    cli = FakeValueClient(wallet="2160")
    _patch_read(monkeypatch, cli)
    portal.reset_value_cache()
    t0 = NOW + 70

    edited1, edit1 = _edit_capture()
    sent1, send1 = _send_capture()
    answers1, answer1 = _answer_capture()
    out1 = portal.handle_portal_callback(
        conn,
        "pp:v",
        callback=_cb("pp:v", chat=501),
        send=send1,
        answer=answer1,
        edit=edit1,
        futures=cli,
        now_ms=t0,
    )
    assert out1 == {"ok": True, "cmd": "refresh"}
    assert len(edited1) == 1
    assert sent1 == []
    first_text = edited1[0]["text"]

    edited2, edit2 = _edit_capture()
    sent2, send2 = _send_capture()
    answers2, answer2 = _answer_capture()
    out2 = portal.handle_portal_callback(
        conn,
        "pp:v",
        callback=_cb("pp:v", chat=501, text=first_text),
        send=send2,
        answer=answer2,
        edit=edit2,
        futures=cli,
        now_ms=t0,
    )
    assert out2.get("unchanged") is True
    assert out2 == {"ok": True, "cmd": "refresh", "unchanged": True}
    assert edited2 == []
    assert sent2 == []
    assert len(answers2) == 1
    conn.close()


def test_static_guards_portal() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "jarvise_assist"
    text = (root / "partner_portal.py").read_text(encoding="utf-8")
    assert "float(" not in text
    banned = (
        "listen",
        "execute",
        "signals",
        "quick",
        "watch",
        "sizing",
        "menu",
        "pause",
        "handle_partner_text",
        "handle_partner_callback",
        "HELP_TEXT",
        "confirm_request",
        "reject_request",
        "mark_paid",
        "apply_",
        "create_partner",
        "create_invite",
        "unbind_partner",
        "partner_audit",
    )
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("from ") or stripped.startswith("import "):
            for bad in banned:
                if bad == "apply_":
                    assert "apply_" not in stripped
                elif bad in ("quick", "watch", "menu", "pause", "execute", "signals", "listen"):
                    assert f"jarvise_assist.{bad}" not in stripped
                    assert f"import {bad}" not in stripped
                else:
                    assert bad not in stripped
    for ch in text:
        o = ord(ch)
        assert not (0x0900 <= o <= 0x0DFF)
        assert not (0x0E80 <= o <= 0x0EFF)
        assert not (0x1000 <= o <= 0x109F)
        assert not (0x1780 <= o <= 0x17FF)


def test_deposit_without_kyc_says_not_to_transfer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_TEST_NO_KYC_SEED", "1")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", ADMIN)
    conn = open_db(tmp_path / "kyc.db")
    admin = ps.create_partner(conn, name="Admin", is_admin=True, now_ms=NOW)
    bob = ps.create_partner(conn, name="Bob", now_ms=NOW)
    assert ps.apply_deposit(conn, str(admin["partner_id"]), D("1000"), D("1000"), NOW)["ok"]
    invited = ps.create_invite(conn, str(bob["partner_id"]), now_ms=NOW)
    assert ps.bind_invite(conn, invited["code"], "501", admin_chat_id=ADMIN, now_ms=NOW)["ok"]
    sent, send = _send_capture()
    portal.handle_portal_text(conn, _msg("ฝาก 100"), send=send, now_ms=NOW + 1)
    assert any("อย่าโอนเงินก่อน" in row["text"] for row in sent)
    conn.close()
