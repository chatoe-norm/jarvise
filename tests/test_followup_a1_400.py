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
from jarvise_assist.quick_card import CARD_TTL_PREFIX, REFUSAL, build_quick_card
from jarvise_assist.quick_execute import BTN_QUICK_BOT_ENTER
from jarvise_assist.quick_m1a1 import MSG_STOP_FAR, _quoted_markup_for_card, handle_quick_trade_callback
from jarvise_assist.real_trades import _quick_owned_client_and_order_ids
from jarvise_assist.sizing import expected_analysis_bar_ms
from jarvise_assist.store import get_exec_intent, insert_exec_intent, insert_exec_log
from jarvise_ingest.db import open_db

D = Decimal
ROOT = Path(__file__).resolve().parents[1]
runner = CliRunner()
RECORD_TAIL = "บอทไม่ส่งคำสั่ง — กดลงทะเบียน แล้วเปิดเองใน Binance ภายใน "
BOT_TAIL = "⚡ = บอทส่งคำสั่งหลังยืนยัน · กดลงทะเบียน = เปิดเองใน Binance ภายใน "
NO_TP1 = "ไม่วาง TP1 — ไม้ครึ่งต่ำกว่า minQty/minNotional, SL คุมเต็มจำนวน"


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


def test_a1_dust_tp_note_uses_shared_constant() -> None:
    src = (ROOT / "src" / "jarvise_assist" / "execute.py").read_text(encoding="utf-8")
    assert NO_TP1 not in src
    assert "MSG_QUICK_NO_TP1" in src


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


def test_a1_stop_far_constants_follow_call_time_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jarvise_assist.quick_card import stop_far_refusal_text
    from jarvise_assist.quick_config import set_quick_config
    from jarvise_assist.quick_m1a1 import msg_stop_far

    conn = _db(tmp_path, "far.db")
    set_quick_config(conn, "quick_stop_far_pct", "12", source="test", now_ms=_daytime_ms())
    assert "12%" in stop_far_refusal_text(conn)
    assert "12%" in msg_stop_far(conn)
    assert "20%" not in stop_far_refusal_text(conn)
    conn.close()

    monkeypatch.setattr(
        "jarvise_assist.quick_config.stop_far_limit",
        lambda conn=None: D("12"),
    )
    assert "12%" in REFUSAL["stop_far"]
    assert "12%" in MSG_STOP_FAR


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
