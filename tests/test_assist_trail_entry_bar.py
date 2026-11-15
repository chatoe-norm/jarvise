"""Entry-hour 15m extremes for move-stop advice (post-fill slice of the open hour)."""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.test_assist import FakeFutures, _ms_bangkok
from tests.test_assist_execute import (
    RecordingTransport,
    _client,
    _exchange_info,
    _move_stop_handlers,
)
from tests.test_assist_listen import _callback

import jarvise_assist.trail_advice as trail_advice
from jarvise_assist.execute import (
    MOVE_STOP_TTL_MS,
    MSG_MOVE_STOP_NO_ADVICE,
    client_order_id,
    run_move_stop_sequence,
)
from jarvise_assist.futures import FuturesPosition
from jarvise_assist.listen import process_update
from jarvise_assist.store import (
    claim_exec_intent,
    get_exec_intent,
    insert_exec_intent,
    upsert_watch_position,
)
from jarvise_assist.trail_advice import (
    current_trail_advice,
    entry_bar_extremes,
    fetch_entry_bar_15m,
)
from jarvise_assist.watch import run_assist_watch
from jarvise_ingest.db import open_db, upsert_market_technicals

ENTRY = Decimal("0.1673")
INITIAL = Decimal("0.1748")
LONG_ENTRY = Decimal("0.1673")
LONG_INITIAL = Decimal("0.1598")
TICK = Decimal("0.0001")
SYM = "ADAUSDT"
DECISION_PREFIX = "assist_watch_decision "


def _hour_bars() -> list[dict[str, Any]]:
    h09 = _ms_bangkok(2026, 11, 14, 9, 0)
    h10 = _ms_bangkok(2026, 11, 14, 10, 0)
    return [
        {
            "timestamp": h09,
            "open": 0.1673,
            "high": 0.1676,
            "low": 0.1624,
            "close": 0.1650,
        },
        {
            "timestamp": h10,
            "open": 0.1650,
            "high": 0.1677,
            "low": 0.1650,
            "close": 0.1655,
        },
    ]


def _fake_15m(
    rows: list[dict[str, Any]],
) -> tuple[list[tuple[str, int, int]], Any]:
    calls: list[tuple[str, int, int]] = []

    def fetch(symbol: str, start_ms: int, until_ms: int) -> list[dict]:
        calls.append((symbol, start_ms, until_ms))
        return list(rows)

    return calls, fetch


def test_entry_bar_fill_0905_breakeven_at_1005() -> None:
    """(a) Fill 09:05, now 10:05, 15m 09:15 low 0.1624 → breakeven 0.1670."""
    entry_ms = _ms_bangkok(2026, 11, 14, 9, 5)
    now = _ms_bangkok(2026, 11, 14, 10, 5)
    start_15 = _ms_bangkok(2026, 11, 14, 9, 15)
    until = _ms_bangkok(2026, 11, 14, 10, 0)
    calls, fetch = _fake_15m(
        [
            {
                "timestamp": start_15,
                "open": 0.166,
                "high": 0.167,
                "low": 0.1624,
                "close": 0.164,
            }
        ]
    )
    out = current_trail_advice(
        side="short",
        entry=ENTRY,
        initial_stop=INITIAL,
        candles_1h=_hour_bars(),
        entry_time_ms=entry_ms,
        now_ms=now,
        tick=TICK,
        symbol=SYM,
        entry_bar_fetch=fetch,
    )
    assert out == (Decimal("0.1670"), "breakeven")
    assert calls == [(SYM, start_15, until)]


def test_entry_bar_hour_still_open_no_fetch() -> None:
    """(b) Fill 09:05, now 09:55 → None; fetcher not called."""
    entry_ms = _ms_bangkok(2026, 11, 14, 9, 5)
    now = _ms_bangkok(2026, 11, 14, 9, 55)
    calls, fetch = _fake_15m([{"timestamp": _ms_bangkok(2026, 11, 14, 9, 15), "low": 0.1624, "high": 0.167}])
    out = current_trail_advice(
        side="short",
        entry=ENTRY,
        initial_stop=INITIAL,
        candles_1h=_hour_bars(),
        entry_time_ms=entry_ms,
        now_ms=now,
        tick=TICK,
        symbol=SYM,
        entry_bar_fetch=fetch,
    )
    assert out is None
    assert calls == []


