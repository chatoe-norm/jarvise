"""A2 one-off historical owner_deposit (2026-11-23 588.525 USDT)."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jarvise.cli import app
from jarvise_assist import partner_store as ps
from jarvise_assist.partner_audit import Transfer, ledger_cash, reconcile
from jarvise_assist.partner_history import (
    ADMIN_COST_MICRO as L1_ADMIN_COST,
)
from jarvise_assist.partner_history import (
    ADMIN_UNITS_E8 as L1_ADMIN_UNITS,
)
from jarvise_assist.partner_history import (
    EFF_DEFAULT_MS as L1_EFF,
)
from jarvise_assist.partner_history import (
    HIST_EVENT_ID as L1_HIST_ID,
)
from jarvise_assist.partner_history import (
    TOTO_COST_MICRO,
    TOTO_FEE_BPS,
    TOTO_UNITS_E8,
)
from jarvise_assist.partner_history import (
    apply_historical as apply_l1,
)
from jarvise_assist.partner_history_20261123 import (
    ACCOUNT_VALUE_AFTER,
    ADMIN_COST_AFTER_MICRO,
    ADMIN_COST_MICRO,
    ADMIN_UNITS_AFTER_E8,
    ADMIN_UNITS_E8,
    AMOUNT,
    AV_MICRO,
    CASH_MICRO,
    EFF_DEFAULT_MS,
    HIST_EVENT_ID,
    MINTED,
    NAV_Q,
    NOTE,
    TOTAL_UNITS_E8,
    TOTO_NET_AFTER_V_NOW,
    TOTO_NET_BEFORE_V_NOW,
    TRAN_ID,
    UNITS_DELTA_E8,
    V_NOW,
)
from jarvise_ingest.db import MIGRATIONS, SCHEMA_SQL, connect, open_db, schema_version

D = Decimal
runner = CliRunner()
T_DEP = 1_794_000_000_000
T_TOTO = 1_794_000_001_000


@pytest.fixture(autouse=True)
def _no_binance_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _open_before(path: Path, stop_before: int) -> sqlite3.Connection:
    conn = connect(path)
    conn.executescript(SCHEMA_SQL)
    current = schema_version(conn)
    for version, _name, fn, repair in MIGRATIONS:
        if version >= stop_before:
            break
        if version <= current:
            if repair:
                fn(conn)
            continue
        fn(conn)
        conn.execute(f"PRAGMA user_version = {int(version)}")
    conn.commit()
    return conn


def _seed_golden_v36(path: Path) -> sqlite3.Connection:
    conn = _open_before(path, 37)
    assert schema_version(conn) == 36
    conn.execute(
        """
        INSERT INTO partner (
            id, name, is_admin, fee_rate_bps, units_e8, cost_micro, status,
            created_at_ms, updated_at_ms
        ) VALUES
            ('pt_admin_l1', 'admin', 1, 0, ?, ?, 'active', ?, ?),
            ('pt_toto_l1', 'ToTo', 0, ?, ?, ?, 'active', ?, ?)
        """,
        (
            L1_ADMIN_UNITS,
            L1_ADMIN_COST,
            T_DEP,
            T_DEP,
            TOTO_FEE_BPS,
            TOTO_UNITS_E8,
            TOTO_COST_MICRO,
            T_TOTO,
            T_TOTO,
        ),
    )
    conn.execute(
        """
        INSERT INTO partner_event (
            id, partner_id, kind, account_value_micro, total_units_before_e8, nav,
            units_delta_e8, cost_delta_micro, cash_micro, fee_micro,
            units_after_e8, cost_after_micro, actor, created_at_ms
        ) VALUES
            ('pe_admin_l1', 'pt_admin_l1', 'deposit', ?, 0, '1',
             ?, ?, ?, 0, ?, ?, 'admin', ?),
            ('pe_toto_l1', 'pt_toto_l1', 'deposit', ?, ?, '1',
             ?, ?, ?, 0, ?, ?, 'admin', ?)
        """,
        (
            L1_ADMIN_COST,
            L1_ADMIN_UNITS,
            L1_ADMIN_COST,
            L1_ADMIN_COST,
            L1_ADMIN_UNITS,
            L1_ADMIN_COST,
            T_DEP,
            L1_ADMIN_COST + TOTO_COST_MICRO,
            L1_ADMIN_UNITS,
            TOTO_UNITS_E8,
            TOTO_COST_MICRO,
            TOTO_COST_MICRO,
            TOTO_UNITS_E8,
            TOTO_COST_MICRO,
            T_TOTO,
        ),
    )
    conn.commit()
    return conn


def _a2_ready(path: Path) -> sqlite3.Connection:
    """Schema 38 + L1 150 redeem applied → A2 pre-state."""
    conn = _seed_golden_v36(path)
    conn.close()
    conn = open_db(path)
    result = apply_l1(conn, L1_EFF, now_ms=L1_EFF)
    assert result["ok"] is True, result
    assert result["reason"] == "recorded"
    admin = ps.get_partner(conn, "pt_admin_l1")
    assert admin is not None
    assert int(admin["units_e8"]) == ADMIN_UNITS_E8
    assert int(admin["cost_micro"]) == ADMIN_COST_MICRO
    assert ps.units_to_e8(ps.total_units(conn)) == TOTAL_UNITS_E8
    assert conn.execute("SELECT 1 FROM partner_event WHERE id = ?", (L1_HIST_ID,)).fetchone()
    return conn


def _invoke(path: Path, *extra: str) -> object:
    return runner.invoke(
        app,
        ["assist", "partner-record-owner-deposit-20261123", "--db", str(path), *extra],
    )


def _json_out(result: object) -> dict:
    blob = (getattr(result, "stdout", "") or "") + (getattr(result, "stderr", "") or "")
    start = blob.find("{")
    end = blob.rfind("}")
    assert start >= 0 and end > start, blob
    return json.loads(blob[start : end + 1])


def test_a2_dry_run_golden_and_no_write(tmp_path: Path) -> None:
    path = tmp_path / "a2.db"
    conn = _a2_ready(path)
    before = ps.view_partner(conn, "pt_toto_l1", V_NOW)
    assert before["net"] == TOTO_NET_BEFORE_V_NOW
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    digest = _digest(path)

    dry = _invoke(path, "--json")
    assert dry.exit_code == 0, dry.stdout + dry.stderr
    payload = _json_out(dry)
    assert payload["ok"] is True
    assert payload["mode"] == "dry-run"
    assert payload["reason"] == "ready"
    golden = payload["golden"]
    assert golden["nav_pre_q"] == format(NAV_Q, "f")
    assert golden["nav_pre"].startswith("1.033046292003")
    assert golden["minted"] == format(MINTED, "f")
    assert golden["units_delta_e8"] == UNITS_DELTA_E8
    assert golden["admin_units_after_e8"] == ADMIN_UNITS_AFTER_E8
    assert golden["admin_cost_after_micro"] == ADMIN_COST_AFTER_MICRO
    assert golden["cash_micro"] == CASH_MICRO
    assert golden["account_value_micro"] == AV_MICRO
    assert golden["total_units_before_e8"] == TOTAL_UNITS_E8
    assert golden["tran_id"] == TRAN_ID
    assert abs(D(golden["nav_delta"])) < D("1e-9")
    assert _digest(path) == digest
    check = open_db(path)
    assert check.execute("SELECT 1 FROM partner_event WHERE id = ?", (HIST_EVENT_ID,)).fetchone() is None
    assert int(check.execute("SELECT units_e8 FROM partner WHERE id = 'pt_admin_l1'").fetchone()[0]) == (
        ADMIN_UNITS_E8
    )
    check.close()


def test_a2_apply_writes_pins_and_toto_net(tmp_path: Path) -> None:
    path = tmp_path / "apply.db"
    conn = _a2_ready(path)
    assert ps.view_partner(conn, "pt_toto_l1", V_NOW)["net"] == TOTO_NET_BEFORE_V_NOW
    conn.close()

    applied = _invoke(path, "--json", "--apply")
    assert applied.exit_code == 0, applied.stdout + applied.stderr
    body = _json_out(applied)
    assert body["reason"] == "recorded"
    assert body["golden"]["units_delta_e8"] == UNITS_DELTA_E8

    check = open_db(path)
    row = check.execute("SELECT * FROM partner_event WHERE id = ?", (HIST_EVENT_ID,)).fetchone()
    assert row is not None
    assert str(row["kind"]) == "owner_deposit"
    assert int(row["units_delta_e8"]) == UNITS_DELTA_E8
    assert int(row["units_after_e8"]) == ADMIN_UNITS_AFTER_E8
    assert int(row["cost_after_micro"]) == ADMIN_COST_AFTER_MICRO
    assert int(row["cost_delta_micro"]) == CASH_MICRO
    assert int(row["cash_micro"]) == CASH_MICRO
    assert int(row["fee_micro"]) == 0
    assert int(row["account_value_micro"]) == AV_MICRO
    assert int(row["total_units_before_e8"]) == TOTAL_UNITS_E8
    assert int(row["payouts_due_micro"]) == 0
    assert int(row["effective_at_ms"]) == EFF_DEFAULT_MS
    assert row["origin"] == "manual"
    assert row["actor"] == "admin"
    assert row["note"] == NOTE
    assert D(str(row["nav"])).quantize(D("1e-8")) == NAV_Q
    assert ps.verify_ledger(check)["ok"] is True
    admin = ps.get_partner(check, "pt_admin_l1")
    assert admin is not None
    assert int(admin["units_e8"]) == ADMIN_UNITS_AFTER_E8
    assert int(admin["cost_micro"]) == ADMIN_COST_AFTER_MICRO
    assert ps.view_partner(check, "pt_toto_l1", V_NOW)["net"] == TOTO_NET_AFTER_V_NOW
    assert ps.view_partner(check, "pt_toto_l1", ACCOUNT_VALUE_AFTER)["net"] == D("100.686234")

    items = ledger_cash(check, T_DEP, EFF_DEFAULT_MS + 60_000)
    audited = reconcile(
        [
            Transfer(
                time_ms=EFF_DEFAULT_MS,
                amount=AMOUNT,
                asset="USDT",
                income_type="TRANSFER",
                tran_id=TRAN_ID,
            )
        ],
        items,
        now_ms=EFF_DEFAULT_MS + 10**8,
        ledger_start_ms=T_DEP,
        report_start_ms=T_DEP,
    )
    assert not any(item.fingerprint == f"bx:TRANSFER:{TRAN_ID}" for item in audited.findings)
    check.close()


def test_a2_idempotent_conflict_window_schema(tmp_path: Path) -> None:
    path = tmp_path / "idemp.db"
    _a2_ready(path).close()
    assert _invoke(path, "--json", "--apply").exit_code == 0
    again = _invoke(path, "--json", "--apply")
    assert again.exit_code == 0
    assert _json_out(again)["reason"] == "already_recorded"

    tampered = tmp_path / "tamper.db"
    shutil.copy(path, tampered)
    raw = connect(tampered)
    raw.execute("DROP TRIGGER partner_event_append_only_update")
    raw.execute("DROP TRIGGER partner_event_append_only_delete")
    raw.execute("DELETE FROM partner_event WHERE id = ?", (HIST_EVENT_ID,))
    raw.execute(
        """
        INSERT INTO partner_event (
            id, partner_id, kind, account_value_micro, total_units_before_e8, nav,
            units_delta_e8, cost_delta_micro, cash_micro, fee_micro,
            units_after_e8, cost_after_micro, actor, note, created_at_ms,
            effective_at_ms, origin, payouts_due_micro
        ) VALUES (
            ?, 'pt_admin_l1', 'owner_deposit', 1, 1, '1',
            1, 1, 1, 0, 1, 1, 'admin', 'tampered', ?, ?, 'manual', 0
        )
        """,
        (HIST_EVENT_ID, EFF_DEFAULT_MS, EFF_DEFAULT_MS),
    )
    # Restore admin to post-apply so only the hist row conflicts.
    raw.execute(
        "UPDATE partner SET units_e8 = ?, cost_micro = ? WHERE id = 'pt_admin_l1'",
        (ADMIN_UNITS_AFTER_E8, ADMIN_COST_AFTER_MICRO),
    )
    raw.commit()
    raw.close()
    conflict = _invoke(tampered, "--json")
    assert conflict.exit_code == 3
    assert _json_out(conflict)["reason"] == "conflict"

    window = _invoke(path, "--json", "--effective-at-ms", "1")
    assert window.exit_code == 2

    stale = tmp_path / "stale.db"
    _seed_golden_v36(stale).close()
    mismatch = _invoke(stale, "--json")
    assert mismatch.exit_code == 2
    assert "schema mismatch" in ((mismatch.stdout or "") + (mismatch.stderr or ""))


def test_a2_pre_state_refusals_and_rollback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "base.db"
    _a2_ready(path).close()
    digest = _digest(path)

    def _refuse(setup, label: str) -> None:
        db = tmp_path / f"{label}.db"
        shutil.copy(path, db)
        setup(db)
        result = _invoke(db, "--json", "--apply")
        assert result.exit_code == 1, result.stdout + result.stderr
        body = _json_out(result)
        assert body["ok"] is False
        check = connect(db)
        assert check.execute("SELECT 1 FROM partner_event WHERE id = ?", (HIST_EVENT_ID,)).fetchone() is None
        admin_row = check.execute("SELECT units_e8 FROM partner WHERE id = 'pt_admin_l1'").fetchone()
        assert int(admin_row[0]) == ADMIN_UNITS_E8
        check.close()

    def units(db: Path) -> None:
        c = connect(db)
        c.execute("UPDATE partner SET units_e8 = units_e8 + 1 WHERE id = 'pt_toto_l1'")
        c.commit()
        c.close()

    def extra(db: Path) -> None:
        c = connect(db)
        c.execute(
            """
            INSERT INTO partner_event (
                id, partner_id, kind, account_value_micro, total_units_before_e8, nav,
                units_delta_e8, cost_delta_micro, cash_micro, fee_micro,
                units_after_e8, cost_after_micro, actor, created_at_ms, payouts_due_micro
            ) VALUES (
                'pe_extra_a2', 'pt_admin_l1', 'adjust_cost', 1, ?, '1',
                0, 0, 0, 0, ?, ?, 'admin', ?, 0
            )
            """,
            (TOTAL_UNITS_E8, ADMIN_UNITS_E8, ADMIN_COST_MICRO, T_DEP + 10),
        )
        c.commit()
        c.close()

    def pending(db: Path) -> None:
        c = connect(db)
        c.execute(
            """
            INSERT INTO partner_request (
                id, partner_id, kind, status, payout_state, created_at_ms, expires_at_ms
            ) VALUES ('pr_pending_a2', 'pt_toto_l1', 'deposit', 'pending', 'none', ?, ?)
            """,
            (T_DEP, EFF_DEFAULT_MS + 86_400_000),
        )
        c.commit()
        c.close()

    def due(db: Path) -> None:
        c = connect(db)
        c.execute(
            """
            INSERT INTO partner_request (
                id, partner_id, kind, status, payout_state, payout_micro,
                created_at_ms, expires_at_ms, decided_at_ms
            ) VALUES (
                'pr_due_a2', 'pt_toto_l1', 'withdraw_profit', 'confirmed', 'due', 1000000,
                ?, ?, ?
            )
            """,
            (T_DEP, T_DEP, T_DEP),
        )
        c.commit()
        c.close()

    _refuse(units, "units")
    _refuse(extra, "extra")
    _refuse(pending, "pending")
    _refuse(due, "due")
    assert _digest(path) == digest

    rolled = tmp_path / "rollback.db"
    shutil.copy(path, rolled)
    real = ps.verify_ledger
    calls = {"n": 0}

    def flaky(conn: sqlite3.Connection) -> dict:
        calls["n"] += 1
        # pre_state + apply pre-verify use real; fail on post-verify
        if calls["n"] >= 3:
            return {
                "ok": False,
                "mismatches": [{"invariant": "I2", "event_id": "x", "expected": 0, "actual": 1, "delta": 1}],
            }
        return real(conn)

    monkeypatch.setattr(ps, "verify_ledger", flaky)
    failed = _invoke(rolled, "--json", "--apply")
    assert failed.exit_code == 1
    check = connect(rolled)
    assert check.execute("SELECT 1 FROM partner_event WHERE id = ?", (HIST_EVENT_ID,)).fetchone() is None
    assert (
        int(check.execute("SELECT units_e8 FROM partner WHERE id = 'pt_admin_l1'").fetchone()[0])
        == ADMIN_UNITS_E8
    )
    check.close()
