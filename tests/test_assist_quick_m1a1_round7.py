"""M1a-1 round 7 — idempotent replay, S-row gaps, orphan repair."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.test_assist_execute import _forbid_trade_client
from tests.test_assist_quick import _fill
from tests.test_assist_quick_m1a1_round1 import _flow_to_quoted as flow_sui_quoted
from tests.test_assist_quick_m1a1_round4 import (
    MSG_ALREADY_REGISTERED,
    MSG_BAD_BUTTON,
    T0,
    _flow_to_quoted,
    _happy_fake,
    _run_qk,
    _seed_settings,
)
from tests.test_assist_real_trades import FakeFutures

from jarvise_assist.messages import fmt_price
from jarvise_assist.quick import create_quick_registration, sync_quick_trades
from jarvise_assist.quick_card import quick_mode_tag
from jarvise_assist.quick_m1a1 import _registered_idempotent_card_text
from jarvise_ingest.db import open_db


class _TxnProbeError(BaseException):
    pass


def test_qk_rg_replay_uses_stored_registration_not_mark(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A1: after mark move, replay shows planned entry/stop/qty from registration."""
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "a1a.db")
    _seed_settings(conn)
    fake = _happy_fake(marks={"SUIUSDT": Decimal("2.0")})
    cid = flow_sui_quoted(conn, fake)
    _, _, out, _ = _run_qk(conn, f"qk:rg:{cid}", fake)
    assert out.get("ok") is True
    reg = conn.execute(
        "SELECT * FROM assist_quick_registration WHERE id=?", (out["registration_id"],)
    ).fetchone()
    before = dict(reg)
    fake.marks["SUIUSDT"] = Decimal("2.1")
    _, answers, out2, edits = _run_qk(conn, f"qk:rg:{cid}", fake)
    assert out2 == {"ok": True, "idempotent": True}
    assert answers == [MSG_ALREADY_REGISTERED]
    assert len(edits) == 1
    assert edits[-1]["reply_markup"] == {"inline_keyboard": []}
    body = edits[-1]["text"]
    assert fmt_price(Decimal(str(before["planned_entry"]))) in body
    assert fmt_price(Decimal(str(before["planned_stop"]))) in body
    assert str(before["planned_entry"]) in body or "2" in body
    assert "2.1" not in body
    after = conn.execute("SELECT * FROM assist_quick_registration WHERE id=?", (reg["id"],)).fetchone()
    assert float(after["planned_entry"]) == float(before["planned_entry"])
    conn.close()


