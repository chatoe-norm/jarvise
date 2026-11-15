"""Partner invite / bind / unbind store rules (P3a)."""

from __future__ import annotations

import hashlib
from decimal import Decimal
from pathlib import Path
from typing import Any

from jarvise_assist import partner_store as ps
from jarvise_ingest.db import open_db

D = Decimal
NOW = 1_700_000_000_000


def _setup(conn: Any) -> tuple[str, str]:
    a = ps.create_partner(conn, name="admin", is_admin=True, now_ms=NOW)
    b = ps.create_partner(conn, name="B", is_admin=False, now_ms=NOW + 1)
    ps.apply_deposit(conn, a["partner_id"], D("1000"), D("1000"), NOW + 2)
    return a["partner_id"], b["partner_id"]


def test_create_invite_stores_hash_not_code(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "inv.db")
    _, b_id = _setup(conn)
    out = ps.create_invite(conn, b_id, now_ms=NOW + 10)
    assert out["ok"] is True
    code = out["code"]
    assert len(code) >= 20
    row = ps.get_partner(conn, b_id)
    assert row["invite_hash"] == hashlib.sha256(code.encode()).hexdigest()
    assert row["invite_expires_at_ms"] == NOW + 10 + ps.INVITE_TTL_MS
    cols = conn.execute("SELECT * FROM partner WHERE id = ?", (b_id,)).fetchone()
    for k in cols.keys():
        assert code not in str(cols[k])
    assert ps.verify_ledger(conn)["ok"] is True
    conn.close()


def test_create_invite_refusals(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "inv_ref.db")
    a_id, b_id = _setup(conn)
    assert ps.create_invite(conn, "missing", now_ms=NOW)["reason"] == "not_found"
    assert ps.create_invite(conn, a_id, now_ms=NOW)["reason"] == "admin_no_invite"
    ps.apply_close(conn, b_id, D("1000"), NOW + 20)
    assert ps.create_invite(conn, b_id, now_ms=NOW + 21)["reason"] == "closed"
    c = ps.create_partner(conn, name="C", is_admin=False, now_ms=NOW + 30)
    inv = ps.create_invite(conn, c["partner_id"], now_ms=NOW + 31)
    assert inv["ok"]
    bind = ps.bind_invite(conn, inv["code"], "501", admin_chat_id="42", now_ms=NOW + 32)
    assert bind["ok"]
    assert ps.create_invite(conn, c["partner_id"], now_ms=NOW + 33)["reason"] == "already_bound"
    assert ps.verify_ledger(conn)["ok"] is True
    conn.close()


def test_bind_ok_and_second_code_fails(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "bind.db")
    _, b_id = _setup(conn)
    inv = ps.create_invite(conn, b_id, now_ms=NOW + 10)
    code = inv["code"]
    ok = ps.bind_invite(conn, code, "501", admin_chat_id="42", now_ms=NOW + 11)
    assert ok == {"ok": True, "partner_id": b_id, "name": "B"}
    row = ps.get_partner(conn, b_id)
    assert row["telegram_user_id"] == "501"
    assert row["invite_hash"] is None
    assert row["invite_expires_at_ms"] is None
    again = ps.bind_invite(conn, code, "502", admin_chat_id="42", now_ms=NOW + 12)
    assert again["ok"] is False
    assert again["reason"] == "invite_invalid"
    assert ps.partner_by_telegram_id(conn, "501")["id"] == b_id
    assert ps.verify_ledger(conn)["ok"] is True
    conn.close()


def test_bind_at_exact_expiry_fails(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "bind_exp.db")
    _, b_id = _setup(conn)
    inv = ps.create_invite(conn, b_id, now_ms=NOW + 10)
    expires = inv["expires_at_ms"]
    out = ps.bind_invite(conn, inv["code"], "501", admin_chat_id="42", now_ms=expires)
    assert out["reason"] == "invite_invalid"
    assert ps.get_partner(conn, b_id)["telegram_user_id"] is None
    conn.close()


