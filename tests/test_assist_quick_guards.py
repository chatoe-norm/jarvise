"""QPR1b: main-side quick guards — pin (P) vs new-behaviour (N) tests."""

from __future__ import annotations

import json
import re
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.test_assist import FakeFutures, _ms_bangkok, _seed_analysis
from tests.test_assist_execute import (
    RecordingTransport,
    _client,
    _forbid_trade_client,
)
from tests.test_assist_listen import _callback, _send_capture

from jarvise_assist.execute import MSG_OFF, _confirm_guards_entry
from jarvise_assist.futures import FuturesPosition
from jarvise_assist.keyboard import skip_inline_keyboard
from jarvise_assist.listen import process_update
from jarvise_assist.menu import render_anything_now
from jarvise_assist.quick import create_quick_registration
from jarvise_assist.signals import build_existing_ideas, run_assist_morning, run_assist_signals
from jarvise_assist.sizing import next_assist_run_ms, size_signal
from jarvise_assist.store import (
    get_exec_intent,
    insert_exec_intent,
    insert_signal,
    insert_watch_advice,
    upsert_watch_position,
)
from jarvise_assist.watch import flush_held_watch_advice, run_assist_watch
from jarvise_ingest.db import open_db

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ID_RE = re.compile(
    r"\b(?:as_|aei_|awa_|aqr_|aqt_|awp_)[0-9a-f]{8,}\b",
    re.IGNORECASE,
)


def _canon(obj: Any) -> Any:
    """Replace generated ids so two runs can be compared."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if k in {"id", "intent_id", "signal_id", "preview"} and isinstance(v, str) and _ID_RE.search(v):
                out[k] = "<ID>"
            else:
                out[k] = _canon(v)
        return out
    if isinstance(obj, list):
        return [_canon(x) for x in obj]
    if isinstance(obj, str):
        return _ID_RE.sub("<ID>", obj)
    return obj


def _pos(
    symbol: str,
    *,
    amt: str = "0.01",
    entry: str = "100",
    mark: str = "100",
) -> FuturesPosition:
    return FuturesPosition(
        symbol=symbol,
        position_side="BOTH",
        position_amt=Decimal(amt),
        entry_price=Decimal(entry),
        mark_price=Decimal(mark),
        update_time_ms=1,
    )


def _btc_eth_positions() -> list[FuturesPosition]:
    return [
        _pos("BTCUSDT", amt="0.01", entry="100", mark="100"),
        _pos("ETHUSDT", amt="0.1", entry="50", mark="50"),
    ]


def _stop_order(symbol: str, *, stop: str, qty: str, side: str = "SELL") -> dict[str, Any]:
    return {
        "symbol": symbol,
        "side": side,
        "type": "STOP_MARKET",
        "reduceOnly": True,
        "positionSide": "BOTH",
        "stopPrice": stop,
        "origQty": qty,
    }


def _patch_sync_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    import jarvise_assist.watch as watch_mod

    monkeypatch.setattr(watch_mod, "sync_quick_trades", lambda *_a, **_k: {})


def _insert_quick_trade(
    conn: Any,
    *,
    symbol: str = "BTCUSDT",
    side: str = "long",
    status: str = "open",
    now_ms: int,
    entry_qty: float = 0.01,
    entry_avg: float = 100.0,
    planned_stop: float = 90.0,
) -> str:
    rid = f"aqr_{uuid.uuid4().hex[:16]}"
    tid = f"aqt_{uuid.uuid4().hex[:16]}"
    conn.execute(
        """
        INSERT INTO assist_quick_registration (
            id, symbol, side, planned_entry, planned_stop, qty,
            created_at_ms, expires_at_ms, status, trade_id, updated_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'tagged', ?, ?)
        """,
        (
            rid,
            symbol.upper(),
            side,
            entry_avg,
            planned_stop,
            entry_qty,
            now_ms,
            now_ms + 600_000,
            tid,
            now_ms,
        ),
    )
    conn.execute(
        """
        INSERT INTO assist_quick_trade (
            id, registration_id, symbol, side, planned_stop, entry_time_ms,
            entry_qty, entry_avg, status, updated_at_ms, risk_usdt
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            tid,
            rid,
            symbol.upper(),
            side,
            planned_stop,
            now_ms - 60_000,
            entry_qty,
            entry_avg,
            status,
            now_ms,
            abs(entry_avg - planned_stop) * entry_qty,
        ),
    )
    if status == "closed":
        conn.execute(
            "UPDATE assist_quick_trade SET closed_at_ms=?, exit_avg=? WHERE id=?",
            (now_ms - 1_000, entry_avg, tid),
        )
    conn.commit()
    return tid


def _insert_quick_registration(
    conn: Any,
    *,
    symbol: str = "BTCUSDT",
    side: str = "long",
    status: str = "open",
    now_ms: int,
) -> str:
    rid = f"aqr_{uuid.uuid4().hex[:16]}"
    conn.execute(
        """
        INSERT INTO assist_quick_registration (
            id, symbol, side, planned_entry, planned_stop, qty,
            created_at_ms, expires_at_ms, status, trade_id, updated_at_ms
        ) VALUES (?, ?, ?, 100.0, 90.0, 0.01, ?, ?, ?, NULL, ?)
        """,
        (rid, symbol.upper(), side, now_ms, now_ms + 600_000, status, now_ms),
    )
    conn.commit()
    return rid


def _seed_non_open_quick(conn: Any, *, now_ms: int) -> None:
    _insert_quick_trade(conn, symbol="BTCUSDT", status="closed", now_ms=now_ms)
    _insert_quick_registration(conn, symbol="BTCUSDT", status="expired", now_ms=now_ms)
    _insert_quick_registration(conn, symbol="BTCUSDT", status="tagged", now_ms=now_ms)


