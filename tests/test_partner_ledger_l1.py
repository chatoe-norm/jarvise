"""Partner ledger L1: migration 37, owner flows, verify invariants, historical redeem."""

from __future__ import annotations

import hashlib
import random
import shutil
import sqlite3
import threading
import time
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jarvise.cli import app
from jarvise_assist import partner_rules
from jarvise_assist import partner_store as ps
from jarvise_assist.partner_admin import HELP_TEXT, _latest_nav
from jarvise_assist.partner_audit import (
    Finding,
    GuardResult,
    Transfer,
    format_alert_th,
    format_guard_th,
    ledger_cash,
    reconcile,
)
from jarvise_assist.partner_history import (
    ADMIN_COST_AFTER,
    ADMIN_COST_MICRO,
    ADMIN_UNITS_AFTER,
    ADMIN_UNITS_AFTER_E8,
    ADMIN_UNITS_E8,
    AMOUNT,
    AV_MICRO,
    BURN,
    CASH_MICRO,
    EFF_DEFAULT_MS,
    HIST_EVENT_ID,
    NAV_Q,
    TOTAL_UNITS_E8,
    TOTO_AFTER,
    TOTO_BEFORE,
    TOTO_COST_MICRO,
    TOTO_FEE_BPS,
    TOTO_UNITS_E8,
    UNITS_DELTA_E8,
    compute_golden,
)
from jarvise_ingest.db import (
    MIGRATIONS,
    SCHEMA_SQL,
    SCHEMA_VERSION,
    _migrate_partner_ledger_owner_flows,
    _partner_event_checksum,
    connect,
    migrate,
    open_db,
    schema_version,
)

D = Decimal
UNIT = D("0.00000001")
runner = CliRunner()
T_DEP = 1_794_000_000_000
T_TOTO = 1_794_000_001_000


@pytest.fixture(autouse=True)
def _no_binance_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)


def _digest(path: Path) -> str:
    """Hash the main database file. A read-only open may create an empty ``-wal``."""
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
            ADMIN_UNITS_E8,
            ADMIN_COST_MICRO,
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
            ADMIN_COST_MICRO,
            ADMIN_UNITS_E8,
            ADMIN_COST_MICRO,
            ADMIN_COST_MICRO,
            ADMIN_UNITS_E8,
            ADMIN_COST_MICRO,
            T_DEP,
            ADMIN_COST_MICRO + TOTO_COST_MICRO,
            ADMIN_UNITS_E8,
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


def _invoke(path: Path, *extra: str) -> object:
    return runner.invoke(
        app,
        ["assist", "partner-record-owner-redeem-20261119", "--db", str(path), *extra],
    )


def test_history_apply_open_db_error_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "open_db_fail.db"
    conn, _, _ = _golden_ready(path)
    conn.close()
    sentinel = "OPEN_DB_HISTORY_APPLY_SENTINEL"
    db_str = str(path)

    def _boom(db_path: Path) -> sqlite3.Connection:
        msg = f"cannot open {db_path} — {sentinel}"
        raise sqlite3.OperationalError(msg)

    import jarvise_ingest.db as db_mod

    monkeypatch.setattr(db_mod, "open_db", _boom)
    result = _invoke(path, "--json", "--apply")
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    body = _json_out(result)
    assert body["reason"] == "db_error"
    blob = (result.stdout or "") + (result.stderr or "")
    assert db_str not in blob
    assert sentinel not in blob


def _json_out(result: object) -> dict:
    import json

    blob = (getattr(result, "stdout", "") or "") + (getattr(result, "stderr", "") or "")
    start = blob.find("{")
    end = blob.rfind("}")
    assert start >= 0 and end > start, blob
    return json.loads(blob[start : end + 1])


def test_migration_37_fresh_and_v36_golden(tmp_path: Path) -> None:
    assert SCHEMA_VERSION == 38
    assert MIGRATIONS[36][0] == 37
    assert MIGRATIONS[36][1] == "partner_ledger_owner_flows"
    assert MIGRATIONS[36][3] is False
    fresh = open_db(tmp_path / "fresh.db")
    assert schema_version(fresh) == 38
    cols = {row[1] for row in fresh.execute("PRAGMA table_info(partner_event)")}
    assert {"effective_at_ms", "origin", "payouts_due_micro"} <= cols
    triggers = {
        row[0]
        for row in fresh.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'partner_event'"
        )
    }
    assert "partner_event_append_only_update" in triggers
    assert "partner_event_append_only_delete" in triggers
    fresh.close()

    path = tmp_path / "golden.db"
    conn = _seed_golden_v36(path)
    before = _partner_event_checksum(conn)
    rowids = [int(row[0]) for row in conn.execute("SELECT rowid FROM partner_event ORDER BY rowid")]
    conn.close()
    migrated = open_db(path)
    after = _partner_event_checksum(migrated)
    assert after == before
    assert [
        int(row[0]) for row in migrated.execute("SELECT rowid FROM partner_event ORDER BY rowid")
    ] == rowids
    dues = [
        int(row[0]) for row in migrated.execute("SELECT payouts_due_micro FROM partner_event ORDER BY rowid")
    ]
    assert dues == [0, 0]
    assert schema_version(migrated) == 38
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        migrated.execute("UPDATE partner_event SET note = 'x' WHERE id = 'pe_admin_l1'")
    migrated.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        migrated.execute("DELETE FROM partner_event WHERE id = 'pe_toto_l1'")
    migrated.rollback()
    assert migrate(migrated) == []
    again = _partner_event_checksum(migrated)
    _migrate_partner_ledger_owner_flows(migrated)
    assert _partner_event_checksum(migrated) == again
    assert schema_version(migrated) == 37
    migrated.close()


