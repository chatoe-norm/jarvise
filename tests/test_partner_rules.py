"""Partner portal rules (PR-A): join date, 30-day close, mins, request source, locks."""

from __future__ import annotations

from datetime import UTC, datetime
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


def test_admin_p_cv_records_admin_source(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
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
    # A portal convert right after the admin one is still allowed (profit back above 20 at V=1600).
    _, _, _, sub = _tap(conn, "pp:go:cv", monkeypatch=monkeypatch, wallet="1600", now_ms=t + 300)
    assert sub["ok"] is True, sub
    req = conn.execute("SELECT source FROM partner_request WHERE id=?", (str(sub["request_id"]),)).fetchone()
    assert req["source"] == "portal"
    conn.close()


def test_min_toasts_use_constants(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    monkeypatch.setattr(partner_rules, "MIN_CONVERT_PROFIT", D("1000"))
    monkeypatch.setattr(partner_rules, "MIN_WITHDRAW_CASH", D("1000"))
    monkeypatch.setattr(partner_rules, "CLOSE_MIN_DAYS", 45)
    conn = open_db(tmp_path / "toasts.db")
    join_ms = _ms(2026, 9, 1)
    _init_pool(conn, monkeypatch, now_ms=join_ms - 10)
    pid = _bind(conn, now_ms=join_ms - 5)
    _deposit(conn, pid, D("200"), D("1200"), join_ms)
    row = ps.get_partner(conn, pid)
    assert row is not None
    t = _ms(2026, 9, 10)
    cv = partner_rules.block(conn, row, "convert", nav=D("1.4"), now_ms=t)
    assert cv is not None and cv.code == "convert_min" and "1000 USDT" in cv.toast
    wd = partner_rules.block(conn, row, "withdraw_profit", nav=D("1.4"), now_ms=t)
    assert wd is not None and wd.code == "withdraw_min" and "1000 USDT" in wd.toast
    cl = partner_rules.block(conn, row, "close", nav=None, now_ms=t)
    assert cl is not None and cl.code == "close_too_soon" and "ครบ 45 วัน" in cl.toast
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
    # second convert, also under min, still allowed via admin
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


# --- PR-A follow-up 2: repeat converts, boundaries, mins ------------------------------------------


def _setup_partner(
    tmp_path: Path,
    name: str,
    monkeypatch: Any,
    *,
    join_ms: int,
    amount: str = "200",
    v: str = "1200",
) -> tuple[Any, str]:
    conn = open_db(tmp_path / name)
    _init_pool(conn, monkeypatch, now_ms=join_ms - 1000)
    pid = _bind(conn, now_ms=join_ms - 500)
    _deposit(conn, pid, D(amount), D(v), join_ms)
    return conn, pid


def _portal_cv_confirmed(
    conn: Any, pid: str, monkeypatch: Any, *, submit_ms: int, confirm_ms: int, wallet: str = "1400"
) -> str:
    _, _, _, sub = _tap(conn, "pp:go:cv", monkeypatch=monkeypatch, wallet=wallet, now_ms=submit_ms)
    assert sub["ok"] is True, sub
    rid = str(sub["request_id"])
    _admin_confirm(conn, rid, monkeypatch=monkeypatch, wallet=wallet, now_ms=confirm_ms)
    req = conn.execute("SELECT status, source FROM partner_request WHERE id=?", (rid,)).fetchone()
    assert (req["status"], req["source"]) == ("confirmed", "portal")
    ev = conn.execute(
        "SELECT created_at_ms FROM partner_event WHERE request_id=? AND kind='convert'", (rid,)
    ).fetchone()
    assert int(ev["created_at_ms"]) == confirm_ms
    return rid


def _pending_count(conn: Any, pid: str) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM partner_request WHERE partner_id=? AND status='pending'", (pid,)
        ).fetchone()["n"]
    )


