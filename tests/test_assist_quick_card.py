"""(N) QPR2a quick sizing card — fail on main @ 122566a, pass after."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.test_assist import FakeFutures, _ms_bangkok
from tests.test_assist_execute import _forbid_trade_client
from tests.test_assist_listen import _send_capture
from tests.test_assist_quick import _answers
from tests.test_assist_quick_guards import (
    _insert_quick_registration,
    _insert_quick_trade,
    _pos,
    _stop_order,
    _table_rows,
)
from tests.test_assist_week_loss_4r import TUE, WED_10, WEEK_ID, _eval

from jarvise_assist.futures import FuturesPosition
from jarvise_assist.keyboard import BTN_ANYTHING, BTN_POSITIONS, BTN_QUICK, BTN_TOGGLE, BTN_WEEKLY
from jarvise_assist.listen import process_update
from jarvise_assist.pause import (
    bangkok_day_id,
    bangkok_week_id,
    clear_pause_manual,
    format_paused_until,
    next_0820_bangkok_ms,
)
from jarvise_assist.quick import (
    QUICK_USAGE,
    create_quick_registration,
    get_quick_settings,
    render_quick_menu,
    write_quick_settings,
)
from jarvise_assist.quick_card import (
    QUICK_REGISTER_PREFIX,
    REFUSAL,
    build_quick_card,
    fee_breakdown,
    handle_quick_register_callback,
    main_week_loss_pause_until,
    parse_quick_command,
)
from jarvise_assist.quick_tags import MSG_QUICK_OPEN, quick_symbols
from jarvise_assist.store import (
    ensure_settings,
    get_edge,
    insert_exec_intent,
    upsert_watch_position,
    write_edge,
    write_settings,
)
from jarvise_ingest.db import open_db

NOW = _ms_bangkok(2026, 11, 14, 15, 0)


class QuickFake(FakeFutures):
    ALLOWED_READS = {
        "exchange_symbol",
        "position_risk",
        "all_open_orders",
        "open_orders",
        "open_algo_orders",
        "mark_price",
        "symbol_filters",
        "usdt_wallet_and_available",
        "symbol_config",
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.calls: list[str] = []
        self._exchange_rows: dict[str, dict[str, Any] | None] = {
            "SUIUSDT": {
                "symbol": "SUIUSDT",
                "contractType": "PERPETUAL",
                "quoteAsset": "USDT",
                "status": "TRADING",
            },
            "BTCUSDT": {
                "symbol": "BTCUSDT",
                "contractType": "PERPETUAL",
                "quoteAsset": "USDT",
                "status": "TRADING",
            },
            "ETHUSDT": {
                "symbol": "ETHUSDT",
                "contractType": "PERPETUAL",
                "quoteAsset": "USDT",
                "status": "TRADING",
            },
            "SETTLINGUSDT": {
                "symbol": "SETTLINGUSDT",
                "contractType": "PERPETUAL",
                "quoteAsset": "USDT",
                "status": "SETTLING",
            },
        }
        self._on_balance: Any = None

    def _record(self, name: str) -> None:
        self.calls.append(name)

    def exchange_symbol(self, symbol: str) -> dict[str, Any] | None:
        self._record("exchange_symbol")
        return self._exchange_rows.get(str(symbol).upper())

    def position_risk(self, *, symbol: str | None = None) -> list[FuturesPosition]:
        self._record("position_risk")
        return super().position_risk(symbol=symbol)

    def all_open_orders(self, *, symbol: str | None = None) -> list[dict[str, Any]]:
        self._record("all_open_orders")
        return super().all_open_orders(symbol=symbol)

    def open_orders(self, *, symbol: str | None = None) -> list[dict[str, Any]]:
        self._record("open_orders")
        return super().open_orders(symbol=symbol)

    def open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, Any]]:
        self._record("open_algo_orders")
        return super().open_algo_orders(symbol=symbol)

    def mark_price(self, symbol: str) -> Decimal:
        self._record("mark_price")
        return super().mark_price(symbol)

    def symbol_filters(self, symbol: str) -> Any:
        self._record("symbol_filters")
        return super().symbol_filters(symbol)

    def usdt_wallet_and_available(self) -> tuple[Decimal, Decimal]:
        self._record("usdt_wallet_and_available")
        if self._on_balance is not None:
            self._on_balance()
        return super().usdt_wallet_and_available()

    def symbol_config(self) -> dict[str, Any]:
        self._record("symbol_config")
        return super().symbol_config()


def _happy_fake(**kwargs: Any) -> QuickFake:
    marks = kwargs.pop("marks", {"SUIUSDT": Decimal("2.0000"), "BTCUSDT": Decimal("100000")})
    ticks = kwargs.pop("ticks", {"SUIUSDT": Decimal("0.0001"), "BTCUSDT": Decimal("0.1")})
    steps = kwargs.pop("steps", {"SUIUSDT": Decimal("0.1"), "BTCUSDT": Decimal("0.001")})
    return QuickFake(
        balance=kwargs.pop("balance", Decimal("1000")),
        available=kwargs.pop("available", None),
        marks=marks,
        ticks=ticks,
        steps=steps,
        min_qty=kwargs.pop("min_qty", {"SUIUSDT": Decimal("0.1"), "BTCUSDT": Decimal("0.001")}),
        min_notional=kwargs.pop("min_notional", {"SUIUSDT": Decimal("5"), "BTCUSDT": Decimal("5")}),
        **kwargs,
    )


def _msg(text: str) -> dict[str, Any]:
    return {"message": {"chat": {"id": 42}, "text": text}}


def _cq(data: str) -> dict[str, Any]:
    return {
        "callback_query": {
            "id": f"cq-{data}",
            "data": data,
            "message": {"chat": {"id": 42}, "message_id": 1},
        }
    }


def _seed_settings(conn: Any) -> None:
    ensure_settings(conn)
    get_edge(conn)
    get_quick_settings(conn)


# --- parse / fee ------------------------------------------------------------


def test_parse_quick_command() -> None:
    assert parse_quick_command("q btc long 67000") == {
        "symbol": "BTCUSDT",
        "side": "long",
        "stop": Decimal("67000"),
    }
    assert parse_quick_command("Q eth s 0.4%") == {
        "symbol": "ETHUSDT",
        "side": "short",
        "stop_pct": Decimal("0.4"),
    }
    assert parse_quick_command("quick SUIUSDT buy 1.994") == {
        "symbol": "SUIUSDT",
        "side": "long",
        "stop": Decimal("1.994"),
    }
    assert parse_quick_command("q BTC/USDT long 67,000") == {
        "symbol": "BTCUSDT",
        "side": "long",
        "stop": Decimal("67000"),
    }
    for bad in (
        "q btc",
        "q btc up 1",
        "q btc long abc",
        "q btc long 0",
        "q btc long 25%",
        "q btc long 1 2",
    ):
        assert parse_quick_command(bad) == {"error": "usage"}
    for none in (
        "hello",
        "q",
        "quick",
        "qtum btc",
        BTN_QUICK,
        BTN_POSITIONS,
        BTN_ANYTHING,
        BTN_WEEKLY,
        BTN_TOGGLE,
    ):
        assert parse_quick_command(none) is None


def test_fee_breakdown_pinned() -> None:
    # stop 0.30% → f 0.40
    e = Decimal("100")
    s = Decimal("99.7")
    d = fee_breakdown(e, s)
    assert d["f"] == Decimal("0.4")
    assert d["net_1r"] == Decimal("0.6")
    assert d["net_1_5r"] == Decimal("1.1")
    assert d["loss_r"] == Decimal("-1.4")
    assert d["be_1r_pct"] == 70
    assert d["be_1_5r_pct"] == 56
    assert d["warn"] is True
    # 0.60%
    d2 = fee_breakdown(Decimal("100"), Decimal("99.4"))
    assert d2["f"] == Decimal("0.2")
    assert d2["be_1r_pct"] == 60
    assert d2["be_1_5r_pct"] == 48
    assert d2["warn"] is False
    # 1.00%
    d3 = fee_breakdown(Decimal("100"), Decimal("99"))
    assert d3["f"] == Decimal("0.12")
    assert d3["be_1r_pct"] == 56
    assert d3["be_1_5r_pct"] == 45
    assert d3["warn"] is False


# --- happy path -------------------------------------------------------------


def test_card_happy_path_sui(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "happy.db")
    _seed_settings(conn)
    before_s = _table_rows(conn, "assist_settings")
    before_e = _table_rows(conn, "assist_edge")
    before_sig = _table_rows(conn, "assist_signal")
    fake = _happy_fake()
    card = build_quick_card(
        conn,
        fake,
        symbol="SUIUSDT",
        side="long",
        stop=Decimal("1.994"),
        now_ms=NOW,
    )
    assert card["ok"] is True
    assert card["qty"] == Decimal("833.3")
    assert abs(float(card["entry"]) - 2.0) < 1e-9
    assert float(card["stop"]) == pytest.approx(1.994)
    assert int(card["leverage"]) == 2
    text = card["text"]
    assert "833.3" in text
    assert "1666.60" in text or "1666.6" in text
    assert "0.40" in text or "0.4" in text
    assert "2.0060" in text
    assert "2.0090" in text
    assert "+0.6" in text or "+0.60" in text
    assert "70%" in text
    assert "56%" in text
    assert "⚠" in text  # fee warning
    cb = f"qr:SUIUSDT:l:1.9940:{NOW // 1000}"
    assert card["reply_markup"] == {"inline_keyboard": [[{"text": "⚡ เริ่มไม้ quick", "callback_data": cb}]]}
    assert len(cb.encode("utf-8")) <= 64
    assert conn.execute("SELECT COUNT(*) FROM assist_quick_registration").fetchone()[0] == 0
    assert _table_rows(conn, "assist_settings") == before_s
    assert _table_rows(conn, "assist_edge") == before_e
    assert _table_rows(conn, "assist_signal") == before_sig
    assert set(fake.calls) <= QuickFake.ALLOWED_READS
    conn.close()


def test_card_happy_path_btc_pct(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / "btc.db")
    _seed_settings(conn)
    fake = _happy_fake(marks={"BTCUSDT": Decimal("100000")}, ticks={"BTCUSDT": Decimal("0.1")})
    card = build_quick_card(
        conn,
        fake,
        symbol="BTCUSDT",
        side="short",
        stop_pct=Decimal("0.5"),
        now_ms=NOW,
    )
    assert card["ok"] is True
    assert card["side"] == "short"
    assert card["reply_markup"] is not None
    conn.close()


def test_text_process_update_card(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "txt.db")
    _seed_settings(conn)
    fake = _happy_fake()
    sent, send = _send_capture()
    direct = build_quick_card(conn, fake, symbol="SUIUSDT", side="long", stop=Decimal("1.994"), now_ms=NOW)
    fake2 = _happy_fake()
    out = process_update(
        conn,
        _msg("q sui long 1.994"),
        send=send,
        futures=fake2,
        now_ms_value=NOW,
    )
    assert out["handled"] is True
    assert out["kind"] == "quick_card"
    assert len(sent) == 1
    assert sent[0]["text"] == direct["text"]
    assert sent[0]["reply_markup"] == direct["reply_markup"]
    conn.close()


def test_text_beats_awaiting_explore(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "expl.db")
    _seed_settings(conn)
    write_settings(conn, awaiting_explore=True)
    before = _table_rows(conn, "assist_settings")
    sent, send = _send_capture()
    process_update(
        conn,
        _msg("q sui long 1.994"),
        send=send,
        futures=_happy_fake(),
        now_ms_value=NOW,
    )
    assert len(sent) == 1
    assert "Quick · SUIUSDT" in sent[0]["text"]
    assert _table_rows(conn, "assist_settings") == before
    conn.close()


def test_usage_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "usage.db")
    sent, send = _send_capture()
    process_update(
        conn,
        _msg("q sui"),
        send=send,
        futures=_happy_fake(),
        now_ms_value=NOW,
    )
    assert len(sent) == 1
    assert sent[0]["text"] == QUICK_USAGE
    conn.close()


def test_menu_contains_usage(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "menu.db")
    text, _kb = render_quick_menu(conn, now_ms=NOW)
    assert QUICK_USAGE in text
    assert text.strip().endswith(QUICK_USAGE)
    conn.close()


def test_execute_mode_and_enabled_irrelevant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / "mode.db")
    _seed_settings(conn)
    write_settings(conn, enabled=False)
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "off")
    a = build_quick_card(
        conn, _happy_fake(), symbol="SUIUSDT", side="long", stop=Decimal("1.994"), now_ms=NOW
    )
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    write_settings(conn, enabled=True)
    b = build_quick_card(
        conn, _happy_fake(), symbol="SUIUSDT", side="long", stop=Decimal("1.994"), now_ms=NOW
    )
    assert a["ok"] and b["ok"]
    assert a["text"] == b["text"]
    assert a["reply_markup"] == b["reply_markup"]
    conn.close()


# --- refusals ---------------------------------------------------------------


def _assert_refusal(
    conn: Any,
    fake: QuickFake | None,
    *,
    reason: str,
    monkeypatch: pytest.MonkeyPatch,
    text_has: str | None = None,
) -> None:
    _forbid_trade_client(monkeypatch)
    open_before = conn.execute(
        "SELECT COUNT(*) FROM assist_quick_registration WHERE status='open'"
    ).fetchone()[0]
    sent, send = _send_capture()
    card = build_quick_card(
        conn,
        fake,
        symbol="SUIUSDT",
        side="long",
        stop=Decimal("1.994"),
        now_ms=NOW,
    )
    send(card["text"], reply_markup=card.get("reply_markup"))
    assert card["ok"] is False
    assert card["reason"] == reason
    assert len(sent) == 1
    assert sent[0]["reply_markup"] is None
    open_after = conn.execute(
        "SELECT COUNT(*) FROM assist_quick_registration WHERE status='open'"
    ).fetchone()[0]
    assert open_after == open_before
    if text_has:
        assert text_has in sent[0]["text"]


def test_refusal_locked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = open_db(tmp_path / "lock.db")
    _seed_settings(conn)
    write_quick_settings(conn, locked_until_ms=NOW + 3_600_000, lock_reason="streak")
    _assert_refusal(conn, _happy_fake(), reason="locked", monkeypatch=monkeypatch)
    conn.close()


def test_refusal_week_loss_pause(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / "wlp.db")
    _seed_settings(conn)
    until = next_0820_bangkok_ms(NOW)
    write_settings(conn, paused_until_ms=until)
    write_edge(conn, week_pause_id=bangkok_week_id(NOW))
    before_s = _table_rows(conn, "assist_settings")
    before_e = _table_rows(conn, "assist_edge")
    fake = _happy_fake()
    card = build_quick_card(conn, fake, symbol="SUIUSDT", side="long", stop=Decimal("1.994"), now_ms=NOW)
    assert card["ok"] is False
    assert card["reason"] == "week_loss_pause"
    assert fake.calls == []
    assert _table_rows(conn, "assist_settings") == before_s
    assert _table_rows(conn, "assist_edge") == before_e
    assert "4R" in card["text"]
    assert format_paused_until(until) in card["text"]
    conn.close()


def test_refusal_quick_open_trade_and_reg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = open_db(tmp_path / "qo.db")
    _seed_settings(conn)
    _insert_quick_trade(conn, symbol="SUIUSDT", now_ms=NOW)
    _assert_refusal(conn, _happy_fake(), reason="quick_open", monkeypatch=monkeypatch)
    conn.close()

    conn = open_db(tmp_path / "qo2.db")
    _seed_settings(conn)
    _insert_quick_registration(conn, symbol="SUIUSDT", now_ms=NOW)
    _assert_refusal(conn, _happy_fake(), reason="quick_open", monkeypatch=monkeypatch)
    conn.close()


def test_refusal_main_position_variants(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # (i) live same side, no watch
    conn = open_db(tmp_path / "mp1.db")
    _seed_settings(conn)
    pos = FuturesPosition(
        symbol="SUIUSDT",
        position_side="BOTH",
        position_amt=Decimal("10"),
        entry_price=Decimal("2"),
        mark_price=Decimal("2"),
        update_time_ms=NOW - 120_000,
    )
    _assert_refusal(conn, _happy_fake(positions=[pos]), reason="main_position", monkeypatch=monkeypatch)
    conn.close()

    # (ii) opposite side
    conn = open_db(tmp_path / "mp2.db")
    _seed_settings(conn)
    pos2 = FuturesPosition(
        symbol="SUIUSDT",
        position_side="BOTH",
        position_amt=Decimal("-10"),
        entry_price=Decimal("2"),
        mark_price=Decimal("2"),
        update_time_ms=NOW - 120_000,
    )
    _assert_refusal(conn, _happy_fake(positions=[pos2]), reason="main_position", monkeypatch=monkeypatch)
    conn.close()

    # (iii) watch row, no live
    conn = open_db(tmp_path / "mp3.db")
    _seed_settings(conn)
    upsert_watch_position(
        conn,
        {
            "symbol": "SUIUSDT",
            "position_side": "LONG",
            "side": "long",
            "entry": 2.0,
            "qty": 1.0,
            "in_universe": 0,
            "signal_id": None,
            "initial_stop": None,
            "initial_stop_source": None,
            "entry_time_ms": NOW,
            "no_stop_latched": 0,
            "last_seen_ms": NOW,
            "closed_at_ms": None,
        },
    )
    _assert_refusal(conn, _happy_fake(), reason="main_position", monkeypatch=monkeypatch)
    conn.close()


def test_refusal_in_flight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = open_db(tmp_path / "if.db")
    _seed_settings(conn)
    iid = insert_exec_intent(
        conn,
        kind="entry",
        symbol="SUIUSDT",
        side="long",
        preview={"entry": "2"},
        created_at_ms=NOW,
        expires_at_ms=NOW + 600_000,
    )
    conn.execute(
        "UPDATE assist_exec_intent SET status='confirmed' WHERE id=?",
        (iid,),
    )
    conn.commit()
    _assert_refusal(conn, _happy_fake(), reason="in_flight", monkeypatch=monkeypatch)
    conn.close()


def test_refusal_pending_order_and_leftover_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / "po.db")
    _seed_settings(conn)
    limit = {
        "symbol": "SUIUSDT",
        "side": "BUY",
        "type": "LIMIT",
        "reduceOnly": False,
        "price": "1.9",
        "origQty": "10",
    }
    _assert_refusal(conn, _happy_fake(orders=[limit]), reason="pending_order", monkeypatch=monkeypatch)
    conn.close()

    # protective only → OK + warning
    conn = open_db(tmp_path / "po2.db")
    _seed_settings(conn)
    stop = _stop_order("SUIUSDT", stop="1.9", qty="10")
    algo = {
        "symbol": "SUIUSDT",
        "side": "SELL",
        "type": "STOP_MARKET",
        "closePosition": True,
        "stopPrice": "1.9",
    }
    card = build_quick_card(
        conn,
        _happy_fake(orders=[stop], algo_orders=[algo]),
        symbol="SUIUSDT",
        side="long",
        stop=Decimal("1.994"),
        now_ms=NOW,
    )
    assert card["ok"] is True
    assert "คำสั่งป้องกันค้าง" in card["text"]
    conn.close()


def test_refusal_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = open_db(tmp_path / "ct.db")
    _seed_settings(conn)
    fake = _happy_fake()
    fake._exchange_rows.pop("SUIUSDT")
    _assert_refusal(conn, fake, reason="contract", monkeypatch=monkeypatch)

    fake3 = _happy_fake(
        marks={"SETTLINGUSDT": Decimal("2")},
        ticks={"SETTLINGUSDT": Decimal("0.0001")},
        steps={"SETTLINGUSDT": Decimal("0.1")},
    )
    card3 = build_quick_card(
        conn, fake3, symbol="SETTLINGUSDT", side="long", stop=Decimal("1.994"), now_ms=NOW
    )
    assert card3["reason"] == "contract"
    conn.close()


def test_refusal_read_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = open_db(tmp_path / "rf.db")
    _seed_settings(conn)
    fake = _happy_fake(read_error=RuntimeError("boom"))
    # position_risk raises via read_error
    _assert_refusal(conn, fake, reason="read_failed", monkeypatch=monkeypatch)
    _assert_refusal(conn, None, reason="read_failed", monkeypatch=monkeypatch)
    conn.close()


def test_refusal_stop_wrong_side(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = open_db(tmp_path / "sw.db")
    _seed_settings(conn)
    _forbid_trade_client(monkeypatch)
    card = build_quick_card(
        conn, _happy_fake(), symbol="SUIUSDT", side="long", stop=Decimal("2.1"), now_ms=NOW
    )
    assert card["reason"] == "stop_wrong_side"
    conn.close()


def test_refusal_below_min_notional(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = open_db(tmp_path / "bm.db")
    _seed_settings(conn)
    fake = _happy_fake(min_notional={"SUIUSDT": Decimal("100000")})
    _assert_refusal(conn, fake, reason="below_min_notional", monkeypatch=monkeypatch)
    conn.close()


def test_refusal_quick_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = open_db(tmp_path / "qc.db")
    _seed_settings(conn)
    # open quick on another symbol risk 6; new ~5; cap 1% of 1000 = 10
    _insert_quick_trade(
        conn,
        symbol="ETHUSDT",
        now_ms=NOW,
        entry_qty=1.0,
        entry_avg=100.0,
        planned_stop=94.0,  # risk = 6
    )
    _assert_refusal(conn, _happy_fake(), reason="quick_cap", monkeypatch=monkeypatch)
    conn.close()


def test_refusal_portfolio_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = open_db(tmp_path / "pc.db")
    _seed_settings(conn)
    # BTC main position risk 36 via stop order
    btc = _pos("BTCUSDT", amt="0.36", entry="100", mark="100")
    stop = _stop_order("BTCUSDT", stop="0", qty="0.36")  # bad — use 90
    stop = _stop_order("BTCUSDT", stop="90", qty="0.36")  # risk = 0.36*10=3.6 — too small
    # need risk 36: qty * |entry-stop| = 36 → amt=3.6, stop=90, entry=100
    btc = _pos("BTCUSDT", amt="3.6", entry="100", mark="100")
    stop = _stop_order("BTCUSDT", stop="90", qty="3.6")
    fake = _happy_fake(
        positions=[btc],
        orders=[stop],
        marks={"SUIUSDT": Decimal("2.0000"), "BTCUSDT": Decimal("100")},
        ticks={"SUIUSDT": Decimal("0.0001"), "BTCUSDT": Decimal("0.1")},
        steps={"SUIUSDT": Decimal("0.1"), "BTCUSDT": Decimal("0.001")},
    )
    _assert_refusal(conn, fake, reason="portfolio_cap", monkeypatch=monkeypatch)
    conn.close()


def test_refusal_margin_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = open_db(tmp_path / "mc.db")
    _seed_settings(conn)
    # Tiny available → assign_shared_leverage shrinks qty to 0 and drops.
    fake = _happy_fake(available=Decimal("0.01"))
    _assert_refusal(conn, fake, reason="margin_cap", monkeypatch=monkeypatch)
    conn.close()


def test_refusal_margin_cap_available_300(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """1a: available 300 + stop 1.994 must refuse shrink (not emit a card)."""
    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / "mc300.db")
    _seed_settings(conn)
    fake = _happy_fake(available=Decimal("300"))
    _assert_refusal(
        conn,
        fake,
        reason="margin_cap",
        monkeypatch=monkeypatch,
        text_has=REFUSAL["margin_cap"],
    )
    assert conn.execute("SELECT COUNT(*) FROM assist_quick_registration").fetchone()[0] == 0
    conn.close()


def test_refusal_leverage_cap_stop_19984(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """1b: stop 1.9984 needs >5x on risk size — refuse, do not shrink qty."""
    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / "lc.db")
    _seed_settings(conn)
    fake = _happy_fake()
    card = build_quick_card(
        conn,
        fake,
        symbol="SUIUSDT",
        side="long",
        stop=Decimal("1.9984"),
        now_ms=NOW,
    )
    assert card["ok"] is False
    assert card["reason"] == "leverage_cap"
    assert card["text"] == REFUSAL["leverage_cap"]
    assert conn.execute("SELECT COUNT(*) FROM assist_quick_registration").fetchone()[0] == 0
    conn.close()


def test_card_ok_available_340_lone_survivor_5x(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """1c: available 340 fits 5x with no qty cut — card OK."""
    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / "ok340.db")
    _seed_settings(conn)
    card = build_quick_card(
        conn,
        _happy_fake(available=Decimal("340")),
        symbol="SUIUSDT",
        side="long",
        stop=Decimal("1.994"),
        now_ms=NOW,
    )
    assert card["ok"] is True
    assert card["qty"] == Decimal("833.3")
    assert int(card["leverage"]) == 5
    conn.close()


def test_qr_tap_refuses_when_available_drops_to_300(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """1d: card at full available; tap after available=300 → margin_cap, no row."""
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "tap_mc.db")
    _seed_settings(conn)
    card = build_quick_card(
        conn, _happy_fake(), symbol="SUIUSDT", side="long", stop=Decimal("1.994"), now_ms=NOW
    )
    assert card["ok"] is True
    cb = card["callback_data"]
    sent, send = _send_capture()
    answers, answer = _answers()
    process_update(
        conn,
        _cq(cb),
        send=send,
        answer=answer,
        futures=_happy_fake(available=Decimal("300")),
        now_ms_value=NOW + 30_000,
    )
    assert len(answers) == 1
    assert len(sent) == 1
    assert sent[0]["text"] == REFUSAL["margin_cap"]
    assert conn.execute("SELECT COUNT(*) FROM assist_quick_registration").fetchone()[0] == 0
    conn.close()


def test_happy_sui_fee_and_target_lines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """1e: exact fee / target lines on happy SUI stop 1.994."""
    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / "lines_e.db")
    _seed_settings(conn)
    card = build_quick_card(
        conn, _happy_fake(), symbol="SUIUSDT", side="long", stop=Decimal("1.994"), now_ms=NOW
    )
    assert card["ok"] is True
    text = card["text"]
    assert "ถ้าชน stop: −7 USDT ≈ −1.40R รวมค่าธรรมเนียม (ราคา −5 USDT = 1R · ค่าธรรมเนียม ≈ 2 USDT)" in text
    assert "ค่าธรรมเนียมไป-กลับ ≈ 0.40R ≈ 2 USDT (โมเดล 6 bps/ข้าง — ยังไม่อ่านค่าจริงจากบัญชี)" in text
    assert "เป้า 1R: 2.0060 → สุทธิ +0.60R · ต้องชนะ ≥ 70% ถึงคุ้มทุน" in text
    assert "เป้า 1.5R: 2.0090 → สุทธิ +1.10R · ต้องชนะ ≥ 56% ถึงคุ้มทุน" in text
    conn.close()


def test_sui_stop_1993_fee_and_target_lines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """1f: stop 1.993 — rounded R / USDT lines, no long Decimal tails."""
    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / "lines_f.db")
    _seed_settings(conn)
    card = build_quick_card(
        conn, _happy_fake(), symbol="SUIUSDT", side="long", stop=Decimal("1.993"), now_ms=NOW
    )
    assert card["ok"] is True
    text = card["text"]
    assert "ถ้าชน stop: −6.71 USDT ≈ −1.34R รวมค่าธรรมเนียม (ราคา −5 USDT = 1R · ค่าธรรมเนียม ≈ 1.71 USDT)" in text
    assert "ค่าธรรมเนียมไป-กลับ ≈ 0.34R ≈ 1.71 USDT (โมเดล 6 bps/ข้าง — ยังไม่อ่านค่าจริงจากบัญชี)" in text
    assert "เป้า 1R: 2.0070 → สุทธิ +0.66R · ต้องชนะ ≥ 68% ถึงคุ้มทุน" in text
    assert "เป้า 1.5R: 2.0105 → สุทธิ +1.16R · ต้องชนะ ≥ 54% ถึงคุ้มทุน" in text
    assert "0.3428571428571428571428571429R" not in text
    conn.close()


def test_fee_ge_k_no_profit_tail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """1g: risk 0.25% + stop 1.9984 → f≥k tails say no profit even if every win."""
    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / "lines_g.db")
    _seed_settings(conn)
    write_quick_settings(conn, risk_pct=0.25)
    card = build_quick_card(
        conn, _happy_fake(), symbol="SUIUSDT", side="long", stop=Decimal("1.9984"), now_ms=NOW
    )
    assert card["ok"] is True
    text = card["text"]
    assert "⚠ ค่าธรรมเนียมสูงเทียบระยะ stop — reward < 3× ค่าไป-กลับ" in text
    assert (
        "ถ้าชน stop: −6.25 USDT ≈ −2.50R รวมค่าธรรมเนียม (ราคา −2.5 USDT = 1R · ค่าธรรมเนียม ≈ 3.75 USDT)" in text
    )
    assert "เป้า 1R: 2.0016 → สุทธิ −0.50R · ชนะทุกไม้ก็ไม่มีกำไร" in text
    assert "เป้า 1.5R: 2.0024 → สุทธิ 0.00R · ชนะทุกไม้ก็ไม่มีกำไร" in text
    assert "+-0.5" not in text
    conn.close()


def test_parse_rejects_nan_inf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """1h: nan/inf stop tokens → usage; process_update sends QUICK_USAGE once."""
    for bad in (
        "q btc long nan",
        "q btc long snan",
        "q btc long inf",
        "q btc long -inf",
        "q btc short nan%",
        "q btc short inf%",
    ):
        assert parse_quick_command(bad) == {"error": "usage"}

    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "nan.db")
    _seed_settings(conn)
    for bad in (
        "q btc long nan",
        "q btc long snan",
        "q btc long inf",
        "q btc long -inf",
        "q btc short nan%",
        "q btc short inf%",
    ):
        sent, send = _send_capture()
        out = process_update(
            conn,
            _msg(bad),
            send=send,
            futures=_happy_fake(),
            now_ms_value=NOW,
        )
        assert out["handled"] is True
        assert len(sent) == 1
        assert sent[0]["text"] == QUICK_USAGE
    conn.close()


def test_qr_bad_stop_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """1i: forged qr stop NaN/Infinity/0/-1 → bad_data; one answer+send; no rows."""
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "badstop.db")
    _seed_settings(conn)
    created = NOW // 1000
    for stop_field in ("NaN", "Infinity", "0", "-1"):
        sent, send = _send_capture()
        answers, answer = _answers()
        cb = f"qr:SUIUSDT:l:{stop_field}:{created}"
        out = process_update(
            conn,
            _cq(cb),
            send=send,
            answer=answer,
            futures=_happy_fake(),
            now_ms_value=NOW,
        )
        assert len(answers) == 1
        assert len(sent) == 1
        assert sent[0]["text"] == REFUSAL["bad_data"]
        assert out.get("reason") == "bad_data"
        assert conn.execute("SELECT COUNT(*) FROM assist_quick_registration").fetchone()[0] == 0
    conn.close()


def test_parse_rejects_huge_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Leftover A: 1e999999 → usage / bad_data; no Overflow; one send each."""
    for text in ("q btc long 1e999999", "q btc short 1e999999"):
        assert parse_quick_command(text) == {"error": "usage"}

    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "huge.db")
    _seed_settings(conn)
    for text in ("q btc long 1e999999", "q btc short 1e999999"):
        sent, send = _send_capture()
        out = process_update(
            conn,
            _msg(text),
            send=send,
            futures=_happy_fake(),
            now_ms_value=NOW,
        )
        assert out["handled"] is True
        assert len(sent) == 1
        assert sent[0]["text"] == QUICK_USAGE

    sent_qr, send_qr = _send_capture()
    answers, answer = _answers()
    cb = f"qr:SUIUSDT:l:1e999999:{NOW // 1000}"
    out_qr = process_update(
        conn,
        _cq(cb),
        send=send_qr,
        answer=answer,
        futures=_happy_fake(),
        now_ms_value=NOW,
    )
    assert len(answers) == 1
    assert len(sent_qr) == 1
    assert sent_qr[0]["text"] == REFUSAL["bad_data"]
    assert out_qr.get("reason") == "bad_data"
    assert conn.execute("SELECT COUNT(*) FROM assist_quick_registration").fetchone()[0] == 0
    conn.close()