def test_migration_37_backfill_due_and_unknown(tmp_path: Path) -> None:
    path = tmp_path / "due.db"
    conn = _open_before(path, 37)
    conn.execute(
        """
        INSERT INTO partner (
            id, name, is_admin, fee_rate_bps, units_e8, cost_micro, status,
            created_at_ms, updated_at_ms
        ) VALUES ('pt_a', 'admin', 1, 0, 10000000000, 100000000, 'active', 100, 100)
        """
    )
    conn.execute(
        """
        INSERT INTO partner_event (
            id, partner_id, kind, request_id, account_value_micro, total_units_before_e8,
            nav, units_delta_e8, cost_delta_micro, cash_micro, fee_micro,
            units_after_e8, cost_after_micro, actor, created_at_ms
        ) VALUES
            ('pe_early', 'pt_a', 'deposit', NULL, 100000000, 0, '1',
             10000000000, 100000000, 100000000, 0, 10000000000, 100000000, 'admin', 400),
            ('pe_late', 'pt_a', 'adjust_cost', NULL, 100000000, 10000000000, '1',
             0, 0, 0, 0, 10000000000, 100000000, 'admin', 1000)
        """
    )
    conn.execute(
        """
        INSERT INTO partner_request (
            id, partner_id, kind, status, payout_state, payout_micro,
            created_at_ms, expires_at_ms, decided_at_ms
        ) VALUES ('pr_due', 'pt_a', 'withdraw_profit', 'confirmed', 'due', 5000000, 500, 500, 500)
        """
    )
    conn.commit()
    conn.close()
    migrated = open_db(path)
    rows = {
        str(row["id"]): row["payouts_due_micro"]
        for row in migrated.execute("SELECT id, payouts_due_micro FROM partner_event")
    }
    assert int(rows["pe_early"]) == 0
    assert int(rows["pe_late"]) == 5_000_000
    migrated.close()

    unknown = tmp_path / "unknown.db"
    conn = _open_before(unknown, 37)
    conn.execute(
        """
        INSERT INTO partner (
            id, name, is_admin, fee_rate_bps, units_e8, cost_micro, status,
            created_at_ms, updated_at_ms
        ) VALUES ('pt_a', 'admin', 1, 0, 1, 1, 'active', 1, 1)
        """
    )
    conn.execute(
        """
        INSERT INTO partner_event (
            id, partner_id, kind, account_value_micro, total_units_before_e8, nav,
            units_delta_e8, cost_delta_micro, cash_micro, fee_micro,
            units_after_e8, cost_after_micro, actor, created_at_ms
        ) VALUES (
            'pe_x', 'pt_a', 'deposit', 1, 0, '1', 1, 1, 1, 0, 1, 1, 'admin', 10
        )
        """
    )
    conn.execute(
        """
        INSERT INTO partner_request (
            id, partner_id, kind, status, payout_state, payout_micro,
            created_at_ms, expires_at_ms, decided_at_ms
        ) VALUES ('pr_bad', 'pt_a', 'close', 'confirmed', 'due', NULL, 1, 1, NULL)
        """
    )
    conn.commit()
    conn.close()
    migrated = open_db(unknown)
    due = migrated.execute("SELECT payouts_due_micro FROM partner_event WHERE id = 'pe_x'").fetchone()[0]
    assert due is None
    migrated.close()


def test_migration_37_failure_rolls_back(tmp_path: Path) -> None:
    path = tmp_path / "boom.db"
    conn = _seed_golden_v36(path)
    before = _partner_event_checksum(conn)

    def authorizer(
        action: int, arg1: str | None, _arg2: str | None, _db: str | None, _src: str | None
    ) -> int:
        if action == sqlite3.SQLITE_INSERT and arg1 == "partner_event_new":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(authorizer)
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        migrate(conn)
    conn.close()
    check = connect(path)
    assert schema_version(check) == 36
    assert _partner_event_checksum(check) == before
    assert "payouts_due_micro" not in {row[1] for row in check.execute("PRAGMA table_info(partner_event)")}
    assert check.execute("SELECT name FROM sqlite_master WHERE name = 'partner_event_new'").fetchone() is None
    check.close()


def test_migration_37_concurrency_rebuilds_once(tmp_path: Path) -> None:
    path = tmp_path / "race.db"
    conn = _seed_golden_v36(path)
    before = _partner_event_checksum(conn)
    conn.close()
    paused = threading.Event()
    release = threading.Event()
    created = {"a": 0, "b": 0}
    errors: dict[str, BaseException] = {}

    def _watch(label: str, hold: bool) -> None:
        local = connect(path)

        def authorizer(
            action: int, arg1: str | None, _arg2: str | None, _db: str | None, _src: str | None
        ) -> int:
            if action == sqlite3.SQLITE_CREATE_TABLE and arg1 == "partner_event_new":
                created[label] += 1
                if hold:
                    paused.set()
                    assert release.wait(20)
            return sqlite3.SQLITE_OK

        local.set_authorizer(authorizer)
        try:
            migrate(local)
        except BaseException as exc:  # noqa: BLE001
            errors[label] = exc
        finally:
            local.close()

    def run_a() -> None:
        _watch("a", True)

    def run_b() -> None:
        _watch("b", False)

    thread_a = threading.Thread(target=run_a)
    thread_a.start()
    assert paused.wait(20)
    thread_b = threading.Thread(target=run_b)
    thread_b.start()
    time.sleep(0.4)
    release.set()
    thread_a.join(30)
    thread_b.join(30)
    assert errors == {}
    assert created == {"a": 1, "b": 0}
    check = connect(path)
    assert schema_version(check) == 38
    assert _partner_event_checksum(check) == before
    check.close()


