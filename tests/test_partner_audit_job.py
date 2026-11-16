"""P4 partner_audit job: dedupe state, fail/recovery, read-only DB."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from jarvise_assist import partner_store as ps
from jarvise_assist.partner_audit import (
    FAIL_ALERT_AFTER_MS,
    GRACE_BINANCE_MS,
    Transfer,
    fetch_transfers,
    run_partner_audit_job,
)
from jarvise_ingest.db import open_db

D = Decimal
NOW = 1_700_000_000_000


class FakeClient:
    def __init__(self, transfers: list[Transfer] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.transfers = list(transfers or [])
        self.exc: Exception | None = None
        self.fail = False

    def income_rows(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc
        if self.fail:
            raise ConnectionError("unavailable")
        # Return all transfers whose type/time fall in window (simple filter)
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

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"unexpected method {name}")


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_job_not_init_no_side_effects(tmp_path: Path) -> None:
    db = tmp_path / "empty.db"
    conn = open_db(db)
    state = tmp_path / "partner_audit_state.json"
    client = FakeClient()
    sent: list[Any] = []

    def send(text: str, **kwargs: Any) -> int:
        sent.append((text, kwargs))
        return 1

    out = run_partner_audit_job(conn, client=client, now_ms=NOW, send=send, state_path=state)
    assert out["ok"] is True
    assert out["status"] == "not_init"
    assert client.calls == []
    assert sent == []
    assert not state.exists()
    conn.close()


def test_job_fail_alert_once_then_recovery(tmp_path: Path) -> None:
    db = tmp_path / "fail.db"
    conn = open_db(db)
    admin = ps.create_partner(conn, name="admin", is_admin=True, now_ms=NOW)
    ps.apply_deposit(conn, admin["partner_id"], D("1000"), D("1000"), NOW)
    state = tmp_path / "partner_audit_state.json"
    client = FakeClient()
    client.fail = True
    sent: list[str] = []

    def send(text: str) -> int:
        sent.append(text)
        return 1

    t0 = NOW + 10_000
    r1 = run_partner_audit_job(conn, client=client, now_ms=t0, send=send, state_path=state)
    assert r1["ok"] is False
    assert sent == []

    r2 = run_partner_audit_job(conn, client=client, now_ms=t0 + 24 * 3_600_000, send=send, state_path=state)
    assert r2["ok"] is False
    assert sent == []

    r3 = run_partner_audit_job(
        conn,
        client=client,
        now_ms=t0 + FAIL_ALERT_AFTER_MS,
        send=send,
        state_path=state,
    )
    assert r3["ok"] is False
    assert len(sent) == 1
    assert "ไม่ได้มา 2 วัน" in sent[0]

    r4 = run_partner_audit_job(
        conn,
        client=client,
        now_ms=t0 + FAIL_ALERT_AFTER_MS + 3_600_000,
        send=send,
        state_path=state,
    )
    assert r4["ok"] is False
    assert len(sent) == 1  # silent

    client.fail = False
    r5 = run_partner_audit_job(
        conn,
        client=client,
        now_ms=t0 + FAIL_ALERT_AFTER_MS + 2 * 3_600_000,
        send=send,
        state_path=state,
    )
    assert r5["ok"] is True
    assert len(sent) == 2
    assert "กลับมาทำงาน" in sent[1]
    conn.close()


def test_job_dedupe_and_send_none(tmp_path: Path) -> None:
    db = tmp_path / "dedupe.db"
    conn = open_db(db)
    admin = ps.create_partner(conn, name="admin", is_admin=True, now_ms=NOW)
    ps.apply_deposit(conn, admin["partner_id"], D("1000"), D("1000"), NOW)
    state = tmp_path / "partner_audit_state.json"
    t_xfer = NOW + GRACE_BINANCE_MS + 1000
    client = FakeClient(
        [
            Transfer(
                time_ms=t_xfer,
                amount=D("50"),
                asset="USDT",
                income_type="TRANSFER",
                tran_id="orphan",
            )
        ]
    )
    sent: list[str] = []

    def send_ok(text: str) -> int:
        sent.append(text)
        return 42

    now = t_xfer + GRACE_BINANCE_MS + 1
    before = _hash(db)
    r1 = run_partner_audit_job(conn, client=client, now_ms=now, send=send_ok, state_path=state)
    assert r1["ok"] is True
    assert r1["new"] == 1
    assert r1["alert_sent"] is True
    assert len(sent) == 1
    assert "รายการใหม่" in sent[0]
    assert _hash(db) == before

    r2 = run_partner_audit_job(conn, client=client, now_ms=now + 1000, send=send_ok, state_path=state)
    assert r2["new"] == 0
    assert len(sent) == 1

    # send None -> not stored
    state.write_text("not-json{{{", encoding="utf-8")  # corrupt handled
    client2 = FakeClient(
        [
            Transfer(
                time_ms=t_xfer + 10,
                amount=D("7"),
                asset="USDT",
                income_type="TRANSFER",
                tran_id="again",
            )
        ]
    )
    sent2: list[str] = []

    def send_none(text: str) -> None:
        sent2.append(text)
        return None

    now2 = t_xfer + 10 + GRACE_BINANCE_MS + 1
    r3 = run_partner_audit_job(conn, client=client2, now_ms=now2, send=send_none, state_path=state)
    assert r3["new"] == 1
    assert len(sent2) == 1
    data = json.loads(state.read_text(encoding="utf-8"))
    assert "bx:TRANSFER:again" not in data.get("alerted", {})

    def send_ok2(text: str) -> int:
        sent2.append(text)
        return 1

    r4 = run_partner_audit_job(conn, client=client2, now_ms=now2 + 1, send=send_ok2, state_path=state)
    assert r4["new"] == 1
    assert len(sent2) == 2
    conn.close()


def test_job_footer_open_and_return_shape(tmp_path: Path) -> None:
    db = tmp_path / "open.db"
    conn = open_db(db)
    admin = ps.create_partner(conn, name="admin", is_admin=True, now_ms=NOW)
    ps.apply_deposit(conn, admin["partner_id"], D("1000"), D("1000"), NOW)
    state = tmp_path / "partner_audit_state.json"
    t1 = NOW + GRACE_BINANCE_MS + 1000
    t2 = t1 + 5000
    client = FakeClient(
        [
            Transfer(time_ms=t1, amount=D("11"), asset="USDT", income_type="TRANSFER", tran_id="a"),
            Transfer(time_ms=t2, amount=D("12"), asset="USDT", income_type="TRANSFER", tran_id="b"),
        ]
    )
    sent: list[str] = []

    def send(text: str, **kwargs: Any) -> int:
        assert kwargs == {}
        sent.append(text)
        return 1

    now = t2 + GRACE_BINANCE_MS + 1
    run_partner_audit_job(conn, client=client, now_ms=now, send=send, state_path=state)
    # second run with only new finding b already alerted? remove a from client, keep both
    # add a third
    client.transfers.append(
        Transfer(time_ms=t2 + 10, amount=D("13"), asset="USDT", income_type="TRANSFER", tran_id="c")
    )
    out = run_partner_audit_job(
        conn, client=client, now_ms=now + 20 + GRACE_BINANCE_MS, send=send, state_path=state
    )
    assert "ยังค้างจากก่อนหน้า" in sent[-1]
    for key in ("ok", "status", "new", "open", "pending", "matched", "alert_sent", "error"):
        assert key in out
    blob = json.dumps(out)
    assert "admin" not in blob
    assert "11" not in blob
    assert "orphan" not in blob
    assert "bx:" not in blob
    conn.close()


def test_full_flow_then_unrecorded_alert(tmp_path: Path) -> None:
    db = tmp_path / "flow.db"
    conn = open_db(db)
    admin = ps.create_partner(conn, name="admin", is_admin=True, now_ms=NOW)
    aid = admin["partner_id"]
    ps.apply_deposit(conn, aid, D("1000"), D("1000"), NOW)  # init
    bob = ps.create_partner(conn, name="Bob", is_admin=False, now_ms=NOW + 1)
    bid = bob["partner_id"]
    dep_t = NOW + 10_000
    ps.apply_deposit(conn, bid, D("100"), D("1100"), dep_t)
    # withdraw/close for payout
    close = ps.apply_close(conn, bid, D("1100"), dep_t + 20_000)
    assert close["ok"]
    rid = close["request_id"]
    out_t = dep_t + 25_000
    paid_t = dep_t + 30_000
    ps.mark_paid(conn, rid, paid_t)
    assert ps.verify_ledger(conn)["ok"] is True

    transfers = [
        Transfer(
            time_ms=dep_t,
            amount=D("100"),
            asset="USDT",
            income_type="TRANSFER",
            tran_id="bob_in",
        ),
        Transfer(
            time_ms=out_t,
            amount=-D(str(close["cash"])),
            asset="USDT",
            income_type="TRANSFER",
            tran_id="bob_out",
        ),
    ]
    client = FakeClient(transfers)
    state = tmp_path / "partner_audit_state.json"
    sent: list[str] = []

    def send(text: str) -> int:
        sent.append(text)
        return 1

    now = paid_t + GRACE_BINANCE_MS + 1
    before = _hash(db)
    r1 = run_partner_audit_job(conn, client=client, now_ms=now, send=send, state_path=state)
    assert r1["ok"] is True
    assert r1["new"] == 0
    assert sent == []
    assert ps.verify_ledger(conn)["ok"] is True
    assert _hash(db) == before

    client.transfers.append(
        Transfer(
            time_ms=now - GRACE_BINANCE_MS - 1000,
            amount=D("9"),
            asset="USDT",
            income_type="TRANSFER",
            tran_id="admin_extra",
        )
    )
    r2 = run_partner_audit_job(conn, client=client, now_ms=now + 1000, send=send, state_path=state)
    assert r2["new"] == 1
    assert len(sent) == 1
    r3 = run_partner_audit_job(conn, client=client, now_ms=now + 2000, send=send, state_path=state)
    assert r3["new"] == 0
    assert len(sent) == 1
    assert ps.verify_ledger(conn)["ok"] is True
    assert _hash(db) == before
    conn.close()


def test_fetch_no_client() -> None:
    r = fetch_transfers(None, NOW - 1000, NOW, now_ms=NOW)
    assert r.status == "no_client"