def test_qk_rg_replay_clears_keyboard_after_failed_first_edit(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A1: first edit failed; replay must edit once with empty keyboard."""
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "a1b.db")
    _seed_settings(conn)
    fake = _happy_fake()
    cid = _flow_to_quoted(conn, fake)
    _, _, out, edits_first = _run_qk(conn, f"qk:rg:{cid}", fake, edit_returns=False)
    assert out.get("registration_id")
    assert len(edits_first) >= 1
    _, _, out2, edits2 = _run_qk(conn, f"qk:rg:{cid}", fake, edit_returns=True)
    assert out2 == {"ok": True, "idempotent": True}
    assert len(edits2) == 1
    assert edits2[0]["reply_markup"] == {"inline_keyboard": []}
    conn.close()


class _OrphanRaceConn:
    """Links registration_id when orphan repair UPDATE runs."""

    def __init__(self, inner: Any, *, cid: str, rid: str) -> None:
        self._inner = inner
        self._cid = cid
        self._rid = rid

    def execute(self, sql: str, params: Any = ()) -> Any:
        if "registration_id IS NULL" in sql and "status='registered'" in sql and "status='quoted'" in sql:
            self._inner.execute(
                "UPDATE assist_quick_card SET registration_id=? WHERE id=?",
                (self._rid, self._cid),
            )
            self._inner.commit()
        return self._inner.execute(sql, params)

    def commit(self) -> None:
        self._inner.commit()

    def rollback(self) -> None:
        self._inner.rollback()

    @property
    def in_transaction(self) -> bool:
        return self._inner.in_transaction

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def test_orphan_repair_noop_when_rid_linked(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """B1: repair no-op when rid linked concurrently → idempotent, not bad button."""
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    inner = open_db(tmp_path / "b1.db")
    _seed_settings(inner)
    fake = _happy_fake()
    cid = _flow_to_quoted(inner, fake)
    _, _, out, _ = _run_qk(inner, f"qk:rg:{cid}", fake)
    rid = out["registration_id"]
    inner.execute(
        "UPDATE assist_quick_card SET status='registered', registration_id=NULL WHERE id=?",
        (cid,),
    )
    inner.commit()
    conn = _OrphanRaceConn(inner, cid=cid, rid=rid)
    _, answers, out2, edits = _run_qk(conn, f"qk:rg:{cid}", fake)
    assert answers == [MSG_ALREADY_REGISTERED]
    assert out2 == {"ok": True, "idempotent": True}
    assert edits[-1]["reply_markup"] == {"inline_keyboard": []}
    conn.close()


def test_s33_opening_fill_fallback_picks_earliest_opening(tmp_path: Path) -> None:
    """S33b/c: two opening-side fills — earliest time/id wins on rebuild fallback."""
    conn = open_db(tmp_path / "s33bc.db")
    t0 = T0
    rid, _ = create_quick_registration(
        conn, symbol="BTCUSDT", side="long", planned_entry=100, planned_stop=90, qty=1.0, now_ms=t0
    )
    t_open = t0 + 5_000
    fake0 = FakeFutures(trades=[_fill(tid=7, symbol="BTCUSDT", side="BUY", price=100, qty=1, time_ms=t_open)])
    sync_quick_trades(conn, fake0, now_ms=t_open + 100, send=lambda *_a, **_k: True)
    trade = conn.execute("SELECT id FROM assist_quick_trade WHERE registration_id=?", (rid,)).fetchone()
    conn.execute(
        "UPDATE assist_quick_trade SET entry_trade_ids='[999]', entry_time_ms=? WHERE id=?",
        (t_open, trade["id"]),
    )
    conn.commit()
    t_late = t_open + 30_000
    t_early = t_open + 10_000
    fake1 = FakeFutures(
        trades=[
            _fill(tid=3, symbol="BTCUSDT", side="SELL", price=110, qty=1, time_ms=t_late + 10_000),
            _fill(tid=9, symbol="BTCUSDT", side="BUY", price=101, qty=0.5, time_ms=t_late),
            _fill(tid=5, symbol="BTCUSDT", side="BUY", price=100, qty=0.5, time_ms=t_early),
        ]
    )
    sync_quick_trades(conn, fake1, now_ms=t_late + 20_000, send=lambda *_a, **_k: True)
    row = conn.execute(
        "SELECT entry_trade_ids, entry_avg, entry_time_ms FROM assist_quick_trade WHERE id=?",
        (trade["id"],),
    ).fetchone()
    ids = json.loads(row["entry_trade_ids"] or "[]")
    assert ids[0] == 5
    assert int(row["entry_time_ms"]) == t_early
    conn.close()


def test_s35a_qk_rg_non_quoted_bad_button(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """S35a: qk:rg on pick_stop → MSG_BAD_BUTTON."""
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "s35a.db")
    _seed_settings(conn)
    fake = _happy_fake()
    _run_qk(conn, "qk:sd:l", fake)
    cid = conn.execute("SELECT id FROM assist_quick_card WHERE status='pick_symbol'").fetchone()["id"]
    _run_qk(conn, f"qk:sy:{cid}:BTCUSDT", fake)
    _, answers, out, _ = _run_qk(conn, f"qk:rg:{cid}", fake)
    assert answers == [MSG_BAD_BUTTON]
    assert out == {"ok": False}
    conn.close()


def test_s35c_claim_race_already_registered(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """S35c: card leaves quoted before claim → MSG_ALREADY_REGISTERED idempotent."""
    import jarvise_assist.quick_m1a1 as m1

    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "s35c2.db")
    _seed_settings(conn)
    fake = _happy_fake()
    cid = _flow_to_quoted(conn, fake)
    real_build = m1.build_card_at_quoted_stop

    def _build_then_register_race(
        inner: Any, futures: Any, row: dict[str, Any], *, now_ms: int
    ) -> dict[str, Any]:
        card = real_build(inner, futures, row, now_ms=now_ms)
        if card.get("ok"):
            inner.execute(
                "UPDATE assist_quick_card SET status='registered', updated_at_ms=? WHERE id=?",
                (int(now_ms), str(row["id"])),
            )
            inner.commit()
        return card

    monkeypatch.setattr(m1, "build_card_at_quoted_stop", _build_then_register_race)
    before = conn.execute("SELECT COUNT(*) c FROM assist_quick_registration").fetchone()["c"]
    _, answers, out, _ = _run_qk(conn, f"qk:rg:{cid}", fake)
    assert answers == [MSG_ALREADY_REGISTERED]
    assert out == {"ok": True, "idempotent": True}
    assert conn.execute("SELECT COUNT(*) c FROM assist_quick_registration").fetchone()["c"] == before
    conn.close()


def test_qk_rg_baseexception_after_claim_reraises(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Optional: BaseException after claim rolls back and re-raises."""
    import jarvise_assist.quick_m1a1 as m1

    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "base.db")
    _seed_settings(conn)
    fake = _happy_fake()
    cid = _flow_to_quoted(conn, fake)

    def boom(*_a: Any, **_k: Any) -> tuple[str | None, str | None]:
        raise _TxnProbeError("txn kill")

    monkeypatch.setattr(m1, "create_quick_registration_from_card", boom)
    with pytest.raises(_TxnProbeError):
        _run_qk(conn, f"qk:rg:{cid}", fake)
    assert conn.execute("SELECT COUNT(*) c FROM assist_quick_registration").fetchone()["c"] == 0
    row = conn.execute("SELECT status, registration_id FROM assist_quick_card WHERE id=?", (cid,)).fetchone()
    assert row["status"] == "quoted"
    assert row["registration_id"] is None
    conn.close()