def test_owner_redeem_golden_and_guards(tmp_path: Path) -> None:
    path = tmp_path / "store.db"
    conn, _before, _rowids = _golden_ready(path)
    toto = ps.get_partner(conn, "pt_toto_l1")
    assert toto is not None
    before = ps.view_partner(conn, "pt_toto_l1", D("289.12"))
    assert before["net"] == TOTO_BEFORE
    result = ps.apply_owner_redeem(conn, amount=D("150"), account_value=D("289.12"), now_ms=EFF_DEFAULT_MS)
    assert result["ok"] is True
    assert result["fee"] == 0
    assert result["nav_pre"].quantize(D("1e-8")) == NAV_Q
    assert result["burned_units"] == BURN
    assert result["units_after"] == ADMIN_UNITS_AFTER
    assert result["cost_after"] == ADMIN_COST_AFTER
    admin = ps.get_partner(conn, "pt_admin_l1")
    assert admin is not None
    assert int(admin["units_e8"]) == ADMIN_UNITS_AFTER_E8
    assert int(admin["cost_micro"]) == 181_536_710
    after = ps.view_partner(conn, "pt_toto_l1", D("289.12"))
    assert after["net"] == TOTO_AFTER
    event = conn.execute("SELECT * FROM partner_event WHERE kind = 'owner_redeem'").fetchone()
    assert int(event["cash_micro"]) == CASH_MICRO
    assert int(event["fee_micro"]) == 0
    assert int(event["payouts_due_micro"]) == 0
    assert event["origin"] == "manual"
    assert int(event["effective_at_ms"]) == EFF_DEFAULT_MS
    assert event["request_id"] is None
    verified = ps.verify_ledger(conn)
    assert verified["ok"] is True
    nav, stamp = _latest_nav(conn) or (None, None)
    assert stamp == EFF_DEFAULT_MS
    assert nav is not None and nav.quantize(D("1e-8")) == NAV_Q
    joined = partner_rules.joined_at_ms(conn, "pt_toto_l1")
    assert joined == T_TOTO
    assert ps.deposit_kyc_required(conn, toto) is False
    conn.close()

    exact = open_db(tmp_path / "exact.db")
    admin_id = ps.create_partner(exact, name="admin", is_admin=True, now_ms=1)["partner_id"]
    other = ps.create_partner(exact, name="Other", now_ms=1)["partner_id"]
    assert ps.apply_deposit(exact, admin_id, D("1000"), D("1000"), 2)["ok"]
    assert ps.apply_deposit(exact, other, D("200"), D("1200"), 3)["ok"]
    full = ps.apply_owner_redeem(exact, amount=D("1000"), account_value=D("200"), now_ms=4)
    assert full["ok"] is True
    assert full["units_after"] == 0
    assert full["fee"] == 0
    exact.close()

    over = open_db(tmp_path / "over.db")
    admin_id = ps.create_partner(over, name="admin", is_admin=True, now_ms=1)["partner_id"]
    other = ps.create_partner(over, name="Other", now_ms=1)["partner_id"]
    assert ps.apply_deposit(over, admin_id, D("1000"), D("1000"), 2)["ok"]
    assert ps.apply_deposit(over, other, D("200"), D("1200"), 3)["ok"]
    refused = ps.apply_owner_redeem(over, amount=D("1000.01"), account_value=D("200"), now_ms=4)
    assert refused == {"ok": False, "reason": "owner_overdraw"}
    assert (
        over.execute("SELECT COUNT(*) AS n FROM partner_event WHERE kind = 'owner_redeem'").fetchone()["n"]
        == 0
    )
    over.close()

    bare = open_db(tmp_path / "bare.db")
    assert ps.apply_owner_redeem(bare, amount=D("1"), account_value=D("1"), now_ms=1)["reason"] == "no_admin"
    ps.create_partner(bare, name="Bob", now_ms=1)
    assert ps.apply_owner_redeem(bare, amount=D("1"), account_value=D("1"), now_ms=1)["reason"] == "not_admin"
    bare.close()

    closed = open_db(tmp_path / "closed.db")
    admin_id = ps.create_partner(closed, name="admin", is_admin=True, now_ms=1)["partner_id"]
    assert ps.apply_deposit(closed, admin_id, D("100"), D("100"), 2)["ok"]
    closed.execute("UPDATE partner SET status = 'closed' WHERE id = ?", (admin_id,))
    closed.commit()
    assert ps.apply_owner_redeem(closed, amount=D("1"), account_value=D("99"), now_ms=3)["reason"] == "closed"
    closed.close()

    bad = open_db(tmp_path / "bad.db")
    ps.create_partner(bad, name="admin", is_admin=True, now_ms=1)
    for amount in (D("0"), D("-1"), D("NaN"), D("Infinity"), D("1.0000001")):
        assert (
            ps.apply_owner_deposit(bad, amount=amount, account_value=D("10"), now_ms=2)["reason"]
            == "bad_amount"
        )
    bad.close()

    pending = open_db(tmp_path / "pending.db")
    admin_id = ps.create_partner(pending, name="admin", is_admin=True, now_ms=1)["partner_id"]
    assert ps.apply_deposit(pending, admin_id, D("1000"), D("1000"), 2)["ok"]
    created = ps.create_request(pending, admin_id, "deposit", 3, amount=D("50"), source="admin")
    assert created["ok"]
    assert (
        ps.apply_owner_redeem(pending, amount=D("10"), account_value=D("990"), now_ms=4)["reason"]
        == "deposit_pending"
    )
    pending.close()

    broken = open_db(tmp_path / "broken.db")
    admin_id = ps.create_partner(broken, name="admin", is_admin=True, now_ms=1)["partner_id"]
    assert ps.apply_deposit(broken, admin_id, D("1000"), D("1000"), 2)["ok"]
    broken.execute("UPDATE partner SET units_e8 = units_e8 + 1 WHERE id = ?", (admin_id,))
    broken.commit()
    n_before = broken.execute("SELECT COUNT(*) AS n FROM partner_event").fetchone()["n"]
    assert ps.apply_owner_redeem(broken, amount=D("10"), account_value=D("990"), now_ms=3)["reason"] == (
        "ledger_unverified"
    )
    assert broken.execute("SELECT COUNT(*) AS n FROM partner_event").fetchone()["n"] == n_before
    broken.close()


def _golden_ready(path: Path) -> tuple[sqlite3.Connection, tuple[int, str], list[int]]:
    conn = _seed_golden_v36(path)
    before = _partner_event_checksum(conn)
    rowids = [int(row[0]) for row in conn.execute("SELECT rowid FROM partner_event ORDER BY rowid")]
    conn.close()
    return open_db(path), before, rowids


