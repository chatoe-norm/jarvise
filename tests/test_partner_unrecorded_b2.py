"""B2: block cv/wd/close and portal while transfers are unrecorded."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from jarvise_assist import partner_admin as pa
from jarvise_assist import partner_portal as portal
from jarvise_assist import partner_store as ps
from jarvise_assist.partner_audit import (
    Finding,
    GuardResult,
    Transfer,
    load_state,
    run_partner_audit_job,
    save_state,
)
from jarvise_assist.partner_value import AccountValue
from jarvise_ingest.db import open_db

D = Decimal
NOW = 1_700_000_000_000
ADMIN = "42"
HOUR = 3_600_000


class FakeClient:
    def __init__(self, transfers: list[Transfer] | None = None, *, wallet: str = "1000") -> None:
        self.transfers = list(transfers or [])
        self.wallet = wallet
        self.fail = False

    def income_rows(self, **kwargs: Any) -> list[dict[str, Any]]:
        if self.fail:
            raise ConnectionError("boom")
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

    def balance_rows(self) -> list[dict[str, Any]]:
        return [{"asset": "USDT", "balance": self.wallet}]

    def position_rows(self) -> list[dict[str, Any]]:
        return []

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


@pytest.fixture
def env_chat(monkeypatch: Any) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", ADMIN)


@pytest.fixture(autouse=True)
def _reset_portal_caches() -> None:
    portal.reset_value_cache()
    portal.reset_guard_cache()


def _seed(conn: Any, *, now_ms: int = NOW - 10 * HOUR) -> tuple[str, str]:
    admin = ps.create_partner(conn, name="admin", is_admin=True, now_ms=now_ms)["partner_id"]
    bob = ps.create_partner(conn, name="Bob", now_ms=now_ms + 1)["partner_id"]
    assert ps.apply_deposit(conn, admin, D("800"), D("800"), now_ms)["ok"]
    assert ps.apply_deposit(conn, bob, D("200"), D("1000"), now_ms + 2)["ok"]
    return str(admin), str(bob)


def _owner_cb(data: str, mid: int = 1) -> dict[str, Any]:
    return {
        "id": "cb1",
        "from": {"id": ADMIN},
        "message": {
            "message_id": mid,
            "chat": {"id": ADMIN, "type": "private"},
            "text": "คำขอ",
        },
        "data": data,
    }


def test_load_state_accepts_old_without_open_unrecorded(tmp_path: Path) -> None:
    path = tmp_path / "partner_audit_state.json"
    path.write_text(json.dumps({"v": 1, "alerted": {"x": 1}, "fail_alerted": False}), encoding="utf-8")
    state = load_state(path)
    assert state["open_unrecorded"] == []
    assert state["alerted"] == {"x": 1}


def test_audit_job_writes_and_clears_open_unrecorded(tmp_path: Path) -> None:
    db = tmp_path / "job.db"
    conn = open_db(db)
    _seed(conn)
    state_path = tmp_path / "partner_audit_state.json"
    t0 = NOW - HOUR  # after ledger start; within report window
    client = FakeClient([_xfer(time_ms=t0, amount="50", tran_id="u1")], wallet="1050")
    sent: list[str] = []

    def send(text: str, **_k: Any) -> int:
        sent.append(text)
        return 1

    out = run_partner_audit_job(conn, client=client, now_ms=NOW, send=send, state_path=state_path)
    assert out["ok"] is True
    state = load_state(state_path)
    assert "bx:TRANSFER:u1" in state["open_unrecorded"]

    # Cleared when resolved (no unmatched transfer)
    client2 = FakeClient(wallet="1000")
    out2 = run_partner_audit_job(conn, client=client2, now_ms=NOW + 1, send=send, state_path=state_path)
    assert out2["ok"] is True
    state2 = load_state(state_path)
    assert state2["open_unrecorded"] == []
    conn.close()


def test_cv_wd_close_blocked_no_force(tmp_path: Path, env_chat: None, monkeypatch: Any) -> None:
    conn = open_db(tmp_path / "block.db")
    _admin, bob = _seed(conn)

    finding = Finding(
        code="transfer_in_unrecorded",
        fingerprint="bx:TRANSFER:z1",
        time_ms=NOW - HOUR,
        amount=D("10"),
        partner_name=None,
        detail="",
        alertable=True,
    )

    def _fake_guard(conn: Any, client: Any, *, now_ms: int, req: Any) -> tuple[GuardResult, str]:
        return (
            GuardResult(
                status="ok",
                checked_at_ms=int(now_ms),
                findings=[finding],
                warn=True,
                error=None,
            ),
            "guard text",
        )

    monkeypatch.setattr(pa, "_transfer_guard", _fake_guard)
    monkeypatch.setattr(
        pa,
        "_read_v",
        lambda *_a, **_k: AccountValue(
            value=D("1000"), wallet=D("1000"), unrealized=D("0"), open_positions=0, read_at_ms=NOW
        ),
    )

    answers: list[str | None] = []
    sent: list[dict[str, Any]] = []

    def answer(_id: str, text: str | None = None) -> None:
        answers.append(text)

    def send(text: str, **kwargs: Any) -> None:
        sent.append({"text": text, **kwargs})

    def edit(_mid: int, text: str, **kwargs: Any) -> bool:
        sent.append({"text": text, **kwargs})
        return True

    for kind in ("convert", "withdraw_profit", "close"):
        created = ps.create_request(conn, bob, kind, NOW, source="admin")
        assert created["ok"], (kind, created)
        rid = str(created["request_id"])
        out = pa.handle_partner_callback(
            conn,
            f"pa:ry:{rid}",
            callback=_owner_cb(f"pa:ry:{rid}"),
            send=send,
            answer=answer,
            edit=edit,
            now_ms=NOW,
        )
        assert out["reason"] == "unrecorded_transfer", kind
        assert answers and "p owner" in (answers[-1] or "")
        markup = sent[-1].get("reply_markup") or {}
        flat = [b.get("callback_data") for row in markup.get("inline_keyboard") or [] for b in row]
        assert not any(str(c).startswith("pa:rf:") for c in flat), kind
        # forged force still refused
        out2 = pa.handle_partner_callback(
            conn,
            f"pa:rf:{rid}:{NOW // 1000}",
            callback=_owner_cb(f"pa:rf:{rid}:{NOW // 1000}"),
            send=send,
            answer=answer,
            edit=edit,
            now_ms=NOW,
        )
        assert out2["reason"] == "unrecorded_transfer", kind
        assert (
            conn.execute("SELECT status FROM partner_request WHERE id = ?", (rid,)).fetchone()["status"]
            == "pending"
        )
        assert ps.reject_request(conn, rid, NOW + 1)["ok"]

    # out direction (pending=false alertable)
    finding_out = Finding(
        code="transfer_out_unrecorded",
        fingerprint="bx:TRANSFER:z2",
        time_ms=NOW - HOUR,
        amount=D("10"),
        partner_name=None,
        detail="",
        alertable=False,
    )

    def _fake_out(conn: Any, client: Any, *, now_ms: int, req: Any) -> tuple[GuardResult, str]:
        return (
            GuardResult(status="ok", checked_at_ms=int(now_ms), findings=[finding_out], warn=True),
            "out",
        )

    monkeypatch.setattr(pa, "_transfer_guard", _fake_out)
    wd = ps.create_request(conn, bob, "convert", NOW + 2, source="admin")
    assert wd["ok"], wd
    out3 = pa.handle_partner_callback(
        conn,
        f"pa:ry:{wd['request_id']}",
        callback=_owner_cb(f"pa:ry:{wd['request_id']}"),
        send=send,
        answer=answer,
        edit=edit,
        now_ms=NOW + 2,
    )
    assert out3["reason"] == "unrecorded_transfer"

    # fetch failure
    def _fake_fail(conn: Any, client: Any, *, now_ms: int, req: Any) -> tuple[GuardResult, str]:
        return (
            GuardResult(status="read_failed", checked_at_ms=int(now_ms), findings=[], warn=True, error="x"),
            "fail",
        )

    monkeypatch.setattr(pa, "_transfer_guard", _fake_fail)
    assert ps.reject_request(conn, str(wd["request_id"]), NOW + 3)["ok"]
    cl = ps.create_request(conn, bob, "close", NOW + 4, source="admin")
    assert cl["ok"], cl
    out4 = pa.handle_partner_callback(
        conn,
        f"pa:ry:{cl['request_id']}",
        callback=_owner_cb(f"pa:ry:{cl['request_id']}"),
        send=send,
        answer=answer,
        edit=edit,
        now_ms=NOW + 4,
    )
    assert out4["reason"] == "unrecorded_transfer"
    conn.close()


def test_cv_near_amount_warn_force_no_guard_text_to_partner(
    tmp_path: Path, env_chat: None, monkeypatch: Any
) -> None:
    """Non-unrecorded warn+force on convert must remain; partner notify has no guard text."""
    conn = open_db(tmp_path / "force_ok.db")
    _admin, bob = _seed(conn)
    conn.execute("UPDATE partner SET telegram_user_id = ? WHERE id = ?", ("501", bob))
    conn.commit()
    # Bob 200/1000 units at V=1200 → profit; convert should succeed after force.
    created = ps.create_request(conn, bob, "convert", NOW, source="admin")
    assert created["ok"]
    rid = str(created["request_id"])

    finding = Finding(
        code="near_amount",
        fingerprint=f"near:x:rq:{rid}",
        time_ms=NOW - HOUR,
        amount=D("1"),
        partner_name=None,
        detail="",
        alertable=False,
    )

    def _fake_guard(conn: Any, client: Any, *, now_ms: int, req: Any) -> tuple[GuardResult, str]:
        return (
            GuardResult(status="ok", checked_at_ms=int(now_ms), findings=[finding], warn=True),
            "⚠️ ตรวจยอดโอนก่อนยืนยัน\nnear",
        )

    monkeypatch.setattr(pa, "_transfer_guard", _fake_guard)
    monkeypatch.setattr(
        pa,
        "_read_v",
        lambda *_a, **_k: AccountValue(
            value=D("1200"), wallet=D("1200"), unrealized=D("0"), open_positions=0, read_at_ms=NOW
        ),
    )

    answers: list[str | None] = []
    sent: list[dict[str, Any]] = []

    def answer(_id: str, text: str | None = None) -> None:
        answers.append(text)

    def send(text: str, **kwargs: Any) -> None:
        sent.append({"text": text, **kwargs})

    def edit(_mid: int, text: str, **kwargs: Any) -> bool:
        sent.append({"text": text, **kwargs})
        return True

    out = pa.handle_partner_callback(
        conn,
        f"pa:ry:{rid}",
        callback=_owner_cb(f"pa:ry:{rid}"),
        send=send,
        answer=answer,
        edit=edit,
        now_ms=NOW,
    )
    assert out["reason"] == "guard_warn"
    markup = sent[-1].get("reply_markup") or {}
    flat = [b.get("callback_data") for row in markup.get("inline_keyboard") or [] for b in row]
    force_cbs = [c for c in flat if str(c).startswith("pa:rf:")]
    assert force_cbs
    stamp = str(force_cbs[0]).rsplit(":", 1)[-1]
    sent.clear()
    out2 = pa.handle_partner_callback(
        conn,
        f"pa:rf:{rid}:{stamp}",
        callback=_owner_cb(f"pa:rf:{rid}:{stamp}"),
        send=send,
        answer=answer,
        edit=edit,
        now_ms=NOW + 1000,
    )
    assert out2.get("ok") is True
    partner_msgs = [s["text"] for s in sent if s.get("chat_id") == "501"]
    assert partner_msgs
    assert all("ตรวจยอดโอน" not in t for t in partner_msgs)
    conn.close()


def test_deposit_confirm_keeps_warn_force(tmp_path: Path, env_chat: None, monkeypatch: Any) -> None:
    conn = open_db(tmp_path / "dep.db")
    _admin, bob = _seed(conn)
    created = ps.create_request(conn, bob, "deposit", NOW, amount=D("50"), source="admin")
    assert created["ok"]
    rid = str(created["request_id"])
    finding = Finding(
        code="transfer_in_unrecorded",
        fingerprint="bx:TRANSFER:d1",
        time_ms=NOW - HOUR,
        amount=D("50"),
        partner_name=None,
        detail="",
        alertable=True,
    )

    def _fake_guard(conn: Any, client: Any, *, now_ms: int, req: Any) -> tuple[GuardResult, str]:
        return (
            GuardResult(status="ok", checked_at_ms=int(now_ms), findings=[finding], warn=True),
            "dep guard",
        )

    monkeypatch.setattr(pa, "_transfer_guard", _fake_guard)
    answers: list[str | None] = []
    sent: list[dict[str, Any]] = []

    def answer(_id: str, text: str | None = None) -> None:
        answers.append(text)

    def send(text: str, **kwargs: Any) -> None:
        sent.append({"text": text, **kwargs})

    def edit(_mid: int, text: str, **kwargs: Any) -> bool:
        sent.append({"text": text, **kwargs})
        return True

    out = pa.handle_partner_callback(
        conn,
        f"pa:dy:{rid}",
        callback=_owner_cb(f"pa:dy:{rid}"),
        send=send,
        answer=answer,
        edit=edit,
        now_ms=NOW,
    )
    assert out["reason"] == "guard_warn"
    markup = sent[-1].get("reply_markup") or {}
    flat = [b.get("callback_data") for row in markup.get("inline_keyboard") or [] for b in row]
    assert any(str(c).startswith("pa:df:") for c in flat)
    conn.close()


def test_portal_card_and_create_request_blocked(tmp_path: Path, monkeypatch: Any) -> None:
    db = tmp_path / "portal.db"
    conn = open_db(db)
    _admin, bob = _seed(conn)
    conn.execute("UPDATE partner SET telegram_user_id = ? WHERE id = ?", ("99", bob))
    conn.commit()
    state_path = Path(str(db)).with_name("partner_audit_state.json")
    save_state(
        state_path,
        {
            "v": 1,
            "alerted": {},
            "open_unrecorded": ["bx:TRANSFER:open1"],
            "last_ok_ms": NOW,
            "fail_since_ms": None,
            "fail_alerted": False,
        },
    )

    row = ps.get_partner(conn, bob)
    assert row is not None
    av = AccountValue(value=D("1000"), wallet=D("1000"), unrealized=D("0"), open_positions=0, read_at_ms=NOW)
    text, markup = portal._build_card(conn, row, av, now_ms=NOW, futures=None)
    assert "กำลังตรวจยอด" in text
    assert "ยอดสุทธิ" not in text
    # net / % must be hidden (no tautology)
    assert "%" not in text
    assert "207.50" not in text
    assert "+108" not in text
    flat = [b.get("text") for row_b in (markup or {}).get("inline_keyboard") or [] for b in row_b]
    assert any("กำลังตรวจยอด" in str(t) for t in flat)

    answers: list[str | None] = []

    def answer(_id: str, text: str | None = None) -> None:
        answers.append(text)

    def send(*_a: Any, **_k: Any) -> None:
        return None

    def edit(*_a: Any, **_k: Any) -> bool:
        return True

    out = portal._submit_request(
        conn,
        row,
        "cv",
        answer=answer,
        edit=edit,
        send=send,
        msg={"message_id": 1, "chat": {"id": "99", "type": "private"}},
        futures=None,
        now_ms=NOW,
    )
    assert out["reason"] == "unrecorded_transfer"
    assert (
        conn.execute(
            "SELECT COUNT(*) AS n FROM partner_request WHERE kind = 'convert' AND status = 'pending'"
        ).fetchone()["n"]
        == 0
    )

    # Guard fetch error / read_failed must fail closed — never show net/%
    portal.reset_guard_cache()
    save_state(
        state_path,
        {
            "v": 1,
            "alerted": {},
            "open_unrecorded": [],
            "last_ok_ms": None,
            "fail_since_ms": None,
            "fail_alerted": False,
        },
    )

    def boom(*_a: Any, **_k: Any) -> GuardResult:
        raise RuntimeError("nope")

    monkeypatch.setattr("jarvise_assist.partner_audit.guard_check", boom)
    text2, markup2 = portal._build_card(conn, row, av, now_ms=NOW + 1, futures=FakeClient())
    assert "กำลังตรวจยอด" in text2
    assert "ยอดสุทธิ" not in text2
    assert "207.50" not in text2
    assert "%" not in text2
    flat2 = [b.get("text") for row_b in (markup2 or {}).get("inline_keyboard") or [] for b in row_b]
    assert any("กำลังตรวจยอด" in str(t) for t in flat2)

    out_fail = portal._submit_request(
        conn,
        row,
        "cv",
        answer=answer,
        edit=edit,
        send=send,
        msg={"message_id": 1, "chat": {"id": "99", "type": "private"}},
        futures=FakeClient(),
        now_ms=NOW + 2,
    )
    assert out_fail["reason"] == "unrecorded_transfer"
    conn.close()


def test_portal_read_failed_hides_net_and_refuses_convert(tmp_path: Path, monkeypatch: Any) -> None:
    """Codex repro: read_failed must not show 207.50 / +108.02% and must refuse convert."""
    db = tmp_path / "rf.db"
    conn = open_db(db)
    _admin, bob = _seed(conn)
    # Mimic ToTo-like cost so a buggy clear path would show net
    conn.execute(
        "UPDATE partner SET units_e8 = ?, cost_micro = ?, telegram_user_id = ? WHERE id = ?",
        (9_837_165_076, 99_750_000, "99", bob),
    )
    conn.commit()
    row = ps.get_partner(conn, bob)
    assert row is not None
    av = AccountValue(
        value=D("897.03482611"),
        wallet=D("897.03482611"),
        unrealized=D("0"),
        open_positions=0,
        read_at_ms=NOW,
    )

    def _failed(*_a: Any, **_k: Any) -> GuardResult:
        return GuardResult(status="read_failed", checked_at_ms=NOW, findings=[], warn=True, error="x")

    monkeypatch.setattr("jarvise_assist.partner_audit.guard_check", _failed)
    text, _m = portal._build_card(conn, row, av, now_ms=NOW, futures=FakeClient())
    assert "กำลังตรวจยอด" in text
    assert "207.50" not in text
    assert "ยอดสุทธิ" not in text
    assert "+108" not in text

    answers: list[str | None] = []

    def answer(_id: str, text: str | None = None) -> None:
        answers.append(text)

    out = portal._submit_request(
        conn,
        row,
        "cv",
        answer=answer,
        edit=lambda *_a, **_k: True,
        send=lambda *_a, **_k: None,
        msg={"message_id": 1, "chat": {"id": "99", "type": "private"}},
        futures=FakeClient(),
        now_ms=NOW,
    )
    assert out["reason"] == "unrecorded_transfer"
    conn.close()


def test_portal_fresh_guard_on_request_after_cached_clear(tmp_path: Path, monkeypatch: Any) -> None:
    db = tmp_path / "cache.db"
    conn = open_db(db)
    _admin, bob = _seed(conn)
    conn.execute("UPDATE partner SET telegram_user_id = ? WHERE id = ?", ("99", bob))
    conn.commit()
    row = ps.get_partner(conn, bob)
    assert row is not None
    av = AccountValue(value=D("1000"), wallet=D("1000"), unrealized=D("0"), open_positions=0, read_at_ms=NOW)
    clean = FakeClient(wallet="1000")
    calls = {"n": 0}

    def _guard(conn: Any, client: Any, *, now_ms: int) -> GuardResult:
        calls["n"] += 1
        if calls["n"] == 1:
            # card display: clean
            return GuardResult(status="ok", checked_at_ms=int(now_ms), findings=[], warn=False)
        # create_request fresh check: new transfer appeared
        return GuardResult(
            status="ok",
            checked_at_ms=int(now_ms),
            findings=[
                Finding(
                    code="transfer_in_unrecorded",
                    fingerprint="bx:TRANSFER:new1",
                    time_ms=int(now_ms) - 1000,
                    amount=D("10"),
                    partner_name=None,
                    detail="",
                    alertable=True,
                )
            ],
            warn=True,
        )

    monkeypatch.setattr("jarvise_assist.partner_audit.guard_check", _guard)
    text, _m = portal._build_card(conn, row, av, now_ms=NOW, futures=clean)
    assert "กำลังตรวจยอด" not in text
    assert "ยอดสุทธิ" in text

    answers: list[str | None] = []

    def answer(_id: str, text: str | None = None) -> None:
        answers.append(text)

    # Even with a cached clear display, create_request must re-check and refuse
    out = portal._submit_request(
        conn,
        row,
        "cv",
        answer=answer,
        edit=lambda *_a, **_k: True,
        send=lambda *_a, **_k: None,
        msg={"message_id": 1, "chat": {"id": "99", "type": "private"}},
        futures=clean,
        now_ms=NOW + 1,
    )
    assert out["reason"] == "unrecorded_transfer"
    assert calls["n"] >= 2
    conn.close()


def test_live_clear_overrides_stale_state_hint(tmp_path: Path, monkeypatch: Any) -> None:
    db = tmp_path / "hint.db"
    conn = open_db(db)
    _admin, bob = _seed(conn)
    conn.execute("UPDATE partner SET telegram_user_id = ? WHERE id = ?", ("99", bob))
    conn.commit()
    save_state(
        Path(str(db)).with_name("partner_audit_state.json"),
        {
            "v": 1,
            "alerted": {},
            "open_unrecorded": ["bx:TRANSFER:stale"],
            "last_ok_ms": NOW,
            "fail_since_ms": None,
            "fail_alerted": False,
        },
    )
    row = ps.get_partner(conn, bob)
    assert row is not None
    av = AccountValue(value=D("1000"), wallet=D("1000"), unrealized=D("0"), open_positions=0, read_at_ms=NOW)

    def _clear(*_a: Any, **_k: Any) -> GuardResult:
        return GuardResult(status="ok", checked_at_ms=NOW, findings=[], warn=False)

    monkeypatch.setattr("jarvise_assist.partner_audit.guard_check", _clear)
    text, _m = portal._build_card(conn, row, av, now_ms=NOW, futures=FakeClient())
    assert "กำลังตรวจยอด" not in text
    assert "ยอดสุทธิ" in text

    # Overview also clears the warn
    ov = pa._overview_text(conn, av, now_ms=NOW, futures=FakeClient())
    assert "ยังไม่บันทึก" not in ov
    conn.close()


def test_overview_warns_unrecorded(tmp_path: Path, env_chat: None, monkeypatch: Any) -> None:
    db = tmp_path / "ov.db"
    conn = open_db(db)
    _seed(conn)
    save_state(
        Path(str(db)).with_name("partner_audit_state.json"),
        {
            "v": 1,
            "alerted": {},
            "open_unrecorded": ["bx:TRANSFER:a", "bx:TRANSFER:b"],
            "last_ok_ms": NOW,
            "fail_since_ms": None,
            "fail_alerted": False,
        },
    )

    def _unknown(*_a: Any, **_k: Any) -> GuardResult:
        return GuardResult(status="read_failed", checked_at_ms=NOW, findings=[], warn=True, error="x")

    monkeypatch.setattr("jarvise_assist.partner_audit.guard_check", _unknown)
    av = AccountValue(value=D("1000"), wallet=D("1000"), unrealized=D("0"), open_positions=0, read_at_ms=NOW)
    text = pa._overview_text(conn, av, now_ms=NOW, futures=FakeClient())
    assert "มีเงินโอนที่ยังไม่บันทึก 2 รายการ" in text
    assert "ตัวเลข partner ยังไม่ถูกต้อง" in text
    conn.close()