def _table_rows(conn: Any, table: str) -> list[dict[str, Any]]:
    rows = conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
    return [_canon(dict(r)) for r in rows]


def _exchange_info_btc_eth() -> dict[str, Any]:
    def sym(name: str, step: str, tick: str, min_n: str = "5") -> dict[str, Any]:
        return {
            "symbol": name,
            "filters": [
                {"filterType": "LOT_SIZE", "stepSize": step, "minQty": step},
                {"filterType": "MIN_NOTIONAL", "notional": min_n},
                {"filterType": "PRICE_FILTER", "tickSize": tick},
            ],
        }

    return {"symbols": [sym("BTCUSDT", "0.001", "0.1"), sym("ETHUSDT", "0.001", "0.01")]}


def _entry_handlers(*, mark_btc: str = "100", mark_eth: str = "50") -> dict[tuple[str, str], Any]:
    marks = {"BTCUSDT": mark_btc, "ETHUSDT": mark_eth}

    def premium(clean: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        sym = str(clean.get("symbol") or "BTCUSDT")
        return (200, {"markPrice": marks.get(sym, "100")})

    return {
        ("GET", "/fapi/v1/positionSide/dual"): (200, {"dualSidePosition": False}),
        ("GET", "/fapi/v1/multiAssetsMargin"): (200, {"multiAssetsMargin": False}),
        ("GET", "/fapi/v3/balance"): (
            200,
            [{"asset": "USDT", "balance": "1000", "availableBalance": "1000"}],
        ),
        ("GET", "/fapi/v3/positionRisk"): (200, []),
        ("GET", "/fapi/v1/premiumIndex"): premium,
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info_btc_eth()),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, []),
        ("GET", "/fapi/v1/openOrders"): (200, []),
    }


def _patch_kill_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )


def _watch_fake(*, day: bool = True) -> FakeFutures:
    orders = [
        _stop_order("BTCUSDT", stop="90", qty="0.01"),
        _stop_order("ETHUSDT", stop="45", qty="0.1"),
    ]
    return FakeFutures(
        balance=Decimal("1000"),
        positions=_btc_eth_positions(),
        orders=orders,
        marks={"BTCUSDT": Decimal("100"), "ETHUSDT": Decimal("50")},
        steps={"BTCUSDT": Decimal("0.001"), "ETHUSDT": Decimal("0.001")},
        ticks={"BTCUSDT": Decimal("0.1"), "ETHUSDT": Decimal("0.01")},
    )


# ===========================================================================
# (P) Pin tests — must pass on 24ef806 and after the change
# ===========================================================================


def test_p_keyboard_literals() -> None:
    sid = "asig_deadbeefdeadbeef"
    assert skip_inline_keyboard(sid) == {"inline_keyboard": [[{"text": "⏭ ข้าม", "callback_data": sid}]]}
    assert skip_inline_keyboard(sid, entry=True) == {
        "inline_keyboard": [
            [
                {"text": "เข้าไม้", "callback_data": f"ec:{sid}"},
                {"text": "⏭ ข้าม", "callback_data": sid},
            ]
        ]
    }


