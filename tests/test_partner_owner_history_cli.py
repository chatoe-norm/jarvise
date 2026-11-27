"""Generic historical owner dep/wd CLI + adjustable live max age (PR #400 follow-up)."""

from __future__ import annotations

import hashlib
import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.test_partner_owner_tx_b1 import FakeClient, _owner_cb, _owner_msg, _xfer
from typer.testing import CliRunner

from jarvise.cli import app
from jarvise_assist import partner_admin as pa
from jarvise_assist import partner_owner_settings as pos
from jarvise_assist import partner_store as ps
from jarvise_assist import partner_unrecorded as pu
from jarvise_ingest.db import open_db

D = Decimal
HOUR = 3_600_000
DAY = 86_400_000
ADMIN = "42"
TRAN = "900000000001"
runner = CliRunner()

EVENT_COLS = (
    "kind",
    "account_value_micro",
    "total_units_before_e8",
    "nav",
    "units_delta_e8",
    "cost_delta_micro",
    "cash_micro",
    "fee_micro",
    "units_after_e8",
    "cost_after_micro",
    "payouts_due_micro",
    "effective_at_ms",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: Any) -> None:
    monkeypatch.delenv(pos.OWNER_TX_MAX_AGE_HOURS_ENV, raising=False)
    monkeypatch.delenv(pos.OWNER_HIST_MAX_LOOKBACK_DAYS_ENV, raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", ADMIN)


def _now() -> int:
    return int(time.time() * 1000)


def _make_db(path: Path, *, wallet: str = "1000", init_ms: int | None = None) -> Path:
    conn = open_db(path)
    try:
        t = _now() - 3 * DAY if init_ms is None else init_ms
        admin = ps.create_partner(conn, name="admin", is_admin=True, now_ms=t)
        assert ps.apply_deposit(conn, str(admin["partner_id"]), D(wallet), D(wallet), t)["ok"]
    finally:
        conn.close()
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _owner_rows(path: Path) -> list[Any]:
    conn = open_db(path)
    try:
        return conn.execute(
            "SELECT * FROM partner_event WHERE kind IN ('owner_deposit','owner_redeem') ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def _invoke(path: Path, *extra: str) -> Any:
    return runner.invoke(app, ["assist", "partner-record-owner-tx", "--db", str(path), "--json", *extra])


def _args(kind: str, amount: str, av: str, eff: int, tran: str = TRAN) -> list[str]:
    return [
        "--kind",
        kind,
        "--tran-id",
        tran,
        "--amount",
        amount,
        "--account-value",
        av,
        "--effective-at-ms",
        str(eff),
    ]


def _json(result: Any) -> dict[str, Any]:
    blob = (getattr(result, "stdout", "") or "") + (getattr(result, "stderr", "") or "")
    start, end = blob.find("{"), blob.rfind("}")
    assert start >= 0 and end > start, blob
    return json.loads(blob[start : end + 1])


# ---------------------------------------------------------------- settings


def test_owner_tx_max_age_setting_default_bounds_and_invalid(monkeypatch: Any) -> None:
    assert pos.owner_tx_max_age_hours() == 6
    assert pos.owner_tx_max_age_ms() == 6 * HOUR
    for raw, want in (
        ("12", 12),
        ("0", 1),
        ("-5", 1),
        ("999", 48),
        ("abc", 6),
        ("  ", 6),
        ("1", 1),
        ("48", 48),
    ):
        monkeypatch.setenv(pos.OWNER_TX_MAX_AGE_HOURS_ENV, raw)
        assert pos.owner_tx_max_age_hours() == want, raw
    assert pos.owner_hist_max_lookback_days() == 30
    for raw, want in (("60", 60), ("0", 1), ("9999", 365), ("x", 30)):
        monkeypatch.setenv(pos.OWNER_HIST_MAX_LOOKBACK_DAYS_ENV, raw)
        assert pos.owner_hist_max_lookback_days() == want, raw
    with pytest.raises(ValueError):
        pos.bounded_int_env("X_UNUSED", default=0, lo=1, hi=2)


def test_hist_hint_names_the_command() -> None:
    assert "jarvise assist partner-record-owner-tx" in pa._OWNER_HIST_HINT
    assert "ย้อนหลัง" in pa._OWNER_HIST_HINT
    assert "ลองใหม่" not in pa._OWNER_HIST_HINT


def _live_preview(conn: Any, client: FakeClient, text: str, now_ms: int) -> tuple[dict[str, Any], list[str]]:
    sent: list[str] = []
    out = pa.handle_partner_text(
        conn,
        text,
        message=_owner_msg(),
        send=lambda t, **k: sent.append(t),
        futures=client,
        now_ms=now_ms,
    )
    return out, sent


@pytest.mark.parametrize(
    "env,age_h,want_ok,shown_h",
    [(None, 7, False, 6), ("12", 13, False, 12), ("12", 7, True, None), ("2", 3, False, 2)],
)
def test_live_age_uses_setting_and_text_shows_real_value(
    tmp_path: Path, monkeypatch: Any, env: str | None, age_h: int, want_ok: bool, shown_h: int | None
) -> None:
    if env is not None:
        monkeypatch.setenv(pos.OWNER_TX_MAX_AGE_HOURS_ENV, env)
    now = _now()
    path = _make_db(tmp_path / "age.db", init_ms=now - 3 * DAY)
    conn = open_db(path)
    try:
        client = FakeClient([_xfer(time_ms=now - age_h * HOUR, amount="-80", tran_id="age1")], wallet="920")
        out, sent = _live_preview(conn, client, "p owner wd 80", now)
        assert bool(out.get("ok")) is want_ok, (out, sent)
        if shown_h is not None:
            assert sent[-1] == f"เงินโอนเก่าเกิน {shown_h} ชม. — {pa._OWNER_HIST_HINT}"
            assert "partner-record-owner-tx" in sent[-1]
    finally:
        conn.close()


# ---------------------------------------------------------------- CLI


def test_cli_wd_8dp_dry_run_then_apply_books_like_live_confirm(tmp_path: Path, monkeypatch: Any) -> None:
    removed: list[str] = []
    real_remove = pu.remove_booked_tran
    monkeypatch.setattr(
        pu, "remove_booked_tran", lambda conn, tid: (removed.append(tid), real_remove(conn, tid))
    )
    now = _now()
    eff = now - 7 * HOUR  # older than the live 6 h limit
    path = _make_db(tmp_path / "hist.db")
    before = _sha(path)

    dry = _invoke(path, *_args("wd", "80.00000000", "920", eff))
    assert dry.exit_code == 0, dry.output
    p = _json(dry)
    assert p["ok"] is True and p["reason"] == "would_record" and p["mode"] == "dry-run"
    assert p["kind"] == "owner_redeem" and p["event_id"] == f"pe_owner_tran{TRAN}"
    assert p["amount"] == "80.000000"
    assert D(p["plan"]["nav_pre"]) == D("1")
    assert D(p["plan"]["units_delta"]) == D("-80")
    assert D(p["plan"]["cost_after"]) == D("920")
    assert _sha(path) == before  # dry-run never writes
    assert _owner_rows(path) == []
    assert removed == []

    app_ = _invoke(path, *_args("wd", "80.00000000", "920", eff), "--apply")
    assert app_.exit_code == 0, app_.output
    a = _json(app_)
    assert a["ok"] is True and a["reason"] == "recorded" and a["verify"] == {"ok": True}
    assert D(a["result"]["burned_units"]) == D("80")
    assert D(a["result"]["units_after"]) == D(p["plan"]["units_after"])  # preview == booked
    assert D(a["result"]["cost_after"]) == D(p["plan"]["cost_after"])
    assert removed == [TRAN]
    rows = _owner_rows(path)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == f"pe_owner_tran{TRAN}"
    assert row["kind"] == "owner_redeem"
    assert int(row["cash_micro"]) == -80_000_000
    assert int(row["effective_at_ms"]) == eff
    assert f"tranId {TRAN}" in str(row["note"])

    # Same booking as the live p owner confirm on an identical ledger (same V, amount, time).
    live_path = _make_db(tmp_path / "live.db")
    conn = open_db(live_path)
    try:
        client = FakeClient([_xfer(time_ms=eff, amount="-80.00000000", tran_id=TRAN)], wallet="920")
        sent: list[dict[str, Any]] = []
        out = pa.handle_partner_text(
            conn,
            "p owner wd 80",
            message=_owner_msg(),
            send=lambda t, **k: sent.append({"text": t, **k}),
            futures=client,
            now_ms=eff + HOUR,
        )
        assert out["ok"] is True, (out, sent)
        cb = sent[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        res = pa.handle_partner_callback(
            conn,
            cb,
            callback=_owner_cb(cb),
            send=lambda *a, **k: None,
            answer=lambda *a, **k: None,
            edit=lambda *a, **k: True,
            futures=client,
            now_ms=eff + HOUR + 1000,
        )
        assert res["ok"] is True, res
    finally:
        conn.close()
    live_rows = _owner_rows(live_path)
    assert len(live_rows) == 1
    assert live_rows[0]["id"] == row["id"]
    for col in EVENT_COLS:
        assert live_rows[0][col] == row[col], col


def test_cli_conflict_exit_3_on_retry_and_after_live_booking(tmp_path: Path) -> None:
    now = _now()
    eff = now - 7 * HOUR
    path = _make_db(tmp_path / "dup.db")
    assert _invoke(path, *_args("wd", "80", "920", eff), "--apply").exit_code == 0
    sha = _sha(path)
    for extra in ((), ("--apply",)):
        r = _invoke(path, *_args("wd", "80", "920", eff), *extra)
        assert r.exit_code == 3, r.output
        assert _json(r)["reason"] == "conflict"
        # same tranId on the other side is a conflict too
        r2 = _invoke(path, *_args("dep", "80", "1000", eff), *extra)
        assert r2.exit_code == 3, r2.output
    assert _sha(path) == sha
    assert len(_owner_rows(path)) == 1

    # booked earlier by a different path (note carries the tranId, other event id)
    other = _make_db(tmp_path / "other.db")
    conn = open_db(other)
    try:
        assert ps.apply_owner_redeem(
            conn,
            amount=D("80"),
            account_value=D("920"),
            now_ms=eff + 1,
            effective_at_ms=eff,
            note=f"historical one-off; Binance TRANSFER tranId {TRAN}",
        )["ok"]
    finally:
        conn.close()
    r3 = _invoke(other, *_args("wd", "80", "920", eff + 10))
    assert r3.exit_code == 3 and _json(r3)["reason"] == "conflict"


def test_cli_dep_8dp_apply(tmp_path: Path) -> None:
    now = _now()
    eff = now - 2 * DAY
    path = _make_db(tmp_path / "dep.db", wallet="400")
    r = _invoke(path, *_args("dep", "100.00000000", "500", eff, tran="dep77"), "--apply")
    assert r.exit_code == 0, r.output
    a = _json(r)
    assert a["kind"] == "owner_deposit" and D(a["result"]["units_issued"]) == D("100")
    rows = _owner_rows(path)
    assert len(rows) == 1 and rows[0]["kind"] == "owner_deposit"
    assert int(rows[0]["cash_micro"]) == 100_000_000


@pytest.mark.parametrize(
    "args,reason",
    [
        (("wd", "80.0000001", "920", -7 * HOUR), "bad_amount"),
        (("wd", "0", "920", -7 * HOUR), "bad_amount"),
        (("wd", "abc", "920", -7 * HOUR), "bad_amount"),
        (("wd", "80", "0", -7 * HOUR), "bad_account_value"),
        (("wd", "80", "nan", -7 * HOUR), "bad_account_value"),
        (("xx", "80", "920", -7 * HOUR), "bad_kind"),
        (("wd", "80", "920", +HOUR), "effective_in_future"),
        (("wd", "80", "920", -31 * DAY), "effective_too_old"),
        (("wd", "1000000", "920", -7 * HOUR), "amount_too_large"),
    ],
)
def test_cli_bad_input_exit_2_no_write(tmp_path: Path, args: tuple[str, str, str, int], reason: str) -> None:
    path = _make_db(tmp_path / "bad.db")
    sha = _sha(path)
    kind, amount, av, rel = args
    for extra in ((), ("--apply",)):
        r = _invoke(path, *_args(kind, amount, av, _now() + rel), *extra)
        assert r.exit_code == 2, r.output
        assert _json(r)["reason"] == reason
    assert _sha(path) == sha


def test_cli_bad_tran_id_and_lookback_setting(tmp_path: Path, monkeypatch: Any) -> None:
    path = _make_db(tmp_path / "lb.db", init_ms=_now() - 90 * DAY)
    r = _invoke(path, *_args("wd", "80", "920", _now() - HOUR, tran="bad id!"))
    assert r.exit_code == 2 and _json(r)["reason"] == "bad_tran_id"
    eff = _now() - 40 * DAY
    r2 = _invoke(path, *_args("wd", "80", "920", eff))
    assert r2.exit_code == 2 and _json(r2)["max_lookback_days"] == 30
    monkeypatch.setenv(pos.OWNER_HIST_MAX_LOOKBACK_DAYS_ENV, "60")
    r3 = _invoke(path, *_args("wd", "80", "920", eff))
    assert r3.exit_code == 0, r3.output


def test_cli_refuses_when_ledger_moved_after_transfer(tmp_path: Path) -> None:
    now = _now()
    path = _make_db(tmp_path / "late.db", init_ms=now - 3 * DAY)
    eff = now - 4 * DAY  # before the initial deposit event
    sha = _sha(path)
    for extra in ((), ("--apply",)):
        r = _invoke(path, *_args("wd", "80", "920", eff), *extra)
        assert r.exit_code == 1, r.output
        assert _json(r)["reason"] == "events_after_effective"
    assert _sha(path) == sha


def test_store_refuse_events_after_inside_transaction(tmp_path: Path) -> None:
    now = _now()
    path = _make_db(tmp_path / "tx.db", init_ms=now - DAY)
    conn = open_db(path)
    try:
        out = ps.apply_owner_redeem(
            conn,
            amount=D("10"),
            account_value=D("990"),
            now_ms=now,
            effective_at_ms=now - 2 * DAY,
            note="tranId tx1",
            refuse_events_after_ms=now - 2 * DAY,
        )
        assert out == {"ok": False, "reason": "events_after_effective"}
        # default (live path) unchanged: no such check
        ok = ps.apply_owner_redeem(
            conn, amount=D("10"), account_value=D("990"), now_ms=now, note="tranId tx2"
        )
        assert ok["ok"] is True
    finally:
        conn.close()


def test_plan_owner_tx_matches_apply_and_never_writes(tmp_path: Path) -> None:
    now = _now()
    path = _make_db(tmp_path / "plan.db")
    conn = open_db(path)
    try:
        n0 = conn.execute("SELECT COUNT(*) FROM partner_event").fetchone()[0]
        plan = ps.plan_owner_tx(
            conn, kind="owner_redeem", amount=D("80.12345600"), account_value=D("919.876544"), now_ms=now
        )
        assert plan["ok"] is True
        assert conn.execute("SELECT COUNT(*) FROM partner_event").fetchone()[0] == n0
        assert ps.plan_owner_tx(conn, kind="nope", amount=D("1"), account_value=D("1"), now_ms=now) == {
            "ok": False,
            "reason": "bad_args",
        }
        assert (
            ps.plan_owner_tx(
                conn, kind="owner_redeem", amount=D("1.0000001"), account_value=D("1"), now_ms=now
            )["reason"]
            == "bad_amount"
        )
        res = ps.apply_owner_redeem(
            conn, amount=D("80.12345600"), account_value=D("919.876544"), now_ms=now, note="tranId p1"
        )
        assert res["ok"] is True
        for key in ("nav_pre", "units_after", "cost_after", "cash", "burned_units"):
            assert res[key] == plan[key], key
    finally:
        conn.close()


def test_cli_schema_mismatch_and_missing_db(tmp_path: Path) -> None:
    path = _make_db(tmp_path / "schema.db")
    import sqlite3

    raw = sqlite3.connect(path)
    raw.execute("PRAGMA user_version = 1")
    raw.commit()
    raw.close()
    r = _invoke(path, *_args("wd", "80", "920", _now() - HOUR))
    assert r.exit_code == 2 and "schema mismatch" in r.output
    r2 = _invoke(tmp_path / "nope.db", *_args("wd", "80", "920", _now() - HOUR))
    assert r2.exit_code == 2


def test_cli_pending_deposit_refused(tmp_path: Path) -> None:
    now = _now()
    path = _make_db(tmp_path / "pend.db")
    conn = open_db(path)
    try:
        bob = ps.create_partner(conn, name="bob", now_ms=now - DAY)
        assert ps.create_request(
            conn, str(bob["partner_id"]), "deposit", now - HOUR, amount=D("50"), source="admin"
        )["ok"]
    finally:
        conn.close()
    r = _invoke(path, *_args("wd", "80", "920", now - 2 * HOUR))
    assert r.exit_code == 1 and _json(r)["reason"] == "deposit_pending"
