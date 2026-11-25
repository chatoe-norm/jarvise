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
    assert cb.startswith("pa:oy:dep:t_one:")

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
    out = pa.handle_partner_text(
        multi_db,
        "p owner dep 50 a2",
        message=_owner_msg(),
        send=send,
        futures=multi,
        now_ms=NOW,
    )
    assert out["ok"] is True
    assert out["tran_id"] == "a2"
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
