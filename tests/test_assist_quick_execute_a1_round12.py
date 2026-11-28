"""Quick Plan A1 fix round 12 — TP1 text, testnet levels, fapi reads, stop-far, card parity."""

from __future__ import annotations

import logging
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.test_assist_quick_execute_a1 import (
    RecordingTrade,
    _daytime_ms,
    _Filters,
    _FuturesRead,
    _insert_quoted_card,
    _quick_preview,
    _seed_quick_settings,
    _seed_settings,
)
from tests.test_assist_quick_execute_a1_round7 import _seed_unresolved_exec_reg

from jarvise_assist.execute import (
    TradeClientError,
    _apply_pct_levels,
    _confirm_guards_quick_entry,
    round_level_to_tick,
    run_entry_sequence,
)
from jarvise_assist.messages import fmt_qty
from jarvise_assist.quick import (
    REG_EXPIRE_GRACE_MS,
    REG_TTL_MS,
    UNRESOLVED_EXEC_KEEP_MS,
    create_quick_registration,
    sync_quick_trades,
)
from jarvise_assist.quick_card import (
    build_quick_card,
    handle_quick_text,
    parse_quick_command,
    stop_far_refusal_text,
)
from jarvise_assist.quick_config import get_quick_config, set_quick_config
from jarvise_assist.quick_errors import e_draft_stop_pct_range, e_parse_stop, e_stop_far
from jarvise_assist.quick_execute import MSG_QUICK_TP1_OFF, build_bot_plan, cfg_became_stricter
from jarvise_assist.quick_m1a1 import handle_quick_draft_text, handle_quick_trade_callback, msg_stop_far
from jarvise_assist.real_trades import _quick_owned_client_and_order_ids
from jarvise_assist.sizing import size_signal
from jarvise_assist.store import claim_exec_intent, get_exec_intent, insert_exec_intent, insert_exec_log
from jarvise_ingest.db import open_db

_TP_QTY_LINE = re.compile(r"TP1 \S+ ×")
_FLAG_OFF_CARD = (
    "⚡ Quick · BTCUSDT · LONG\n"
    "💰 เงินจริง · บอทไม่ส่งคำสั่ง\n"
    "เข้า (mark ตอนนี้): 100.00\n"
    "Stop: 99.00 (-1.00%)\n"
    "ขนาด: 44.642 BTC ≈ 4464.2 USDT\n"
    "ฐาน: Futures 10000 USDT × 0.5% = งบ 50 USDT\n"
    "risk จริง: −50 USDT = 0.50% ของ Futures 10000 USDT (เป้า 0.5%)\n"
    "Leverage ที่ต้องใช้: 1x · margin ≈ 4464.2 USDT\n"
    "ถ้าชน stop: −50 USDT ≈ −1.12R รวมค่าธรรมเนียม (ราคา −44.64 USDT = 1R · ค่าธรรมเนียม ≈ 5.36 USDT)\n"
    "ค่าธรรมเนียมไป-กลับ ≈ 0.12R ≈ 5.36 USDT (โมเดล 6 bps/ข้าง — ยังไม่อ่านค่าจริงจากบัญชี)\n"
    "เป้า 1R: 101.00 → สุทธิ +0.88R · ต้องชนะ ≥ 56% ถึงคุ้มทุน\n"
    "เป้า 1.5R: 101.50 → สุทธิ +1.38R · ต้องชนะ ≥ 45% ถึงคุ้มทุน\n"
    "ความเสี่ยงเปิดอยู่: หลัก 0% · quick 0% + ไม้นี้ 0.45% (ไม่รวมค่าธรรมเนียม) "
    "(เพดาน quick 1% · รวม 2%)\n"
    "การ์ดนี้ใช้ได้ถึง 12:22:00 (หมดแล้วกด 🔄 อัปเดตราคา) · "
    "บอทไม่ส่งคำสั่ง — กดลงทะเบียน แล้วเปิดเองใน Binance ภายใน 10 นาที\n"
    "ตั้ง Stop-Market (Reduce-Only, trigger = Mark Price) ทันทีหลังเข้า"
)
_WRITES = {
    "market_order",
    "place_algo_stop",
    "set_isolated",
    "set_leverage",
    "cancel_order",
    "cancel_algo_order",
    "cancel_algo",
}


def _db(tmp_path: Path, name: str) -> Any:
    conn = open_db(tmp_path / name)
    _seed_settings(conn)
    _seed_quick_settings(conn)
    return conn