def test_owner_redeem_rolls_back_on_ledger_check_failed(tmp_path: Path) -> None:
    """Burn rounding can break I3 on tiny pools; refused write must not stick."""
    conn = open_db(tmp_path / "i3-rollback.db")
    t0 = 1_700_000_000_000
    admin_id = ps.create_partner(conn, name="admin", is_admin=True, now_ms=t0)["partner_id"]
    toto_pid = ps.create_partner(conn, name="ToTo", is_admin=False, now_ms=t0 + 1)["partner_id"]
    ps.apply_deposit(conn, admin_id, D("100"), D("100"), t0 + 10)
    ps.apply_deposit(conn, toto_pid, D("3"), D("103"), t0 + 20)
    admin_row = ps.get_partner(conn, admin_id)
    assert admin_row is not None
    units_before = int(admin_row["units_e8"])
    cost_before = int(admin_row["cost_micro"])
    drifted = D("103.12")
    redeem_amount = drifted - D("3.5")
    failed = ps.apply_owner_redeem(conn, amount=redeem_amount, account_value=D("3.5"), now_ms=t0 + 30)
    assert failed["ok"] is False
    assert failed["reason"] == "ledger_check_failed"
    assert (
        conn.execute("SELECT COUNT(*) AS n FROM partner_event WHERE kind = 'owner_redeem'").fetchone()["n"]
        == 0
    )
    admin_after = ps.get_partner(conn, admin_id)
    assert admin_after is not None
    assert int(admin_after["units_e8"]) == units_before
    assert int(admin_after["cost_micro"]) == cost_before
    assert ps.verify_ledger(conn)["ok"] is True
    conn.close()


def test_owner_flows_do_not_change_join_or_kyc_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_TEST_NO_KYC_SEED", "1")
    conn = open_db(tmp_path / "join.db")
    admin = ps.create_partner(conn, name="admin", is_admin=True, now_ms=10)["partner_id"]
    bob = ps.create_partner(conn, name="Bob", now_ms=11)["partner_id"]
    carol = ps.create_partner(conn, name="Carol", now_ms=12)["partner_id"]
    assert partner_rules.joined_at_ms(conn, admin) is None
    real_kyc = ps.deposit_kyc_required
    monkeypatch.setattr(ps, "deposit_kyc_required", lambda *_a, **_k: False)
    assert ps.apply_deposit(conn, carol, D("50"), D("50"), 13)["ok"] is True
    monkeypatch.setattr(ps, "deposit_kyc_required", real_kyc)
    deposited = ps.apply_owner_deposit(conn, amount=D("50"), account_value=D("100"), now_ms=14)
    assert deposited["ok"] is True
    assert partner_rules.joined_at_ms(conn, admin) is None
    assert ps.deposit_kyc_required(conn, ps.get_partner(conn, bob)) is True
    assert ps.apply_deposit(conn, bob, D("50"), D("150"), 20)["ok"] is False
    conn.close()


def _assert_events_due_and_nav(conn: sqlite3.Connection, event_ids: list[str], due_used: Decimal) -> None:
    from jarvise_assist.partner_ledger import nav as nav_fn

    due_micro = ps.usdt_to_micro(due_used)
    for eid in event_ids:
        row = conn.execute(
            """
            SELECT payouts_due_micro, nav, total_units_before_e8, account_value_micro
            FROM partner_event WHERE id = ?
            """,
            (eid,),
        ).fetchone()
        assert int(row["payouts_due_micro"]) == due_micro
        stored_nav = D(str(row["nav"]))
        av = ps.usdt_from_micro(int(row["account_value_micro"]))
        units_before = ps.units_from_e8(int(row["total_units_before_e8"]))
        assert stored_nav == nav_fn(av - due_used, units_before)


def test_payouts_due_matches_nav_input(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "due.db")
    now = 1_700_000_000_000
    admin = ps.create_partner(conn, name="Admin", is_admin=True, now_ms=now)["partner_id"]
    bob = ps.create_partner(conn, name="Bob", is_admin=False, now_ms=now + 1)["partner_id"]
    carol = ps.create_partner(conn, name="Carol", is_admin=False, now_ms=now + 2)["partner_id"]
    ps.apply_deposit(conn, admin, D("1000"), D("1000"), now + 10)
    ps.apply_deposit(conn, bob, D("600"), D("1600"), now + 20)
    ps.apply_deposit(conn, carol, D("100"), D("1700"), now + 25)
    due_before = ps.payouts_due_total(conn)
    assert due_before == 0
    withdrawn = ps.apply_withdraw_profit(conn, bob, D("2000"), now + 30)
    assert withdrawn["ok"] is True
    assert ps.payouts_due_total(conn) == withdrawn["cash"]
    assert withdrawn["cash"] > 0
    _assert_events_due_and_nav(conn, withdrawn["event_ids"], due_before)
    due_with_payout = ps.payouts_due_total(conn)
    assert due_with_payout > 0
    converted = ps.apply_convert(conn, carol, D("2000"), now + 35)
    assert converted["ok"] is True
    _assert_events_due_and_nav(conn, converted["event_ids"], due_with_payout)
    due_before_close = ps.payouts_due_total(conn)
    closed = ps.apply_close(conn, carol, D("2000"), now + 45)
    assert closed["ok"] is True
    _assert_events_due_and_nav(conn, closed["event_ids"], due_before_close)
    due_now = ps.payouts_due_total(conn)
    minted = ps.apply_owner_deposit(conn, amount=D("50"), account_value=D("2050"), now_ms=now + 50)
    assert minted["ok"] is True
    for eid in minted["event_ids"]:
        micro = int(
            conn.execute("SELECT payouts_due_micro FROM partner_event WHERE id = ?", (eid,)).fetchone()[0]
        )
        assert micro == ps.usdt_to_micro(due_now)
    conn.close()


def test_origin_manual_and_portal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_TEST_NO_KYC_SEED", "1")
    conn = open_db(tmp_path / "origin.db")
    now = 1_700_000_000_000
    admin = ps.create_partner(conn, name="admin", is_admin=True, now_ms=now)["partner_id"]
    bob = ps.create_partner(conn, name="Bob", now_ms=now + 1)["partner_id"]
    assert ps.apply_deposit(conn, admin, D("1000"), D("1000"), now + 2)["ok"]
    manual = conn.execute(
        "SELECT origin FROM partner_event WHERE partner_id = ? AND kind = 'deposit'",
        (admin,),
    ).fetchone()
    assert manual["origin"] == "manual"
    monkeypatch.setattr(ps, "deposit_kyc_required", lambda *_a, **_k: False)
    pending = ps.create_request(conn, bob, "deposit", now + 3, amount=D("100"), source="portal")
    assert ps.confirm_request(conn, pending["request_id"], D("1100"), now + 4, amount_received=D("100"))["ok"]
    portal = conn.execute(
        "SELECT origin FROM partner_event WHERE partner_id = ? AND kind = 'deposit'",
        (bob,),
    ).fetchone()
    assert portal["origin"] == "portal"
    conn.close()