def test_s34b_bnb_zero_risk_plain_r_line(tmp_path: Path) -> None:
    """S34b: BNB other + risk_usdt==0 → plain R line (no BNB clause)."""
    from jarvise_assist.quick import _send_close_notice

    conn = open_db(tmp_path / "s34b.db")
    conn.execute(
        """
        INSERT INTO assist_quick_trade (
            id, registration_id, symbol, side, status, net_usdt, net_r,
            risk_usdt, commission_other_asset, close_notice_at_ms, updated_at_ms
        ) VALUES ('tb', 'r', 'BTCUSDT', 'long', 'closed', 1, NULL, 0, 'BNB', NULL, 1)
        """
    )
    conn.commit()
    sent: list[str] = []

    _send_close_notice(
        conn,
        lambda t, **_k: sent.append(t),
        trade_id="tb",
        client=None,
        payload={
            "symbol": "BTCUSDT",
            "side": "long",
            "net_usdt": 1.0,
            "net_r": None,
            "risk_usdt": 0.0,
            "commission_other_asset": "BNB",
        },
        now_ms=T0,
    )
    assert sent == [
        "🏁 ไม้ quick BTCUSDT LONG ปิดแล้ว (ปิดใน Binance) — สุทธิ +1.00 USDT · R คำนวณไม่ได้\n" + quick_mode_tag()
    ]
    conn.close()


def test_s34c_bnb_low_positive_risk_keeps_clause(tmp_path: Path) -> None:
    """S34c: 0 < risk_usdt <= 1 with BNB → BNB fee clause."""
    from jarvise_assist.quick import _send_close_notice

    conn = open_db(tmp_path / "s34c.db")
    conn.execute(
        """
        INSERT INTO assist_quick_trade (
            id, registration_id, symbol, side, status, net_usdt, net_r,
            risk_usdt, commission_other_asset, close_notice_at_ms, updated_at_ms
        ) VALUES ('tc', 'r', 'BTCUSDT', 'long', 'closed', 1, NULL, 0.5, 'BNB', NULL, 1)
        """
    )
    conn.commit()
    sent: list[str] = []

    _send_close_notice(
        conn,
        lambda t, **_k: sent.append(t),
        trade_id="tc",
        client=None,
        payload={
            "symbol": "BTCUSDT",
            "side": "long",
            "net_usdt": 1.0,
            "net_r": None,
            "risk_usdt": 0.5,
            "commission_other_asset": "BNB",
        },
        now_ms=T0,
    )
    assert sent == [
        "🏁 ไม้ quick BTCUSDT LONG ปิดแล้ว (ปิดใน Binance) — สุทธิ +1.00 USDT · "
        "R คำนวณไม่ได้ (ค่าธรรมเนียมเป็น BNB)\n" + quick_mode_tag()
    ]
    conn.close()


def test_registered_idempotent_text_helper(tmp_path: Path) -> None:
    row = {
        "registration_id": "r1",
    }
    conn = open_db(tmp_path / "txt.db")
    conn.execute(
        """
        INSERT INTO assist_quick_registration (
            id, symbol, side, planned_entry, planned_stop, qty,
            created_at_ms, expires_at_ms, status, updated_at_ms
        ) VALUES ('r1', 'SUIUSDT', 'long', 2.0, 1.99, 500, 1, 2, 'open', 1)
        """
    )
    conn.commit()
    text = _registered_idempotent_card_text(conn, row)
    assert "2" in text
    assert "1.99" in text
    assert "500" in text
    assert quick_mode_tag() in text
    conn.close()