def test_fmt_r_signed_no_ascii_neg_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Leftover B: tiny negative net R displays 0.00R, not ASCII -0.00."""
    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / "negzero.db")
    _seed_settings(conn)
    card = build_quick_card(
        conn,
        _happy_fake(marks={"SUIUSDT": Decimal("2.0003")}),
        symbol="SUIUSDT",
        side="long",
        stop=Decimal("1.9979"),
        now_ms=NOW,
    )
    assert card["ok"] is True
    text = card["text"]
    assert "เป้า 1R: 2.0027 → สุทธิ 0.00R · ชนะทุกไม้ก็ไม่มีกำไร" in text
    assert "-0.00" not in text
    conn.close()


def test_stop_floors_to_zero_wrong_side(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Leftover C: long stop that rounds to 0 at tick → stop_wrong_side, no callback."""
    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / "zerostop.db")
    _seed_settings(conn)
    card = build_quick_card(
        conn,
        _happy_fake(),
        symbol="SUIUSDT",
        side="long",
        stop=Decimal("0.00001"),
        now_ms=NOW,
    )
    assert card["ok"] is False
    assert card["reason"] == "stop_wrong_side"
    assert card.get("callback_data") is None
    conn.close()


# --- qr: register -----------------------------------------------------------


def test_qr_register_happy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "reg.db")
    _seed_settings(conn)
    t0 = NOW
    fake = _happy_fake()
    card = build_quick_card(conn, fake, symbol="SUIUSDT", side="long", stop=Decimal("1.994"), now_ms=t0)
    assert card["ok"]
    cb = card["callback_data"]
    assert cb and cb.startswith(QUICK_REGISTER_PREFIX)

    # change mark
    fake2 = _happy_fake(marks={"SUIUSDT": Decimal("2.0100")})
    sent, send = _send_capture()
    answers, answer = _answers()
    tap_ms = t0 + 30_000
    out = process_update(
        conn,
        _cq(cb),
        send=send,
        answer=answer,
        futures=fake2,
        now_ms_value=tap_ms,
    )
    assert len(answers) == 1
    assert len(sent) == 1
    assert out.get("ok") is True
    rows = conn.execute("SELECT * FROM assist_quick_registration").fetchall()
    assert len(rows) == 1
    row = dict(rows[0])
    assert row["symbol"] == "SUIUSDT"
    assert row["side"] == "long"
    assert float(row["planned_entry"]) == pytest.approx(2.01)
    assert float(row["planned_stop"]) == pytest.approx(1.994)
    assert row["created_at_ms"] == tap_ms
    assert row["expires_at_ms"] == tap_ms + 600_000
    assert row["status"] == "open"
    assert quick_symbols(conn) == frozenset({"SUIUSDT"})
    assert set(fake2.calls) <= QuickFake.ALLOWED_READS
    conn.close()


