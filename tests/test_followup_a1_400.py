"""Follow-up lows/nits from PR #401 (Quick A1) and PR #400 (owner hist CLI)."""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.test_assist import _seed_analysis
from tests.test_assist_quick_execute_a1 import (
    RecordingTrade,
    _daytime_ms,
    _FuturesRead,
    _insert_quoted_card,
    _Pos,
    _quick_preview,
)
from tests.test_assist_quick_execute_a1_round12 import _bot, _db, _kill_off, _open_quick
from tests.test_partner_owner_history_cli import DAY, HOUR, _args, _invoke, _json, _make_db, _now
from typer.testing import CliRunner

from jarvise.cli import app
from jarvise_assist import partner_admin as pa
from jarvise_assist import partner_store as ps
from jarvise_assist.partner_audit import Finding
from jarvise_assist.partner_value import AccountValue
from jarvise_assist.quick import REG_TTL_MS, _expire_window_phrase
from jarvise_assist.quick_card import (
    CARD_TTL_PREFIX,
    REFUSAL,
    _fail,
    build_quick_card,
    stop_far_refusal_text,
)
from jarvise_assist.quick_execute import BTN_QUICK_BOT_ENTER
from jarvise_assist.quick_m1a1 import (
    _quoted_markup_for_card,
    _registration_refusal_text,
    handle_quick_trade_callback,
    msg_stop_far,
    registration_success_message,
)
from jarvise_assist.real_trades import _quick_owned_client_and_order_ids
from jarvise_assist.sizing import expected_analysis_bar_ms
from jarvise_assist.store import get_exec_intent, insert_exec_intent, insert_exec_log
from jarvise_ingest.db import open_db

D = Decimal
runner = CliRunner()
RECORD_TAIL = "บอทไม่ส่งคำสั่ง — กดลงทะเบียน แล้วเปิดเองใน Binance ภายใน "
BOT_TAIL = "⚡ = บอทส่งคำสั่งหลังยืนยัน · กดลงทะเบียน = เปิดเองใน Binance ภายใน "


def _footer(text: str) -> str:
    for line in str(text).splitlines():
        if line.startswith(CARD_TTL_PREFIX):
            return line
    raise AssertionError(text)


def _span(window_ms: int | None = None) -> str:
    return _expire_window_phrase(REG_TTL_MS if window_ms is None else window_ms)


