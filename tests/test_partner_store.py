"""SQLite partner ledger store (tmp_path only; never production DB)."""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from jarvise_assist import partner_store as ps
from jarvise_ingest.db import SCHEMA_VERSION, open_db, schema_version

D = Decimal


def _units(conn: sqlite3.Connection, partner_id: str) -> Decimal:
    row = ps.get_partner(conn, partner_id)
    assert row is not None
    return ps.units_from_e8(int(row["units_e8"]))


def _cost(conn: sqlite3.Connection, partner_id: str) -> Decimal:
    row = ps.get_partner(conn, partner_id)
    assert row is not None
    return ps.usdt_from_micro(int(row["cost_micro"]))


def test_migration_creates_tables_fresh_and_from_29(tmp_path: Path) -> None:
    assert SCHEMA_VERSION == 30
    fresh = open_db(tmp_path / "fresh.db")
    assert schema_version(fresh) == 30
    for table in ("partner", "partner_event", "partner_request"):
        row = fresh.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        assert row is not None
    idx_names = {
        r["name"]
        for r in fresh.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name IN ("
            "'ux_partner_single_admin', 'ix_partner_event_partner', "
            "'ux_partner_request_one_pending')"
        )
    }
    assert idx_names == {
        "ux_partner_single_admin",
        "ix_partner_event_partner",
        "ux_partner_request_one_pending",
    }
    fresh.close()

    # Stamp a DB at user_version 29, then reopen → 30; existing rows untouched
    path = tmp_path / "from29.db"
    conn = open_db(path)
    # Use an existing table that migration 30 must not touch
    before = conn.execute("SELECT COUNT(*) AS n FROM assist_settings").fetchone()
    n_before = before[0] if not hasattr(before, "keys") else before["n"]
    conn.execute("PRAGMA user_version = 29")
    conn.commit()
    # Drop partner tables if repair re-created them so we simulate a true 29 DB
    conn.executescript(
        """
        DROP TABLE IF EXISTS partner_request;
        DROP TABLE IF EXISTS partner_event;
        DROP TABLE IF EXISTS partner;
        PRAGMA user_version = 29;
        """
    )
    conn.commit()
    conn.close()

    conn = open_db(path)
    assert schema_version(conn) == 30
    after = conn.execute("SELECT COUNT(*) AS n FROM assist_settings").fetchone()
    n_after = after[0] if not hasattr(after, "keys") else after["n"]
    assert n_after == n_before
    assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='partner'").fetchone()
    # migrate twice is a no-op
    from jarvise_ingest.db import migrate

    applied = migrate(conn)
    assert applied == []
    conn.close()