def test_portal_convert_twice_same_month(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    """Owner decision (PR #361): no per-month limit on portal converts. Each needs >=20 USDT
    pre-fee profit, and only one request may be pending at a time."""
    from jarvise_assist import partner_ledger as pl

    conn, pid = _setup_partner(tmp_path, "cv_twice.db", monkeypatch, join_ms=_ms(2026, 9, 1))
    t = _ms(2026, 11, 10)

    # First portal convert (profit 33.33 at V=1400), requested and confirmed.
    rid1 = _portal_cv_confirmed(conn, pid, monkeypatch, submit_ms=t, confirm_ms=t + 100, wallet="1400")

    # Second portal convert in the same Bangkok month (profit ~46 at V=1700) is accepted.
    _, _, _, sub2 = _tap(conn, "pp:go:cv", monkeypatch=monkeypatch, wallet="1700", now_ms=t + 200)
    assert sub2["ok"] is True, sub2
    rid2 = str(sub2["request_id"])

    # While it is pending, a third convert is refused at tap and at create; no new rows.
    before = _counts(conn)
    a, s, e, o = _tap(conn, "pp:go:cv", monkeypatch=monkeypatch, wallet="1700", now_ms=t + 300)
    assert o == {"ok": False, "reason": "pending"}
    assert a == [("cq1", "มีคำขอค้างอยู่ รอแอดมิน")]
    assert s == [] and e == []
    direct = ps.create_request(conn, pid, "convert", t + 300, source="portal")
    assert direct == {"ok": False, "reason": "request_pending"}
    assert _counts(conn) == before
    assert _pending_count(conn, pid) == 1

    _admin_confirm(conn, rid2, monkeypatch=monkeypatch, wallet="1700", now_ms=t + 400)
    rows = conn.execute(
        """
        SELECT r.id, r.status, r.source, r.created_at_ms FROM partner_event e
        JOIN partner_request r ON r.id = e.request_id
        WHERE e.partner_id = ? AND e.kind = 'convert' ORDER BY r.created_at_ms
        """,
        (pid,),
    ).fetchall()
    assert [(r["id"], r["status"], r["source"]) for r in rows] == [
        (rid1, "confirmed", "portal"),
        (rid2, "confirmed", "portal"),
    ]
    months = {
        datetime.fromtimestamp(int(r["created_at_ms"]) / 1000, tz=BANGKOK).strftime("%Y-%m") for r in rows
    }
    assert months == {"2026-11"}

    # A third convert with pre-fee profit under 20 is still refused by the min (no month lock).
    row = ps.get_partner(conn, pid)
    assert row is not None
    units = ps.units_from_e8(int(row["units_e8"]))
    cost = ps.usdt_from_micro(int(row["cost_micro"]))
    due = ps.payouts_due_total(conn)
    u_total = ps.total_units(conn)
    w_low = (due + u_total * (cost + D("10")) / units).quantize(D("0.01"))
    nav_low = pl.nav(w_low - due, u_total)
    assert not isinstance(nav_low, pl.Refusal)
    plan = pl.plan_convert(
        units=units, cost=cost, fee_rate=ps.fee_rate_from_bps(int(row["fee_rate_bps"])), nav=nav_low
    )
    assert not isinstance(plan, pl.Refusal)
    assert D("0") < plan.profit < partner_rules.MIN_CONVERT_PROFIT
    before = _counts(conn)
    a, s, e, o = _tap(conn, "pp:go:cv", monkeypatch=monkeypatch, wallet=str(w_low), now_ms=t + 500)
    assert o == {"ok": False, "reason": "convert_min"}
    assert a == [("cq1", "ทบกำไรได้เมื่อกำไรก่อนหักค่าธรรมเนียมถึง 20 USDT")]
    assert s == [] and e == []
    assert _counts(conn) == before
    conn.close()


def test_double_go_single_request(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    conn, pid = _setup_partner(tmp_path, "dbl.db", monkeypatch, join_ms=_ms(2026, 9, 1))
    for second in ("pp:go:cv", "pp:go:wd"):
        _, _, _, first = _tap(conn, "pp:go:cv", monkeypatch=monkeypatch, wallet="1400", now_ms=NOW)
        assert first["ok"] is True, first
        a, s, e, o = _tap(conn, second, monkeypatch=monkeypatch, wallet="1400", now_ms=NOW + 1)
        assert o == {"ok": False, "reason": "pending"}
        assert a == [("cq1", "มีคำขอค้างอยู่ รอแอดมิน")]
        assert s == [] and e == []
        assert _pending_count(conn, pid) == 1
        assert ps.reject_request(conn, str(first["request_id"]), NOW + 2)["ok"] is True

    # Same with the tap-time check bypassed: create_request alone keeps a single pending row.
    monkeypatch.setattr(partner_rules, "block", lambda *a, **k: None)
    for second in ("pp:go:cv", "pp:go:wd"):
        _, _, _, first = _tap(conn, "pp:go:cv", monkeypatch=monkeypatch, wallet="1400", now_ms=NOW + 10)
        assert first["ok"] is True, first
        a, s, e, o = _tap(conn, second, monkeypatch=monkeypatch, wallet="1400", now_ms=NOW + 11)
        assert o == {"ok": False, "reason": "request_pending"}
        assert a == [("cq1", "มีคำขอค้างอยู่ รอแอดมิน")]
        assert _pending_count(conn, pid) == 1
        assert ps.reject_request(conn, str(first["request_id"]), NOW + 12)["ok"] is True
    conn.close()


def test_close_join_bkk_vs_utc_date(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    """Join 01/10 00:30 BKK is still 30/09 in UTC; the 30-day clock uses the Bangkok date."""
    join_ms = _ms(2026, 10, 1, 0, 30)
    assert datetime.fromtimestamp(join_ms / 1000, tz=UTC).day == 30
    conn, pid = _setup_partner(tmp_path, "bkkjoin.db", monkeypatch, join_ms=join_ms, amount="100", v="1100")
    assert partner_rules.joined_at_ms(conn, pid) == join_ms
    row = ps.get_partner(conn, pid)
    assert row is not None
    last_locked = int(datetime(2026, 10, 30, 23, 59, 59, 999000, tzinfo=BANGKOK).timestamp() * 1000)
    first_open = _ms(2026, 10, 31, 0, 0)
    assert first_open - last_locked == 1
    b = partner_rules.block(conn, row, "close", nav=None, now_ms=last_locked)
    assert b is not None and b.code == "close_too_soon"
    assert b.label == "🔒 ปิดพอร์ต · อีก 1 วัน"
    assert "ตั้งแต่ 31/10/2026" in b.toast
    assert partner_rules.block(conn, row, "close", nav=None, now_ms=first_open) is None
    conn.close()


def test_join_ignores_pending_and_uses_first_confirm(
    tmp_path: Path, monkeypatch: Any, env_chat: None
) -> None:
    conn = open_db(tmp_path / "joinfirst.db")
    t1 = _ms(2026, 9, 1, 20, 0)
    _init_pool(conn, monkeypatch, now_ms=t1 - 1000)
    pid = _bind(conn, now_ms=t1 - 500)
    req1 = ps.create_request(conn, pid, "deposit", t1, amount=D("100"), source="portal")
    assert req1["ok"] is True
    # Pending deposit request is not a join.
    assert partner_rules.joined_at_ms(conn, pid) is None

    t2 = _ms(2026, 9, 2, 9, 0)  # next Bangkok day, inside the 24h TTL
    assert ps.confirm_request(conn, str(req1["request_id"]), D("1100"), t2, amount_received=D("100"))["ok"]
    assert partner_rules.joined_at_ms(conn, pid) == t2  # confirm time, not request time

    t3 = _ms(2026, 9, 5)
    req2 = ps.create_request(conn, pid, "deposit", t3, amount=D("100"), source="portal")
    assert req2["ok"] is True
    assert ps.confirm_request(conn, str(req2["request_id"]), D("1200"), t3 + 10, amount_received=D("100"))[
        "ok"
    ]
    assert partner_rules.joined_at_ms(conn, pid) == t2  # first confirm stays

    row = ps.get_partner(conn, pid)
    assert row is not None
    assert partner_rules.close_open_date(t2).strftime("%d/%m/%Y") == "02/10/2026"
    locked = partner_rules.block(conn, row, "close", nav=None, now_ms=_ms(2026, 10, 1, 23, 59))
    assert locked is not None and locked.code == "close_too_soon"
    assert partner_rules.block(conn, row, "close", nav=None, now_ms=_ms(2026, 10, 2, 0, 0)) is None
    conn.close()


def test_withdraw_min_exact_20_and_rounding(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    from jarvise_assist import partner_ledger as pl

    conn, pid = _setup_partner(
        tmp_path, "wd20.db", monkeypatch, join_ms=_ms(2026, 9, 1), amount="100", v="1100"
    )
    row = ps.get_partner(conn, pid)
    assert row is not None
    assert ps.units_from_e8(int(row["units_e8"])) == D("100")
    assert ps.usdt_from_micro(int(row["cost_micro"])) == D("100")
    assert int(row["fee_rate_bps"]) == 5000

    def cash(r: Any, nav: str) -> Decimal:
        plan = pl.plan_withdraw_profit(
            units=ps.units_from_e8(int(r["units_e8"])),
            cost=ps.usdt_from_micro(int(r["cost_micro"])),
            fee_rate=ps.fee_rate_from_bps(int(r["fee_rate_bps"])),
            nav=D(nav),
        )
        assert not isinstance(plan, pl.Refusal)
        return plan.cash

    # 50%: profit 40.00 → 20.00 ok; profit 39.99 → 19.995 → floor 19.99 blocked.
    assert cash(row, "1.40") == D("20.00")
    assert partner_rules.block(conn, row, "withdraw_profit", nav=D("1.40"), now_ms=NOW) is None
    assert cash(row, "1.3999") == D("19.99")
    b = partner_rules.block(conn, row, "withdraw_profit", nav=D("1.3999"), now_ms=NOW)
    assert b is not None and b.code == "withdraw_min"

    # fee_rate_bps=3333: share = floor_micro(profit * 0.6667), cash = floor_cent(share).
    conn.execute("UPDATE partner SET fee_rate_bps = 3333 WHERE id = ?", (pid,))
    row = ps.get_partner(conn, pid)
    assert row is not None
    # profit 29.9985 → 19.99999995 → 19.999999 → 19.99 (half-up would say 20.00): blocked
    assert cash(row, "1.299985") == D("19.99")
    b2 = partner_rules.block(conn, row, "withdraw_profit", nav=D("1.299985"), now_ms=NOW)
    assert b2 is not None and b2.code == "withdraw_min"
    # profit 29.9986 → 20.00006662 → 20.00: ok
    assert cash(row, "1.299986") == D("20.00")
    assert partner_rules.block(conn, row, "withdraw_profit", nav=D("1.299986"), now_ms=NOW) is None
    conn.close()


def _partner_msg(text: str) -> dict[str, Any]:
    return {
        "message_id": 1,
        "chat": {"id": PARTNER_TG, "type": "private"},
        "from": {"id": PARTNER_TG},
        "text": text,
    }


def _source_of_rid(conn: Any, rid: str) -> str | None:
    row = conn.execute("SELECT source FROM partner_request WHERE id=?", (rid,)).fetchone()
    assert row is not None
    return row["source"]


def test_direct_apply_wd_close_records_admin_source(tmp_path: Path, monkeypatch: Any, env_chat: None) -> None:
    from jarvise_ingest.db import SCHEMA_VERSION

    assert SCHEMA_VERSION >= 31
    conn, pid = _setup_partner(tmp_path, "directsrc.db", monkeypatch, join_ms=_ms(2026, 9, 1))
    wd = ps.apply_withdraw_profit(conn, pid, D("1600"), NOW)
    assert wd["ok"] is True, wd
    cl = ps.apply_close(conn, pid, D("1600"), NOW + 1000)
    assert cl["ok"] is True, cl
    rows = conn.execute(
        "SELECT id, kind, status, source FROM partner_request WHERE partner_id=? ORDER BY created_at_ms",
        (pid,),
    ).fetchall()
    got = [(str(r["id"]), str(r["kind"]), str(r["status"]), r["source"]) for r in rows]
    assert got == [
        (str(wd["request_id"]), "withdraw_profit", "confirmed", "admin"),
        (str(cl["request_id"]), "close", "confirmed", "admin"),
    ]
    conn.close()


def _drive_portal_dep_text(conn: Any, monkeypatch: Any, now_ms: int) -> dict[str, Any]:
    cli = FakeValueClient(wallet="1600")
    _patch_read(monkeypatch, cli)
    portal.reset_value_cache()
    return portal.handle_portal_text(
        conn, _partner_msg("ฝาก 100"), send=_send_capture()[1], futures=cli, now_ms=now_ms
    )


def _drive_portal_go(code: str) -> Any:
    def drive(conn: Any, monkeypatch: Any, now_ms: int) -> dict[str, Any]:
        return _tap(conn, f"pp:go:{code}", monkeypatch=monkeypatch, wallet="1600", now_ms=now_ms)[3]

    return drive


def _drive_admin(text: str, wallet: str = "1600") -> Any:
    def drive(conn: Any, monkeypatch: Any, now_ms: int) -> dict[str, Any]:
        cli = FakeValueClient(wallet=wallet)
        _patch_read(monkeypatch, cli)
        portal.reset_value_cache()
        return pa.handle_partner_text(
            conn, text, message=_owner_msg(text), send=_send_capture()[1], futures=cli, now_ms=now_ms
        )

    return drive


@pytest.mark.parametrize(
    ("drive", "kind", "source"),
    [
        pytest.param(_drive_portal_dep_text, "deposit", "portal", id="portal-dep-text"),
        pytest.param(_drive_portal_go("wd"), "withdraw_profit", "portal", id="portal-go-wd"),
        pytest.param(_drive_portal_go("cl"), "close", "portal", id="portal-go-close"),
        pytest.param(_drive_admin("p dep B 100", wallet="1700"), "deposit", "admin", id="admin-p-dep"),
        pytest.param(_drive_admin("p wd B"), "withdraw_profit", "admin", id="admin-p-wd"),
        pytest.param(_drive_admin("p close B"), "close", "admin", id="admin-p-close"),
    ],
)
def test_handlers_record_request_source(
    tmp_path: Path, monkeypatch: Any, env_chat: None, drive: Any, kind: str, source: str
) -> None:
    conn, pid = _setup_partner(tmp_path, "handlersrc.db", monkeypatch, join_ms=_ms(2026, 9, 1))
    assert (
        conn.execute("SELECT COUNT(*) AS n FROM partner_request WHERE partner_id=?", (pid,)).fetchone()["n"]
        == 0
    )
    out = drive(conn, monkeypatch, NOW)
    assert out.get("ok") is True, out
    rows = conn.execute(
        "SELECT kind, status, source FROM partner_request WHERE partner_id=?", (pid,)
    ).fetchall()
    assert [(str(r["kind"]), str(r["status"]), r["source"]) for r in rows] == [(kind, "pending", source)]
    conn.close()