def test_entry_bar_prefill_leak_filtered() -> None:
    """(c) Prefill 09:00 low must not count; post-fill lows above 0.5R → None."""
    entry_ms = _ms_bangkok(2026, 11, 14, 9, 5)
    now = _ms_bangkok(2026, 11, 14, 10, 5)
    calls, fetch = _fake_15m(
        [
            {
                "timestamp": _ms_bangkok(2026, 11, 14, 9, 0),
                "low": 0.1600,
                "high": 0.167,
            },
            {
                "timestamp": _ms_bangkok(2026, 11, 14, 9, 15),
                "low": 0.16356,
                "high": 0.167,
            },
            {
                "timestamp": _ms_bangkok(2026, 11, 14, 9, 30),
                "low": 0.16356,
                "high": 0.167,
            },
            {
                "timestamp": _ms_bangkok(2026, 11, 14, 9, 45),
                "low": 0.16356,
                "high": 0.167,
            },
        ]
    )
    out = current_trail_advice(
        side="short",
        entry=ENTRY,
        initial_stop=INITIAL,
        candles_1h=_hour_bars(),
        entry_time_ms=entry_ms,
        now_ms=now,
        tick=TICK,
        symbol=SYM,
        entry_bar_fetch=fetch,
    )
    assert out is None
    assert len(calls) == 1


def test_entry_bar_upper_bound_10m_row_filtered() -> None:
    """10:00 15m row is past hour close; post-fill lows above 0.5R → None."""
    entry_ms = _ms_bangkok(2026, 11, 14, 9, 5)
    now = _ms_bangkok(2026, 11, 14, 10, 5)
    calls, fetch = _fake_15m(
        [
            {
                "timestamp": _ms_bangkok(2026, 11, 14, 9, 15),
                "low": 0.16356,
                "high": 0.167,
            },
            {
                "timestamp": _ms_bangkok(2026, 11, 14, 9, 30),
                "low": 0.16356,
                "high": 0.167,
            },
            {
                "timestamp": _ms_bangkok(2026, 11, 14, 9, 45),
                "low": 0.16356,
                "high": 0.167,
            },
            {
                "timestamp": _ms_bangkok(2026, 11, 14, 10, 0),
                "low": 0.1600,
                "high": 0.167,
            },
        ]
    )
    out = current_trail_advice(
        side="short",
        entry=ENTRY,
        initial_stop=INITIAL,
        candles_1h=_hour_bars(),
        entry_time_ms=entry_ms,
        now_ms=now,
        tick=TICK,
        symbol=SYM,
        entry_bar_fetch=fetch,
    )
    assert out is None
    assert len(calls) == 1


def test_entry_bar_cache_hit_and_fail_memo() -> None:
    """Success caches; empty result cools down 120s on now_ms (not wall clock)."""
    entry_ms = _ms_bangkok(2026, 11, 14, 9, 5)
    now = _ms_bangkok(2026, 11, 14, 10, 5)
    start_15 = _ms_bangkok(2026, 11, 14, 9, 15)
    until = _ms_bangkok(2026, 11, 14, 10, 0)
    calls, fetch = _fake_15m(
        [
            {
                "timestamp": start_15,
                "open": 0.166,
                "high": 0.167,
                "low": 0.1624,
                "close": 0.164,
            }
        ]
    )
    first = entry_bar_extremes(symbol=SYM, entry_time_ms=entry_ms, now_ms=now, fetch=fetch)
    second = entry_bar_extremes(symbol=SYM, entry_time_ms=entry_ms, now_ms=now, fetch=fetch)
    assert first == (0.1624, 0.167)
    assert second == first
    assert calls == [(SYM, start_15, until)]

    trail_advice._ENTRY_BAR_CACHE.clear()
    empty_calls: list[tuple[str, int, int]] = []

    def empty_fetch(symbol: str, start_ms: int, until_ms: int) -> list[dict]:
        empty_calls.append((symbol, start_ms, until_ms))
        return []

    assert entry_bar_extremes(symbol=SYM, entry_time_ms=entry_ms, now_ms=now, fetch=empty_fetch) == (
        None,
        None,
    )
    assert len(empty_calls) == 1
    assert entry_bar_extremes(symbol=SYM, entry_time_ms=entry_ms, now_ms=now + 60_000, fetch=empty_fetch) == (
        None,
        None,
    )
    assert len(empty_calls) == 1
    assert entry_bar_extremes(
        symbol=SYM, entry_time_ms=entry_ms, now_ms=now + 121_000, fetch=empty_fetch
    ) == (None, None)
    assert len(empty_calls) == 2


