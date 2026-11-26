"""B1: p owner dep|wd — match unrecorded Binance transfers and book owner flows."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from jarvise_assist import partner_admin as pa
from jarvise_assist import partner_store as ps
from jarvise_assist.partner_audit import Transfer
from jarvise_assist.partner_value import AccountValue
from jarvise_ingest.db import open_db

D = Decimal
NOW = 1_700_000_000_000
ADMIN = "42"
HOUR = 3_600_000


class FakeClient:
    def __init__(
        self,
        transfers: list[Transfer] | None = None,
        *,
        income: list[dict[str, Any]] | None = None,
        wallet: str = "1000",
        unrealized: str = "0",
    ) -> None:
        self.transfers = list(transfers or [])
        self.income = list(income or [])
        self.wallet = wallet
        self.unrealized = unrealized
        self.income_history_calls = 0

    def income_rows(self, **kwargs: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for t in self.transfers:
            if t.income_type != kwargs["income_type"]:
                continue
            if not (kwargs["start_ms"] <= t.time_ms <= kwargs["end_ms"]):
                continue
            out.append(
                {
                    "time": t.time_ms,
                    "income": str(t.amount),
                    "asset": t.asset,
                    "incomeType": t.income_type,
                    "tranId": t.tran_id,
                }
            )
        return out

    def income_history(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.income_history_calls += 1
        out: list[dict[str, Any]] = []
        for row in self.income:
            t = int(row["time"])
            if not (kwargs["start_ms"] <= t <= kwargs["end_ms"]):
                continue
            if kwargs.get("income_type") and row.get("incomeType") != kwargs["income_type"]:
                continue
            out.append(row)
        return out

    def balance_rows(self) -> list[dict[str, Any]]:
        return [{"asset": "USDT", "balance": self.wallet}]

    def position_rows(self) -> list[dict[str, Any]]:
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

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"unexpected method {name}")


def _xfer(*, time_ms: int, amount: str, tran_id: str) -> Transfer:
    return Transfer(
        time_ms=time_ms,
        amount=D(amount),
        asset="USDT",
        income_type="TRANSFER",
        tran_id=tran_id,
    )


def _owner_msg() -> dict[str, Any]:
    return {"chat": {"id": ADMIN, "type": "private"}, "from": {"id": ADMIN}}


def _owner_cb(data: str, mid: int = 1) -> dict[str, Any]:
    return {
        "id": "cb1",
        "from": {"id": ADMIN},
        "message": {"message_id": mid, "chat": {"id": ADMIN, "type": "private"}, "text": "preview"},
        "data": data,
    }


@pytest.fixture
def env_chat(monkeypatch: Any) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", ADMIN)


def _init(conn: Any, *, wallet: str = "1000", now_ms: int = NOW) -> str:
    admin = ps.create_partner(conn, name="admin", is_admin=True, now_ms=now_ms)
    pid = str(admin["partner_id"])
    assert ps.apply_deposit(conn, pid, D(wallet), D(wallet), now_ms)["ok"]
    return pid


def test_parse_owner_commands() -> None:
    assert pa.parse_partner_command("p owner dep 100") == {
        "cmd": "owner",
        "side": "dep",
        "amount": D("100"),
        "tran_id": None,
    }
    assert pa.parse_partner_command("p owner wd 50.5 12345") == {
        "cmd": "owner",
        "side": "wd",
        "amount": D("50.5"),
        "tran_id": "12345",
    }
    assert pa.parse_partner_command("p owner dep") == {"error": "usage"}
    assert pa.parse_partner_command("p owner foo 1") == {"error": "usage"}
    assert pa.parse_partner_command("p owner dep 0") == {"error": "usage"}
    assert "p owner dep" in pa.HELP_TEXT
    assert "p owner wd" in pa.HELP_TEXT
    assert "จัดประเภทใน L2" not in pa.HELP_TEXT


def test_owner_gate_refuses_non_private(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "gate.db")
    _init(conn)
    sent: list[str] = []

    def send(text: str, **_k: Any) -> None:
        sent.append(text)

    out = pa.handle_partner_text(
        conn,
        "p owner dep 100",
        message={"chat": {"id": "99", "type": "private"}, "from": {"id": "99"}},
        send=send,
        now_ms=NOW,
    )
    assert out["refused"] == "not_private"
    assert sent and "ส่วนตัว" in sent[0]
    conn.close()


def test_owner_preview_match_none_multi_and_confirm(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "match.db")
    t_init = NOW - 10 * HOUR
    _init(conn, wallet="400", now_ms=t_init)
    t0 = NOW - HOUR
    client = FakeClient(
        [_xfer(time_ms=t0, amount="100", tran_id="t_one")],
        wallet="500",
    )
    sent: list[dict[str, Any]] = []

    def send(text: str, **kwargs: Any) -> None:
        sent.append({"text": text, **kwargs})

    # none
    empty = FakeClient(wallet="400")
    out = pa.handle_partner_text(
        conn,
        "p owner dep 100",
        message=_owner_msg(),
        send=send,
        futures=empty,
        now_ms=NOW,
    )
    assert out["reason"] == "no_match"
    assert "ไม่พบเงินโอน" in sent[-1]["text"]

    # single match → preview
    sent.clear()
    out = pa.handle_partner_text(
        conn,
        "p owner dep 100",
        message=_owner_msg(),
        send=send,
        futures=client,
        now_ms=NOW,
    )
    assert out["ok"] is True
    assert out["tran_id"] == "t_one"
    assert "reply_markup" in sent[-1]
    cb = sent[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    assert pa._OWNER_CONFIRM_RE.match(cb)
    assert out.get("token")
    assert cb == f"pa:oy:{out['token']}"

    # confirm
    answers: list[str | None] = []

    def answer(_id: str, text: str | None = None) -> None:
        answers.append(text)

    edits: list[str] = []

    def edit(_mid: int, text: str, **_k: Any) -> bool:
        edits.append(text)
        return True

    n_before = conn.execute("SELECT COUNT(*) AS n FROM partner_event").fetchone()["n"]
    conf = pa.handle_partner_callback(
        conn,
        cb,
        callback=_owner_cb(cb),
        send=send,
        answer=answer,
        edit=edit,
        futures=client,
        now_ms=NOW,
    )
    assert conf["ok"] is True
    assert conf["cmd"] == "owner_confirm"
    row = conn.execute(
        "SELECT * FROM partner_event WHERE kind = 'owner_deposit' ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert str(row["id"]) == "pe_owner_trant_one"
    assert int(row["effective_at_ms"]) == t0
    assert "tranId t_one" in str(row["note"])
    assert int(conn.execute("SELECT COUNT(*) AS n FROM partner_event").fetchone()["n"]) == n_before + 1

    # idempotent second tap
    again = pa.handle_partner_callback(
        conn,
        cb,
        callback=_owner_cb(cb),
        send=send,
        answer=answer,
        edit=edit,
        futures=client,
        now_ms=NOW + 1,
    )
    assert again["reason"] == "already_recorded"
    assert int(conn.execute("SELECT COUNT(*) AS n FROM partner_event").fetchone()["n"]) == n_before + 1

    # multi requires tranId (fresh ledger — avoid group-matching a prior owner_deposit)
    multi_db = open_db(tmp_path / "multi.db")
    _init(multi_db, wallet="400", now_ms=t_init)
    multi = FakeClient(
        [
            _xfer(time_ms=t0, amount="50", tran_id="a1"),
            _xfer(time_ms=t0 + 1000, amount="50", tran_id="a2"),
        ],
        wallet="500",
    )
    sent.clear()
    out = pa.handle_partner_text(
        multi_db,
        "p owner dep 50",
        message=_owner_msg(),
        send=send,
        futures=multi,
        now_ms=NOW,
    )
    assert out["reason"] == "ambiguous"
    assert "ระบุ tranId" in sent[-1]["text"]
    sent.clear()
    # Selecting one while another remains unrecorded must refuse (no partial V book).
    out = pa.handle_partner_text(
        multi_db,
        "p owner dep 50 a2",
        message=_owner_msg(),
        send=send,
        futures=multi,
        now_ms=NOW,
    )
    assert out["reason"] == "unsafe"
    assert "ย้อนหลัง" in sent[-1]["text"] or "historical" in sent[-1]["text"]
    multi_db.close()
    conn.close()


def test_owner_wd_and_near_tol_and_refusals(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "wd.db")
    t_init = NOW - 10 * HOUR
    _init(conn, wallet="1000", now_ms=t_init)
    t0 = NOW - HOUR
    # near amount: 100.005 vs 100 within NEAR_ABS=1
    client = FakeClient(
        [_xfer(time_ms=t0, amount="-100.005", tran_id="out1")],
        wallet="899.995",
    )
    sent: list[dict[str, Any]] = []

    def send(text: str, **kwargs: Any) -> None:
        sent.append({"text": text, **kwargs})

    out = pa.handle_partner_text(
        conn,
        "p owner wd 100",
        message=_owner_msg(),
        send=send,
        futures=client,
        now_ms=NOW,
    )
    assert out["ok"] is True
    assert out["side"] == "wd"

    # stale > 6h
    stale_t = NOW - (6 * HOUR + 1)
    stale = FakeClient([_xfer(time_ms=stale_t, amount="10", tran_id="old")], wallet="1010")
    sent.clear()
    out = pa.handle_partner_text(
        conn,
        "p owner dep 10",
        message=_owner_msg(),
        send=send,
        futures=stale,
        now_ms=NOW,
    )
    assert out["reason"] == "unsafe"
    assert "6 ชม" in sent[-1]["text"]

    # income since transfer
    rich = FakeClient(
        [_xfer(time_ms=t0, amount="10", tran_id="inc1")],
        income=[
            {
                "time": t0 + 10,
                "income": "2.5",
                "asset": "USDT",
                "incomeType": "REALIZED_PNL",
                "tranId": "p1",
                "symbol": "BTCUSDT",
                "tradeId": "1",
            }
        ],
        wallet="1012.5",
    )
    sent.clear()
    out = pa.handle_partner_text(
        conn,
        "p owner dep 10",
        message=_owner_msg(),
        send=send,
        futures=rich,
        now_ms=NOW,
    )
    assert out["reason"] == "unsafe"
    assert "เทรดหลังเงินโอน" in sent[-1]["text"]

    # event after transfer
    later = FakeClient([_xfer(time_ms=t0, amount="10", tran_id="late1")], wallet="1010")
    ps.apply_owner_deposit(conn, amount=D("1"), account_value=D("1001"), now_ms=t0 + 5000)
    sent.clear()
    out = pa.handle_partner_text(
        conn,
        "p owner dep 10",
        message=_owner_msg(),
        send=send,
        futures=later,
        now_ms=NOW,
    )
    assert out["reason"] == "unsafe"
    assert "หลังเงินโอน" in sent[-1]["text"]

    # deposit pending
    pending_db = open_db(tmp_path / "pend.db")
    admin = _init(pending_db, wallet="500", now_ms=t_init)
    bob = ps.create_partner(pending_db, name="Bob", now_ms=t_init + 1)["partner_id"]
    assert ps.create_request(pending_db, bob, "deposit", NOW, amount=D("50"), source="admin")["ok"]
    pend_client = FakeClient([_xfer(time_ms=t0, amount="20", tran_id="pdep")], wallet="520")
    sent.clear()
    # Preview may succeed; confirm must refuse deposit_pending
    out = pa.handle_partner_text(
        pending_db,
        "p owner dep 20",
        message=_owner_msg(),
        send=send,
        futures=pend_client,
        now_ms=NOW,
    )
    assert out["ok"] is True
    cb = sent[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    answers: list[str | None] = []

    def answer(_id: str, text: str | None = None) -> None:
        answers.append(text)

    def edit(_mid: int, text: str, **_k: Any) -> bool:
        return True

    conf = pa.handle_partner_callback(
        pending_db,
        cb,
        callback=_owner_cb(cb),
        send=send,
        answer=answer,
        edit=edit,
        futures=pend_client,
        now_ms=NOW,
    )
    assert conf["ok"] is False
    assert conf["reason"] == "deposit_pending"
    pending_db.close()
    conn.close()
    _ = admin
    _ = AccountValue


def test_owner_refuses_multi_unrecorded_and_open_pos(tmp_path: Path, env_chat: None) -> None:
    """Codex repro: two unrecorded deposits must not mint from current V."""
    conn = open_db(tmp_path / "multi_u.db")
    t_init = NOW - 10 * HOUR
    _init(conn, wallet="400", now_ms=t_init)
    t0 = NOW - HOUR
    client = FakeClient(
        [
            _xfer(time_ms=t0, amount="100", tran_id="u_a"),
            _xfer(time_ms=t0 + 1000, amount="100", tran_id="u_b"),
        ],
        wallet="600",
    )
    sent: list[str] = []

    def send(text: str, **_k: Any) -> None:
        sent.append(text)

    out = pa.handle_partner_text(
        conn,
        "p owner dep 100 u_a",
        message=_owner_msg(),
        send=send,
        futures=client,
        now_ms=NOW,
    )
    assert out["reason"] == "unsafe"
    assert "historical one-off" in sent[-1] or "ย้อนหลัง" in sent[-1]
    assert (
        conn.execute("SELECT COUNT(*) AS n FROM partner_event WHERE kind = 'owner_deposit'").fetchone()["n"]
        == 0
    )
    # Never minted 80 units (V=600, NAV would falsely become 1.25)
    admin = ps.admin_partner(conn)
    assert admin is not None
    assert int(admin["units_e8"]) == 40_000_000_000

    # open position refuses even for a single match
    single_db = open_db(tmp_path / "open_pos.db")
    _init(single_db, wallet="400", now_ms=t_init)
    open_client = FakeClient(
        [_xfer(time_ms=t0, amount="100", tran_id="op1")],
        wallet="500",
        unrealized="5",
    )
    sent.clear()
    out2 = pa.handle_partner_text(
        single_db,
        "p owner dep 100",
        message=_owner_msg(),
        send=send,
        futures=open_client,
        now_ms=NOW,
    )
    assert out2["reason"] == "unsafe"
    assert "โพซิชัน" in sent[-1]
    single_db.close()
    conn.close()


def test_owner_confirm_ts_and_amount_integrity(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "ts.db")
    t_init = NOW - 10 * HOUR
    _init(conn, wallet="400", now_ms=t_init)
    t0 = NOW - HOUR
    client = FakeClient([_xfer(time_ms=t0, amount="100", tran_id="ts1")], wallet="500")
    sent: list[dict[str, Any]] = []

    def send(text: str, **kwargs: Any) -> None:
        sent.append({"text": text, **kwargs})

    def answer(_id: str, text: str | None = None) -> None:
        sent.append({"answer": text})

    def edit(_mid: int, text: str, **_k: Any) -> bool:
        sent.append({"edit": text})
        return True

    out = pa.handle_partner_text(
        conn,
        "p owner dep 100",
        message=_owner_msg(),
        send=send,
        futures=client,
        now_ms=NOW,
    )
    assert out["ok"] is True
    good_cb = sent[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    cancel_cb = sent[-1]["reply_markup"]["inline_keyboard"][0][1]["callback_data"]
    token = str(out["token"])

    # forged old-style callback (no token) must not book
    old = pa.handle_partner_callback(
        conn,
        "pa:oy:d:ts1:1000000000:100000000",
        callback=_owner_cb("x"),
        send=send,
        answer=answer,
        edit=edit,
        futures=client,
        now_ms=NOW,
    )
    assert old["reason"] == "stale_confirm"

    # never-issued token
    fake = "a" * 32
    fut = pa.handle_partner_callback(
        conn,
        f"pa:oy:{fake}",
        callback=_owner_cb("x"),
        send=send,
        answer=answer,
        edit=edit,
        futures=client,
        now_ms=NOW,
    )
    assert fut["reason"] == "stale_confirm"

    # amount mismatch vs live finding (token stores wrong micro)
    import json

    from jarvise_assist.partner_json_store import owner_preview_path_for

    path = owner_preview_path_for(conn)
    assert path is not None
    path.write_text(
        json.dumps(
            {
                "v": 1,
                "previews": {
                    token: {
                        "side": "dep",
                        "tran_id": "ts1",
                        "amount_micro": 999_000_000,
                        "issued_at_ms": NOW,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    mism = pa.handle_partner_callback(
        conn,
        good_cb,
        callback=_owner_cb("x"),
        send=send,
        answer=answer,
        edit=edit,
        futures=client,
        now_ms=NOW,
    )
    assert mism["reason"] == "amount_mismatch"

    # re-issue a clean preview for cancel
    sent.clear()
    out2 = pa.handle_partner_text(
        conn,
        "p owner dep 100",
        message=_owner_msg(),
        send=send,
        futures=client,
        now_ms=NOW + 1,
    )
    assert out2["ok"] is True
    cancel_cb = sent[-1]["reply_markup"]["inline_keyboard"][0][1]["callback_data"]
    confirm_cb = sent[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]

    # cancel revokes; later confirm is stale
    cancel = pa.handle_partner_callback(
        conn,
        cancel_cb,
        callback=_owner_cb("x"),
        send=send,
        answer=answer,
        edit=edit,
        futures=client,
        now_ms=NOW + 2,
    )
    assert cancel["cmd"] == "owner_cancel"
    after_cancel = pa.handle_partner_callback(
        conn,
        confirm_cb,
        callback=_owner_cb("x"),
        send=send,
        answer=answer,
        edit=edit,
        futures=client,
        now_ms=NOW + 3,
    )
    assert after_cancel["reason"] == "stale_confirm"
    assert (
        conn.execute("SELECT COUNT(*) AS n FROM partner_event WHERE kind = 'owner_deposit'").fetchone()["n"]
        == 0
    )
    conn.close()


def test_owner_idempotent_race_and_tran_prefix(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "race.db")
    admin = ps.create_partner(conn, name="admin", is_admin=True, now_ms=NOW)["partner_id"]
    assert ps.apply_deposit(conn, admin, D("400"), D("400"), NOW)["ok"]
    note = "owner tx via p owner; Binance TRANSFER tranId 100"
    # prefix must not match
    assert (
        ps.apply_owner_deposit(
            conn,
            amount=D("10"),
            account_value=D("410"),
            now_ms=NOW + 1,
            note="owner tx via p owner; Binance TRANSFER tranId 1001",
            event_id="pe_owner_tran1001",
        )["ok"]
        is True
    )
    assert not ps._note_has_exact_tran_id("tranId 1001", "100")
    assert ps._note_has_exact_tran_id("tranId 1001", "1001")
    assert not pa._owner_already_recorded(conn, side="dep", tran_id="100")

    # two apply calls with same event_id → one row
    r1 = ps.apply_owner_deposit(
        conn,
        amount=D("50"),
        account_value=D("460"),
        now_ms=NOW + 2,
        note=note,
        event_id="pe_owner_tran100",
    )
    assert r1["ok"] is True and r1.get("reason") != "already_recorded"
    r2 = ps.apply_owner_deposit(
        conn,
        amount=D("50"),
        account_value=D("460"),
        now_ms=NOW + 3,
        note=note,
        event_id="pe_owner_tran100",
    )
    assert r2.get("reason") == "already_recorded"
    n = conn.execute("SELECT COUNT(*) AS n FROM partner_event WHERE id = 'pe_owner_tran100'").fetchone()["n"]
    assert int(n) == 1

    # Two connections: second apply hits the real in-tx recheck (do not disable it)
    path = tmp_path / "race2.db"
    c1 = open_db(path)
    adm = ps.create_partner(c1, name="admin", is_admin=True, now_ms=NOW)["partner_id"]
    assert ps.apply_deposit(c1, adm, D("400"), D("400"), NOW)["ok"]
    c1.close()

    a = open_db(path)
    b = open_db(path)
    ra = ps.apply_owner_deposit(
        a,
        amount=D("25"),
        account_value=D("425"),
        now_ms=NOW + 10,
        note="owner tx via p owner; Binance TRANSFER tranId race",
        event_id="pe_owner_tranrace",
    )
    rb = ps.apply_owner_deposit(
        b,
        amount=D("25"),
        account_value=D("425"),
        now_ms=NOW + 11,
        note="owner tx via p owner; Binance TRANSFER tranId race",
        event_id="pe_owner_tranrace",
    )
    assert ra["ok"] is True and ra.get("reason") != "already_recorded"
    assert rb.get("reason") == "already_recorded"
    n_race = a.execute("SELECT COUNT(*) AS n FROM partner_event WHERE id = 'pe_owner_tranrace'").fetchone()[
        "n"
    ]
    assert int(n_race) == 1
    a.close()
    b.close()
    conn.close()


def test_owner_b1_clears_open_unrecorded_fingerprint(tmp_path: Path, env_chat: None) -> None:
    from jarvise_assist.partner_audit import load_state, save_state

    db = tmp_path / "b1fp.db"
    conn = open_db(db)
    t_init = NOW - 10 * HOUR
    _init(conn, wallet="400", now_ms=t_init)
    t0 = NOW - HOUR
    client = FakeClient([_xfer(time_ms=t0, amount="100", tran_id="fp1")], wallet="500")
    save_state(
        db.with_name("partner_audit_state.json"),
        {
            "v": 1,
            "alerted": {},
            "open_unrecorded": ["bx:TRANSFER:fp1", "bx:TRANSFER:keep"],
            "last_ok_ms": None,
            "fail_since_ms": None,
            "fail_alerted": False,
        },
    )
    sent: list[dict[str, Any]] = []

    def send(text: str, **kwargs: Any) -> None:
        sent.append({"text": text, **kwargs})

    def answer(_id: str, text: str | None = None) -> None:
        return None

    def edit(_mid: int, text: str, **_k: Any) -> bool:
        return True

    out = pa.handle_partner_text(
        conn,
        "p owner dep 100",
        message=_owner_msg(),
        send=send,
        futures=client,
        now_ms=NOW,
    )
    assert out["ok"] is True
    cb = sent[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    conf = pa.handle_partner_callback(
        conn,
        cb,
        callback=_owner_cb(cb),
        send=send,
        answer=answer,
        edit=edit,
        futures=client,
        now_ms=NOW,
    )
    assert conf["ok"] is True
    state = load_state(db.with_name("partner_audit_state.json"))
    assert "bx:TRANSFER:fp1" not in state["open_unrecorded"]
    assert "bx:TRANSFER:keep" in state["open_unrecorded"]
    conn.close()


def test_apply_owner_passthrough_effective_and_note(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "note.db")
    admin = ps.create_partner(conn, name="admin", is_admin=True, now_ms=NOW)["partner_id"]
    assert ps.apply_deposit(conn, admin, D("100"), D("100"), NOW)["ok"]
    result = ps.apply_owner_deposit(
        conn,
        amount=D("50"),
        account_value=D("150"),
        now_ms=NOW + 10,
        effective_at_ms=NOW - 1000,
        note="owner tx via p owner; Binance TRANSFER tranId abc",
    )
    assert result["ok"] is True
    row = conn.execute("SELECT * FROM partner_event WHERE kind = 'owner_deposit'").fetchone()
    assert int(row["effective_at_ms"]) == NOW - 1000
    assert "tranId abc" in str(row["note"])
    # default path still stamps now when omitted
    result2 = ps.apply_owner_redeem(
        conn,
        amount=D("10"),
        account_value=D("140"),
        now_ms=NOW + 20,
    )
    assert result2["ok"] is True
    row2 = conn.execute(
        "SELECT * FROM partner_event WHERE kind = 'owner_redeem' ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    assert int(row2["effective_at_ms"]) == NOW + 20
    assert row2["note"] is None
    conn.close()


def test_owner_integrity_error_not_already_recorded(tmp_path: Path, monkeypatch: Any) -> None:
    """CHECK/FK-style IntegrityError with no matching row → ok False, nothing written."""
    import sqlite3

    conn = open_db(tmp_path / "int.db")
    admin = ps.create_partner(conn, name="admin", is_admin=True, now_ms=NOW)["partner_id"]
    assert ps.apply_deposit(conn, admin, D("400"), D("400"), NOW)["ok"]
    units_before = int(ps.admin_partner(conn)["units_e8"])  # type: ignore[index]
    n_before = int(conn.execute("SELECT COUNT(*) AS n FROM partner_event").fetchone()["n"])
    real_insert = ps._insert_event

    def boom(*_a: Any, **_k: Any) -> None:
        raise sqlite3.IntegrityError("CHECK constraint failed")

    monkeypatch.setattr(ps, "_insert_event", boom)
    out = ps.apply_owner_deposit(
        conn,
        amount=D("100"),
        account_value=D("500"),
        now_ms=NOW + 1,
        note="owner tx via p owner; Binance TRANSFER tranId inj1",
        event_id="pe_owner_traninj1",
    )
    assert out["ok"] is False
    assert out["reason"] == "integrity_error"
    assert int(conn.execute("SELECT COUNT(*) AS n FROM partner_event").fetchone()["n"]) == n_before
    assert int(ps.admin_partner(conn)["units_e8"]) == units_before  # type: ignore[index]

    # Real duplicate id → already_recorded (IntegrityError after row exists)
    monkeypatch.setattr(ps, "_insert_event", real_insert)
    first = ps.apply_owner_deposit(
        conn,
        amount=D("50"),
        account_value=D("450"),
        now_ms=NOW + 2,
        note="owner tx via p owner; Binance TRANSFER tranId dup1",
        event_id="pe_owner_trandup1",
    )
    assert first["ok"] is True and first.get("reason") != "already_recorded"

    def boom_pk(*_a: Any, **_k: Any) -> None:
        raise sqlite3.IntegrityError("UNIQUE constraint failed: partner_event.id")

    monkeypatch.setattr(ps, "_insert_event", boom_pk)
    # Skip only the in-tx recheck; post-rollback verify must see the existing row
    real_check = ps._owner_tx_already_in_ledger
    check_calls = {"n": 0}

    def _skip_first(conn: Any, **kwargs: Any) -> bool:
        check_calls["n"] += 1
        if check_calls["n"] == 1:
            return False
        return real_check(conn, **kwargs)

    monkeypatch.setattr(ps, "_owner_tx_already_in_ledger", _skip_first)
    again = ps.apply_owner_deposit(
        conn,
        amount=D("50"),
        account_value=D("450"),
        now_ms=NOW + 3,
        note="owner tx via p owner; Binance TRANSFER tranId dup1",
        event_id="pe_owner_trandup1",
    )
    assert again["ok"] is True
    assert again["reason"] == "already_recorded"
    n_dup = int(
        conn.execute("SELECT COUNT(*) AS n FROM partner_event WHERE id = 'pe_owner_trandup1'").fetchone()["n"]
    )
    assert n_dup == 1
    conn.close()


def test_owner_in_tx_recheck_by_note_tran_different_id(tmp_path: Path) -> None:
    """Existing owner_deposit with different id but note tranId X → already_recorded, no mint."""
    conn = open_db(tmp_path / "recheck.db")
    admin = ps.create_partner(conn, name="admin", is_admin=True, now_ms=NOW)["partner_id"]
    assert ps.apply_deposit(conn, admin, D("400"), D("400"), NOW)["ok"]
    first = ps.apply_owner_deposit(
        conn,
        amount=D("25"),
        account_value=D("425"),
        now_ms=NOW + 1,
        note="owner tx via p owner; Binance TRANSFER tranId X",
        event_id="pe_owner_old_X",
    )
    assert first["ok"] is True and first.get("reason") != "already_recorded"
    units_after = int(ps.admin_partner(conn)["units_e8"])  # type: ignore[index]
    n_events = int(conn.execute("SELECT COUNT(*) AS n FROM partner_event").fetchone()["n"])

    again = ps.apply_owner_deposit(
        conn,
        amount=D("25"),
        account_value=D("425"),
        now_ms=NOW + 2,
        note="owner tx via p owner; Binance TRANSFER tranId X",
        event_id="pe_owner_tranX",
    )
    assert again["ok"] is True
    assert again["reason"] == "already_recorded"
    assert int(ps.admin_partner(conn)["units_e8"]) == units_after  # type: ignore[index]
    assert int(conn.execute("SELECT COUNT(*) AS n FROM partner_event").fetchone()["n"]) == n_events
    assert conn.execute("SELECT 1 FROM partner_event WHERE id = 'pe_owner_tranX'").fetchone() is None
    conn.close()


def test_owner_booking_v_open_pos_refuses(tmp_path: Path, env_chat: None, monkeypatch: Any) -> None:
    """Safety V flat, booking V has open position → refuse; nothing minted (not 98.76…)."""
    conn = open_db(tmp_path / "vflip.db")
    t_init = NOW - 10 * HOUR
    _init(conn, wallet="400", now_ms=t_init)
    t0 = NOW - HOUR
    client = FakeClient([_xfer(time_ms=t0, amount="100", tran_id="vflip")], wallet="500")
    reads = {"n": 0}

    def _seq_read(_futures: Any, *, now_ms: int) -> AccountValue:
        reads["n"] += 1
        if reads["n"] == 1:
            # safety read: flat
            return AccountValue(
                value=D("500"),
                wallet=D("500"),
                unrealized=D("0"),
                open_positions=0,
                read_at_ms=int(now_ms),
            )
        # booking read: open position, V=505
        return AccountValue(
            value=D("505"),
            wallet=D("500"),
            unrealized=D("5"),
            open_positions=1,
            read_at_ms=int(now_ms),
        )

    monkeypatch.setattr(pa, "_read_v", _seq_read)
    sent: list[dict[str, Any]] = []

    def send(text: str, **kwargs: Any) -> None:
        sent.append({"text": text, **kwargs})

    def answer(_id: str, text: str | None = None) -> None:
        sent.append({"answer": text})

    def edit(_mid: int, text: str, **_k: Any) -> bool:
        sent.append({"edit": text})
        return True

    # Preview must also refuse when booking V has open positions
    out = pa.handle_partner_text(
        conn,
        "p owner dep 100",
        message=_owner_msg(),
        send=send,
        futures=client,
        now_ms=NOW,
    )
    assert out["reason"] == "unsafe"
    assert "โพซิชัน" in sent[-1]["text"]
    admin = ps.admin_partner(conn)
    assert admin is not None
    assert int(admin["units_e8"]) == 40_000_000_000
    assert (
        conn.execute("SELECT COUNT(*) AS n FROM partner_event WHERE kind = 'owner_deposit'").fetchone()["n"]
        == 0
    )

    # Confirm path: issue a real preview while flat, then flip booking V to open.
    reads["n"] = 0

    def _flat_then_open(_futures: Any, *, now_ms: int) -> AccountValue:
        # Preview uses 2 flat reads; confirm safety is flat; booking V is open.
        reads["n"] += 1
        if reads["n"] <= 3:
            return AccountValue(
                value=D("500"),
                wallet=D("500"),
                unrealized=D("0"),
                open_positions=0,
                read_at_ms=int(now_ms),
            )
        return AccountValue(
            value=D("505"),
            wallet=D("500"),
            unrealized=D("5"),
            open_positions=1,
            read_at_ms=int(now_ms),
        )

    monkeypatch.setattr(pa, "_read_v", _flat_then_open)
    preview = pa.handle_partner_text(
        conn,
        "p owner dep 100",
        message=_owner_msg(),
        send=send,
        futures=client,
        now_ms=NOW + 1,
    )
    assert preview.get("ok") is True
    cb = sent[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    conf = pa.handle_partner_callback(
        conn,
        cb,
        callback=_owner_cb("x"),
        send=send,
        answer=answer,
        edit=edit,
        futures=client,
        now_ms=NOW + 2,
    )
    assert conf["reason"] == "unsafe"
    assert (
        conn.execute("SELECT COUNT(*) AS n FROM partner_event WHERE kind = 'owner_deposit'").fetchone()["n"]
        == 0
    )
    # Never minted 98.76543209 units
    assert int(ps.admin_partner(conn)["units_e8"]) == 40_000_000_000  # type: ignore[index]
    conn.close()


def test_owner_safety_v_error_refuses(tmp_path: Path, env_chat: None, monkeypatch: Any) -> None:
    conn = open_db(tmp_path / "verr.db")
    t_init = NOW - 10 * HOUR
    _init(conn, wallet="400", now_ms=t_init)
    t0 = NOW - HOUR
    client = FakeClient([_xfer(time_ms=t0, amount="100", tran_id="verr")], wallet="500")
    from jarvise_assist.partner_value import AccountValueError

    def _err_read(_futures: Any, *, now_ms: int) -> AccountValueError:
        return AccountValueError(reason="read_failed")

    monkeypatch.setattr(pa, "_read_v", _err_read)
    sent: list[str] = []

    def send(text: str, **_k: Any) -> None:
        sent.append(text)

    out = pa.handle_partner_text(
        conn,
        "p owner dep 100",
        message=_owner_msg(),
        send=send,
        futures=client,
        now_ms=NOW,
    )
    assert out["reason"] == "unsafe"
    assert sent and ("อ่านยอด" in sent[-1] or "ย้อนหลัง" in sent[-1])
    assert (
        conn.execute("SELECT COUNT(*) AS n FROM partner_event WHERE kind = 'owner_deposit'").fetchone()["n"]
        == 0
    )
    conn.close()


def test_owner_amount_max_preview_and_callback_bytes(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "amax.db")
    t_init = NOW - 10 * HOUR
    _init(conn, wallet="400", now_ms=t_init)
    sent: list[str] = []

    def send(text: str, **_k: Any) -> None:
        sent.append(text)

    out = pa.handle_partner_text(
        conn,
        "p owner dep 1000000",
        message=_owner_msg(),
        send=send,
        futures=FakeClient(wallet="400"),
        now_ms=NOW,
    )
    assert out["reason"] == "amount_too_large"
    assert "ใหญ่เกิน" in sent[-1]

    # Token callback stays well under Telegram's 64-byte limit; legacy shape rejected.
    token = "a" * 32
    cb = f"pa:oy:{token}"
    assert len(cb.encode("utf-8")) <= 64
    assert pa._OWNER_CONFIRM_RE.match(cb)
    assert pa._OWNER_CONFIRM_RE.match("pa:oy:d:tran:1700000000:100000000") is None
    conn.close()


def test_owner_preview_token_expired_and_restart(tmp_path: Path, env_chat: None, monkeypatch: Any) -> None:
    """Expired token refuses; unconsumed token on disk still works after 'restart'."""
    conn = open_db(tmp_path / "tok.db")
    t_init = NOW - 10 * HOUR
    _init(conn, wallet="400", now_ms=t_init)
    t0 = NOW - HOUR
    client = FakeClient([_xfer(time_ms=t0, amount="100", tran_id="tok1")], wallet="500")
    monkeypatch.setattr(
        pa,
        "_read_v",
        lambda *_a, **_k: AccountValue(
            value=D("500"), wallet=D("500"), unrealized=D("0"), open_positions=0, read_at_ms=NOW
        ),
    )
    sent: list[dict[str, Any]] = []

    def send(text: str, **kwargs: Any) -> None:
        sent.append({"text": text, **kwargs})

    out = pa.handle_partner_text(
        conn,
        "p owner dep 100",
        message=_owner_msg(),
        send=send,
        futures=client,
        now_ms=NOW,
    )
    assert out["ok"] is True
    token = str(out["token"])
    cb = f"pa:oy:{token}"

    # Expired (>900s)
    stale = pa.handle_partner_callback(
        conn,
        cb,
        callback=_owner_cb(cb),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: True,
        futures=client,
        now_ms=NOW + 901_000,
    )
    assert stale["reason"] == "stale_confirm"
    assert (
        conn.execute("SELECT COUNT(*) AS n FROM partner_event WHERE kind = 'owner_deposit'").fetchone()["n"]
        == 0
    )

    # Re-issue; simulate process restart by only using the on-disk preview file.
    sent.clear()
    out2 = pa.handle_partner_text(
        conn,
        "p owner dep 100",
        message=_owner_msg(),
        send=send,
        futures=client,
        now_ms=NOW + 1,
    )
    assert out2["ok"] is True
    token2 = str(out2["token"])
    from jarvise_assist.partner_json_store import load_json_object, owner_preview_path_for

    path = owner_preview_path_for(conn)
    assert path is not None
    disk = load_json_object(path)
    assert token2 in (disk.get("previews") or {})
    conf = pa.handle_partner_callback(
        conn,
        f"pa:oy:{token2}",
        callback=_owner_cb(f"pa:oy:{token2}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: True,
        futures=client,
        now_ms=NOW + 2,
    )
    assert conf.get("ok") is True
    assert conf.get("cmd") == "owner_confirm"
    # Second tap → already_recorded
    again = pa.handle_partner_callback(
        conn,
        f"pa:oy:{token2}",
        callback=_owner_cb(f"pa:oy:{token2}"),
        send=send,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: True,
        futures=client,
        now_ms=NOW + 3,
    )
    assert again["reason"] == "already_recorded"
    conn.close()


def test_audit_state_locked_rmw_serializes(tmp_path: Path, monkeypatch: Any) -> None:
    """Two overlapping remove updates do not drop each other's changes.

    Monkeypatch flock to a no-op so this pins the threading.Lock (on Linux
    flock alone does not exclude same-process threads).
    """
    import threading
    import time

    import jarvise_assist.partner_json_store as pjs
    from jarvise_assist.partner_audit import load_state, remove_open_unrecorded_for_tran, save_state
    from jarvise_assist.partner_json_store import locked_json_update

    monkeypatch.setattr(pjs.fcntl, "flock", lambda *_a, **_k: None)

    path = tmp_path / "partner_audit_state.json"
    save_state(
        path,
        {
            "v": 1,
            "alerted": {},
            "open_unrecorded": ["bx:TRANSFER:a1", "bx:TRANSFER:b2", "bx:TRANSFER:c3"],
            "last_ok_ms": NOW,
            "fail_since_ms": None,
            "fail_alerted": False,
        },
    )
    order: list[str] = []
    started = threading.Event()
    errors: list[BaseException] = []

    def slow_a() -> None:
        try:

            def mutate(state: dict[str, Any]) -> None:
                order.append("a-start")
                started.set()
                time.sleep(0.05)
                state["open_unrecorded"] = [
                    fp for fp in list(state.get("open_unrecorded") or []) if not str(fp).endswith(":a1")
                ]
                order.append("a-end")

            locked_json_update(path, mutate, load=load_state, save=save_state)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def fast_b() -> None:
        try:
            assert started.wait(timeout=2)

            def mutate(state: dict[str, Any]) -> None:
                order.append("b")
                state["open_unrecorded"] = [
                    fp for fp in list(state.get("open_unrecorded") or []) if not str(fp).endswith(":b2")
                ]

            locked_json_update(path, mutate, load=load_state, save=save_state)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=slow_a)
    t2 = threading.Thread(target=fast_b)
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert not errors
    assert order == ["a-start", "a-end", "b"]
    state = load_state(path)
    assert state["open_unrecorded"] == ["bx:TRANSFER:c3"]
    remove_open_unrecorded_for_tran(path, "c3")
    assert load_state(path)["open_unrecorded"] == []