def test_property_owner_flows_preserve_outsiders(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "prop.db")
    now = 1_700_000_000_000
    admin = ps.create_partner(conn, name="admin", is_admin=True, now_ms=now)["partner_id"]
    bob = ps.create_partner(conn, name="Bob", now_ms=now + 1)["partner_id"]
    cara = ps.create_partner(conn, name="Cara", now_ms=now + 2)["partner_id"]
    account = D("1000")
    assert ps.apply_deposit(conn, admin, D("1000"), account, now + 3)["ok"]
    account = D("1400")
    assert ps.apply_deposit(conn, bob, D("200"), account, now + 4)["ok"]
    assert ps.apply_deposit(conn, cara, D("200"), account, now + 5)["ok"]
    rng = random.Random(20261122)
    holders = [bob, cara]
    for step in range(24):
        kind = rng.choice(("deposit", "owner_deposit", "owner_redeem", "nav"))
        before = _marks(conn, account)
        if kind == "nav":
            factor = D(rng.randrange(60, 140)) / D("100")
            account = (account * factor).quantize(D("0.000001"))
            if account <= 0:
                continue
            after = _marks(conn, account)
            navs = [row["nav"] for row in after if row["units"] > 0]
            assert navs
            assert max(navs) == min(navs)
            for row in before:
                new = next(item for item in after if item["id"] == row["id"])
                assert new["units"] == row["units"]
                if row["units"] <= 0:
                    continue
                scaled = row["gross_raw"] * (navs[0] / row["nav"])
                assert abs(new["gross_raw"] - scaled) <= row["units"] * D("1e-24")
        else:
            amount = D(rng.randrange(10, 40))
            if kind == "deposit":
                target = holders[step % 2]
                new_account = account + amount
                result = ps.apply_deposit(conn, target, amount, new_account, now + 10 + step)
                if not result["ok"]:
                    continue
                account = new_account
                changed = target
            elif kind == "owner_deposit":
                new_account = account + amount
                result = ps.apply_owner_deposit(
                    conn, amount=amount, account_value=new_account, now_ms=now + 10 + step
                )
                if not result["ok"]:
                    continue
                account = new_account
                changed = admin
            else:
                admin_row = ps.get_partner(conn, admin)
                assert admin_row is not None
                units_now = ps.total_units(conn)
                if units_now <= 0 or account <= 0:
                    continue
                admin_value = ps.units_from_e8(int(admin_row["units_e8"])) * (account / units_now)
                if amount + D("1") >= admin_value:
                    continue
                new_account = account - amount
                if new_account <= 0:
                    continue
                if units_now - (amount / (account / units_now)) < D("100"):
                    continue
                result = ps.apply_owner_redeem(
                    conn, amount=amount, account_value=new_account, now_ms=now + 10 + step
                )
                if not result["ok"]:
                    continue
                account = new_account
                changed = None
            after = _marks(conn, account)
            nav = after[0]["nav"]
            for row in before:
                if row["admin"]:
                    continue
                new = next(item for item in after if item["id"] == row["id"])
                delta = new["net"] - row["net"]
                if changed == row["id"]:
                    assert delta >= 0
                else:
                    assert delta >= 0
                    assert delta <= UNIT * nav
        units = sum(
            (ps.units_from_e8(int(row["units_e8"])) for row in ps.list_partners(conn)),
            D("0"),
        )
        assert units == ps.total_units(conn)
        assert ps.verify_ledger(conn)["ok"] is True
    conn.close()


def _marks(conn: sqlite3.Connection, account: Decimal) -> list[dict[str, object]]:
    out = []
    for row in ps.list_partners(conn):
        if str(row["status"]) != "active":
            continue
        view = ps.view_partner(conn, str(row["id"]), account)
        assert view["ok"] is True
        units = ps.units_from_e8(int(row["units_e8"]))
        out.append(
            {
                "id": str(row["id"]),
                "admin": int(row["is_admin"]) == 1,
                "units": units,
                "net": view["net"],
                "nav": view["nav"],
                "gross_raw": units * view["nav"],
            }
        )
    return out


def test_audit_matches_owner_flows(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "audit.db")
    start = 1_700_000_000_000
    when = start + 3_600_000
    admin = ps.create_partner(conn, name="admin", is_admin=True, now_ms=start)["partner_id"]
    assert ps.apply_deposit(conn, admin, D("1000"), D("1000"), start)["ok"]
    assert ps.apply_owner_redeem(conn, amount=D("150"), account_value=D("850"), now_ms=when)["ok"]
    items = ledger_cash(conn, start, when + 1000)
    assert any(
        item.kind == "owner_redeem" and item.direction == -1 and item.amount == D("150") for item in items
    )
    result = reconcile(
        [
            Transfer(
                time_ms=when, amount=D("-150"), asset="USDT", income_type="TRANSFER", tran_id="432610282749"
            )
        ],
        items,
        now_ms=when + 10**8,
        ledger_start_ms=start,
        report_start_ms=start,
    )
    assert not any(f.fingerprint == "bx:TRANSFER:432610282749" for f in result.findings)
    assert result.matched_n >= 1
    conn.close()

    conn = open_db(tmp_path / "audit_in.db")
    admin = ps.create_partner(conn, name="admin", is_admin=True, now_ms=start)["partner_id"]
    assert ps.apply_deposit(conn, admin, D("1000"), D("1000"), start)["ok"]
    assert ps.apply_owner_deposit(conn, amount=D("150"), account_value=D("1150"), now_ms=when)["ok"]
    items = ledger_cash(conn, start, when + 1000)
    assert any(
        item.kind == "owner_deposit" and item.direction == 1 and item.amount == D("150") for item in items
    )
    result = reconcile(
        [Transfer(time_ms=when, amount=D("150"), asset="USDT", income_type="TRANSFER", tran_id="in150")],
        items,
        now_ms=when + 10**8,
        ledger_start_ms=start,
        report_start_ms=start,
    )
    assert not any(f.fingerprint == "bx:TRANSFER:in150" for f in result.findings)
    assert result.matched_n >= 1
    conn.close()


def _finding(code: str) -> Finding:
    return Finding(
        code=code,
        fingerprint=f"fp:{code}",
        time_ms=1_700_000_000_000,
        amount=D("150"),
        partner_name="ToTo",
        detail="",
        alertable=True,
    )