def test_entry_bar_long_upper_bound_filtered() -> None:
    """Long-side mirror: 10:00 high must not count; in-hour highs below 0.5R → None."""
    entry_ms = _ms_bangkok(2026, 11, 14, 9, 5)
    now = _ms_bangkok(2026, 11, 14, 10, 5)
    h09 = _ms_bangkok(2026, 11, 14, 9, 0)
    h10 = _ms_bangkok(2026, 11, 14, 10, 0)
    candles = [
        {
            "timestamp": h09,
            "open": 0.1673,
            "high": 0.1700,
            "low": 0.1660,
            "close": 0.1680,
        },
        {
            "timestamp": h10,
            "open": 0.1680,
            "high": 0.1700,
            "low": 0.1670,
            "close": 0.1690,
        },
    ]
    calls, fetch = _fake_15m(
        [
            {
                "timestamp": _ms_bangkok(2026, 11, 14, 9, 15),
                "low": 0.167,
                "high": 0.17104,
            },
            {
                "timestamp": _ms_bangkok(2026, 11, 14, 9, 30),
                "low": 0.167,
                "high": 0.17104,
            },
            {
                "timestamp": _ms_bangkok(2026, 11, 14, 9, 45),
                "low": 0.167,
                "high": 0.17104,
            },
            {
                "timestamp": _ms_bangkok(2026, 11, 14, 10, 0),
                "low": 0.167,
                "high": 0.1800,
            },
        ]
    )
    out = current_trail_advice(
        side="long",
        entry=LONG_ENTRY,
        initial_stop=LONG_INITIAL,
        candles_1h=candles,
        entry_time_ms=entry_ms,
        now_ms=now,
        tick=TICK,
        symbol=SYM,
        entry_bar_fetch=fetch,
    )
    assert out is None
    assert len(calls) == 1


def test_entry_bar_exact_half_r_threshold() -> None:
    """(d) Exact Decimal 0.5R on the 15m path."""
    entry_ms = _ms_bangkok(2026, 11, 14, 9, 5)
    now = _ms_bangkok(2026, 11, 14, 10, 5)
    start_15 = _ms_bangkok(2026, 11, 14, 9, 15)

    def advice(low: float) -> tuple[Decimal, str] | None:
        trail_advice._ENTRY_BAR_CACHE.clear()
        trail_advice._ENTRY_BAR_FAIL_MEMO.clear()
        _, fetch = _fake_15m(
            [{"timestamp": start_15, "low": low, "high": 0.1676, "open": 0.166, "close": 0.164}]
        )
        return current_trail_advice(
            side="short",
            entry=ENTRY,
            initial_stop=INITIAL,
            candles_1h=_hour_bars(),
            entry_time_ms=entry_ms,
            now_ms=now,
            tick=TICK,
            symbol=SYM,
            entry_bar_fetch=fetch,
        )

    assert advice(0.16355) == (Decimal("0.1670"), "breakeven")
    assert advice(0.16356) is None


def test_entry_bar_exact_hour_fill_uses_1h_only() -> None:
    """(e) Fill at exactly 09:00:00.000 → 1h bar counts; fetcher not called."""
    entry_ms = _ms_bangkok(2026, 11, 14, 9, 0)
    now = _ms_bangkok(2026, 11, 14, 10, 5)
    calls, fetch = _fake_15m([{"timestamp": _ms_bangkok(2026, 11, 14, 9, 15), "low": 0.1600, "high": 0.167}])
    out = current_trail_advice(
        side="short",
        entry=ENTRY,
        initial_stop=INITIAL,
        candles_1h=_hour_bars(),
        entry_time_ms=entry_ms,
        now_ms=now,
        tick=TICK,
        symbol=SYM,
        entry_bar_fetch=fetch,
    )
    assert out == (Decimal("0.1670"), "breakeven")
    assert calls == []