def test_full_sequence_a_b_c_fee_50(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "seq.db")
    now = 1_700_000_000_000
    a = ps.create_partner(conn, name="Admin", is_admin=True, now_ms=now)
    b = ps.create_partner(conn, name="Bob", is_admin=False, now_ms=now + 1)
    c = ps.create_partner(conn, name="Carol", is_admin=False, now_ms=now + 2)
    assert a["ok"] and b["ok"] and c["ok"]
    a_id, b_id, c_id = a["partner_id"], b["partner_id"], c["partner_id"]
    assert ps.get_partner(conn, a_id)["fee_rate_bps"] == 0
    assert ps.get_partner(conn, b_id)["fee_rate_bps"] == 5000

    # (1) A deposits 1000, V after 1000
    r1 = ps.apply_deposit(conn, a_id, D("1000"), D("1000"), now + 10)
    assert r1["ok"] is True
    assert _units(conn, a_id) == D("1000")
    assert _cost(conn, a_id) == D("1000")

    # (2) B deposits 600, V after 1800
    r2 = ps.apply_deposit(conn, b_id, D("600"), D("1800"), now + 20)
    assert r2["ok"] is True
    assert _units(conn, b_id) == D("500")
    assert _cost(conn, b_id) == D("600")

    # (3) C deposits 330, V after 1980
    r3 = ps.apply_deposit(conn, c_id, D("330"), D("1980"), now + 30)
    assert r3["ok"] is True
    assert _units(conn, c_id) == D("300")
    assert _cost(conn, c_id) == D("330")
    assert ps.total_units(conn) == D("1800")

    # (4) Views at V 2160
    va = ps.view_partner(conn, a_id, D("2160"))
    vb = ps.view_partner(conn, b_id, D("2160"))
    vc = ps.view_partner(conn, c_id, D("2160"))
    assert va["net"] == D("1200")
    assert vb["net"] == D("600") and vb["state"] == "flat"
    assert vc["gross"] == D("360")
    assert vc["fee"] == D("15")
    assert vc["net"] == D("345")
    assert vc["state"] == "up"

    # (5) C converts at V 2160
    r5 = ps.apply_convert(conn, c_id, D("2160"), now + 50)
    assert r5["ok"] is True
    assert _units(conn, c_id) == D("287.5")
    assert _cost(conn, c_id) == D("345")
    assert _units(conn, a_id) == D("1012.5")
    assert ps.total_units(conn) == D("1800")

    # (6) Views at V 1800
    vb6 = ps.view_partner(conn, b_id, D("1800"))
    vc6 = ps.view_partner(conn, c_id, D("1800"))
    assert vb6["net"] == D("500") and vb6["state"] == "down"
    assert vc6["net"] == D("287.5") and vc6["state"] == "down"
    assert vb6["fee"] == D("0") and vc6["fee"] == D("0")

    # (7) B closes at V 1800
    r7 = ps.apply_close(conn, b_id, D("1800"), now + 70)
    assert r7["ok"] is True
    assert r7["cash"] == D("500")
    assert r7["fee"] == D("0")
    assert _units(conn, b_id) == D("0")
    assert ps.get_partner(conn, b_id)["status"] == "closed"
    # NAV at V 1300 is 1.0
    assert ps.total_units(conn) == D("1300")
    from jarvise_assist.partner_ledger import nav

    assert nav(D("1300"), D("1300")) == D("1")

    # (8) View at V 1560: C gross 345 = cost, profit 0, fee 0
    vc8 = ps.view_partner(conn, c_id, D("1560"))
    assert vc8["gross"] == D("345")
    assert vc8["gross"] == _cost(conn, c_id)
    assert vc8["profit"] == D("0")
    assert vc8["fee"] == D("0")
    conn.close()


def test_events_and_fee_credit_one_transaction(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "ev.db")
    now = 1_700_000_000_000
    a = ps.create_partner(conn, name="Admin", is_admin=True, now_ms=now)
    c = ps.create_partner(conn, name="Carol", is_admin=False, now_ms=now + 1)
    a_id, c_id = a["partner_id"], c["partner_id"]
    ps.apply_deposit(conn, a_id, D("1000"), D("1000"), now + 10)
    ps.apply_deposit(conn, c_id, D("200"), D("1200"), now + 20)
    # V=1440, U=1200? Wait: after A 1000 @1, C deposits 200 with V=1200 → NAV_pre=1, units=200
    # At V=1440 NAV=1.2; C gross=240, profit=40, fee=20, share=20
    r = ps.apply_convert(conn, c_id, D("1440"), now + 30)
    assert r["ok"] is True
    events = conn.execute(
        "SELECT id, kind, partner_id, related_event_id, fee_micro "
        "FROM partner_event ORDER BY created_at_ms, id"
    ).fetchall()
    kinds = [e["kind"] for e in events]
    assert kinds.count("deposit") == 2
    assert "convert" in kinds
    assert "fee_credit" in kinds
    fee_rows = [e for e in events if e["kind"] == "fee_credit"]
    assert len(fee_rows) == 1
    fee = fee_rows[0]
    assert fee["partner_id"] == a_id
    assert fee["related_event_id"] is not None
    convert = [e for e in events if e["kind"] == "convert"][0]
    assert fee["related_event_id"] == convert["id"]
    assert "event_ids" in r and len(r["event_ids"]) >= 2
    conn.close()


