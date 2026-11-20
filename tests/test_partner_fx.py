"""Partner THB payout record and USDT carry (migration 34). No Telegram."""

from __future__ import annotations

import ast
import sqlite3
import uuid
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any

import pytest

from jarvise_assist import partner_fx as fx
from jarvise_assist import partner_kyc as pk
from jarvise_assist import partner_store as ps
from jarvise_assist.partner_ledger import nav
from jarvise_ingest.db import SCHEMA_VERSION, migrate, open_db, schema_version

D = Decimal
NOW = 1_700_000_000_000
PAYOUT = 500_000_000
CHAIN = 10_000
SOLD = 499_000_000
LEFTOVER = 990_000
RATE = D("33.8800")
RATE_E4 = 338_800
GROSS = 1_690_612
TRADE = 4_227
WD = 2_000
BANK = 0
NET = GROSS - TRADE - WD - BANK
REASON = "แก้ไขยอดตามสลิป"


def _partner(conn: sqlite3.Connection, name: str, *, admin: bool = False, now_ms: int = NOW) -> str:
    created = ps.create_partner(conn, name=name, is_admin=admin, now_ms=now_ms)
    assert created["ok"] is True
    return str(created["partner_id"])


def _request(
    conn: sqlite3.Connection,
    partner_id: str,
    *,
    kind: str = "withdraw_profit",
    status: str = "confirmed",
    payout_state: str = "paid",
    payout_micro: int | None = PAYOUT,
    now_ms: int = NOW,
) -> str:
    rid = f"pr_{uuid.uuid4().hex[:16]}"
    paid_at = now_ms if payout_state == "paid" else None
    conn.execute(
        """
        INSERT INTO partner_request (
            id, partner_id, kind, status, payout_state, payout_micro,
            created_at_ms, expires_at_ms, decided_at_ms, paid_at_ms, source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'admin')
        """,
        (
            rid,
            partner_id,
            kind,
            status,
            payout_state,
            payout_micro,
            now_ms,
            now_ms + 86_400_000,
            now_ms,
            paid_at,
        ),
    )
    conn.commit()
    return rid


def _net(gross: int, trade: int, wd: int, bank: int, residual: int = 0) -> int:
    return gross - trade - wd - bank - residual


_AUTO_KYC = object()


def _record(
    conn: sqlite3.Connection,
    request_id: str,
    *,
    carry_in: int = 0,
    chain: int = CHAIN,
    sold: int = SOLD,
    rate: Decimal = RATE,
    trade: int = TRADE,
    wd: int = WD,
    bank: int = BANK,
    net: int = NET,
    ref_status: str = "off",
    ref_rate: Decimal | None = None,
    ref_source: str | None = None,
    ref_fetched_at_ms: int | None = None,
    reason: str | None = None,
    slip_ref: str | None = None,
    paid_kyc_id: Any = _AUTO_KYC,
    now_ms: int = NOW,
) -> dict[str, Any]:
    if paid_kyc_id is _AUTO_KYC:
        row = conn.execute(
            """
            SELECT k.id
            FROM partner_kyc AS k
            JOIN partner_request AS r ON r.partner_id = k.partner_id
            WHERE r.id = ?
            ORDER BY k.version DESC
            LIMIT 1
            """,
            (request_id,),
        ).fetchone()
        paid_kyc_id = None if row is None else str(row["id"])
    return fx.record_payout_fx(
        conn,
        request_id=request_id,
        carry_in_micro=carry_in,
        chain_fee_micro=chain,
        usdt_sold_micro=sold,
        rate=rate,
        trade_fee_satang=trade,
        thb_wd_fee_satang=wd,
        bank_fee_satang=bank,
        net_satang=net,
        ref_status=ref_status,
        ref_rate=ref_rate,
        ref_source=ref_source,
        ref_fetched_at_ms=ref_fetched_at_ms,
        reason=reason,
        slip_ref=slip_ref,
        paid_kyc_id=paid_kyc_id,
        entered_by="42",
        now_ms=now_ms,
    )


