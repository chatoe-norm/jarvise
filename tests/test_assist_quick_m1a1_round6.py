"""Round 6: qk:rg transaction probe, C1–C6, telegram edit parity, execute vs order_lines."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.test_assist_quick_m1a1_round1 import _forbid_trade_client
from tests.test_assist_quick_m1a1_round4 import (
    NOW,
    T0,
    _flow_to_quoted,
    _happy_fake,
    _run_qk,
    _seed_settings,
)

from jarvise_assist.execute import OWNER_CLOSING_ORDER_NOTE
from jarvise_assist.execute import _format_owner_order_line as execute_owner_line
from jarvise_assist.order_lines import format_closing_order_line
from jarvise_assist.quick_card import QUICK_CARD_TTL_MS
from jarvise_assist.quick_m1a1 import (
    MSG_ALREADY_REGISTERED,
    MSG_REGISTER_FAILED,
    _quoted_markup,
    _refresh_only_markup,
    _stop_step_markup,
    _symbol_buttons,
)
from jarvise_ingest.db import open_db
from jarvise_notify import telegram as tg_mod


class _ProbeError(BaseException):
    """Simulate process kill during qk:rg pre-commit reads."""


def test_qk_rg_probe_baseexception_then_replay(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A2: crash during build_card_at_quoted_stop leaves quoted; replay succeeds once."""
    import jarvise_assist.quick_m1a1 as m1

    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "probe.db")
    _seed_settings(conn)
    fake = _happy_fake()
    cid = _flow_to_quoted(conn, fake)

    real_build = m1.build_card_at_quoted_stop

    def boom_once(conn_a: Any, futures: Any, row: dict[str, Any], *, now_ms: int) -> dict[str, Any]:
        if not getattr(boom_once, "boomed", False):
            boom_once.boomed = True  # type: ignore[attr-defined]
            raise _ProbeError("kill")
        return real_build(conn_a, futures, row, now_ms=now_ms)

    monkeypatch.setattr(m1, "build_card_at_quoted_stop", boom_once)
    with pytest.raises(_ProbeError):
        _run_qk(conn, f"qk:rg:{cid}", fake)

    row = conn.execute("SELECT status, registration_id FROM assist_quick_card WHERE id=?", (cid,)).fetchone()
    assert row["status"] == "quoted"
    assert row["registration_id"] is None
    assert conn.execute("SELECT COUNT(*) c FROM assist_quick_registration").fetchone()["c"] == 0

    _, answers_replay, _, _ = _run_qk(conn, f"qk:rg:{cid}", fake)
    assert MSG_ALREADY_REGISTERED not in (answers_replay or [])
    assert conn.execute("SELECT COUNT(*) c FROM assist_quick_registration").fetchone()["c"] == 1
    row2 = conn.execute("SELECT status, registration_id FROM assist_quick_card WHERE id=?", (cid,)).fetchone()
    assert row2["status"] == "registered"
    assert row2["registration_id"] is not None

    _, answers_dup, out_dup, _ = _run_qk(conn, f"qk:rg:{cid}", fake)
    assert answers_dup == [MSG_ALREADY_REGISTERED]
    assert out_dup == {"ok": True, "idempotent": True}
    conn.close()


def test_qk_rg_commit_called_once(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """C1: registration txn ends with a single commit after claim+insert+link."""
    import jarvise_assist.quick_m1a1 as m1

    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "c1.db")
    _seed_settings(conn)
    fake = _happy_fake()
    cid = _flow_to_quoted(conn, fake)
    commits_at_link: list[int] = []
    real_from_card = m1.create_quick_registration_from_card

    def wrap_from_card(*args: Any, **kwargs: Any) -> tuple[str | None, str | None]:
        rid, reason = real_from_card(*args, **kwargs)
        if rid is not None:
            commits_at_link.append(int(conn.in_transaction))
        return rid, reason

    monkeypatch.setattr(m1, "create_quick_registration_from_card", wrap_from_card)
    _, _, out, _ = _run_qk(conn, f"qk:rg:{cid}", fake)
    assert out.get("ok") is True
    assert commits_at_link == [1]
    conn.close()


def test_quote_ttl_zero_and_future_skew_expired(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """C5: quoted_at_ms <= 0 or far-future quoted_at expires on qk:rg."""
    from jarvise_assist.quick_m1a1 import QUOTE_CLOCK_SKEW_MS

    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "c5a.db")
    _seed_settings(conn)
    cid = _flow_to_quoted(conn, _happy_fake())
    conn.execute("UPDATE assist_quick_card SET quoted_at_ms=0 WHERE id=?", (cid,))
    conn.commit()
    _, answers, out, _ = _run_qk(conn, f"qk:rg:{cid}", _happy_fake())
    assert out == {"ok": False, "quote_expired": True}
    assert answers != [MSG_REGISTER_FAILED]
    conn.close()

    conn2 = open_db(tmp_path / "c5b.db")
    _seed_settings(conn2)
    cid2 = _flow_to_quoted(conn2, _happy_fake())
    future = NOW + QUOTE_CLOCK_SKEW_MS + 1
    conn2.execute("UPDATE assist_quick_card SET quoted_at_ms=? WHERE id=?", (future, cid2))
    conn2.commit()
    _, answers2, out2, _ = _run_qk(conn2, f"qk:rg:{cid2}", _happy_fake(), now_ms=NOW)
    assert out2 == {"ok": False, "quote_expired": True}
    conn2.close()