def test_alert_guard_and_help_do_not_suggest_admin_capital_commands() -> None:
    forbidden = ("p dep admin", "p wd admin", "p cost admin")
    for code in ("transfer_in_unrecorded", "transfer_out_unrecorded", "paid_no_outflow"):
        text = format_alert_th([_finding(code)], 0)
        for token in forbidden:
            assert token not in text
        assert "รอจัดประเภท" in text
        if code == "paid_no_outflow":
            assert "ToTo" in text
    guard = format_guard_th(
        GuardResult(status="ok", checked_at_ms=1, findings=[_finding("transfer_out_unrecorded")], warn=True),
        last_ok_ms=None,
    )
    for token in forbidden:
        assert token not in guard
    assert "อย่าบันทึกเองด้วยคำสั่งฝาก/ถอน" in guard
    hint = format_guard_th(
        GuardResult(
            status="ok",
            checked_at_ms=1,
            findings=[_finding("this_deposit_amount_diff")],
            warn=True,
        ),
        last_ok_ms=None,
    )
    assert "p dep ToTo" in hint
    for token in forbidden:
        assert token not in HELP_TEXT
    assert "owner_deposit" in HELP_TEXT and "owner_redeem" in HELP_TEXT


def test_history_cli_dry_run_apply_and_refusals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "hist.db"
    conn, before, _rowids = _golden_ready(path)
    assert ps.verify_ledger(conn)["ok"] is True
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    digest = _digest(path)
    dry = _invoke(path, "--json")
    assert dry.exit_code == 0
    payload = _json_out(dry)
    assert payload["ok"] is True
    assert payload["mode"] == "dry-run"
    golden = payload["golden"]
    assert golden["nav_pre_q"] == "1.03290948"
    assert golden["nav_pre"].startswith("1.032909479336")
    assert golden["burn"] == format(BURN, "f")
    assert golden["units_delta_e8"] == UNITS_DELTA_E8
    assert golden["admin_units_after"] == format(ADMIN_UNITS_AFTER, "f")
    assert golden["admin_cost_after"] == format(ADMIN_COST_AFTER, "f")
    assert golden["cash_micro"] == CASH_MICRO
    assert golden["account_value_micro"] == AV_MICRO
    assert golden["payouts_due_micro"] == 0
    assert golden["toto_before"] == format(TOTO_BEFORE, "f")
    assert golden["toto_after"] == format(TOTO_AFTER, "f")
    assert abs(D(golden["nav_delta"])) < D("1e-9")
    assert golden["total_units_before_e8"] == TOTAL_UNITS_E8
    assert _digest(path) == digest
    assert before[0] == 2

    copy = tmp_path / "copy.db"
    shutil.copy(path, copy)
    applied = _invoke(copy, "--json", "--apply")
    assert applied.exit_code == 0, applied.stdout + applied.stderr
    body = _json_out(applied)
    assert body["reason"] == "recorded"
    assert body["golden"]["burn"] == format(BURN, "f")
    check = open_db(copy)
    row = check.execute("SELECT * FROM partner_event WHERE id = ?", (HIST_EVENT_ID,)).fetchone()
    assert row is not None
    assert int(row["units_delta_e8"]) == UNITS_DELTA_E8
    assert int(row["units_after_e8"]) == ADMIN_UNITS_AFTER_E8
    assert int(row["cost_after_micro"]) == 181_536_710
    assert int(row["cash_micro"]) == CASH_MICRO
    assert int(row["account_value_micro"]) == AV_MICRO
    assert row["origin"] == "manual"
    assert row["note"].startswith("historical owner_redeem:")
    assert ps.verify_ledger(check)["ok"] is True
    nav, stamp = _latest_nav(check) or (None, None)
    assert stamp == EFF_DEFAULT_MS
    assert nav is not None and nav.quantize(D("1e-8")) == NAV_Q
    toto_view = ps.view_partner(check, "pt_toto_l1", D("289.12"))
    assert toto_view["net"] == TOTO_AFTER
    admin = ps.get_partner(check, "pt_admin_l1")
    assert admin is not None
    assert int(admin["units_e8"]) == ADMIN_UNITS_AFTER_E8
    n_events = int(check.execute("SELECT COUNT(*) AS n FROM partner_event").fetchone()["n"])
    items = ledger_cash(check, T_DEP, EFF_DEFAULT_MS + 60_000)
    audited = reconcile(
        [
            Transfer(
                time_ms=EFF_DEFAULT_MS,
                amount=-AMOUNT,
                asset="USDT",
                income_type="TRANSFER",
                tran_id="432610282749",
            )
        ],
        items,
        now_ms=EFF_DEFAULT_MS + 10**8,
        ledger_start_ms=T_DEP,
        report_start_ms=T_DEP,
    )
    assert not any(item.fingerprint == "bx:TRANSFER:432610282749" for item in audited.findings)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        check.execute("UPDATE partner_event SET note = 'tamper' WHERE id = ?", (HIST_EVENT_ID,))
    check.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        check.execute("DELETE FROM partner_event WHERE id = ?", (HIST_EVENT_ID,))
    check.rollback()
    check.close()

    again = _invoke(copy, "--json", "--apply")
    assert again.exit_code == 0
    assert _json_out(again)["reason"] == "already_recorded"
    check = open_db(copy)
    assert int(check.execute("SELECT COUNT(*) AS n FROM partner_event").fetchone()["n"]) == n_events
    check.close()

    tampered = tmp_path / "tamper.db"
    shutil.copy(path, tampered)
    raw = connect(tampered)
    raw.execute("DROP TRIGGER partner_event_append_only_update")
    raw.execute("DROP TRIGGER partner_event_append_only_delete")
    raw.execute(
        """
        INSERT INTO partner_event (
            id, partner_id, kind, account_value_micro, total_units_before_e8, nav,
            units_delta_e8, cost_delta_micro, cash_micro, fee_micro,
            units_after_e8, cost_after_micro, actor, note, created_at_ms,
            effective_at_ms, origin, payouts_due_micro
        ) VALUES (
            ?, 'pt_admin_l1', 'owner_redeem', 1, 1, '1',
            -1, -1, -1, 0, 1, 1, 'admin', 'tampered', ?, ?, 'manual', 0
        )
        """,
        (HIST_EVENT_ID, EFF_DEFAULT_MS, EFF_DEFAULT_MS),
    )
    raw.commit()
    raw.close()
    conflict = _invoke(tampered, "--json")
    assert conflict.exit_code == 3
    assert _json_out(conflict)["reason"] == "conflict"

    window = _invoke(path, "--json", "--effective-at-ms", "1")
    assert window.exit_code == 2
    assert _digest(path) == digest

    stale = tmp_path / "stale.db"
    _seed_golden_v36(stale).close()
    mismatch = _invoke(stale, "--json")
    assert mismatch.exit_code == 2
    assert "schema mismatch" in ((mismatch.stdout or "") + (mismatch.stderr or ""))

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

    def extra(db: Path) -> None:
        c = connect(db)
        c.execute(
            """
            INSERT INTO partner_event (
                id, partner_id, kind, account_value_micro, total_units_before_e8, nav,
                units_delta_e8, cost_delta_micro, cash_micro, fee_micro,
                units_after_e8, cost_after_micro, actor, created_at_ms, payouts_due_micro
            ) VALUES (
                'pe_extra', 'pt_admin_l1', 'adjust_cost', 1, ?, '1',
                0, 0, 0, 0, ?, ?, 'admin', ?, 0
            )
            """,
            (TOTAL_UNITS_E8, ADMIN_UNITS_E8, ADMIN_COST_MICRO, T_DEP + 10),
        )
        c.commit()
        c.close()

    def late(db: Path) -> None:
        c = connect(db)
        c.execute("DROP TRIGGER partner_event_append_only_update")
        c.execute(
            "UPDATE partner_event SET created_at_ms = ? WHERE id = 'pe_toto_l1'",
            (EFF_DEFAULT_MS + 1,),
        )
        c.commit()
        c.close()

    def pending(db: Path) -> None:
        c = connect(db)
        c.execute(
            """
            INSERT INTO partner_request (
                id, partner_id, kind, status, payout_state, created_at_ms, expires_at_ms
            ) VALUES ('pr_pending_l1', 'pt_toto_l1', 'deposit', 'pending', 'none', ?, ?)
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
                'pr_due_l1', 'pt_toto_l1', 'withdraw_profit', 'confirmed', 'due', 1000000,
                ?, ?, ?
            )
            """,
            (T_DEP, T_DEP, T_DEP),
        )
        c.commit()
        c.close()

    def units(db: Path) -> None:
        c = connect(db)
        c.execute("UPDATE partner SET units_e8 = units_e8 + 1 WHERE id = 'pt_toto_l1'")
        c.commit()
        c.close()

    _refuse(extra, "extra")
    _refuse(late, "late")
    _refuse(pending, "pending")
    _refuse(due, "due")
    _refuse(units, "units")

    rolled = tmp_path / "rollback.db"
    shutil.copy(path, rolled)
    real = ps.verify_ledger
    calls = {"n": 0}

    def flaky(conn: sqlite3.Connection) -> dict:
        calls["n"] += 1
        if calls["n"] >= 2:
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


