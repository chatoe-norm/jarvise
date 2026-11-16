"""Admin-only partner Telegram commands (P2)."""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from jarvise_assist import partner_admin as pa
from jarvise_assist import partner_store as ps
from jarvise_assist import partner_value
from jarvise_assist.partner_value import AccountValue, AccountValueError
from jarvise_ingest.db import open_db

D = Decimal
NOW = 1_700_000_000_000


class FakeValueClient:
    """Scripted client: balance_rows / position_rows for a fixed V, or an error."""

    def __init__(
        self,
        *,
        wallet: str | None = None,
        unrealized: str = "0",
        error: AccountValueError | None = None,
        positions: list[dict[str, Any]] | None = None,
    ) -> None:
        self.error = error
        self.wallet = wallet
        self.unrealized = unrealized
        self.positions = positions or []
        self.calls = 0

    def balance_rows(self) -> list[dict[str, Any]]:
        self.calls += 1
        if self.error is not None:
            raise RuntimeError("should_not_hit_rows")
        assert self.wallet is not None
        return [{"asset": "USDT", "balance": self.wallet}]

    def position_rows(self) -> list[dict[str, Any]]:
        if self.error is not None:
            raise RuntimeError("should_not_hit_rows")
        if self.positions:
            return self.positions
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
    """Route read_account_value through a fixed AccountValue or error (bypass bracket)."""

    def fake(cli: Any, *, now_ms: int, attempts: int = 3) -> AccountValue | AccountValueError:
        if cli is None:
            return AccountValueError(reason="no_client")
        if isinstance(cli, FakeValueClient) and cli.error is not None:
            return cli.error
        if isinstance(cli, FakeValueClient) and cli.wallet is not None:
            w = D(cli.wallet)
            u = D(cli.unrealized)
            open_n = (
                0
                if u == 0 and not cli.positions
                else max(
                    1,
                    len([p for p in cli.positions if D(str(p.get("positionAmt") or "0")) != 0])
                    or (1 if u != 0 else 0),
                )
            )
            if cli.positions:
                open_n = sum(1 for p in cli.positions if D(str(p.get("positionAmt") or "0")) != 0)
                u = sum(
                    (
                        D(str(p.get("unRealizedProfit") or "0"))
                        for p in cli.positions
                        if D(str(p.get("positionAmt") or "0")) != 0
                    ),
                    D("0"),
                )
            return AccountValue(wallet=w, unrealized=u, value=w + u, open_positions=open_n, read_at_ms=now_ms)
        return AccountValueError(reason="no_client")

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


def _owner_msg(
    *, text: str = "p", chat_type: str = "private", chat_id: int = 42, from_id: int = 42
) -> dict[str, Any]:
    return {
        "message_id": 1,
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": from_id},
        "text": text,
    }


def _owner_cb(
    *, data: str, chat_type: str = "private", chat_id: int = 42, from_id: int = 42, mid: int = 7
) -> dict[str, Any]:
    return {
        "id": "cq1",
        "data": data,
        "from": {"id": from_id},
        "message": {"message_id": mid, "chat": {"id": chat_id, "type": chat_type}, "text": "preview"},
    }


def _n_events(conn: sqlite3.Connection, *, kind: str | None = None) -> int:
    if kind is None:
        row = conn.execute("SELECT COUNT(*) AS n FROM partner_event").fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) AS n FROM partner_event WHERE kind = ?", (kind,)).fetchone()
    return int(row["n"])


def _n_partners(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM partner").fetchone()["n"])


@pytest.fixture
def env_chat(monkeypatch: Any) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")


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


# ---- parser ------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("p", {"cmd": "overview"}),
        ("P", {"cmd": "overview"}),
        ("  p  ", {"cmd": "overview"}),
        ("p help", {"cmd": "help"}),
        ("p init", {"cmd": "init"}),
        ("p add Bob", {"cmd": "add", "name": "Bob"}),
        ("p add สมชาย", {"cmd": "add", "name": "สมชาย"}),
        ("p add a_b-1.x", {"cmd": "add", "name": "a_b-1.x"}),
        ("p dep Bob 100", {"cmd": "dep", "name": "Bob", "amount": D("100")}),
        ("p dep Bob 1,234.5", {"cmd": "dep", "name": "Bob", "amount": D("1234.5")}),
        ("p dep Bob 0.000001", {"cmd": "dep", "name": "Bob", "amount": D("0.000001")}),
        (
            "p cost Bob 100 reason long enough",
            {"cmd": "cost", "name": "Bob", "new_cost": D("100"), "reason": "reason long enough"},
        ),
        (
            "p cost Bob 0 adjust cost now",
            {"cmd": "cost", "name": "Bob", "new_cost": D("0"), "reason": "adjust cost now"},
        ),
        ("p foo", {"error": "usage"}),
        ("p add", {"error": "usage"}),
        ("p dep Bob", {"error": "usage"}),
        (
            "p cost Bob 1 short",
            {"cmd": "cost", "name": "Bob", "new_cost": D("1"), "reason": "short"},
        ),
        ("hello", None),
        ("price", None),
        ("padd Bob", None),
    ],
)
def test_parse_partner_command(text: str, expected: dict[str, Any] | None) -> None:
    out = pa.parse_partner_command(text)
    assert out == expected


@pytest.mark.parametrize(
    "text",
    [
        "p add ",
        "p add this_name_is_way_too_long_xx",
        "p add Bob!",
        "p dep Bob 1e3",
        "p dep Bob NaN",
        "p dep Bob Infinity",
        "p dep Bob -1",
        "p dep Bob 1.1234567",
        "p dep Bob 0",
        "p cost Bob -1 reason long enough",
        "p cost Bob 1e3 reason long enough",
    ],
)
def test_parse_rejects(text: str) -> None:
    out = pa.parse_partner_command(text)
    assert out == {"error": "usage"}


# ---- formatters --------------------------------------------------------------


