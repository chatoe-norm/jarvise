"""Observability: one assist_watch_decision JSON line per universe position."""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from tests.test_assist import FakeFutures, _ms_bangkok, _seed_candle

from jarvise.jobs import configure_process_logging
from jarvise_assist.futures import FuturesPosition
from jarvise_assist.sizing import size_signal
from jarvise_assist.store import insert_signal, upsert_watch_position, write_settings
from jarvise_assist.watch import run_assist_watch
from jarvise_ingest.db import open_db, upsert_market_technicals

ONE_H_MS = 3_600_000
PREFIX = "assist_watch_decision "
REQUIRED_FIELDS = {
    "symbol",
    "side",
    "entry_time_ms",
    "milestone",
    "best_extreme",
    "last_closed_bar_ts",
    "threshold_price",
    "suggested",
    "compare_stop",
    "compare_source",
    "mark",
    "r_now",
    "progress_sent",
    "decision",
    "reason",
}
FORBIDDEN = ("api", "secret", "signature", "token")


def _decision_lines(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for rec in caplog.records:
        if rec.name != "jarvise_assist.watch":
            continue
        msg = rec.getMessage()
        if not msg.startswith(PREFIX):
            continue
        assert rec.levelno == logging.INFO
        payload = json.loads(msg[len(PREFIX) :])
        assert isinstance(payload, dict)
        out.append(payload)
    return out


def _assert_safe_fields(payload: dict[str, Any]) -> None:
    for key, value in payload.items():
        blob = f"{key}={value}".lower()
        for bad in FORBIDDEN:
            assert bad not in blob, f"forbidden {bad!r} in {key}={value!r}"


def _assert_position_payload(payload: dict[str, Any]) -> None:
    assert REQUIRED_FIELDS <= set(payload)
    _assert_safe_fields(payload)


def _btc_long_setup(
    conn: Any,
    *,
    day: int,
    entry_ts: int,
    high: float,
    mark: Decimal,
    stop_px: str = "90",
) -> FakeFutures:
    _seed_candle(
        conn,
        symbol="BTCUSDT",
        ts=entry_ts,
        open_=100,
        high=high,
        low=99,
        close=min(high, 105),
        atr=6.67,
        timeframe="1h",
    )
    sized = size_signal(
        symbol="BTCUSDT",
        side="long",
        entry=Decimal("100"),
        stop=Decimal("90"),
        balance=Decimal("1000"),
        risk_pct=Decimal("2"),
        step_size=Decimal("0.001"),
    )
    assert sized is not None
    insert_signal(
        conn,
        sized,
        status="sent",
        created_at_ms=entry_ts,
        expires_at_ms=day + 3_600_000,
        sent_at_ms=entry_ts,
    )
    pos = FuturesPosition(
        symbol="BTCUSDT",
        position_side="BOTH",
        position_amt=Decimal("0.1"),
        entry_price=Decimal("100"),
        mark_price=mark,
        update_time_ms=entry_ts,
    )
    return FakeFutures(
        positions=[pos],
        orders=[
            {
                "symbol": "BTCUSDT",
                "side": "SELL",
                "type": "STOP_MARKET",
                "reduceOnly": True,
                "stopPrice": stop_px,
            }
        ],
        marks={"BTCUSDT": mark},
    )


def test_decision_sent_held_skipped_not_tighter_not_reached_no_initial_stop(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    day = _ms_bangkok(2026, 10, 27, 14, 0)
    night = _ms_bangkok(2026, 10, 27, 0, 0)
    entry_ts = day - 2 * ONE_H_MS

    # --- sent (daytime move-stop) ---
    conn = open_db(tmp_path / "sent.db")
    fake = _btc_long_setup(conn, day=day, entry_ts=entry_ts, high=106, mark=Decimal("105"))
    with caplog.at_level(logging.INFO, logger="jarvise_assist.watch"):
        run_assist_watch(conn, futures=fake, now_ms_value=day, send=lambda *_a, **_k: True)
    sent_rows = _decision_lines(caplog)
    assert len(sent_rows) == 1
    _assert_position_payload(sent_rows[0])
    assert sent_rows[0]["decision"] == "sent"
    assert sent_rows[0]["symbol"] == "BTCUSDT"
    assert sent_rows[0]["milestone"] == "breakeven"
    assert sent_rows[0]["progress_sent"] == [0.5]
    conn.close()
    caplog.clear()

    # --- skipped_dedupe (same level again) ---
    conn = open_db(tmp_path / "dedupe.db")
    fake = _btc_long_setup(conn, day=day, entry_ts=entry_ts, high=106, mark=Decimal("105"))
    run_assist_watch(conn, futures=fake, now_ms_value=day, send=lambda *_a, **_k: True)
    with caplog.at_level(logging.INFO, logger="jarvise_assist.watch"):
        run_assist_watch(conn, futures=fake, now_ms_value=day + 60_000, send=lambda *_a, **_k: True)
    dedupe_rows = _decision_lines(caplog)
    assert len(dedupe_rows) == 1
    _assert_position_payload(dedupe_rows[0])
    assert dedupe_rows[0]["decision"] == "skipped_dedupe"
    assert dedupe_rows[0]["progress_sent"] == []
    conn.close()
    caplog.clear()

    # --- held (night) ---
    conn = open_db(tmp_path / "held.db")
    night_entry = night - 2 * ONE_H_MS
    fake = _btc_long_setup(conn, day=night, entry_ts=night_entry, high=106, mark=Decimal("105"))
    with caplog.at_level(logging.INFO, logger="jarvise_assist.watch"):
        out = run_assist_watch(conn, futures=fake, now_ms_value=night, send=lambda *_a, **_k: True)
    assert out["night_hold"] is True
    held_rows = _decision_lines(caplog)
    assert len(held_rows) == 1
    _assert_position_payload(held_rows[0])
    assert held_rows[0]["decision"] == "held"
    assert held_rows[0]["progress_sent"] == []
    conn.close()
    caplog.clear()

    # --- not_reached ---
    conn = open_db(tmp_path / "not_reached.db")
    fake = _btc_long_setup(conn, day=day, entry_ts=entry_ts, high=101, mark=Decimal("101"))
    with caplog.at_level(logging.INFO, logger="jarvise_assist.watch"):
        run_assist_watch(conn, futures=fake, now_ms_value=day, send=lambda *_a, **_k: True)
    nr_rows = _decision_lines(caplog)
    assert len(nr_rows) == 1
    _assert_position_payload(nr_rows[0])
    assert nr_rows[0]["decision"] == "not_reached"
    assert nr_rows[0]["milestone"] == "none"
    assert nr_rows[0]["suggested"] is None
    conn.close()
    caplog.clear()

    # --- not_tighter (exchange stop already tighter than advice) ---
    conn = open_db(tmp_path / "not_tighter.db")
    doge_entry = day - 2 * ONE_H_MS
    upsert_watch_position(
        conn,
        {
            "symbol": "DOGEUSDT",
            "position_side": "BOTH",
            "side": "short",
            "entry": 0.1673,
            "qty": 100.0,
            "in_universe": 1,
            "signal_id": None,
            "initial_stop": 0.1748,
            "initial_stop_source": "signal",
            "entry_time_ms": doge_entry,
            "no_stop_latched": 0,
            "last_seen_ms": doge_entry,
            "closed_at_ms": None,
        },
    )
    upsert_market_technicals(
        conn,
        [
            {
                "symbol": "DOGEUSDT",
                "timestamp": doge_entry,
                "timeframe": "1h",
                "open": 0.1673,
                "high": 0.1680,
                "low": 0.16355,
                "close": 0.1640,
                "volume": 1.0,
            }
        ],
    )
    pos = FuturesPosition(
        symbol="DOGEUSDT",
        position_side="BOTH",
        position_amt=Decimal("-100"),
        entry_price=Decimal("0.1673"),
        mark_price=Decimal("0.164"),
        update_time_ms=doge_entry,
    )
    fake = FakeFutures(
        positions=[pos],
        orders=[
            {
                "symbol": "DOGEUSDT",
                "side": "BUY",
                "type": "STOP_MARKET",
                "reduceOnly": True,
                "stopPrice": "0.1668",
                "origQty": "100",
            }
        ],
        ticks={"DOGEUSDT": Decimal("0.0001")},
        steps={"DOGEUSDT": Decimal("1")},
        marks={"DOGEUSDT": Decimal("0.164")},
    )
    with caplog.at_level(logging.INFO, logger="jarvise_assist.watch"):
        run_assist_watch(conn, futures=fake, now_ms_value=day, send=lambda *_a, **_k: True)
    nt_rows = _decision_lines(caplog)
    assert len(nt_rows) == 1
    _assert_position_payload(nt_rows[0])
    assert nt_rows[0]["decision"] == "not_tighter"
    assert nt_rows[0]["compare_source"] == "exchange"
    assert nt_rows[0]["suggested"] is not None
    conn.close()
    caplog.clear()

    # --- no_initial_stop (no ATR candle, no linkable signal) ---
    conn = open_db(tmp_path / "no_stop.db")
    pos = FuturesPosition(
        symbol="ADAUSDT",
        position_side="BOTH",
        position_amt=Decimal("100"),
        entry_price=Decimal("0.40"),
        mark_price=Decimal("0.40"),
        update_time_ms=day - ONE_H_MS,
    )
    fake = FakeFutures(positions=[pos], orders=[])
    with caplog.at_level(logging.INFO, logger="jarvise_assist.watch"):
        run_assist_watch(conn, futures=fake, now_ms_value=day, send=lambda *_a, **_k: True)
    nis_rows = _decision_lines(caplog)
    assert len(nis_rows) == 1
    _assert_position_payload(nis_rows[0])
    assert nis_rows[0]["decision"] == "no_initial_stop"
    assert nis_rows[0]["symbol"] == "ADAUSDT"
    assert nis_rows[0]["progress_sent"] == []
    conn.close()


def test_decision_assist_disabled_one_line_no_symbol(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    conn = open_db(tmp_path / "off.db")
    write_settings(conn, enabled=False)
    day = _ms_bangkok(2026, 10, 27, 14, 0)
    with caplog.at_level(logging.INFO, logger="jarvise_assist.watch"):
        out = run_assist_watch(
            conn,
            futures=FakeFutures(positions=[]),
            now_ms_value=day,
            send=lambda *_a, **_k: True,
        )
    assert out.get("assist_enabled") is False
    assert out.get("ok") is True
    assert out.get("skipped") is None
    rows = _decision_lines(caplog)
    assert len(rows) == 1
    assert rows[0]["decision"] == "watch_only"
    assert rows[0]["reason"] == "assist_disabled"
    assert "symbol" not in rows[0] or rows[0].get("symbol") is None
    _assert_safe_fields(rows[0])
    conn.close()


def test_decision_send_failed_maps_failed_emit(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Move-stop soft-fails to held; force hold_ok=False path → send_failed."""
    import jarvise_assist.watch as watch_mod

    day = _ms_bangkok(2026, 10, 27, 14, 0)
    entry_ts = day - 2 * ONE_H_MS
    conn = open_db(tmp_path / "fail.db")
    fake = _btc_long_setup(conn, day=day, entry_ts=entry_ts, high=106, mark=Decimal("101"))
    # Mark below 0.5R so progress does not fire; move-stop still suggested from candles.
    fake.positions[0] = FuturesPosition(
        symbol="BTCUSDT",
        position_side="BOTH",
        position_amt=Decimal("0.1"),
        entry_price=Decimal("100"),
        mark_price=Decimal("101"),
        update_time_ms=entry_ts,
    )
    fake.marks["BTCUSDT"] = Decimal("101")

    real_emit = watch_mod._emit_advice

    def _emit_fail(*args: Any, **kwargs: Any) -> str:
        kwargs = dict(kwargs)
        kwargs["hold_ok"] = False
        kwargs["night"] = False
        return real_emit(*args, **kwargs)

    monkeypatch.setattr(watch_mod, "_emit_advice", _emit_fail)

    def send_fail(*_a: Any, **_k: Any) -> bool:
        return False

    with caplog.at_level(logging.INFO, logger="jarvise_assist.watch"):
        run_assist_watch(conn, futures=fake, now_ms_value=day, send=send_fail)
    rows = _decision_lines(caplog)
    assert len(rows) == 1
    _assert_position_payload(rows[0])
    assert rows[0]["decision"] == "send_failed"
    conn.close()


def test_watch_logger_reaches_configure_process_logging_handler() -> None:
    stream = StringIO()
    log = logging.getLogger("jarvise_assist")
    prior_handlers = list(log.handlers)
    prior_level = log.level
    prior_propagate = log.propagate
    try:
        log.handlers.clear()
        configure_process_logging(stream=stream)
        logging.getLogger("jarvise_assist.watch").info(
            "assist_watch_decision %s",
            json.dumps({"decision": "not_reached", "symbol": "BTCUSDT"}, sort_keys=True),
        )
        text = stream.getvalue()
        assert "assist_watch_decision" in text
        assert "jarvise_assist.watch" in text
        assert "not_reached" in text
    finally:
        log.handlers.clear()
        log.handlers.extend(prior_handlers)
        log.setLevel(prior_level)
        log.propagate = prior_propagate