def test_quote_ttl_boundary_exact_ms(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """S35b: age == TTL registers; TTL+1 ms expires."""
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "ttl.db")
    _seed_settings(conn)
    fake = _happy_fake()
    cid = _flow_to_quoted(conn, fake)
    qa = conn.execute("SELECT quoted_at_ms FROM assist_quick_card WHERE id=?", (cid,)).fetchone()[0]
    t_ok = int(qa) + QUICK_CARD_TTL_MS
    _, _, out_ok, _ = _run_qk(conn, f"qk:rg:{cid}", fake, now_ms=t_ok)
    assert out_ok.get("ok") is True

    conn2 = open_db(tmp_path / "ttl2.db")
    _seed_settings(conn2)
    cid2 = _flow_to_quoted(conn2, fake)
    qa2 = conn2.execute("SELECT quoted_at_ms FROM assist_quick_card WHERE id=?", (cid2,)).fetchone()[0]
    t_bad = int(qa2) + QUICK_CARD_TTL_MS + 1
    _, answers, out_bad, edits = _run_qk(conn2, f"qk:rg:{cid2}", fake, now_ms=t_bad)
    from jarvise_assist.quick_m1a1 import MSG_CARD_QUOTE_EXPIRED

    assert out_bad == {"ok": False, "quote_expired": True}
    assert answers == [MSG_CARD_QUOTE_EXPIRED]
    assert edits[-1]["reply_markup"] == _refresh_only_markup(cid2)
    conn.close()
    conn2.close()


def test_execute_and_order_lines_parity() -> None:
    """C6: restored execute helpers match order_lines on the same orders."""
    tick = Decimal("0.1")
    orders = [
        {
            "type": "STOP_MARKET",
            "side": "SELL",
            "stopPrice": "95000",
            "origQty": "1",
            "reduceOnly": True,
        },
        {
            "type": "STOP_MARKET",
            "side": "SELL",
            "stopPrice": "95000",
            "closePosition": True,
        },
        {
            "type": "LIMIT",
            "side": "SELL",
            "price": "110000",
            "origQty": "0.01",
            "reduceOnly": True,
        },
        {
            "type": "STOP_MARKET",
            "side": "SELL",
            "stopPrice": "99",
            "origQty": "1",
        },
    ]
    for order in orders:
        for t in (tick, None):
            assert execute_owner_line(order, tick=t) == format_closing_order_line(
                order, tick=t, note=OWNER_CLOSING_ORDER_NOTE
            )


def test_callback_data_lengths_under_o() -> None:
    """A3/B14: 64-byte guard is runtime, not assert (run with python -O separately)."""
    short = _symbol_buttons("c1", ["BTCUSDT", "1000000BABYDOGEUSDT"])
    assert any(row[0]["callback_data"].endswith("BTCUSDT") for row in short if row)
    for row in short:
        for btn in row:
            assert len(btn["callback_data"].encode("utf-8")) <= 64
    cid = "card_" + "x" * 8
    for markup in (_quoted_markup(cid), _stop_step_markup(cid), _refresh_only_markup(cid)):
        for row in markup.get("inline_keyboard") or []:
            for btn in row:
                assert len(btn["callback_data"].encode("utf-8")) <= 64


def test_tag_decision_picks_earliest_opening_fill(tmp_path: Path) -> None:
    """S36: list order ≠ time order; earliest BUY after reg creation tags."""
    from tests.test_assist_quick import _fill
    from tests.test_assist_real_trades import FakeFutures

    from jarvise_assist.quick import create_quick_registration, sync_quick_trades

    conn = open_db(tmp_path / "s36.db")
    _seed_settings(conn)
    t0 = T0
    create_quick_registration(
        conn, symbol="BTCUSDT", side="long", planned_entry=100, planned_stop=90, qty=1, now_ms=t0
    )
    buy_t = t0 + 10_000
    sell_t = t0 + 20_000
    fake = FakeFutures(
        trades=[
            _fill(tid=2, symbol="BTCUSDT", side="SELL", price=99, qty=1, time_ms=sell_t),
            _fill(tid=5, symbol="BTCUSDT", side="BUY", price=100, qty=1, time_ms=buy_t),
        ]
    )
    sent: list[str] = []
    info = sync_quick_trades(conn, fake, now_ms=sell_t + 1, send=lambda t, **_k: sent.append(t) or True)
    assert info.get("tagged") == 1
    trade = conn.execute("SELECT * FROM assist_quick_trade WHERE registration_id IS NOT NULL").fetchone()
    assert trade is not None
    assert int(trade["entry_time_ms"]) == buy_t
    assert json.loads(trade["entry_trade_ids"]) == [5]
    assert json.loads(trade["exit_trade_ids"]) == [2]
    assert len(sent) >= 1
    assert sent[0].startswith("✅ จับไม้ quick")
    conn.close()


def test_telegram_edit_message_not_modified_is_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """B14: HTTP 400 'message is not modified' counts as success."""

    class _Resp:
        status_code = 400

        @staticmethod
        def json() -> dict[str, str]:
            return {"description": "Bad Request: message is not modified"}

    class _Http:
        def post(self, *_a: Any, **_k: Any) -> _Resp:
            return _Resp()

        def close(self) -> None:
            pass

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    assert tg_mod.edit_message(1, "same", client=_Http()) is True