def test_qr_refuses_when_position_appears(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    for amt in (Decimal("10"), Decimal("-10")):
        conn = open_db(tmp_path / f"race_{amt}.db")
        _seed_settings(conn)
        card = build_quick_card(
            conn, _happy_fake(), symbol="SUIUSDT", side="long", stop=Decimal("1.994"), now_ms=NOW
        )
        cb = card["callback_data"]
        pos = FuturesPosition(
            symbol="SUIUSDT",
            position_side="BOTH",
            position_amt=amt,
            entry_price=Decimal("2"),
            mark_price=Decimal("2"),
            update_time_ms=NOW + 20_000,
        )
        sent, send = _send_capture()
        answers, answer = _answers()
        process_update(
            conn,
            _cq(cb),
            send=send,
            answer=answer,
            futures=_happy_fake(positions=[pos]),
            now_ms_value=NOW + 30_000,
        )
        assert len(answers) == 1
        assert len(sent) == 1
        assert "position" in sent[0]["text"].lower() or "ถือ" in sent[0]["text"]
        assert conn.execute("SELECT COUNT(*) FROM assist_quick_registration").fetchone()[0] == 0
        conn.close()


def test_qr_refuses_pending_and_in_flight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "pend.db")
    _seed_settings(conn)
    card = build_quick_card(
        conn, _happy_fake(), symbol="SUIUSDT", side="long", stop=Decimal("1.994"), now_ms=NOW
    )
    cb = card["callback_data"]
    limit = {
        "symbol": "SUIUSDT",
        "side": "BUY",
        "type": "LIMIT",
        "reduceOnly": False,
        "price": "1.9",
        "origQty": "10",
    }
    sent, send = _send_capture()
    answers, answer = _answers()
    process_update(
        conn,
        _cq(cb),
        send=send,
        answer=answer,
        futures=_happy_fake(orders=[limit]),
        now_ms_value=NOW + 30_000,
    )
    assert conn.execute("SELECT COUNT(*) FROM assist_quick_registration").fetchone()[0] == 0
    conn.close()

    conn = open_db(tmp_path / "infl.db")
    _seed_settings(conn)
    card = build_quick_card(
        conn, _happy_fake(), symbol="SUIUSDT", side="long", stop=Decimal("1.994"), now_ms=NOW
    )
    cb = card["callback_data"]
    iid = insert_exec_intent(
        conn,
        kind="entry",
        symbol="SUIUSDT",
        side="long",
        preview={},
        created_at_ms=NOW,
        expires_at_ms=NOW + 600_000,
    )
    conn.execute("UPDATE assist_exec_intent SET status='confirmed' WHERE id=?", (iid,))
    conn.commit()
    sent, send = _send_capture()
    answers, answer = _answers()
    process_update(
        conn,
        _cq(cb),
        send=send,
        answer=answer,
        futures=_happy_fake(),
        now_ms_value=NOW + 30_000,
    )
    assert conn.execute("SELECT COUNT(*) FROM assist_quick_registration").fetchone()[0] == 0
    conn.close()


