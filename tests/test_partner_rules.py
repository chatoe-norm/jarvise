"""Partner portal rules (PR-A): join date, 30-day close, mins, monthly convert, locks."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from jarvise_assist import partner_admin as pa
from jarvise_assist import partner_portal as portal
from jarvise_assist import partner_rules
from jarvise_assist import partner_store as ps
from jarvise_assist.partner_value import AccountValue, AccountValueError
from jarvise_assist.pause import BANGKOK
from jarvise_ingest.db import open_db

D = Decimal
ADMIN = "42"
PARTNER_TG = 501


def _ms(y: int, m: int, d: int, hh: int = 12, mm: int = 0) -> int:
    return int(datetime(y, m, d, hh, mm, tzinfo=BANGKOK).timestamp() * 1000)


NOW = _ms(2026, 11, 17, 12, 0)


@pytest.fixture(autouse=True)
def _clean_transfer_guard(monkeypatch: Any) -> None:
    """P5 guard is covered in test_partner_guard.py; keep legacy confirms clean."""
    from jarvise_assist.partner_audit import GuardResult

    def _fake(conn: Any, client: Any, *, now_ms: int, req: Any) -> tuple[GuardResult, str]:
        return (
            GuardResult(status="ok", checked_at_ms=int(now_ms), findings=[], warn=False, error=None),
            "",
        )

    monkeypatch.setattr(pa, "_transfer_guard", _fake)


@pytest.fixture
def env_chat(monkeypatch: Any) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", ADMIN)


class FakeValueClient:
    def __init__(
        self,
        *,
        wallet: str | None = None,
        unrealized: str = "0",
        error: AccountValueError | None = None,
    ) -> None:
        self.wallet = wallet
        self.unrealized = unrealized
        self.error = error
        self.calls = 0

    def balance_rows(self) -> list[dict[str, Any]]:
        self.calls += 1
        if self.error is not None:
            raise RuntimeError("no")
        assert self.wallet is not None
        return [{"asset": "USDT", "balance": self.wallet}]

    def position_rows(self) -> list[dict[str, Any]]:
        if self.error is not None:
            raise RuntimeError("no")
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


def _patch_read(monkeypatch: Any, client: FakeValueClient) -> None:
    def fake(cli: Any, *, now_ms: int, attempts: int = 3) -> AccountValue | AccountValueError:
        if isinstance(cli, FakeValueClient):
            cli.calls += 1
            if cli.error is not None:
                return cli.error
            if cli.wallet is not None:
                w = D(cli.wallet)
                u = D(cli.unrealized)
                return AccountValue(
                    wallet=w,
                    unrealized=u,
                    value=w + u,
                    open_positions=0 if u == 0 else 1,
                    read_at_ms=now_ms,
                )
        return AccountValueError(reason="no_client")

    monkeypatch.setattr(portal, "read_account_value", fake)
    monkeypatch.setattr(pa, "read_account_value", fake)


def _send_capture() -> tuple[list[dict[str, Any]], Any]:
    sent: list[dict[str, Any]] = []

    def send(
        text: str, *, reply_markup: dict[str, Any] | None = None, chat_id: str | None = None, **kw: Any
    ) -> int:
        mid = len(sent) + 1
        entry: dict[str, Any] = {"text": text, "reply_markup": reply_markup, "message_id": mid}
        if chat_id is not None:
            entry["chat_id"] = chat_id
        sent.append(entry)
        return mid

    return sent, send


def _edit_capture() -> tuple[list[dict[str, Any]], Any]:
    edited: list[dict[str, Any]] = []

    def edit(
        message_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        chat_id: str | None = None,
        **kw: Any,
    ) -> bool:
        entry: dict[str, Any] = {"message_id": message_id, "text": text, "reply_markup": reply_markup}
        if chat_id is not None:
            entry["chat_id"] = chat_id
        edited.append(entry)
        return True

    return edited, edit


def _answer_capture() -> tuple[list[Any], Any]:
    answers: list[Any] = []

    def answer(cq_id: str, text: str | None = None) -> bool:
        answers.append((cq_id, text))
        return True

    return answers, answer


def _owner_msg(text: str) -> dict[str, Any]:
    return {
        "message_id": 1,
        "chat": {"id": int(ADMIN), "type": "private"},
        "from": {"id": int(ADMIN)},
        "text": text,
    }


def _owner_cb(data: str, *, mid: int = 7) -> dict[str, Any]:
    return {
        "id": "cq1",
        "data": data,
        "from": {"id": int(ADMIN)},
        "message": {
            "message_id": mid,
            "chat": {"id": int(ADMIN), "type": "private"},
            "text": "preview",
        },
    }


def _pcb(data: str, *, chat: int = PARTNER_TG, mid: int = 7) -> dict[str, Any]:
    return {
        "id": "cq1",
        "data": data,
        "from": {"id": chat},
        "message": {
            "message_id": mid,
            "chat": {"id": chat, "type": "private"},
            "text": "card",
        },
    }


def _counts(conn: Any) -> tuple[int, int]:
    req_n = int(conn.execute("SELECT COUNT(*) AS n FROM partner_request").fetchone()["n"])
    ev_n = int(conn.execute("SELECT COUNT(*) AS n FROM partner_event").fetchone()["n"])
    return req_n, ev_n


def _assert_privacy_partner(texts: list[str]) -> None:
    for text in texts:
        for bad in ("ตรวจยอด", "audit", "guard"):
            assert bad not in text
        assert len(text) <= 200


def _init_pool(conn: Any, monkeypatch: Any, *, wallet: str = "1000", now_ms: int = NOW) -> None:
    portal.reset_value_cache()
    client = FakeValueClient(wallet=wallet)
    _patch_read(monkeypatch, client)
    pa.handle_partner_callback(
        conn,
        "pa:iy",
        callback=_owner_cb(data="pa:iy"),
        send=_send_capture()[1],
        answer=_answer_capture()[1],
        edit=_edit_capture()[1],
        futures=client,
        now_ms=now_ms,
    )
    pa.handle_partner_text(
        conn,
        "p add B",
        message=_owner_msg("p add B"),
        send=_send_capture()[1],
        futures=client,
        now_ms=now_ms + 1,
    )


def _bind(conn: Any, *, tg: str = str(PARTNER_TG), now_ms: int = NOW + 2) -> str:
    sent, send = _send_capture()
    pa.handle_partner_text(
        conn,
        "p invite B",
        message=_owner_msg("p invite B"),
        send=send,
        futures=FakeValueClient(wallet="1000"),
        now_ms=now_ms,
    )
    code = ""
    for line in sent[0]["text"].splitlines():
        if line.startswith("/start "):
            code = line.split(None, 1)[1].strip()
    assert ps.bind_invite(conn, code, tg, admin_chat_id=ADMIN, now_ms=now_ms + 1)["ok"]
    return str(conn.execute("SELECT id FROM partner WHERE name='B'").fetchone()["id"])


def _deposit(conn: Any, pid: str, amount: Decimal, v: Decimal, now_ms: int) -> None:
    assert ps.apply_deposit(conn, pid, amount, v, now_ms)["ok"]


def _tap(
    conn: Any,
    data: str,
    *,
    monkeypatch: Any,
    wallet: str | None = "1500",
    error: AccountValueError | None = None,
    now_ms: int,
    chat: int = PARTNER_TG,
) -> tuple[list[Any], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    portal.reset_value_cache()
    cli = FakeValueClient(wallet=wallet, error=error)
    _patch_read(monkeypatch, cli)
    answers, answer = _answer_capture()
    edited, edit = _edit_capture()
    sent, send = _send_capture()
    out = portal.handle_portal_callback(
        conn,
        data,
        callback=_pcb(data, chat=chat),
        send=send,
        answer=answer,
        edit=edit,
        futures=cli,
        now_ms=now_ms,
    )
    return answers, sent, edited, out


def test_join_date_on_card_and_overview(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "join.db")
    join_ms = _ms(2026, 10, 1, 23, 30)
    _init_pool(conn, monkeypatch, now_ms=join_ms - 1000)
    pid = _bind(conn, now_ms=join_ms - 500)
    # no deposit yet
    portal.reset_value_cache()
    cli = FakeValueClient(wallet="1000")
    _patch_read(monkeypatch, cli)
    text, _ = portal._build_card(
        conn,
        ps.get_partner(conn, pid),  # type: ignore[arg-type]
        AccountValue(wallet=D("1000"), unrealized=D("0"), value=D("1000"), open_positions=0, read_at_ms=NOW),
        now_ms=NOW,
    )
    assert "เข้าร่วม" not in text
    overview = pa._overview_text(
        conn,
        AccountValue(wallet=D("1000"), unrealized=D("0"), value=D("1000"), open_positions=0, read_at_ms=NOW),
        now_ms=NOW,
    )
    # Pool init deposits to admin, so the overview may show เข้าร่วม on the admin line.
    # The unbound partner B must not have a join date until their first deposit.
    b_lines = [ln for ln in overview.splitlines() if ln.startswith("· B")]
    assert b_lines, overview
    assert "เข้าร่วม" not in b_lines[0]

    _deposit(conn, pid, D("100"), D("1100"), join_ms)
    text2, _ = portal._build_card(
        conn,
        ps.get_partner(conn, pid),  # type: ignore[arg-type]
        AccountValue(wallet=D("1100"), unrealized=D("0"), value=D("1100"), open_positions=0, read_at_ms=NOW),
        now_ms=NOW,
    )
    assert "เข้าร่วม 01/10/2026" in text2
    overview2 = pa._overview_text(
        conn,
        AccountValue(wallet=D("1100"), unrealized=D("0"), value=D("1100"), open_positions=0, read_at_ms=NOW),
        now_ms=NOW,
    )
    assert "· B · TG ✅ · เข้าร่วม 01/10/2026" in overview2
    conn.close()


def test_close_day_29_locked_day_30_open(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "close30.db")
    join_ms = _ms(2026, 10, 1, 23, 30)
    _init_pool(conn, monkeypatch, now_ms=join_ms - 1000)
    pid = _bind(conn, now_ms=join_ms - 500)
    _deposit(conn, pid, D("100"), D("1100"), join_ms)
    day29 = _ms(2026, 10, 30, 12, 0)
    day30 = _ms(2026, 10, 31, 0, 0)
    row = ps.get_partner(conn, pid)
    assert row is not None
    b29 = partner_rules.block(conn, row, "close", nav=D("1"), now_ms=day29)
    assert b29 is not None
    assert b29.code == "close_too_soon"
    assert "ตั้งแต่ 31/10/2026" in b29.toast
    assert b29.label == "🔒 ปิดพอร์ต · อีก 1 วัน"
    b30 = partner_rules.block(conn, row, "close", nav=D("1"), now_ms=day30)
    assert b30 is None
    answers, sent, edited, out = _tap(conn, "pp:rq:cl", monkeypatch=monkeypatch, wallet="1100", now_ms=day29)
    assert out["ok"] is False
    assert out["reason"] == "close_too_soon"
    assert len(answers) == 1 and answers[0][1] == b29.toast
    assert sent == [] and edited == []
    answers2, sent2, edited2, out2 = _tap(
        conn, "pp:rq:cl", monkeypatch=monkeypatch, wallet="1100", now_ms=day30
    )
    assert out2["ok"] is True
    assert any("ปิดพอร์ต" in (e.get("text") or "") for e in edited2) or any(
        "ปิดพอร์ต" in (s.get("text") or "") for s in sent2
    )
    assert answers2[0][1] is None
    conn.close()


def test_close_no_deposit_locked(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "nodep.db")
    _init_pool(conn, monkeypatch)
    pid = _bind(conn)
    row = ps.get_partner(conn, pid)
    assert row is not None
    b = partner_rules.block(conn, row, "close", nav=None, now_ms=NOW)
    assert b is not None
    assert b.code == "no_deposit"
    assert b.toast == "ยังไม่มีเงินในพอร์ต"
    answers, sent, edited, out = _tap(conn, "pp:go:cl", monkeypatch=monkeypatch, wallet="1000", now_ms=NOW)
    assert out == {"ok": False, "reason": "no_deposit"}
    assert answers == [("cq1", "ยังไม่มีเงินในพอร์ต")]
    assert sent == [] and edited == []
    assert _counts(conn)[0] == 0
    conn.close()


def test_convert_profit_min(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "cvmin.db")
    join_ms = _ms(2026, 9, 1)
    _init_pool(conn, monkeypatch, now_ms=join_ms - 10)
    pid = _bind(conn, now_ms=join_ms - 5)
    _deposit(conn, pid, D("100"), D("1100"), join_ms)
    # 1100 total units; partner 100 u / cost 100 → profit 19.99 at V=1319.89, 20 at V=1320
    row = ps.get_partner(conn, pid)
    assert row is not None
    from jarvise_assist import partner_ledger as pl

    due = ps.payouts_due_total(conn)
    u_total = ps.total_units(conn)
    n_locked = pl.nav(D("1319.89") - due, u_total)
    assert not isinstance(n_locked, pl.Refusal)
    b = partner_rules.block(conn, row, "convert", nav=n_locked, now_ms=NOW)
    assert b is not None and b.code == "convert_min"
    assert b.toast == "ทบกำไรได้เมื่อกำไรก่อนหักค่าธรรมเนียมถึง 20 USDT"
    n_ok = pl.nav(D("1320") - due, u_total)
    assert not isinstance(n_ok, pl.Refusal)
    assert partner_rules.block(conn, row, "convert", nav=n_ok, now_ms=NOW) is None
    answers, sent, edited, out = _tap(conn, "pp:rq:cv", monkeypatch=monkeypatch, wallet="1319.89", now_ms=NOW)
    assert out["reason"] == "convert_min"
    assert answers[0][1] == b.toast
    assert sent == [] and edited == []
    answers2, sent2, edited2, out2 = _tap(
        conn, "pp:rq:cv", monkeypatch=monkeypatch, wallet="1320", now_ms=NOW
    )
    assert out2["ok"] is True
    assert answers2[0][1] is None
    assert edited2 or sent2
    conn.close()


def _pending_rid(conn: Any, pid: str) -> str:
    return str(
        conn.execute(
            "SELECT id FROM partner_request WHERE partner_id=? AND status='pending'",
            (pid,),
        ).fetchone()["id"]
    )


def _admin_confirm(conn: Any, rid: str, *, monkeypatch: Any, wallet: str, now_ms: int) -> None:
    cli = FakeValueClient(wallet=wallet)
    _patch_read(monkeypatch, cli)
    portal.reset_value_cache()
    pa.handle_partner_callback(
        conn,
        f"pa:ry:{rid}",
        callback=_owner_cb(data=f"pa:ry:{rid}"),
        send=_send_capture()[1],
        answer=_answer_capture()[1],
        edit=_edit_capture()[1],
        futures=cli,
        now_ms=now_ms,
    )


def test_admin_p_cv_does_not_use_portal_convert_month(
    tmp_path: Path, monkeypatch: Any, env_chat: None
) -> None:
    conn = open_db(tmp_path / "cvadmin.db")
    join_ms = _ms(2026, 9, 1)
    _init_pool(conn, monkeypatch, now_ms=join_ms - 10)
    pid = _bind(conn, now_ms=join_ms - 5)
    _deposit(conn, pid, D("200"), D("1200"), join_ms)
    t = _ms(2026, 11, 10)
    cli = FakeValueClient(wallet="1400")
    _patch_read(monkeypatch, cli)
    portal.reset_value_cache()
    pa.handle_partner_text(
        conn,
        "p cv B",
        message=_owner_msg("p cv B"),
        send=_send_capture()[1],
        futures=cli,
        now_ms=t,
    )
    rid = _pending_rid(conn, pid)
    assert (
        conn.execute("SELECT source FROM partner_request WHERE id=?", (rid,)).fetchone()["source"] == "admin"
    )
    _admin_confirm(conn, rid, monkeypatch=monkeypatch, wallet="1400", now_ms=t + 100)
    ev = conn.execute(
        "SELECT COUNT(*) AS n FROM partner_event WHERE partner_id=? AND kind='convert' AND request_id=?",
        (pid, rid),
    ).fetchone()
    assert ev["n"] == 1
    assert partner_rules.convert_used_this_month(conn, pid, t + 200) is False
    row = ps.get_partner(conn, pid)
    assert row is not None
    b = partner_rules.block(conn, row, "convert", nav=D("2"), now_ms=t + 200)
    assert b is None or b.code != "convert_month"
    # Partner can still submit a portal convert this month (profit back above 20 at V=1600).
    _, _, _, sub = _tap(conn, "pp:go:cv", monkeypatch=monkeypatch, wallet="1600", now_ms=t + 300)
    assert sub["ok"] is True, sub
    conn.close()


def test_legacy_null_source_convert_does_not_lock(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "cvlegacy.db")
    join_ms = _ms(2026, 9, 1)
    _init_pool(conn, monkeypatch, now_ms=join_ms - 10)
    pid = _bind(conn, now_ms=join_ms - 5)
    _deposit(conn, pid, D("200"), D("1200"), join_ms)
    t = _ms(2026, 11, 10)
    _, _, _, sub = _tap(conn, "pp:go:cv", monkeypatch=monkeypatch, wallet="1400", now_ms=t)
    assert sub["ok"] is True
    rid = _pending_rid(conn, pid)
    _admin_confirm(conn, rid, monkeypatch=monkeypatch, wallet="1400", now_ms=t + 100)
    assert partner_rules.convert_used_this_month(conn, pid, t + 200) is True
    conn.execute("UPDATE partner_request SET source = NULL WHERE id = ?", (rid,))
    assert partner_rules.convert_used_this_month(conn, pid, t + 200) is False
    conn.close()


def test_convert_month_boundary_and_non_confirmed(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "cvmonth.db")
    join_ms = _ms(2026, 9, 1)
    _init_pool(conn, monkeypatch, now_ms=join_ms - 10)
    pid = _bind(conn, now_ms=join_ms - 5)
    _deposit(conn, pid, D("200"), D("1200"), join_ms)
    nov_end = _ms(2026, 11, 30, 23, 59)
    dec_start = _ms(2026, 12, 1, 0, 0)
    # rejected / expired / cancelled convert requests do not count
    for status, t in (
        ("rejected", nov_end - 3000),
        ("expired", nov_end - 2000),
        ("cancelled", nov_end - 1000),
    ):
        rid = f"r-{status}"
        conn.execute(
            """
            INSERT INTO partner_request (
                id, partner_id, kind, status, payout_state,
                created_at_ms, expires_at_ms, decided_at_ms
            ) VALUES (?, ?, 'convert', ?, 'none', ?, ?, ?)
            """,
            (rid, pid, status, t, t + 1, t + 1),
        )
    assert partner_rules.convert_used_this_month(conn, pid, nov_end) is False
    # partner portal submit, admin confirms → counts
    _, _, _, sub = _tap(conn, "pp:go:cv", monkeypatch=monkeypatch, wallet="1400", now_ms=nov_end - 500)
    assert sub["ok"] is True
    rid = _pending_rid(conn, pid)
    assert (
        conn.execute("SELECT source FROM partner_request WHERE id=?", (rid,)).fetchone()["source"] == "portal"
    )
    _admin_confirm(conn, rid, monkeypatch=monkeypatch, wallet="1400", now_ms=nov_end - 400)
    assert (
        conn.execute("SELECT status FROM partner_request WHERE id=?", (rid,)).fetchone()["status"]
        == "confirmed"
    )
    assert partner_rules.convert_used_this_month(conn, pid, nov_end) is True
    row = ps.get_partner(conn, pid)
    assert row is not None
    b = partner_rules.block(conn, row, "convert", nav=D("1.2"), now_ms=nov_end)
    assert b is not None and b.code == "convert_month"
    assert "ครั้งถัดไป 01/12/2026" in b.toast
    assert partner_rules.convert_used_this_month(conn, pid, dec_start) is False
    b_dec = partner_rules.block(conn, row, "convert", nav=D("1.2"), now_ms=dec_start)
    assert b_dec is None or b_dec.code != "convert_month"
    answers, sent, edited, out = _tap(
        conn, "pp:rq:cv", monkeypatch=monkeypatch, wallet="1400", now_ms=nov_end
    )
    assert out["reason"] == "convert_month"
    assert answers[0][1] == b.toast
    assert sent == [] and edited == []
    conn.close()


def test_withdraw_cash_min(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "wdmin.db")
    join_ms = _ms(2026, 9, 1)
    _init_pool(conn, monkeypatch, now_ms=join_ms - 10)
    pid = _bind(conn, now_ms=join_ms - 5)
    _deposit(conn, pid, D("100"), D("1100"), join_ms)
    # cash = floor(profit*0.5, 0.01); profit 39.98 → V=1539.78; profit 40 → V=1540
    answers, sent, edited, out = _tap(conn, "pp:rq:wd", monkeypatch=monkeypatch, wallet="1539.78", now_ms=NOW)
    assert out["reason"] == "withdraw_min"
    assert answers[0][1] == "ถอนกำไรได้เมื่อยอดถอนถึง 20 USDT"
    assert sent == [] and edited == []
    answers2, sent2, edited2, out2 = _tap(
        conn, "pp:rq:wd", monkeypatch=monkeypatch, wallet="1540", now_ms=NOW
    )
    assert out2["ok"] is True
    assert answers2[0][1] is None
    assert edited2 or sent2
    conn.close()


def test_no_profit_toast_no_edit(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "noprofit.db")
    join_ms = _ms(2026, 9, 1)
    _init_pool(conn, monkeypatch, now_ms=join_ms - 10)
    pid = _bind(conn, now_ms=join_ms - 5)
    _deposit(conn, pid, D("100"), D("1100"), join_ms)
    before = _counts(conn)
    answers, sent, edited, out = _tap(conn, "pp:rq:cv", monkeypatch=monkeypatch, wallet="1100", now_ms=NOW)
    assert out["reason"] == "no_profit"
    assert answers[0][1] == "ยังไม่มีกำไร ทำรายการนี้ไม่ได้"
    assert sent == [] and edited == []
    assert _counts(conn) == before
    conn.close()


def test_pending_locks_cv_wd_cl(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "pending.db")
    join_ms = _ms(2026, 9, 1)
    _init_pool(conn, monkeypatch, now_ms=join_ms - 10)
    pid = _bind(conn, now_ms=join_ms - 5)
    _deposit(conn, pid, D("200"), D("1200"), join_ms)
    # open a pending convert after day-30 and with enough profit
    answers, sent, edited, out = _tap(conn, "pp:go:cv", monkeypatch=monkeypatch, wallet="1400", now_ms=NOW)
    assert out["ok"] is True
    before = _counts(conn)
    for data, code in (("pp:rq:cv", "pending"), ("pp:rq:wd", "pending"), ("pp:rq:cl", "pending")):
        a, s, e, o = _tap(conn, data, monkeypatch=monkeypatch, wallet="1400", now_ms=NOW + 1)
        assert o == {"ok": False, "reason": code}
        assert a == [("cq1", "มีคำขอค้างอยู่ รอแอดมิน")]
        assert s == [] and e == []
    assert _counts(conn) == before
    conn.close()


def test_closed_partner_stale_buttons(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "closed.db")
    join_ms = _ms(2026, 9, 1)
    _init_pool(conn, monkeypatch, now_ms=join_ms - 10)
    pid = _bind(conn, now_ms=join_ms - 5)
    _deposit(conn, pid, D("100"), D("1100"), join_ms)
    # force closed status without going through close cash path
    conn.execute(
        "UPDATE partner SET status='closed', units_e8=0, cost_micro=0, updated_at_ms=? WHERE id=?",
        (NOW, pid),
    )
    conn.commit()
    before = _counts(conn)
    for data in ("pp:rq:cv", "pp:rq:wd", "pp:rq:cl", "pp:go:cv", "pp:go:wd", "pp:go:cl", "pp:dep"):
        a, s, e, o = _tap(conn, data, monkeypatch=monkeypatch, wallet="1000", now_ms=NOW)
        assert o == {"ok": False, "reason": "closed"}
        assert a == [("cq1", "ปิดพอร์ตแล้ว")]
        assert s == [] and e == []
    assert _counts(conn) == before
    conn.close()


def test_blocked_tap_no_side_effects(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "noside.db")
    join_ms = _ms(2026, 10, 20)
    _init_pool(conn, monkeypatch, now_ms=join_ms - 10)
    pid = _bind(conn, now_ms=join_ms - 5)
    _deposit(conn, pid, D("100"), D("1100"), join_ms)
    before = _counts(conn)
    for data in ("pp:rq:cl", "pp:go:cl"):
        a, s, e, o = _tap(conn, data, monkeypatch=monkeypatch, wallet="1100", now_ms=NOW)
        assert o["ok"] is False
        assert len(a) == 1 and a[0][1]
        assert s == [] and e == []
        _assert_privacy_partner([str(a[0][1])])
    assert _counts(conn) == before
    conn.close()


def test_stale_locked_button_now_eligible_shows_preview(
    tmp_path: Path, monkeypatch: Any, env_chat: None
) -> None:
    conn = open_db(tmp_path / "stale_ok.db")
    join_ms = _ms(2026, 9, 1)
    _init_pool(conn, monkeypatch, now_ms=join_ms - 10)
    pid = _bind(conn, now_ms=join_ms - 5)
    _deposit(conn, pid, D("100"), D("1100"), join_ms)
    answers, sent, edited, out = _tap(conn, "pp:rq:cv", monkeypatch=monkeypatch, wallet="1320", now_ms=NOW)
    assert out["ok"] is True
    body = (edited[0]["text"] if edited else sent[0]["text"]) if (edited or sent) else ""
    assert "ทบกำไร" in body
    assert answers[0][1] is None
    conn.close()


def test_pp_go_cv_wd_v_error(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "verr.db")
    join_ms = _ms(2026, 9, 1)
    _init_pool(conn, monkeypatch, now_ms=join_ms - 10)
    pid = _bind(conn, now_ms=join_ms - 5)
    _deposit(conn, pid, D("200"), D("1200"), join_ms)
    before = _counts(conn)
    err = AccountValueError(reason="no_client")
    for data in ("pp:go:cv", "pp:go:wd"):
        a, s, e, o = _tap(conn, data, monkeypatch=monkeypatch, wallet=None, error=err, now_ms=NOW)
        assert o["ok"] is False
        assert a == [("cq1", "อ่านยอดไม่ได้ตอนนี้ ลองใหม่อีกครั้ง")]
        assert s == [] and e == []
    assert _counts(conn) == before
    conn.close()


def test_pp_go_cv_wd_nav_refused(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    """V readable but pool NAV refuses (V - due <= 0, units > 0): fail closed like V error."""
    conn = open_db(tmp_path / "navref.db")
    join_ms = _ms(2026, 9, 1)
    _init_pool(conn, monkeypatch, now_ms=join_ms - 10)
    pid = _bind(conn, now_ms=join_ms - 5)
    _deposit(conn, pid, D("200"), D("1200"), join_ms)
    before = _counts(conn)
    for data in ("pp:go:cv", "pp:go:wd"):
        a, s, e, o = _tap(conn, data, monkeypatch=monkeypatch, wallet="0", now_ms=NOW)
        assert o == {"ok": False, "reason": "nav_not_positive"}
        assert a == [("cq1", "อ่านยอดไม่ได้ตอนนี้ ลองใหม่อีกครั้ง")]
        assert s == [] and e == []
    assert _counts(conn) == before
    assert not partner_rules._pending(conn, pid, NOW)
    conn.close()


def test_owner_override_under_min_and_twice(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn = open_db(tmp_path / "override.db")
    join_ms = _ms(2026, 9, 1)
    _init_pool(conn, monkeypatch, now_ms=join_ms - 10)
    pid = _bind(conn, now_ms=join_ms - 5)
    _deposit(conn, pid, D("100"), D("1100"), join_ms)
    cli = FakeValueClient(wallet="1110")  # profit 10 < 20
    _patch_read(monkeypatch, cli)
    portal.reset_value_cache()
    sent, send = _send_capture()
    out = pa.handle_partner_text(
        conn,
        "p cv B",
        message=_owner_msg("p cv B"),
        send=send,
        futures=cli,
        now_ms=NOW,
    )
    assert out.get("ok") is True
    rid = conn.execute(
        "SELECT id FROM partner_request WHERE partner_id=? AND status='pending'",
        (pid,),
    ).fetchone()["id"]
    pa.handle_partner_callback(
        conn,
        f"pa:ry:{rid}",
        callback=_owner_cb(data=f"pa:ry:{rid}"),
        send=_send_capture()[1],
        answer=_answer_capture()[1],
        edit=_edit_capture()[1],
        futures=cli,
        now_ms=NOW + 1,
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) AS n FROM partner_event WHERE partner_id=? AND kind='convert'",
            (pid,),
        ).fetchone()["n"]
        == 1
    )
    # second convert same month still allowed via admin
    cli2 = FakeValueClient(wallet="1120")
    _patch_read(monkeypatch, cli2)
    portal.reset_value_cache()
    out2 = pa.handle_partner_text(
        conn,
        "p cv B",
        message=_owner_msg("p cv B"),
        send=_send_capture()[1],
        futures=cli2,
        now_ms=NOW + 2,
    )
    assert out2.get("ok") is True
    rid2 = conn.execute(
        "SELECT id FROM partner_request WHERE partner_id=? AND status='pending'",
        (pid,),
    ).fetchone()["id"]
    pa.handle_partner_callback(
        conn,
        f"pa:ry:{rid2}",
        callback=_owner_cb(data=f"pa:ry:{rid2}"),
        send=_send_capture()[1],
        answer=_answer_capture()[1],
        edit=_edit_capture()[1],
        futures=cli2,
        now_ms=NOW + 3,
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) AS n FROM partner_event WHERE partner_id=? AND kind='convert'",
            (pid,),
        ).fetchone()["n"]
        == 2
    )
    # withdraw under min via admin
    cli3 = FakeValueClient(wallet="1130")
    _patch_read(monkeypatch, cli3)
    portal.reset_value_cache()
    out3 = pa.handle_partner_text(
        conn,
        "p wd B",
        message=_owner_msg("p wd B"),
        send=_send_capture()[1],
        futures=cli3,
        now_ms=NOW + 4,
    )
    assert out3.get("ok") is True
    conn.close()


def test_card_buttons_default_layout_unchanged() -> None:
    markup = portal._card_buttons()
    rows = markup["inline_keyboard"]
    assert rows[0] == [
        {"text": "🔄 ดูยอด", "callback_data": "pp:v"},
        {"text": "💰 แจ้งฝาก", "callback_data": "pp:dep"},
    ]
    assert rows[1] == [
        {"text": "🔁 ทบกำไร", "callback_data": "pp:rq:cv"},
        {"text": "💸 ถอนกำไร", "callback_data": "pp:rq:wd"},
    ]
    assert rows[2] == [{"text": "🚪 ปิดพอร์ต", "callback_data": "pp:rq:cl"}]
    assert rows[3] == [{"text": "📊 ไม้ที่เปิดอยู่", "callback_data": "pp:pos"}]


def test_locked_buttons_keep_callback() -> None:
    locks = {
        "cv": partner_rules.Block("convert_min", "x", "🔒 ทบกำไร"),
        "wd": partner_rules.Block("withdraw_min", "y", "🔒 ถอนกำไร"),
        "cl": partner_rules.Block("close_too_soon", "z", "🔒 ปิดพอร์ต · อีก 3 วัน"),
    }
    rows = portal._card_buttons(locks)["inline_keyboard"]
    assert rows[1][0] == {"text": "🔒 ทบกำไร", "callback_data": "pp:rq:cv"}
    assert rows[1][1] == {"text": "🔒 ถอนกำไร", "callback_data": "pp:rq:wd"}
    assert rows[2][0] == {"text": "🔒 ปิดพอร์ต · อีก 3 วัน", "callback_data": "pp:rq:cl"}


def test_partner_rules_import_guard() -> None:
    import ast

    root = Path(__file__).resolve().parents[1] / "src" / "jarvise_assist"
    src = (root / "partner_rules.py").read_text(encoding="utf-8")
    assert "float(" not in src
    tree = ast.parse(src)
    allowed = {"__future__", "sqlite3", "dataclasses", "datetime", "decimal", "jarvise_assist"}
    banned = {"partner_audit", "partner_portal"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                assert top in allowed, alias.name
                assert top not in banned and alias.name not in banned, alias.name
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, node.module
            mod = node.module or ""
            top = mod.split(".")[0]
            assert top in allowed, mod
            parts = set(mod.split("."))
            assert banned.isdisjoint(parts), mod
            for alias in node.names:
                assert alias.name not in banned, alias.name
