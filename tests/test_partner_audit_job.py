"""P4 partner_audit job: dedupe state, fail/recovery, read-only DB."""

from __future__ import annotations

import json
import threading
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from jarvise_assist import partner_store as ps
from jarvise_assist.partner_audit import (
    BINANCE_HISTORY_MS,
    DAY_MS,
    FAIL_ALERT_AFTER_MS,
    GRACE_BINANCE_MS,
    HOUR_MS,
    Transfer,
    audit_report,
    export_events_csv,
    fetch_transfers,
    load_state,
    remove_open_unrecorded_for_tran,
    run_partner_audit_job,
    save_state,
)
from jarvise_assist.partner_json_store import (
    consume_owner_preview,
    issue_owner_preview,
    revoke_owner_preview,
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
    dump0 = list(conn.iterdump())
    conn.execute("PRAGMA query_only = ON")
    r1 = run_partner_audit_job(conn, client=client, now_ms=now, send=send_ok, state_path=state)
    assert r1["ok"] is True
    assert r1["new"] == 1
    assert r1["alert_sent"] is True
    assert len(sent) == 1
    assert "รายการใหม่" in sent[0]
    assert list(conn.iterdump()) == dump0

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
    dump0 = list(conn.iterdump())
    conn.execute("PRAGMA query_only = ON")
    r1 = run_partner_audit_job(conn, client=client, now_ms=now, send=send, state_path=state)
    assert r1["ok"] is True
    assert r1["new"] == 0
    assert sent == []
    assert ps.verify_ledger(conn)["ok"] is True
    assert list(conn.iterdump()) == dump0

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
    assert list(conn.iterdump()) == dump0
    conn.close()


def _late_mark_paid_scenario(tmp_path: Path, *, lag_days: int) -> tuple[Any, FakeClient, int, Path]:
    """Init + Bob deposit (early) + close at T + outflow + late mark_paid."""
    db = tmp_path / f"late_{lag_days}.db"
    conn = open_db(db)
    t0 = NOW
    admin = ps.create_partner(conn, name="admin", is_admin=True, now_ms=t0)
    ps.apply_deposit(conn, admin["partner_id"], D("1000"), D("1000"), t0)
    bob = ps.create_partner(conn, name="Bob", is_admin=False, now_ms=t0 + 1)
    bid = bob["partner_id"]
    # Deposit well before T-72h so late-day fetch extension does not orphan it
    dep_t = t0 + DAY_MS
    ps.apply_deposit(conn, bid, D("100"), D("1100"), dep_t)
    t_close = t0 + 10 * DAY_MS
    close = ps.apply_close(conn, bid, D("1100"), t_close)
    assert close["ok"]
    rid = close["request_id"]
    out_t = t_close + HOUR_MS
    paid_t = out_t + lag_days * DAY_MS
    ps.mark_paid(conn, rid, paid_t)
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
    state = tmp_path / f"late_{lag_days}_state.json"
    return conn, client, paid_t, state


def test_late_mark_paid_no_false_paid_no_outflow(tmp_path: Path) -> None:
    for lag in (5, 1):
        conn, client, paid_t, state = _late_mark_paid_scenario(tmp_path, lag_days=lag)
        sent: list[str] = []

        def send(text: str, _sent: list[str] = sent) -> int:
            _sent.append(text)
            return 1

        day12_result: dict[str, Any] | None = None
        for day in range(22):
            now = paid_t + HOUR_MS + day * DAY_MS
            result = run_partner_audit_job(conn, client=client, now_ms=now, send=send, state_path=state)
            if lag == 5 and day == 12:
                day12_result = result
        assert sent == [], f"lag={lag}d unexpected sends: {sent}"

        if lag == 5:
            assert day12_result is not None
            assert day12_result["matched"] == 1
            day12 = paid_t + HOUR_MS + 12 * DAY_MS
            report = audit_report(conn, client, now_ms=day12, days=14)
            assert report.status == "ok"
            assert not any(f.code == "paid_no_outflow" for f in report.findings)
        conn.close()


def test_early_paid_no_outflow_alerts(tmp_path: Path) -> None:
    """Close at NOW+2d, mark_paid close+1h, no outflow → one กดโอนแล้ว after grace."""
    db = tmp_path / "early_paid.db"
    conn = open_db(db)
    t0 = NOW
    admin = ps.create_partner(conn, name="admin", is_admin=True, now_ms=t0)
    ps.apply_deposit(conn, admin["partner_id"], D("1000"), D("1000"), t0)
    bob = ps.create_partner(conn, name="Bob", is_admin=False, now_ms=t0 + 1)
    bid = bob["partner_id"]
    dep_t = t0 + 60_000
    ps.apply_deposit(conn, bid, D("100"), D("1100"), dep_t)
    t_close = t0 + 2 * DAY_MS
    close = ps.apply_close(conn, bid, D("1100"), t_close)
    assert close["ok"]
    paid_t = t_close + HOUR_MS
    ps.mark_paid(conn, close["request_id"], paid_t)
    client = FakeClient(
        [
            Transfer(
                time_ms=dep_t,
                amount=D("100"),
                asset="USDT",
                income_type="TRANSFER",
                tran_id="bob_in",
            )
        ]
    )
    state = tmp_path / "early_paid_state.json"
    sent: list[str] = []

    def send(text: str) -> int:
        sent.append(text)
        return 1

    for day in range(3):
        run_partner_audit_job(
            conn,
            client=client,
            now_ms=paid_t + HOUR_MS + day * DAY_MS,
            send=send,
            state_path=state,
        )
    assert len(sent) == 1
    assert "กดโอนแล้ว" in sent[0]
    conn.close()


def test_late_paid_no_outflow_fetch_extension_alerts(tmp_path: Path) -> None:
    """Close+10d, mark_paid +20d after close+1h, no outflow → fetch extension still alerts."""
    db = tmp_path / "late_paid_alert.db"
    conn = open_db(db)
    t0 = NOW
    admin = ps.create_partner(conn, name="admin", is_admin=True, now_ms=t0)
    ps.apply_deposit(conn, admin["partner_id"], D("1000"), D("1000"), t0)
    bob = ps.create_partner(conn, name="Bob", is_admin=False, now_ms=t0 + 1)
    bid = bob["partner_id"]
    dep_t = t0 + DAY_MS
    ps.apply_deposit(conn, bid, D("100"), D("1100"), dep_t)
    t_close = t0 + 10 * DAY_MS
    close = ps.apply_close(conn, bid, D("1100"), t_close)
    assert close["ok"]
    # Outflow time close+1h is NOT added — proves unpaid outflow alert via fetch extension.
    paid_t = t_close + HOUR_MS + 20 * DAY_MS
    ps.mark_paid(conn, close["request_id"], paid_t)
    client = FakeClient(
        [
            Transfer(
                time_ms=dep_t,
                amount=D("100"),
                asset="USDT",
                income_type="TRANSFER",
                tran_id="bob_in",
            )
        ]
    )
    state = tmp_path / "late_paid_alert_state.json"
    sent: list[str] = []

    def send(text: str) -> int:
        sent.append(text)
        return 1

    for day in range(3):
        run_partner_audit_job(
            conn,
            client=client,
            now_ms=paid_t + HOUR_MS + day * DAY_MS,
            send=send,
            state_path=state,
        )
    assert len(sent) == 1
    assert "กดโอนแล้ว" in sent[0]
    conn.close()


def test_paid_beyond_binance_history_no_alert(tmp_path: Path) -> None:
    """mark_paid 95d after close+1h, no outflow → coverage older than history → zero sends."""
    db = tmp_path / "beyond_hist.db"
    conn = open_db(db)
    t0 = NOW
    admin = ps.create_partner(conn, name="admin", is_admin=True, now_ms=t0)
    ps.apply_deposit(conn, admin["partner_id"], D("1000"), D("1000"), t0)
    bob = ps.create_partner(conn, name="Bob", is_admin=False, now_ms=t0 + 1)
    bid = bob["partner_id"]
    dep_t = t0 + DAY_MS
    ps.apply_deposit(conn, bid, D("100"), D("1100"), dep_t)
    t_close = t0 + 10 * DAY_MS
    close = ps.apply_close(conn, bid, D("1100"), t_close)
    assert close["ok"]
    paid_t = t_close + HOUR_MS + 95 * DAY_MS
    assert paid_t - (t_close - 3 * DAY_MS) > BINANCE_HISTORY_MS
    ps.mark_paid(conn, close["request_id"], paid_t)
    client = FakeClient(
        [
            Transfer(
                time_ms=dep_t,
                amount=D("100"),
                asset="USDT",
                income_type="TRANSFER",
                tran_id="bob_in",
            )
        ]
    )
    state = tmp_path / "beyond_hist_state.json"
    sent: list[str] = []

    def send(text: str) -> int:
        sent.append(text)
        return 1

    for day in range(3):
        run_partner_audit_job(
            conn,
            client=client,
            now_ms=paid_t + HOUR_MS + day * DAY_MS,
            send=send,
            state_path=state,
        )
    assert sent == []
    conn.close()


def test_job_verify_ledger_fail_alerts_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "verify.db"
    conn = open_db(db)
    admin = ps.create_partner(conn, name="admin", is_admin=True, now_ms=NOW)
    ps.apply_deposit(conn, admin["partner_id"], D("1000"), D("1000"), NOW)
    monkeypatch.setattr(
        "jarvise_assist.partner_audit.verify_ledger",
        lambda _conn: {"ok": False, "mismatches": [{"x": 1}]},
    )
    client = FakeClient()
    state = tmp_path / "verify_state.json"
    sent: list[str] = []

    def send(text: str) -> int:
        sent.append(text)
        return 1

    r1 = run_partner_audit_job(conn, client=client, now_ms=NOW + DAY_MS, send=send, state_path=state)
    assert r1["ok"] is True
    assert len(sent) == 1
    assert "verify_ledger" in sent[0]
    r2 = run_partner_audit_job(conn, client=client, now_ms=NOW + DAY_MS + 1000, send=send, state_path=state)
    assert r2["new"] == 0
    assert len(sent) == 1
    conn.close()


def test_audit_report_and_csv_read_only(tmp_path: Path) -> None:
    db = tmp_path / "ro.db"
    conn = open_db(db)
    admin = ps.create_partner(conn, name="admin", is_admin=True, now_ms=NOW)
    ps.apply_deposit(conn, admin["partner_id"], D("1000"), D("1000"), NOW)
    bob = ps.create_partner(conn, name="Bob", is_admin=False, now_ms=NOW + 1)
    dep_t = NOW + 10_000
    ps.apply_deposit(conn, bob["partner_id"], D("100"), D("1100"), dep_t)
    client = FakeClient(
        [
            Transfer(
                time_ms=dep_t,
                amount=D("100"),
                asset="USDT",
                income_type="TRANSFER",
                tran_id="bob_in",
            )
        ]
    )
    dump0 = list(conn.iterdump())
    conn.execute("PRAGMA query_only = ON")
    report = audit_report(conn, client, now_ms=dep_t + GRACE_BINANCE_MS, days=14)
    assert report.status == "ok"
    csv_path = tmp_path / "events.csv"
    export_events_csv(conn, csv_path)
    assert csv_path.is_file()
    assert list(conn.iterdump()) == dump0
    conn.close()


def test_fetch_no_client() -> None:
    r = fetch_transfers(None, NOW - 1000, NOW, now_ms=NOW)
    assert r.status == "no_client"


def _blocking_send(gate: threading.Event, entered: threading.Event) -> Any:
    def send(text: str) -> int:
        entered.set()
        assert gate.wait(timeout=5), "send gate was not released"
        return 1

    return send


def _assert_preview_ops_while_send_blocked(
    *,
    preview_path: Path,
    entered: threading.Event,
    gate: threading.Event,
    job_thread: threading.Thread,
    errors: list[BaseException],
) -> None:
    assert entered.wait(timeout=2), "audit send never started"
    preview_done = threading.Event()
    preview_errors: list[BaseException] = []

    def do_preview_ops() -> None:
        try:
            token = issue_owner_preview(
                preview_path,
                side="dep",
                tran_id="live",
                amount_micro=10_000_000,
                issued_at_ms=NOW,
            )
            taken = consume_owner_preview(preview_path, token, now_ms=NOW + 1)
            assert taken is not None
            assert taken["tran_id"] == "live"
            assert revoke_owner_preview(preview_path, token, now_ms=NOW + 1) is True
        except BaseException as exc:  # noqa: BLE001
            preview_errors.append(exc)
        finally:
            preview_done.set()

    pt = threading.Thread(target=do_preview_ops)
    pt.start()
    try:
        assert preview_done.wait(timeout=1.5), "preview ops blocked by audit send under lock"
        pt.join(timeout=2)
        assert not preview_errors
        assert job_thread.is_alive(), "audit finished before preview ops (send not held?)"
    finally:
        gate.set()
        job_thread.join(timeout=5)
        pt.join(timeout=2)
    assert not job_thread.is_alive()
    assert not errors


def _seed_partner_db(db: Path) -> None:
    conn = open_db(db)
    admin = ps.create_partner(conn, name="admin", is_admin=True, now_ms=NOW)
    ps.apply_deposit(conn, admin["partner_id"], D("1000"), D("1000"), NOW)
    conn.close()


def test_audit_fail_send_does_not_block_preview_ops(tmp_path: Path) -> None:
    db = tmp_path / "fail_lock.db"
    _seed_partner_db(db)
    state = tmp_path / "partner_audit_state.json"
    save_state(
        state,
        {
            "v": 1,
            "alerted": {},
            "open_unrecorded": [],
            "last_ok_ms": NOW,
            "fail_since_ms": NOW,
            "fail_alerted": False,
        },
    )
    preview_path = tmp_path / "partner_owner_preview.json"
    client = FakeClient()
    client.fail = True
    gate = threading.Event()
    entered = threading.Event()
    errors: list[BaseException] = []
    result: dict[str, Any] = {}

    def run() -> None:
        conn = open_db(db)
        try:
            result.update(
                run_partner_audit_job(
                    conn,
                    client=client,
                    now_ms=NOW + FAIL_ALERT_AFTER_MS,
                    send=_blocking_send(gate, entered),
                    state_path=state,
                )
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            conn.close()

    t = threading.Thread(target=run)
    t.start()
    _assert_preview_ops_while_send_blocked(
        preview_path=preview_path,
        entered=entered,
        gate=gate,
        job_thread=t,
        errors=errors,
    )
    assert result.get("ok") is False
    assert result.get("alert_sent") is True
    assert load_state(state)["fail_alerted"] is True


def test_audit_new_alert_send_does_not_block_preview_ops(tmp_path: Path) -> None:
    db = tmp_path / "alert_lock.db"
    _seed_partner_db(db)
    state = tmp_path / "partner_audit_state.json"
    preview_path = tmp_path / "partner_owner_preview.json"
    t_xfer = NOW + GRACE_BINANCE_MS + 1000
    client = FakeClient(
        [
            Transfer(
                time_ms=t_xfer,
                amount=D("50"),
                asset="USDT",
                income_type="TRANSFER",
                tran_id="orphan_lock",
            )
        ]
    )
    gate = threading.Event()
    entered = threading.Event()
    errors: list[BaseException] = []
    result: dict[str, Any] = {}
    now = t_xfer + GRACE_BINANCE_MS + 1

    def run() -> None:
        conn = open_db(db)
        try:
            result.update(
                run_partner_audit_job(
                    conn,
                    client=client,
                    now_ms=now,
                    send=_blocking_send(gate, entered),
                    state_path=state,
                )
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            conn.close()

    t = threading.Thread(target=run)
    t.start()
    _assert_preview_ops_while_send_blocked(
        preview_path=preview_path,
        entered=entered,
        gate=gate,
        job_thread=t,
        errors=errors,
    )
    assert result.get("ok") is True
    assert result.get("alert_sent") is True
    assert "bx:TRANSFER:orphan_lock" in load_state(state)["alerted"]


def test_audit_recovery_send_does_not_block_preview_ops(tmp_path: Path) -> None:
    db = tmp_path / "recovery_lock.db"
    _seed_partner_db(db)
    state = tmp_path / "partner_audit_state.json"
    save_state(
        state,
        {
            "v": 1,
            "alerted": {},
            "open_unrecorded": [],
            "last_ok_ms": None,
            "fail_since_ms": NOW,
            "fail_alerted": True,
        },
    )
    preview_path = tmp_path / "partner_owner_preview.json"
    client = FakeClient()
    gate = threading.Event()
    entered = threading.Event()
    errors: list[BaseException] = []
    result: dict[str, Any] = {}

    def run() -> None:
        conn = open_db(db)
        try:
            result.update(
                run_partner_audit_job(
                    conn,
                    client=client,
                    now_ms=NOW + DAY_MS,
                    send=_blocking_send(gate, entered),
                    state_path=state,
                )
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            conn.close()

    t = threading.Thread(target=run)
    t.start()
    _assert_preview_ops_while_send_blocked(
        preview_path=preview_path,
        entered=entered,
        gate=gate,
        job_thread=t,
        errors=errors,
    )
    assert result.get("ok") is True
    assert result.get("alert_sent") is True
    assert load_state(state)["fail_alerted"] is False


def test_audit_finalize_preserves_concurrent_open_unrecorded_prune(tmp_path: Path) -> None:
    db = tmp_path / "finalize_prune.db"
    _seed_partner_db(db)
    state = tmp_path / "partner_audit_state.json"
    t_xfer = NOW + GRACE_BINANCE_MS + 1000
    client = FakeClient(
        [
            Transfer(
                time_ms=t_xfer,
                amount=D("50"),
                asset="USDT",
                income_type="TRANSFER",
                tran_id="keep_open",
            ),
            Transfer(
                time_ms=t_xfer + 1,
                amount=D("25"),
                asset="USDT",
                income_type="TRANSFER",
                tran_id="booked_now",
            ),
        ]
    )
    gate = threading.Event()
    entered = threading.Event()
    wrote_open = threading.Event()
    errors: list[BaseException] = []
    result: dict[str, Any] = {}
    now = t_xfer + GRACE_BINANCE_MS + 1

    # Poll until the pre-send locked update has written open_unrecorded.
    def watch_open() -> None:
        try:
            pause = threading.Event()
            for _ in range(100):
                if state.exists():
                    open_list = load_state(state).get("open_unrecorded") or []
                    if "bx:TRANSFER:booked_now" in open_list and "bx:TRANSFER:keep_open" in open_list:
                        wrote_open.set()
                        return
                pause.wait(0.02)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def run() -> None:
        conn = open_db(db)
        try:
            result.update(
                run_partner_audit_job(
                    conn,
                    client=client,
                    now_ms=now,
                    send=_blocking_send(gate, entered),
                    state_path=state,
                )
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            conn.close()

    watcher = threading.Thread(target=watch_open)
    t = threading.Thread(target=run)
    watcher.start()
    t.start()
    assert entered.wait(timeout=2), "audit send never started"
    assert wrote_open.wait(timeout=2), "open_unrecorded not written before send"
    remove_open_unrecorded_for_tran(state, "booked_now")
    assert "bx:TRANSFER:booked_now" not in (load_state(state).get("open_unrecorded") or [])
    gate.set()
    t.join(timeout=5)
    watcher.join(timeout=5)
    assert not t.is_alive()
    assert not errors
    assert result.get("alert_sent") is True
    final = load_state(state)
    assert "bx:TRANSFER:booked_now" not in final["open_unrecorded"]
    assert "bx:TRANSFER:keep_open" in final["open_unrecorded"]
    assert "bx:TRANSFER:keep_open" in final["alerted"]
    assert "bx:TRANSFER:booked_now" in final["alerted"]