def _bot(monkeypatch: pytest.MonkeyPatch, mode: str = "live") -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", mode)
    monkeypatch.setenv("JARVISE_QUICK_EXECUTE", "on")


def _kill_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )


def _open_quick(conn: Any, *, symbol: str, risk: str, now: int) -> None:
    conn.execute(
        """
        INSERT INTO assist_quick_trade (
            id, registration_id, symbol, side, status, risk_usdt, entry_time_ms, updated_at_ms
        ) VALUES (?, ?, ?, 'long', 'open', ?, ?, ?)
        """,
        (f"aqt-{symbol}", f"reg-{symbol}", symbol, risk, now, now),
    )
    conn.commit()


def _qk_ex(conn: Any, card: str, futures: Any, now: int) -> tuple[dict[str, Any], list[str]]:
    sent: list[str] = []
    out = handle_quick_trade_callback(
        conn,
        f"qk:ex:{card}",
        futures=futures,
        send=lambda text, **_k: sent.append(str(text)) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: True,
        now_ms=now,
        message={"message_id": 1, "chat": {"id": 1}},
    )
    return out, sent


class _TpTrade(RecordingTrade):
    def __init__(self, *, mode: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.mode = mode

    def place_algo_stop(self, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("order_type") == "TAKE_PROFIT_MARKET":
            if self.mode == "clear":
                raise TradeClientError("tp rejected", http_status=400, binance_code=-2021)
            raise TradeClientError("tp unknown", http_status=500, binance_code=-1001)
        return super().place_algo_stop(**kwargs)

    def find_algo_by_client_id(self, *, symbol: str, client_id: str) -> dict[str, Any] | None:
        if self.mode == "found" and str(client_id).endswith("-tp"):
            return {"algoId": "4242", "clientAlgoId": client_id, "symbol": symbol}
        return None


class _ReadBoom(RecordingTrade):
    def __init__(self, *, boom: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.boom = boom

    def mark_price(self, symbol: str) -> Decimal:
        self.calls.append(("mark_price", {"symbol": symbol}))
        if self.boom == "mark":
            raise TradeClientError("mark down", http_status=500, binance_code=-1001)
        if self.boom == "zero":
            return Decimal("0")
        return self.mark

    def symbol_filters(self, symbol: str) -> _Filters:
        self.calls.append(("symbol_filters", {"symbol": symbol}))
        if self.boom == "filters":
            raise TradeClientError("filters down", http_status=500, binance_code=-1001)
        return _Filters()


class _StepBook(_FuturesRead):
    def __init__(self, *, step: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._step = Decimal(step)

    def symbol_filters(self, symbol: str) -> _Filters:
        filters = super().symbol_filters(symbol)
        filters.step_size = self._step
        filters.min_qty = self._step
        return filters


def _dragged_stop(mark: Decimal) -> Decimal:
    mapped, _t1 = _apply_pct_levels(
        mainnet_entry=Decimal("100"),
        mainnet_stop=Decimal("99"),
        mainnet_target=Decimal("101"),
        testnet_mark=mark,
        side="long",
    )
    return round_level_to_tick(mapped, Decimal("0.01"), side="long", kind="stop")


def _guard_intent(conn: Any, ts: int, **extra: Any) -> Any:
    preview = _quick_preview(**extra)
    iid = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview=preview,
        created_at_ms=ts,
        expires_at_ms=ts + 30_000,
    )
    claim_exec_intent(conn, iid)
    intent = get_exec_intent(conn, iid)
    assert intent is not None
    return iid, intent


def _run_fill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, mode: str, avg: Decimal, tp1: str) -> str:
    _bot(monkeypatch)
    conn = _db(tmp_path, f"tp-{mode}-{avg}.db")
    ts = _daytime_ms()
    _iid, _intent = _guard_intent(conn, ts, qty="0.2", tp1pct=tp1)
    trade = _TpTrade(mode=mode, mark=Decimal("100"), fill_qty=Decimal("0.2"), fill_avg=avg)
    text = run_entry_sequence(conn, _iid, trade=trade, origin="quick", sleep=lambda _s: None)
    conn.close()
    return str(text)


@pytest.mark.parametrize("avg", [Decimal("100"), Decimal("0")], ids=["avg_known", "avg_unknown"])
@pytest.mark.parametrize("mode", ["clear", "missing"])
def test_r12_tp1_failure_says_no_tp1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, avg: Decimal
) -> None:
    text = _run_fill(tmp_path, monkeypatch, mode=mode, avg=avg, tp1="50")
    assert "ไม่มี TP1" in text
    assert "วาง TP1 ไม่ได้" in text
    assert _TP_QTY_LINE.search(text) is None
    if avg > 0:
        assert "fill ราคายังไม่ยืนยัน" not in text
    else:
        assert "fill ราคายังไม่ยืนยัน" in text


@pytest.mark.parametrize("avg", [Decimal("100"), Decimal("0")], ids=["avg_known", "avg_unknown"])
def test_r12_tp1_found_after_error_shows_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, avg: Decimal
) -> None:
    text = _run_fill(tmp_path, monkeypatch, mode="found", avg=avg, tp1="50")
    assert _TP_QTY_LINE.search(text)
    assert "วาง TP1 ไม่ได้" not in text
    assert "ไม่มี TP1" not in text


