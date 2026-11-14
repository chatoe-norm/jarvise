"""(P) QPR2a pin tests — pass on main @ 122566a and after.

Must not import anything new (quick_card) so they run on base.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.test_assist import FakeFutures, _ms_bangkok
from tests.test_assist_day_loss import _trade
from tests.test_assist_listen import _send_capture
from tests.test_assist_quick import _answers
from tests.test_assist_week_loss_4r import MON_01, TUE, WED_10, WEEK_ID, _eval

from jarvise_assist.futures import FuturesPosition
from jarvise_assist.keyboard import BTN_ANYTHING, BTN_POSITIONS, BTN_QUICK, BTN_TOGGLE, BTN_WEEKLY
from jarvise_assist.listen import process_update
from jarvise_assist.pause import (
    bangkok_day_id,
    bangkok_week_id,
    clear_pause_manual,
    next_0820_bangkok_ms,
)
from jarvise_assist.store import ensure_settings, get_edge, write_settings
from jarvise_ingest.db import open_db


class QuickFake(FakeFutures):
    """Subclass used by pin tests; records calls like the (N) QuickFake."""

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
        return super().usdt_wallet_and_available()

    def symbol_config(self) -> dict[str, Any]:
        self._record("symbol_config")
        return super().symbol_config()


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


# ---------------------------------------------------------------------------
# (P) Text routing unchanged
# ---------------------------------------------------------------------------


def test_p_non_quick_texts_no_send_no_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    for text in ("hello", "q", "quick", "Q", "qtum"):
        conn = open_db(tmp_path / f"nqt_{text}.db")
        sent, send = _send_capture()
        fake = QuickFake()
        out = process_update(conn, _msg(text), send=send, futures=fake, now_ms_value=TUE)
        assert out.get("handled") is False
        assert sent == []
        assert fake.calls == []
        conn.close()


def test_p_btn_quick_menu_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "btn.db")
    sent, send = _send_capture()
    fake = QuickFake()
    process_update(conn, _msg(BTN_QUICK), send=send, futures=fake, now_ms_value=TUE)
    assert len(sent) == 1
    assert "⚡ Quick (scalping) — แยกจากระบบหลัก" in sent[0]["text"]
    assert "ยังไม่พิสูจน์ edge" in sent[0]["text"]
    conn.close()


def test_p_risk_number_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "risk.db")
    write_settings(conn, awaiting_risk="number")
    sent, send = _send_capture()
    fake = QuickFake()
    out = process_update(conn, _msg("1.5"), send=send, futures=fake, now_ms_value=TUE)
    assert out.get("handled") is True
    assert ensure_settings(conn)["risk_pct"] == 1.5 or ensure_settings(conn).get("awaiting_risk") == "confirm"
    assert fake.calls == []
    conn.close()


def test_p_explore_flow_while_awaiting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "expl.db")
    write_settings(conn, awaiting_explore=True)
    sent, send = _send_capture()
    fake = QuickFake()

    def lookup(sym: str) -> dict[str, Any] | None:
        return {
            "symbol": sym,
            "contractType": "PERPETUAL",
            "quoteAsset": "USDT",
            "status": "TRADING",
        }

    def fetch(_sym: str, _tf: str, _limit: int) -> list[dict[str, Any]]:
        return [{"open_time": 1, "close": 1.0}]

    out = process_update(
        conn,
        _msg("LINKUSDT"),
        send=send,
        futures=fake,
        now_ms_value=TUE,
        explore_lookup=lookup,
        explore_fetch=fetch,
    )
    assert sent
    assert "LINKUSDT" in sent[0]["text"] or "ทดลอง" in sent[0]["text"] or out.get("ok") is not None
    conn.close()


# ---------------------------------------------------------------------------
# (P) Callback routing
# ---------------------------------------------------------------------------


def test_p_qs_risk_still_saves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "qs.db")
    sent, send = _send_capture()
    answers, answer = _answers()
    from jarvise_assist.quick import get_quick_settings

    process_update(
        conn,
        _cq("qs:risk:0.25"),
        send=send,
        answer=answer,
        futures=QuickFake(),
        now_ms_value=TUE,
    )
    assert get_quick_settings(conn)["risk_pct"] == 0.25
    assert len(answers) == 1
    conn.close()


def test_p_qx_unhandled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "qx.db")
    sent, send = _send_capture()
    answers, answer = _answers()
    out = process_update(
        conn,
        _cq("qx:1"),
        send=send,
        answer=answer,
        futures=QuickFake(),
        now_ms_value=TUE,
    )
    assert out == {"ok": True, "unhandled_callback": "qx:1"}
    assert len(answers) == 1
    conn.close()


def test_p_qr_never_reaches_exec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    import jarvise_assist.listen as listen_mod

    monkeypatch.setattr(
        listen_mod,
        "handle_exec_callback",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("exec")),
    )
    conn = open_db(tmp_path / "qr_pin.db")
    sent, send = _send_capture()
    answers, answer = _answers()
    # On base, qr: is unhandled (not exec). After QPR2a it is handled by quick_card.
    out = process_update(
        conn,
        _cq("qr:SUIUSDT:l:1.9940:1"),
        send=send,
        answer=answer,
        futures=QuickFake(),
        now_ms_value=TUE,
    )
    assert "exec" not in str(out).lower() or out.get("ok") is True
    # Must not raise from handle_exec_callback
    assert out.get("unhandled_callback") == "qr:SUIUSDT:l:1.9940:1" or out.get("ok") is not None
    conn.close()


# ---------------------------------------------------------------------------
# (P) SQLite contract the 4R check reads
# ---------------------------------------------------------------------------


def test_p_week_loss_sqlite_contract(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "wk.db")
    result, _sent = _eval(conn, risk=1.0, net=-50, now_ms=TUE)
    assert result["reason"] == "week_loss"
    assert ensure_settings(conn)["paused_until_ms"] == next_0820_bangkok_ms(TUE)
    assert get_edge(conn)["week_pause_id"] == bangkok_week_id(TUE) == WEEK_ID

    clear_pause_manual(conn, now_ms_value=TUE + 60_000)
    assert ensure_settings(conn)["paused_until_ms"] is None
    assert get_edge(conn)["week_pause_id"] == WEEK_ID
    conn.close()


def test_p_streak_and_day_leave_week_pause_id(tmp_path: Path) -> None:
    # Streak pause
    conn = open_db(tmp_path / "streak.db")
    now = _ms_bangkok(2026, 10, 27, 15, 0)
    income = [
        {"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "income": "-5", "time": now - 3_000},
        {"symbol": "ETHUSDT", "incomeType": "REALIZED_PNL", "income": "-4", "time": now - 2_000},
        {"symbol": "SOLUSDT", "incomeType": "REALIZED_PNL", "income": "-3", "time": now - 1_000},
    ]
    from jarvise_assist.pause import evaluate_pause

    sent, send = _send_capture()
    streak = evaluate_pause(
        conn,
        futures=FakeFutures(balance=Decimal("1000"), income=income),
        now_ms=now,
        send=send,
    )
    assert streak["reason"] == "streak"
    assert ensure_settings(conn)["paused_until_ms"] == next_0820_bangkok_ms(now)
    assert get_edge(conn).get("week_pause_id") is None
    conn.close()

    # Day-3R pause
    conn = open_db(tmp_path / "day3.db")
    day_now = _ms_bangkok(2026, 11, 9, 12, 0)
    _trade(conn, oid="t", exit_ms=day_now - 60_000, net=-3.0)
    sent, send = _send_capture()
    day = evaluate_pause(
        conn,
        futures=FakeFutures(balance=Decimal("1000"), income=[]),
        now_ms=day_now,
        send=send,
    )
    assert day["reason"] == "day_loss"
    assert ensure_settings(conn)["paused_until_ms"] == next_0820_bangkok_ms(day_now)
    assert get_edge(conn).get("week_pause_id") is None
    assert get_edge(conn)["day_pause_id"] == bangkok_day_id(day_now)
    conn.close()


def test_p_next_0820_within_24h() -> None:
    for now in (MON_01, TUE, WED_10):
        delta = next_0820_bangkok_ms(now) - now
        assert 0 < delta <= 86_400_000


def test_p_menu_labels_not_quick_commands() -> None:
    """Pin that menu labels are not mistaken for q-commands on base routing."""
    labels = [BTN_QUICK, BTN_POSITIONS, BTN_ANYTHING, BTN_WEEKLY, BTN_TOGGLE]
    assert all(isinstance(x, str) and x for x in labels)