def _insert_raw(conn: sqlite3.Connection, request_id: str, partner_id: str, **over: Any) -> None:
    sold = int(over.get("usdt_sold_micro", SOLD))
    rate_e4 = int(over.get("rate_e4", RATE_E4))
    gross = int(over.get("gross_satang", (sold * rate_e4) // 100_000_000))
    trade = int(over.get("trade_fee_satang", TRADE))
    wd = int(over.get("thb_wd_fee_satang", WD))
    bank = int(over.get("bank_fee_satang", BANK))
    residual = int(over.get("residual_satang", 0))
    payout = int(over.get("usdt_payout_micro", PAYOUT))
    carry = int(over.get("carry_in_micro", 0))
    chain = int(over.get("chain_fee_micro", CHAIN))
    vals: dict[str, Any] = {
        "id": f"px_{uuid.uuid4().hex[:16]}",
        "request_id": request_id,
        "partner_id": partner_id,
        "version": 1,
        "supersedes_id": None,
        "paid_kyc_id": None,
        "usdt_payout_micro": payout,
        "carry_in_micro": carry,
        "chain_fee_micro": chain,
        "usdt_sold_micro": sold,
        "usdt_leftover_micro": payout + carry - chain - sold,
        "rate_e4": rate_e4,
        "gross_satang": gross,
        "trade_fee_satang": trade,
        "thb_wd_fee_satang": wd,
        "bank_fee_satang": bank,
        "residual_satang": residual,
        "residual_tol_satang": fx.RESIDUAL_TOL_SATANG,
        "net_satang": _net(gross, trade, wd, bank, residual),
        "ref_status": "off",
        "ref_rate_e4": None,
        "ref_source": None,
        "ref_fetched_at_ms": None,
        "ref_error": None,
        "deviation_bps": None,
        "threshold_bps": fx.DEVIATION_THRESHOLD_BPS,
        "reason": None,
        "slip_ref": None,
        "sale_ref": None,
        "entered_by": "42",
        "entered_at_ms": NOW,
    }
    vals.update(over)
    cols = list(vals)
    sql = f"INSERT INTO partner_payout_fx ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
    conn.execute(sql, [vals[col] for col in cols])


def _dump(conn: sqlite3.Connection, table: str) -> list[tuple[Any, ...]]:
    cols = [str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")]
    quoted = ", ".join(cols)
    rows = conn.execute(f"SELECT {quoted} FROM {table} ORDER BY rowid").fetchall()
    return [tuple(row) for row in rows]


def test_migration_34_is_additive_and_repair_writes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "fx.db"
    conn = open_db(path)
    assert SCHEMA_VERSION == 35
    assert schema_version(conn) == 35
    for name in ("partner_payout_fx_draft", "partner_usdt_carry", "partner_payout_fx"):
        conn.execute(f"DROP TABLE {name}")
    conn.execute("ALTER TABLE partner_kyc_draft DROP COLUMN confirm_message_id")
    conn.execute("PRAGMA user_version = 33")
    conn.commit()
    draft_cols = {str(row["name"]) for row in conn.execute("PRAGMA table_info(partner_kyc_draft)")}
    assert "confirm_message_id" not in draft_cols
    admin = _partner(conn, "Admin", admin=True)
    bob = _partner(conn, "Bob", now_ms=NOW + 1)
    cara = _partner(conn, "Cara", now_ms=NOW + 2)
    rid = _request(conn, bob)
    assert ps.apply_deposit(conn, admin, D("1000"), D("1000"), NOW + 3)["ok"] is True
    before = {
        "partner": _dump(conn, "partner"),
        "partner_request": _dump(conn, "partner_request"),
        "partner_kyc": _dump(conn, "partner_kyc"),
        "partner_event": _dump(conn, "partner_event"),
    }
    assert before["partner_kyc"]
    conn.execute(
        """
        INSERT INTO partner_kyc_draft (
            partner_id, token, key_id,
            real_name_enc, phone_enc, account_no_enc, holder_name_enc,
            bank_code, account_last4, phone_last4, real_name_mask, holder_name_mask,
            holder_matches, account_fp, phone_fp, created_at_ms, expires_at_ms
        ) VALUES (?, 'abcd1234', 'k', ?, ?, ?, ?, 'KBANK', '0000', '0000', 'x***', 'x***', 1, 'fp', 'fp', ?, ?)
        """,
        (bob, b"\x01", b"\x02", b"\x03", b"\x04", NOW, NOW + 1),
    )
    conn.commit()
    conn.close()

    conn = open_db(path)
    assert schema_version(conn) == 35
    for table, rows in before.items():
        assert _dump(conn, table) == rows
    for name in ("partner_payout_fx", "partner_usdt_carry", "partner_payout_fx_draft"):
        assert conn.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()["n"] == 0
    names = {
        str(row["name"])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('trigger', 'index')")
    }
    for name in (
        "partner_payout_fx_no_update",
        "partner_payout_fx_no_delete",
        "partner_payout_fx_link",
        "partner_payout_fx_chain",
        "partner_usdt_carry_no_update",
        "partner_usdt_carry_no_delete",
        "partner_usdt_carry_balance",
        "ix_partner_payout_fx_partner",
    ):
        assert name in names
    assert cara
    assert rid
    draft = conn.execute("SELECT confirm_message_id FROM partner_kyc_draft").fetchone()
    assert draft is not None
    assert draft["confirm_message_id"] is None
    touched = conn.total_changes
    cookie = conn.execute("PRAGMA schema_version").fetchone()[0]
    assert migrate(conn) == []
    assert conn.total_changes == touched
    assert conn.execute("PRAGMA schema_version").fetchone()[0] == cookie
    conn.close()


def _draft(conn: sqlite3.Connection, partner_id: str, token: str) -> None:
    conn.execute(
        """
        INSERT INTO partner_kyc_draft (
            partner_id, token, key_id,
            real_name_enc, phone_enc, account_no_enc, holder_name_enc,
            bank_code, account_last4, phone_last4, real_name_mask, holder_name_mask,
            holder_matches, account_fp, phone_fp, created_at_ms, expires_at_ms
        ) VALUES (?, ?, 'k', ?, ?, ?, ?, 'KBANK', '0000', '0000', 'x***', 'x***', 1, 'fp', 'fp', ?, ?)
        """,
        (partner_id, token, b"\x01", b"\x02", b"\x03", b"\x04", NOW, NOW + 1),
    )
    conn.commit()


def test_draft_confirm_message_id_matches_partner_and_token(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "mid.db")
    bob = _partner(conn, "Bob")
    cara = _partner(conn, "Cara", now_ms=NOW + 1)
    _draft(conn, bob, "tok-bob")
    _draft(conn, cara, "tok-cara")
    assert pk.draft_confirm_message_id(conn, bob) is None
    assert pk.draft_confirm_message_id(conn, "pt_missing") is None
    assert pk.set_draft_confirm_message_id(conn, bob, "other", 11) is False
    assert pk.draft_confirm_message_id(conn, bob) is None
    assert pk.set_draft_confirm_message_id(conn, bob, "tok-bob", 77) is True
    assert pk.draft_confirm_message_id(conn, bob) == 77
    assert pk.draft_confirm_message_id(conn, cara) is None
    assert migrate(conn) == []
    assert pk.draft_confirm_message_id(conn, bob) == 77
    conn.close()


def test_fx_and_carry_are_immutable(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "imm.db")
    bob = _partner(conn, "Bob")
    rid = _request(conn, bob)
    saved = _record(conn, rid)
    assert saved["ok"] is True
    with pytest.raises(sqlite3.IntegrityError, match="partner_payout_fx immutable"):
        conn.execute("UPDATE partner_payout_fx SET reason = 'xxxxxxxxxx'")
    with pytest.raises(sqlite3.IntegrityError, match="partner_payout_fx immutable"):
        conn.execute("DELETE FROM partner_payout_fx")
    with pytest.raises(sqlite3.IntegrityError, match="partner_usdt_carry immutable"):
        conn.execute("UPDATE partner_usdt_carry SET reason = 'xxxxxxxxxx'")
    with pytest.raises(sqlite3.IntegrityError, match="partner_usdt_carry immutable"):
        conn.execute("DELETE FROM partner_usdt_carry")
    assert conn.execute("SELECT COUNT(*) AS n FROM partner_payout_fx").fetchone()["n"] == 1
    conn.close()


def test_check_math_floor_net_conservation_and_caps(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "math.db")
    bob = _partner(conn, "Bob")
    rid = _request(conn, bob)
    _insert_raw(conn, rid, bob)
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        _insert_raw(conn, _request(conn, bob, payout_micro=PAYOUT), bob, gross_satang=GROSS + 1)
    # 1 USDT at 33.8801 THB is 33.8801 THB; floor is 3388 satang, not 3389.
    fraction = _request(conn, bob, payout_micro=1_010_000)
    _insert_raw(
        conn,
        fraction,
        bob,
        usdt_payout_micro=1_010_000,
        usdt_sold_micro=1_000_000,
        rate_e4=338_801,
        trade_fee_satang=0,
        thb_wd_fee_satang=0,
        bank_fee_satang=0,
        gross_satang=3_388,
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        _insert_raw(
            conn,
            _request(conn, bob, payout_micro=1_010_000),
            bob,
            usdt_payout_micro=1_010_000,
            usdt_sold_micro=1_000_000,
            rate_e4=338_801,
            trade_fee_satang=0,
            thb_wd_fee_satang=0,
            bank_fee_satang=0,
            gross_satang=3_389,
        )
    with pytest.raises(sqlite3.IntegrityError):
        _insert_raw(conn, _request(conn, bob), bob, trade_fee_satang=TRADE + 1, net_satang=NET)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_raw(conn, _request(conn, bob), bob, thb_wd_fee_satang=WD + 1, net_satang=NET)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_raw(conn, _request(conn, bob), bob, bank_fee_satang=1, net_satang=NET)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_raw(conn, _request(conn, bob), bob, usdt_leftover_micro=LEFTOVER - 1)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_raw(conn, _request(conn, bob), bob, usdt_leftover_micro=-1)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_raw(
            conn,
            _request(conn, bob, payout_micro=10_000_000_000_001 + CHAIN),
            bob,
            usdt_payout_micro=10_000_000_000_001 + CHAIN,
            usdt_sold_micro=10_000_000_000_001,
            rate_e4=1,
            trade_fee_satang=0,
            thb_wd_fee_satang=0,
            bank_fee_satang=0,
        )
    with pytest.raises(sqlite3.IntegrityError):
        _insert_raw(conn, _request(conn, bob), bob, rate_e4=1_000_001)
    sold_cap = 1_000_000_000_000
    cap_gross = (sold_cap * 1_000_000) // 100_000_000
    assert cap_gross == 10_000_000_000
    capped = _request(conn, bob, payout_micro=sold_cap)
    _insert_raw(
        conn,
        capped,
        bob,
        usdt_payout_micro=sold_cap,
        chain_fee_micro=0,
        usdt_sold_micro=sold_cap,
        rate_e4=1_000_000,
        trade_fee_satang=0,
        thb_wd_fee_satang=0,
        bank_fee_satang=0,
        gross_satang=cap_gross,
    )
    conn.commit()
    stored = conn.execute(
        "SELECT gross_satang FROM partner_payout_fx WHERE request_id = ?",
        (capped,),
    ).fetchone()
    assert stored is not None
    assert stored["gross_satang"] == cap_gross
    with pytest.raises(sqlite3.IntegrityError):
        _insert_raw(
            conn,
            _request(conn, bob, payout_micro=sold_cap + 1),
            bob,
            usdt_payout_micro=sold_cap + 1,
            chain_fee_micro=0,
            usdt_sold_micro=sold_cap + 1,
            rate_e4=1_000_000,
            trade_fee_satang=0,
            thb_wd_fee_satang=0,
            bank_fee_satang=0,
        )
    with pytest.raises(sqlite3.IntegrityError):
        _insert_raw(conn, _request(conn, bob), bob, rate_e4=338_800.5)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_raw(conn, _request(conn, bob), bob, net_satang=float(NET) + 0.5)
    text_fee = _request(conn, bob, payout_micro=sold_cap)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO partner_payout_fx (
                id, request_id, partner_id, version, supersedes_id, paid_kyc_id,
                usdt_payout_micro, carry_in_micro, chain_fee_micro, usdt_sold_micro,
                usdt_leftover_micro, rate_e4, gross_satang, trade_fee_satang,
                thb_wd_fee_satang, bank_fee_satang, residual_satang, residual_tol_satang,
                net_satang, ref_status, ref_rate_e4, ref_source, ref_fetched_at_ms,
                ref_error, deviation_bps, threshold_bps, reason, slip_ref, sale_ref,
                entered_by, entered_at_ms
            )
            SELECT ?, ?, partner_id, version, supersedes_id, paid_kyc_id,
                usdt_payout_micro, carry_in_micro, chain_fee_micro, usdt_sold_micro,
                usdt_leftover_micro, rate_e4, gross_satang, 'x',
                thb_wd_fee_satang, bank_fee_satang, residual_satang, residual_tol_satang,
                net_satang, ref_status, ref_rate_e4, ref_source, ref_fetched_at_ms,
                ref_error, deviation_bps, threshold_bps, reason, slip_ref, sale_ref,
                entered_by, entered_at_ms
            FROM partner_payout_fx WHERE request_id = ?
            """,
            (f"px_{uuid.uuid4().hex[:16]}", text_fee, capped),
        )
    fx_id = conn.execute(
        "SELECT id FROM partner_payout_fx WHERE request_id = ?",
        (capped,),
    ).fetchone()["id"]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO partner_usdt_carry (
                id, partner_id, seq, kind, amount_micro, balance_after_micro,
                fx_id, reverses_id, reason, entered_by, created_at_ms
            ) VALUES (?, ?, 1, 'leftover', 'x', 1, ?, NULL, NULL, '42', ?)
            """,
            (f"pc_{uuid.uuid4().hex[:16]}", bob, fx_id, NOW),
        )
    conn.close()


def test_reason_required_null_does_not_pass(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "why.db")
    bob = _partner(conn, "Bob")

    def attempt(**over: Any) -> None:
        _insert_raw(conn, _request(conn, bob), bob, **over)

    with pytest.raises(sqlite3.IntegrityError):
        attempt(
            ref_status="ok",
            ref_rate_e4=338_800,
            ref_source="binance_th",
            ref_fetched_at_ms=NOW,
            deviation_bps=101,
        )
    attempt(
        ref_status="ok",
        ref_rate_e4=338_800,
        ref_source="binance_th",
        ref_fetched_at_ms=NOW,
        deviation_bps=101,
        reason=REASON,
    )
    attempt(
        ref_status="ok",
        ref_rate_e4=RATE_E4,
        ref_source="binance_th",
        ref_fetched_at_ms=NOW,
        deviation_bps=100,
    )
    with pytest.raises(sqlite3.IntegrityError):
        attempt(residual_satang=101)
    attempt(residual_satang=100)
    attempt(residual_satang=-100)
    with pytest.raises(sqlite3.IntegrityError):
        attempt(residual_satang=-101)
    with pytest.raises(sqlite3.IntegrityError):
        attempt(
            deviation_bps=101,
            ref_status="ok",
            ref_rate_e4=338_800,
            ref_source="binance_th",
            ref_fetched_at_ms=NOW,
            reason="123456789",
        )
    with pytest.raises(sqlite3.IntegrityError):
        attempt(residual_satang=101, reason="          ")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        attempt(residual_satang=101, reason="\t" * 12)
    with pytest.raises(sqlite3.IntegrityError):
        attempt(residual_satang=101, reason=None)
    first = _request(conn, bob)
    _insert_raw(conn, first, bob)
    conn.commit()
    base = conn.execute("SELECT id FROM partner_payout_fx WHERE request_id = ?", (first,)).fetchone()["id"]
    with pytest.raises(sqlite3.IntegrityError):
        _insert_raw(conn, first, bob, version=2, supersedes_id=base, reason=None)
    _insert_raw(conn, first, bob, version=2, supersedes_id=base, reason="ก" * 10)
    conn.commit()
    conn.execute(
        """
        INSERT INTO partner_usdt_carry (
            id, partner_id, seq, kind, amount_micro, balance_after_micro,
            fx_id, reverses_id, reason, entered_by, created_at_ms
        ) VALUES (?, ?, 1, 'leftover', 1000, 1000, ?, NULL, NULL, '42', ?)
        """,
        (f"pc_{uuid.uuid4().hex[:16]}", bob, base, NOW),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        conn.execute(
            """
            INSERT INTO partner_usdt_carry (
                id, partner_id, seq, kind, amount_micro, balance_after_micro,
                fx_id, reverses_id, reason, entered_by, created_at_ms
            ) VALUES (?, ?, 2, 'settle', -100, 900, NULL, NULL, ?, '42', ?)
            """,
            (f"pc_{uuid.uuid4().hex[:16]}", bob, "\t" * 12, NOW),
        )
    conn.rollback()
    conn.execute(
        """
        INSERT INTO partner_usdt_carry (
            id, partner_id, seq, kind, amount_micro, balance_after_micro,
            fx_id, reverses_id, reason, entered_by, created_at_ms
        ) VALUES (?, ?, 2, 'settle', -100, 900, NULL, NULL, ?, '42', ?)
        """,
        (f"pc_{uuid.uuid4().hex[:16]}", bob, "ก" * 10, NOW),
    )
    conn.commit()
    conn.close()


def test_reference_shape(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "ref.db")
    bob = _partner(conn, "Bob")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_raw(
            conn,
            _request(conn, bob),
            bob,
            ref_status="ok",
            ref_rate_e4=None,
            deviation_bps=None,
        )
    with pytest.raises(sqlite3.IntegrityError):
        _insert_raw(
            conn, _request(conn, bob), bob, ref_status="unavailable", ref_rate_e4=RATE_E4, deviation_bps=None
        )
    _insert_raw(conn, _request(conn, bob), bob, ref_status="unavailable", ref_error="no_book")
    _insert_raw(conn, _request(conn, bob), bob, ref_status="off")
    conn.commit()
    conn.close()


def test_link_trigger_accepts_only_a_paid_cash_payout(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "link.db")
    bob = _partner(conn, "Bob")
    cara = _partner(conn, "Cara", now_ms=NOW + 1)
    due = _request(conn, bob, payout_state="due")
    with pytest.raises(sqlite3.IntegrityError, match="partner_payout_fx link"):
        _insert_raw(conn, due, bob)
    deposit = _request(conn, bob, kind="deposit", payout_state="paid", payout_micro=PAYOUT)
    with pytest.raises(sqlite3.IntegrityError, match="partner_payout_fx link"):
        _insert_raw(conn, deposit, bob)
    pending = _request(conn, bob, status="pending", payout_state="paid")
    with pytest.raises(sqlite3.IntegrityError, match="partner_payout_fx link"):
        _insert_raw(conn, pending, bob)
    mismatch = _request(conn, bob, payout_micro=PAYOUT - 1)
    with pytest.raises(sqlite3.IntegrityError, match="partner_payout_fx link"):
        _insert_raw(conn, mismatch, bob, usdt_payout_micro=PAYOUT)
    with pytest.raises(sqlite3.IntegrityError, match="partner_payout_fx link"):
        _insert_raw(conn, _request(conn, bob), cara)
    with pytest.raises(sqlite3.IntegrityError, match="partner_payout_fx link"):
        _insert_raw(conn, f"pr_{uuid.uuid4().hex[:16]}", bob)
    marked = _request(conn, bob, payout_state="due")
    assert ps.mark_paid(conn, marked, NOW + 2)["ok"] is True
    _insert_raw(conn, marked, bob)
    conn.commit()
    same = _request(conn, bob, payout_state="due")
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "UPDATE partner_request SET payout_state = 'paid', paid_at_ms = ? WHERE id = ?",
        (NOW + 3, same),
    )
    _insert_raw(conn, same, bob)
    conn.commit()
    assert conn.execute("SELECT COUNT(*) AS n FROM partner_payout_fx").fetchone()["n"] == 2
    conn.close()


def test_chain_trigger_blocks_gaps_forks_and_a_second_root(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "chain.db")
    bob = _partner(conn, "Bob")
    first = _request(conn, bob)
    other = _request(conn, bob)
    _insert_raw(conn, first, bob)
    _insert_raw(conn, other, bob)
    conn.commit()
    base = conn.execute("SELECT id FROM partner_payout_fx WHERE request_id = ?", (first,)).fetchone()["id"]
    other_id = conn.execute("SELECT id FROM partner_payout_fx WHERE request_id = ?", (other,)).fetchone()[
        "id"
    ]
    _insert_raw(conn, first, bob, version=2, supersedes_id=base, reason=REASON)
    conn.commit()
    v2 = conn.execute(
        "SELECT id FROM partner_payout_fx WHERE request_id = ? AND version = 2",
        (first,),
    ).fetchone()["id"]
    with pytest.raises(sqlite3.IntegrityError, match="partner_payout_fx chain"):
        _insert_raw(conn, first, bob, version=3, supersedes_id=base, reason=REASON)
    _insert_raw(conn, first, bob, version=3, supersedes_id=v2, reason=REASON)
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        _insert_raw(conn, first, bob, version=2, supersedes_id=base, reason=REASON)
    with pytest.raises(sqlite3.IntegrityError, match="partner_payout_fx chain"):
        _insert_raw(conn, other, bob, version=2, supersedes_id=base, reason=REASON)
    with pytest.raises(sqlite3.IntegrityError, match="partner_payout_fx chain"):
        _insert_raw(conn, first, bob, version=1, supersedes_id=None)
    assert other_id
    conn.close()


def test_carry_ledger_applies_the_whole_balance(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "carry.db")
    ada = _partner(conn, "Ada")
    bea = _partner(conn, "Bea", now_ms=NOW + 1)
    first = _request(conn, ada)
    saved = _record(conn, first, slip_ref="slip-not-logged")
    assert saved["ok"] is True
    assert saved["carry_after"] == LEFTOVER
    assert fx.carry_balance(conn, ada) == LEFTOVER
    assert fx.carry_balance(conn, bea) == 0
    second = _request(conn, ada, payout_micro=300_000_000, now_ms=NOW + 2)
    refused = _record(conn, second, carry_in=0, sold=300_000_000, trade=2_541, net=1_011_859)
    assert refused["reason"] == "carry_mismatch"
    gross2 = (300_000_000 * RATE_E4) // 100_000_000
    assert gross2 == 1_016_400
    net2 = _net(gross2, 2_541, WD, 0)
    kept = _record(
        conn,
        second,
        carry_in=LEFTOVER,
        sold=300_000_000,
        trade=2_541,
        net=net2,
        now_ms=NOW + 3,
    )
    assert kept["ok"] is True
    assert fx.carry_balance(conn, ada) == 980_000
    assert fx.carry_balance(conn, bea) == 0
    kinds = [row["kind"] for row in fx.carry_entries(conn, ada)]
    assert kinds == ["leftover", "apply", "leftover"]
    lone = _request(conn, bea, payout_micro=2_010_000, now_ms=NOW + 4)
    bea_gross = (1_000_000 * RATE_E4) // 100_000_000
    bea_saved = _record(
        conn,
        lone,
        sold=1_000_000,
        trade=0,
        wd=0,
        net=bea_gross,
        now_ms=NOW + 5,
    )
    assert bea_saved["ok"] is True
    assert fx.carry_balance(conn, ada) == 980_000
    assert fx.carry_balance(conn, bea) == 1_000_000
    entry = fx.carry_entries(conn, ada)[-1]
    with pytest.raises(sqlite3.IntegrityError, match="partner_usdt_carry balance"):
        conn.execute(
            """
            INSERT INTO partner_usdt_carry (
                id, partner_id, seq, kind, amount_micro, balance_after_micro,
                fx_id, reverses_id, reason, entered_by, created_at_ms
            ) VALUES (?, ?, 1, 'leftover', 1, 1, ?, NULL, NULL, '42', ?)
            """,
            (f"pc_{uuid.uuid4().hex[:16]}", ada, saved["fx_id"], NOW),
        )
    with pytest.raises(sqlite3.IntegrityError, match="partner_usdt_carry balance"):
        conn.execute(
            """
            INSERT INTO partner_usdt_carry (
                id, partner_id, seq, kind, amount_micro, balance_after_micro,
                fx_id, reverses_id, reason, entered_by, created_at_ms
            ) VALUES (?, ?, 4, 'leftover', 1, 1, ?, NULL, NULL, '42', ?)
            """,
            (f"pc_{uuid.uuid4().hex[:16]}", ada, saved["fx_id"], NOW),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO partner_usdt_carry (
                id, partner_id, seq, kind, amount_micro, balance_after_micro,
                fx_id, reverses_id, reason, entered_by, created_at_ms
            ) VALUES (?, ?, 4, 'apply', -1000000000, 0, ?, NULL, NULL, '42', ?)
            """,
            (f"pc_{uuid.uuid4().hex[:16]}", ada, saved["fx_id"], NOW),
        )
    with pytest.raises(sqlite3.IntegrityError, match="partner_usdt_carry balance"):
        conn.execute(
            """
            INSERT INTO partner_usdt_carry (
                id, partner_id, seq, kind, amount_micro, balance_after_micro,
                fx_id, reverses_id, reason, entered_by, created_at_ms
            ) VALUES (?, ?, 1, 'reverse', ?, 0, ?, ?, NULL, '42', ?)
            """,
            (
                f"pc_{uuid.uuid4().hex[:16]}",
                bea,
                -int(entry["amount_micro"]),
                saved["fx_id"],
                entry["id"],
                NOW,
            ),
        )
    conn.rollback()
    short = fx.settle_carry(conn, ada, 1, "short", "42", NOW + 6)
    assert short["reason"] == "reason_required"
    too_much = fx.carry_balance(conn, ada) + 1
    assert fx.settle_carry(conn, ada, too_much, REASON, "42", NOW + 7)["reason"] == "settle_exceeds"
    settled = fx.settle_carry(conn, ada, 1_000, REASON, "42", NOW + 8)
    assert settled["ok"] is True
    assert fx.carry_balance(conn, ada) == 979_000
    assert fx.carry_balance(conn, bea) == 1_000_000
    bea_entry = fx.carry_entries(conn, bea)[0]
    conn.execute(
        """
        INSERT INTO partner_usdt_carry (
            id, partner_id, seq, kind, amount_micro, balance_after_micro,
            fx_id, reverses_id, reason, entered_by, created_at_ms
        ) VALUES (?, ?, 2, 'reverse', ?, 0, ?, ?, NULL, '42', ?)
        """,
        (
            f"pc_{uuid.uuid4().hex[:16]}",
            bea,
            -int(bea_entry["amount_micro"]),
            str(bea_entry["fx_id"]),
            bea_entry["id"],
            NOW,
        ),
    )
    conn.commit()
    assert fx.carry_balance(conn, bea) == 0
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO partner_usdt_carry (
                id, partner_id, seq, kind, amount_micro, balance_after_micro,
                fx_id, reverses_id, reason, entered_by, created_at_ms
            ) VALUES (?, ?, 3, 'reverse', ?, ?, ?, ?, NULL, '42', ?)
            """,
            (
                f"pc_{uuid.uuid4().hex[:16]}",
                bea,
                -int(bea_entry["amount_micro"]),
                -int(bea_entry["amount_micro"]),
                str(bea_entry["fx_id"]),
                bea_entry["id"],
                NOW,
            ),
        )
    conn.close()


def test_correction_reverses_the_latest_and_blocks_a_consumed_leftover(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "fix.db")
    bob = _partner(conn, "Bob")
    first = _request(conn, bob)
    assert _record(conn, first)["ok"] is True
    base = fx.current_fx(conn, first)
    assert base is not None
    gross_bank = GROSS
    net_bank = _net(gross_bank, TRADE, WD, 1_000)
    changed = fx.record_correction(
        conn,
        base_fx_id=str(base["id"]),
        carry_in_micro=0,
        chain_fee_micro=CHAIN,
        usdt_sold_micro=SOLD,
        rate=RATE,
        trade_fee_satang=TRADE,
        thb_wd_fee_satang=WD,
        bank_fee_satang=1_000,
        net_satang=net_bank,
        ref_status="off",
        reason=REASON,
        entered_by="42",
        now_ms=NOW + 1,
    )
    assert changed["ok"] is True
    assert changed["version"] == 2
    assert fx.carry_balance(conn, bob) == LEFTOVER
    assert len(fx.fx_history(conn, first)) == 2
    second = _request(conn, bob, payout_micro=300_000_000, now_ms=NOW + 2)
    gross2 = (300_000_000 * RATE_E4) // 100_000_000
    assert _record(
        conn,
        second,
        carry_in=LEFTOVER,
        sold=300_000_000,
        trade=2_541,
        net=_net(gross2, 2_541, WD, 0),
        now_ms=NOW + 3,
    )["ok"]
    balance = fx.carry_balance(conn, bob)
    blocked = fx.record_correction(
        conn,
        base_fx_id=str(fx.current_fx(conn, first)["id"]),  # type: ignore[index]
        carry_in_micro=0,
        chain_fee_micro=CHAIN,
        usdt_sold_micro=498_000_000,
        rate=RATE,
        trade_fee_satang=TRADE,
        thb_wd_fee_satang=WD,
        bank_fee_satang=1_000,
        net_satang=_net((498_000_000 * RATE_E4) // 100_000_000, TRADE, WD, 1_000),
        ref_status="off",
        reason=REASON,
        entered_by="42",
        now_ms=NOW + 4,
    )
    assert blocked["reason"] == "carry_consumed"
    assert fx.carry_balance(conn, bob) == balance
    same = fx.record_correction(
        conn,
        base_fx_id=str(fx.current_fx(conn, first)["id"]),  # type: ignore[index]
        carry_in_micro=0,
        chain_fee_micro=CHAIN,
        usdt_sold_micro=SOLD,
        rate=RATE,
        trade_fee_satang=TRADE,
        thb_wd_fee_satang=WD,
        bank_fee_satang=1_000,
        net_satang=net_bank,
        ref_status="off",
        reason=REASON,
        entered_by="42",
        now_ms=NOW + 5,
    )
    assert same["ok"] is True
    assert fx.carry_balance(conn, bob) == balance
    latest = fx.current_fx(conn, second)
    assert latest is not None
    sold_new = 299_000_000
    leftover_new = 300_000_000 + LEFTOVER - CHAIN - sold_new
    gross_new = (sold_new * RATE_E4) // 100_000_000
    moved = fx.record_correction(
        conn,
        base_fx_id=str(latest["id"]),
        carry_in_micro=LEFTOVER,
        chain_fee_micro=CHAIN,
        usdt_sold_micro=sold_new,
        rate=RATE,
        trade_fee_satang=2_541,
        thb_wd_fee_satang=WD,
        bank_fee_satang=0,
        net_satang=_net(gross_new, 2_541, WD, 0),
        ref_status="off",
        reason=REASON,
        entered_by="42",
        now_ms=NOW + 6,
    )
    assert moved["ok"] is True
    assert fx.carry_balance(conn, bob) == leftover_new
    conn.close()


def test_record_is_atomic_when_the_carry_insert_aborts(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "atom.db")
    bob = _partner(conn, "Bob")
    rid = _request(conn, bob)
    conn.execute(
        """
        CREATE TRIGGER fx_test_abort_carry
        BEFORE INSERT ON partner_usdt_carry
        BEGIN
            SELECT RAISE(ABORT, 'test abort carry');
        END
        """
    )
    failed = _record(conn, rid)
    assert failed["reason"] == "db_constraint"
    assert conn.execute("SELECT COUNT(*) AS n FROM partner_payout_fx").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM partner_usdt_carry").fetchone()["n"] == 0
    conn.execute("DROP TRIGGER fx_test_abort_carry")
    assert _record(conn, rid)["ok"] is True
    conn.close()


def test_carry_does_not_change_pool_value(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "pool.db")
    admin = _partner(conn, "Admin", admin=True)
    bob = _partner(conn, "Bob", now_ms=NOW + 1)
    assert ps.apply_deposit(conn, admin, D("1000"), D("1000"), NOW + 2)["ok"] is True
    assert ps.apply_deposit(conn, bob, D("200"), D("1200"), NOW + 3)["ok"] is True
    _request(conn, bob, payout_state="due", payout_micro=50_000_000, now_ms=NOW + 4)
    wallet = D("1200")

    def snap() -> tuple[Decimal, Any, Any]:
        due = ps.payouts_due_total(conn)
        view = ps.view_partner(conn, bob, wallet)
        price = nav(wallet - due, ps.total_units(conn))
        return due, view, price

    before = snap()
    paid = _request(conn, bob, now_ms=NOW + 5)
    assert _record(conn, paid, now_ms=NOW + 6)["ok"] is True
    assert fx.carry_balance(conn, bob) == LEFTOVER
    assert snap() == before
    root = Path(__file__).resolve().parents[1] / "src" / "jarvise_assist"
    for name in ("partner_ledger.py", "partner_rules.py", "partner_store.py", "partner_audit.py"):
        text = (root / name).read_text(encoding="utf-8")
        assert "partner_usdt_carry" not in text
        assert "partner_payout_fx" not in text
    conn.close()


def test_compute_and_parsers_refuse_extra_decimals() -> None:
    assert parse_matches()
    figured = fx.compute(
        usdt_payout_micro=PAYOUT,
        carry_in_micro=0,
        chain_fee_micro=CHAIN,
        usdt_sold_micro=SOLD,
        rate=RATE,
        trade_fee_satang=TRADE,
        thb_wd_fee_satang=WD,
        bank_fee_satang=BANK,
        net_satang=NET,
    )
    assert figured["gross_satang"] == GROSS
    assert figured["leftover_micro"] == LEFTOVER
    assert figured["residual_satang"] == 0
    assert figured["refusals"] == []
    half_up = fx.compute(
        usdt_payout_micro=PAYOUT,
        carry_in_micro=0,
        chain_fee_micro=CHAIN,
        usdt_sold_micro=SOLD,
        rate=D("20.0010"),
        trade_fee_satang=0,
        thb_wd_fee_satang=0,
        bank_fee_satang=0,
        net_satang=1,
        ref_rate=D("20.0000"),
    )
    assert half_up["deviation_bps"] == 1
    half_down = fx.compute(
        usdt_payout_micro=PAYOUT,
        carry_in_micro=0,
        chain_fee_micro=CHAIN,
        usdt_sold_micro=SOLD,
        rate=D("19.9990"),
        trade_fee_satang=0,
        thb_wd_fee_satang=0,
        bank_fee_satang=0,
        net_satang=1,
        ref_rate=D("20.0000"),
    )
    assert half_down["deviation_bps"] == -1
    assert "rate_out_of_bounds" in half_down["refusals"]
    edges_lo = fx.compute(
        usdt_payout_micro=1_000_000,
        carry_in_micro=0,
        chain_fee_micro=0,
        usdt_sold_micro=1_000_000,
        rate=D("20"),
        trade_fee_satang=0,
        thb_wd_fee_satang=0,
        bank_fee_satang=0,
        net_satang=2_000,
    )
    edges_hi = fx.compute(
        usdt_payout_micro=1_000_000,
        carry_in_micro=0,
        chain_fee_micro=0,
        usdt_sold_micro=1_000_000,
        rate=D("60"),
        trade_fee_satang=0,
        thb_wd_fee_satang=0,
        bank_fee_satang=0,
        net_satang=6_000,
    )
    assert edges_lo["refusals"] == []
    assert edges_hi["refusals"] == []
    below = fx.compute(
        usdt_payout_micro=1_000_000,
        carry_in_micro=0,
        chain_fee_micro=0,
        usdt_sold_micro=1_000_000,
        rate=D("19.9999"),
        trade_fee_satang=0,
        thb_wd_fee_satang=0,
        bank_fee_satang=0,
        net_satang=1,
    )
    above = fx.compute(
        usdt_payout_micro=1_000_000,
        carry_in_micro=0,
        chain_fee_micro=0,
        usdt_sold_micro=1_000_000,
        rate=D("60.0001"),
        trade_fee_satang=0,
        thb_wd_fee_satang=0,
        bank_fee_satang=0,
        net_satang=1,
    )
    assert "rate_out_of_bounds" in below["refusals"]
    assert "rate_out_of_bounds" in above["refusals"]
    dusty = fx.compute(
        usdt_payout_micro=PAYOUT,
        carry_in_micro=0,
        chain_fee_micro=CHAIN,
        usdt_sold_micro=SOLD,
        rate=D("33.88001"),
        trade_fee_satang=TRADE,
        thb_wd_fee_satang=WD,
        bank_fee_satang=0,
        net_satang=NET,
    )
    assert dusty["refusals"] == ["too_many_decimals"]
    assert dusty["gross_satang"] is None
    step = fx.compute(
        usdt_payout_micro=PAYOUT,
        carry_in_micro=0,
        chain_fee_micro=CHAIN,
        usdt_sold_micro=499_500_000,
        rate=RATE,
        trade_fee_satang=TRADE,
        thb_wd_fee_satang=WD,
        bank_fee_satang=0,
        net_satang=1,
    )
    assert "sold_not_step" in step["refusals"]
    shown = fx.compute(
        usdt_payout_micro=PAYOUT,
        carry_in_micro=0,
        chain_fee_micro=CHAIN,
        usdt_sold_micro=SOLD,
        rate=RATE,
        trade_fee_satang=TRADE,
        thb_wd_fee_satang=WD,
        bank_fee_satang=0,
        net_satang=NET,
    )
    denom = PAYOUT - LEFTOVER
    expected = (D(NET) * D(10_000) / D(denom)).quantize(D("0.0001"), rounding=ROUND_DOWN)
    assert shown["effective_rate"] == expected
    assert shown["effective_rate"].as_tuple().exponent >= -4
    truncated = fx.compute(
        usdt_payout_micro=PAYOUT,
        carry_in_micro=0,
        chain_fee_micro=CHAIN,
        usdt_sold_micro=SOLD,
        rate=RATE,
        trade_fee_satang=D("42.27"),
        thb_wd_fee_satang=WD,
        bank_fee_satang=BANK,
        net_satang=NET,
    )
    fractional_sold = fx.compute(
        usdt_payout_micro=PAYOUT,
        carry_in_micro=0,
        chain_fee_micro=CHAIN,
        usdt_sold_micro=4_227.9,
        rate=RATE,
        trade_fee_satang=TRADE,
        thb_wd_fee_satang=WD,
        bank_fee_satang=BANK,
        net_satang=NET,
    )
    flagged = fx.compute(
        usdt_payout_micro=PAYOUT,
        carry_in_micro=0,
        chain_fee_micro=CHAIN,
        usdt_sold_micro=SOLD,
        rate=RATE,
        trade_fee_satang=True,
        thb_wd_fee_satang=WD,
        bank_fee_satang=BANK,
        net_satang=NET,
    )
    assert truncated["refusals"] == ["bad_input"]
    assert truncated["gross_satang"] is None
    assert fractional_sold["refusals"] == ["bad_input"]
    assert fractional_sold["gross_satang"] is None
    assert flagged["refusals"] == ["bad_input"]
    huge = fx.compute(
        usdt_payout_micro=PAYOUT,
        carry_in_micro=0,
        chain_fee_micro=CHAIN,
        usdt_sold_micro=SOLD,
        rate=D("1E+30"),
        trade_fee_satang=TRADE,
        thb_wd_fee_satang=WD,
        bank_fee_satang=BANK,
        net_satang=NET,
    )
    assert huge["refusals"] == ["rate_out_of_bounds"]


def parse_matches() -> bool:
    assert fx.parse_thb("16906.12") == GROSS
    assert fx.parse_thb("16906.123") is None
    assert fx.parse_thb("1.005") is None
    assert fx.parse_rate("33.8800") == RATE_E4
    assert fx.parse_rate("33.88001") is None
    assert fx.parse_usdt("0.01") == CHAIN
    assert fx.parse_usdt("499.99") == 499_990_000
    assert fx.parse_usdt("1.1234567") is None
    assert fx.parse_thb("๑๐๐") is None
    assert fx.parse_thb("１００") is None
    assert fx.parse_usdt("1" * 30) is None
    assert fx.parse_usdt("1" * 23) is None
    assert fx.parse_usdt("9" * 24) is None
    assert fx.parse_usdt("1" * 17) is None
    assert fx.parse_usdt("999999999999") == 999_999_999_999 * 1_000_000
    return True


def test_draft_round_trip_and_slip_is_not_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    conn = open_db(tmp_path / "draft.db")
    bob = _partner(conn, "Bob")
    rid = _request(conn, bob)
    caplog.set_level("INFO")
    saved = _record(conn, rid, bank=1_000, net=_net(GROSS, TRADE, WD, 1_000), slip_ref="SLIP-SECRET")
    assert saved["ok"] is True
    assert "SLIP-SECRET" not in caplog.text
    staged = fx.stage_fx_draft(
        conn,
        request_id=rid,
        mode="paid",
        payload={"rate": RATE, "sold_micro": SOLD},
        created_by="42",
        now_ms=NOW + 1,
    )
    assert staged["ok"] is True
    loaded = fx.load_fx_draft(conn, rid, str(staged["token"]), NOW + 2)
    assert loaded is not None
    assert loaded["payload"]["rate"] == str(RATE)
    assert fx.load_fx_draft(conn, rid, "nope", NOW + 2) is None
    assert fx.load_fx_draft(conn, rid, str(staged["token"]), int(staged["expires_at_ms"])) is None
    assert fx.delete_fx_draft(conn, rid) is True
    assert fx.load_fx_draft(conn, rid, str(staged["token"]), NOW + 2) is None
    conn.close()


def _correct(conn: sqlite3.Connection, request_id: str, **over: Any) -> dict[str, Any]:
    base = fx.current_fx(conn, request_id)
    assert base is not None
    fields: dict[str, Any] = {
        "base_fx_id": str(base["id"]),
        "carry_in_micro": int(base["carry_in_micro"]),
        "chain_fee_micro": int(base["chain_fee_micro"]),
        "usdt_sold_micro": int(base["usdt_sold_micro"]),
        "rate": RATE,
        "trade_fee_satang": int(base["trade_fee_satang"]),
        "thb_wd_fee_satang": int(base["thb_wd_fee_satang"]),
        "bank_fee_satang": int(base["bank_fee_satang"]),
        "net_satang": int(base["net_satang"]),
        "ref_status": "off",
        "reason": REASON,
        "entered_by": "42",
        "now_ms": NOW + 20 + int(base["version"]),
    }
    fields.update(over)
    return fx.record_correction(conn, **fields)


def test_repeated_correction_does_not_repost_a_used_leftover(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "repost.db")
    bob = _partner(conn, "Bob")
    first = _request(conn, bob)
    assert _record(conn, first)["ok"] is True
    assert fx.carry_balance(conn, bob) == LEFTOVER
    second = _request(conn, bob, payout_micro=300_000_000, now_ms=NOW + 1)
    gross2 = (300_000_000 * RATE_E4) // 100_000_000
    assert gross2 == 1_016_400
    assert _record(
        conn,
        second,
        carry_in=LEFTOVER,
        sold=300_000_000,
        trade=2_541,
        net=_net(gross2, 2_541, WD, 0),
        now_ms=NOW + 2,
    )["ok"]
    assert fx.carry_balance(conn, bob) == 980_000
    bank_net = _net(GROSS, TRADE, WD, 1_000)
    for version in (2, 3, 4):
        again = _correct(
            conn,
            first,
            bank_fee_satang=1_000,
            net_satang=bank_net,
            now_ms=NOW + version,
        )
        assert again["ok"] is True
        assert again["version"] == version
        assert fx.carry_balance(conn, bob) == 980_000
    consumed = _correct(
        conn,
        first,
        usdt_sold_micro=498_000_000,
        bank_fee_satang=1_000,
        net_satang=_net((498_000_000 * RATE_E4) // 100_000_000, TRADE, WD, 1_000),
        now_ms=NOW + 8,
    )
    assert consumed["reason"] == "carry_consumed"
    assert fx.carry_balance(conn, bob) == 980_000
    mismatch = _correct(
        conn,
        second,
        carry_in_micro=0,
        usdt_sold_micro=299_000_000,
        net_satang=_net((299_000_000 * RATE_E4) // 100_000_000, 2_541, WD, 0),
        now_ms=NOW + 9,
    )
    assert mismatch["reason"] == "carry_mismatch"
    assert fx.carry_balance(conn, bob) == 980_000
    for bank in (1_000, 2_000):
        latest = _correct(
            conn,
            second,
            bank_fee_satang=bank,
            net_satang=_net(gross2, 2_541, WD, bank),
            now_ms=NOW + 10 + bank,
        )
        assert latest["ok"] is True
        assert fx.carry_balance(conn, bob) == 980_000
    sold_new = 299_000_000
    leftover_new = 300_000_000 + LEFTOVER - CHAIN - sold_new
    moved = _correct(
        conn,
        second,
        usdt_sold_micro=sold_new,
        trade_fee_satang=2_541,
        bank_fee_satang=2_000,
        net_satang=_net((sold_new * RATE_E4) // 100_000_000, 2_541, WD, 2_000),
        now_ms=NOW + 14,
    )
    assert moved["ok"] is True
    assert fx.carry_balance(conn, bob) == leftover_new
    conn.close()


def test_zero_carry_base_cannot_grow_a_leftover_after_a_later_payout(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "zero.db")
    bob = _partner(conn, "Bob")
    payout = 500_010_000
    sold = 500_000_000
    gross = (sold * RATE_E4) // 100_000_000
    assert gross == 1_694_000
    trade = 4_235
    first = _request(conn, bob, payout_micro=payout)
    assert _record(
        conn,
        first,
        chain=CHAIN,
        sold=sold,
        trade=trade,
        wd=WD,
        bank=0,
        net=_net(gross, trade, WD, 0),
    )["ok"]
    assert fx.carry_balance(conn, bob) == 0
    later = _request(conn, bob, payout_micro=PAYOUT, now_ms=NOW + 1)
    assert _record(conn, later, now_ms=NOW + 2)["ok"] is True
    blocked = _correct(
        conn,
        first,
        usdt_sold_micro=499_000_000,
        trade_fee_satang=TRADE,
        thb_wd_fee_satang=WD,
        bank_fee_satang=0,
        net_satang=NET,
        now_ms=NOW + 3,
    )
    assert blocked["reason"] == "carry_consumed"
    assert fx.carry_balance(conn, bob) == LEFTOVER
    conn.close()


def test_trade_fee_above_int64_is_bad_input(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "wide.db")
    bob = _partner(conn, "Bob")
    rid = _request(conn, bob)
    assert _record(conn, rid)["ok"] is True
    fx_n = conn.execute("SELECT COUNT(*) AS n FROM partner_payout_fx").fetchone()["n"]
    carry_n = conn.execute("SELECT COUNT(*) AS n FROM partner_usdt_carry").fetchone()["n"]
    other = _request(conn, bob, now_ms=NOW + 1)
    kyc = conn.execute("SELECT id FROM partner_kyc WHERE partner_id = ?", (bob,)).fetchone()
    assert kyc is not None
    refused = fx.record_payout_fx(
        conn,
        request_id=other,
        carry_in_micro=0,
        chain_fee_micro=CHAIN,
        usdt_sold_micro=SOLD,
        rate=RATE,
        trade_fee_satang=10**19,
        thb_wd_fee_satang=WD,
        bank_fee_satang=BANK,
        net_satang=NET,
        ref_status="off",
        reason=REASON,
        paid_kyc_id=str(kyc["id"]),
        entered_by="42",
        now_ms=NOW + 2,
    )
    assert refused["reason"] == "bad_input"
    assert conn.execute("SELECT COUNT(*) AS n FROM partner_payout_fx").fetchone()["n"] == fx_n
    assert conn.execute("SELECT COUNT(*) AS n FROM partner_usdt_carry").fetchone()["n"] == carry_n
    conn.close()


def test_typeof_alone_rejects_non_integers(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "typeof.db")
    bob = _partner(conn, "Bob")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _insert_raw(conn, _request(conn, bob), bob, entered_at_ms="x")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _insert_raw(conn, _request(conn, bob, now_ms=NOW + 1), bob, threshold_bps=100.5)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _insert_raw(
            conn,
            _request(conn, bob, now_ms=NOW + 2),
            bob,
            ref_status="off",
            ref_fetched_at_ms=1.5,
        )
    kept = _request(conn, bob, now_ms=NOW + 3)
    _insert_raw(conn, kept, bob)
    conn.commit()
    fx_id = conn.execute("SELECT id FROM partner_payout_fx WHERE request_id = ?", (kept,)).fetchone()["id"]
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        conn.execute(
            """
            INSERT INTO partner_usdt_carry (
                id, partner_id, seq, kind, amount_micro, balance_after_micro,
                fx_id, reverses_id, reason, entered_by, created_at_ms
            ) VALUES (?, ?, 1, 'leftover', 1000, 1000, ?, NULL, NULL, '42', 'x')
            """,
            (f"pc_{uuid.uuid4().hex[:16]}", bob, fx_id),
        )
    conn.close()


def test_kyc_gate_and_open_transaction_and_second_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = open_db(tmp_path / "gate.db")
    bob = _partner(conn, "Bob")
    cara = _partner(conn, "Cara", now_ms=NOW + 1)
    rid = _request(conn, bob)
    missing = _record(conn, rid, paid_kyc_id=None)
    assert missing["reason"] == "kyc_required"
    cara_kyc = conn.execute(
        "SELECT id FROM partner_kyc WHERE partner_id = ?",
        (cara,),
    ).fetchone()
    assert cara_kyc is not None
    foreign = _record(conn, rid, paid_kyc_id=str(cara_kyc["id"]))
    assert foreign["reason"] == "kyc_mismatch"
    with pytest.raises(sqlite3.IntegrityError, match="partner_payout_fx link"):
        _insert_raw(conn, rid, bob, paid_kyc_id=str(cara_kyc["id"]))
    conn.rollback()
    saved = _record(conn, rid, reason=f" \t{REASON}\n")
    assert saved["ok"] is True
    stored = fx.current_fx(conn, rid)
    assert stored is not None
    assert stored["reason"] == REASON
    wrong_kyc = _correct(conn, rid, paid_kyc_id=str(cara_kyc["id"]))
    assert wrong_kyc["reason"] == "kyc_mismatch"
    assert fx.carry_balance(conn, bob) == LEFTOVER
    again = _record(conn, rid)
    assert again["reason"] == "already_recorded"
    dusty = _record(conn, _request(conn, bob, now_ms=NOW + 2), trade=D("42.27"))  # type: ignore[arg-type]
    assert dusty["reason"] == "bad_input"
    extra = _request(conn, bob, now_ms=NOW + 3)
    conn.execute("BEGIN")
    busy = _record(conn, extra)
    assert busy["reason"] == "caller_transaction"
    assert conn.in_transaction
    assert conn.execute("SELECT COUNT(*) AS n FROM partner_payout_fx").fetchone()["n"] == 1
    correcting = _correct(conn, rid, bank_fee_satang=1_000, net_satang=_net(GROSS, TRADE, WD, 1_000))
    assert correcting["reason"] == "caller_transaction"
    settling = fx.settle_carry(conn, bob, 1, REASON, "42", NOW + 4)
    assert settling["reason"] == "caller_transaction"
    assert conn.execute("SELECT COUNT(*) AS n FROM partner_usdt_carry").fetchone()["n"] == 1
    conn.rollback()
    assert conn.in_transaction is False
    conn.close()

    monkeypatch.setenv("JARVISE_TEST_NO_KYC_SEED", "1")
    legacy = open_db(tmp_path / "legacy.db")
    old = _partner(legacy, "Old")
    old_req = _request(legacy, old)
    assert legacy.execute("SELECT COUNT(*) AS n FROM partner_kyc").fetchone()["n"] == 0
    accepted = _record(legacy, old_req, paid_kyc_id=None)
    assert accepted["ok"] is True
    row = fx.current_fx(legacy, old_req)
    assert row is not None
    assert row["paid_kyc_id"] is None
    legacy.close()


def _assert_boundary(path: Path, allowed: set[str]) -> None:
    src = path.read_text(encoding="utf-8")
    for banned in ("_decrypt", "_master_key", "reveal_current", "partner_portal", "partner_admin"):
        assert banned not in src
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] in allowed
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert module.split(".")[0] in allowed
            assert "listen" not in module
            assert "telegram" not in module
            assert "httpx" not in module


def test_partner_fx_does_not_import_telegram_or_kyc_secrets() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "jarvise_assist"
    _assert_boundary(
        root / "partner_fx.py",
        {
            "__future__",
            "json",
            "logging",
            "re",
            "secrets",
            "sqlite3",
            "uuid",
            "decimal",
            "typing",
            "jarvise_ingest",
        },
    )
    _assert_boundary(
        root / "partner_thb.py",
        {
            "__future__",
            "logging",
            "re",
            "datetime",
            "decimal",
            "typing",
            "jarvise_assist",
        },
    )


def test_absurd_rate_is_rate_out_of_bounds() -> None:
    common = dict(
        usdt_payout_micro=PAYOUT,
        carry_in_micro=0,
        chain_fee_micro=CHAIN,
        usdt_sold_micro=SOLD,
        trade_fee_satang=TRADE,
        thb_wd_fee_satang=WD,
        bank_fee_satang=BANK,
        net_satang=NET,
    )
    for raw in ("1E+30", "922337203685478", "61.12345"):
        out = fx.compute(rate=D(raw), **common)
        assert out["refusals"] == ["rate_out_of_bounds"]
    extra = fx.compute(rate=D("33.12345"), **common)
    assert extra["refusals"] == ["too_many_decimals"]
    nan = fx.compute(rate=D("NaN"), **common)
    assert nan["refusals"] == ["too_many_decimals"]


def test_int64_boundaries_exact() -> None:
    assert fx.parse_usdt("9223372036854.775807") == 2**63 - 1
    assert fx.parse_usdt("9223372036854.775808") is None
    assert fx.parse_thb("92233720368547758.07") == 2**63 - 1
    assert fx.parse_thb("92233720368547758.08") is None
    assert fx.parse_rate("922337203685477.5807") == 2**63 - 1
    assert fx.parse_rate("922337203685477.5808") is None
    assert fx._as_int(2**63 - 1) == 2**63 - 1
    assert fx._as_int(-(2**63)) == -(2**63)
    assert fx._as_int(2**63) is None
    assert fx._as_int(-(2**63) - 1) is None
    assert fx._as_int(True) is None
    assert fx._as_int(False) is None


def test_record_payout_fx_overflow_reaches_the_catch(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    conn = open_db(tmp_path / "ov.db")
    bob = _partner(conn, "Bob")
    first = _request(conn, bob)
    assert _record(conn, first)["ok"] is True
    second = _request(conn, bob, now_ms=NOW + 1)
    before_fx = conn.execute("SELECT COUNT(*) AS n FROM partner_payout_fx").fetchone()["n"]
    before_carry = conn.execute("SELECT COUNT(*) AS n FROM partner_usdt_carry").fetchone()["n"]
    caplog.set_level("INFO")
    huge = 2**63 - 1
    out = _record(
        conn,
        second,
        carry_in=LEFTOVER,
        trade=huge,
        net=huge,
        reason="x" * 10,
        now_ms=NOW + 2,
    )
    assert out["reason"] == "bad_input"
    assert conn.execute("SELECT COUNT(*) AS n FROM partner_payout_fx").fetchone()["n"] == before_fx
    assert conn.execute("SELECT COUNT(*) AS n FROM partner_usdt_carry").fetchone()["n"] == before_carry
    assert bob in caplog.text
    conn.close()


def test_record_correction_overflow_is_bad_input(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    conn = open_db(tmp_path / "ovc.db")
    bob = _partner(conn, "Bob")
    rid = _request(conn, bob)
    saved = _record(conn, rid)
    assert saved["ok"] is True
    base = str(saved["fx_id"])
    caplog.set_level("INFO")
    huge = 2**63 - 1
    out = fx.record_correction(
        conn,
        base_fx_id=base,
        carry_in_micro=0,
        chain_fee_micro=CHAIN,
        usdt_sold_micro=SOLD,
        rate=RATE,
        trade_fee_satang=huge,
        thb_wd_fee_satang=WD,
        bank_fee_satang=BANK,
        net_satang=huge,
        ref_status="off",
        reason="x" * 10,
        entered_by="42",
        now_ms=NOW + 5,
    )
    assert out["reason"] == "bad_input"
    assert fx.current_fx(conn, rid)["version"] == 1
    assert bob in caplog.text
    conn.close()


def test_settle_carry_overflow_is_bad_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = open_db(tmp_path / "ovs.db")
    bob = _partner(conn, "Bob")
    rid = _request(conn, bob)
    assert _record(conn, rid)["ok"] is True

    def _boom(*_a: Any, **_k: Any) -> int:
        raise OverflowError("too big")

    monkeypatch.setattr(fx, "_post_carry", _boom)
    out = fx.settle_carry(conn, bob, LEFTOVER, "จ่ายเศษตามสลิป", "42", NOW + 9)
    assert out["reason"] == "bad_input"
    assert fx.carry_balance(conn, bob) == LEFTOVER
    conn.close()


def test_correction_keeps_null_kyc_after_partner_adds_kyc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_TEST_NO_KYC_SEED", "1")
    conn = open_db(tmp_path / "null-kyc.db")
    bob = _partner(conn, "Bob")
    rid = _request(conn, bob)
    saved = _record(conn, rid, paid_kyc_id=None)
    assert saved["ok"] is True
    assert fx.current_fx(conn, rid)["paid_kyc_id"] is None
    added = pk.admin_set_kyc(
        conn,
        bob,
        "ข้อมูลรับเงิน\nชื่อจริง: สมชาย ใจดี\nเบอร์: 0812345678\nธนาคาร: KBANK\nเลขบัญชี: 0000001234\nชื่อบัญชี: สมชาย ใจดี\n",
        "เพิ่มหลังบันทึก",
        NOW + 3,
    )
    assert added["ok"] is True
    fixed = fx.record_correction(
        conn,
        base_fx_id=str(saved["fx_id"]),
        carry_in_micro=0,
        chain_fee_micro=CHAIN,
        usdt_sold_micro=SOLD,
        rate=RATE,
        trade_fee_satang=TRADE,
        thb_wd_fee_satang=WD,
        bank_fee_satang=1_000,
        net_satang=NET - 1_000,
        ref_status="off",
        reason="ธนาคารหักค่าโอนเพิ่ม",
        entered_by="42",
        now_ms=NOW + 4,
    )
    assert fixed["ok"] is True
    assert fx.current_fx(conn, rid)["paid_kyc_id"] is None
    conn.close()


def test_correction_null_base_rejects_foreign_kyc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_TEST_NO_KYC_SEED", "1")
    conn = open_db(tmp_path / "foreign.db")
    bob = _partner(conn, "Bob")
    other = _partner(conn, "Other", now_ms=NOW + 1)
    rid = _request(conn, bob)
    saved = _record(conn, rid, paid_kyc_id=None)
    assert saved["ok"] is True
    added = pk.admin_set_kyc(
        conn,
        other,
        "ข้อมูลรับเงิน\nชื่อจริง: สมชาย ใจดี\nเบอร์: 0812345678\nธนาคาร: KBANK\nเลขบัญชี: 0000001234\nชื่อบัญชี: สมชาย ใจดี\n",
        "",
        NOW + 2,
    )
    assert added["ok"] is True
    foreign = str(added["kyc_id"])
    out = fx.record_correction(
        conn,
        base_fx_id=str(saved["fx_id"]),
        carry_in_micro=0,
        chain_fee_micro=CHAIN,
        usdt_sold_micro=SOLD,
        rate=RATE,
        trade_fee_satang=TRADE,
        thb_wd_fee_satang=WD,
        bank_fee_satang=BANK,
        net_satang=NET,
        ref_status="off",
        reason="ลองใส่บัญชีคนอื่น",
        paid_kyc_id=foreign,
        entered_by="42",
        now_ms=NOW + 3,
    )
    assert out["reason"] == "kyc_mismatch"
    conn.rollback()
    conn.close()


def test_correction_null_base_accepts_own_kyc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_TEST_NO_KYC_SEED", "1")
    conn = open_db(tmp_path / "own.db")
    bob = _partner(conn, "Bob")
    rid = _request(conn, bob)
    saved = _record(conn, rid, paid_kyc_id=None)
    added = pk.admin_set_kyc(
        conn,
        bob,
        "ข้อมูลรับเงิน\nชื่อจริง: สมชาย ใจดี\nเบอร์: 0812345678\nธนาคาร: KBANK\nเลขบัญชี: 0000001234\nชื่อบัญชี: สมชาย ใจดี\n",
        "",
        NOW + 2,
    )
    assert added["ok"] is True
    out = fx.record_correction(
        conn,
        base_fx_id=str(saved["fx_id"]),
        carry_in_micro=0,
        chain_fee_micro=CHAIN,
        usdt_sold_micro=SOLD,
        rate=RATE,
        trade_fee_satang=TRADE,
        thb_wd_fee_satang=WD,
        bank_fee_satang=BANK,
        net_satang=NET,
        ref_status="off",
        reason="ผูกบัญชีของตัวเอง",
        paid_kyc_id=str(added["kyc_id"]),
        entered_by="42",
        now_ms=NOW + 3,
    )
    assert out["ok"] is True
    assert fx.current_fx(conn, rid)["paid_kyc_id"] == str(added["kyc_id"])
    conn.close()


def test_correction_kyc_base_keeps_base_id(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "keep.db")
    bob = _partner(conn, "Bob")
    rid = _request(conn, bob)
    saved = _record(conn, rid)
    original = fx.current_fx(conn, rid)["paid_kyc_id"]
    assert original
    out = fx.record_correction(
        conn,
        base_fx_id=str(saved["fx_id"]),
        carry_in_micro=0,
        chain_fee_micro=CHAIN,
        usdt_sold_micro=SOLD,
        rate=RATE,
        trade_fee_satang=TRADE,
        thb_wd_fee_satang=WD,
        bank_fee_satang=1_000,
        net_satang=NET - 1_000,
        ref_status="off",
        reason="คงบัญชีเดิมไว้",
        entered_by="42",
        now_ms=NOW + 4,
    )
    assert out["ok"] is True
    assert fx.current_fx(conn, rid)["paid_kyc_id"] == original
    conn.close()


def _kyc_of(conn: sqlite3.Connection, partner_id: str) -> str:
    row = conn.execute(
        "SELECT id FROM partner_kyc WHERE partner_id = ? ORDER BY version DESC LIMIT 1",
        (partner_id,),
    ).fetchone()
    assert row is not None
    return str(row["id"])


def _pay(conn: sqlite3.Connection, rid: str, partner_id: str, **extra: Any) -> dict[str, Any]:
    now_ms = int(extra.pop("now_ms", NOW + 1))
    carry = int(extra.pop("carry_in_micro", 0))
    return fx.record_payout_fx(
        conn,
        request_id=rid,
        carry_in_micro=carry,
        chain_fee_micro=CHAIN,
        usdt_sold_micro=SOLD,
        rate=RATE,
        trade_fee_satang=TRADE,
        thb_wd_fee_satang=WD,
        bank_fee_satang=BANK,
        net_satang=NET,
        ref_status="off",
        paid_kyc_id=_kyc_of(conn, partner_id),
        entered_by="42",
        now_ms=now_ms,
        **extra,
    )


def test_draft_token_consumed_in_commit(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "draft.db")
    bob = _partner(conn, "Bob")
    rid = _request(conn, bob)
    staged = fx.stage_fx_draft(
        conn, request_id=rid, mode="paid", payload={"sold": "1"}, created_by="42", now_ms=NOW
    )
    ok = _pay(conn, rid, bob, draft_token=str(staged["token"]))
    assert ok["ok"] is True
    assert fx.fx_draft_row(conn, rid) is None

    again = _request(conn, bob, now_ms=NOW + 2)
    fx.stage_fx_draft(
        conn, request_id=again, mode="paid", payload={"a": "1"}, created_by="42", now_ms=NOW + 2
    )
    wrong = _pay(conn, again, bob, draft_token="00000000", now_ms=NOW + 3)
    assert wrong["reason"] == "draft_gone"
    assert fx.current_fx(conn, again) is None
    assert fx.fx_draft_row(conn, again) is not None

    fx.stage_fx_draft(conn, request_id=again, mode="paid", payload={"a": "1"}, created_by="42", now_ms=NOW)
    expired = _pay(
        conn, again, bob, draft_token=fx.fx_draft_row(conn, again)["token"], now_ms=NOW + fx.DRAFT_TTL_MS + 1
    )
    assert expired["reason"] == "draft_gone"
    assert fx.current_fx(conn, again) is None

    fx.stage_fx_draft(
        conn, request_id=again, mode="paid", payload={"a": "1"}, created_by="42", now_ms=NOW + 10
    )
    mismatch = _pay(
        conn, again, bob, carry_in_micro=1, draft_token=fx.fx_draft_row(conn, again)["token"], now_ms=NOW + 11
    )
    assert mismatch["reason"] == "carry_mismatch"
    assert fx.fx_draft_row(conn, again) is None
    assert fx.current_fx(conn, again) is None

    fx.stage_fx_draft(
        conn, request_id=again, mode="paid", payload={"a": "1"}, created_by="42", now_ms=NOW + 12
    )
    token = str(fx.fx_draft_row(conn, again)["token"])

    def _explode(*_a: Any, **_k: Any) -> None:
        raise sqlite3.IntegrityError("forced")

    original = fx._insert_fx
    fx._insert_fx = _explode  # type: ignore[method-assign]
    try:
        forced = _pay(conn, again, bob, carry_in_micro=LEFTOVER, draft_token=token, now_ms=NOW + 13)
    finally:
        fx._insert_fx = original  # type: ignore[method-assign]
    assert forced["reason"] == "db_constraint"
    kept = fx.fx_draft_row(conn, again)
    assert kept is not None and kept["token"] == token
    conn.close()


def test_delete_fx_draft_stale_token_keeps_new_draft(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "del.db")
    bob = _partner(conn, "Bob")
    rid = _request(conn, bob)
    first = fx.stage_fx_draft(
        conn, request_id=rid, mode="paid", payload={"n": "1"}, created_by="42", now_ms=NOW
    )
    second = fx.stage_fx_draft(
        conn, request_id=rid, mode="paid", payload={"n": "2"}, created_by="42", now_ms=NOW + 1
    )
    assert fx.delete_fx_draft(conn, rid, token=str(first["token"])) is False
    kept = fx.fx_draft_row(conn, rid)
    assert kept is not None and kept["token"] == second["token"]
    assert fx.delete_fx_draft(conn, rid, token=str(second["token"])) is True
    assert fx.fx_draft_row(conn, rid) is None
    conn.close()


def test_set_fx_draft_message_id_matches_token(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "mid.db")
    bob = _partner(conn, "Bob")
    rid = _request(conn, bob)
    staged = fx.stage_fx_draft(
        conn, request_id=rid, mode="paid", payload={"n": "1"}, created_by="42", now_ms=NOW
    )
    assert fx.set_fx_draft_message_id(conn, rid, "00000000", 9) is False
    assert fx.fx_draft_row(conn, rid)["admin_message_id"] is None
    assert fx.set_fx_draft_message_id(conn, rid, str(staged["token"]), 9) is True
    assert int(fx.fx_draft_row(conn, rid)["admin_message_id"]) == 9
    conn.close()


def test_fx_draft_row_reports_expired_and_replaced(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "row.db")
    bob = _partner(conn, "Bob")
    rid = _request(conn, bob)
    staged = fx.stage_fx_draft(
        conn, request_id=rid, mode="paid", payload={"n": "1"}, created_by="42", now_ms=NOW
    )
    assert fx.load_fx_draft(conn, rid, str(staged["token"]), NOW + fx.DRAFT_TTL_MS) is None
    raw = fx.fx_draft_row(conn, rid)
    assert raw is not None and raw["token"] == staged["token"]
    replaced = fx.stage_fx_draft(
        conn, request_id=rid, mode="paid", payload={"n": "2"}, created_by="42", now_ms=NOW + 1
    )
    assert fx.fx_draft_row(conn, rid)["token"] == replaced["token"]
    conn.close()


def test_awaiting_thb_cutoff_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = open_db(tmp_path / "cut.db")
    bob = _partner(conn, "Bob")
    old = _request(conn, bob, now_ms=100)
    new = _request(conn, bob, now_ms=200)
    monkeypatch.setattr(fx, "THB_FLOW_SINCE_MS", 150)
    rows = fx.awaiting_thb(conn, bob)
    assert [str(row["id"]) for row in rows] == [new]
    assert old not in [str(row["id"]) for row in rows]
    assert "THB_FLOW_SINCE_MS = 0" in (
        Path(__file__).resolve().parents[1] / "src" / "jarvise_assist" / "partner_fx.py"
    ).read_text(encoding="utf-8")
    conn.close()


def test_open_settlements_does_not_change_pool_value(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "pool.db")
    admin = _partner(conn, "admin", admin=True)
    bob = _partner(conn, "Bob")
    assert ps.apply_deposit(conn, admin, D("1000"), D("1000"), NOW)["ok"]
    assert ps.apply_deposit(conn, bob, D("100"), D("1100"), NOW + 1)["ok"]
    assert ps.apply_withdraw_profit(conn, bob, D("1200"), NOW + 2)["ok"]
    due = ps.payouts_due_total(conn)
    pool_before = nav(D("1200") - due, ps.total_units(conn))
    listed = fx.open_settlements(conn)
    assert any(row["stage"] == "due" for row in listed)
    assert ps.payouts_due_total(conn) == due
    assert nav(D("1200") - ps.payouts_due_total(conn), ps.total_units(conn)) == pool_before
    rid = next(str(row["id"]) for row in ps.list_due_payouts(conn))
    assert ps.mark_paid(conn, rid, NOW + 3)["ok"]
    waiting = fx.awaiting_thb(conn)
    assert len(waiting) == 1
    assert ps.payouts_due_total(conn) == D("0")
    conn.close()