@pytest.mark.parametrize("avg", [Decimal("100"), Decimal("0")], ids=["avg_known", "avg_unknown"])
def test_r12_tp1_off_uses_shared_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, avg: Decimal
) -> None:
    text = _run_fill(tmp_path, monkeypatch, mode="clear", avg=avg, tp1="0")
    assert MSG_QUICK_TP1_OFF in text
    assert "ปิดครึ่ง 0%" not in text


def test_r12_testnet_levels_skip_second_remap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _bot(monkeypatch, "testnet")
    _kill_off(monkeypatch)
    conn = _db(tmp_path, "tn-skip.db")
    ts = _daytime_ms()
    mark = Decimal("100.2")
    dragged = _dragged_stop(mark)
    assert dragged != Decimal("99")
    _iid, intent = _guard_intent(conn, ts, testnet_levels=True)
    trade = RecordingTrade(mark=mark, balance=Decimal("10000"))
    guard = _confirm_guards_quick_entry(conn, intent, trade, now_ms_value=ts + 1_000)
    assert guard is None, guard
    updated = get_exec_intent(conn, _iid)
    assert updated is not None
    assert Decimal(str(updated["preview"]["stop"])) == Decimal("99")
    assert Decimal(str(updated["preview"]["stop"])) != dragged
    conn.close()


def test_r12_testnet_without_flag_still_remaps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _bot(monkeypatch, "testnet")
    _kill_off(monkeypatch)
    conn = _db(tmp_path, "tn-remap.db")
    ts = _daytime_ms()
    mark = Decimal("100.2")
    _iid, intent = _guard_intent(conn, ts)
    trade = RecordingTrade(mark=mark, balance=Decimal("10000"))
    guard = _confirm_guards_quick_entry(conn, intent, trade, now_ms_value=ts + 1_000)
    assert guard is None, guard
    updated = get_exec_intent(conn, _iid)
    assert updated is not None
    assert Decimal(str(updated["preview"]["stop"])) == _dragged_stop(mark)
    conn.close()