def test_refusal_writes_nothing(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "ref.db")
    now = 1_700_000_000_000
    a = ps.create_partner(conn, name="Admin", is_admin=True, now_ms=now)
    b = ps.create_partner(conn, name="Bob", is_admin=False, now_ms=now + 1)
    a_id, b_id = a["partner_id"], b["partner_id"]
    ps.apply_deposit(conn, a_id, D("1000"), D("1000"), now + 10)
    ps.apply_deposit(conn, b_id, D("500"), D("1500"), now + 20)
    n_events = conn.execute("SELECT COUNT(*) AS n FROM partner_event").fetchone()
    n0 = int(n_events["n"] if hasattr(n_events, "keys") else n_events[0])
    units_b = _units(conn, b_id)
    cost_b = _cost(conn, b_id)
    # loss → no_profit
    r = ps.apply_convert(conn, b_id, D("1200"), now + 30)
    assert r["ok"] is False and r["reason"] == "no_profit"
    n1 = conn.execute("SELECT COUNT(*) AS n FROM partner_event").fetchone()
    assert int(n1["n"] if hasattr(n1, "keys") else n1[0]) == n0
    assert _units(conn, b_id) == units_b
    assert _cost(conn, b_id) == cost_b
    conn.close()


def test_exception_mid_apply_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = open_db(tmp_path / "rb.db")
    now = 1_700_000_000_000
    a = ps.create_partner(conn, name="Admin", is_admin=True, now_ms=now)
    c = ps.create_partner(conn, name="Carol", is_admin=False, now_ms=now + 1)
    a_id, c_id = a["partner_id"], c["partner_id"]
    ps.apply_deposit(conn, a_id, D("1000"), D("1000"), now + 10)
    ps.apply_deposit(conn, c_id, D("200"), D("1200"), now + 20)
    units_c = _units(conn, c_id)
    units_a = _units(conn, a_id)
    n0 = int(conn.execute("SELECT COUNT(*) AS n FROM partner_event").fetchone()[0])

    real_credit = ps._credit_fee_to_admin

    def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("forced")

    monkeypatch.setattr(ps, "_credit_fee_to_admin", boom)
    with pytest.raises(RuntimeError, match="forced"):
        ps.apply_convert(conn, c_id, D("1440"), now + 30)

    monkeypatch.setattr(ps, "_credit_fee_to_admin", real_credit)
    assert _units(conn, c_id) == units_c
    assert _units(conn, a_id) == units_a
    n1 = int(conn.execute("SELECT COUNT(*) AS n FROM partner_event").fetchone()[0])
    assert n1 == n0
    conn.close()


def test_verify_ledger_ok_and_mismatch(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "ver.db")
    now = 1_700_000_000_000
    a = ps.create_partner(conn, name="Admin", is_admin=True, now_ms=now)
    b = ps.create_partner(conn, name="Bob", is_admin=False, now_ms=now + 1)
    a_id, b_id = a["partner_id"], b["partner_id"]
    ps.apply_deposit(conn, a_id, D("1000"), D("1000"), now + 10)
    ps.apply_deposit(conn, b_id, D("600"), D("1800"), now + 20)
    ps.apply_convert(conn, b_id, D("2160"), now + 30)  # B may be flat/up depending
    # At V=2160 after deposits U=1500 NAV=1.44; B units 500 cost 600 gross=720 profit=120
    ok = ps.verify_ledger(conn)
    assert ok["ok"] is True
    assert ok["mismatches"] == []

    conn.execute(
        "UPDATE partner SET units_e8 = units_e8 + 1 WHERE id = ?",
        (b_id,),
    )
    conn.commit()
    bad = ps.verify_ledger(conn)
    assert bad["ok"] is False
    assert len(bad["mismatches"]) >= 1
    conn.close()


