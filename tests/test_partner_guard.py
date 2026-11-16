"""Partner P5 confirm-time transfer guard: guard_check + format_guard_th."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

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
from jarvise_ingest.db import open_db

D = Decimal
NOW = 1_700_000_000_000


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