def test_r12_qk_ex_testnet_sizes_from_demo_book(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _bot(monkeypatch, "testnet")
    conn = _db(tmp_path, "tn-size.db")
    ts = _daytime_ms()
    card = _insert_quoted_card(conn, now_ms_value=ts, entry=100.0, stop=99.0, qty=0.05)
    mainnet = _StepBook(step="0.001", mark=Decimal("100"), balance=Decimal("10000"))
    demo = _StepBook(step="1", mark=Decimal("100"), balance=Decimal("2000"), available=Decimal("2000"))
    monkeypatch.setattr("jarvise_assist.execute.build_trade_client", lambda: (demo, None))
    out, sent = _qk_ex(conn, card, mainnet, ts)
    assert out.get("ok") is True, (out, sent)
    stored = get_exec_intent(conn, str(out["intent_id"]))
    assert stored is not None
    demo_plan = build_bot_plan(
        conn,
        symbol="BTCUSDT",
        side="long",
        entry=Decimal("100"),
        stop=Decimal("99"),
        balance=Decimal("2000"),
        available=Decimal("2000"),
        step=Decimal("1"),
        tick=Decimal("0.01"),
        min_qty=Decimal("1"),
        min_notional=Decimal("5"),
        now_ms=ts,
    )
    main_plan = build_bot_plan(
        conn,
        symbol="BTCUSDT",
        side="long",
        entry=Decimal("100"),
        stop=Decimal("99"),
        balance=Decimal("10000"),
        available=Decimal("10000"),
        step=Decimal("0.001"),
        tick=Decimal("0.01"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("5"),
        now_ms=ts,
    )
    assert demo_plan is not None and main_plan is not None
    assert demo_plan["qty"] != main_plan["qty"]
    assert Decimal(str(stored["preview"]["qty"])) == demo_plan["qty"]
    conn.close()


def test_r12_qk_ex_live_ignores_trade_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _bot(monkeypatch, "live")
    conn = _db(tmp_path, "live-size.db")
    ts = _daytime_ms()
    card = _insert_quoted_card(conn, now_ms_value=ts, entry=100.0, stop=99.0, qty=0.05)
    mainnet = _FuturesRead(mark=Decimal("100"), balance=Decimal("10000"))

    def _boom() -> Any:
        raise AssertionError("live qk:ex must size from the mainnet read client")

    monkeypatch.setattr("jarvise_assist.execute.build_trade_client", _boom)
    out, sent = _qk_ex(conn, card, mainnet, ts)
    assert out.get("ok") is True, (out, sent)
    stored = get_exec_intent(conn, str(out["intent_id"]))
    assert stored is not None
    plan = build_bot_plan(
        conn,
        symbol="BTCUSDT",
        side="long",
        entry=Decimal("100"),
        stop=Decimal("99"),
        balance=Decimal("10000"),
        available=Decimal("10000"),
        step=Decimal("0.001"),
        tick=Decimal("0.01"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("5"),
        now_ms=ts,
    )
    assert plan is not None
    assert Decimal(str(stored["preview"]["qty"])) == plan["qty"]
    conn.close()


@pytest.mark.parametrize(
    ("boom", "step"),
    [("mark", "mark"), ("filters", "filters"), ("zero", "mark")],
)
def test_r12_confirm_read_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    boom: str,
    step: str,
) -> None:
    from jarvise_assist.execute import handle_exec_callback

    _bot(monkeypatch)
    _kill_off(monkeypatch)
    caplog.set_level(logging.ERROR)
    conn = _db(tmp_path, f"read-{boom}.db")
    ts = _daytime_ms()
    preview = _quick_preview()
    iid = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview=preview,
        created_at_ms=ts,
        expires_at_ms=ts + 30_000,
    )
    trade = _ReadBoom(boom=boom, mark=Decimal("100"))
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
        now_ms_value=ts + 1_000,
        message={"message_id": 77},
    )
    assert out.get("reason") == f"fapi_read_failed:{step}"
    assert "exec callback failed after claim" not in caplog.text
    assert any(item["markup"] == {"inline_keyboard": []} for item in edits)
    assert any(f"อ่านบัญชีไม่ได้ ({step})" in str(item["text"]) for item in edits)
    assert not [c for c in trade.calls if c[0] in _WRITES]
    again = handle_exec_callback(
        conn,
        f"ey:{iid}",
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=edit,
        trade=trade,
        now_ms_value=ts + 1_000,
        message={"message_id": 77},
    )
    assert again.get("reason") != "handler_error"
    assert not [c for c in trade.calls if c[0] in _WRITES]
    row = get_exec_intent(conn, iid)
    assert row is not None and row["status"] == "failed"
    conn.close()


def test_r12_stop_far_default_text_unchanged(tmp_path: Path) -> None:
    conn = _db(tmp_path, "far-default.db")
    ts = _daytime_ms()
    assert stop_far_refusal_text(conn).startswith("❌ stop ห่างเกิน 20%")
    assert "20%" in msg_stop_far(conn)
    set_quick_config(conn, "quick_stop_far_pct", "15", source="test", now_ms=ts)
    assert "15%" in stop_far_refusal_text(conn)
    assert "15%" in msg_stop_far(conn)
    assert "20%" not in stop_far_refusal_text(conn)
    assert "20%" not in msg_stop_far(conn)
    conn.close()
    assert "เกิน 20%" in e_stop_far(Decimal("25"))
    assert "น้อยกว่า 20" in e_parse_stop("25%")
    assert "น้อยกว่า 20%" in e_draft_stop_pct_range("25.00")
    parsed = parse_quick_command("q btc long 25%")
    assert parsed is not None and parsed.get("error") == "usage"
    allowed = parse_quick_command("q btc long 15%")
    assert allowed is not None and allowed.get("error") is None