def test_p_unchanged_watch_day_and_night(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_sync_idle(monkeypatch)

    def seed_b(conn: Any, now: int) -> None:
        _seed_non_open_quick(conn, now_ms=now)

    def run_one(conn: Any, now: int, *, night: bool) -> dict[str, Any]:
        ts = _ms_bangkok(2026, 11, 14, 23, 45) if night else now
        fake = _watch_fake()
        sent, send = _send_capture()
        result = run_assist_watch(conn, futures=fake, now_ms_value=ts, send=send)
        return {
            "result": _canon(result),
            "sent": _canon(sent),
            "watch": _table_rows(conn, "assist_watch_position"),
            "advice": _table_rows(conn, "assist_watch_advice"),
            "intent": _table_rows(conn, "assist_exec_intent"),
            "signal": _table_rows(conn, "assist_signal"),
        }

    for night in (False, True):
        snaps: list[dict[str, Any]] = []
        for label, seeder in (("empty", lambda _c, _n: None), ("non_open", seed_b)):
            conn = open_db(tmp_path / f"watch_{night}_{label}.db")
            now = _ms_bangkok(2026, 11, 14, 12, 0)
            seeder(conn, now)
            snaps.append(run_one(conn, now, night=night))
            conn.close()
        assert snaps[0] == snaps[1]


def test_p_unchanged_flush_held(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_sync_idle(monkeypatch)

    def seed_held(conn: Any, now: int) -> None:
        upsert_watch_position(
            conn,
            {
                "symbol": "BTCUSDT",
                "position_side": "BOTH",
                "side": "long",
                "entry": 100.0,
                "qty": 0.01,
                "in_universe": 1,
                "signal_id": None,
                "initial_stop": 90.0,
                "initial_stop_source": "signal",
                "entry_time_ms": now - 3_600_000,
                "no_stop_latched": 0,
                "last_seen_ms": now,
                "closed_at_ms": None,
            },
        )
        insert_watch_advice(
            conn,
            symbol="BTCUSDT",
            position_side="BOTH",
            kind="move_stop",
            reason="trail",
            level=95.0,
            approx_r=0.5,
            status="held",
            created_at_ms=now - 60_000,
        )

    def run(conn: Any, now: int) -> dict[str, Any]:
        seed_held(conn, now)
        fake = FakeFutures(
            balance=Decimal("1000"),
            positions=[_pos("BTCUSDT")],
            orders=[_stop_order("BTCUSDT", stop="90", qty="0.01")],
            marks={"BTCUSDT": Decimal("100")},
            ticks={"BTCUSDT": Decimal("0.1")},
        )
        sent, send = _send_capture()
        out = flush_held_watch_advice(conn, futures=fake, now_ms_value=now, send=send)
        return {
            "out": _canon(out),
            "sent": _canon(sent),
            "advice": _table_rows(conn, "assist_watch_advice"),
        }

    snaps = []
    for label, extra in (("empty", False), ("non_open", True)):
        conn = open_db(tmp_path / f"flush_{label}.db")
        now = _ms_bangkok(2026, 11, 14, 8, 20)
        if extra:
            _seed_non_open_quick(conn, now_ms=now)
        snaps.append(run(conn, now))
        conn.close()
    assert snaps[0] == snaps[1]


def test_p_unchanged_build_existing_ideas(tmp_path: Path) -> None:
    snaps = []
    for label, extra in (("empty", False), ("non_open", True)):
        conn = open_db(tmp_path / f"ideas_{label}.db")
        now = _ms_bangkok(2026, 11, 14, 12, 0)
        if extra:
            _seed_non_open_quick(conn, now_ms=now)
        positions = _btc_eth_positions()
        orders = [
            _stop_order("BTCUSDT", stop="90", qty="0.01"),
            _stop_order("ETHUSDT", stop="45", qty="0.1"),
        ]
        ideas = build_existing_ideas(
            conn,
            positions=positions,
            orders=orders,
            balance=Decimal("1000"),
            risk_pct=Decimal("1"),
            now_ms=now,
        )
        snaps.append([(i.symbol, i.side, str(i.risk_usdt)) for i in ideas])
        conn.close()
    assert snaps[0] == snaps[1]
    assert any(s == "BTCUSDT" for s, _, _ in snaps[0])


def test_p_unchanged_signals_and_morning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVISE_ASSIST_EXECUTE", raising=False)

    def setup_and_run(conn: Any, now: int) -> dict[str, Any]:
        _seed_analysis(
            conn, symbol="BTCUSDT", action="long", confidence=0.8, invalidation=90.0, ts=now - 60_000
        )
        _seed_analysis(
            conn, symbol="ETHUSDT", action="long", confidence=0.8, invalidation=45.0, ts=now - 60_000
        )
        # BTC open → open_position skip; ETH sent
        fake = FakeFutures(
            balance=Decimal("1000"),
            positions=[_pos("BTCUSDT")],
            marks={"BTCUSDT": Decimal("100"), "ETHUSDT": Decimal("50")},
            steps={"BTCUSDT": Decimal("0.001"), "ETHUSDT": Decimal("0.001")},
        )
        sent, send = _send_capture()
        r = run_assist_signals(conn, futures=fake, now_ms_value=now, send=send)
        return {
            "result": _canon(r),
            "sent": _canon(sent),
            "signal": _table_rows(conn, "assist_signal"),
        }

    snaps = []
    for label, extra in (("empty", False), ("non_open", True)):
        conn = open_db(tmp_path / f"sig_{label}.db")
        now = _ms_bangkok(2026, 11, 14, 12, 25)
        if extra:
            _seed_non_open_quick(conn, now_ms=now)
        snaps.append(setup_and_run(conn, now))
        conn.close()
    assert snaps[0] == snaps[1]
    assert any(
        s.get("reason") == "open_position" and s.get("symbol") == "BTCUSDT"
        for s in snaps[0]["result"]["skipped"]
    )

    # Morning with held BTC+ETH
    morning_snaps = []
    for label, extra in (("empty", False), ("non_open", True)):
        conn = open_db(tmp_path / f"morn_{label}.db")
        night = _ms_bangkok(2026, 11, 13, 23, 45)
        morning = _ms_bangkok(2026, 11, 14, 8, 20)
        if extra:
            _seed_non_open_quick(conn, now_ms=morning)
        for sym, inv in (("BTCUSDT", 90.0), ("ETHUSDT", 45.0)):
            _seed_analysis(
                conn, symbol=sym, action="long", confidence=0.8, invalidation=inv, ts=night - 60_000
            )
        fake_n = FakeFutures(
            balance=Decimal("1000"),
            marks={"BTCUSDT": Decimal("100"), "ETHUSDT": Decimal("50")},
            steps={"BTCUSDT": Decimal("0.001"), "ETHUSDT": Decimal("0.001")},
        )
        run_assist_signals(conn, futures=fake_n, now_ms_value=night, send=lambda *_a, **_k: True)
        for sym, inv in (("BTCUSDT", 90.0), ("ETHUSDT", 45.0)):
            _seed_analysis(
                conn, symbol=sym, action="long", confidence=0.8, invalidation=inv, ts=morning - 60_000
            )
        sent, send = _send_capture()
        r = run_assist_morning(conn, futures=fake_n, now_ms_value=morning, send=send)
        morning_snaps.append(
            {"result": _canon(r), "sent": _canon(sent), "signal": _table_rows(conn, "assist_signal")}
        )
        conn.close()
    assert morning_snaps[0] == morning_snaps[1]


def test_p_unchanged_render_anything_now(tmp_path: Path) -> None:
    snaps = []
    for label, extra in (("empty", False), ("non_open", True)):
        conn = open_db(tmp_path / f"ond_{label}.db")
        now = _ms_bangkok(2026, 11, 14, 12, 0)
        if extra:
            _seed_non_open_quick(conn, now_ms=now)
        _seed_analysis(
            conn, symbol="ETHUSDT", action="long", confidence=0.8, invalidation=45.0, ts=now - 60_000
        )
        fake = FakeFutures(
            balance=Decimal("1000"),
            marks={"ETHUSDT": Decimal("50")},
            steps={"ETHUSDT": Decimal("0.001")},
        )
        text, markup = render_anything_now(conn, futures=fake, now_ms=now)
        snaps.append((_canon(text), _canon(markup)))
        conn.close()
    assert snaps[0] == snaps[1]


def test_p_unchanged_entry_callbacks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    _patch_kill_clear(monkeypatch)

    def run(conn: Any, now: int) -> dict[str, Any]:
        _seed_analysis(
            conn, symbol="ETHUSDT", action="long", confidence=0.8, invalidation=45.0, ts=now - 60_000
        )
        sized = size_signal(
            symbol="ETHUSDT",
            side="long",
            entry=Decimal("50"),
            stop=Decimal("45"),
            balance=Decimal("1000"),
            risk_pct=Decimal("1"),
            step_size=Decimal("0.001"),
        )
        assert sized is not None
        sid = insert_signal(
            conn,
            sized,
            status="sent",
            created_at_ms=now - 60_000,
            expires_at_ms=now + 3_600_000,
            sent_at_ms=now - 60_000,
        )
        handlers = _entry_handlers()
        transport = RecordingTransport(handlers)
        trade = _client(transport)
        trade.assert_trade_key_safe = lambda: None  # type: ignore[method-assign]
        trade.assert_account_modes = lambda: None  # type: ignore[method-assign]
        monkeypatch.setattr("jarvise_assist.execute.build_trade_client", lambda: (trade, None))
        answers: list[Any] = []
        sent, send = _send_capture()

        def answer(cq_id: str, text: str | None = None) -> bool:
            answers.append(text)
            return True

        # Pin: preview taps only (ey: would place orders).
        out_sid = process_update(
            conn,
            _callback(f"ec:{sid}"),
            send=send,
            answer=answer,
            futures=FakeFutures(),
            now_ms_value=now,
        )
        out_sym = process_update(
            conn,
            _callback("ec:ETHUSDT:l"),
            send=send,
            answer=answer,
            futures=FakeFutures(),
            now_ms_value=now,
        )
        return {
            "out_sid": _canon(out_sid),
            "out_sym": _canon(out_sym),
            "answers": answers,
            "sent": _canon(sent),
            "calls": list(transport.calls),
            "intent": _table_rows(conn, "assist_exec_intent"),
        }

    snaps = []
    for label, extra in (("empty", False), ("non_open", True)):
        conn = open_db(tmp_path / f"ec_{label}.db")
        now = _ms_bangkok(2026, 11, 14, 12, 0)
        if extra:
            _seed_non_open_quick(conn, now_ms=now)
        snaps.append(run(conn, now))
        conn.close()
    assert snaps[0] == snaps[1]


# ===========================================================================
# (N) New behaviour — must FAIL on 24ef806
# ===========================================================================


MSG_QUICK_OPEN_EXPECTED = "มีไม้ quick อยู่ — ปิดก่อนถึงเข้าได้"


def test_n_quick_symbols(tmp_path: Path) -> None:
    from jarvise_assist import quick_tags

    assert hasattr(quick_tags, "quick_symbols"), "quick_symbols missing"
    quick_symbols = quick_tags.quick_symbols
    conn = open_db(tmp_path / "qs.db")
    now = _ms_bangkok(2026, 11, 14, 12, 0)
    assert quick_symbols(conn) == frozenset()
    _insert_quick_trade(conn, symbol="btcusdt", status="open", now_ms=now)
    assert "BTCUSDT" in quick_symbols(conn)
    conn.execute("DELETE FROM assist_quick_trade")
    conn.execute("DELETE FROM assist_quick_registration")
    conn.commit()
    _insert_quick_registration(conn, symbol="ethusdt", status="open", now_ms=now)
    assert quick_symbols(conn) == frozenset({"ETHUSDT"})
    _insert_quick_trade(conn, symbol="BTCUSDT", status="closed", now_ms=now)
    _insert_quick_registration(conn, symbol="SOLUSDT", status="expired", now_ms=now)
    _insert_quick_registration(conn, symbol="BNBUSDT", status="tagged", now_ms=now)
    assert quick_symbols(conn) == frozenset({"ETHUSDT"})
    conn.close()


@pytest.mark.parametrize("quick_kind", ["trade", "registration"])
def test_n_watch_skips_quick_symbol(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quick_kind: str) -> None:
    _patch_sync_idle(monkeypatch)
    conn = open_db(tmp_path / f"w_{quick_kind}.db")
    now = _ms_bangkok(2026, 11, 14, 12, 0)
    if quick_kind == "trade":
        _insert_quick_trade(conn, status="open", now_ms=now)
    else:
        _insert_quick_registration(conn, status="open", now_ms=now)

    upsert_watch_position(
        conn,
        {
            "symbol": "BTCUSDT",
            "position_side": "BOTH",
            "side": "long",
            "entry": 100.0,
            "qty": 0.01,
            "in_universe": 1,
            "signal_id": None,
            "initial_stop": 90.0,
            "initial_stop_source": "signal",
            "entry_time_ms": now - 3_600_000,
            "no_stop_latched": 0,
            "last_seen_ms": now - 60_000,
            "closed_at_ms": None,
        },
    )
    fake = _watch_fake()
    sent, send = _send_capture()
    r = run_assist_watch(conn, futures=fake, now_ms_value=now, send=send)
    assert {"symbol": "BTCUSDT", "action": "quick_skip"} in r["actions"]
    assert not any(s["text"] for s in sent if "BTCUSDT" in s["text"] and "ETHUSDT" not in s["text"])
    btc_watch = conn.execute(
        "SELECT * FROM assist_watch_position WHERE symbol='BTCUSDT' AND closed_at_ms IS NULL"
    ).fetchone()
    # pre-existing stays open; no NEW row created beyond that one
    assert btc_watch is not None
    assert not any(c.get("symbol") == "BTCUSDT" for c in r.get("closed") or [])
    # ETH still processed (no_stop or similar / advice rows)
    eth_actions = [a for a in r["actions"] if a.get("symbol") == "ETHUSDT"]
    assert eth_actions
    assert eth_actions[0].get("action") != "quick_skip"
    advice_btc = conn.execute("SELECT COUNT(*) FROM assist_watch_advice WHERE symbol='BTCUSDT'").fetchone()[0]
    assert advice_btc == 0
    intent_btc = conn.execute("SELECT COUNT(*) FROM assist_exec_intent WHERE symbol='BTCUSDT'").fetchone()[0]
    assert intent_btc == 0

    # Night: no held advice for BTC
    night = _ms_bangkok(2026, 11, 14, 23, 45)
    sent2, send2 = _send_capture()
    run_assist_watch(conn, futures=fake, now_ms_value=night, send=send2)
    held_btc = conn.execute(
        "SELECT COUNT(*) FROM assist_watch_advice WHERE symbol='BTCUSDT' AND status='held'"
    ).fetchone()[0]
    assert held_btc == 0
    conn.close()


def test_n_watch_sui_quick_no_outside_universe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_sync_idle(monkeypatch)
    conn = open_db(tmp_path / "sui.db")
    now = _ms_bangkok(2026, 11, 14, 12, 0)
    _insert_quick_trade(conn, symbol="SUIUSDT", status="open", now_ms=now)
    fake = FakeFutures(
        balance=Decimal("1000"),
        positions=[_pos("SUIUSDT", amt="10", entry="1", mark="1")],
        orders=[],
    )
    sent, send = _send_capture()
    r = run_assist_watch(conn, futures=fake, now_ms_value=now, send=send)
    assert {"symbol": "SUIUSDT", "action": "quick_skip"} in r["actions"]
    assert not any(a.get("action") == "outside_universe" for a in r["actions"])
    assert (
        conn.execute("SELECT COUNT(*) FROM assist_watch_position WHERE symbol='SUIUSDT'").fetchone()[0] == 0
    )
    assert sent == []
    conn.close()


def test_n_watch_closed_quick_remainder_processed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_sync_idle(monkeypatch)
    conn = open_db(tmp_path / "xz.db")
    now = _ms_bangkok(2026, 11, 14, 12, 0)
    _insert_quick_trade(conn, status="closed", now_ms=now)
    fake = FakeFutures(
        balance=Decimal("1000"),
        positions=[_pos("BTCUSDT")],
        orders=[],
        marks={"BTCUSDT": Decimal("100")},
    )
    r = run_assist_watch(conn, futures=fake, now_ms_value=now, send=lambda *_a, **_k: True)
    assert not any(a.get("action") == "quick_skip" for a in r["actions"])
    assert any(a.get("symbol") == "BTCUSDT" for a in r["actions"])
    conn.close()


def test_n_flush_drops_quick_held(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "flush_q.db")
    now = _ms_bangkok(2026, 11, 14, 8, 20)
    _insert_quick_trade(conn, status="open", now_ms=now)
    upsert_watch_position(
        conn,
        {
            "symbol": "BTCUSDT",
            "position_side": "BOTH",
            "side": "long",
            "entry": 100.0,
            "qty": 0.01,
            "in_universe": 1,
            "signal_id": None,
            "initial_stop": 90.0,
            "initial_stop_source": "signal",
            "entry_time_ms": now - 3_600_000,
            "no_stop_latched": 0,
            "last_seen_ms": now,
            "closed_at_ms": None,
        },
    )
    a1 = insert_watch_advice(
        conn,
        symbol="BTCUSDT",
        position_side="BOTH",
        kind="move_stop",
        reason="trail",
        level=95.0,
        approx_r=0.5,
        status="held",
        created_at_ms=now - 60_000,
    )
    a2 = insert_watch_advice(
        conn,
        symbol="BTCUSDT",
        position_side="BOTH",
        kind="exit",
        reason="flat",
        level=None,
        approx_r=1.0,
        status="held",
        created_at_ms=now - 50_000,
    )
    fake = FakeFutures(
        balance=Decimal("1000"),
        positions=[_pos("BTCUSDT")],
        orders=[_stop_order("BTCUSDT", stop="90", qty="0.01")],
        marks={"BTCUSDT": Decimal("100")},
    )
    sent, send = _send_capture()
    out = flush_held_watch_advice(conn, futures=fake, now_ms_value=now, send=send)
    assert a1 in out["dropped"] and a2 in out["dropped"]
    assert out["sent"] == []
    assert sent == []
    conn.close()


def test_n_sizing_ignores_quick_position(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "size.db")
    now = _ms_bangkok(2026, 11, 14, 12, 0)
    _insert_quick_trade(conn, status="open", now_ms=now)
    positions = [_pos("BTCUSDT", amt="1", entry="100", mark="100")]
    orders = [_stop_order("BTCUSDT", stop="90", qty="1")]
    ideas = build_existing_ideas(
        conn,
        positions=positions,
        orders=orders,
        balance=Decimal("1000"),
        risk_pct=Decimal("1"),
        now_ms=now,
    )
    assert ideas == []

    sized = size_signal(
        symbol="BTCUSDT",
        side="long",
        entry=Decimal("100"),
        stop=Decimal("90"),
        balance=Decimal("1000"),
        risk_pct=Decimal("1"),
        step_size=Decimal("0.001"),
    )
    assert sized is not None
    insert_signal(
        conn,
        sized,
        status="sent",
        created_at_ms=now - 60_000,
        expires_at_ms=now + 3_600_000,
        sent_at_ms=now - 60_000,
    )
    ideas2 = build_existing_ideas(
        conn,
        positions=positions,
        orders=orders,
        balance=Decimal("1000"),
        risk_pct=Decimal("1"),
        now_ms=now,
        include_sent_held=True,
    )
    assert len(ideas2) == 1
    assert ideas2[0].symbol == "BTCUSDT"
    conn.close()


def test_n_sizing_risk_cap_exemption(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Huge BTC risk: with quick → ETH admits; without → risk_cap."""
    monkeypatch.delenv("JARVISE_ASSIST_EXECUTE", raising=False)
    _patch_kill_clear(monkeypatch)
    now = _ms_bangkok(2026, 11, 14, 12, 25)
    # qty=1, entry=100, stop=50 → risk 50 USDT on 1000 balance = 5% > 4%
    btc = _pos("BTCUSDT", amt="1", entry="100", mark="100")
    orders = [_stop_order("BTCUSDT", stop="50", qty="1")]

    def run(with_quick: bool) -> tuple[Any, str | None]:
        conn = open_db(tmp_path / f"risk_{with_quick}.db")
        if with_quick:
            _insert_quick_trade(conn, status="open", now_ms=now)
        _seed_analysis(
            conn, symbol="ETHUSDT", action="long", confidence=0.8, invalidation=45.0, ts=now - 60_000
        )
        fake = FakeFutures(
            balance=Decimal("1000"),
            positions=[btc],
            orders=orders,
            marks={"ETHUSDT": Decimal("50"), "BTCUSDT": Decimal("100")},
            steps={"ETHUSDT": Decimal("0.001"), "BTCUSDT": Decimal("0.001")},
        )
        r = run_assist_signals(conn, futures=fake, now_ms_value=now, send=lambda *_a, **_k: True)
        eth_created = any(c.get("symbol") == "ETHUSDT" for c in r.get("created") or [])
        eth_skip = next(
            (s for s in r.get("skipped") or [] if s.get("symbol") == "ETHUSDT"),
            None,
        )
        intent_id = insert_exec_intent(
            conn,
            kind="entry",
            symbol="ETHUSDT",
            side="long",
            preview={
                "qty": "0.2",
                "stop": "45",
                "target": "60",
                "leverage": 5,
                "entry": "50",
                "loss_at_stop": "1.0",
                "r_distance": "5",
            },
            created_at_ms=now,
            expires_at_ms=now + 120_000,
        )
        intent = get_exec_intent(conn, intent_id)
        assert intent is not None

        class TradeLike(FakeFutures):
            def assert_trade_key_safe(self) -> None:
                return None

            def assert_account_modes(self) -> None:
                return None

        trade = TradeLike(
            balance=Decimal("1000"),
            positions=[btc],
            orders=orders,
            marks={"ETHUSDT": Decimal("50"), "BTCUSDT": Decimal("100")},
            steps={"ETHUSDT": Decimal("0.001")},
            ticks={"ETHUSDT": Decimal("0.01")},
        )
        _seed_analysis(
            conn, symbol="ETHUSDT", action="long", confidence=0.8, invalidation=45.0, ts=now - 60_000
        )
        guard = _confirm_guards_entry(conn, intent, trade, now_ms_value=now)  # type: ignore[arg-type]
        conn.close()
        return (eth_created, eth_skip, guard)

    created_q, skip_q, guard_q = run(True)
    created_n, skip_n, guard_n = run(False)
    assert created_q is True
    assert guard_q is None
    assert created_n is False
    assert skip_n is not None and skip_n.get("reason") == "risk_cap"
    assert guard_n == "risk_cap"


@pytest.mark.parametrize("quick_kind", ["trade", "registration"])
def test_n_signals_sends_with_quick_block_button(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quick_kind: str
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / f"sig_q_{quick_kind}.db")
    now = _ms_bangkok(2026, 11, 14, 12, 25)
    if quick_kind == "trade":
        _insert_quick_trade(conn, status="open", now_ms=now)
        positions = [_pos("BTCUSDT")]
    else:
        _insert_quick_registration(conn, status="open", now_ms=now)
        positions = []
    _seed_analysis(conn, symbol="BTCUSDT", action="long", confidence=0.8, invalidation=90.0, ts=now - 60_000)
    fake = FakeFutures(
        balance=Decimal("1000"),
        positions=positions,
        marks={"BTCUSDT": Decimal("100")},
        steps={"BTCUSDT": Decimal("0.001")},
    )
    sent, send = _send_capture()
    r = run_assist_signals(conn, futures=fake, now_ms_value=now, send=send)
    created = [c for c in r["created"] if c.get("symbol") == "BTCUSDT"]
    assert len(created) == 1
    assert created[0]["status"] == "sent"
    sid = created[0]["id"]
    assert len(sent) == 1
    assert sent[0]["reply_markup"] == {
        "inline_keyboard": [
            [
                {"text": MSG_QUICK_OPEN_EXPECTED, "callback_data": f"ec:{sid}"},
                {"text": "⏭ ข้าม", "callback_data": sid},
            ]
        ]
    }

    # Same analysis text as no-position / no-quick
    conn2 = open_db(tmp_path / f"sig_ref_{quick_kind}.db")
    _seed_analysis(conn2, symbol="BTCUSDT", action="long", confidence=0.8, invalidation=90.0, ts=now - 60_000)
    sent2, send2 = _send_capture()
    fake2 = FakeFutures(
        balance=Decimal("1000"),
        positions=[],
        marks={"BTCUSDT": Decimal("100")},
        steps={"BTCUSDT": Decimal("0.001")},
    )
    run_assist_signals(conn2, futures=fake2, now_ms_value=now, send=send2)
    assert sent[0]["text"] == sent2[0]["text"]
    conn.close()
    conn2.close()


def test_n_signals_execute_off_skip_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "off")
    conn = open_db(tmp_path / "sig_off.db")
    now = _ms_bangkok(2026, 11, 14, 12, 25)
    _insert_quick_trade(conn, status="open", now_ms=now)
    _seed_analysis(conn, symbol="BTCUSDT", action="long", confidence=0.8, invalidation=90.0, ts=now - 60_000)
    fake = FakeFutures(
        balance=Decimal("1000"),
        positions=[_pos("BTCUSDT")],
        marks={"BTCUSDT": Decimal("100")},
        steps={"BTCUSDT": Decimal("0.001")},
    )
    sent, send = _send_capture()
    r = run_assist_signals(conn, futures=fake, now_ms_value=now, send=send)
    sid = next(c["id"] for c in r["created"] if c["symbol"] == "BTCUSDT")
    assert sent[0]["reply_markup"] == {"inline_keyboard": [[{"text": "⏭ ข้าม", "callback_data": sid}]]}
    conn.close()


def test_n_morning_sends_held_with_quick_then_normal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / "morn_q.db")
    night = _ms_bangkok(2026, 11, 13, 23, 45)
    morning = _ms_bangkok(2026, 11, 14, 8, 20)
    _insert_quick_trade(conn, status="open", now_ms=night)
    _seed_analysis(
        conn, symbol="BTCUSDT", action="long", confidence=0.8, invalidation=90.0, ts=night - 60_000
    )
    fake = FakeFutures(
        balance=Decimal("1000"),
        positions=[_pos("BTCUSDT")],
        marks={"BTCUSDT": Decimal("100")},
        steps={"BTCUSDT": Decimal("0.001")},
    )
    run_assist_signals(conn, futures=fake, now_ms_value=night, send=lambda *_a, **_k: True)
    held = conn.execute("SELECT id FROM assist_signal WHERE status='held'").fetchone()
    assert held is not None
    sid = held[0]
    _seed_analysis(
        conn, symbol="BTCUSDT", action="long", confidence=0.8, invalidation=90.0, ts=morning - 60_000
    )
    sent, send = _send_capture()
    r = run_assist_morning(conn, futures=fake, now_ms_value=morning, send=send)
    assert sid in r["sent"]
    assert sent[0]["reply_markup"]["inline_keyboard"][0][0]["text"] == MSG_QUICK_OPEN_EXPECTED
    assert sent[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"ec:{sid}"

    # Close quick + flat → morning sends the normal entry button on a fresh hold.
    conn.execute("UPDATE assist_quick_trade SET status='closed', closed_at_ms=?", (morning,))
    conn.execute("DELETE FROM assist_quick_registration")
    conn.commit()
    morn2 = _ms_bangkok(2026, 11, 15, 8, 20)
    _seed_analysis(
        conn, symbol="BTCUSDT", action="long", confidence=0.8, invalidation=90.0, ts=morn2 - 60_000
    )
    sized2 = size_signal(
        symbol="BTCUSDT",
        side="long",
        entry=Decimal("100"),
        stop=Decimal("90"),
        balance=Decimal("1000"),
        risk_pct=Decimal("1"),
        step_size=Decimal("0.001"),
    )
    assert sized2 is not None
    insert_signal(
        conn,
        sized2,
        status="held",
        created_at_ms=morn2 - 8 * 3_600_000,
        expires_at_ms=next_assist_run_ms(morn2 - 8 * 3_600_000),
    )
    fake_flat = FakeFutures(
        balance=Decimal("1000"),
        positions=[],
        marks={"BTCUSDT": Decimal("100")},
        steps={"BTCUSDT": Decimal("0.001")},
    )
    sent2, send2 = _send_capture()
    run_assist_morning(conn, futures=fake_flat, now_ms_value=morn2, send=send2)
    assert sent2
    assert sent2[0]["reply_markup"]["inline_keyboard"][0][0]["text"] == "เข้าไม้"
    conn.close()


@pytest.mark.parametrize("quick_kind", ["trade", "registration"])
def test_n_entry_refuses_quick(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quick_kind: str) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    _patch_kill_clear(monkeypatch)
    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / f"ec_q_{quick_kind}.db")
    now = _ms_bangkok(2026, 11, 14, 12, 0)
    if quick_kind == "trade":
        _insert_quick_trade(conn, status="open", now_ms=now)
    else:
        _insert_quick_registration(conn, status="open", now_ms=now)
    _seed_analysis(conn, symbol="BTCUSDT", action="long", confidence=0.8, invalidation=90.0, ts=now - 60_000)
    sized = size_signal(
        symbol="BTCUSDT",
        side="long",
        entry=Decimal("100"),
        stop=Decimal("90"),
        balance=Decimal("1000"),
        risk_pct=Decimal("1"),
        step_size=Decimal("0.001"),
    )
    assert sized is not None
    sid = insert_signal(
        conn,
        sized,
        status="sent",
        created_at_ms=now - 60_000,
        expires_at_ms=now + 3_600_000,
        sent_at_ms=now - 60_000,
    )
    # Preview intent created before registration (for ey:)
    intent_id = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview={
            "qty": "0.1",
            "stop": "90",
            "target": "120",
            "leverage": 5,
            "entry": "100",
            "loss_at_stop": "1",
            "r_distance": "10",
        },
        created_at_ms=now - 30_000,
        expires_at_ms=now + 90_000,
    )

    for data in (f"ec:{sid}", "ec:BTCUSDT:l"):
        answers: list[Any] = []
        sent, send = _send_capture()

        def _answer(cq_id: str, text: str | None = None, *, _answers: list[Any] = answers) -> bool:
            del cq_id
            _answers.append(text)
            return True

        out = process_update(
            conn,
            _callback(data),
            send=send,
            answer=_answer,
            futures=FakeFutures(),
            now_ms_value=now,
        )
        assert "unhandled_callback" not in out
        assert out.get("reason") == "quick_open"
        assert len(answers) == 1  # answer once (None from ec:)
        assert len(sent) == 1 and sent[0]["text"] == MSG_QUICK_OPEN_EXPECTED
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM assist_exec_intent WHERE kind='entry' AND status='preview' AND id != ?",
                (intent_id,),
            ).fetchone()[0]
            == 0
        )

    answers_ey: list[Any] = []
    sent_ey, send_ey = _send_capture()

    def _answer_ey(cq_id: str, text: str | None = None) -> bool:
        del cq_id
        answers_ey.append(text)
        return True

    out_ey = process_update(
        conn,
        _callback(f"ey:{intent_id}"),
        send=send_ey,
        answer=_answer_ey,
        futures=FakeFutures(),
        now_ms_value=now,
    )
    assert out_ey.get("reason") == "quick_open"
    assert len(answers_ey) == 1
    assert sent_ey[0]["text"] == MSG_QUICK_OPEN_EXPECTED
    assert get_exec_intent(conn, intent_id)["status"] == "preview"
    assert (
        conn.execute("SELECT COUNT(*) FROM assist_exec_log WHERE intent_id=?", (intent_id,)).fetchone()[0]
        == 0
    )
    conn.close()


def test_n_entry_off_and_eth_and_after_close(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    _patch_kill_clear(monkeypatch)
    conn = open_db(tmp_path / "ec_mix.db")
    now = _ms_bangkok(2026, 11, 14, 12, 0)
    _insert_quick_trade(conn, status="open", now_ms=now)
    _seed_analysis(conn, symbol="BTCUSDT", action="long", confidence=0.8, invalidation=90.0, ts=now - 60_000)
    _seed_analysis(conn, symbol="ETHUSDT", action="long", confidence=0.8, invalidation=45.0, ts=now - 60_000)

    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "off")
    _forbid_trade_client(monkeypatch)
    sent_off: list[str] = []
    answers_off: list[Any] = []
    process_update(
        conn,
        _callback("ec:BTCUSDT:l"),
        send=lambda t, **_: sent_off.append(t),
        answer=lambda _cq, text=None: answers_off.append(text),
        futures=FakeFutures(),
        now_ms_value=now,
    )
    assert "ปิดอยู่" in answers_off
    assert MSG_OFF in sent_off

    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")

    def _safe_trade(transport: RecordingTransport) -> Any:
        trade = _client(transport)
        trade.assert_trade_key_safe = lambda: None  # type: ignore[method-assign]
        trade.assert_account_modes = lambda: None  # type: ignore[method-assign]
        return trade

    transport = RecordingTransport(_entry_handlers())
    monkeypatch.setattr(
        "jarvise_assist.execute.build_trade_client",
        lambda: (_safe_trade(transport), None),
    )
    sent_eth, send_eth = _send_capture()
    out_eth = process_update(
        conn,
        _callback("ec:ETHUSDT:l"),
        send=send_eth,
        answer=lambda *_a, **_k: True,
        futures=FakeFutures(),
        now_ms_value=now,
    )
    assert out_eth.get("ok") is True
    assert out_eth.get("preview")
    assert transport.calls  # Binance used for ETH

    # Close quick → BTC ec opens preview
    conn.execute("UPDATE assist_quick_trade SET status='closed', closed_at_ms=?", (now,))
    conn.execute("DELETE FROM assist_quick_registration WHERE status='open'")
    conn.commit()
    sized = size_signal(
        symbol="BTCUSDT",
        side="long",
        entry=Decimal("100"),
        stop=Decimal("90"),
        balance=Decimal("1000"),
        risk_pct=Decimal("1"),
        step_size=Decimal("0.001"),
    )
    assert sized is not None
    sid = insert_signal(
        conn,
        sized,
        status="sent",
        created_at_ms=now - 60_000,
        expires_at_ms=now + 3_600_000,
        sent_at_ms=now - 60_000,
    )
    transport2 = RecordingTransport(_entry_handlers())
    monkeypatch.setattr(
        "jarvise_assist.execute.build_trade_client",
        lambda: (_safe_trade(transport2), None),
    )
    sent_btc, send_btc = _send_capture()
    out_btc = process_update(
        conn,
        _callback(f"ec:{sid}"),
        send=send_btc,
        answer=lambda *_a, **_k: True,
        futures=FakeFutures(),
        now_ms_value=now,
    )
    assert out_btc.get("ok") is True
    assert out_btc.get("preview")
    assert MSG_QUICK_OPEN_EXPECTED not in [s["text"] for s in sent_btc]
    conn.close()


def test_n_on_demand_quick_open(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "ond_q.db")
    now = _ms_bangkok(2026, 11, 14, 12, 0)
    _insert_quick_trade(conn, status="open", now_ms=now)
    _seed_analysis(conn, symbol="BTCUSDT", action="long", confidence=0.8, invalidation=90.0, ts=now - 60_000)
    fake = FakeFutures(
        balance=Decimal("1000"),
        positions=[],
        marks={"BTCUSDT": Decimal("100")},
        steps={"BTCUSDT": Decimal("0.001")},
    )
    text, markup = render_anything_now(conn, futures=fake, now_ms=now)
    assert "BTCUSDT" in text
    assert MSG_QUICK_OPEN_EXPECTED in text
    assert "BTCUSDT" not in (json.dumps(markup) if markup else "")
    from jarvise_assist.signals import scan_signal_candidates

    scanned = scan_signal_candidates(conn, futures=fake, now_ms=now, persist=False, on_demand=True)
    assert any(s.get("symbol") == "BTCUSDT" and s.get("reason") == "quick_open" for s in scanned["skipped"])
    conn.close()


def test_n_create_registration_still_works(tmp_path: Path) -> None:
    """Sanity: create_quick_registration path still seeds open status for guards."""
    from jarvise_assist import quick_tags

    conn = open_db(tmp_path / "reg.db")
    now = _ms_bangkok(2026, 11, 14, 12, 0)
    rid, err = create_quick_registration(
        conn,
        symbol="BTCUSDT",
        side="long",
        planned_entry=100.0,
        planned_stop=90.0,
        qty=0.01,
        now_ms=now,
    )
    assert err is None and rid
    assert hasattr(quick_tags, "quick_symbols")
    assert quick_tags.quick_symbols(conn) == frozenset({"BTCUSDT"})
    conn.close()