@pytest.mark.parametrize(
    "val,expected",
    [
        (D("-0.004"), "0.00"),
        (D("1234.567"), "1,234.56"),
        (D("0"), "0.00"),
        (D("-0.004"), "0.00"),
        (D("1"), "1.00"),
    ],
)
def test_fmt_usdt(val: Decimal, expected: str) -> None:
    assert pa.fmt_usdt(val) == expected


@pytest.mark.parametrize(
    "val,expected",
    [
        (D("15"), "+15.00"),
        (D("-100"), "-100.00"),
        (D("0"), "0.00"),
        (D("-0.004"), "0.00"),
    ],
)
def test_fmt_signed_usdt(val: Decimal, expected: str) -> None:
    assert pa.fmt_signed_usdt(val) == expected


def test_fmt_pct_and_nav_units() -> None:
    assert pa.fmt_pct(D("1000"), D("1800")) == "55.55%"
    assert pa.fmt_pct(D("15"), D("330")) == "4.54%"
    assert pa.fmt_pct(D("-10"), D("100")) == "-10.00%"
    assert pa.fmt_pct(D("1"), D("0")) == "—"
    assert pa.fmt_nav(D("1.2")) == "1.200000"
    assert pa.fmt_nav(D("1")) == "1.000000"
    assert pa.fmt_units(D("1000")) == "1,000.0000"
    assert pa.fmt_units(D("500.12345")) == "500.1234"


# ---- gate --------------------------------------------------------------------


def test_gate_group_chat_no_db_change(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "gate.db")
    sent, send = _send_capture()
    msg = _owner_msg(text="p init", chat_type="group")
    out = pa.handle_partner_text(conn, "p init", message=msg, send=send, now_ms=NOW)
    assert out.get("ok") is False or out.get("refused") == "not_private"
    assert len(sent) == 1
    assert "แชทส่วนตัว" in sent[0]["text"]
    assert _n_partners(conn) == 0
    assert _n_events(conn) == 0
    for call in sent:
        assert "chat_id" not in call
    conn.close()


def test_gate_wrong_from_no_db_change(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "gate2.db")
    sent, send = _send_capture()
    msg = _owner_msg(text="p init", from_id=99)
    pa.handle_partner_text(conn, "p init", message=msg, send=send, now_ms=NOW)
    assert len(sent) == 1
    assert _n_partners(conn) == 0
    conn.close()


# ---- init --------------------------------------------------------------------


def test_init_preview_writes_nothing(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "init_prev.db")
    client = FakeValueClient(wallet="1000", unrealized="0")
    _patch_read(monkeypatch, client)
    sent, send = _send_capture()
    pa.handle_partner_text(
        conn,
        "p init",
        message=_owner_msg(text="p init"),
        send=send,
        futures=client,
        now_ms=NOW,
    )
    assert _n_partners(conn) == 0
    assert _n_events(conn) == 0
    assert len(sent) == 1
    text = sent[0]["text"]
    assert "1,000.00" in text
    assert "NAV 1.000000" in text
    assert "แอดมินถือ 100%" in text
    kb = sent[0]["reply_markup"]["inline_keyboard"]
    assert kb[0][0]["callback_data"] == "pa:iy"
    assert kb[0][1]["callback_data"] == "pa:in"
    conn.close()


def test_init_confirm_and_idempotent(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "init_ok.db")
    client = FakeValueClient(wallet="1000")
    _patch_read(monkeypatch, client)
    edited, edit = _edit_capture()
    answers, answer = _answer_capture()
    out = pa.handle_partner_callback(
        conn,
        "pa:iy",
        callback=_owner_cb(data="pa:iy"),
        send=_send_capture()[1],
        answer=answer,
        edit=edit,
        futures=client,
        now_ms=NOW,
    )
    assert out.get("ok") is True
    assert _n_partners(conn) == 1
    assert _n_events(conn, kind="deposit") == 1
    admin = ps.admin_partner(conn)
    assert admin is not None
    assert str(admin["name"]) == "admin"
    assert int(admin["fee_rate_bps"]) == 0
    view = ps.view_partner(conn, str(admin["id"]), D("1000"))
    assert view["nav"] == D("1")
    assert view["units"] == D("1000")
    assert len(answers) == 1
    assert "1.000000" in edited[0]["text"]
    assert edited[0]["reply_markup"] == {"inline_keyboard": []}

    n_before = _n_events(conn)
    answers2, answer2 = _answer_capture()
    edited2, edit2 = _edit_capture()
    pa.handle_partner_callback(
        conn,
        "pa:iy",
        callback=_owner_cb(data="pa:iy"),
        send=_send_capture()[1],
        answer=answer2,
        edit=edit2,
        futures=client,
        now_ms=NOW + 1,
    )
    assert _n_events(conn) == n_before
    assert answers2[0][1] == "ตั้งต้นไปแล้ว"
    conn.close()


def test_init_v_error_no_rows(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "init_err.db")
    client = FakeValueClient(error=AccountValueError(reason="wallet_moving"))
    _patch_read(monkeypatch, client)
    edited, edit = _edit_capture()
    answers, answer = _answer_capture()
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
    assert _n_partners(conn) == 0
    assert _n_events(conn) == 0
    assert edited == []
    assert answers[0][1]  # toast with reason
    conn.close()


def test_init_cancel(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "init_no.db")
    edited, edit = _edit_capture()
    answers, answer = _answer_capture()
    pa.handle_partner_callback(
        conn,
        "pa:in",
        callback=_owner_cb(data="pa:in"),
        send=_send_capture()[1],
        answer=answer,
        edit=edit,
        futures=FakeValueClient(wallet="1"),
        now_ms=NOW,
    )
    assert edited[0]["text"] == "ยกเลิกแล้ว"
    assert _n_events(conn) == 0
    conn.close()


# ---- add ---------------------------------------------------------------------


def _do_init(conn: sqlite3.Connection, monkeypatch: Any, wallet: str = "1000") -> FakeValueClient:
    client = FakeValueClient(wallet=wallet)
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
    return client