def test_entry_bar_fetcher_raises_or_empty() -> None:
    """(f) Fetcher raises or returns [] → None; fetch_entry_bar_15m 500 → []."""
    entry_ms = _ms_bangkok(2026, 11, 14, 9, 5)
    now = _ms_bangkok(2026, 11, 14, 10, 5)

    def boom(*_a: Any, **_k: Any) -> list[dict]:
        raise RuntimeError("network")

    assert (
        current_trail_advice(
            side="short",
            entry=ENTRY,
            initial_stop=INITIAL,
            candles_1h=_hour_bars(),
            entry_time_ms=entry_ms,
            now_ms=now,
            tick=TICK,
            symbol=SYM,
            entry_bar_fetch=boom,
        )
        is None
    )
    trail_advice._ENTRY_BAR_FAIL_MEMO.clear()
    assert (
        current_trail_advice(
            side="short",
            entry=ENTRY,
            initial_stop=INITIAL,
            candles_1h=_hour_bars(),
            entry_time_ms=entry_ms,
            now_ms=now,
            tick=TICK,
            symbol=SYM,
            entry_bar_fetch=lambda *_a, **_k: [],
        )
        is None
    )

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(500, json={"msg": "err"})

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def _client_factory(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs = dict(kwargs)
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(trail_advice.httpx, "Client", _client_factory)
        assert fetch_entry_bar_15m(SYM, entry_ms, _ms_bangkok(2026, 11, 14, 10, 0)) == []
    finally:
        monkey.undo()
    assert seen
    req = seen[0]
    assert req.url.params.get("interval") == "15m"
    assert "X-MBX-APIKEY" not in req.headers
    assert "x-mbx-apikey" not in {k.lower() for k in req.headers.keys()}


def test_entry_bar_decision_log_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Watch decision log tags entry_bar_15m as used / aligned / open."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "off")
    t1005 = _ms_bangkok(2026, 11, 14, 10, 5)
    t0955 = _ms_bangkok(2026, 11, 14, 9, 55)
    start_15 = _ms_bangkok(2026, 11, 14, 9, 15)
    until = _ms_bangkok(2026, 11, 14, 10, 0)

    def fetch(symbol: str, start_ms: int, until_ms: int) -> list[dict]:
        assert symbol == SYM
        assert start_ms == start_15
        assert until_ms == until
        return [
            {
                "timestamp": start_15,
                "open": 0.166,
                "high": 0.167,
                "low": 0.1624,
                "close": 0.164,
            }
        ]

    monkeypatch.setattr(trail_advice, "_ENTRY_BAR_FETCH", fetch)

    def _run(*, entry_ms: int, now_ms: int) -> dict[str, Any]:
        trail_advice._ENTRY_BAR_CACHE.clear()
        trail_advice._ENTRY_BAR_FAIL_MEMO.clear()
        conn = open_db(tmp_path / f"label-{entry_ms}-{now_ms}.db")
        _seed_ada_watch(conn, entry_ms=entry_ms)
        with caplog.at_level(logging.INFO, logger="jarvise_assist.watch"):
            caplog.clear()
            run_assist_watch(
                conn,
                futures=_ada_futures(mark="0.1650", entry_ms=entry_ms),
                now_ms_value=now_ms,
                send=lambda *_a, **_k: True,
            )
        conn.close()
        lines = [
            json.loads(rec.getMessage()[len(DECISION_PREFIX) :])
            for rec in caplog.records
            if rec.name == "jarvise_assist.watch" and rec.getMessage().startswith(DECISION_PREFIX)
        ]
        assert len(lines) == 1
        return lines[0]

    used = _run(entry_ms=_ms_bangkok(2026, 11, 14, 9, 5), now_ms=t1005)
    assert used["entry_bar_15m"] == "used"
    aligned = _run(entry_ms=_ms_bangkok(2026, 11, 14, 9, 0), now_ms=t1005)
    assert aligned["entry_bar_15m"] == "aligned"
    open_tag = _run(entry_ms=_ms_bangkok(2026, 11, 14, 9, 5), now_ms=t0955)
    assert open_tag["entry_bar_15m"] == "open"


def _seed_ada_watch(conn: Any, *, entry_ms: int) -> None:
    upsert_watch_position(
        conn,
        {
            "symbol": SYM,
            "position_side": "BOTH",
            "side": "short",
            "entry": float(ENTRY),
            "qty": 100.0,
            "in_universe": 1,
            "signal_id": None,
            "initial_stop": float(INITIAL),
            "initial_stop_source": "signal",
            "entry_time_ms": int(entry_ms),
            "no_stop_latched": 0,
            "last_seen_ms": int(entry_ms),
            "closed_at_ms": None,
        },
    )
    rows = []
    for bar in _hour_bars():
        rows.append(
            {
                "symbol": SYM,
                "timestamp": int(bar["timestamp"]),
                "timeframe": "1h",
                "open": float(bar["open"]),
                "high": float(bar["high"]),
                "low": float(bar["low"]),
                "close": float(bar["close"]),
                "volume": 1.0,
            }
        )
    upsert_market_technicals(conn, rows)


def _ada_futures(*, mark: str, entry_ms: int) -> FakeFutures:
    return FakeFutures(
        positions=[
            FuturesPosition(
                symbol=SYM,
                position_side="BOTH",
                position_amt=Decimal("-100"),
                entry_price=ENTRY,
                mark_price=Decimal(mark),
                update_time_ms=entry_ms,
            )
        ],
        orders=[
            {
                "symbol": SYM,
                "side": "BUY",
                "type": "STOP_MARKET",
                "reduceOnly": True,
                "stopPrice": "0.1748",
                "origQty": "100",
                "clientAlgoId": "owner-stop",
            }
        ],
        ticks={SYM: TICK},
        steps={SYM: Decimal("1")},
        marks={SYM: Decimal(mark)},
    )


def test_entry_bar_e2e_watch_mn_and_recompute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """(g) Live watch card, mn: decline, dedupe, and move-stop recompute."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    entry_ms = _ms_bangkok(2026, 11, 14, 9, 5)
    t1005 = _ms_bangkok(2026, 11, 14, 10, 5)
    t1006 = _ms_bangkok(2026, 11, 14, 10, 6)
    t1010 = _ms_bangkok(2026, 11, 14, 10, 10)
    start_15 = _ms_bangkok(2026, 11, 14, 9, 15)
    until = _ms_bangkok(2026, 11, 14, 10, 0)

    def fetch(symbol: str, start_ms: int, until_ms: int) -> list[dict]:
        assert symbol == SYM
        assert start_ms == start_15
        assert until_ms == until
        return [
            {
                "timestamp": start_15,
                "open": 0.166,
                "high": 0.167,
                "low": 0.1624,
                "close": 0.164,
            }
        ]

    monkeypatch.setattr(trail_advice, "_ENTRY_BAR_FETCH", fetch)
    trail_advice._ENTRY_BAR_CACHE.clear()

    conn = open_db(tmp_path / "e2e.db")
    _seed_ada_watch(conn, entry_ms=entry_ms)
    sent: list[dict[str, Any]] = []

    def _send(text: str, reply_markup: dict[str, Any] | None = None) -> bool:
        sent.append({"text": text, "reply_markup": reply_markup})
        return True

    run_assist_watch(
        conn,
        futures=_ada_futures(mark="0.1650", entry_ms=entry_ms),
        now_ms_value=t1005,
        send=_send,
    )
    assert len(sent) == 1
    buttons = [button for line in sent[0]["reply_markup"]["inline_keyboard"] for button in line]
    labels = [b["text"] for b in buttons]
    assert "✅ เลื่อน → 0.1670" in labels
    assert "❌ ไม่เลื่อน" in labels
    mn = next(b["callback_data"] for b in buttons if b["callback_data"].startswith("mn:"))
    intent_id = mn.split(":", 1)[1]
    assert get_exec_intent(conn, intent_id)["status"] == "preview"
    advice_row = conn.execute("SELECT status FROM assist_watch_advice WHERE kind='move_stop'").fetchone()
    assert advice_row is not None and advice_row[0] == "sent"

    answers: list[Any] = []
    edits: list[tuple[Any, ...]] = []
    out = process_update(
        conn,
        _callback(mn),
        send=lambda text, **_k: None,
        answer=lambda _cq, text=None: answers.append(text),
        edit=lambda mid, text, *, reply_markup=None: edits.append((mid, text, reply_markup)),
        futures=_ada_futures(mark="0.1650", entry_ms=entry_ms),
        now_ms_value=t1005,
    )
    assert "unhandled_callback" not in out
    assert answers == ["ไม่เลื่อน"]
    assert len(edits) == 1
    assert "(ไม่เลื่อน)" in edits[0][1]
    assert edits[0][2] == {"inline_keyboard": []}
    assert get_exec_intent(conn, intent_id)["status"] == "declined"
    assert (
        conn.execute("SELECT status FROM assist_watch_advice WHERE kind='move_stop'").fetchone()[0] == "sent"
    )

    sent.clear()
    run_assist_watch(
        conn,
        futures=_ada_futures(mark="0.1650", entry_ms=entry_ms),
        now_ms_value=t1010,
        send=_send,
    )
    assert sent == []

    # Fresh card: recompute at 10:06 must still land 0.1670 (not MSG_MOVE_STOP_NO_ADVICE).
    trail_advice._ENTRY_BAR_CACHE.clear()
    fresh_id = insert_exec_intent(
        conn,
        kind="move_stop",
        symbol=SYM,
        side="short",
        preview={"stop": "0.1670", "qty": "100", "advice_text": "advice", "tick": "0.0001"},
        created_at_ms=t1006,
        expires_at_ms=t1006 + MOVE_STOP_TTL_MS,
    )
    claim_exec_intent(conn, fresh_id)
    old = {
        "symbol": SYM,
        "type": "STOP_MARKET",
        "side": "BUY",
        "positionSide": "BOTH",
        "reduceOnly": True,
        "triggerPrice": "0.1748",
        "quantity": "100",
        "algoId": 99,
        "clientAlgoId": "owner-stop",
        "algoStatus": "NEW",
        "workingType": "MARK_PRICE",
    }
    handlers = _move_stop_handlers(
        mark="0.1650",
        entry="0.1673",
        old=old,
        ms_client_id=client_order_id(fresh_id, "ms"),
        new_stop="0.1670",
    )
    # _move_stop_handlers defaults symbol filters to DOGE; retarget exchangeInfo tick.
    info = _exchange_info()
    info["symbols"][0]["symbol"] = SYM
    info["symbols"][0]["filters"][2]["tickSize"] = "0.0001"
    handlers[("GET", "/fapi/v1/exchangeInfo")] = (200, info)
    # positionRisk / openAlgoOrders use DOGE in helpers — override for ADA.
    handlers[("GET", "/fapi/v3/positionRisk")] = (
        200,
        [
            {
                "symbol": SYM,
                "positionSide": "BOTH",
                "positionAmt": "-100",
                "entryPrice": "0.1673",
                "markPrice": "0.1650",
                "unRealizedProfit": "0",
                "liquidationPrice": "0",
                "updateTime": entry_ms,
            }
        ],
    )

    n = {"i": 0}
    ms_row = {
        "symbol": SYM,
        "type": "STOP_MARKET",
        "side": "BUY",
        "positionSide": "BOTH",
        "reduceOnly": True,
        "triggerPrice": "0.1670",
        "quantity": "100",
        "algoId": 100,
        "clientAlgoId": client_order_id(fresh_id, "ms"),
        "algoStatus": "NEW",
        "workingType": "MARK_PRICE",
    }

    def _open_after(_clean: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
        n["i"] += 1
        if n["i"] == 1:
            return (200, [old])
        return (200, [old, ms_row])

    handlers[("GET", "/fapi/v1/openAlgoOrders")] = _open_after
    result = run_move_stop_sequence(
        conn,
        fresh_id,
        trade=_client(RecordingTransport(handlers)),
        now_ms_value=t1006,
    )
    assert result.startswith("✅")
    assert "→ 0.1670" in result
    assert MSG_MOVE_STOP_NO_ADVICE not in result
    conn.close()
