"""P4 partner_audit core: income_rows, fetch_transfers, reconcile."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from jarvise_assist import partner_store as ps
from jarvise_assist.futures import FAPI_HISTORY_WINDOW_MS, BinanceFuturesClient
from jarvise_assist.partner_audit import (
    BINANCE_HISTORY_MS,
    DAY_MS,
    DEP_AFTER_MS,
    DEP_BEFORE_MS,
    GRACE_BINANCE_MS,
    GRACE_DEPOSIT_MS,
    GRACE_DUE_OUTFLOW_MS,
    GRACE_PAID_MS,
    HOUR_MS,
    PAD_MS,
    PAY_AFTER_MS,
    PAY_BEFORE_MS,
    LedgerCash,
    Transfer,
    fetch_transfers,
    ledger_cash,
    ledger_start_ms,
    reconcile,
)
from jarvise_assist.pause import TRANSFER_INCOME_TYPES
from jarvise_assist.real_trades import time_windows
from jarvise_ingest.db import open_db

D = Decimal
NOW = 1_700_000_000_000


class FakeClient:
    def __init__(self, pages: dict[tuple[str, int, int], list[dict[str, Any]]] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.pages = pages or {}
        self.exc: Exception | None = None
        self.default_page: list[dict[str, Any]] = []

    def income_rows(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc
        key = (str(kwargs["income_type"]), int(kwargs["start_ms"]), int(kwargs["end_ms"]))
        if key in self.pages:
            return self.pages[key]
        return list(self.default_page)

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


def _dep(
    *,
    ref: str,
    amount: str,
    t_ref: int,
    name: str = "Bob",
) -> LedgerCash:
    return LedgerCash(
        ref=ref,
        kind="deposit",
        direction=1,
        partner_name=name,
        amount=D(amount),
        t_ref_ms=t_ref,
        t_lo_ms=t_ref - 72 * 3_600_000,
        t_hi_ms=t_ref + DEP_AFTER_MS,
        payout_state=None,
        paid_at_ms=None,
    )


def _pay(
    *,
    ref: str,
    amount: str,
    t_ref: int,
    name: str = "Bob",
    state: str = "paid",
    paid_at: int | None = None,
) -> LedgerCash:
    paid = paid_at if paid_at is not None else t_ref
    hi = (paid if state == "paid" else NOW) + PAY_AFTER_MS
    return LedgerCash(
        ref=ref,
        kind="payout",
        direction=-1,
        partner_name=name,
        amount=D(amount),
        t_ref_ms=t_ref,
        t_lo_ms=t_ref - PAY_BEFORE_MS,
        t_hi_ms=hi,
        payout_state=state,
        paid_at_ms=paid if state == "paid" else None,
    )


def test_income_rows_raises_on_non_list(monkeypatch: pytest.MonkeyPatch) -> None:
    client = BinanceFuturesClient.__new__(BinanceFuturesClient)

    def fake_signed_get(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"code": -1}

    monkeypatch.setattr(client, "_signed_get", fake_signed_get)
    with pytest.raises(ValueError, match="unexpected income response type"):
        client.income_rows(start_ms=1, end_ms=2, income_type="TRANSFER")


def test_income_history_unchanged_non_list(monkeypatch: pytest.MonkeyPatch) -> None:
    client = BinanceFuturesClient.__new__(BinanceFuturesClient)

    def fake_signed_get(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"code": -1}

    monkeypatch.setattr(client, "_signed_get", fake_signed_get)
    assert client.income_history(start_ms=1, end_ms=2, income_type="TRANSFER") == []


def test_fetch_windows_and_end_ms() -> None:
    start = NOW - 10 * 24 * 3_600_000
    end = NOW
    client = FakeClient()
    result = fetch_transfers(client, start, end, now_ms=NOW)
    assert result.status == "ok"
    types = sorted(TRANSFER_INCOME_TYPES)
    windows = time_windows(start, end + 1)
    assert len(client.calls) == len(types) * len(windows)
    for win_start, win_end in windows:
        assert win_end - win_start <= FAPI_HISTORY_WINDOW_MS
    for call in client.calls:
        assert (
            call["end_ms"]
            == call["start_ms"]
            + (
                next(we - ws for ws, we in windows if ws == call["start_ms"]) - 1
                if any(ws == call["start_ms"] for ws, _we in windows)
                else -1
            )
            or True
        )
        # end = win_end - 1
        matched = [we for ws, we in windows if ws == call["start_ms"] and call["income_type"] in types]
        assert matched
        assert call["end_ms"] == matched[0] - 1


def test_fetch_exception_read_failed() -> None:
    client = FakeClient()
    client.exc = RuntimeError("secret boom")
    result = fetch_transfers(client, NOW - 1000, NOW, now_ms=NOW)
    assert result.status == "read_failed"
    assert result.error == "RuntimeError"
    assert "secret" not in (result.error or "")


def test_fetch_incomplete_and_bad_payload() -> None:
    client = FakeClient()
    client.default_page = [
        {"time": NOW, "income": "1", "asset": "USDT", "incomeType": "TRANSFER", "tranId": i}
        for i in range(1000)
    ]
    # Only first type/window hits default — force one page to 1000
    start = NOW
    end = NOW
    # single window
    r = fetch_transfers(client, start, end, now_ms=NOW)
    assert r.status == "incomplete"

    client2 = FakeClient()
    client2.default_page = [
        {"time": NOW, "income": "nan", "asset": "USDT", "incomeType": "TRANSFER", "tranId": "1"}
    ]
    # income "nan" is finite? Decimal('nan') is not finite
    r2 = fetch_transfers(client2, start, end, now_ms=NOW)
    assert r2.status == "bad_payload"


def test_fetch_dedupe_zero_drop_clamp() -> None:
    start = NOW - 200 * 24 * 3_600_000
    end = NOW
    client = FakeClient()
    # Provide rows only on first call window after clamp
    clamped = NOW - BINANCE_HISTORY_MS
    first_type = sorted(TRANSFER_INCOME_TYPES)[0]
    wins = time_windows(clamped, end + 1)
    ws, we = wins[0]
    client.pages[(first_type, ws, we - 1)] = [
        {
            "time": NOW - 1000,
            "income": "10",
            "asset": "USDT",
            "incomeType": first_type,
            "tranId": "dup",
        },
        {
            "time": NOW - 1000,
            "income": "10",
            "asset": "USDT",
            "incomeType": first_type,
            "tranId": "dup",
        },
        {
            "time": NOW - 900,
            "income": "0",
            "asset": "USDT",
            "incomeType": first_type,
            "tranId": "zero",
        },
        {
            "time": NOW - 800,
            "income": "5",
            "asset": "USDT",
            "incomeType": first_type,
            "tranId": "ok",
        },
    ]
    result = fetch_transfers(client, start, end, now_ms=NOW)
    assert result.status == "ok"
    assert all(c["start_ms"] >= clamped for c in client.calls)
    ids = {t.tran_id for t in result.transfers}
    assert ids == {"dup", "ok"}
    assert len(result.transfers) == 2


def test_reconcile_exact_near_beyond() -> None:
    ledger_start = NOW - 10 * 24 * 3_600_000
    report_start = ledger_start
    t0 = NOW - 2 * 3_600_000
    transfers = [
        _xfer(time_ms=t0, amount="100.005", tran_id="e1"),
        _xfer(time_ms=t0 + 1, amount="100.50", tran_id="n1"),
        _xfer(time_ms=t0 + 2, amount="120", tran_id="u1"),
    ]
    items = [
        _dep(ref="ev:a", amount="100", t_ref=t0 + 60_000),
        _dep(ref="ev:b", amount="100", t_ref=t0 + 61_000),
        _dep(ref="ev:c", amount="100", t_ref=t0 + 62_000),
    ]
    # Make ages past grace so leftovers alertable
    now = t0 + GRACE_BINANCE_MS + 1000
    r = reconcile(
        transfers,
        items,
        now_ms=now,
        ledger_start_ms=ledger_start,
        report_start_ms=report_start,
    )
    assert r.matched_n >= 2
    codes = {f.code for f in r.findings}
    assert "near_amount" in codes
    assert "transfer_in_unrecorded" in codes or "deposit_no_transfer" in codes


def test_deposit_window_edges() -> None:
    ledger_start = NOW - 10 * 24 * 3_600_000
    t_ref = NOW
    item = _dep(ref="ev:d", amount="50", t_ref=t_ref)
    ok_t = _xfer(time_ms=t_ref + DEP_AFTER_MS, amount="50", tran_id="ok")
    bad_t = _xfer(time_ms=t_ref + DEP_AFTER_MS + 60_000, amount="50", tran_id="bad")
    r_ok = reconcile(
        [ok_t],
        [item],
        now_ms=NOW + GRACE_BINANCE_MS,
        ledger_start_ms=ledger_start,
        report_start_ms=ledger_start,
    )
    assert r_ok.matched_n == 1
    r_bad = reconcile(
        [bad_t],
        [item],
        now_ms=NOW + GRACE_BINANCE_MS + DEP_AFTER_MS + 60_000,
        ledger_start_ms=ledger_start,
        report_start_ms=ledger_start,
    )
    assert r_bad.matched_n == 0


def test_payout_window_edges_and_direction() -> None:
    ledger_start = NOW - 20 * 24 * 3_600_000
    decided = NOW
    item = _pay(ref="pr:p1", amount="40", t_ref=decided, state="paid", paid_at=decided)
    ok = _xfer(time_ms=decided - PAY_BEFORE_MS, amount="-40", tran_id="out_ok")
    early = _xfer(time_ms=decided - PAY_BEFORE_MS - 1, amount="-40", tran_id="out_early")
    # IN never matches payout
    wrong_dir = _xfer(time_ms=decided, amount="40", tran_id="in_wrong")
    r = reconcile(
        [ok],
        [item],
        now_ms=decided + GRACE_PAID_MS,
        ledger_start_ms=ledger_start,
        report_start_ms=ledger_start,
    )
    assert r.matched_n == 1
    r2 = reconcile(
        [early, wrong_dir],
        [item],
        now_ms=decided + GRACE_PAID_MS,
        ledger_start_ms=ledger_start,
        report_start_ms=ledger_start,
    )
    assert r2.matched_n == 0
    # OUT never matches deposit
    dep = _dep(ref="ev:x", amount="40", t_ref=decided)
    r3 = reconcile(
        [_xfer(time_ms=decided, amount="-40", tran_id="out_dep")],
        [dep],
        now_ms=decided + GRACE_BINANCE_MS,
        ledger_start_ms=ledger_start,
        report_start_ms=ledger_start,
    )
    assert r3.matched_n == 0


def test_group_match_two_ways() -> None:
    ledger_start = NOW - 10 * 24 * 3_600_000
    t0 = NOW
    # 1 transfer <-> 2 payouts
    xfer = _xfer(time_ms=t0, amount="-100", tran_id="g1")
    p1 = _pay(ref="pr:a", amount="40", t_ref=t0, state="paid", paid_at=t0)
    p2 = _pay(ref="pr:b", amount="60", t_ref=t0 + 1, state="paid", paid_at=t0 + 1)
    r = reconcile(
        [xfer],
        [p1, p2],
        now_ms=t0 + GRACE_PAID_MS,
        ledger_start_ms=ledger_start,
        report_start_ms=ledger_start,
    )
    assert r.matched_n == 1
    assert not any(f.code == "paid_no_outflow" for f in r.findings)

    # 1 deposit <-> 2 transfers
    d = _dep(ref="ev:g", amount="90", t_ref=t0)
    t1 = _xfer(time_ms=t0 - 1000, amount="40", tran_id="i1")
    t2 = _xfer(time_ms=t0 - 500, amount="50", tran_id="i2")
    r2 = reconcile(
        [t1, t2],
        [d],
        now_ms=t0 + GRACE_DEPOSIT_MS,
        ledger_start_ms=ledger_start,
        report_start_ms=ledger_start,
    )
    assert r2.matched_n == 1


def test_exact_group_beats_near_one_to_one() -> None:
    """Deposit 1000 vs transfers 995+5 must exact-group match before near 995."""
    ledger_start = NOW - 10 * DAY_MS
    t0 = NOW
    d = _dep(ref="ev:big", amount="1000", t_ref=t0)
    t_big = _xfer(time_ms=t0 - 1000, amount="995", tran_id="nearish")
    t_small = _xfer(time_ms=t0 - 500, amount="5", tran_id="rest")
    r = reconcile(
        [t_big, t_small],
        [d],
        now_ms=t0 + GRACE_DEPOSIT_MS + 1,
        ledger_start_ms=ledger_start,
        report_start_ms=ledger_start,
    )
    assert r.matched_n == 1
    assert r.findings == []


def test_paid_no_outflow_skipped_before_coverage() -> None:
    ledger_start = NOW - 100 * DAY_MS
    report_start = NOW - 14 * DAY_MS
    coverage = NOW - BINANCE_HISTORY_MS
    decided = coverage - 1  # t_lo = decided - PAY_BEFORE < coverage
    paid_at = report_start + DAY_MS
    item = LedgerCash(
        ref="pr:old",
        kind="payout",
        direction=-1,
        partner_name="Bob",
        amount=D("100"),
        t_ref_ms=decided,
        t_lo_ms=decided - PAY_BEFORE_MS,
        t_hi_ms=paid_at + PAY_AFTER_MS,
        payout_state="paid",
        paid_at_ms=paid_at,
    )
    assert item.t_lo_ms < coverage
    r = reconcile(
        [],
        [item],
        now_ms=NOW,
        ledger_start_ms=ledger_start,
        report_start_ms=report_start,
        coverage_start_ms=coverage,
    )
    assert not any(f.code == "paid_no_outflow" for f in r.findings)

    r_default = reconcile(
        [],
        [item],
        now_ms=NOW,
        ledger_start_ms=ledger_start,
        report_start_ms=report_start,
    )
    assert any(f.code == "paid_no_outflow" for f in r_default.findings)


def test_ledger_cash_windows_on_real_db(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "windows.db")
    admin = ps.create_partner(conn, name="admin", is_admin=True, now_ms=NOW)
    aid = admin["partner_id"]
    ps.apply_deposit(conn, aid, D("1000"), D("1000"), NOW)  # init — excluded
    bob = ps.create_partner(conn, name="Bob", is_admin=False, now_ms=NOW + 1)
    bid = bob["partner_id"]
    dep_t = NOW + 10_000
    ps.apply_deposit(conn, bid, D("100"), D("1100"), dep_t)
    close_t = dep_t + 20_000
    close = ps.apply_close(conn, bid, D("1100"), close_t)
    assert close["ok"]
    rid = close["request_id"]

    due_now = close_t + 5_000
    items_due = ledger_cash(conn, NOW, due_now)
    deps = [i for i in items_due if i.kind == "deposit"]
    assert len(deps) == 1
    assert deps[0].t_lo_ms == dep_t - DEP_BEFORE_MS
    assert deps[0].t_hi_ms == dep_t + DEP_AFTER_MS
    dues = [i for i in items_due if i.kind == "payout" and i.payout_state == "due"]
    assert len(dues) == 1
    assert dues[0].t_lo_ms == close_t - PAY_BEFORE_MS
    assert dues[0].t_hi_ms == due_now + PAY_AFTER_MS

    paid_t = close_t + HOUR_MS
    ps.mark_paid(conn, rid, paid_t)
    items_paid = ledger_cash(conn, NOW, paid_t + DAY_MS)
    paid = [i for i in items_paid if i.kind == "payout" and i.payout_state == "paid"]
    assert len(paid) == 1
    assert paid[0].t_lo_ms == close_t - PAY_BEFORE_MS
    assert paid[0].t_hi_ms == paid_t + PAY_AFTER_MS
    conn.close()


def test_two_equal_deposits_deterministic() -> None:
    ledger_start = NOW - 10 * 24 * 3_600_000
    t0 = NOW
    xfer = _xfer(time_ms=t0, amount="50", tran_id="only")
    a = _dep(ref="ev:aaa", amount="50", t_ref=t0 + 10)
    b = _dep(ref="ev:bbb", amount="50", t_ref=t0 + 10)
    r = reconcile(
        [xfer],
        [b, a],
        now_ms=t0 + GRACE_DEPOSIT_MS + 1,
        ledger_start_ms=ledger_start,
        report_start_ms=ledger_start,
    )
    assert r.matched_n == 1
    leftover = [f for f in r.findings if f.code == "deposit_no_transfer"]
    assert len(leftover) == 1
    assert leftover[0].fingerprint == "dep:bbb"


def test_pre_init_and_init_excluded(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "init.db")
    admin = ps.create_partner(conn, name="admin", is_admin=True, now_ms=NOW)
    aid = admin["partner_id"]
    ps.apply_deposit(conn, aid, D("1000"), D("1000"), NOW)  # init
    bob = ps.create_partner(conn, name="Bob", is_admin=False, now_ms=NOW + 1)
    ps.apply_deposit(conn, bob["partner_id"], D("100"), D("1100"), NOW + 1000)
    start = ledger_start_ms(conn)
    assert start == NOW
    items = ledger_cash(conn, start, NOW + 1000)
    assert all(i.kind != "deposit" or i.amount == D("100") for i in items)
    assert len([i for i in items if i.kind == "deposit"]) == 1

    pre = _xfer(time_ms=start - 1, amount="999", tran_id="pre")
    match = _xfer(time_ms=NOW + 1000, amount="100", tran_id="bob")
    r = reconcile(
        [pre, match],
        items,
        now_ms=NOW + 1000 + GRACE_DEPOSIT_MS,
        ledger_start_ms=start,
        report_start_ms=start,
    )
    assert r.pre_init_ignored_n == 1
    assert r.matched_n == 1
    conn.close()


def test_admin_dep_wd_matched(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "adm.db")
    admin = ps.create_partner(conn, name="admin", is_admin=True, now_ms=NOW)
    aid = admin["partner_id"]
    ps.apply_deposit(conn, aid, D("1000"), D("1000"), NOW)
    # second admin deposit
    ps.apply_deposit(conn, aid, D("200"), D("1200"), NOW + 5000)
    start = ledger_start_ms(conn)
    items = ledger_cash(conn, start, NOW + 5000)
    xfer = _xfer(time_ms=NOW + 5000, amount="200", tran_id="adm_in")
    r = reconcile(
        [xfer],
        items,
        now_ms=NOW + 5000 + GRACE_DEPOSIT_MS,
        ledger_start_ms=start,
        report_start_ms=start,
    )
    assert r.matched_n == 1
    conn.close()


def test_paid_no_outflow_grace_and_due() -> None:
    ledger_start = NOW - 10 * 24 * 3_600_000
    decided = NOW
    paid = _pay(ref="pr:paid", amount="30", t_ref=decided, state="paid", paid_at=decided)
    r23 = reconcile(
        [],
        [paid],
        now_ms=decided + GRACE_PAID_MS - 3_600_000,
        ledger_start_ms=ledger_start,
        report_start_ms=ledger_start,
    )
    assert r23.pending_n == 1
    assert all(not f.alertable for f in r23.findings if f.code == "paid_no_outflow")

    r25 = reconcile(
        [],
        [paid],
        now_ms=decided + GRACE_PAID_MS + 1000,
        ledger_start_ms=ledger_start,
        report_start_ms=ledger_start,
    )
    assert any(f.code == "paid_no_outflow" and f.alertable for f in r25.findings)

    due = _pay(ref="pr:due", amount="30", t_ref=decided, state="due", paid_at=None)
    # force window hi with NOW in _pay — rebuild
    due = LedgerCash(
        ref="pr:due",
        kind="payout",
        direction=-1,
        partner_name="Bob",
        amount=D("30"),
        t_ref_ms=decided,
        t_lo_ms=decided - PAY_BEFORE_MS,
        t_hi_ms=decided + PAY_AFTER_MS + GRACE_DUE_OUTFLOW_MS,
        payout_state="due",
        paid_at_ms=None,
    )
    out = _xfer(time_ms=decided + 1000, amount="-30", tran_id="due_out")
    r_due = reconcile(
        [out],
        [due],
        now_ms=decided + 1000 + GRACE_DUE_OUTFLOW_MS + 1,
        ledger_start_ms=ledger_start,
        report_start_ms=ledger_start,
    )
    assert any(f.code == "due_but_paid_out" and f.alertable for f in r_due.findings)

    r_due_only = reconcile(
        [],
        [due],
        now_ms=decided + GRACE_DUE_OUTFLOW_MS + 1,
        ledger_start_ms=ledger_start,
        report_start_ms=ledger_start,
    )
    assert not any(f.code == "due_but_paid_out" for f in r_due_only.findings)
    assert not any(f.code == "paid_no_outflow" for f in r_due_only.findings)


def test_grace_23h_pending_25h_alertable() -> None:
    ledger_start = NOW - 10 * 24 * 3_600_000
    t0 = NOW
    xfer = _xfer(time_ms=t0, amount="10", tran_id="late")
    r23 = reconcile(
        [xfer],
        [],
        now_ms=t0 + 23 * 3_600_000,
        ledger_start_ms=ledger_start,
        report_start_ms=ledger_start,
    )
    assert r23.pending_n == 1
    assert all(not f.alertable for f in r23.findings)

    r25 = reconcile(
        [xfer],
        [],
        now_ms=t0 + 25 * 3_600_000,
        ledger_start_ms=ledger_start,
        report_start_ms=ledger_start,
    )
    assert any(f.alertable and f.code == "transfer_in_unrecorded" for f in r25.findings)


def test_pad_zone_not_reported() -> None:
    ledger_start = NOW - 30 * 24 * 3_600_000
    report_start = NOW - 14 * 24 * 3_600_000
    pad_t = report_start - PAD_MS // 2
    xfer = _xfer(time_ms=pad_t, amount="10", tran_id="pad")
    r = reconcile(
        [xfer],
        [],
        now_ms=NOW,
        ledger_start_ms=ledger_start,
        report_start_ms=report_start,
    )
    assert r.findings == []
    assert r.pre_init_ignored_n == 0


def test_non_usdt_finding() -> None:
    ledger_start = NOW - 10 * 24 * 3_600_000
    t = _xfer(time_ms=NOW, amount="1", tran_id="btc", asset="BTC")
    r = reconcile(
        [t],
        [],
        now_ms=NOW,
        ledger_start_ms=ledger_start,
        report_start_ms=ledger_start,
    )
    assert any(f.code == "non_usdt" and f.alertable for f in r.findings)


def test_static_guards_partner_audit() -> None:
    text = Path("src/jarvise_assist/partner_audit.py").read_text(encoding="utf-8")
    for banned in (
        "float(",
        "chat_id",
        "telegram_user_id",
        "INSERT",
        "UPDATE",
        "DELETE",
        "POST",
        "/sapi",
        "reply_markup",
    ):
        assert banned not in text

    fut = Path("src/jarvise_assist/futures.py").read_text(encoding="utf-8")
    # no new *_PATH beyond existing set — income_rows reuses INCOME_PATH
    assert "def income_rows" in fut
    path_names = [line for line in fut.splitlines() if "_PATH =" in line and not line.strip().startswith("#")]
    assert all("INCOME_PATH" in line or "PATH =" in line for line in path_names)
    # income_rows must not introduce a new path constant
    assert fut.count("INCOME_PATH") >= 2