def test_add_before_and_after_init(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "add.db")
    sent, send = _send_capture()
    client = FakeValueClient(wallet="1000")
    _patch_read(monkeypatch, client)
    pa.handle_partner_text(
        conn,
        "p add B",
        message=_owner_msg(text="p add B"),
        send=send,
        futures=client,
        now_ms=NOW,
    )
    assert "ยังไม่ได้ตั้งต้น" in sent[0]["text"] or "ตั้งต้น" in sent[0]["text"]
    assert _n_partners(conn) == 0

    _do_init(conn, monkeypatch)
    sent2, send2 = _send_capture()
    pa.handle_partner_text(
        conn,
        "p add B",
        message=_owner_msg(text="p add B"),
        send=send2,
        futures=FakeValueClient(wallet="1000"),
        now_ms=NOW + 10,
    )
    assert "เพิ่ม B แล้ว" in sent2[0]["text"]
    assert "50%" in sent2[0]["text"]
    row = conn.execute("SELECT fee_rate_bps FROM partner WHERE name = 'B'").fetchone()
    assert int(row["fee_rate_bps"]) == 5000

    sent3, send3 = _send_capture()
    pa.handle_partner_text(
        conn,
        "p add b",
        message=_owner_msg(text="p add b"),
        send=send3,
        futures=FakeValueClient(wallet="1000"),
        now_ms=NOW + 20,
    )
    assert _n_partners(conn) == 2  # admin + B
    assert "b" not in {r["name"] for r in ps.list_partners(conn)} or any(
        "มีอยู่" in sent3[0]["text"] or "ซ้ำ" in sent3[0]["text"] or "แล้ว" in sent3[0]["text"] for _ in [0]
    )
    # only one non-admin
    assert sum(1 for r in ps.list_partners(conn) if not r["is_admin"]) == 1
    conn.close()


# ---- deposit flow ------------------------------------------------------------


def _confirm_dep(
    conn: sqlite3.Connection,
    rid: str,
    client: FakeValueClient,
    monkeypatch: Any,
    *,
    now_ms: int,
) -> list[dict[str, Any]]:
    _patch_read(monkeypatch, client)
    edited, edit = _edit_capture()
    answers, answer = _answer_capture()
    pa.handle_partner_callback(
        conn,
        f"pa:dy:{rid}",
        callback=_owner_cb(data=f"pa:dy:{rid}"),
        send=_send_capture()[1],
        answer=answer,
        edit=edit,
        futures=client,
        now_ms=now_ms,
    )
    return edited