def test_qr_second_tap_and_expired_and_bad(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "2nd.db")
    _seed_settings(conn)
    card = build_quick_card(
        conn, _happy_fake(), symbol="SUIUSDT", side="long", stop=Decimal("1.994"), now_ms=NOW
    )
    cb = card["callback_data"]
    sent, send = _send_capture()
    answers, answer = _answers()
    process_update(conn, _cq(cb), send=send, answer=answer, futures=_happy_fake(), now_ms_value=NOW + 30_000)
    assert conn.execute("SELECT COUNT(*) FROM assist_quick_registration").fetchone()[0] == 1
    process_update(conn, _cq(cb), send=send, answer=answer, futures=_happy_fake(), now_ms_value=NOW + 40_000)
    assert "quick" in sent[-1]["text"].lower() or "ลงทะเบียน" in sent[-1]["text"]
    assert conn.execute("SELECT COUNT(*) FROM assist_quick_registration").fetchone()[0] == 1

    # expired
    sent2, send2 = _send_capture()
    answers2, answer2 = _answers()
    out = process_update(
        conn,
        _cq(cb),
        send=send2,
        answer=answer2,
        futures=_happy_fake(),
        now_ms_value=NOW + 121_000,
    )
    assert out.get("reason") == "card_expired" or "หมดอายุ" in sent2[0]["text"]

    out2 = handle_quick_register_callback(conn, "qr:bad", futures=_happy_fake(), send=send2, now_ms=NOW)
    assert out2["reason"] == "bad_data"
    conn.close()