def test_r12_stop_far_follows_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _bot(monkeypatch)
    conn = _db(tmp_path, "far.db")
    ts = _daytime_ms()
    fut = _FuturesRead(mark=Decimal("100"), balance=Decimal("10000"))
    set_quick_config(conn, "quick_stop_far_pct", "10", source="test", now_ms=ts)
    tight = build_quick_card(
        conn, fut, symbol="BTCUSDT", side="long", stop=Decimal("85"), now_ms=ts, enforce_stop_far=True
    )
    assert tight.get("ok") is False
    assert "10%" in str(tight.get("text"))
    sent: list[str] = []
    typed = handle_quick_text(
        conn, "q btc long 15%", futures=fut, send=lambda text, **_k: sent.append(str(text)) or True, now_ms=ts
    )
    assert typed is not None and typed.get("ok") is False
    # Percent tokens use the parse sentence ("น้อยกว่า {limit}"), not the distance "เกิน N%" line.
    assert "น้อยกว่า 10" in "\n".join(sent)
    set_quick_config(conn, "quick_stop_far_pct", "30", source="test", now_ms=ts)
    wide = build_quick_card(
        conn, fut, symbol="BTCUSDT", side="long", stop=Decimal("75"), now_ms=ts, enforce_stop_far=True
    )
    assert wide.get("ok") is True, wide
    sent.clear()
    typed_ok = handle_quick_text(
        conn, "q btc long 25%", futures=fut, send=lambda text, **_k: sent.append(str(text)) or True, now_ms=ts
    )
    assert typed_ok is not None and typed_ok.get("ok") is True
    assert "เกิน 20%" not in "\n".join(sent)
    conn.close()


def _draft(conn: Any, now: int) -> None:
    conn.execute(
        """
        INSERT INTO assist_quick_card (
            id, side, symbol, status, message_id, created_at_ms, updated_at_ms
        ) VALUES ('draft12', 'long', 'BTCUSDT', 'pick_stop', 7, ?, ?)
        """,
        (now, now),
    )
    conn.commit()


def _draft_text(conn: Any, token: str, fut: Any, now: int) -> tuple[dict[str, Any] | None, str]:
    texts: list[str] = []

    def send(text: str, **_k: Any) -> int:
        texts.append(str(text))
        return 1

    def edit(*args: Any, **_k: Any) -> bool:
        if len(args) >= 2:
            texts.append(str(args[1]))
        return True

    out = handle_quick_draft_text(conn, token, futures=fut, send=send, edit=edit, now_ms=now)
    return out, "\n".join(texts)


def test_r12_draft_stop_far_follows_config(tmp_path: Path) -> None:
    conn = _db(tmp_path, "draft-far.db")
    ts = _daytime_ms()
    fut = _FuturesRead(mark=Decimal("100"), balance=Decimal("10000"))
    _draft(conn, ts)
    refused, text = _draft_text(conn, "75", fut, ts)
    assert refused is not None and refused.get("rejected") is True
    assert "20%" in text
    set_quick_config(conn, "quick_stop_far_pct", "30", source="test", now_ms=ts)
    conn.execute("UPDATE assist_quick_card SET status='pick_stop', updated_at_ms=? WHERE id='draft12'", (ts,))
    conn.commit()
    quoted, _text = _draft_text(conn, "75", fut, ts)
    assert quoted is not None and quoted.get("quoted") is True
    conn.execute("UPDATE assist_quick_card SET status='pick_stop', updated_at_ms=? WHERE id='draft12'", (ts,))
    conn.commit()
    set_quick_config(conn, "quick_stop_far_pct", "10", source="test", now_ms=ts)
    rejected, pct_text = _draft_text(conn, "15%", fut, ts)
    assert rejected is not None and rejected.get("rejected") is True
    assert "10%" in pct_text
    conn.close()