def test_dep_flow_b_then_c_units(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "dep.db")
    _do_init(conn, monkeypatch, wallet="1000")
    # rename admin display? keep name admin; tests refer to A/B/C conceptually
    sent, send = _send_capture()
    client = FakeValueClient(wallet="1000")
    _patch_read(monkeypatch, client)
    pa.handle_partner_text(
        conn, "p add B", message=_owner_msg(text="p add B"), send=send, futures=client, now_ms=NOW + 10
    )
    pa.handle_partner_text(
        conn, "p add C", message=_owner_msg(text="p add C"), send=send, futures=client, now_ms=NOW + 11
    )

    # B deposits 600 at V 1800
    client_b = FakeValueClient(wallet="1800")
    _patch_read(monkeypatch, client_b)
    sent_b, send_b = _send_capture()
    pa.handle_partner_text(
        conn,
        "p dep B 600",
        message=_owner_msg(text="p dep B 600"),
        send=send_b,
        futures=client_b,
        now_ms=NOW + 20,
    )
    assert "600.00" in sent_b[0]["text"] or "600" in sent_b[0]["text"]
    assert "ต้องเข้า futures wallet แล้ว" in sent_b[0]["text"]
    assert "ตัวเลขจริงคิดใหม่ตอนกดยืนยัน" in sent_b[0]["text"]
    rid_b = sent_b[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[-1]
    edited_b = _confirm_dep(conn, rid_b, client_b, monkeypatch, now_ms=NOW + 21)
    assert "✅ บันทึกฝากแล้ว" in edited_b[0]["text"]
    b = conn.execute("SELECT * FROM partner WHERE name = 'B'").fetchone()
    assert ps.units_from_e8(int(b["units_e8"])) == D("500")

    # C deposits 330 at V 1980
    client_c = FakeValueClient(wallet="1980")
    _patch_read(monkeypatch, client_c)
    sent_c, send_c = _send_capture()
    pa.handle_partner_text(
        conn,
        "p dep C 330",
        message=_owner_msg(text="p dep C 330"),
        send=send_c,
        futures=client_c,
        now_ms=NOW + 30,
    )
    rid_c = sent_c[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[-1]
    _confirm_dep(conn, rid_c, client_c, monkeypatch, now_ms=NOW + 31)
    c = conn.execute("SELECT * FROM partner WHERE name = 'C'").fetchone()
    assert ps.units_from_e8(int(c["units_e8"])) == D("300")

    assert ps.verify_ledger(conn)["ok"] is True
    conn.close()


def test_dep_idempotent_request_and_confirm(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "dep_id.db")
    _do_init(conn, monkeypatch)
    client = FakeValueClient(wallet="1000")
    _patch_read(monkeypatch, client)
    sent, send = _send_capture()
    pa.handle_partner_text(
        conn, "p add B", message=_owner_msg(text="p add B"), send=send, futures=client, now_ms=NOW + 1
    )

    client_v = FakeValueClient(wallet="1800")
    _patch_read(monkeypatch, client_v)
    sent1, send1 = _send_capture()
    pa.handle_partner_text(
        conn,
        "p dep B 600",
        message=_owner_msg(text="p dep B 600"),
        send=send1,
        futures=client_v,
        now_ms=NOW + 2,
    )
    sent2, send2 = _send_capture()
    pa.handle_partner_text(
        conn,
        "p dep B 600",
        message=_owner_msg(text="p dep B 600"),
        send=send2,
        futures=client_v,
        now_ms=NOW + 3,
    )
    assert "ค้างอยู่" in sent2[0]["text"]
    n_req = int(
        conn.execute("SELECT COUNT(*) AS n FROM partner_request WHERE kind='deposit'").fetchone()["n"]
    )
    assert n_req == 1
    rid = conn.execute("SELECT id FROM partner_request WHERE status='pending'").fetchone()["id"]

    _confirm_dep(conn, rid, client_v, monkeypatch, now_ms=NOW + 4)
    assert _n_events(conn, kind="deposit") == 2  # init + B
    _confirm_dep(conn, rid, client_v, monkeypatch, now_ms=NOW + 5)
    # second confirm: toast only path — edit may clear buttons or not change event count
    assert _n_events(conn, kind="deposit") == 2
    assert ps.verify_ledger(conn)["ok"] is True
    conn.close()


def test_dep_reject_then_confirm_no_event(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "dep_rej.db")
    _do_init(conn, monkeypatch)
    client = FakeValueClient(wallet="1000")
    _patch_read(monkeypatch, client)
    sent, send = _send_capture()
    pa.handle_partner_text(
        conn, "p add B", message=_owner_msg(text="p add B"), send=send, futures=client, now_ms=NOW + 1
    )
    client_v = FakeValueClient(wallet="1800")
    _patch_read(monkeypatch, client_v)
    sent1, send1 = _send_capture()
    pa.handle_partner_text(
        conn,
        "p dep B 600",
        message=_owner_msg(text="p dep B 600"),
        send=send1,
        futures=client_v,
        now_ms=NOW + 2,
    )
    rid = sent1[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[-1]
    edited, edit = _edit_capture()
    answers, answer = _answer_capture()
    pa.handle_partner_callback(
        conn,
        f"pa:dn:{rid}",
        callback=_owner_cb(data=f"pa:dn:{rid}"),
        send=_send_capture()[1],
        answer=answer,
        edit=edit,
        futures=client_v,
        now_ms=NOW + 3,
    )
    assert edited[0]["text"] == "ยกเลิกแล้ว"
    before = _n_events(conn)
    _confirm_dep(conn, rid, client_v, monkeypatch, now_ms=NOW + 4)
    assert _n_events(conn) == before
    conn.close()


def test_dep_v_error_keeps_pending(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "dep_verr.db")
    _do_init(conn, monkeypatch)
    client = FakeValueClient(wallet="1000")
    _patch_read(monkeypatch, client)
    sent, send = _send_capture()
    pa.handle_partner_text(
        conn, "p add B", message=_owner_msg(text="p add B"), send=send, futures=client, now_ms=NOW + 1
    )
    client_v = FakeValueClient(wallet="1800")
    _patch_read(monkeypatch, client_v)
    sent1, send1 = _send_capture()
    pa.handle_partner_text(
        conn,
        "p dep B 600",
        message=_owner_msg(text="p dep B 600"),
        send=send1,
        futures=client_v,
        now_ms=NOW + 2,
    )
    rid = sent1[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[-1]
    err_client = FakeValueClient(error=AccountValueError(reason="read_failed", detail="TimeoutError"))
    _patch_read(monkeypatch, err_client)
    edited, edit = _edit_capture()
    answers, answer = _answer_capture()
    before = _n_events(conn)
    pa.handle_partner_callback(
        conn,
        f"pa:dy:{rid}",
        callback=_owner_cb(data=f"pa:dy:{rid}"),
        send=_send_capture()[1],
        answer=answer,
        edit=edit,
        futures=err_client,
        now_ms=NOW + 3,
    )
    assert _n_events(conn) == before
    row = conn.execute("SELECT status FROM partner_request WHERE id = ?", (rid,)).fetchone()
    assert row["status"] == "pending"
    assert edited == []  # buttons stay
    conn.close()


def test_dep_v_below_amount_and_min(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "dep_low.db")
    _do_init(conn, monkeypatch)
    client = FakeValueClient(wallet="1000")
    _patch_read(monkeypatch, client)
    sent, send = _send_capture()
    pa.handle_partner_text(
        conn, "p add B", message=_owner_msg(text="p add B"), send=send, futures=client, now_ms=NOW + 1
    )

    low_v = FakeValueClient(wallet="500")
    _patch_read(monkeypatch, low_v)
    sent2, send2 = _send_capture()
    pa.handle_partner_text(
        conn, "p dep B 600", message=_owner_msg(text="p dep B 600"), send=send2, futures=low_v, now_ms=NOW + 2
    )
    assert "ยอดพอร์ตน้อยกว่าเงินฝาก" in sent2[0]["text"]
    assert int(conn.execute("SELECT COUNT(*) AS n FROM partner_request").fetchone()["n"]) == 0

    ok_v = FakeValueClient(wallet="1800")
    _patch_read(monkeypatch, ok_v)
    sent3, send3 = _send_capture()
    pa.handle_partner_text(
        conn, "p dep B 49", message=_owner_msg(text="p dep B 49"), send=send3, futures=ok_v, now_ms=NOW + 3
    )
    assert int(conn.execute("SELECT COUNT(*) AS n FROM partner_request").fetchone()["n"]) == 0
    conn.close()


def test_dep_nav_warning_line(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "dep_warn.db")
    _do_init(conn, monkeypatch)  # NAV 1.0
    client = FakeValueClient(wallet="1000")
    _patch_read(monkeypatch, client)
    sent, send = _send_capture()
    pa.handle_partner_text(
        conn, "p add B", message=_owner_msg(text="p add B"), send=send, futures=client, now_ms=NOW + 1
    )

    # NAV_pre = (850-50)/1000 = 0.80 → |0.80/1 - 1| = 0.20 > 0.10 → warn
    # Use amount 50 at V 850 → nav_pre = 0.8
    warn_v = FakeValueClient(wallet="850")
    _patch_read(monkeypatch, warn_v)
    sent_w, send_w = _send_capture()
    pa.handle_partner_text(
        conn, "p dep B 50", message=_owner_msg(text="p dep B 50"), send=send_w, futures=warn_v, now_ms=NOW + 2
    )
    assert "⚠️" in sent_w[0]["text"]
    # reject so we can try again
    rid = sent_w[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[-1]
    ps.reject_request(conn, rid, NOW + 3)

    # nav_pre at 0.95x: V=1000, amount=50 → nav_pre=0.95; |0.95/1-1|=0.05 ≤ 0.10 → no warn
    ok_v = FakeValueClient(wallet="1000")
    _patch_read(monkeypatch, ok_v)
    sent_ok, send_ok = _send_capture()
    pa.handle_partner_text(
        conn, "p dep B 50", message=_owner_msg(text="p dep B 50"), send=send_ok, futures=ok_v, now_ms=NOW + 4
    )
    assert "⚠️" not in sent_ok[0]["text"]
    conn.close()


def test_dep_confirm_convert_refused(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "dep_cv.db")
    _do_init(conn, monkeypatch)
    admin = ps.admin_partner(conn)
    assert admin is not None
    req = ps.create_request(conn, str(admin["id"]), "convert", NOW + 10)
    assert req["ok"]
    rid = req["request_id"]
    client = FakeValueClient(wallet="1000")
    _patch_read(monkeypatch, client)
    answers, answer = _answer_capture()
    edited, edit = _edit_capture()
    pa.handle_partner_callback(
        conn,
        f"pa:dy:{rid}",
        callback=_owner_cb(data=f"pa:dy:{rid}"),
        send=_send_capture()[1],
        answer=answer,
        edit=edit,
        futures=client,
        now_ms=NOW + 11,
    )
    assert answers[0][1] == "ไม่ใช่รายการฝาก"
    row = conn.execute("SELECT status FROM partner_request WHERE id = ?", (rid,)).fetchone()
    assert row["status"] == "pending"
    assert _n_events(conn, kind="convert") == 0
    conn.close()


def test_dep_expired_allows_new_request(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "dep_exp_new.db")
    _do_init(conn, monkeypatch)
    client = FakeValueClient(wallet="1000")
    _patch_read(monkeypatch, client)
    sent, send = _send_capture()
    pa.handle_partner_text(
        conn, "p add B", message=_owner_msg(text="p add B"), send=send, futures=client, now_ms=NOW + 1
    )
    client_v = FakeValueClient(wallet="1100")
    _patch_read(monkeypatch, client_v)
    t0 = NOW + 2
    sent1, send1 = _send_capture()
    pa.handle_partner_text(
        conn,
        "p dep B 100",
        message=_owner_msg(text="p dep B 100"),
        send=send1,
        futures=client_v,
        now_ms=t0,
    )
    rid1 = sent1[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[-1]
    t1 = t0 + ps.REQUEST_TTL_MS + 1
    sent2, send2 = _send_capture()
    pa.handle_partner_text(
        conn,
        "p dep B 100",
        message=_owner_msg(text="p dep B 100"),
        send=send2,
        futures=client_v,
        now_ms=t1,
    )
    rid2 = sent2[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[-1]
    assert rid2 != rid1
    row1 = conn.execute("SELECT status FROM partner_request WHERE id = ?", (rid1,)).fetchone()
    assert row1["status"] == "expired"
    pending_n = int(
        conn.execute("SELECT COUNT(*) AS n FROM partner_request WHERE status = 'pending'").fetchone()["n"]
    )
    assert pending_n == 1
    assert ps.verify_ledger(conn)["ok"] is True
    conn.close()


def test_dep_confirm_expired_no_read_no_event(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "dep_exp_dy.db")
    _do_init(conn, monkeypatch)
    client = FakeValueClient(wallet="1000")
    _patch_read(monkeypatch, client)
    sent, send = _send_capture()
    pa.handle_partner_text(
        conn, "p add B", message=_owner_msg(text="p add B"), send=send, futures=client, now_ms=NOW + 1
    )
    client_v = FakeValueClient(wallet="1100")
    _patch_read(monkeypatch, client_v)
    t0 = NOW + 2
    sent1, send1 = _send_capture()
    pa.handle_partner_text(
        conn,
        "p dep B 100",
        message=_owner_msg(text="p dep B 100"),
        send=send1,
        futures=client_v,
        now_ms=t0,
    )
    rid = sent1[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[-1]
    read_calls = {"n": 0}
    real_read = pa.read_account_value

    def counting_read(*args: Any, **kwargs: Any) -> Any:
        read_calls["n"] += 1
        return real_read(*args, **kwargs)

    monkeypatch.setattr(pa, "read_account_value", counting_read)
    answers, answer = _answer_capture()
    edited, edit = _edit_capture()
    before_events = _n_events(conn)
    pa.handle_partner_callback(
        conn,
        f"pa:dy:{rid}",
        callback=_owner_cb(data=f"pa:dy:{rid}", mid=sent1[0]["message_id"]),
        send=_send_capture()[1],
        answer=answer,
        edit=edit,
        futures=client_v,
        now_ms=t0 + ps.REQUEST_TTL_MS + 1,
    )
    assert answers[0][1] is not None
    assert "หมดอายุ" in answers[0][1]
    assert read_calls["n"] == 0
    assert _n_events(conn) == before_events
    assert len(edited) == 1
    assert edited[0]["reply_markup"] == {"inline_keyboard": []}
    assert ps.verify_ledger(conn)["ok"] is True
    conn.close()


def test_admin_source_has_no_recorded_toast() -> None:
    text = (Path(__file__).resolve().parents[1] / "src" / "jarvise_assist" / "partner_admin.py").read_text(
        encoding="utf-8"
    )
    assert "บันทึกไปแล้ว" not in text


def test_overview_real_reader_no_usdt_row(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "ov_real.db")
    _do_init(conn, monkeypatch, wallet="1000")
    monkeypatch.setattr(pa, "read_account_value", partner_value.read_account_value)

    class NoUsdtClient:
        def balance_rows(self) -> list[dict[str, Any]]:
            return [{"asset": "BNB", "balance": "1"}]

        def position_rows(self) -> list[dict[str, Any]]:
            return []

    sent, send = _send_capture()
    pa.handle_partner_text(
        conn, "p", message=_owner_msg(text="p"), send=send, futures=NoUsdtClient(), now_ms=NOW + 10
    )
    text = sent[0]["text"]
    assert pa.thai_reason("no_usdt_row") in text
    assert "NAV" not in text
    assert "ค่าธรรมเนียมถ้าปิดทั้งหมด" not in text
    assert ps.verify_ledger(conn)["ok"] is True
    conn.close()


# ---- overview ----------------------------------------------------------------


def test_overview_abc_and_v_error(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "ov.db")
    _do_init(conn, monkeypatch, wallet="1000")
    client = FakeValueClient(wallet="1000")
    _patch_read(monkeypatch, client)
    sent, send = _send_capture()
    for name in ("B", "C"):
        pa.handle_partner_text(
            conn,
            f"p add {name}",
            message=_owner_msg(text=f"p add {name}"),
            send=send,
            futures=client,
            now_ms=NOW + 1,
        )

    client_b = FakeValueClient(wallet="1800")
    _patch_read(monkeypatch, client_b)
    sent_b, send_b = _send_capture()
    pa.handle_partner_text(
        conn,
        "p dep B 600",
        message=_owner_msg(text="p dep B 600"),
        send=send_b,
        futures=client_b,
        now_ms=NOW + 2,
    )
    rid_b = sent_b[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[-1]
    _confirm_dep(conn, rid_b, client_b, monkeypatch, now_ms=NOW + 3)

    client_c = FakeValueClient(wallet="1980")
    _patch_read(monkeypatch, client_c)
    sent_c, send_c = _send_capture()
    pa.handle_partner_text(
        conn,
        "p dep C 330",
        message=_owner_msg(text="p dep C 330"),
        send=send_c,
        futures=client_c,
        now_ms=NOW + 4,
    )
    rid_c = sent_c[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[-1]
    _confirm_dep(conn, rid_c, client_c, monkeypatch, now_ms=NOW + 5)

    ov_client = FakeValueClient(wallet="2160")
    _patch_read(monkeypatch, ov_client)
    sent_ov, send_ov = _send_capture()
    pa.handle_partner_text(
        conn, "p", message=_owner_msg(text="p"), send=send_ov, futures=ov_client, now_ms=NOW + 6
    )
    text = sent_ov[0]["text"]
    assert "2,160.00" in text
    assert "1.200000" in text
    assert "360.00" in text and "15.00" in text and "345.00" in text
    assert "🟢" in text and "+4.54%" in text
    assert "600.00" in text and "⚪" in text
    assert "15.00" in text  # total fee
    assert "✅ ตรงกัน" in text
    assert ps.verify_ledger(conn)["ok"] is True

    err = FakeValueClient(error=AccountValueError(reason="no_usdt_row"))
    _patch_read(monkeypatch, err)
    sent_e, send_e = _send_capture()
    pa.handle_partner_text(conn, "p", message=_owner_msg(text="p"), send=send_e, futures=err, now_ms=NOW + 7)
    et = sent_e[0]["text"]
    assert "NAV" not in et
    assert pa.thai_reason("no_usdt_row") in et
    assert "ค่าธรรมเนียมถ้าปิดทั้งหมด" not in et
    conn.close()


def test_overview_before_init(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "ov0.db")
    client = FakeValueClient(wallet="500")
    _patch_read(monkeypatch, client)
    sent, send = _send_capture()
    pa.handle_partner_text(conn, "p", message=_owner_msg(text="p"), send=send, futures=client, now_ms=NOW)
    assert "ยังไม่ได้ตั้งต้น" in sent[0]["text"]
    assert "p init" in sent[0]["text"]
    assert "500.00" in sent[0]["text"]
    conn.close()


# ---- cost --------------------------------------------------------------------


def test_cost_rules(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "cost.db")
    _do_init(conn, monkeypatch)
    client = FakeValueClient(wallet="1000")
    _patch_read(monkeypatch, client)
    sent, send = _send_capture()
    pa.handle_partner_text(
        conn, "p add B", message=_owner_msg(text="p add B"), send=send, futures=client, now_ms=NOW + 1
    )
    # give B some cost via deposit
    client_b = FakeValueClient(wallet="1600")
    _patch_read(monkeypatch, client_b)
    sent_b, send_b = _send_capture()
    pa.handle_partner_text(
        conn,
        "p dep B 600",
        message=_owner_msg(text="p dep B 600"),
        send=send_b,
        futures=client_b,
        now_ms=NOW + 2,
    )
    rid = sent_b[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[-1]
    _confirm_dep(conn, rid, client_b, monkeypatch, now_ms=NOW + 3)

    # reason under 10 characters → refused at handler
    sent_s, send_s = _send_capture()
    pa.handle_partner_text(
        conn,
        "p cost B 600 short!!!",
        message=_owner_msg(text="p cost B 600 short!!!"),
        send=send_s,
        futures=client_b,
        now_ms=NOW + 3,
    )
    assert "10" in sent_s[0]["text"] or "สั้น" in sent_s[0]["text"]

    # same cost → no event
    before = _n_events(conn)
    sent_same, send_same = _send_capture()
    pa.handle_partner_text(
        conn,
        "p cost B 600 same cost ok",
        message=_owner_msg(text="p cost B 600 same cost ok"),
        send=send_same,
        futures=client_b,
        now_ms=NOW + 4,
    )
    assert "ทุนเท่าเดิม" in sent_same[0]["text"]
    assert _n_events(conn) == before

    # valid change
    sent_ok, send_ok = _send_capture()
    pa.handle_partner_text(
        conn,
        "p cost B 550 adjust down",
        message=_owner_msg(text="p cost B 550 adjust down"),
        send=send_ok,
        futures=client_b,
        now_ms=NOW + 5,
    )
    assert _n_events(conn, kind="adjust_cost") == 1
    note = conn.execute("SELECT note FROM partner_event WHERE kind='adjust_cost'").fetchone()["note"]
    assert note == "adjust down"
    assert ps.verify_ledger(conn)["ok"] is True
    conn.close()


# ---- static guards -----------------------------------------------------------


def test_static_guards() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "jarvise_assist"
    for name in ("partner_admin.py", "partner_value.py"):
        text = (root / name).read_text(encoding="utf-8")
        assert "float(" not in text
        for bad in ("execute", "signals", "quick", "watch", "sizing", "menu", "pause"):
            # import lines only — allow words inside strings carefully
            for line in text.splitlines():
                if line.strip().startswith("from ") or line.strip().startswith("import "):
                    assert bad not in line.split() and f"jarvise_assist.{bad}" not in line
                    assert f"import {bad}" not in line
                    assert f".{bad}" not in line or bad in ("execute",) and "apply_" in line
        for ch in text:
            o = ord(ch)
            assert not (0x0900 <= o <= 0x0DFF)
            assert not (0x0E80 <= o <= 0x0EFF)
            assert not (0x1000 <= o <= 0x109F)
            assert not (0x1780 <= o <= 0x17FF)

    admin = (root / "partner_admin.py").read_text(encoding="utf-8")
    for banned in ("apply_convert", "apply_withdraw_profit", "apply_close"):
        assert banned not in admin


def test_no_chat_id_on_sends(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "noch.db")
    client = FakeValueClient(wallet="100")
    _patch_read(monkeypatch, client)
    calls: list[dict[str, Any]] = []

    def send(text: str, **kwargs: Any) -> int:
        calls.append({"text": text, **kwargs})
        return 1

    pa.handle_partner_text(
        conn, "p help", message=_owner_msg(text="p help"), send=send, futures=client, now_ms=NOW
    )
    for c in calls:
        assert "chat_id" not in c
    conn.close()


# ---- P3a polish + invite / unbind --------------------------------------------


def test_amount_re_rejects_thai_and_fullwidth_digits() -> None:
    assert pa.parse_amount("๕๐๐", allow_zero=False) is None
    assert pa.parse_amount("５００", allow_zero=False) is None
    assert pa.parse_amount("500", allow_zero=False) == D("500")


def test_value_th_no_usdt_row_text() -> None:
    assert pa.thai_reason("no_usdt_row") == "ไม่พบยอด USDT ใน futures wallet"
    admin = (Path(__file__).resolve().parents[1] / "src" / "jarvise_assist" / "partner_admin.py").read_text(
        encoding="utf-8"
    )
    assert "แถว" not in admin


def test_overview_and_init_signed_unrealized(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "signed_u.db")
    client = FakeValueClient(wallet="940", unrealized="60")
    _patch_read(monkeypatch, client)
    sent, send = _send_capture()
    pa.handle_partner_text(
        conn, "p init", message=_owner_msg(text="p init"), send=send, futures=client, now_ms=NOW
    )
    assert "ยังไม่ปิด +60.00" in sent[0]["text"]

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
        now_ms=NOW + 1,
    )
    sent2, send2 = _send_capture()
    pa.handle_partner_text(
        conn, "p", message=_owner_msg(text="p"), send=send2, futures=client, now_ms=NOW + 2
    )
    assert "ยังไม่ปิด +60.00" in sent2[0]["text"]
    conn.close()


def test_pa_dn_bad_id_no_db(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "dn_bad.db")
    _do_init(conn, monkeypatch)
    answers, answer = _answer_capture()
    edited, edit = _edit_capture()
    called: list[str] = []

    def boom(*a: Any, **k: Any) -> dict[str, Any]:
        called.append("reject")
        raise AssertionError("reject_request must not run for bad id")

    monkeypatch.setattr(ps, "reject_request", boom)
    pa.handle_partner_callback(
        conn,
        "pa:dn:xyz",
        callback=_owner_cb(data="pa:dn:xyz"),
        send=_send_capture()[1],
        answer=answer,
        edit=edit,
        futures=FakeValueClient(wallet="1000"),
        now_ms=NOW + 1,
    )
    assert answers[0][1] == "รายการนี้ปิดไปแล้ว"
    assert called == []
    assert edited == []
    conn.close()


def test_pa_dn_on_convert_refused(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "dn_cv.db")
    _do_init(conn, monkeypatch)
    admin = ps.admin_partner(conn)
    assert admin is not None
    req = ps.create_request(conn, str(admin["id"]), "convert", NOW + 10)
    rid = req["request_id"]
    answers, answer = _answer_capture()
    edited, edit = _edit_capture()
    pa.handle_partner_callback(
        conn,
        f"pa:dn:{rid}",
        callback=_owner_cb(data=f"pa:dn:{rid}"),
        send=_send_capture()[1],
        answer=answer,
        edit=edit,
        futures=FakeValueClient(wallet="1000"),
        now_ms=NOW + 11,
    )
    assert answers[0][1] == "ไม่ใช่รายการฝาก"
    assert (
        conn.execute("SELECT status FROM partner_request WHERE id=?", (rid,)).fetchone()["status"]
        == "pending"
    )
    conn.close()


def test_pa_dy_expired_after_newer_dep(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "dy_old.db")
    _do_init(conn, monkeypatch)
    client = FakeValueClient(wallet="1000")
    _patch_read(monkeypatch, client)
    sent, send = _send_capture()
    pa.handle_partner_text(
        conn, "p add B", message=_owner_msg(text="p add B"), send=send, futures=client, now_ms=NOW + 1
    )
    client_v = FakeValueClient(wallet="1100")
    _patch_read(monkeypatch, client_v)
    sent1, send1 = _send_capture()
    pa.handle_partner_text(
        conn,
        "p dep B 100",
        message=_owner_msg(text="p dep B 100"),
        send=send1,
        futures=client_v,
        now_ms=NOW + 2,
    )
    rid_old = sent1[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[-1]
    # expire old by creating new after TTL
    t1 = NOW + 2 + ps.REQUEST_TTL_MS + 1
    sent2, send2 = _send_capture()
    pa.handle_partner_text(
        conn, "p dep B 100", message=_owner_msg(text="p dep B 100"), send=send2, futures=client_v, now_ms=t1
    )
    answers, answer = _answer_capture()
    edited, edit = _edit_capture()
    pa.handle_partner_callback(
        conn,
        f"pa:dy:{rid_old}",
        callback=_owner_cb(data=f"pa:dy:{rid_old}", mid=sent1[0]["message_id"]),
        send=_send_capture()[1],
        answer=answer,
        edit=edit,
        futures=client_v,
        now_ms=t1 + 1,
    )
    assert answers[0][1] is not None
    assert "หมดอายุ" in answers[0][1]
    assert edited[0]["reply_markup"] == {"inline_keyboard": []}
    conn.close()


def test_handle_dep_pending_non_deposit(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "dep_cv_pending.db")
    _do_init(conn, monkeypatch)
    client = FakeValueClient(wallet="1000")
    _patch_read(monkeypatch, client)
    sent, send = _send_capture()
    pa.handle_partner_text(
        conn, "p add B", message=_owner_msg(text="p add B"), send=send, futures=client, now_ms=NOW + 1
    )
    b = conn.execute("SELECT id FROM partner WHERE name='B'").fetchone()
    ps.create_request(conn, str(b["id"]), "convert", NOW + 2)
    sent2, send2 = _send_capture()
    pa.handle_partner_text(
        conn,
        "p dep B 100",
        message=_owner_msg(text="p dep B 100"),
        send=send2,
        futures=client,
        now_ms=NOW + 3,
    )
    assert "มีคำขอ" in sent2[0]["text"] and "ค้างอยู่" in sent2[0]["text"]
    assert "ทบกำไร" in sent2[0]["text"]
    kb = sent2[0]["reply_markup"]["inline_keyboard"][0]
    assert kb[0]["callback_data"].startswith("pa:ry:")
    assert kb[1]["callback_data"].startswith("pa:rn:")
    assert int(conn.execute("SELECT COUNT(*) AS n FROM partner_request").fetchone()["n"]) == 1
    conn.close()


def test_invite_and_unbind(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "inv_cmd.db")
    _do_init(conn, monkeypatch)
    client = FakeValueClient(wallet="1000")
    _patch_read(monkeypatch, client)
    sent, send = _send_capture()
    pa.handle_partner_text(
        conn, "p add B", message=_owner_msg(text="p add B"), send=send, futures=client, now_ms=NOW + 1
    )
    sent_i, send_i = _send_capture()
    pa.handle_partner_text(
        conn, "p invite B", message=_owner_msg(text="p invite B"), send=send_i, futures=client, now_ms=NOW + 2
    )
    text = sent_i[0]["text"]
    assert "เชิญ B" in text
    assert "/start " in text
    assert "ใช้ได้ครั้งเดียว" in text
    code = ""
    for line in text.splitlines():
        if line.startswith("/start "):
            code = line.split(None, 1)[1].strip()
    assert ps.bind_invite(conn, code, "501", admin_chat_id="42", now_ms=NOW + 3)["ok"]

    sent_bound, send_bound = _send_capture()
    pa.handle_partner_text(
        conn,
        "p invite B",
        message=_owner_msg(text="p invite B"),
        send=send_bound,
        futures=client,
        now_ms=NOW + 4,
    )
    assert "ผูกอยู่แล้ว" in sent_bound[0]["text"]
    assert "p unbind" in sent_bound[0]["text"]

    sent_u, send_u = _send_capture()
    pa.handle_partner_text(
        conn, "p unbind B", message=_owner_msg(text="p unbind B"), send=send_u, futures=client, now_ms=NOW + 5
    )
    assert "ยกเลิกการผูก B แล้ว" in sent_u[0]["text"]
    assert (
        ps.get_partner(conn, str(conn.execute("SELECT id FROM partner WHERE name='B'").fetchone()["id"]))[
            "telegram_user_id"
        ]
        is None
    )

    # overview TG badge
    inv2 = ps.create_invite(
        conn, str(conn.execute("SELECT id FROM partner WHERE name='B'").fetchone()["id"]), now_ms=NOW + 6
    )
    ps.bind_invite(conn, inv2["code"], "501", admin_chat_id="42", now_ms=NOW + 7)
    sent_ov, send_ov = _send_capture()
    pa.handle_partner_text(
        conn, "p", message=_owner_msg(text="p"), send=send_ov, futures=client, now_ms=NOW + 8
    )
    assert "· B · TG ✅" in sent_ov[0]["text"]
    assert "p invite" in pa.HELP_TEXT and "p unbind" in pa.HELP_TEXT
    assert ps.verify_ledger(conn)["ok"] is True
    conn.close()


def test_cost_notifies_bound_partner(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "cost_n.db")
    _do_init(conn, monkeypatch)
    client = FakeValueClient(wallet="1000")
    _patch_read(monkeypatch, client)
    sent, send = _send_capture()
    pa.handle_partner_text(
        conn, "p add B", message=_owner_msg(text="p add B"), send=send, futures=client, now_ms=NOW + 1
    )
    # give B some cost via deposit
    client_v = FakeValueClient(wallet="1600")
    _patch_read(monkeypatch, client_v)
    sent_d, send_d = _send_capture()
    pa.handle_partner_text(
        conn,
        "p dep B 600",
        message=_owner_msg(text="p dep B 600"),
        send=send_d,
        futures=client_v,
        now_ms=NOW + 2,
    )
    rid = sent_d[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[-1]
    _confirm_dep(conn, rid, client_v, monkeypatch, now_ms=NOW + 3)
    b_id = str(conn.execute("SELECT id FROM partner WHERE name='B'").fetchone()["id"])
    inv = ps.create_invite(conn, b_id, now_ms=NOW + 4)
    ps.bind_invite(conn, inv["code"], "501", admin_chat_id="42", now_ms=NOW + 5)

    calls: list[dict[str, Any]] = []

    def send2(text: str, **kwargs: Any) -> int:
        calls.append({"text": text, **kwargs})
        return len(calls)

    pa.handle_partner_text(
        conn,
        "p cost B 550 adjust down",
        message=_owner_msg(text="p cost B 550 adjust down"),
        send=send2,
        futures=client_v,
        now_ms=NOW + 6,
    )
    partner_msgs = [c for c in calls if c.get("chat_id") == "501"]
    assert partner_msgs
    assert "แอดมินปรับทุน" in partner_msgs[0]["text"]
    assert "550" in partner_msgs[0]["text"]
    assert ps.verify_ledger(conn)["ok"] is True
    conn.close()
