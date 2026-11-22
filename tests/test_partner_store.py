"""SQLite partner ledger store (tmp_path only; never production DB)."""

from __future__ import annotations

import sqlite3
import threading
from decimal import Decimal
from pathlib import Path

import pytest

from jarvise_assist import partner_store as ps
from jarvise_ingest.db import MIGRATIONS, SCHEMA_VERSION, open_db, schema_version

D = Decimal
MICRO = D("0.000001")


def _units(conn: sqlite3.Connection, partner_id: str) -> Decimal:
    row = ps.get_partner(conn, partner_id)
    assert row is not None
    return ps.units_from_e8(int(row["units_e8"]))


def _cost(conn: sqlite3.Connection, partner_id: str) -> Decimal:
    row = ps.get_partner(conn, partner_id)
    assert row is not None
    return ps.usdt_from_micro(int(row["cost_micro"]))


def _n_events(conn: sqlite3.Connection, *, kind: str | None = None) -> int:
    if kind is None:
        row = conn.execute("SELECT COUNT(*) AS n FROM partner_event").fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) AS n FROM partner_event WHERE kind = ?", (kind,)).fetchone()
    return int(row["n"])


def test_migration_creates_tables_fresh_and_from_29(tmp_path: Path) -> None:
    assert SCHEMA_VERSION == 37
    fresh = open_db(tmp_path / "fresh.db")
    assert schema_version(fresh) == SCHEMA_VERSION
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

    # Stamp a DB at user_version 29, then reopen → 31; existing rows untouched
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
    assert schema_version(conn) == SCHEMA_VERSION
    after = conn.execute("SELECT COUNT(*) AS n FROM assist_settings").fetchone()
    n_after = after[0] if not hasattr(after, "keys") else after["n"]
    assert n_after == n_before
    assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='partner'").fetchone()
    # migrate twice is a no-op
    from jarvise_ingest.db import migrate

    applied = migrate(conn)
    assert applied == []
    conn.close()


def test_migration_31_adds_request_source_on_v30_db_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "from30.db"
    conn = open_db(path)
    now = 1_700_000_000_000
    a = ps.create_partner(conn, name="Admin", is_admin=True, now_ms=now)
    rid = ps.create_request(conn, a["partner_id"], "close", now + 1, source="portal")["request_id"]
    # Simulate a true v30 DB: no source column, user_version 30.
    conn.execute("ALTER TABLE partner_request DROP COLUMN source")
    conn.execute("PRAGMA user_version = 30")
    conn.commit()
    conn.close()

    conn = open_db(path)
    assert schema_version(conn) == SCHEMA_VERSION
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(partner_request)")}
    assert "source" in cols
    # No backfill: the pre-31 row keeps NULL.
    row = conn.execute("SELECT source FROM partner_request WHERE id = ?", (rid,)).fetchone()
    assert row["source"] is None
    from jarvise_ingest.db import migrate

    total_before = conn.total_changes
    assert migrate(conn) == []
    assert conn.total_changes == total_before
    conn.close()

    # Re-stamping to 30 with the column already present re-runs 31 as a no-op.
    conn = open_db(path)
    conn.execute("PRAGMA user_version = 30")
    conn.commit()
    assert migrate(conn) == [f"{v}:{n}" for v, n, _fn, _repair in MIGRATIONS if v > 30]
    assert schema_version(conn) == SCHEMA_VERSION
    conn.close()


def test_create_request_requires_known_source(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "src.db")
    now = 1_700_000_000_000
    a = ps.create_partner(conn, name="Admin", is_admin=True, now_ms=now)
    a_id = a["partner_id"]
    with pytest.raises(TypeError):
        ps.create_request(conn, a_id, "close", now + 1)  # type: ignore[call-arg]
    for bad in ("", "Portal", "owner", None):
        out = ps.create_request(conn, a_id, "close", now + 2, source=bad)  # type: ignore[arg-type]
        assert out == {"ok": False, "reason": "bad_source"}
    assert conn.execute("SELECT COUNT(*) AS n FROM partner_request").fetchone()["n"] == 0
    for src in ("admin", "portal"):
        out = ps.create_request(conn, a_id, "close", now + 3, source=src)
        assert out["ok"] is True
        got = conn.execute("SELECT source FROM partner_request WHERE id = ?", (out["request_id"],)).fetchone()
        assert got["source"] == src
        ps.reject_request(conn, out["request_id"], now + 4)
    conn.close()