def test_second_admin_raises_integrity(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "adm.db")
    now = 1_700_000_000_000
    r1 = ps.create_partner(conn, name="Admin", is_admin=True, now_ms=now)
    assert r1["ok"]
    with pytest.raises(sqlite3.IntegrityError):
        ps.create_partner(conn, name="OtherAdmin", is_admin=True, now_ms=now + 1)
    conn.close()


def test_second_pending_request_raises_integrity(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "req.db")
    now = 1_700_000_000_000
    ps.create_partner(conn, name="Admin", is_admin=True, now_ms=now)
    b = ps.create_partner(conn, name="Bob", is_admin=False, now_ms=now + 1)
    b_id = b["partner_id"]
    conn.execute(
        """
        INSERT INTO partner_request (
            id, partner_id, kind, status, payout_state,
            created_at_ms, expires_at_ms
        ) VALUES ('pr_1', ?, 'convert', 'pending', 'none', ?, ?)
        """,
        (b_id, now, now + 60_000),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO partner_request (
                id, partner_id, kind, status, payout_state,
                created_at_ms, expires_at_ms
            ) VALUES ('pr_2', ?, 'close', 'pending', 'none', ?, ?)
            """,
            (b_id, now + 1, now + 60_000),
        )
        conn.commit()
    conn.close()


def test_closed_and_no_admin_refusals(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "refuse.db")
    now = 1_700_000_000_000
    # No admin: fee-bearing partner convert refuses no_admin
    b = ps.create_partner(conn, name="Bob", is_admin=False, now_ms=now)
    b_id = b["partner_id"]
    # Seed units/cost without deposit math (empty fund deposit still works)
    seed = ps.apply_deposit(conn, b_id, D("100"), D("100"), now + 1)
    assert seed["ok"]
    # Bump mark so there is profit
    r = ps.apply_convert(conn, b_id, D("120"), now + 2)
    assert r["ok"] is False and r["reason"] == "no_admin"

    a = ps.create_partner(conn, name="Admin", is_admin=True, now_ms=now + 3)
    a_id = a["partner_id"]
    ps.apply_deposit(conn, a_id, D("1000"), D("1120"), now + 4)  # V incl
    # Close B at profit, then ops refuse closed
    # Current U and V: need consistent V. Simpler path: deposit admin first in new db
    conn.close()

    conn = open_db(tmp_path / "refuse2.db")
    a = ps.create_partner(conn, name="Admin", is_admin=True, now_ms=now)
    b = ps.create_partner(conn, name="Bob", is_admin=False, now_ms=now + 1)
    a_id, b_id = a["partner_id"], b["partner_id"]
    ps.apply_deposit(conn, a_id, D("1000"), D("1000"), now + 10)
    ps.apply_deposit(conn, b_id, D("100"), D("1100"), now + 20)
    ps.apply_close(conn, b_id, D("900"), now + 30)  # loss close
    r2 = ps.apply_convert(conn, b_id, D("900"), now + 40)
    assert r2["ok"] is False and r2["reason"] == "closed"
    r3 = ps.apply_deposit(conn, b_id, D("10"), D("910"), now + 50)
    assert r3["ok"] is False and r3["reason"] == "closed"
    conn.close()


def test_adjust_cost_and_reason_short(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "adj.db")
    now = 1_700_000_000_000
    a = ps.create_partner(conn, name="Admin", is_admin=True, now_ms=now)
    a_id = a["partner_id"]
    ps.apply_deposit(conn, a_id, D("100"), D("100"), now + 1)
    bad = ps.apply_adjust_cost(conn, a_id, D("80"), "short", D("100"), now + 2)
    assert bad["ok"] is False and bad["reason"] == "reason_short"
    ok = ps.apply_adjust_cost(conn, a_id, D("80"), "admin fix cost basis", D("100"), now + 3)
    assert ok["ok"] is True
    assert _cost(conn, a_id) == D("80")
    assert _units(conn, a_id) == D("100")
    conn.close()