class _TickBook(_FuturesRead):
    def __init__(self, *, tick: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._tick = Decimal(tick)

    def symbol_filters(self, symbol: str) -> Any:
        filters = super().symbol_filters(symbol)
        filters.tick_size = self._tick
        return filters


class _WalletBoom(RecordingTrade):
    def usdt_wallet_and_available(self) -> tuple[Decimal, Decimal]:
        self.calls.append(("usdt_wallet_and_available", {}))
        raise RuntimeError("balance down")


class _SecondWalletBoom(_FuturesRead):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._wallet_n = 0

    def usdt_wallet_and_available(self) -> tuple[Decimal, Decimal]:
        self._wallet_n += 1
        self.calls.append(("usdt_wallet_and_available", {"n": self._wallet_n}))
        if self._wallet_n >= 2:
            raise RuntimeError("balance down")
        return self.balance, self.available


class _NthFiltersBoom(_FuturesRead):
    """Card build may read filters once. The qk:ex read is the later call."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._filters_n = 0

    def symbol_filters(self, symbol: str) -> Any:
        self._filters_n += 1
        if self._filters_n >= 2:
            raise RuntimeError("filters down")
        return super().symbol_filters(symbol)


class _SecondPositionBoom(RecordingTrade):
    """First position_risk succeeds. A later call raises."""

    def position_risk(self, symbol: str | None = None) -> list[Any]:
        seen = sum(1 for name, _payload in self.calls if name == "position_risk")
        if seen >= 1:
            self.calls.append(("position_risk", {"symbol": symbol}))
            raise RuntimeError("positions down")
        return super().position_risk(symbol)


class _StepBoom(RecordingTrade):
    def __init__(self, *, step: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._step = step

    def position_risk(self, symbol: str | None = None) -> list[Any]:
        if self._step == "positions":
            self.calls.append(("position_risk", {"symbol": symbol}))
            raise RuntimeError("positions down")
        return super().position_risk(symbol)

    def mark_price(self, symbol: str) -> Decimal:
        if self._step == "mark":
            self.calls.append(("mark_price", {"symbol": symbol}))
            raise RuntimeError("mark down")
        return super().mark_price(symbol)

    def symbol_filters(self, symbol: str) -> Any:
        if self._step == "filters":
            self.calls.append(("symbol_filters", {"symbol": symbol}))
            raise RuntimeError("filters down")
        return super().symbol_filters(symbol)


def _ey(
    conn: Any,
    iid: str,
    trade: Any,
    ts: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from jarvise_assist.execute import handle_exec_callback

    edits: list[dict[str, Any]] = []

    def edit(mid: int, text: str, reply_markup: dict[str, Any] | None = None) -> bool:
        edits.append({"mid": mid, "text": text, "markup": reply_markup})
        return True

    out = handle_exec_callback(
        conn,
        f"ey:{iid}",
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=edit,
        trade=trade,
        now_ms_value=ts,
        message={"message_id": 77},
    )
    return out, edits


# ---------------------------------------------------------------- A1


def test_a1_bot_footer_matches_execute_button(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _bot(monkeypatch)
    conn = _db(tmp_path, "footer.db")
    ts = _daytime_ms()
    card = build_quick_card(
        conn,
        _FuturesRead(mark=D("100"), balance=D("10000")),
        symbol="BTCUSDT",
        side="long",
        stop=D("99"),
        now_ms=ts,
    )
    assert card["ok"] is True and card["bot_eligible"] is True
    footer = _footer(card["text"])
    assert "บอทไม่ส่งคำสั่ง" not in footer
    assert f"{BOT_TAIL}{_span()}" in footer
    markup = json.dumps(_quoted_markup_for_card(conn, "aqc_x", card), ensure_ascii=False)
    assert BTN_QUICK_BOT_ENTER in markup
    conn.close()


def test_a1_footer_window_comes_from_reg_ttl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("jarvise_assist.quick.REG_TTL_MS", 15 * 60_000)
    span = _expire_window_phrase(15 * 60_000)
    conn = _db(tmp_path, "win-off.db")
    off = build_quick_card(
        conn,
        _FuturesRead(mark=D("100"), balance=D("10000")),
        symbol="BTCUSDT",
        side="long",
        stop=D("99"),
        now_ms=_daytime_ms(),
    )
    assert f"{RECORD_TAIL}{span}" in _footer(off["text"])
    conn.close()

    _bot(monkeypatch)
    conn_on = _db(tmp_path, "win-on.db")
    on = build_quick_card(
        conn_on,
        _FuturesRead(mark=D("100"), balance=D("10000")),
        symbol="BTCUSDT",
        side="long",
        stop=D("99"),
        now_ms=_daytime_ms(),
    )
    assert on["bot_eligible"] is True
    assert f"{BOT_TAIL}{span}" in _footer(on["text"])
    assert "10 นาที" not in _footer(on["text"])
    conn_on.close()


def test_a1_record_only_footer_stays_when_button_hidden(tmp_path: Path) -> None:
    conn = _db(tmp_path, "record.db")
    card = build_quick_card(
        conn,
        _FuturesRead(mark=D("100"), balance=D("10000")),
        symbol="BTCUSDT",
        side="long",
        stop=D("99"),
        now_ms=_daytime_ms(),
    )
    assert card["bot_eligible"] is False
    assert f"{RECORD_TAIL}{_span()}" in _footer(card["text"])
    conn.close()


def test_a1_dust_tp_result_uses_shared_constant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_assist_quick_execute_a1 import _quick_preview

    from jarvise_assist.execute import run_entry_sequence
    from jarvise_assist.quick_execute import MSG_QUICK_NO_TP1
    from jarvise_assist.store import claim_exec_intent

    _bot(monkeypatch)
    conn = _db(tmp_path, "dust-tp.db")
    ts = _daytime_ms()
    iid = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview=_quick_preview(qty="0.05", tp1pct="50"),
        created_at_ms=ts,
        expires_at_ms=ts + 60_000,
    )
    claim_exec_intent(conn, iid)
    text = run_entry_sequence(
        conn,
        iid,
        trade=RecordingTrade(mark=D("100"), fill_qty=D("0.05"), fill_avg=D("100")),
        origin="quick",
        sleep=lambda _s: None,
    )
    assert MSG_QUICK_NO_TP1 in text
    assert "ลองใหม่" not in text
    conn.close()


def test_a1_testnet_qk_ex_rounds_stop_to_demo_tick(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _bot(monkeypatch, "testnet")
    _kill_off(monkeypatch)
    conn = _db(tmp_path, "tick.db")
    ts = _daytime_ms()
    cid = _insert_quoted_card(conn, now_ms_value=ts, entry=100.0, stop=99.0)
    demo = _TickBook(tick="0.07", mark=D("100.01"), balance=D("10000"))
    monkeypatch.setattr("jarvise_assist.execute.build_trade_client", lambda: (demo, None))
    sent: list[str] = []
    out = handle_quick_trade_callback(
        conn,
        f"qk:ex:{cid}",
        futures=_TickBook(tick="0.01", mark=D("100"), balance=D("10000")),
        send=lambda text, **_k: sent.append(str(text)) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: True,
        now_ms=ts,
        message={"message_id": 1, "chat": {"id": 1}},
    )
    assert out.get("ok") is True, out
    intent = get_exec_intent(conn, str(out["intent_id"]))
    assert intent is not None
    assert D(str(intent["preview"]["stop"])) == D("98.98")
    assert intent["preview"]["tick"] == "0.07"
    assert intent["preview"]["testnet_levels"] is True
    conn.close()


def test_a1_testnet_short_stop_rounds_away_from_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bot(monkeypatch, "testnet")
    _kill_off(monkeypatch)
    conn = _db(tmp_path, "tick-s.db")
    ts = _daytime_ms()
    cid = _insert_quoted_card(conn, side="short", entry=100.0, stop=101.0, now_ms_value=ts)
    demo = _TickBook(tick="0.07", mark=D("100.01"), balance=D("10000"))
    monkeypatch.setattr("jarvise_assist.execute.build_trade_client", lambda: (demo, None))
    out = handle_quick_trade_callback(
        conn,
        f"qk:ex:{cid}",
        futures=_TickBook(tick="0.01", mark=D("100"), balance=D("10000")),
        send=lambda text, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: True,
        now_ms=ts,
        message={"message_id": 1, "chat": {"id": 1}},
    )
    assert out.get("ok") is True, out
    intent = get_exec_intent(conn, str(out["intent_id"]))
    assert intent is not None
    assert D(str(intent["preview"]["stop"])) == D("101.08")
    conn.close()


def test_a1_mainnet_qk_ex_stop_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _bot(monkeypatch, "live")
    _kill_off(monkeypatch)
    conn = _db(tmp_path, "mainnet.db")
    ts = _daytime_ms()
    cid = _insert_quoted_card(conn, now_ms_value=ts, entry=100.0, stop=99.0)
    out = handle_quick_trade_callback(
        conn,
        f"qk:ex:{cid}",
        futures=_FuturesRead(mark=D("100"), balance=D("10000")),
        send=lambda text, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: True,
        now_ms=ts,
        message={"message_id": 1, "chat": {"id": 1}},
    )
    assert out.get("ok") is True, out
    intent = get_exec_intent(conn, str(out["intent_id"]))
    assert intent is not None
    assert D(str(intent["preview"]["stop"])) == D("99")
    assert intent["preview"]["tick"] == "0.01"
    assert intent["preview"]["testnet_levels"] is False
    conn.close()


def test_a1_quick_cap_fallback_counts_other_symbols(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _bot(monkeypatch)
    _kill_off(monkeypatch)
    conn = _db(tmp_path, "cap.db")
    ts = _daytime_ms()
    _open_quick(conn, symbol="ADAUSDT", risk="60", now=ts)
    cid = _insert_quoted_card(conn, now_ms_value=ts)
    fut = _FuturesRead(
        mark=D("100"),
        balance=D("10000"),
        positions=[
            _Pos("ETHUSDT", qty="1", entry="100"),
            _Pos("SOLUSDT", qty="1", entry="100"),
        ],
    )
    sent: list[str] = []
    out = handle_quick_trade_callback(
        conn,
        f"qk:ex:{cid}",
        futures=fut,
        send=lambda text, **_k: sent.append(str(text)) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: True,
        now_ms=ts,
        message={"message_id": 1, "chat": {"id": 1}},
    )
    assert out.get("ok") is False
    assert out.get("reason") == "portfolio_cap"
    assert sent == [] or all("ยืนยันให้บอทเข้า" not in line for line in sent)
    filtered = [c for c in fut.calls if c[0] == "position_risk" and c[1].get("symbol")]
    assert filtered == []
    conn.close()


def test_a1_quick_confirm_balance_read_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvise_assist.execute import handle_exec_callback

    _bot(monkeypatch)
    _kill_off(monkeypatch)
    conn = _db(tmp_path, "qbal.db")
    ts = _daytime_ms()
    iid = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview=_quick_preview(),
        created_at_ms=ts,
        expires_at_ms=ts + 30_000,
    )
    out, edits = _ey(conn, iid, _WalletBoom(mark=D("100")), ts)
    assert out.get("reason") == "fapi_read_failed:balance"
    assert out.get("reason") != "handler_error"
    assert any(item["markup"] == {"inline_keyboard": []} for item in edits)
    assert any("อ่านบัญชีไม่ได้ (balance)" in item["text"] for item in edits)
    again = handle_exec_callback(
        conn,
        f"ey:{iid}",
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: True,
        trade=_WalletBoom(mark=D("100")),
        now_ms_value=ts + 1_000,
        message={"message_id": 77},
    )
    assert again.get("reason") != "handler_error"
    conn.close()


def test_a1_main_confirm_balance_read_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _bot(monkeypatch)
    _kill_off(monkeypatch)
    conn = _db(tmp_path, "mbal.db")
    ts = _daytime_ms()
    bar = expected_analysis_bar_ms(ts)
    _seed_analysis(conn, symbol="BTCUSDT", action="long", confidence=0.8, invalidation=99.0, ts=bar)
    iid = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview={
            "qty": "0.01",
            "entry": "100",
            "stop": "99",
            "target": "102",
            "leverage": 2,
            "seen_entry": "100",
            "seen_stop": "99",
        },
        created_at_ms=ts,
        expires_at_ms=ts + 60_000,
    )
    out, edits = _ey(conn, iid, _WalletBoom(mark=D("100")), ts)
    assert out.get("reason") == "fapi_read_failed:balance", out
    assert any(item["markup"] == {"inline_keyboard": []} for item in edits)
    assert any("อ่านบัญชีไม่ได้ (balance)" in item["text"] for item in edits)
    conn.close()


def test_a1_qk_ex_balance_read_strips_card(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _bot(monkeypatch)
    _kill_off(monkeypatch)
    conn = _db(tmp_path, "qkbal.db")
    ts = _daytime_ms()
    cid = _insert_quoted_card(conn, now_ms_value=ts)
    edits: list[Any] = []
    out = handle_quick_trade_callback(
        conn,
        f"qk:ex:{cid}",
        futures=_SecondWalletBoom(mark=D("100"), balance=D("10000")),
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda *a, **k: edits.append((a, k)) or True,
        now_ms=ts,
        message={"message_id": 1, "chat": {"id": 1}},
    )
    assert out.get("reason") == "fapi_read_failed:balance", out
    assert out.get("reason") != "read_failed"
    blob = json.dumps(edits, ensure_ascii=False, default=str)
    assert '"inline_keyboard": []' in blob or '"inline_keyboard":[]' in blob
    assert "อ่านบัญชีไม่ได้ (balance)" in blob
    assert "ลองใหม่" not in blob
    conn.close()


def test_r2_qk_ex_balance_failure_sends_and_strips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _bot(monkeypatch)
    _kill_off(monkeypatch)
    conn = _db(tmp_path, "qkbal-send.db")
    ts = _daytime_ms()
    cid = _insert_quoted_card(conn, now_ms_value=ts)
    sent: list[str] = []
    edits: list[Any] = []
    out = handle_quick_trade_callback(
        conn,
        f"qk:ex:{cid}",
        futures=_SecondWalletBoom(mark=D("100"), balance=D("10000")),
        send=lambda text, **_k: sent.append(str(text)) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *a, **k: edits.append((a, k)) or True,
        now_ms=ts,
        message={"message_id": 1, "chat": {"id": 1}},
    )
    assert out.get("reason") == "fapi_read_failed:balance", out
    assert any("อ่านบัญชีไม่ได้ (balance)" in line for line in sent)
    blob = json.dumps({"sent": sent, "edits": edits}, ensure_ascii=False, default=str)
    assert '"inline_keyboard": []' in blob or '"inline_keyboard":[]' in blob
    assert "ลองใหม่" not in blob
    conn.close()


def test_r2_qk_ex_filters_failure_strips_keyboard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _bot(monkeypatch)
    _kill_off(monkeypatch)
    conn = _db(tmp_path, "qkfilt.db")
    ts = _daytime_ms()
    cid = _insert_quoted_card(conn, now_ms_value=ts)
    answers: list[Any] = []
    edits: list[Any] = []
    sent: list[str] = []
    out = handle_quick_trade_callback(
        conn,
        f"qk:ex:{cid}",
        futures=_NthFiltersBoom(mark=D("100"), balance=D("10000")),
        send=lambda text, **_k: sent.append(str(text)) or True,
        answer=lambda text=None, **_k: answers.append(text),
        edit=lambda *a, **k: edits.append((a, k)) or True,
        now_ms=ts,
        message={"message_id": 1, "chat": {"id": 1}},
    )
    assert out.get("reason") == "fapi_read_failed:filters", out
    assert any("อ่านบัญชีไม่ได้ (filters)" in line for line in sent)
    assert answers == [None]
    blob = json.dumps({"sent": sent, "edits": edits, "answers": answers}, ensure_ascii=False, default=str)
    assert '"inline_keyboard": []' in blob or '"inline_keyboard":[]' in blob
    assert "อ่านบัญชีไม่ได้ (filters)" in blob
    assert "ลองใหม่" not in blob
    conn.close()


def _main_entry_intent(conn: Any, ts: int, *, qty: str = "0.01") -> str:
    bar = expected_analysis_bar_ms(ts)
    _seed_analysis(conn, symbol="BTCUSDT", action="long", confidence=0.8, invalidation=99.0, ts=bar)
    return insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview={
            "qty": qty,
            "entry": "100",
            "stop": "99",
            "target": "102",
            "leverage": 2,
            "seen_entry": "100",
            "seen_stop": "99",
        },
        created_at_ms=ts,
        expires_at_ms=ts + 60_000,
    )


@pytest.mark.parametrize("step", ["positions", "mark", "filters"])
def test_r2_main_confirm_read_failures_strip_keyboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    from jarvise_assist.execute import _reason_th, handle_exec_callback

    _bot(monkeypatch)
    _kill_off(monkeypatch)
    conn = _db(tmp_path, f"guard-{step}.db")
    ts = _daytime_ms()
    iid = _main_entry_intent(conn, ts)
    sent: list[str] = []
    edits: list[dict[str, Any]] = []

    def edit(mid: int, text: str, reply_markup: dict[str, Any] | None = None) -> bool:
        edits.append({"mid": mid, "text": text, "markup": reply_markup})
        return True

    out = handle_exec_callback(
        conn,
        f"ey:{iid}",
        send=lambda text, **_k: sent.append(str(text)) or True,
        answer=lambda *_a, **_k: None,
        edit=edit,
        trade=_StepBoom(step=step, mark=D("100")),
        now_ms_value=ts,
        message={"message_id": 77},
    )
    reason = f"fapi_read_failed:{step}"
    thai = _reason_th(reason)
    assert out.get("reason") == reason, out
    assert thai == f"อ่านบัญชีไม่ได้ ({step})"
    assert "ลองใหม่" not in thai
    assert any(thai in line for line in sent)
    assert any(item["markup"] == {"inline_keyboard": []} and thai in item["text"] for item in edits)
    conn.close()


def test_r2_main_confirm_success_path_is_not_a_read_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jarvise_assist.execute import _confirm_guards_entry

    _bot(monkeypatch)
    _kill_off(monkeypatch)
    conn = _db(tmp_path, "guard-ok.db")
    ts = _daytime_ms()
    iid = _main_entry_intent(conn, ts, qty="0.1")
    intent = get_exec_intent(conn, iid)
    assert intent is not None
    trade = RecordingTrade(mark=D("100"))
    guard = _confirm_guards_entry(conn, intent, trade, now_ms_value=ts)
    assert guard is None
    conn.close()


def test_r3_confirm_reuses_positions_already_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvise_assist.execute import _confirm_guards_entry

    _bot(monkeypatch)
    _kill_off(monkeypatch)
    conn = _db(tmp_path, "guard-reuse.db")
    ts = _daytime_ms()
    iid = _main_entry_intent(conn, ts, qty="0.1")
    intent = get_exec_intent(conn, iid)
    assert intent is not None
    trade = _SecondPositionBoom(mark=D("100"))
    guard = _confirm_guards_entry(conn, intent, trade, now_ms_value=ts)
    assert guard is None
    assert sum(1 for name, _payload in trade.calls if name == "position_risk") == 1
    conn.close()


def test_r2_registration_success_window_follows_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    kwargs = {
        "sym": "BTCUSDT",
        "side": "long",
        "entry": D("100"),
        "stop": D("90"),
        "qty": D("0.01"),
        "tick": D("0.1"),
        "now_ms": _daytime_ms(),
    }
    default = registration_success_message(**kwargs)
    assert "ภายใน 10 นาที" in default
    assert "เหลือ 10 นาที" in default
    monkeypatch.setattr("jarvise_assist.quick_m1a1.REG_TTL_MS", 15 * 60_000)
    changed = registration_success_message(**kwargs)
    assert "ภายใน 15 นาที" in changed
    assert "เหลือ 15 นาที" in changed
    assert "10 นาที" not in changed


def test_a1_quick_cohort_menu_ids_are_owned(tmp_path: Path) -> None:
    conn = _db(tmp_path, "ids.db")
    ts = _daytime_ms()
    menu = insert_exec_intent(
        conn,
        kind="exit",
        symbol="BTCUSDT",
        side="long",
        preview={"source": "menu", "cohort": "quick", "symbol": "BTCUSDT"},
        created_at_ms=ts,
        expires_at_ms=ts + 60_000,
    )
    insert_exec_log(
        conn,
        intent_id=menu,
        step="exit_market",
        endpoint="local",
        client_order_id="jv-menu-ex",
        order_or_algo_id="9001",
        http_status=200,
    )
    insert_exec_log(
        conn,
        intent_id=menu,
        step="reduce_market",
        endpoint="local",
        client_order_id="jv-menu-rd",
        order_or_algo_id="9003",
        http_status=200,
    )
    insert_exec_log(
        conn,
        intent_id=menu,
        step="move_stop_place",
        endpoint="local",
        client_order_id="jv-menu-ms",
        order_or_algo_id="9002",
        http_status=200,
    )
    main = insert_exec_intent(
        conn,
        kind="exit",
        symbol="ETHUSDT",
        side="long",
        preview={"source": "menu", "symbol": "ETHUSDT"},
        created_at_ms=ts,
        expires_at_ms=ts + 60_000,
    )
    insert_exec_log(
        conn,
        intent_id=main,
        step="exit_market",
        endpoint="local",
        client_order_id="jv-main-ex",
        order_or_algo_id="8001",
        http_status=200,
    )
    cids, order_ids, algo_ids = _quick_owned_client_and_order_ids(conn)
    assert "jv-menu-ex" in cids
    assert "jv-menu-rd" in cids
    assert "jv-menu-ms" in cids
    assert ("BTCUSDT", "9001") in order_ids
    assert ("BTCUSDT", "9003") in order_ids
    assert ("BTCUSDT", "9002") in algo_ids
    assert ("BTCUSDT", "9002") not in order_ids
    assert "jv-main-ex" not in cids
    assert ("ETHUSDT", "8001") not in order_ids
    conn.close()


def test_a1_lightning_hidden_when_bot_plan_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _bot(monkeypatch)
    monkeypatch.setattr("jarvise_assist.quick_execute.build_bot_plan", lambda *_a, **_k: None)
    conn = _db(tmp_path, "noplan.db")
    card = build_quick_card(
        conn,
        _FuturesRead(mark=D("100"), balance=D("10000")),
        symbol="BTCUSDT",
        side="long",
        stop=D("99"),
        now_ms=_daytime_ms(),
    )
    assert card["ok"] is True
    assert card["bot_eligible"] is False
    flat = json.dumps(_quoted_markup_for_card(conn, "aqc_x", card), ensure_ascii=False)
    assert BTN_QUICK_BOT_ENTER not in flat
    assert "บอทไม่ส่งคำสั่ง" in _footer(card["text"])
    conn.close()


def test_r2_stop_far_text_reads_db_setting(tmp_path: Path) -> None:
    import jarvise_assist.quick_m1a1 as quick_m1a1
    from jarvise_assist.quick_config import set_quick_config

    conn = _db(tmp_path, "far.db")
    ts = _daytime_ms()
    assert stop_far_refusal_text(conn).startswith("❌ stop ห่างเกิน 20%")
    assert "20%" in msg_stop_far(conn)
    assert type(REFUSAL) is dict
    assert "stop_far" not in REFUSAL
    assert not hasattr(quick_m1a1, "MSG_STOP_FAR")
    set_quick_config(conn, "quick_stop_far_pct", "15", source="test", now_ms=ts)
    refusal = stop_far_refusal_text(conn)
    draft = msg_stop_far(conn)
    looked_up = _fail("stop_far", conn=conn)["text"]
    registered = _registration_refusal_text("stop_far", conn)
    for text in (refusal, draft, looked_up, registered):
        assert "15%" in text
        assert "20%" not in text
    assert "ลองใหม่" not in refusal
    conn.close()


# ---------------------------------------------------------------- B


def _boom(*_a: Any, **_k: Any) -> Any:
    raise sqlite3.OperationalError("SUPER_SECRET_DB")


def test_b_probe_sqlite_error_exits_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("jarvise_ingest.universe_cli.connect_readonly", _boom)
    path = _make_db(tmp_path / "probe.db")
    now = _now()
    result = _invoke(path, *_args("dep", "10", "1010", now - DAY))
    assert result.exit_code == 2, result.output
    assert "Traceback" not in (result.output or "")
    assert "SUPER_SECRET_DB" not in (result.output or "")
    assert _json(result)["reason"] == "db_error"


def test_b_dry_run_sqlite_error_exits_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("jarvise_assist.partner_owner_history.preview_owner_tx", _boom)
    path = _make_db(tmp_path / "dry.db")
    result = _invoke(path, *_args("dep", "10", "1010", _now() - DAY))
    assert result.exit_code == 2, result.output
    assert "Traceback" not in (result.output or "")
    assert "SUPER_SECRET_DB" not in (result.output or "")
    assert _json(result)["reason"] == "db_error"


def test_b_apply_sqlite_error_exits_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("jarvise_assist.partner_owner_history.apply_owner_tx", _boom)
    path = _make_db(tmp_path / "apply.db")
    result = _invoke(path, *_args("dep", "10", "1010", _now() - DAY), "--apply")
    assert result.exit_code == 2, result.output
    assert "Traceback" not in (result.output or "")
    assert "SUPER_SECRET_DB" not in (result.output or "")
    body = _json(result)
    assert body["reason"] == "db_error"
    assert body["mode"] == "apply"


def test_b_help_describes_account_value_before_payouts() -> None:
    import jarvise_assist.partner_owner_history as poh

    result = runner.invoke(app, ["assist", "partner-record-owner-tx", "--help"])
    assert result.exit_code == 0
    phrase = "wallet + uPnL"
    assert phrase in (result.output or "")
    assert "ค้างจ่าย" in (result.output or "")
    assert "BEFORE" in (result.output or "")
    doc = poh.__doc__ or ""
    assert phrase in doc
    assert "ค้างจ่าย" in doc
    assert "BEFORE" in doc


def test_b_second_backdated_booking_uses_effective_time(tmp_path: Path) -> None:
    now = _now()
    t1 = now - 3 * DAY
    t2 = now - 2 * DAY
    path = _make_db(tmp_path / "seq.db", init_ms=now - 10 * DAY)
    first = _invoke(path, *_args("dep", "100", "1100", t1, tran="900000000011"), "--apply")
    assert first.exit_code == 0, first.output
    second = _invoke(path, *_args("wd", "50", "1050", t2, tran="900000000012"), "--apply")
    assert second.exit_code == 0, second.output
    assert _json(second)["reason"] == "recorded"
    conn = open_db(path)
    try:
        assert ps.owner_events_after(conn, t2) == "events_after_effective"
        assert ps.owner_events_after(conn, t2, historical=True) is None
    finally:
        conn.close()


def test_b_normal_event_after_transfer_still_blocks(tmp_path: Path) -> None:
    now = _now()
    t_transfer = now - 2 * DAY
    path = _make_db(tmp_path / "normal.db", init_ms=now - 10 * DAY)
    conn = open_db(path)
    try:
        bob = ps.create_partner(conn, name="bob", now_ms=now - 5 * DAY)
        booked = ps.apply_deposit(conn, str(bob["partner_id"]), D("10"), D("1010"), t_transfer + HOUR)
        assert booked["ok"] is True, booked
    finally:
        conn.close()
    refused = _invoke(path, *_args("dep", "100", "1110", t_transfer, tran="900000000021"), "--apply")
    assert refused.exit_code == 1, refused.output
    assert _json(refused)["reason"] == "events_after_effective"


def test_b_later_backdated_effective_still_blocks(tmp_path: Path) -> None:
    now = _now()
    path = _make_db(tmp_path / "later.db", init_ms=now - 10 * DAY)
    late = _invoke(path, *_args("dep", "100", "1100", now - 2 * DAY, tran="900000000031"), "--apply")
    assert late.exit_code == 0, late.output
    early = _invoke(path, *_args("dep", "10", "1110", now - 4 * DAY, tran="900000000032"), "--apply")
    assert early.exit_code == 1, early.output
    assert _json(early)["reason"] == "events_after_effective"


def test_b_dust_account_value_is_bad_input(tmp_path: Path) -> None:
    from jarvise_assist.partner_owner_history import validate_args

    now = _now()
    _params, err = validate_args(
        side="dep",
        tran_id="900000000041",
        amount="10",
        account_value="0.0000004",
        effective_at_ms=now - DAY,
        now_ms=now,
    )
    assert err is not None and err["reason"] == "bad_account_value"
    kept, kept_err = validate_args(
        side="dep",
        tran_id="900000000042",
        amount="10",
        account_value="0.000001",
        effective_at_ms=now - DAY,
        now_ms=now,
    )
    assert kept_err is None and kept is not None
    path = _make_db(tmp_path / "dust.db")
    cli = _invoke(path, *_args("dep", "10", "0.0000004", now - DAY))
    assert cli.exit_code == 2, cli.output
    assert _json(cli)["reason"] == "bad_account_value"
    conn = open_db(path)
    try:
        planned = ps.plan_owner_tx(
            conn,
            kind="owner_deposit",
            amount=D("10"),
            account_value=D("0.0000004"),
            now_ms=now,
        )
        assert planned["ok"] is False
        assert planned["reason"] == "bad_account_value"
        tiny = ps.plan_owner_tx(
            conn,
            kind="owner_deposit",
            amount=D("10"),
            account_value=D("0.000001"),
            now_ms=now,
        )
        assert tiny.get("reason") != "bad_account_value"
    finally:
        conn.close()


def test_b_live_safety_uses_shared_owner_events_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[bool] = []

    def spy(conn: Any, t_ms: int, historical: bool = False) -> str:
        _ = (conn, t_ms)
        seen.append(historical)
        return "events_after_effective" if len(seen) == 1 else "requests_after_effective"

    monkeypatch.setattr(ps, "owner_events_after", spy)
    monkeypatch.setattr(
        pa,
        "_read_v",
        lambda *_a, **_k: AccountValue(
            wallet=D("1000"),
            unrealized=D("0"),
            value=D("1000"),
            open_positions=0,
            read_at_ms=_now(),
        ),
    )
    monkeypatch.setattr("jarvise_assist.real_trades.fetch_all_income", lambda *_a, **_k: [])
    path = _make_db(tmp_path / "live.db")
    conn = open_db(path)
    try:
        now = _now()
        finding = Finding(
            code="transfer_in_unrecorded",
            fingerprint="x:1",
            time_ms=now - HOUR,
            amount=D("10"),
            partner_name=None,
            detail="",
            alertable=True,
        )
        late = pa._owner_safety_refuse(conn, object(), finding=finding, findings=[finding], now_ms=now)
        req = pa._owner_safety_refuse(conn, object(), finding=finding, findings=[finding], now_ms=now)
    finally:
        conn.close()
    assert late is not None and "มีรายการ partner หลังเงินโอนแล้ว" in late
    assert req is not None and "มีคำขอที่ยืนยันหลังเงินโอนแล้ว" in req
    assert seen == [False, False]


def _insert_event_shape(
    conn: Any,
    *,
    event_id: str,
    kind: str,
    note: str,
    created_at_ms: int,
    effective_at_ms: int | None,
) -> None:
    """Append one event. partner_event is append-only, so tests cannot UPDATE it."""
    base = conn.execute("SELECT * FROM partner_event ORDER BY created_at_ms LIMIT 1").fetchone()
    conn.execute(
        """
        INSERT INTO partner_event (
            id, partner_id, kind, request_id, related_event_id,
            account_value_micro, total_units_before_e8, nav,
            units_delta_e8, cost_delta_micro, cash_micro, fee_micro,
            units_after_e8, cost_after_micro, actor, note, created_at_ms,
            effective_at_ms, origin, payouts_due_micro
        ) VALUES (
            ?, ?, ?, NULL, NULL,
            ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, ?
        )
        """,
        (
            event_id,
            base["partner_id"],
            kind,
            base["account_value_micro"],
            base["total_units_before_e8"],
            base["nav"],
            base["units_delta_e8"],
            base["cost_delta_micro"],
            base["cash_micro"],
            base["fee_micro"],
            base["units_after_e8"],
            base["cost_after_micro"],
            base["actor"],
            note,
            int(created_at_ms),
            None if effective_at_ms is None else int(effective_at_ms),
            base["origin"],
            base["payouts_due_micro"],
        ),
    )
    conn.commit()


def test_r2_hist_marker_non_owner_still_blocks(tmp_path: Path) -> None:
    """A p-cost note with the historical marker is not judged by effective time."""
    now = _now()
    t = now - 2 * DAY
    path = _make_db(tmp_path / "cost-mark.db", init_ms=now - 10 * DAY)
    conn = open_db(path)
    try:
        admin_id = str(conn.execute("SELECT id FROM partner WHERE is_admin=1").fetchone()["id"])
        booked = ps.apply_adjust_cost(
            conn,
            admin_id,
            D("900"),
            f"p cost {ps.OWNER_HIST_NOTE_MARK}",
            D("1000"),
            now,
        )
        assert booked["ok"] is True, booked
        assert ps.owner_events_after(conn, t, historical=True) == "events_after_effective"
    finally:
        conn.close()

    spoof = _make_db(tmp_path / "cost-spoof.db", init_ms=now - 10 * DAY)
    conn = open_db(spoof)
    try:
        _insert_event_shape(
            conn,
            event_id="pe_cost_marker",
            kind="adjust_cost",
            note=f"p cost {ps.OWNER_HIST_NOTE_MARK}",
            created_at_ms=now,
            effective_at_ms=t - DAY,
        )
        row = conn.execute("SELECT * FROM partner_event WHERE id='pe_cost_marker'").fetchone()
        assert row["kind"] == "adjust_cost"
        assert int(row["effective_at_ms"]) < t < int(row["created_at_ms"])
        assert ps.owner_events_after(conn, t, historical=True) == "events_after_effective"
    finally:
        conn.close()


def test_r2_hist_owner_null_effective_blocks_on_created(tmp_path: Path) -> None:
    from jarvise_assist.partner_owner_history import owner_hist_note

    now = _now()
    t = now - DAY
    tran = "900000000099"
    path = _make_db(tmp_path / "owner-null.db", init_ms=now - 10 * DAY)
    conn = open_db(path)
    try:
        eid = pa._owner_event_id(tran)
        booked = ps.apply_owner_deposit(
            conn,
            amount=D("10"),
            account_value=D("1010"),
            now_ms=now,
            effective_at_ms=now - 3 * DAY,
            note=owner_hist_note(tran),
            event_id=eid,
            refuse_events_after_ms=now - 3 * DAY,
        )
        assert booked["ok"] is True, booked
        # The booked row's effective time is before t, so it is not the block.
        assert ps.owner_events_after(conn, t, historical=True) is None
        null_id = pa._owner_event_id("900000000098")
        _insert_event_shape(
            conn,
            event_id=null_id,
            kind="owner_deposit",
            note=owner_hist_note("900000000098"),
            created_at_ms=now,
            effective_at_ms=None,
        )
        row = conn.execute("SELECT * FROM partner_event WHERE id=?", (null_id,)).fetchone()
        assert row["kind"] == "owner_deposit"
        assert str(row["id"]).startswith("pe_owner_tran")
        assert row["effective_at_ms"] is None
        assert ps.OWNER_HIST_NOTE_MARK in str(row["note"])
        assert int(row["created_at_ms"]) > t
        assert ps.owner_events_after(conn, t, historical=True) == "events_after_effective"
    finally:
        conn.close()