def test_bind_admin_chat_id_and_closed_and_admin_row(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "bind_bad.db")
    a_id, b_id = _setup(conn)
    inv = ps.create_invite(conn, b_id, now_ms=NOW + 10)
    assert (
        ps.bind_invite(conn, inv["code"], "42", admin_chat_id="42", now_ms=NOW + 11)["reason"] == "admin_id"
    )
    c = ps.create_partner(conn, name="C", is_admin=False, now_ms=NOW + 20)
    inv_c = ps.create_invite(conn, c["partner_id"], now_ms=NOW + 21)
    conn.execute(
        "UPDATE partner SET status='closed', updated_at_ms=? WHERE id=?",
        (NOW + 22, c["partner_id"]),
    )
    conn.commit()
    assert (
        ps.bind_invite(conn, inv_c["code"], "503", admin_chat_id="42", now_ms=NOW + 23)["reason"]
        == "invite_invalid"
    )
    inv2 = ps.create_invite(conn, b_id, now_ms=NOW + 30)
    h = hashlib.sha256(inv2["code"].encode()).hexdigest()
    conn.execute(
        "UPDATE partner SET invite_hash=?, invite_expires_at_ms=? WHERE id=?",
        (h, NOW + 30 + ps.INVITE_TTL_MS, a_id),
    )
    conn.execute(
        "UPDATE partner SET invite_hash=NULL, invite_expires_at_ms=NULL WHERE id=?",
        (b_id,),
    )
    conn.commit()
    assert (
        ps.bind_invite(conn, inv2["code"], "504", admin_chat_id="42", now_ms=NOW + 31)["reason"]
        == "invite_invalid"
    )
    conn.close()


def test_two_partners_cannot_share_telegram_id(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "tg_dup.db")
    _, b_id = _setup(conn)
    c = ps.create_partner(conn, name="C", is_admin=False, now_ms=NOW + 5)
    inv_b = ps.create_invite(conn, b_id, now_ms=NOW + 10)
    assert ps.bind_invite(conn, inv_b["code"], "501", admin_chat_id="42", now_ms=NOW + 11)["ok"]
    inv_c = ps.create_invite(conn, c["partner_id"], now_ms=NOW + 12)
    out = ps.bind_invite(conn, inv_c["code"], "501", admin_chat_id="42", now_ms=NOW + 13)
    assert out["reason"] == "tg_in_use"
    assert ps.get_partner(conn, c["partner_id"])["telegram_user_id"] is None
    assert ps.verify_ledger(conn)["ok"] is True
    conn.close()


def test_unbind_clears_fields(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "unbind.db")
    _, b_id = _setup(conn)
    inv = ps.create_invite(conn, b_id, now_ms=NOW + 10)
    ps.bind_invite(conn, inv["code"], "501", admin_chat_id="42", now_ms=NOW + 11)
    out = ps.unbind_partner(conn, b_id, now_ms=NOW + 20)
    assert out["ok"] is True
    row = ps.get_partner(conn, b_id)
    assert row["telegram_user_id"] is None
    assert row["invite_hash"] is None
    assert row["invite_expires_at_ms"] is None
    assert ps.partner_by_telegram_id(conn, "501") is None
    assert ps.verify_ledger(conn)["ok"] is True
    conn.close()


def test_get_request_and_list_helpers(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lists.db")
    _, b_id = _setup(conn)
    req = ps.create_request(conn, b_id, "deposit", NOW + 10, amount=D("100"))
    assert req["ok"]
    rid = req["request_id"]
    got = ps.get_request(conn, rid)
    assert got is not None and got["id"] == rid
    pending = ps.list_pending_requests(conn, now_ms=NOW + 11)
    assert len(pending) == 1
    assert pending[0]["id"] == rid
    empty = ps.list_pending_requests(conn, now_ms=NOW + 10 + ps.REQUEST_TTL_MS + 1)
    assert empty == []
    assert ps.list_due_payouts(conn) == []
    ps.confirm_request(conn, rid, D("1100"), NOW + 12, amount_received=D("100"))
    assert ps.list_due_payouts(conn) == []
    assert ps.verify_ledger(conn)["ok"] is True
    conn.close()


def test_new_invite_replaces_old(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "repl.db")
    _, b_id = _setup(conn)
    inv1 = ps.create_invite(conn, b_id, now_ms=NOW + 10)
    inv2 = ps.create_invite(conn, b_id, now_ms=NOW + 11)
    assert inv1["code"] != inv2["code"]
    assert (
        ps.bind_invite(conn, inv1["code"], "501", admin_chat_id="42", now_ms=NOW + 12)["reason"]
        == "invite_invalid"
    )
    assert ps.bind_invite(conn, inv2["code"], "501", admin_chat_id="42", now_ms=NOW + 13)["ok"]
    conn.close()