IDLE_PARTNER_ID = "pt_idle_l1"
IDLE_CREATED_MS = T_TOTO + 60_000
_FORBIDDEN_PRIVACY = ("IdleSentinelZ9", "777000111999", "invhashSentinelZ9")


def _insert_idle_partner(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO partner (
            id, name, is_admin, fee_rate_bps, units_e8, cost_micro, status,
            telegram_user_id, invite_hash, created_at_ms, updated_at_ms
        ) VALUES (?, 'IdleSentinelZ9', 0, 0, 0, 0, 'active',
                  '777000111999', 'invhashSentinelZ9', ?, ?)
        """,
        (IDLE_PARTNER_ID, IDLE_CREATED_MS, IDLE_CREATED_MS),
    )
    conn.commit()


def _golden_with_idle(path: Path) -> Path:
    conn, _, _ = _golden_ready(path)
    _insert_idle_partner(conn)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    return path


def _assert_no_privacy_leak(result: object) -> None:
    blob = (getattr(result, "stdout", "") or "") + (getattr(result, "stderr", "") or "")
    for token in _FORBIDDEN_PRIVACY:
        assert token not in blob


def _baseline_golden_dict(path: Path) -> dict:
    conn, _, _ = _golden_ready(path)
    values = compute_golden(conn)["values"]
    conn.close()
    return values


def test_history_idle_partner_happy_path_cli(tmp_path: Path) -> None:
    baseline_path = tmp_path / "baseline.db"
    baseline_golden = _baseline_golden_dict(baseline_path)

    path = _golden_with_idle(tmp_path / "idle_happy.db")
    dry = _invoke(path, "--json")
    assert dry.exit_code == 0
    _assert_no_privacy_leak(dry)
    payload = _json_out(dry)
    assert payload["ok"] is True
    assert payload["reason"] == "ready"
    assert payload["golden"] == baseline_golden
    assert payload["pre"]["idle_partner_count"] == 1

    copy = tmp_path / "idle_apply.db"
    shutil.copy(path, copy)
    before_idle = connect(copy)
    idle_before = dict(
        before_idle.execute("SELECT * FROM partner WHERE id = ?", (IDLE_PARTNER_ID,)).fetchone()
    )
    before_idle.close()

    applied = _invoke(copy, "--json", "--apply")
    assert applied.exit_code == 0, applied.stdout + applied.stderr
    _assert_no_privacy_leak(applied)
    body = _json_out(applied)
    assert body["reason"] == "recorded"
    assert body["golden"] == baseline_golden
    assert body["pre"]["idle_partner_count"] == 1

    check = open_db(copy)
    row = check.execute("SELECT * FROM partner_event WHERE id = ?", (HIST_EVENT_ID,)).fetchone()
    assert row is not None
    assert int(row["units_delta_e8"]) == UNITS_DELTA_E8
    assert int(row["units_after_e8"]) == ADMIN_UNITS_AFTER_E8
    assert int(row["cost_after_micro"]) == 181_536_710
    assert int(row["cash_micro"]) == CASH_MICRO
    assert int(row["account_value_micro"]) == AV_MICRO
    assert ps.verify_ledger(check)["ok"] is True
    toto_view = ps.view_partner(check, "pt_toto_l1", D("289.12"))
    assert toto_view["net"] == TOTO_AFTER
    idle_after = dict(check.execute("SELECT * FROM partner WHERE id = ?", (IDLE_PARTNER_ID,)).fetchone())
    assert idle_after == idle_before
    n_events = int(check.execute("SELECT COUNT(*) AS n FROM partner_event").fetchone()["n"])
    check.close()

    again = _invoke(copy, "--json", "--apply")
    assert again.exit_code == 0
    _assert_no_privacy_leak(again)
    assert _json_out(again)["reason"] == "already_recorded"
    check = open_db(copy)
    assert int(check.execute("SELECT COUNT(*) AS n FROM partner_event").fetchone()["n"]) == n_events
    check.close()


def test_history_idle_partner_compute_golden_unchanged(tmp_path: Path) -> None:
    baseline_path = tmp_path / "baseline2.db"
    conn_base, _, _ = _golden_ready(baseline_path)
    golden_base = compute_golden(conn_base)
    assert golden_base["ok"] is True
    conn_base.close()

    idle_path = _golden_with_idle(tmp_path / "idle_compute.db")
    conn_idle = open_db(idle_path)
    golden_idle = compute_golden(conn_idle)
    assert golden_idle["ok"] is True
    assert golden_idle["values"] == golden_base["values"]
    conn_idle.close()


def test_compute_golden_non_idle_units_on_idle_partner(tmp_path: Path) -> None:
    path = tmp_path / "idle_non_idle_units.db"
    conn, _, _ = _golden_ready(path)
    _insert_idle_partner(conn)
    conn.execute("UPDATE partner SET units_e8 = 1 WHERE id = ?", (IDLE_PARTNER_ID,))
    conn.commit()
    golden = compute_golden(conn)
    assert golden["ok"] is False
    assert golden["values"] == {}
    non_idle = [item for item in golden["diffs"] if item.get("field") == "non_idle_partner"]
    assert any(item["actual"]["partner_id"] == IDLE_PARTNER_ID for item in non_idle)
    conn.close()


def test_history_idle_partner_refusal_non_idle(tmp_path: Path) -> None:
    base = _golden_with_idle(tmp_path / "refuse_base.db")

    def _assert_refused(setup, expect_single_non_idle: bool = False) -> None:
        db = tmp_path / f"refuse_{setup.__name__}.db"
        shutil.copy(base, db)
        setup(db)
        result = _invoke(db, "--json", "--apply")
        assert result.exit_code == 1, result.stdout + result.stderr
        _assert_no_privacy_leak(result)
        body = _json_out(result)
        assert body["ok"] is False
        diff = body["diff"]
        non_idle = [item for item in diff if item.get("field") == "non_idle_partner"]
        assert any(item["actual"]["partner_id"] == IDLE_PARTNER_ID for item in non_idle)
        check = connect(db)
        assert check.execute("SELECT 1 FROM partner_event WHERE id = ?", (HIST_EVENT_ID,)).fetchone() is None
        admin_row = check.execute("SELECT units_e8 FROM partner WHERE id = 'pt_admin_l1'").fetchone()
        assert int(admin_row[0]) == ADMIN_UNITS_E8
        check.close()
        if expect_single_non_idle:
            assert diff == non_idle and len(non_idle) == 1

    def c1_units(db: Path) -> None:
        c = connect(db)
        c.execute("UPDATE partner SET units_e8 = 1 WHERE id = ?", (IDLE_PARTNER_ID,))
        c.commit()
        c.close()

    def c2_cost(db: Path) -> None:
        c = connect(db)
        c.execute("UPDATE partner SET cost_micro = 1 WHERE id = ?", (IDLE_PARTNER_ID,))
        c.commit()
        c.close()

    def c3_event(db: Path) -> None:
        c = connect(db)
        c.execute(
            """
            INSERT INTO partner_event (
                id, partner_id, kind, account_value_micro, total_units_before_e8, nav,
                units_delta_e8, cost_delta_micro, cash_micro, fee_micro,
                units_after_e8, cost_after_micro, actor, created_at_ms, payouts_due_micro
            ) VALUES (
                'pe_idle_evt', ?, 'deposit', 0, 0, '1',
                0, 0, 0, 0, 0, 0, 'admin', ?, 0
            )
            """,
            (IDLE_PARTNER_ID, T_DEP),
        )
        c.commit()
        c.close()

    def c4_request(db: Path) -> None:
        c = connect(db)
        c.execute(
            """
            INSERT INTO partner_request (
                id, partner_id, kind, status, payout_state, created_at_ms, expires_at_ms,
                decided_at_ms
            ) VALUES (
                'pr_idle_rej', ?, 'deposit', 'rejected', 'none', ?, ?, ?
            )
            """,
            (IDLE_PARTNER_ID, T_DEP, T_DEP, T_DEP),
        )
        c.commit()
        c.close()

    def c5_closed(db: Path) -> None:
        c = connect(db)
        c.execute("UPDATE partner SET status = 'closed' WHERE id = ?", (IDLE_PARTNER_ID,))
        c.commit()
        c.close()

    _assert_refused(c1_units)
    _assert_refused(c2_cost)
    _assert_refused(c3_event)
    _assert_refused(c4_request, expect_single_non_idle=True)
    _assert_refused(c5_closed)


def test_history_idle_partner_refusal_toto_missing(tmp_path: Path) -> None:
    path = _golden_with_idle(tmp_path / "nototo.db")
    conn = connect(path)
    conn.execute("UPDATE partner SET name = 'NotToTo' WHERE id = 'pt_toto_l1'")
    conn.commit()
    conn.close()

    dry = _invoke(path, "--json")
    assert dry.exit_code == 1
    _assert_no_privacy_leak(dry)
    body = _json_out(dry)
    assert body["ok"] is False
    fields = {item["field"] for item in body["diff"]}
    assert "toto" in fields
    assert any(
        item.get("field") == "non_idle_partner" and item["actual"]["partner_id"] == "pt_toto_l1"
        for item in body["diff"]
    )
    blob = (dry.stdout or "") + (dry.stderr or "")
    assert "NotToTo" not in blob

    apply = _invoke(path, "--json", "--apply")
    assert apply.exit_code == 1
    _assert_no_privacy_leak(apply)