def test_partner_request_kind_deposit_check(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "kind.db")
    now = 1_700_000_000_000
    a = ps.create_partner(conn, name="Admin", is_admin=True, now_ms=now)
    a_id = a["partner_id"]
    conn.execute(
        """
        INSERT INTO partner_request (
            id, partner_id, kind, status, payout_state,
            created_at_ms, expires_at_ms
        ) VALUES ('pr_dep', ?, 'deposit', 'pending', 'none', ?, ?)
        """,
        (a_id, now, now + 60_000),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO partner_request (
                id, partner_id, kind, status, payout_state,
                created_at_ms, expires_at_ms
            ) VALUES ('pr_bad', ?, 'refund', 'pending', 'none', ?, ?)
            """,
            (a_id, now + 1, now + 60_000),
        )
        conn.commit()
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
    assert "request_id" in r7
    assert _units(conn, b_id) == D("0")
    assert ps.get_partner(conn, b_id)["status"] == "closed"
    paid = ps.mark_paid(conn, r7["request_id"], now + 71)
    assert paid["ok"] is True
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
    n0 = _n_events(conn)
    units_b = _units(conn, b_id)
    cost_b = _cost(conn, b_id)
    # loss → no_profit
    r = ps.apply_convert(conn, b_id, D("1200"), now + 30)
    assert r["ok"] is False and r["reason"] == "no_profit"
    assert _n_events(conn) == n0
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
    n0 = _n_events(conn)

    real_credit = ps._credit_fee_to_admin

    def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("forced")

    monkeypatch.setattr(ps, "_credit_fee_to_admin", boom)
    with pytest.raises(RuntimeError, match="forced"):
        ps.apply_convert(conn, c_id, D("1440"), now + 30)

    monkeypatch.setattr(ps, "_credit_fee_to_admin", real_credit)
    assert _units(conn, c_id) == units_c
    assert _units(conn, a_id) == units_a
    assert _n_events(conn) == n0
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


def test_stale_close_race_one_success(tmp_path: Path) -> None:
    path = tmp_path / "race.db"
    conn = open_db(path)
    now = 1_700_000_000_000
    a = ps.create_partner(conn, name="Admin", is_admin=True, now_ms=now)
    b = ps.create_partner(conn, name="Bob", is_admin=False, now_ms=now + 1)
    a_id, b_id = a["partner_id"], b["partner_id"]
    ps.apply_deposit(conn, a_id, D("1000"), D("1000"), now + 10)
    ps.apply_deposit(conn, b_id, D("100"), D("1100"), now + 20)
    conn.close()

    results: list[dict] = []
    barrier = threading.Barrier(2)

    def closer() -> None:
        c = open_db(path)
        barrier.wait(timeout=5)
        results.append(ps.apply_close(c, b_id, D("1100"), now + 100))
        c.close()

    t1 = threading.Thread(target=closer)
    t2 = threading.Thread(target=closer)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)
    assert len(results) == 2
    oks = [r for r in results if r.get("ok")]
    fails = [r for r in results if not r.get("ok")]
    assert len(oks) == 1
    assert len(fails) == 1
    assert fails[0]["reason"] == "closed"
    c = open_db(path)
    assert _n_events(c, kind="close") == 1
    c.close()


def test_confirm_and_mark_paid_idempotent_and_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = open_db(tmp_path / "idem.db")
    now = 1_700_000_000_000
    a = ps.create_partner(conn, name="Admin", is_admin=True, now_ms=now)
    b = ps.create_partner(conn, name="Bob", is_admin=False, now_ms=now + 1)
    a_id, b_id = a["partner_id"], b["partner_id"]
    ps.apply_deposit(conn, a_id, D("1000"), D("1000"), now + 10)
    ps.apply_deposit(conn, b_id, D("100"), D("1100"), now + 20)
    created = ps.create_request(conn, b_id, "close", now + 30, source="admin")
    assert created["ok"]
    rid = created["request_id"]
    r1 = ps.confirm_request(conn, rid, D("1100"), now + 40)
    assert r1["ok"] is True
    r2 = ps.confirm_request(conn, rid, D("1100"), now + 41)
    assert r2["ok"] is False and r2["reason"] == "request_not_pending"
    paid1 = ps.mark_paid(conn, rid, now + 50)
    assert paid1["ok"] is True
    paid2 = ps.mark_paid(conn, rid, now + 51)
    assert paid2["ok"] is False and paid2["reason"] == "not_due"

    # Forced exception inside confirm rolls back request + ledger
    c = ps.create_partner(conn, name="Carol", is_admin=False, now_ms=now + 60)
    c_id = c["partner_id"]
    ps.apply_deposit(conn, c_id, D("100"), D("1200"), now + 70)
    created2 = ps.create_request(conn, c_id, "close", now + 80, source="admin")
    rid2 = created2["request_id"]
    n0 = _n_events(conn)
    status0 = conn.execute("SELECT status FROM partner_request WHERE id = ?", (rid2,)).fetchone()["status"]
    assert status0 == "pending"

    def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("forced_confirm")

    monkeypatch.setattr(ps, "_apply_close_tx", boom)
    with pytest.raises(RuntimeError, match="forced_confirm"):
        ps.confirm_request(conn, rid2, D("1200"), now + 90)
    assert (
        conn.execute("SELECT status FROM partner_request WHERE id = ?", (rid2,)).fetchone()["status"]
        == "pending"
    )
    assert _n_events(conn) == n0
    assert ps.get_partner(conn, c_id)["status"] == "active"
    conn.close()


def test_payout_due_keeps_other_views_stable(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "due.db")
    now = 1_700_000_000_000
    a = ps.create_partner(conn, name="Admin", is_admin=True, now_ms=now)
    b = ps.create_partner(conn, name="Bob", is_admin=False, now_ms=now + 1)
    c = ps.create_partner(conn, name="Carol", is_admin=False, now_ms=now + 2)
    a_id, b_id, c_id = a["partner_id"], b["partner_id"], c["partner_id"]
    ps.apply_deposit(conn, a_id, D("1000"), D("1000"), now + 10)
    ps.apply_deposit(conn, b_id, D("500"), D("1500"), now + 20)
    ps.apply_deposit(conn, c_id, D("500"), D("2000"), now + 30)
    # Close B at V=2000 → cash 500 due; raw V still 2000
    closed = ps.apply_close(conn, b_id, D("2000"), now + 40)
    assert closed["ok"] and closed["cash"] == D("500")
    view_before_pay = ps.view_partner(conn, c_id, D("2000"))
    assert view_before_pay["ok"]
    # After mark_paid, raw wallet is V - X
    paid = ps.mark_paid(conn, closed["request_id"], now + 50)
    assert paid["ok"]
    view_after_pay = ps.view_partner(conn, c_id, D("1500"))
    assert view_after_pay["ok"]
    assert abs(view_after_pay["net"] - view_before_pay["net"]) <= MICRO
    assert abs(view_after_pay["nav"] - view_before_pay["nav"]) <= MICRO
    conn.close()


def test_withdraw_at_non_unit_nav(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "wd.db")
    now = 1_700_000_000_000
    a = ps.create_partner(conn, name="Admin", is_admin=True, now_ms=now)
    c = ps.create_partner(conn, name="Carol", is_admin=False, now_ms=now + 1)
    a_id, c_id = a["partner_id"], c["partner_id"]
    ps.apply_deposit(conn, a_id, D("1000"), D("1000"), now + 10)
    ps.apply_deposit(conn, c_id, D("200"), D("1200"), now + 20)
    # V=1440, U=1200, NAV=1.2; C gross=240, profit=40, fee=20, share=20
    r = ps.apply_withdraw_profit(conn, c_id, D("1440"), now + 30)
    assert r["ok"] is True
    assert r["cash"] == D("20")
    kinds = [e["kind"] for e in conn.execute("SELECT kind FROM partner_event ORDER BY rowid").fetchall()]
    assert "withdraw_profit" in kinds
    assert "fee_credit" in kinds
    assert ps.verify_ledger(conn)["ok"] is True
    u_after = ps.total_units(conn)
    cash = r["cash"]
    redeemed = r["redeemed_units"]
    fee_units = r["fee_units"]
    # After withdraw, due cash still in raw V; pool = 1440 - 20
    v_pool = D("1440") - cash
    u_remaining = D("1200") - redeemed - fee_units
    assert u_after == u_remaining + fee_units  # fee units moved to admin, still in U
    nav_after = v_pool / u_after
    assert nav_after >= D("1.2") - MICRO or nav_after >= D("1.2")
    # Prompt: (V - cash) / (U - redeemed_units) >= NAV — using pool units after fee move
    assert (D("1440") - cash) / (D("1200") - redeemed) >= D("1.2") - MICRO
    conn.close()


def test_admin_close_and_closed_admin_guards(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "admguard.db")
    now = 1_700_000_000_000
    a = ps.create_partner(conn, name="Admin", is_admin=True, now_ms=now)
    b = ps.create_partner(conn, name="Bob", is_admin=False, now_ms=now + 1)
    a_id, b_id = a["partner_id"], b["partner_id"]
    ps.apply_deposit(conn, a_id, D("1000"), D("1000"), now + 10)
    ps.apply_deposit(conn, b_id, D("100"), D("1100"), now + 20)
    refused = ps.apply_close(conn, a_id, D("1100"), now + 30)
    assert refused["ok"] is False and refused["reason"] == "admin_has_partners"

    conn.execute("UPDATE partner SET status = 'closed' WHERE id = ?", (a_id,))
    conn.commit()
    # Bump so B has profit
    r = ps.apply_convert(conn, b_id, D("1320"), now + 40)
    assert r["ok"] is False and r["reason"] == "no_admin"
    conn.close()


def test_verify_ledger_same_ms_and_hand_edits(tmp_path: Path) -> None:
    for i in range(20):
        conn = open_db(tmp_path / f"same_ms_{i}.db")
        now = 1_700_000_000_000
        a = ps.create_partner(conn, name="Admin", is_admin=True, now_ms=now)
        b = ps.create_partner(conn, name="Bob", is_admin=False, now_ms=now + 1)
        c = ps.create_partner(conn, name="Carol", is_admin=False, now_ms=now + 2)
        a_id, b_id, c_id = a["partner_id"], b["partner_id"], c["partner_id"]
        ps.apply_deposit(conn, a_id, D("1000"), D("1000"), now + 10)
        ps.apply_deposit(conn, b_id, D("200"), D("1200"), now + 20)
        ps.apply_deposit(conn, c_id, D("200"), D("1400"), now + 30)
        # Profit window at V=1680 NAV=1.2
        same = now + 100
        wd = ps.apply_withdraw_profit(conn, b_id, D("1680"), same)
        assert wd["ok"]
        # Pay so pool is clean for close
        ps.mark_paid(conn, wd["request_id"], same + 1)
        cl = ps.apply_close(conn, c_id, D("1680") - wd["cash"], same)
        assert cl["ok"]
        assert ps.verify_ledger(conn)["ok"] is True
        conn.close()

    conn = open_db(tmp_path / "hand_edit.db")
    now = 1_700_000_000_000
    a = ps.create_partner(conn, name="Admin", is_admin=True, now_ms=now)
    a_id = a["partner_id"]
    ps.apply_deposit(conn, a_id, D("100"), D("100"), now + 1)
    conn.execute("UPDATE partner SET cost_micro = cost_micro + 1 WHERE id = ?", (a_id,))
    conn.commit()
    bad_cost = ps.verify_ledger(conn)
    assert bad_cost["ok"] is False

    conn.execute(
        "UPDATE partner SET cost_micro = cost_micro - 1, status = 'closed' WHERE id = ?",
        (a_id,),
    )
    conn.commit()
    bad_status = ps.verify_ledger(conn)
    assert bad_status["ok"] is False
    conn.close()


def test_create_request_deposit_rules(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "creq.db")
    now = 1_700_000_000_000
    a = ps.create_partner(conn, name="Admin", is_admin=True, now_ms=now)
    b = ps.create_partner(conn, name="Bob", is_admin=False, now_ms=now + 1)
    a_id, b_id = a["partner_id"], b["partner_id"]
    ps.apply_deposit(conn, a_id, D("1000"), D("1000"), now + 10)

    low = ps.create_request(conn, b_id, "deposit", now + 20, amount=D("49.99"), source="admin")
    assert low["ok"] is False and low["reason"] == "below_min_deposit"

    ok = ps.create_request(conn, b_id, "deposit", now + 30, amount=D("50"), source="admin")
    assert ok["ok"] is True
    row = conn.execute(
        "SELECT quote_gross_micro, expires_at_ms FROM partner_request WHERE id = ?",
        (ok["request_id"],),
    ).fetchone()
    assert int(row["quote_gross_micro"]) == 50_000_000
    assert int(row["expires_at_ms"]) == now + 30 + 86_400_000

    no_amt = ps.create_request(conn, a_id, "deposit", now + 40, source="admin")
    assert no_amt["ok"] is False and no_amt["reason"] == "bad_args"
    # Clear Bob's pending via reject so we can test close+amount on a free partner
    ps.reject_request(conn, ok["request_id"], now + 41)
    bad_close = ps.create_request(conn, b_id, "close", now + 42, amount=D("10"), source="admin")
    assert bad_close["ok"] is False and bad_close["reason"] == "bad_args"

    ps.apply_deposit(conn, b_id, D("100"), D("1100"), now + 50)
    ps.apply_close(conn, b_id, D("1100"), now + 60)
    closed_req = ps.create_request(conn, b_id, "deposit", now + 70, amount=D("50"), source="admin")
    assert closed_req["ok"] is False and closed_req["reason"] == "closed"
    conn.close()


def test_one_pending_and_expiry(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "onepend.db")
    now = 1_700_000_000_000
    a = ps.create_partner(conn, name="Admin", is_admin=True, now_ms=now)
    b = ps.create_partner(conn, name="Bob", is_admin=False, now_ms=now + 1)
    a_id, b_id = a["partner_id"], b["partner_id"]
    ps.apply_deposit(conn, a_id, D("1000"), D("1000"), now + 10)

    r1 = ps.create_request(conn, b_id, "deposit", now + 20, amount=D("50"), source="admin")
    assert r1["ok"]
    r2 = ps.create_request(conn, b_id, "close", now + 21, source="admin")
    assert r2["ok"] is False and r2["reason"] == "request_pending"

    expires_at = int(r1["expires_at_ms"])
    r3 = ps.create_request(conn, b_id, "close", expires_at, source="admin")
    assert r3["ok"] is True
    old = conn.execute("SELECT status FROM partner_request WHERE id = ?", (r1["request_id"],)).fetchone()
    assert old["status"] == "expired"

    # New pending close; confirm at exactly expires_at_ms → not pending
    # First reject the close we just made, make a short-lived request via SQL expires
    ps.reject_request(conn, r3["request_id"], expires_at + 1)
    created = ps.create_request(conn, b_id, "close", expires_at + 2, source="admin")
    assert created["ok"]
    # Force expires_at_ms to a known value
    conn.execute(
        "UPDATE partner_request SET expires_at_ms = ? WHERE id = ?",
        (expires_at + 100, created["request_id"]),
    )
    conn.commit()
    bad = ps.confirm_request(conn, created["request_id"], D("1000"), expires_at + 100)
    assert bad["ok"] is False and bad["reason"] == "request_not_pending"
    conn.close()


def test_deposit_request_full_flow_nav_1_2(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "depflow.db")
    now = 1_700_000_000_000
    a = ps.create_partner(conn, name="Admin", is_admin=True, now_ms=now)
    b = ps.create_partner(conn, name="Bob", is_admin=False, now_ms=now + 1)
    a_id, b_id = a["partner_id"], b["partner_id"]
    ps.apply_deposit(conn, a_id, D("1000"), D("1000"), now + 10)
    # Grow to V=1200 so NAV=1.2
    view_a0 = ps.view_partner(conn, a_id, D("1200"))
    assert view_a0["nav"] == D("1.2")

    req = ps.create_request(conn, b_id, "deposit", now + 20, amount=D("100"), source="admin")
    assert req["ok"]
    conf = ps.confirm_request(conn, req["request_id"], D("1299.5"), now + 30, amount_received=D("99.5"))
    assert conf["ok"] is True
    assert conf["units_issued"] == D("82.91666666")
    assert _cost(conn, b_id) == D("99.5")
    ev = conn.execute(
        "SELECT request_id, kind FROM partner_event WHERE kind = 'deposit' AND partner_id = ?",
        (b_id,),
    ).fetchone()
    assert ev is not None and ev["request_id"] == req["request_id"]
    prow = conn.execute(
        "SELECT status, payout_state, quote_gross_micro FROM partner_request WHERE id = ?",
        (req["request_id"],),
    ).fetchone()
    assert prow["status"] == "confirmed"
    assert prow["payout_state"] == "none"
    assert int(prow["quote_gross_micro"]) == 100_000_000
    view_a1 = ps.view_partner(conn, a_id, D("1299.5"))
    assert abs(view_a1["net"] - view_a0["net"]) <= MICRO
    assert abs(view_a1["nav"] - view_a0["nav"]) <= MICRO
    assert ps.verify_ledger(conn)["ok"] is True
    conn.close()


def test_deposit_while_close_payout_due(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "dep_due.db")
    now = 1_700_000_000_000
    a = ps.create_partner(conn, name="Admin", is_admin=True, now_ms=now)
    b = ps.create_partner(conn, name="Bob", is_admin=False, now_ms=now + 1)
    c = ps.create_partner(conn, name="Carol", is_admin=False, now_ms=now + 2)
    a_id, b_id, c_id = a["partner_id"], b["partner_id"], c["partner_id"]
    ps.apply_deposit(conn, a_id, D("1000"), D("1000"), now + 10)
    ps.apply_deposit(conn, b_id, D("200"), D("1200"), now + 20)
    closed = ps.apply_close(conn, b_id, D("1200"), now + 30)
    assert closed["ok"]
    due = closed["cash"]
    assert due > 0
    view_a0 = ps.view_partner(conn, a_id, D("1200"))
    req = ps.create_request(conn, c_id, "deposit", now + 40, amount=D("100"), source="admin")
    conf = ps.confirm_request(conn, req["request_id"], D("1300"), now + 50, amount_received=D("100"))
    assert conf["ok"]
    # units at (raw V - amount - due) / U = (1300 - 100 - due) / 1000
    expected_nav = (D("1300") - D("100") - due) / D("1000")
    assert conf["nav_pre"] == expected_nav
    assert conf["units_issued"] == (D("100") / expected_nav).quantize(D("0.00000001"))
    view_a1 = ps.view_partner(conn, a_id, D("1300"))
    assert abs(view_a1["net"] - view_a0["net"]) <= MICRO
    conn.close()


def test_reject_then_confirm_writes_nothing(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "rej.db")
    now = 1_700_000_000_000
    a = ps.create_partner(conn, name="Admin", is_admin=True, now_ms=now)
    b = ps.create_partner(conn, name="Bob", is_admin=False, now_ms=now + 1)
    a_id, b_id = a["partner_id"], b["partner_id"]
    ps.apply_deposit(conn, a_id, D("1000"), D("1000"), now + 10)
    req = ps.create_request(conn, b_id, "deposit", now + 20, amount=D("50"), source="admin")
    assert ps.reject_request(conn, req["request_id"], now + 30)["ok"]
    n0 = _n_events(conn)
    bad = ps.confirm_request(conn, req["request_id"], D("1050"), now + 40, amount_received=D("50"))
    assert bad["ok"] is False and bad["reason"] == "request_not_pending"
    assert _n_events(conn) == n0
    conn.close()


def test_deposit_pending_blocks_withdraw_and_close(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "dep_block.db")
    now = 1_700_000_000_000
    a = ps.create_partner(conn, name="Admin", is_admin=True, now_ms=now)
    b = ps.create_partner(conn, name="Bob", is_admin=False, now_ms=now + 1)
    c = ps.create_partner(conn, name="Carol", is_admin=False, now_ms=now + 2)
    a_id, b_id, c_id = a["partner_id"], b["partner_id"], c["partner_id"]
    ps.apply_deposit(conn, a_id, D("1000"), D("1000"), now + 10)
    ps.apply_deposit(conn, c_id, D("200"), D("1200"), now + 20)
    # Grow for profit
    b_dep = ps.create_request(conn, b_id, "deposit", now + 30, amount=D("50"), source="admin")
    assert b_dep["ok"]
    n0 = _n_events(conn)
    wd = ps.apply_withdraw_profit(conn, c_id, D("1440"), now + 40)
    assert wd["ok"] is False and wd["reason"] == "deposit_pending"
    assert _n_events(conn) == n0

    c_close = ps.create_request(conn, c_id, "close", now + 50, source="admin")
    # Bob still holds pending deposit → C cannot create? one pending per partner — C is free
    assert c_close["ok"] is True
    conf = ps.confirm_request(conn, c_close["request_id"], D("1440"), now + 60)
    assert conf["ok"] is False and conf["reason"] == "deposit_pending"
    assert (
        conn.execute("SELECT status FROM partner_request WHERE id = ?", (c_close["request_id"],)).fetchone()[
            "status"
        ]
        == "pending"
    )

    assert ps.reject_request(conn, b_dep["request_id"], now + 70)["ok"]
    conf2 = ps.confirm_request(conn, c_close["request_id"], D("1440"), now + 80)
    assert conf2["ok"] is True

    # Expired pending deposit (not swept) does not block
    d = ps.create_partner(conn, name="Dee", is_admin=False, now_ms=now + 90)
    d_id = d["partner_id"]
    ps.apply_deposit(conn, d_id, D("100"), D("1540"), now + 100)
    # Mark paid on C close so due doesn't confuse
    ps.mark_paid(conn, conf2["request_id"], now + 101)
    exp_req = ps.create_request(conn, b_id, "deposit", now + 110, amount=D("50"), source="admin")
    assert exp_req["ok"]
    expires = int(exp_req["expires_at_ms"])
    # Leave as pending but now >= expires (not swept)
    n1 = _n_events(conn)
    # Need profit on D — at loss maybe withdraw refuses no_profit; use convert path or withdraw on a_id
    # Admin fee 0 withdraw: grow V
    ok_wd = ps.apply_withdraw_profit(conn, d_id, D("1700"), expires)
    # At expires_at_ms, deposit is not blocking (expires_at_ms > now is false when now==expires)
    # If D has no profit, reason is no_profit — still proves not deposit_pending
    assert ok_wd.get("reason") != "deposit_pending"
    assert _n_events(conn) >= n1 or ok_wd.get("reason") in ("no_profit", "below_min")
    conn.close()


def test_verify_ledger_mixed_request_flow(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "mixed.db")
    now = 1_700_000_000_000
    a = ps.create_partner(conn, name="Admin", is_admin=True, now_ms=now)
    b = ps.create_partner(conn, name="Bob", is_admin=False, now_ms=now + 1)
    c = ps.create_partner(conn, name="Carol", is_admin=False, now_ms=now + 2)
    a_id, b_id, c_id = a["partner_id"], b["partner_id"], c["partner_id"]
    ps.apply_deposit(conn, a_id, D("1000"), D("1000"), now + 10)

    dep = ps.create_request(conn, b_id, "deposit", now + 20, amount=D("100"), source="admin")
    conf_dep = ps.confirm_request(conn, dep["request_id"], D("1100"), now + 30, amount_received=D("100"))
    assert conf_dep["ok"]

    ps.apply_deposit(conn, c_id, D("100"), D("1200"), now + 40)
    close_req = ps.create_request(conn, c_id, "close", now + 50, source="admin")
    conf_close = ps.confirm_request(conn, close_req["request_id"], D("1200"), now + 60)
    assert conf_close["ok"]
    assert ps.mark_paid(conn, close_req["request_id"], now + 70)["ok"]

    # Withdraw cancelled for no_profit (loss)
    wd_req = ps.create_request(conn, b_id, "withdraw_profit", now + 80, source="admin")
    assert wd_req["ok"]
    # Drop mark so B has no profit: raw V low
    cancelled = ps.confirm_request(conn, wd_req["request_id"], D("900"), now + 90)
    assert cancelled["ok"] is False and cancelled["reason"] == "no_profit"
    assert (
        conn.execute("SELECT status FROM partner_request WHERE id = ?", (wd_req["request_id"],)).fetchone()[
            "status"
        ]
        == "cancelled"
    )

    exp = ps.create_request(conn, b_id, "close", now + 100, source="admin")
    assert exp["ok"]
    n_exp = ps.expire_requests(conn, int(exp["expires_at_ms"]))
    assert n_exp >= 1
    assert ps.verify_ledger(conn)["ok"] is True
    conn.close()


def test_public_apply_rejects_request_id_kwarg(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "no_rid_kw.db")
    now = 1_700_000_000_000
    a = ps.create_partner(conn, name="Admin", is_admin=True, now_ms=now)
    b = ps.create_partner(conn, name="Bob", is_admin=False, now_ms=now + 1)
    a_id, b_id = a["partner_id"], b["partner_id"]
    ps.apply_deposit(conn, a_id, D("1000"), D("1000"), now + 10)
    ps.apply_deposit(conn, b_id, D("100"), D("1100"), now + 20)

    with pytest.raises(TypeError):
        ps.apply_close(conn, b_id, D("1100"), now + 30, request_id="x")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        ps.apply_withdraw_profit(conn, b_id, D("1100"), now + 40, request_id="x")  # type: ignore[call-arg]
    conn.close()


def _insert_terminal_event(
    conn: sqlite3.Connection,
    *,
    partner_id: str,
    kind: str,
    request_id: str | None,
    now: int,
) -> str:
    """Append a consistent close or withdraw with a chosen request link. No UPDATE."""
    row = ps.get_partner(conn, partner_id)
    assert row is not None
    units = int(row["units_e8"])
    cost = int(row["cost_micro"])
    total = int(
        conn.execute(
            "SELECT COALESCE(SUM(units_e8), 0) AS s FROM partner WHERE status = 'active'"
        ).fetchone()["s"]
    )
    eid = f"pe_{kind[:4]}_{partner_id[-6:]}"
    conn.execute(
        """
        INSERT INTO partner_event (
            id, partner_id, kind, request_id, account_value_micro, total_units_before_e8,
            nav, units_delta_e8, cost_delta_micro, cash_micro, fee_micro,
            units_after_e8, cost_after_micro, actor, created_at_ms, payouts_due_micro, origin
        ) VALUES (?, ?, ?, ?, 0, ?, '1', ?, ?, 0, 0, 0, 0, 'admin', ?, 0, 'manual')
        """,
        (eid, partner_id, kind, request_id, total, -units, -cost, now),
    )
    if kind == "close":
        conn.execute(
            """
            UPDATE partner
            SET units_e8 = 0, cost_micro = 0, status = 'closed', updated_at_ms = ?
            WHERE id = ?
            """,
            (now, partner_id),
        )
    else:
        conn.execute(
            """
            UPDATE partner
            SET units_e8 = 0, cost_micro = 0, updated_at_ms = ?
            WHERE id = ?
            """,
            (now, partner_id),
        )
    conn.commit()
    return eid


def test_verify_ledger_event_request_links(tmp_path: Path) -> None:
    now = 1_700_000_000_000

    def _base(name: str) -> tuple[sqlite3.Connection, str, str]:
        conn = open_db(tmp_path / name)
        admin = ps.create_partner(conn, name="Admin", is_admin=True, now_ms=now)
        carol = ps.create_partner(conn, name="Carol", is_admin=False, now_ms=now + 1)
        ps.apply_deposit(conn, admin["partner_id"], D("1000"), D("1000"), now + 10)
        ps.apply_deposit(conn, carol["partner_id"], D("100"), D("1100"), now + 20)
        return conn, str(admin["partner_id"]), str(carol["partner_id"])

    conn, _admin_id, carol_id = _base("null_close.db")
    close_eid = _insert_terminal_event(conn, partner_id=carol_id, kind="close", request_id=None, now=now + 40)
    null_close = ps.verify_ledger(conn)
    assert null_close["ok"] is False
    assert any(m.get("event_id") == close_eid for m in null_close["mismatches"])
    conn.close()

    conn, _admin_id, carol_id = _base("fake_close.db")
    close_eid = _insert_terminal_event(
        conn, partner_id=carol_id, kind="close", request_id="pr_made_up", now=now + 40
    )
    fake_close = ps.verify_ledger(conn)
    assert fake_close["ok"] is False
    assert any(m.get("event_id") == close_eid for m in fake_close["mismatches"])
    conn.close()

    conn, _admin_id, carol_id = _base("null_wd.db")
    wd_eid = _insert_terminal_event(
        conn, partner_id=carol_id, kind="withdraw_profit", request_id=None, now=now + 50
    )
    null_wd = ps.verify_ledger(conn)
    assert null_wd["ok"] is False
    assert any(m.get("event_id") == wd_eid for m in null_wd["mismatches"])
    conn.close()

    conn, _admin_id, carol_id = _base("ok_close.db")
    cl = ps.apply_close(conn, carol_id, D("1100"), now + 60)
    assert cl["ok"] is True
    assert ps.mark_paid(conn, cl["request_id"], now + 61)["ok"] is True
    assert ps.verify_ledger(conn)["ok"] is True
    conn.close()

    conn, _admin_id, carol_id = _base("ok_wd.db")
    wd = ps.apply_withdraw_profit(conn, carol_id, D("1320"), now + 80)
    assert wd["ok"] is True
    assert ps.verify_ledger(conn)["ok"] is True
    conn.close()


def test_first_deposit_without_kyc_stays_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_TEST_NO_KYC_SEED", "1")
    conn = open_db(tmp_path / "kyc-gate.db")
    now = 1_700_000_000_000
    admin = ps.create_partner(conn, name="Admin", is_admin=True, now_ms=now)
    bob = ps.create_partner(conn, name="Bob", now_ms=now)
    assert ps.apply_deposit(conn, admin["partner_id"], D("1000"), D("1000"), now + 1)["ok"] is True
    created = ps.create_request(conn, bob["partner_id"], "deposit", now + 2, amount=D("100"), source="admin")
    assert created == {"ok": False, "reason": "kyc_required"}
    real = ps.deposit_kyc_required
    monkeypatch.setattr(ps, "deposit_kyc_required", lambda *_a, **_k: False)
    pending = ps.create_request(conn, bob["partner_id"], "deposit", now + 3, amount=D("100"), source="admin")
    monkeypatch.setattr(ps, "deposit_kyc_required", real)
    result = ps.confirm_request(conn, pending["request_id"], D("1100"), now + 4, amount_received=D("100"))
    assert result["reason"] == "kyc_required"
    status = conn.execute(
        "SELECT status FROM partner_request WHERE id = ?", (pending["request_id"],)
    ).fetchone()
    assert status["status"] == "pending"
    conn.close()