def test_ec_blocked_after_reg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / "ec.db")
    _seed_settings(conn)
    write_settings(conn, enabled=True)
    create_quick_registration(
        conn,
        symbol="BTCUSDT",
        side="long",
        planned_entry=100.0,
        planned_stop=90.0,
        qty=0.01,
        now_ms=NOW,
        live_positions=[],
    )
    sent, send = _send_capture()
    answers, answer = _answers()
    process_update(
        conn,
        _cq("ec:BTCUSDT:l"),
        send=send,
        answer=answer,
        futures=_happy_fake(),
        now_ms_value=NOW,
    )
    assert any(MSG_QUICK_OPEN in s["text"] for s in sent)
    conn.close()


def test_create_live_positions_kwarg(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lp.db")
    pos = _pos("SUIUSDT", amt="1", entry="2", mark="2")
    bad, why = create_quick_registration(
        conn,
        symbol="SUIUSDT",
        side="long",
        planned_entry=2.0,
        planned_stop=1.994,
        qty=1.0,
        now_ms=NOW,
        live_positions=[pos],
    )
    assert bad is None and why == "live_position"
    ok, why2 = create_quick_registration(
        conn,
        symbol="SUIUSDT",
        side="long",
        planned_entry=2.0,
        planned_stop=1.994,
        qty=1.0,
        now_ms=NOW,
        live_positions=[],
    )
    assert ok is not None and why2 is None
    conn.close()


# --- weekly 4R --------------------------------------------------------------


def test_week_loss_blocks_card_e2e(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "w4.db")
    _eval(conn, risk=1.0, net=-50, now_ms=TUE)
    fake = _happy_fake()
    sent, send = _send_capture()
    process_update(
        conn,
        _msg("q sui long 1.994"),
        send=send,
        futures=fake,
        now_ms_value=TUE + 60_000,
    )
    assert len(sent) == 1
    assert "4R" in sent[0]["text"]
    until = next_0820_bangkok_ms(TUE)
    assert format_paused_until(until) in sent[0]["text"]
    assert conn.execute("SELECT COUNT(*) FROM assist_quick_registration").fetchone()[0] == 0
    assert fake.calls == []
    conn.close()


def test_week_loss_sunday_to_monday(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / "sun.db")
    _seed_settings(conn)
    sun = _ms_bangkok(2026, 11, 22, 22, 0)
    until = next_0820_bangkok_ms(sun)
    write_settings(conn, paused_until_ms=until)
    write_edge(conn, week_pause_id=WEEK_ID)
    mon_am = _ms_bangkok(2026, 11, 23, 1, 0)
    card = build_quick_card(
        conn, _happy_fake(), symbol="SUIUSDT", side="long", stop=Decimal("1.994"), now_ms=mon_am
    )
    assert card["reason"] == "week_loss_pause"
    after = _ms_bangkok(2026, 11, 23, 8, 21)
    card2 = build_quick_card(
        conn, _happy_fake(), symbol="SUIUSDT", side="long", stop=Decimal("1.994"), now_ms=after
    )
    assert card2["ok"] is True
    conn.close()


def test_week_loss_does_not_block_other_pauses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    # (i) streak-style
    conn = open_db(tmp_path / "nb1.db")
    _seed_settings(conn)
    write_settings(conn, paused_until_ms=NOW + 3_600_000)
    # week_pause_id None
    card = build_quick_card(
        conn, _happy_fake(), symbol="SUIUSDT", side="long", stop=Decimal("1.994"), now_ms=NOW
    )
    assert card["ok"] is True
    conn.close()

    # (ii) day-3R with last-week week_pause_id
    conn = open_db(tmp_path / "nb2.db")
    _seed_settings(conn)
    write_settings(conn, paused_until_ms=next_0820_bangkok_ms(TUE))
    write_edge(conn, day_pause_id=bangkok_day_id(TUE), week_pause_id="2026-11-09")
    card = build_quick_card(
        conn, _happy_fake(), symbol="SUIUSDT", side="long", stop=Decimal("1.994"), now_ms=TUE
    )
    assert card["ok"] is True
    conn.close()

    # (iii) 4R then clear_pause_manual
    conn = open_db(tmp_path / "nb3.db")
    _eval(conn, risk=1.0, net=-50, now_ms=TUE)
    clear_pause_manual(conn, now_ms_value=TUE + 60_000)
    card = build_quick_card(
        conn, _happy_fake(), symbol="SUIUSDT", side="long", stop=Decimal("1.994"), now_ms=TUE + 120_000
    )
    assert card["ok"] is True
    conn.close()

    # (iv) expired
    conn = open_db(tmp_path / "nb4.db")
    _seed_settings(conn)
    write_settings(conn, paused_until_ms=NOW - 1)
    write_edge(conn, week_pause_id=bangkok_week_id(NOW))
    card = build_quick_card(
        conn, _happy_fake(), symbol="SUIUSDT", side="long", stop=Decimal("1.994"), now_ms=NOW
    )
    assert card["ok"] is True
    conn.close()


def test_week_loss_known_limit_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail-closed: 4R fired at TUE; streak-style pause at WED_10 still blocks."""
    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / "kl.db")
    _eval(conn, risk=1.0, net=-50, now_ms=TUE)
    clear_pause_manual(conn, now_ms_value=TUE + 60_000)
    # streak-style pause later in the week; week_pause_id still WEEK_ID
    write_settings(conn, paused_until_ms=next_0820_bangkok_ms(WED_10))
    assert get_edge(conn)["week_pause_id"] == WEEK_ID
    card = build_quick_card(
        conn, _happy_fake(), symbol="SUIUSDT", side="long", stop=Decimal("1.994"), now_ms=WED_10
    )
    assert card["reason"] == "week_loss_pause"
    conn.close()


def test_week_loss_tap_after_pause_starts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "tap_pause.db")
    _seed_settings(conn)
    card = build_quick_card(
        conn, _happy_fake(), symbol="SUIUSDT", side="long", stop=Decimal("1.994"), now_ms=NOW
    )
    cb = card["callback_data"]
    until = next_0820_bangkok_ms(NOW + 30_000)
    write_settings(conn, paused_until_ms=until)
    write_edge(conn, week_pause_id=bangkok_week_id(NOW + 30_000))
    sent, send = _send_capture()
    answers, answer = _answers()
    process_update(
        conn,
        _cq(cb),
        send=send,
        answer=answer,
        futures=_happy_fake(),
        now_ms_value=NOW + 30_000,
    )
    assert len(answers) == 1
    assert len(sent) == 1
    assert "4R" in sent[0]["text"]
    assert conn.execute("SELECT COUNT(*) FROM assist_quick_registration").fetchone()[0] == 0
    conn.close()


def test_week_loss_tap_race_during_balance_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "race.db")
    _seed_settings(conn)
    card = build_quick_card(
        conn, _happy_fake(), symbol="SUIUSDT", side="long", stop=Decimal("1.994"), now_ms=NOW
    )
    cb = card["callback_data"]
    fake = _happy_fake()

    def _engage() -> None:
        until = next_0820_bangkok_ms(NOW + 30_000)
        write_settings(conn, paused_until_ms=until)
        write_edge(conn, week_pause_id=bangkok_week_id(NOW + 30_000))

    fake._on_balance = _engage
    sent, send = _send_capture()
    answers, answer = _answers()
    process_update(
        conn,
        _cq(cb),
        send=send,
        answer=answer,
        futures=fake,
        now_ms_value=NOW + 30_000,
    )
    assert len(answers) == 1
    assert len(sent) == 1
    assert "4R" in sent[0]["text"]
    assert conn.execute("SELECT COUNT(*) FROM assist_quick_registration").fetchone()[0] == 0
    conn.close()


def test_main_week_loss_pause_until_no_write(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "nowrite.db")
    # open_db seeds id=1 rows; wipe them so the helper must not recreate.
    conn.execute("DELETE FROM assist_settings")
    conn.execute("DELETE FROM assist_edge")
    conn.commit()
    before = conn.total_changes
    assert main_week_loss_pause_until(conn, now_ms=NOW) is None
    assert conn.total_changes == before
    assert conn.execute("SELECT COUNT(*) FROM assist_settings").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM assist_edge").fetchone()[0] == 0
    conn.close()


def test_qr_never_hits_exec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    import jarvise_assist.listen as listen_mod

    monkeypatch.setattr(
        listen_mod,
        "handle_exec_callback",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("exec")),
    )
    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / "noexec.db")
    _seed_settings(conn)
    sent, send = _send_capture()
    answers, answer = _answers()
    process_update(
        conn,
        _cq("qr:SUIUSDT:l:1.9940:1"),
        send=send,
        answer=answer,
        futures=_happy_fake(),
        now_ms_value=NOW,
    )
    assert len(answers) == 1
    conn.close()


def test_refusal_texts_cover_reasons() -> None:
    for key in (
        "locked",
        "quick_open",
        "main_position",
        "in_flight",
        "pending_order",
        "contract",
        "read_failed",
        "stop_wrong_side",
        "below_min_notional",
        "quick_cap",
        "portfolio_cap",
        "margin_cap",
        "leverage_cap",
        "card_expired",
        "bad_data",
    ):
        assert key in REFUSAL
        assert REFUSAL[key]
    assert "ไม่ย่อขนาด" in REFUSAL["margin_cap"]
    assert "เกิน 5x" in REFUSAL["leverage_cap"]


def test_card_json_markup_serializable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / "json.db")
    _seed_settings(conn)
    card = build_quick_card(
        conn, _happy_fake(), symbol="SUIUSDT", side="long", stop=Decimal("1.994"), now_ms=NOW
    )
    json.dumps(card["reply_markup"])
    conn.close()