def test_r12_far_snapshot_and_plan_refuses(tmp_path: Path) -> None:
    conn = _db(tmp_path, "far-plan.db")
    ts = _daytime_ms()
    kwargs = dict(
        symbol="BTCUSDT",
        side="long",
        entry=Decimal("100"),
        balance=Decimal("10000"),
        available=Decimal("10000"),
        step=Decimal("0.001"),
        tick=Decimal("0.01"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("5"),
        now_ms=ts,
    )
    near = build_bot_plan(conn, stop=Decimal("99"), **kwargs)
    assert near is not None
    assert "quick_stop_far_pct" in near["cfg"]
    frozen = dict(near["cfg"])
    live_tight = dict(get_quick_config(conn).values)
    live_tight["quick_stop_far_pct"] = Decimal("10")
    assert cfg_became_stricter(frozen, live_tight) is True
    live_wide = dict(get_quick_config(conn).values)
    live_wide["quick_stop_far_pct"] = Decimal("30")
    assert cfg_became_stricter(frozen, live_wide) is False
    assert build_bot_plan(conn, stop=Decimal("75"), **kwargs) is None
    set_quick_config(conn, "quick_stop_far_pct", "30", source="test", now_ms=ts)
    assert build_bot_plan(conn, stop=Decimal("75"), **kwargs) is not None
    conn.close()


def test_r12_qk_ex_stop_far_uses_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _bot(monkeypatch)
    conn = _db(tmp_path, "qk-far.db")
    ts = _daytime_ms()
    fut = _FuturesRead(mark=Decimal("100"), balance=Decimal("10000"))
    far = _insert_quoted_card(conn, now_ms_value=ts, entry=100.0, stop=75.0)
    out, sent = _qk_ex(conn, far, fut, ts)
    assert out.get("ok") is False
    assert "เกิน 20%" in "\n".join(sent)
    set_quick_config(conn, "quick_stop_far_pct", "10", source="test", now_ms=ts)
    mid = _insert_quoted_card(conn, symbol="ETHUSDT", now_ms_value=ts, entry=100.0, stop=85.0)
    out10, sent10 = _qk_ex(conn, mid, fut, ts)
    assert out10.get("ok") is False
    assert "10%" in "\n".join(sent10)
    set_quick_config(conn, "quick_stop_far_pct", "30", source="test", now_ms=ts)
    wide = _insert_quoted_card(conn, symbol="SOLUSDT", now_ms_value=ts + 1, entry=100.0, stop=75.0)
    out30, sent30 = _qk_ex(conn, wide, fut, ts + 1)
    assert out30.get("ok") is True, (out30, sent30)
    conn.close()


def test_r12_flag_off_card_byte_identical(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVISE_QUICK_REG_TTL_MS", raising=False)
    monkeypatch.delenv("JARVISE_QUICK_EXECUTE", raising=False)
    monkeypatch.delenv("JARVISE_ASSIST_EXECUTE", raising=False)
    conn = _db(tmp_path, "flag-off.db")
    fut = _FuturesRead(mark=Decimal("100"), balance=Decimal("10000"), available=Decimal("10000"))
    out = build_quick_card(
        conn, fut, symbol="BTCUSDT", side="long", stop=Decimal("99"), now_ms=1_781_500_800_000
    )
    assert out.get("ok") is True
    assert out.get("text") == _FLAG_OFF_CARD
    conn.close()


def test_r12_bot_preview_clamps_leverage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _bot(monkeypatch)
    conn = _db(tmp_path, "lev.db")
    ts = _daytime_ms()
    set_quick_config(conn, "quick_max_leverage", "1", source="test", now_ms=ts)
    fut = _FuturesRead(mark=Decimal("100"), balance=Decimal("10000"))
    raw = size_signal(
        symbol="BTCUSDT",
        side="long",
        entry=Decimal("100"),
        stop=Decimal("99.90"),
        balance=Decimal("10000"),
        risk_pct=Decimal("0.25"),
        step_size=Decimal("0.001"),
        fee_bps_per_side=Decimal("6"),
    )
    plan = build_bot_plan(
        conn,
        symbol="BTCUSDT",
        side="long",
        entry=Decimal("100"),
        stop=Decimal("99.90"),
        balance=Decimal("10000"),
        available=Decimal("10000"),
        step=Decimal("0.001"),
        tick=Decimal("0.01"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("5"),
        now_ms=ts,
    )
    assert raw is not None and plan is not None
    assert plan["qty"] != raw.qty
    card = build_quick_card(conn, fut, symbol="BTCUSDT", side="long", stop=Decimal("99.90"), now_ms=ts)
    assert card.get("ok") is True, card
    bot = [ln for ln in str(card["text"]).splitlines() if "ไม่ทราบเทรนด์" in ln]
    assert bot
    assert fmt_qty(plan["qty"]) in bot[0]
    assert fmt_qty(raw.qty) not in bot[0]
    conn.close()


def test_r12_bot_preview_combined_uses_counter_risk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _bot(monkeypatch)
    conn = _db(tmp_path, "warn.db")
    ts = _daytime_ms()
    set_quick_config(conn, "quick_risk_cap_pct", "2", source="test", now_ms=ts)
    set_quick_config(conn, "quick_combined_risk_warn_pct", "1", source="test", now_ms=ts)
    _open_quick(conn, symbol="ETHUSDT", risk="70", now=ts)
    fut = _FuturesRead(mark=Decimal("100"), balance=Decimal("10000"))
    card = build_quick_card(conn, fut, symbol="BTCUSDT", side="long", stop=Decimal("99"), now_ms=ts)
    assert card.get("ok") is True, card
    text = str(card["text"])
    assert "ไม่ทราบเทรนด์" in text
    assert "ความเสี่ยงทางเดียวกัน" not in text
    conn.close()


def test_r12_qk_ex_accepts_counter_near_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _bot(monkeypatch)
    conn = _db(tmp_path, "cap.db")
    ts = _daytime_ms()
    _open_quick(conn, symbol="ETHUSDT", risk="70", now=ts)
    fut = _FuturesRead(mark=Decimal("100"), balance=Decimal("10000"))
    card = _insert_quoted_card(conn, now_ms_value=ts, entry=100.0, stop=99.0)
    out, sent = _qk_ex(conn, card, fut, ts)
    joined = "\n".join(sent)
    assert out.get("ok") is True, (out, joined)
    assert "ยืนยันบอทเข้าไม้" in joined
    assert "เพดาน" not in joined
    conn.close()


def test_r12_qk_ex_refuses_counter_over_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Record-only quick_cap bypass still re-checks the cap on the counter-trend bot plan."""
    _bot(monkeypatch)
    conn = _db(tmp_path, "capover.db")
    ts = _daytime_ms()
    _open_quick(conn, symbol="ETHUSDT", risk="90", now=ts)
    fut = _FuturesRead(mark=Decimal("100"), balance=Decimal("10000"))
    card = _insert_quoted_card(conn, now_ms_value=ts, entry=100.0, stop=99.0)
    out, sent = _qk_ex(conn, card, fut, ts)
    joined = "\n".join(sent)
    assert out.get("ok") is False, (out, joined)
    assert out.get("reason") in {"quick_cap", "portfolio_cap"}, out
    assert "ยืนยันบอทเข้าไม้" not in joined
    conn.close()


def test_r12_qk_ex_refuses_fee_r(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _bot(monkeypatch)
    conn = _db(tmp_path, "fee.db")
    ts = _daytime_ms()
    fut = _FuturesRead(mark=Decimal("100"), balance=Decimal("10000"))
    card = _insert_quoted_card(conn, now_ms_value=ts, entry=100.0, stop=99.7)
    out, sent = _qk_ex(conn, card, fut, ts)
    joined = "\n".join(sent)
    assert out.get("ok") is False
    assert "ค่าธรรมเนียมเทียบระยะ stop สูงเกินไป" in joined
    assert "ยืนยันบอทเข้าไม้" not in joined
    conn.close()


def test_r12_qk_ex_refuses_stop_buffer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _bot(monkeypatch)
    conn = _db(tmp_path, "buf.db")
    ts = _daytime_ms()
    fut = _FuturesRead(mark=Decimal("100"), balance=Decimal("10000"))
    card = _insert_quoted_card(conn, now_ms_value=ts, entry=100.0, stop=99.95)
    out, sent = _qk_ex(conn, card, fut, ts)
    joined = "\n".join(sent)
    assert out.get("ok") is False
    assert "stop ใกล้ราคาเกินไป" in joined
    assert "ค่าธรรมเนียมเทียบระยะ stop สูงเกินไป" not in joined
    assert "ยืนยันบอทเข้าไม้" not in joined
    conn.close()


def test_r12_quick_id_sets_leave_main_out(tmp_path: Path) -> None:
    conn = _db(tmp_path, "ids.db")
    ts = _daytime_ms()
    quick_id = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview={"origin": "quick", "qty": "0.1"},
        created_at_ms=ts,
        expires_at_ms=ts + 30_000,
    )
    main_id = insert_exec_intent(
        conn,
        kind="entry",
        symbol="ETHUSDT",
        side="long",
        preview={"qty": "0.1"},
        created_at_ms=ts,
        expires_at_ms=ts + 30_000,
    )
    insert_exec_log(
        conn,
        intent_id=quick_id,
        step="market",
        endpoint="/fapi/v1/order",
        client_order_id="jv-quick-mkt",
        order_or_algo_id="111",
        http_status=200,
        created_at_ms=ts,
    )
    insert_exec_log(
        conn,
        intent_id=quick_id,
        step="tp",
        endpoint="/fapi/v1/algoOrder",
        client_order_id="jv-quick-tp",
        order_or_algo_id="333",
        http_status=200,
        created_at_ms=ts,
    )
    insert_exec_log(
        conn,
        intent_id=main_id,
        step="market",
        endpoint="/fapi/v1/order",
        client_order_id="jv-main-mkt",
        order_or_algo_id="222",
        http_status=200,
        created_at_ms=ts,
    )
    rid, reason = create_quick_registration(
        conn,
        symbol="BTCUSDT",
        side="long",
        planned_entry=100.0,
        planned_stop=99.0,
        qty=0.1,
        now_ms=ts,
        exec_intent_id=quick_id,
    )
    assert rid is not None and reason is None
    cids, order_ids, algo_ids = _quick_owned_client_and_order_ids(conn)
    assert cids == {"jv-quick-mkt", "jv-quick-tp"}
    assert order_ids == {("BTCUSDT", "111")}
    assert algo_ids == {("BTCUSDT", "333")}
    assert "jv-main-mkt" not in cids
    assert ("ETHUSDT", "222") not in order_ids
    conn.close()


class _Flat:
    def position_risk(self, symbol: str | None = None) -> list[Any]:
        return []


def _sync_notice(conn: Any, now: int, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.setattr("jarvise_assist.quick.fetch_all_user_trades", lambda *_a, **_k: [])
    monkeypatch.setattr("jarvise_assist.quick.fetch_all_orders", lambda *_a, **_k: [])
    sent: list[str] = []
    sync_quick_trades(conn, _Flat(), now_ms=now, send=lambda text, **_k: sent.append(str(text)) or True)
    return sent


def test_r12_expire_notice_default_ten_minutes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVISE_QUICK_REG_TTL_MS", raising=False)
    conn = _db(tmp_path, "exp10.db")
    created = _daytime_ms()
    rid, reason = create_quick_registration(
        conn,
        symbol="BTCUSDT",
        side="long",
        planned_entry=100.0,
        planned_stop=99.0,
        qty=0.1,
        now_ms=created,
    )
    assert rid is not None and reason is None
    row = conn.execute(
        "SELECT created_at_ms, expires_at_ms FROM assist_quick_registration WHERE id=?", (rid,)
    ).fetchone()
    assert int(row["expires_at_ms"]) - int(row["created_at_ms"]) == REG_TTL_MS
    sent = _sync_notice(conn, int(row["expires_at_ms"]) + REG_EXPIRE_GRACE_MS + 1, monkeypatch)
    assert any("ภายใน 10 นาที" in text for text in sent)
    conn.close()


def test_r12_expire_notice_uses_registration_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _db(tmp_path, "exp15.db")
    created = _daytime_ms()
    rid, reason = create_quick_registration(
        conn,
        symbol="BTCUSDT",
        side="long",
        planned_entry=100.0,
        planned_stop=99.0,
        qty=0.1,
        now_ms=created,
    )
    assert rid is not None and reason is None
    window = 15 * 60_000
    expires = created + window
    conn.execute(
        "UPDATE assist_quick_registration SET expires_at_ms=? WHERE id=?",
        (expires, rid),
    )
    conn.commit()
    sent = _sync_notice(conn, expires + REG_EXPIRE_GRACE_MS + 1, monkeypatch)
    joined = "\n".join(sent)
    assert "ภายใน 15 นาที" in joined
    assert "ภายใน 10 นาที" not in joined
    conn.close()


def test_r12_expire_notice_unresolved_keep_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _db(tmp_path, "exp2h.db")
    created = _daytime_ms()
    _seed_unresolved_exec_reg(conn, created=created)
    sent = _sync_notice(conn, created + UNRESOLVED_EXEC_KEEP_MS + 1, monkeypatch)
    joined = "\n".join(sent)
    assert "ภายใน 2 ชม." in joined
    assert "10 นาที" not in joined
    conn.close()


def test_r12_expire_notice_known_order_uses_registration_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exec-linked reg with a logged market orderId, flat, no fills: real window in the notice."""
    conn = _db(tmp_path, "expknown.db")
    created = _daytime_ms()
    _iid, rid, _mkt = _seed_unresolved_exec_reg(conn, created=created, order_id_logged="777001")
    window = 15 * 60_000
    expires = created + window
    conn.execute("UPDATE assist_quick_registration SET expires_at_ms=? WHERE id=?", (expires, rid))
    conn.commit()
    sent = _sync_notice(conn, expires + REG_EXPIRE_GRACE_MS + 1, monkeypatch)
    joined = "\n".join(sent)
    status = conn.execute("SELECT status FROM assist_quick_registration WHERE id=?", (rid,)).fetchone()
    assert str(status["status"]) == "expired", joined
    assert "ภายใน 15 นาที" in joined
    assert "ภายใน 10 นาที" not in joined
    conn.close()
