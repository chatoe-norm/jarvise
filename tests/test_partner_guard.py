"""Partner P5 confirm-time transfer guard: guard_check + callbacks + isolation."""

from __future__ import annotations

import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from jarvise_assist import partner_admin as pa
from jarvise_assist import partner_portal as portal
from jarvise_assist import partner_store as ps
from jarvise_assist.partner_audit import (
    EXACT_TOL,
    GRACE_BINANCE_MS,
    HOUR_MS,
    NEAR_ABS,
    Finding,
    GuardResult,
    Transfer,
    format_guard_th,
    guard_check,
)
from jarvise_assist.partner_value import AccountValue, AccountValueError
from jarvise_ingest.db import open_db

D = Decimal
NOW = 1_700_000_000_000
ADMIN = "42"


class FakeClient:
    """Income + optional balance/position rows (audit FakeClient + value reads)."""

    def __init__(self, transfers: list[Transfer] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.transfers = list(transfers or [])
        self.exc: Exception | None = None
        self.incomplete = False
        self.bad_payload: list[Any] | None = None
        self.wallet: str | None = None
        self.unrealized: str = "0"
        self.balance_calls = 0

    def income_rows(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc
        if self.incomplete:
            return [
                {
                    "time": NOW,
                    "income": "1",
                    "asset": "USDT",
                    "incomeType": kwargs["income_type"],
                    "tranId": str(i),
                }
                for i in range(1000)
            ]
        if self.bad_payload is not None:
            return list(self.bad_payload)
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
        self.balance_calls += 1
        assert self.wallet is not None
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


def _xfer(
    *,
    time_ms: int,
    amount: str,
    tran_id: str,
    asset: str = "USDT",
    income_type: str = "TRANSFER",
) -> Transfer:
    return Transfer(
        time_ms=time_ms,
        amount=D(amount),
        asset=asset,
        income_type=income_type,
        tran_id=tran_id,
    )


def _init_ledger(conn: Any, *, wallet: str = "1000", now_ms: int = NOW) -> None:
    admin = ps.create_partner(conn, name="admin", is_admin=True, now_ms=now_ms)
    ps.apply_deposit(conn, admin["partner_id"], D(wallet), D(wallet), now_ms)


def test_guard_clean_ledger(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "clean.db")
    _init_ledger(conn)
    client = FakeClient()
    before = conn.total_changes
    state = tmp_path / "partner_audit_state.json"
    assert not state.exists()
    r = guard_check(conn, client, now_ms=NOW + 10_000)
    assert r.status == "ok"
    assert r.warn is False
    assert r.findings == []
    assert conn.total_changes == before
    assert not state.exists()
    conn.close()


def test_guard_unrecorded_inflow_inside_grace_warns(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "in.db")
    _init_ledger(conn)
    t_xfer = NOW + 2 * HOUR_MS
    client = FakeClient([_xfer(time_ms=t_xfer, amount="50", tran_id="orphan_in")])
    # age 2h < GRACE_BINANCE_MS (24h) — P4 would not alert; guard must warn
    now = t_xfer + 2 * HOUR_MS
    assert now - t_xfer < GRACE_BINANCE_MS
    r = guard_check(conn, client, now_ms=now)
    assert r.warn is True
    assert any(f.code == "transfer_in_unrecorded" for f in r.findings)
    text = format_guard_th(r, last_ok_ms=None)
    assert "ยังไม่อยู่ในสมุด" in text
    assert "ไม่พบรายการค้าง" not in text
    conn.close()


def test_guard_unrecorded_outflow_warns(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "out.db")
    _init_ledger(conn)
    t_xfer = NOW + HOUR_MS
    client = FakeClient([_xfer(time_ms=t_xfer, amount="-25", tran_id="orphan_out")])
    r = guard_check(conn, client, now_ms=t_xfer + HOUR_MS)
    assert r.warn is True
    assert any(f.code == "transfer_out_unrecorded" for f in r.findings)
    conn.close()


def test_guard_deposit_exact_match_clean(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "dep_ok.db")
    _init_ledger(conn)
    rid = "pr_0123456789abcdef"
    amt = D("100")
    t_xfer = NOW + HOUR_MS
    client = FakeClient([_xfer(time_ms=t_xfer, amount="100", tran_id="dep_exact")])
    r = guard_check(
        conn,
        client,
        now_ms=t_xfer + 60_000,
        deposit_request_id=rid,
        deposit_amount=amt,
        deposit_partner="Bob",
    )
    assert r.warn is False
    assert not any(
        f.code in ("this_deposit_not_seen", "this_deposit_amount_diff", "transfer_in_unrecorded")
        for f in r.findings
    )
    conn.close()


def test_guard_deposit_no_inflow_this_deposit_not_seen(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "dep_miss.db")
    _init_ledger(conn)
    rid = "pr_abcdef0123456789"
    r = guard_check(
        conn,
        FakeClient(),
        now_ms=NOW + HOUR_MS,
        deposit_request_id=rid,
        deposit_amount=D("100"),
        deposit_partner="Bob",
    )
    assert r.warn is True
    assert any(f.code == "this_deposit_not_seen" and f.fingerprint == f"dep:{rid}" for f in r.findings)
    text = format_guard_th(r, last_ok_ms=None)
    assert "ยังไม่เห็นเงินฝาก" in text
    assert "ไม่พบรายการค้าง" not in text
    conn.close()


def test_guard_deposit_near_amount_diff(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "dep_near.db")
    _init_ledger(conn)
    rid = "pr_1111222233334444"
    # within near tol but outside exact: 100 vs 100.50
    diff = D("0.50")
    assert diff > EXACT_TOL
    assert diff <= NEAR_ABS
    t_xfer = NOW + HOUR_MS
    client = FakeClient([_xfer(time_ms=t_xfer, amount="100.50", tran_id="near1")])
    r = guard_check(
        conn,
        client,
        now_ms=t_xfer + 60_000,
        deposit_request_id=rid,
        deposit_amount=D("100"),
        deposit_partner="Bob",
    )
    assert r.warn is True
    assert any(f.code == "this_deposit_amount_diff" for f in r.findings)
    text = format_guard_th(r, last_ok_ms=None)
    assert "ยอดเข้าจริงต่างจากที่จะบันทึก" in text
    assert "p dep Bob" in text
    conn.close()


def test_guard_other_partner_inflow_while_confirming_deposit(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "other.db")
    _init_ledger(conn)
    bob = ps.create_partner(conn, name="Bob", now_ms=NOW + 1)
    # recorded deposit for Bob already in ledger
    ps.apply_deposit(conn, bob["partner_id"], D("50"), D("1050"), NOW + 2)
    rid = "pr_9999888877776666"
    # confirming a new deposit for Carol, but orphan inflow remains
    t_xfer = NOW + 3 * HOUR_MS
    client = FakeClient(
        [
            _xfer(time_ms=NOW + 2, amount="50", tran_id="bob_dep"),  # matches Bob's ledger deposit
            _xfer(time_ms=t_xfer, amount="77", tran_id="orphan_other"),
        ]
    )
    r = guard_check(
        conn,
        client,
        now_ms=t_xfer + 60_000,
        deposit_request_id=rid,
        deposit_amount=D("100"),
        deposit_partner="Carol",
    )
    assert r.warn is True
    assert any(f.code == "transfer_in_unrecorded" for f in r.findings)
    assert any(f.code == "this_deposit_not_seen" for f in r.findings)
    conn.close()


def test_guard_verify_mismatch_warns(tmp_path: Path, monkeypatch: Any) -> None:
    conn = open_db(tmp_path / "verify.db")
    _init_ledger(conn)
    monkeypatch.setattr(
        "jarvise_assist.partner_audit.verify_ledger",
        lambda _c: {"ok": False, "mismatches": [{"partner": "x"}]},
    )
    r = guard_check(conn, FakeClient(), now_ms=NOW + 1000)
    assert r.warn is True
    assert any(f.code == "verify_mismatch" for f in r.findings)
    text = format_guard_th(r, last_ok_ms=None)
    assert "verify_ledger" in text
    conn.close()


def test_guard_non_usdt_only_no_warn(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "btc.db")
    _init_ledger(conn)
    t_xfer = NOW + HOUR_MS
    client = FakeClient([_xfer(time_ms=t_xfer, amount="1", tran_id="btc1", asset="BTC")])
    r = guard_check(conn, client, now_ms=t_xfer + 1000)
    assert r.warn is False
    assert any(f.code == "non_usdt" for f in r.findings)
    conn.close()


@pytest.mark.parametrize(
    "status,setup",
    [
        ("read_failed", "exc"),
        ("incomplete", "incomplete"),
        ("bad_payload", "bad"),
        ("no_client", "none"),
    ],
)
def test_guard_fetch_failures_warn(tmp_path: Path, status: str, setup: str) -> None:
    conn = open_db(tmp_path / f"fail_{status}.db")
    _init_ledger(conn)
    if setup == "none":
        client: FakeClient | None = None
    else:
        client = FakeClient()
        if setup == "exc":
            client.exc = ConnectionError("down")
        elif setup == "incomplete":
            client.incomplete = True
        else:
            client.bad_payload = ["not-a-dict"]
    r = guard_check(conn, client, now_ms=NOW + 1000)
    assert r.status == status
    assert r.warn is True
    text = format_guard_th(r, last_ok_ms=None)
    assert "ตรวจยอดโอนไม่ได้ตอนนี้" in text
    assert "ไม่พบรายการค้าง" not in text
    assert "ยังไม่เคยตรวจสำเร็จ" in text
    if status == "read_failed":
        assert "ConnectionError" in text
        assert "down" not in text
    conn.close()


def test_guard_not_init_zero_income_calls(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "empty.db")
    client = FakeClient()
    r = guard_check(conn, client, now_ms=NOW)
    assert r.status == "not_init"
    assert r.warn is False
    assert client.calls == []
    conn.close()


def test_guard_does_not_touch_state_or_db(tmp_path: Path) -> None:
    db = tmp_path / "ro.db"
    conn = open_db(db)
    _init_ledger(conn)
    state = Path(str(db)).with_name("partner_audit_state.json")
    state.write_text(
        json.dumps({"v": 1, "alerted": {}, "last_ok_ms": NOW, "fail_since_ms": None, "fail_alerted": False}),
        encoding="utf-8",
    )
    before_state = state.read_text(encoding="utf-8")
    mtime = state.stat().st_mtime_ns
    before_changes = conn.total_changes
    dump0 = list(conn.iterdump())
    client = FakeClient([_xfer(time_ms=NOW + HOUR_MS, amount="9", tran_id="x")])
    guard_check(conn, client, now_ms=NOW + 2 * HOUR_MS)
    assert conn.total_changes == before_changes
    assert list(conn.iterdump()) == dump0
    assert state.read_text(encoding="utf-8") == before_state
    assert state.stat().st_mtime_ns == mtime
    conn.close()


def test_format_guard_th_last_ok_line() -> None:
    r = GuardResult(status="no_client", checked_at_ms=NOW, findings=[], warn=True, error=None)
    text = format_guard_th(r, last_ok_ms=NOW)
    assert "ตรวจสำเร็จล่าสุด (รอบเช้า)" in text
    assert "ไม่มีคีย์อ่าน Binance" in text


def test_format_guard_max_lines() -> None:
    findings = [
        Finding(
            code="transfer_in_unrecorded",
            fingerprint=f"bx:TRANSFER:{i}",
            time_ms=NOW + i,
            amount=D("1"),
            partner_name=None,
            detail="",
            alertable=True,
        )
        for i in range(8)
    ]
    r = GuardResult(status="ok", checked_at_ms=NOW, findings=findings, warn=True, error=None)
    text = format_guard_th(r, last_ok_ms=None)
    assert "และอีก 2 รายการ" in text
    assert text.count("\n• ") <= 6


# ---- callback helpers --------------------------------------------------------


def _patch_read(monkeypatch: Any, client: FakeClient) -> None:
    def fake(cli: Any, *, now_ms: int, attempts: int = 3) -> AccountValue | AccountValueError:
        if cli is None:
            return AccountValueError(reason="no_client")
        if isinstance(cli, FakeClient) and cli.wallet is not None:
            w = D(cli.wallet)
            u = D(cli.unrealized)
            return AccountValue(
                wallet=w,
                unrealized=u,
                value=w + u,
                open_positions=0 if u == 0 else 1,
                read_at_ms=now_ms,
            )
        return AccountValueError(reason="no_client")

    monkeypatch.setattr(pa, "read_account_value", fake)
    monkeypatch.setattr(portal, "read_account_value", fake)


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


def _owner_cb(*, data: str, mid: int = 7, text: str = "preview") -> dict[str, Any]:
    return {
        "id": "cq1",
        "data": data,
        "from": {"id": 42},
        "message": {"message_id": mid, "chat": {"id": 42, "type": "private"}, "text": text},
    }


@pytest.fixture
def env_chat(monkeypatch: Any) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", ADMIN)


def _setup_bob_with_profit(conn: Any, *, now_ms: int = NOW) -> str:
    """Init admin + Bob deposit; Bob has profit at V≈2300. Returns bob partner id."""
    admin = ps.create_partner(conn, name="admin", is_admin=True, now_ms=now_ms)
    ps.apply_deposit(conn, admin["partner_id"], D("1000"), D("1000"), now_ms)
    bob = ps.create_partner(conn, name="Bob", now_ms=now_ms + 1)
    # 600 at V=1600 → 375 units; equity > cost when V > 2200
    ps.apply_deposit(conn, bob["partner_id"], D("600"), D("1600"), now_ms + 2)
    return str(bob["partner_id"])


def _bind_partner(conn: Any, partner_id: str, *, tg: str = "501", now_ms: int = NOW) -> None:
    """Bind partner to a Telegram chat (same pattern as portal tests)."""
    invite = ps.create_invite(conn, partner_id, now_ms=now_ms)
    assert invite.get("ok")
    bound = ps.bind_invite(conn, str(invite["code"]), tg, admin_chat_id=ADMIN, now_ms=now_ms + 1)
    assert bound.get("ok")


_PROFIT_V = "2300"


def _pa_cb(
    conn: Any,
    data: str,
    *,
    client: FakeClient,
    monkeypatch: Any,
    now_ms: int,
    text: str = "preview",
    mid: int = 7,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[Any]]:
    _patch_read(monkeypatch, client)
    sent, send = _send_capture()
    edited, edit = _edit_capture()
    answers, answer = _answer_capture()
    out = pa.handle_partner_callback(
        conn,
        data,
        callback=_owner_cb(data=data, mid=mid, text=text),
        send=send,
        answer=answer,
        edit=edit,
        futures=client,
        now_ms=now_ms,
    )
    return out, sent, edited, answers


# ---- callback tests (real guard) ---------------------------------------------


def test_cb_pa_ry_warn_blocks_confirm(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "ry_warn.db")
    bob_id = _setup_bob_with_profit(conn)
    created = ps.create_request(conn, bob_id, "convert", NOW + 10, source="admin")
    assert created.get("ok")
    rid = str(created["request_id"])
    t_xfer = NOW + 11
    client = FakeClient([_xfer(time_ms=t_xfer, amount="40", tran_id="orphan_ry")])
    client.wallet = _PROFIT_V
    n_income_before = 0
    out, sent, edited, answers = _pa_cb(
        conn, f"pa:ry:{rid}", client=client, monkeypatch=monkeypatch, now_ms=t_xfer + 60_000
    )
    assert out == {"ok": False, "reason": "unrecorded_transfer"}
    req = ps.get_request(conn, rid)
    assert req is not None and str(req["status"]) == "pending"
    assert req["payout_state"] in (None, "none") or str(req["payout_state"]) == "none"
    assert client.balance_calls == 0  # no _read_v
    assert edited
    assert "ยังไม่อยู่ในสมุด" in edited[0]["text"]
    kb = edited[0]["reply_markup"]["inline_keyboard"]
    flat = [b["callback_data"] for row in kb for b in row]
    assert not any(str(c).startswith("pa:rf:") for c in flat)
    assert any(str(c).startswith("pa:rn:") for c in flat)
    assert sent == []
    assert answers and "p owner" in (answers[0][1] or "")
    assert len(client.calls) > n_income_before
    conn.close()


def test_cb_pa_rf_unrecorded_still_refused(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    """B2: forged/force confirm cannot override unrecorded transfers on cv/wd/close."""
    conn = open_db(tmp_path / "rf_ok.db")
    bob_id = _setup_bob_with_profit(conn)
    created = ps.create_request(conn, bob_id, "convert", NOW + 10, source="admin")
    rid = str(created["request_id"])
    t_xfer = NOW + 11
    client = FakeClient([_xfer(time_ms=t_xfer, amount="40", tran_id="orphan_rf")])
    client.wallet = _PROFIT_V
    now = t_xfer + 60_000
    out1, _s1, edited1, _a1 = _pa_cb(conn, f"pa:ry:{rid}", client=client, monkeypatch=monkeypatch, now_ms=now)
    assert out1["reason"] == "unrecorded_transfer"
    stamp = str(now // 1000)
    out2, _sent2, _edited2, _a2 = _pa_cb(
        conn,
        f"pa:rf:{rid}:{stamp}",
        client=client,
        monkeypatch=monkeypatch,
        now_ms=now + 1000,
        text=edited1[0]["text"],
    )
    assert out2 == {"ok": False, "reason": "unrecorded_transfer"}
    assert str(ps.get_request(conn, rid)["status"]) == "pending"
    conn.close()


def test_cb_pa_rf_after_901_unrecorded_still_refused(
    tmp_path: Path, monkeypatch: Any, env_chat: None
) -> None:
    conn = open_db(tmp_path / "rf_old.db")
    bob_id = _setup_bob_with_profit(conn)
    created = ps.create_request(conn, bob_id, "convert", NOW + 10, source="admin")
    rid = str(created["request_id"])
    t_xfer = NOW + 11
    client = FakeClient([_xfer(time_ms=t_xfer, amount="40", tran_id="orphan_old")])
    client.wallet = _PROFIT_V
    now = t_xfer + 60_000
    out1, _s, edited1, _a = _pa_cb(conn, f"pa:ry:{rid}", client=client, monkeypatch=monkeypatch, now_ms=now)
    assert out1["reason"] == "unrecorded_transfer"
    old_stamp = now // 1000
    later = now + 901_000
    out2, _s2, edited2, _a2 = _pa_cb(
        conn,
        f"pa:rf:{rid}:{old_stamp}",
        client=client,
        monkeypatch=monkeypatch,
        now_ms=later,
        text=edited1[0]["text"],
    )
    assert out2 == {"ok": False, "reason": "unrecorded_transfer"}
    assert str(ps.get_request(conn, rid)["status"]) == "pending"
    flat = [b["callback_data"] for row in edited2[0]["reply_markup"]["inline_keyboard"] for b in row]
    assert not any(str(c).startswith("pa:rf:") for c in flat)
    conn.close()


@pytest.mark.parametrize(
    "data", ["pa:rf:not-an-id:1234567890", "pa:df:zz:1234567890", "pa:rf:pr_0123456789abcdef"]
)
def test_cb_malformed_force(tmp_path: Path, monkeypatch: Any, env_chat: None, data: str) -> None:
    conn = open_db(tmp_path / "mal.db")
    _init_ledger(conn)
    client = FakeClient()
    client.wallet = "1000"
    out, _s, _e, answers = _pa_cb(conn, data, client=client, monkeypatch=monkeypatch, now_ms=NOW + 1)
    assert out.get("ok") is False
    assert "รายการนี้ปิดไปแล้ว" in (answers[0][1] or "")
    conn.close()


def test_cb_clean_pa_ry_first_tap(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "ry_clean.db")
    bob_id = _setup_bob_with_profit(conn)
    created = ps.create_request(conn, bob_id, "convert", NOW + 10, source="admin")
    rid = str(created["request_id"])
    # Match Bob's recorded deposit so the convert confirm is clean
    client = FakeClient([_xfer(time_ms=NOW + 2, amount="600", tran_id="bob_dep_clean")])
    client.wallet = _PROFIT_V
    out, _s, edited, _a = _pa_cb(
        conn, f"pa:ry:{rid}", client=client, monkeypatch=monkeypatch, now_ms=NOW + 20
    )
    assert out.get("ok") is True
    assert "ตรวจยอดโอนแล้ว ไม่พบรายการค้าง" in edited[0]["text"]
    assert str(ps.get_request(conn, rid)["status"]) == "confirmed"
    conn.close()


def test_cb_double_tap_pa_ry_settled(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "ry_dbl.db")
    bob_id = _setup_bob_with_profit(conn)
    created = ps.create_request(conn, bob_id, "convert", NOW + 10, source="admin")
    rid = str(created["request_id"])
    client = FakeClient([_xfer(time_ms=NOW + 2, amount="600", tran_id="bob_dep_dbl")])
    client.wallet = _PROFIT_V
    _pa_cb(conn, f"pa:ry:{rid}", client=client, monkeypatch=monkeypatch, now_ms=NOW + 20)
    client.calls.clear()
    out2, _s2, edited2, answers2 = _pa_cb(
        conn, f"pa:ry:{rid}", client=client, monkeypatch=monkeypatch, now_ms=NOW + 30
    )
    assert out2.get("ok") is False
    assert out2.get("reason") == "request_not_pending"
    assert "รายการนี้ปิดไปแล้ว" in (answers2[0][1] or "")
    assert client.calls == []  # settled before guard → zero income_rows
    assert edited2  # settled view
    conn.close()


def test_cb_pa_dy_matching_inflow_confirms(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "dy_ok.db")
    _init_ledger(conn)
    bob = ps.create_partner(conn, name="Bob", now_ms=NOW + 1)
    created = ps.create_request(conn, bob["partner_id"], "deposit", NOW + 2, amount=D("100"), source="admin")
    rid = str(created["request_id"])
    t_xfer = NOW + 3
    client = FakeClient([_xfer(time_ms=t_xfer, amount="100", tran_id="dep_match")])
    client.wallet = "1100"
    out, sent, edited, _a = _pa_cb(
        conn, f"pa:dy:{rid}", client=client, monkeypatch=monkeypatch, now_ms=t_xfer + 30_000
    )
    assert out.get("ok") is True
    assert "บันทึกฝากแล้ว" in edited[0]["text"]
    assert "ตรวจยอดโอนแล้ว ไม่พบรายการค้าง" in edited[0]["text"]
    assert str(ps.get_request(conn, rid)["status"]) == "confirmed"
    partner_msgs = [s for s in sent if "chat_id" in s]
    assert all("ตรวจยอดโอน" not in s["text"] for s in partner_msgs)
    conn.close()


def test_cb_pa_dy_no_inflow_warns_no_event(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "dy_miss.db")
    _init_ledger(conn)
    bob = ps.create_partner(conn, name="Bob", now_ms=NOW + 1)
    n_ev = int(conn.execute("SELECT COUNT(*) AS n FROM partner_event").fetchone()["n"])
    created = ps.create_request(conn, bob["partner_id"], "deposit", NOW + 2, amount=D("100"), source="admin")
    rid = str(created["request_id"])
    client = FakeClient()
    client.wallet = "1100"
    out, _s, edited, _a = _pa_cb(conn, f"pa:dy:{rid}", client=client, monkeypatch=monkeypatch, now_ms=NOW + 3)
    assert out == {"ok": False, "reason": "guard_warn"}
    assert "ยังไม่เห็นเงินฝาก" in edited[0]["text"]
    assert int(conn.execute("SELECT COUNT(*) AS n FROM partner_event").fetchone()["n"]) == n_ev
    assert str(ps.get_request(conn, rid)["status"]) == "pending"
    conn.close()


def test_cb_guard_raising_warns_type_only(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "raise.db")
    bob_id = _setup_bob_with_profit(conn)
    created = ps.create_request(conn, bob_id, "convert", NOW + 10, source="admin")
    rid = str(created["request_id"])
    client = FakeClient()
    client.wallet = _PROFIT_V

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("secret detail must not leak")

    monkeypatch.setattr("jarvise_assist.partner_audit.guard_check", boom)
    out, _s, edited, _a = _pa_cb(
        conn, f"pa:ry:{rid}", client=client, monkeypatch=monkeypatch, now_ms=NOW + 20
    )
    assert out == {"ok": False, "reason": "unrecorded_transfer"}
    assert "RuntimeError" in edited[0]["text"]
    assert "secret detail" not in edited[0]["text"]
    assert str(ps.get_request(conn, rid)["status"]) == "pending"
    conn.close()


def test_cb_second_warning_replaces_marker_once(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "repl.db")
    bob_id = _setup_bob_with_profit(conn)
    created = ps.create_request(conn, bob_id, "convert", NOW + 10, source="admin")
    rid = str(created["request_id"])
    t_xfer = NOW + 11
    client = FakeClient([_xfer(time_ms=t_xfer, amount="40", tran_id="orphan_rep")])
    client.wallet = _PROFIT_V
    now = t_xfer + 60_000
    o1, _s1, edited1, _a1 = _pa_cb(conn, f"pa:ry:{rid}", client=client, monkeypatch=monkeypatch, now_ms=now)
    assert o1["reason"] == "unrecorded_transfer"
    assert edited1[0]["text"].count("⚠️ ตรวจยอดโอน") == 1
    o2, _s2, edited2, _a2 = _pa_cb(
        conn,
        f"pa:ry:{rid}",
        client=client,
        monkeypatch=monkeypatch,
        now_ms=now + 1000,
        text=edited1[0]["text"],
    )
    assert o2["reason"] == "unrecorded_transfer"
    assert edited2[0]["text"].count("\n\n⚠️ ตรวจยอดโอน") == 1
    assert edited2[0]["text"].count("⚠️ ตรวจยอดโอนก่อนยืนยัน") == 1
    conn.close()


def test_cb_portal_request_pa_ry_runs_guard(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "portal_ry.db")
    bob_id = _setup_bob_with_profit(conn)
    # bind Bob and create convert via portal
    invite = ps.create_invite(conn, bob_id, now_ms=NOW + 5)
    assert invite.get("ok")
    code = str(invite["code"])
    bound = ps.bind_invite(conn, code, "501", admin_chat_id=ADMIN, now_ms=NOW + 6)
    assert bound.get("ok")
    # Portal create must see a clean book; orphan arrives only at admin confirm.
    clean = FakeClient()
    clean.wallet = _PROFIT_V
    _patch_read(monkeypatch, clean)
    portal.reset_value_cache()
    sent, send = _send_capture()
    portal.handle_portal_callback(
        conn,
        "pp:go:cv",
        callback={
            "id": "cqp",
            "data": "pp:go:cv",
            "from": {"id": 501},
            "message": {"message_id": 9, "chat": {"id": 501, "type": "private"}, "text": "x"},
        },
        send=send,
        answer=_answer_capture()[1],
        edit=_edit_capture()[1],
        futures=clean,
        now_ms=NOW + 15,
    )
    admin_cards = [s for s in sent if "chat_id" not in s and "reply_markup" in s and s["reply_markup"]]
    assert admin_cards
    rid = admin_cards[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[-1]
    assert rid.startswith("pr_")
    orphan = FakeClient([_xfer(time_ms=NOW + 12, amount="33", tran_id="portal_orphan")])
    orphan.wallet = _PROFIT_V
    out, _s, edited, _a = _pa_cb(
        conn,
        f"pa:ry:{rid}",
        client=orphan,
        monkeypatch=monkeypatch,
        now_ms=NOW + 16,
        text=admin_cards[0]["text"],
    )
    assert out == {"ok": False, "reason": "unrecorded_transfer"}
    assert "ยังไม่อยู่ในสมุด" in edited[0]["text"]
    conn.close()


def test_cb_pa_dy_warn_then_df_override(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "dy_df.db")
    _init_ledger(conn)
    bob = ps.create_partner(conn, name="Bob", now_ms=NOW + 1)
    bob_id = str(bob["partner_id"])
    _bind_partner(conn, bob_id, tg="501", now_ms=NOW + 2)
    created = ps.create_request(conn, bob_id, "deposit", NOW + 4, amount=D("100"), source="admin")
    assert created.get("ok")
    rid = str(created["request_id"])
    client = FakeClient()
    client.wallet = "1100"
    now = NOW + 5
    out1, sent1, edited1, _a1 = _pa_cb(
        conn, f"pa:dy:{rid}", client=client, monkeypatch=monkeypatch, now_ms=now
    )
    assert out1 == {"ok": False, "reason": "guard_warn"}
    assert edited1[0]["reply_markup"]["inline_keyboard"] == [
        [{"text": "⚠️ ยืนยันทั้งที่มีเตือน", "callback_data": f"pa:df:{rid}:{now // 1000}"}],
        [{"text": "✖️ ยกเลิก", "callback_data": f"pa:dn:{rid}"}],
    ]
    assert sent1 == []
    stamp = now // 1000
    client.calls.clear()
    out2, sent2, edited2, _a2 = _pa_cb(
        conn,
        f"pa:df:{rid}:{stamp}",
        client=client,
        monkeypatch=monkeypatch,
        now_ms=now + 899_000,
        text=edited1[0]["text"],
    )
    assert out2.get("ok") is True
    assert client.calls == []
    assert "บันทึกฝากแล้ว" in edited2[0]["text"]
    assert "⚠️ ยืนยันทั้งที่มีคำเตือนยอดโอน" in edited2[0]["text"]
    partner_msgs = [s["text"] for s in sent2 if s.get("chat_id") == "501"]
    assert partner_msgs == ["✅ บันทึกเงินฝาก 100.00 USDT แล้ว · ทุนรวม 100.00 USDT"]
    n_ev = int(conn.execute("SELECT COUNT(*) AS n FROM partner_event").fetchone()["n"])
    out3, _s3, _e3, _a3 = _pa_cb(
        conn,
        f"pa:df:{rid}:{stamp}",
        client=client,
        monkeypatch=monkeypatch,
        now_ms=now + 899_000,
        text=edited2[0]["text"],
    )
    assert out3.get("ok") is False
    assert out3.get("reason") == "request_not_pending"
    assert int(conn.execute("SELECT COUNT(*) AS n FROM partner_event").fetchone()["n"]) == n_ev
    conn.close()


def test_cb_force_future_stamp_unrecorded_refused(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "rf_future.db")
    bob_id = _setup_bob_with_profit(conn)
    created = ps.create_request(conn, bob_id, "convert", NOW + 10, source="admin")
    assert created.get("ok")
    rid = str(created["request_id"])
    t_xfer = NOW + 11
    client = FakeClient([_xfer(time_ms=t_xfer, amount="40", tran_id="orphan_future")])
    client.wallet = _PROFIT_V
    now = t_xfer + 60_000
    stamp = now // 1000 + 120
    out, _s, _e, _a = _pa_cb(
        conn,
        f"pa:rf:{rid}:{stamp}",
        client=client,
        monkeypatch=monkeypatch,
        now_ms=now,
    )
    assert out == {"ok": False, "reason": "unrecorded_transfer"}
    assert client.calls != []
    assert str(ps.get_request(conn, rid)["status"]) == "pending"
    conn.close()


def test_cb_ry_unrecorded_bound_partner_gets_nothing(
    tmp_path: Path, monkeypatch: Any, env_chat: None
) -> None:
    conn = open_db(tmp_path / "ry_bound.db")
    bob_id = _setup_bob_with_profit(conn)
    _bind_partner(conn, bob_id, tg="501", now_ms=NOW + 3)
    created = ps.create_request(conn, bob_id, "withdraw_profit", NOW + 10, source="admin")
    assert created.get("ok")
    rid = str(created["request_id"])
    t_xfer = NOW + 11
    client = FakeClient([_xfer(time_ms=t_xfer, amount="40", tran_id="orphan_wd")])
    client.wallet = _PROFIT_V
    now = t_xfer + 60_000
    out1, sent1, edited1, _a1 = _pa_cb(
        conn, f"pa:ry:{rid}", client=client, monkeypatch=monkeypatch, now_ms=now
    )
    assert out1 == {"ok": False, "reason": "unrecorded_transfer"}
    assert sent1 == []
    stamp = str(now // 1000)
    client.calls.clear()
    out2, sent2, _edited2, _a2 = _pa_cb(
        conn,
        f"pa:rf:{rid}:{stamp}",
        client=client,
        monkeypatch=monkeypatch,
        now_ms=now + 1000,
        text=edited1[0]["text"],
    )
    assert out2 == {"ok": False, "reason": "unrecorded_transfer"}
    assert str(ps.get_request(conn, rid)["status"]) == "pending"
    assert sent2 == [] or all(s.get("chat_id") != "501" for s in sent2)
    conn.close()


def test_cb_refusal_expired_before_guard(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "ref_exp.db")
    _init_ledger(conn)
    bob = ps.create_partner(conn, name="Bob", now_ms=NOW + 1)
    created = ps.create_request(conn, bob["partner_id"], "deposit", NOW + 2, amount=D("100"), source="admin")
    assert created.get("ok")
    rid = str(created["request_id"])
    expires = int(created["expires_at_ms"])
    client = FakeClient()
    client.wallet = "1100"
    out_dy, _s1, _e1, _a1 = _pa_cb(
        conn,
        f"pa:dy:{rid}",
        client=client,
        monkeypatch=monkeypatch,
        now_ms=expires + 1,
    )
    assert out_dy == {"ok": False, "reason": "expired"}
    assert client.calls == []
    stale = (expires + 1) // 1000 - 1000
    out_df, _s2, _e2, _a2 = _pa_cb(
        conn,
        f"pa:df:{rid}:{stale}",
        client=client,
        monkeypatch=monkeypatch,
        now_ms=expires + 1,
    )
    assert out_df == {"ok": False, "reason": "expired"}
    assert client.calls == []
    conn.close()


def test_cb_refusal_dy_payout_due_before_guard(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "ref_dy_due.db")
    bob_id = _setup_bob_with_profit(conn)
    wd = ps.create_request(conn, bob_id, "withdraw_profit", NOW + 10, source="admin")
    assert wd.get("ok")
    assert ps.confirm_request(conn, str(wd["request_id"]), D(_PROFIT_V), NOW + 20).get("ok")
    carol = ps.create_partner(conn, name="Carol", now_ms=NOW + 25)
    created = ps.create_request(
        conn, carol["partner_id"], "deposit", NOW + 30, amount=D("100"), source="admin"
    )
    assert created.get("ok")
    rid = str(created["request_id"])
    client = FakeClient()
    client.wallet = _PROFIT_V
    out, _s, _e, _a = _pa_cb(
        conn,
        f"pa:dy:{rid}",
        client=client,
        monkeypatch=monkeypatch,
        now_ms=NOW + 40,
    )
    assert out == {"ok": False, "reason": "payout_due"}
    assert client.calls == []
    conn.close()


def test_cb_refusal_payout_due_before_guard(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "ref_due.db")
    bob_id = _setup_bob_with_profit(conn)
    wd = ps.create_request(conn, bob_id, "withdraw_profit", NOW + 10, source="admin")
    assert wd.get("ok")
    conf = ps.confirm_request(conn, str(wd["request_id"]), D(_PROFIT_V), NOW + 20)
    assert conf.get("ok")
    assert str(ps.get_request(conn, str(wd["request_id"]))["payout_state"]) == "due"
    created = ps.create_request(conn, bob_id, "convert", NOW + 30, source="admin")
    assert created.get("ok")
    rid = str(created["request_id"])
    client = FakeClient()
    client.wallet = _PROFIT_V
    stamp = (NOW + 40) // 1000
    out_ry, _s1, _e1, _a1 = _pa_cb(
        conn, f"pa:ry:{rid}", client=client, monkeypatch=monkeypatch, now_ms=NOW + 40
    )
    assert out_ry == {"ok": False, "reason": "payout_due"}
    assert client.calls == []
    out_rf, _s2, _e2, _a2 = _pa_cb(
        conn,
        f"pa:rf:{rid}:{stamp}",
        client=client,
        monkeypatch=monkeypatch,
        now_ms=NOW + 40,
    )
    assert out_rf == {"ok": False, "reason": "payout_due"}
    assert client.calls == []
    conn.close()


def test_cb_refusal_not_this_kind_before_guard(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "ref_kind.db")
    _init_ledger(conn)
    bob = ps.create_partner(conn, name="Bob", now_ms=NOW + 1)
    created = ps.create_request(conn, bob["partner_id"], "deposit", NOW + 2, amount=D("100"), source="admin")
    assert created.get("ok")
    rid = str(created["request_id"])
    client = FakeClient()
    client.wallet = "1100"
    stamp = (NOW + 3) // 1000
    out, _s, _e, _a = _pa_cb(
        conn,
        f"pa:rf:{rid}:{stamp}",
        client=client,
        monkeypatch=monkeypatch,
        now_ms=NOW + 3,
    )
    assert out == {"ok": False, "reason": "not_this_kind"}
    assert client.calls == []
    out_ry, _s2, _e2, _a2 = _pa_cb(
        conn,
        f"pa:ry:{rid}",
        client=client,
        monkeypatch=monkeypatch,
        now_ms=NOW + 3,
    )
    assert out_ry == {"ok": False, "reason": "not_this_kind"}
    assert client.calls == []
    conn.close()


def test_cb_read_failure_shows_last_ok(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    db = tmp_path / "read_fail.db"
    conn = open_db(db)
    bob_id = _setup_bob_with_profit(conn)
    created = ps.create_request(conn, bob_id, "convert", NOW + 10, source="admin")
    assert created.get("ok")
    rid = str(created["request_id"])
    state = Path(str(db)).with_name("partner_audit_state.json")
    state.write_text(
        json.dumps({"v": 1, "alerted": {}, "last_ok_ms": NOW, "fail_since_ms": None, "fail_alerted": False}),
        encoding="utf-8",
    )
    before_state = state.read_text(encoding="utf-8")
    mtime = state.stat().st_mtime_ns
    client = FakeClient()
    client.wallet = _PROFIT_V
    client.exc = ConnectionError("down")
    out, _s, edited, _a = _pa_cb(
        conn, f"pa:ry:{rid}", client=client, monkeypatch=monkeypatch, now_ms=NOW + 20
    )
    assert out == {"ok": False, "reason": "unrecorded_transfer"}
    text = edited[0]["text"]
    assert "ตรวจยอดโอนไม่ได้ตอนนี้" in text
    assert "ConnectionError" in text
    assert "ตรวจสำเร็จล่าสุด (รอบเช้า)" in text
    assert "down" not in text
    assert "ไม่พบรายการค้าง" not in text
    assert state.read_text(encoding="utf-8") == before_state
    assert state.stat().st_mtime_ns == mtime
    conn.close()


def test_cb_edit_fails_sends_one_card(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "edit_fail.db")
    bob_id = _setup_bob_with_profit(conn)
    created = ps.create_request(conn, bob_id, "convert", NOW + 10, source="admin")
    assert created.get("ok")
    rid = str(created["request_id"])
    t_xfer = NOW + 11
    client = FakeClient([_xfer(time_ms=t_xfer, amount="40", tran_id="orphan_edit")])
    client.wallet = _PROFIT_V
    _patch_read(monkeypatch, client)
    sent, send = _send_capture()
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
        return False

    answers, answer = _answer_capture()
    data = f"pa:ry:{rid}"
    out = pa.handle_partner_callback(
        conn,
        data,
        callback=_owner_cb(data=data, mid=7, text="preview"),
        send=send,
        answer=answer,
        edit=edit,
        futures=client,
        now_ms=t_xfer + 60_000,
    )
    assert out == {"ok": False, "reason": "unrecorded_transfer"}
    assert len(sent) == 1
    assert "chat_id" not in sent[0]
    flat = [b["callback_data"] for row in sent[0]["reply_markup"]["inline_keyboard"] for b in row]
    assert not any(str(c).startswith("pa:rf:") for c in flat)
    assert any(str(c).startswith(f"pa:rn:{rid}") for c in flat)
    conn.close()


# ---- isolation ---------------------------------------------------------------


def test_isolation_portal_does_not_load_partner_audit() -> None:
    import os

    repo = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo / "src")
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, jarvise_assist.partner_portal; assert 'jarvise_assist.partner_audit' not in sys.modules",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=str(repo),
        env=env,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_isolation_no_module_level_partner_audit_import() -> None:
    admin = Path("src/jarvise_assist/partner_admin.py").read_text(encoding="utf-8")
    for line in admin.splitlines():
        if not line or line[0] in " \t":
            continue
        assert "partner_audit" not in line or line.lstrip().startswith("#")
    # TYPE_CHECKING block may mention partner_audit indented — already skipped by column-0 check
    portal_text = Path("src/jarvise_assist/partner_portal.py").read_text(encoding="utf-8")
    for line in portal_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("from ") or stripped.startswith("import "):
            assert "partner_audit" not in stripped
            assert "guard" not in stripped
