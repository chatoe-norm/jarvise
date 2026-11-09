"""PR-C1: seen-idea link, actual R, pairs-only diff, pre-journal label."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.test_assist import _ms_bangkok
from tests.test_assist_execute import RecordingTransport, _client, _exchange_info, _seed_doge_short
from tests.test_assist_real_trades import FakeFutures, _seed_jv_entry

from jarvise_assist.execute import handle_exec_callback
from jarvise_assist.journal import (
    JOURNAL_SHIPPED_MS,
    format_journal_lines,
    summarize_journal,
)
from jarvise_assist.real_trades import (
    SPEC_START_MS,
    format_bkk_window,
    format_real_lines,
    linked_signal_id,
    slip_entry_r,
    summarize_real_vs_virtual,
    sync_real_trades,
)
from jarvise_assist.sizing import size_signal
from jarvise_assist.store import (
    get_exec_intent,
    get_signal,
    insert_exec_log,
    insert_signal,
    recent_same_idea,
    symbol_has_in_flight_exec,
)
from jarvise_assist.track import TRACK_COHORT, _r_and_pnl, resolve_open_tracks
from jarvise_assist.weekly import format_weekly_summary, summarize_virtual
from jarvise_ingest.db import open_db, schema_version, upsert_market_technicals

BANGKOK = timezone(timedelta(hours=7), name="Asia/Bangkok")
WINDOW_START = int(datetime(2026, 11, 1, tzinfo=BANGKOK).timestamp() * 1000)
WINDOW_END = int(datetime(2026, 11, 7, 12, tzinfo=BANGKOK).timestamp() * 1000)
WINDOW = "01–07 พ.ย. (เวลาไทย)"


def test_bkk_window_label_matches_the_sample() -> None:
    assert format_bkk_window(WINDOW_START, WINDOW_END) == WINDOW


def test_linked_signal_id_prefers_the_card() -> None:
    assert linked_signal_id({"signal_id": "card", "seen_signal_id": "seen"}) == "card"
    assert linked_signal_id({"seen_signal_id": "seen"}) == "seen"
    assert linked_signal_id({}) is None


def test_one_tap_without_signal_id_links_seen_idea(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ec:SYMBOL tap stores seen_signal_id, does not mark taken, and does not block."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    conn = open_db(tmp_path / "ontap.db")
    bar = _ms_bangkok(2026, 11, 5, 3, 0)
    now = _ms_bangkok(2026, 11, 5, 8, 20)
    _seed_doge_short(conn, ts=bar)
    first = size_signal(
        symbol="DOGEUSDT",
        side="short",
        entry=Decimal("0.08"),
        stop=Decimal("0.083"),
        balance=Decimal("1000"),
        risk_pct=Decimal("1"),
        step_size=Decimal("1"),
    )
    assert first is not None
    sid = insert_signal(
        conn,
        first,
        status="sent",
        created_at_ms=now - 60_000,
        expires_at_ms=now + 3_600_000,
        sent_at_ms=now - 60_000,
    )
    # A confirmed intent on another symbol still blocks only that symbol.
    conn.execute(
        """
        INSERT INTO assist_exec_intent (
            id, kind, symbol, side, preview_json, created_at_ms, expires_at_ms, status
        ) VALUES ('aei_eth', 'entry', 'ETHUSDT', 'long', '{}', ?, ?, 'confirmed')
        """,
        (now, now + 60_000),
    )
    conn.commit()
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/positionSide/dual"): (200, {"dualSidePosition": False}),
        ("GET", "/fapi/v1/multiAssetsMargin"): (200, {"multiAssetsMargin": False}),
        ("GET", "/fapi/v3/balance"): (
            200,
            [{"asset": "USDT", "balance": "1000", "availableBalance": "1000"}],
        ),
        ("GET", "/fapi/v3/positionRisk"): (200, []),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.08"}),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
    }
    trade = _client(RecordingTransport(handlers))
    sent: list[str] = []
    out = handle_exec_callback(
        conn,
        "ec:DOGEUSDT:s",
        send=lambda text, **_: sent.append(text) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=trade,
        now_ms_value=now,
    )
    assert out["ok"] is True
    intent = get_exec_intent(conn, str(out["preview"]))
    assert intent is not None
    preview = intent["preview"]
    assert preview["seen_signal_id"] == sid
    assert "signal_id" not in preview
    assert get_signal(conn, sid)["status"] == "sent"
    assert recent_same_idea(conn, symbol="DOGEUSDT", side="short", now_ms=now) is True
    assert symbol_has_in_flight_exec(conn, "DOGEUSDT") is False
    assert symbol_has_in_flight_exec(conn, "ETHUSDT") is True

    insert_exec_log(
        conn,
        intent_id=str(out["preview"]),
        step="market",
        endpoint="/fapi/v1/order",
        client_order_id="jv-ontap-mkt",
        http_status=200,
        created_at_ms=now,
    )
    t0 = now
    orders = [
        {
            "orderId": 11,
            "clientOrderId": "jv-ontap-mkt",
            "symbol": "DOGEUSDT",
            "side": "SELL",
            "type": "MARKET",
            "time": t0,
            "status": "FILLED",
            "reduceOnly": False,
        },
        {
            "orderId": 12,
            "clientOrderId": "jv-ontap-sl0",
            "symbol": "DOGEUSDT",
            "side": "BUY",
            "type": "MARKET",
            "time": t0 + 1_000,
            "status": "FILLED",
            "reduceOnly": True,
        },
    ]
    trades = [
        _fill(1, 11, "DOGEUSDT", "SELL", "0.08", "100", t0),
        _fill(2, 12, "DOGEUSDT", "BUY", "0.079", "100", t0 + 1_000, realized="-0.1"),
    ]
    income = [
        _pnl("DOGEUSDT", "-0.1", t0 + 1_000, 2),
        _funding("DOGEUSDT", "0", t0 + 500),
    ]
    sync_real_trades(
        conn,
        FakeFutures(trades=trades, orders=orders, income=income),  # type: ignore[arg-type]
        since_ms=SPEC_START_MS,
        until_ms=t0 + 10_000,
    )
    row = conn.execute("SELECT signal_id, status FROM assist_real_trade WHERE entry_order_id='11'").fetchone()
    assert row["signal_id"] == sid
    assert row["status"] == "closed"
    assert get_signal(conn, sid)["status"] == "sent"
    assert symbol_has_in_flight_exec(conn, "DOGEUSDT") is False
    conn.close()


def test_second_trade_on_same_signal_is_re_entry(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "re.db")
    sid = "as_re"
    preview = {
        "entry": "100",
        "stop": "90",
        "target": "120",
        "qty": "1",
        "loss_at_stop": "10",
        "signal_id": sid,
        "leverage": 5,
    }
    _seed_jv_entry(conn, intent_id="aei_re1", client_id="jv-re1-mkt", preview=preview, ref_price="100")
    conn.execute(
        """
        INSERT INTO assist_exec_intent (
            id, kind, symbol, side, preview_json, created_at_ms, expires_at_ms, status
        ) VALUES ('aei_re2', 'entry', 'BTCUSDT', 'long', ?, 1, 2, 'done')
        """,
        (json.dumps(preview),),
    )
    insert_exec_log(
        conn,
        intent_id="aei_re2",
        step="market",
        endpoint="/fapi/v1/order",
        client_order_id="jv-re2-mkt",
        http_status=200,
        created_at_ms=SPEC_START_MS + 2_000,
    )
    t0 = SPEC_START_MS + 60_000
    t1 = t0 + 20_000
    orders = [
        _order(101, "jv-re1-mkt", t0, "BUY"),
        _order(102, "jv-re1-sl0", t0 + 1_000, "SELL", reduce_only=True),
        _order(201, "jv-re2-mkt", t1, "BUY"),
        _order(202, "jv-re2-sl0", t1 + 1_000, "SELL", reduce_only=True),
    ]
    trades = [
        _fill(1, 101, "BTCUSDT", "BUY", "100", "1", t0),
        _fill(2, 102, "BTCUSDT", "SELL", "95", "1", t0 + 1_000, realized="-5"),
        _fill(3, 201, "BTCUSDT", "BUY", "100", "1", t1),
        _fill(4, 202, "BTCUSDT", "SELL", "120", "1", t1 + 1_000, realized="20"),
    ]
    income = [
        _pnl("BTCUSDT", "-5", t0 + 1_000, 2),
        _pnl("BTCUSDT", "20", t1 + 1_000, 4),
        _funding("BTCUSDT", "0", t0 + 500),
        _funding("BTCUSDT", "0", t1 + 500),
    ]
    _lonely_track(conn, signal_id="as_lonely", entry=100, stop=90, loss=10, net_r=0.4, exit_at_ms=t0 + 2_000)
    _lonely_track(conn, signal_id="as_flat", entry=50, stop=50, loss=1, net_r=0.0, exit_at_ms=t0 + 3_000)
    until = t1 + 10_000
    sync_real_trades(
        conn,
        FakeFutures(trades=trades, orders=orders, income=income),  # type: ignore[arg-type]
        since_ms=SPEC_START_MS,
        until_ms=until,
    )
    summary = summarize_real_vs_virtual(conn, since_ms=SPEC_START_MS, until_ms=until)
    # First net = -5/10 = -0.5R. Second = 20/10 = 2R. Virtual track is +0.8R.
    assert summary["n_re_entry"] == 1
    assert summary["n_pairs"] == 1
    assert summary["mean_net_r"] == pytest.approx((-0.5 + 2.0) / 2)
    assert summary["mean_diff_r"] == pytest.approx(-0.5 - 0.8)
    assert summary["n_real_without_pair"] == 1
    assert summary["n_virtual_without_real"] == 1
    assert summary["virtual_dropped_n"] == 1
    text = "\n".join(format_real_lines(summary))
    assert "re_entry " in text
    assert "n=1" in text.split("re_entry ", 1)[1].split("\n", 1)[0]
    conn.close()


def test_actual_r_uses_fill_versus_planned_stop(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "slip.db")
    preview = {
        "entry": "100",
        "stop": "90",
        "target": "120",
        "qty": "1",
        "loss_at_stop": "10",
        "signal_id": "as_slip",
        "leverage": 5,
    }
    _seed_jv_entry(conn, intent_id="aei_slip", client_id="jv-slip-mkt", preview=preview, ref_price="100")
    t0 = SPEC_START_MS + 60_000
    # Fill 102 vs stop 90 → risk_actual 12. Planned loss_at_stop is 10.
    # net = 8 + (-0.2) + (-0.1) = 7.7
    orders = [
        _order(11, "jv-slip-mkt", t0, "BUY"),
        _order(12, "jv-slip-sl0", t0 + 1_000, "SELL", reduce_only=True),
    ]
    trades = [
        _fill(1, 11, "BTCUSDT", "BUY", "102", "1", t0, commission="0.1"),
        _fill(2, 12, "BTCUSDT", "SELL", "110", "1", t0 + 1_000, commission="0.1", realized="8"),
    ]
    income = [
        _pnl("BTCUSDT", "8", t0 + 1_000, 2),
        _funding("BTCUSDT", "-0.1", t0 + 500),
    ]
    until = t0 + 10_000
    sync_real_trades(
        conn,
        FakeFutures(trades=trades, orders=orders, income=income),  # type: ignore[arg-type]
        since_ms=SPEC_START_MS,
        until_ms=until,
    )
    row = conn.execute(
        """
        SELECT entry_fill_avg, entry_qty, risk_actual_usdt, net_r_real, net_r_actual
        FROM assist_real_trade WHERE entry_order_id='11'
        """
    ).fetchone()
    net = 8.0 - 0.2 - 0.1
    assert row["entry_fill_avg"] == pytest.approx(102)
    assert row["entry_qty"] == pytest.approx(1)
    assert row["risk_actual_usdt"] == pytest.approx(12)
    assert row["net_r_real"] == pytest.approx(net / 10)
    assert row["net_r_actual"] == pytest.approx(net / 12)
    summary = summarize_real_vs_virtual(conn, since_ms=SPEC_START_MS, until_ms=until)
    assert "mean_slip_r" not in summary
    # Long fill 102 vs plan 100, stop 90: (102-100)/10 = +0.2R (worse).
    assert summary["slip_entry_r_n"] == 1
    assert summary["mean_slip_entry_r"] == pytest.approx(0.2)
    conn.close()


def test_partial_entry_uses_weighted_average_and_filled_qty(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "part.db")
    preview = {
        "entry": "100",
        "stop": "90",
        "target": "120",
        "qty": "4",
        "loss_at_stop": "40",
        "signal_id": "as_part",
        "leverage": 5,
    }
    _seed_jv_entry(conn, intent_id="aei_part", client_id="jv-part-mkt", preview=preview, ref_price="100")
    t0 = SPEC_START_MS + 60_000
    orders = [
        _order(11, "jv-part-mkt", t0, "BUY"),
        _order(12, "jv-part-sl0", t0 + 1_000, "SELL", reduce_only=True),
    ]
    trades = [
        _fill(1, 11, "BTCUSDT", "BUY", "100", "1", t0),
        _fill(2, 11, "BTCUSDT", "BUY", "110", "3", t0 + 10),
        _fill(3, 12, "BTCUSDT", "SELL", "120", "4", t0 + 1_000, realized="50"),
    ]
    income = [
        _pnl("BTCUSDT", "50", t0 + 1_000, 3),
        _funding("BTCUSDT", "0", t0 + 500),
    ]
    until = t0 + 10_000
    sync_real_trades(
        conn,
        FakeFutures(trades=trades, orders=orders, income=income),  # type: ignore[arg-type]
        since_ms=SPEC_START_MS,
        until_ms=until,
    )
    row = conn.execute(
        """
        SELECT entry_fill_avg, entry_qty, risk_actual_usdt, net_r_real, net_r_actual
        FROM assist_real_trade WHERE entry_order_id='11'
        """
    ).fetchone()
    assert row["entry_qty"] == pytest.approx(4)
    assert row["entry_fill_avg"] == pytest.approx(107.5)
    assert row["risk_actual_usdt"] == pytest.approx(70)
    assert row["net_r_real"] == pytest.approx(50 / 40)
    assert row["net_r_actual"] == pytest.approx(50 / 70)
    conn.close()


def test_missing_stop_or_zero_risk_leaves_actual_r_none(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "none.db")
    missing = {
        "entry": "100",
        "target": "120",
        "qty": "1",
        "loss_at_stop": "10",
        "signal_id": "as_miss",
        "leverage": 5,
    }
    flat = {
        "entry": "100",
        "stop": "100",
        "target": "120",
        "qty": "1",
        "loss_at_stop": "10",
        "signal_id": "as_zero",
        "leverage": 5,
    }
    _seed_jv_entry(conn, intent_id="aei_miss", client_id="jv-miss-mkt", preview=missing, ref_price="100")
    _seed_jv_entry(conn, intent_id="aei_zero", client_id="jv-zero-mkt", preview=flat, ref_price="100")
    t0 = SPEC_START_MS + 60_000
    orders = [
        _order(11, "jv-miss-mkt", t0, "BUY"),
        _order(12, "jv-miss-sl0", t0 + 1_000, "SELL", reduce_only=True),
        _order(21, "jv-zero-mkt", t0 + 5_000, "BUY"),
        _order(22, "jv-zero-sl0", t0 + 6_000, "SELL", reduce_only=True),
    ]
    trades = [
        _fill(1, 11, "BTCUSDT", "BUY", "100", "1", t0),
        _fill(2, 12, "BTCUSDT", "SELL", "110", "1", t0 + 1_000, realized="10"),
        _fill(3, 21, "BTCUSDT", "BUY", "100", "1", t0 + 5_000),
        _fill(4, 22, "BTCUSDT", "SELL", "110", "1", t0 + 6_000, realized="10"),
    ]
    income = [
        _pnl("BTCUSDT", "10", t0 + 1_000, 2),
        _pnl("BTCUSDT", "10", t0 + 6_000, 4),
        _funding("BTCUSDT", "0", t0 + 500),
        _funding("BTCUSDT", "0", t0 + 5_500),
    ]
    sync_real_trades(
        conn,
        FakeFutures(trades=trades, orders=orders, income=income),  # type: ignore[arg-type]
        since_ms=SPEC_START_MS,
        until_ms=t0 + 20_000,
    )
    miss = conn.execute(
        "SELECT risk_actual_usdt, net_r_actual FROM assist_real_trade WHERE entry_order_id='11'"
    ).fetchone()
    zero = conn.execute(
        "SELECT risk_actual_usdt, net_r_actual FROM assist_real_trade WHERE entry_order_id='21'"
    ).fetchone()
    assert miss["risk_actual_usdt"] is None
    assert miss["net_r_actual"] is None
    assert zero["risk_actual_usdt"] == pytest.approx(0)
    assert zero["net_r_actual"] is None
    conn.close()


def test_slip_entry_r_sign_for_worse_and_better_fills() -> None:
    # Distance is 10 in every case. A worse fill is positive for both sides.
    assert slip_entry_r(side="long", entry_fill_avg=102, plan_entry=100, planned_stop=90) == pytest.approx(
        0.2
    )
    assert slip_entry_r(side="short", entry_fill_avg=98, plan_entry=100, planned_stop=110) == pytest.approx(
        0.2
    )
    assert slip_entry_r(side="long", entry_fill_avg=98, plan_entry=100, planned_stop=90) == pytest.approx(
        -0.2
    )
    assert slip_entry_r(side="short", entry_fill_avg=102, plan_entry=100, planned_stop=110) == pytest.approx(
        -0.2
    )
    assert slip_entry_r(side="long", entry_fill_avg=100, plan_entry=100, planned_stop=100) is None
    assert slip_entry_r(side="short", entry_fill_avg=100, plan_entry=100, planned_stop=100) is None
    assert slip_entry_r(side="long", entry_fill_avg=None, plan_entry=100, planned_stop=90) is None
    assert slip_entry_r(side="long", entry_fill_avg=102, plan_entry=None, planned_stop=90) is None
    assert slip_entry_r(side="long", entry_fill_avg=102, plan_entry=100, planned_stop=None) is None


def test_closed_real_without_virtual_counts_as_unpaired(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "unpaired.db")
    ts = SPEC_START_MS + 1_000
    conn.execute(
        """
        INSERT INTO assist_signal (
            id, symbol, side, entry, stop, target, qty, notional, leverage, loss_at_stop,
            created_at_ms, expires_at_ms, status, sent_at_ms
        ) VALUES ('as_pair', 'BTCUSDT', 'long', 100, 90, 120, 1, 100, 2, 10, ?, ?, 'taken', ?)
        """,
        (ts, ts + 1, ts),
    )
    conn.execute(
        """
        INSERT INTO assist_track (
            signal_id, symbol, side, entry, stop, target, qty, sent_at_ms,
            exit_at_ms, exit_px, exit_reason, gross_r, net_r, cohort, kind
        ) VALUES ('as_pair', 'BTCUSDT', 'long', 100, 90, 120, 1, ?, ?, 110, 'flat', 0.5, 0.4, ?, 'sent')
        """,
        (ts, ts + 2_000, TRACK_COHORT),
    )
    conn.execute(
        """
        INSERT INTO assist_real_trade (
            symbol, side, entry_order_id, entry_client_id, status, updated_at_ms,
            entry_time_ms, signal_id, net_r_real, net_r_virtual
        ) VALUES ('BTCUSDT', 'long', 'paired', 'jv-pair-mkt', 'closed', ?, ?, 'as_pair', 0.5, 0.4)
        """,
        (ts, ts),
    )
    conn.execute(
        """
        INSERT INTO assist_real_trade (
            symbol, side, entry_order_id, entry_client_id, status, updated_at_ms,
            entry_time_ms, signal_id, net_r_real, net_r_virtual
        ) VALUES ('ETHUSDT', 'long', 'solo', 'jv-solo-mkt', 'closed', ?, ?, NULL, -0.3, NULL)
        """,
        (ts + 1, ts + 1),
    )
    conn.commit()
    summary = summarize_real_vs_virtual(conn, since_ms=SPEC_START_MS, until_ms=ts + 10_000)
    assert summary["n_pairs"] == 1
    assert summary["n_real_without_pair"] == 1
    conn.close()


def test_labels_state_unit_bangkok_window_and_n() -> None:
    summary = {
        "closed_n": 5,
        "mean_net_r": 0.21,
        "mean_net_r_actual": 0.18,
        "mean_slip_entry_r": 0.03,
        "slip_entry_r_n": 5,
        "mean_diff_r": 0.05,
        "n_pairs": 3,
        "n_real_without_pair": 1,
        "n_virtual_without_real": 2,
        "n_re_entry": 1,
        "virtual_dropped_n": 1,
        "window_start_ms": WINDOW_START,
        "window_end_ms": WINDOW_END,
        "mean_slip_entry_bps": 1.2,
        "p90_slip_entry_bps": 2.0,
        "entry_slip_n": 5,
        "confirm_n": 4,
        "preview_n": 1,
        "mean_drift_bps": 0.4,
        "mean_slip_stop_bps": 3.0,
        "p90_slip_stop_bps": 4.0,
        "stop_n": 2,
        "unmatched_n": 0,
        "unmatched_jv_before_start": 0,
        "unmatched_jv_no_log": 0,
        "unmatched_owner": 0,
        "unknown_n": 0,
        "open_n": 0,
        "unlinked_n": 0,
        "non_usdt_fee_n": 0,
        "stop_trigger_missing_n": 0,
        "target_n": 20,
    }
    lines = format_real_lines(summary)
    text_block = "\n".join(lines)
    assert lines[0] == (
        "ไม้จริง 01–07 พ.ย. (เวลาไทย): n=5 · เฉลี่ย +0.21R (planned) · "
        "+0.18R (actual) · ต้องการอย่างน้อย 20 ไม้ก่อนสรุป"
    )
    assert "slip เข้าเป็น R 01–07 พ.ย. (เวลาไทย): n=5 · เฉลี่ย +0.03R (บวก = เข้าแย่กว่าแผน)" in lines
    assert "คู่จริง-จำลอง 01–07 พ.ย. (เวลาไทย): n=3 · ต่างเฉลี่ย (จริง − จำลอง) +0.05R" in lines
    assert "slippage ใน R" not in text_block
    assert "actual − planned" not in text_block
    assert "mean_slip_r" not in summary
    for line in lines:
        assert WINDOW in line
        assert "n=" in line
    assert any("bps" in line for line in lines)
    text = format_weekly_summary(
        counts={"sent": 0, "skipped": 0, "taken": 0},
        virtual={"n": 0, "dropped_risk_n": 1},
        real={"universe_usdt": 0.0},
        closed_all=0,
        real_vs_virtual=summary,
        window_start_ms=WINDOW_START,
        window_end_ms=WINDOW_END,
    )
    assert lines[0] in text
    assert f"ไม้เสมือนที่ความเสี่ยง ≤ 0 {WINDOW}: n=1 · ไม่นับในค่าเฉลี่ย R" in text


def test_virtual_zero_risk_is_dropped_and_stored_as_none(tmp_path: Path) -> None:
    gross, net, _fee, _pnl = _r_and_pnl(
        side="long", entry=100.0, stop=100.0, exit_px=90.0, qty=1.0, loss_at_stop=10.0
    )
    assert gross is None and net is None
    gross, net, _fee, _pnl = _r_and_pnl(
        side="long", entry=100.0, stop=90.0, exit_px=90.0, qty=1.0, loss_at_stop=0.0
    )
    assert net is None

    kept = {
        "exit_reason": "flat",
        "gross_r": 1.0,
        "net_r": 1.0,
        "pnl_usdt": 10.0,
        "entry": 100.0,
        "stop": 90.0,
        "loss_at_stop": 10.0,
    }
    same = {**kept, "entry": 100.0, "stop": 100.0, "net_r": 0.0}
    zero_loss = {**kept, "loss_at_stop": 0.0, "net_r": 0.0}
    stats = summarize_virtual([kept, same, zero_loss])
    assert stats["n"] == 1
    assert stats["dropped_risk_n"] == 2
    assert stats["mean_r"] == pytest.approx(1.0)

    conn = open_db(tmp_path / "zero.db")
    sent = _ms_bangkok(2026, 11, 5, 8, 0)
    conn.execute(
        """
        INSERT INTO assist_signal (
            id, symbol, side, entry, stop, target, qty, notional, leverage, loss_at_stop,
            created_at_ms, expires_at_ms, status, sent_at_ms
        ) VALUES ('as_eq', 'BTCUSDT', 'long', 100, 100, 120, 1, 100, 2, 10, ?, ?, 'sent', ?)
        """,
        (sent, sent + 1, sent),
    )
    conn.execute(
        """
        INSERT INTO assist_track (
            signal_id, symbol, side, entry, stop, target, qty, sent_at_ms, cohort, kind
        ) VALUES ('as_eq', 'BTCUSDT', 'long', 100, 100, 120, 1, ?, ?, 'sent')
        """,
        (sent, TRACK_COHORT),
    )
    upsert_market_technicals(
        conn,
        [
            {
                "timestamp": sent + 3_600_000,
                "symbol": "BTCUSDT",
                "timeframe": "1h",
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 99.0,
                "volume": 1.0,
            }
        ],
    )
    assert resolve_open_tracks(conn, now_ms=sent + 2 * 3_600_000)["n"] == 1
    stored = conn.execute("SELECT net_r FROM assist_track WHERE signal_id='as_eq'").fetchone()
    assert stored["net_r"] is None
    conn.close()


def test_pre_journal_trade_is_labelled_and_excluded_from_grades(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "pre.db")
    before = JOURNAL_SHIPPED_MS - 60_000
    after = JOURNAL_SHIPPED_MS + 60_000
    _journal_trade(conn, oid="old", exit_ms=before, grade="pass", net=2.0)
    _journal_trade(conn, oid="new", exit_ms=after, grade="fail", net=-1.0)
    _journal_trade(conn, oid="bare", exit_ms=before - 1_000, grade=None, net=9.0, with_journal=False)
    summary = summarize_journal(conn, since_ms=before - 10_000, until_ms=after + 10_000)
    assert summary["pre_journal_n"] == 2
    assert summary["n"] == 1
    assert summary["pass_n"] == 0
    assert summary["fail_n"] == 1
    assert summary["ungraded"] == 0
    assert summary["pass_rate"] == pytest.approx(0.0)
    lines = format_journal_lines(summary)
    text = format_weekly_summary(
        counts={"sent": 0, "skipped": 0, "taken": 0},
        virtual={"n": 0},
        real={"universe_usdt": 0.0},
        closed_all=0,
        journal=summary,
    )
    assert any(line.startswith("ก่อนมีบันทึก ") and "n=2" in line for line in lines)
    assert "ก่อนมีบันทึก" in text
    assert "ยังไม่ให้เกรด 0" in text
    conn.close()


def test_actual_r_migration_is_additive(tmp_path: Path) -> None:
    path = tmp_path / "mig.db"
    conn = open_db(path)
    conn.execute(
        """
        INSERT INTO assist_real_trade (
            symbol, side, entry_order_id, status, updated_at_ms, net_r_real, entry_time_ms
        ) VALUES ('BTCUSDT', 'long', 'keep', 'closed', 1, 1.25, 10)
        """
    )
    conn.execute("ALTER TABLE assist_real_trade DROP COLUMN risk_actual_usdt")
    conn.execute("ALTER TABLE assist_real_trade DROP COLUMN net_r_actual")
    conn.execute("PRAGMA user_version = 23")
    conn.commit()
    conn.close()

    conn = open_db(path)
    assert schema_version(conn) == 24
    row = conn.execute(
        """
        SELECT net_r_real, entry_time_ms, risk_actual_usdt, net_r_actual
        FROM assist_real_trade WHERE entry_order_id='keep'
        """
    ).fetchone()
    assert row["net_r_real"] == pytest.approx(1.25)
    assert row["entry_time_ms"] == 10
    assert row["risk_actual_usdt"] is None
    assert row["net_r_actual"] is None
    conn.close()


def _fill(
    trade_id: int,
    order_id: int,
    symbol: str,
    side: str,
    price: str,
    qty: str,
    time_ms: int,
    *,
    commission: str = "0",
    realized: str = "0",
) -> dict[str, Any]:
    return {
        "id": trade_id,
        "orderId": order_id,
        "symbol": symbol,
        "side": side,
        "price": price,
        "qty": qty,
        "time": time_ms,
        "commission": commission,
        "commissionAsset": "USDT",
        "realizedPnl": realized,
    }


def _order(
    order_id: int,
    client_id: str,
    time_ms: int,
    side: str,
    *,
    reduce_only: bool = False,
    symbol: str = "BTCUSDT",
) -> dict[str, Any]:
    return {
        "orderId": order_id,
        "clientOrderId": client_id,
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "time": time_ms,
        "status": "FILLED",
        "reduceOnly": reduce_only,
    }


def _pnl(symbol: str, income: str, time_ms: int, trade_id: int) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "incomeType": "REALIZED_PNL",
        "income": income,
        "time": time_ms,
        "tradeId": trade_id,
    }


def _funding(symbol: str, income: str, time_ms: int) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "incomeType": "FUNDING_FEE",
        "income": income,
        "time": time_ms,
    }


def _lonely_track(
    conn: Any,
    *,
    signal_id: str,
    entry: float,
    stop: float,
    loss: float,
    net_r: float,
    exit_at_ms: int | None = None,
) -> None:
    ts = SPEC_START_MS + 1_000
    closed_at = ts + 3_600_000 if exit_at_ms is None else int(exit_at_ms)
    conn.execute(
        """
        INSERT INTO assist_signal (
            id, symbol, side, entry, stop, target, qty, notional, leverage, loss_at_stop,
            created_at_ms, expires_at_ms, status
        ) VALUES (?, 'ETHUSDT', 'long', ?, ?, 120, 1, 100, 2, ?, ?, ?, 'sent')
        """,
        (signal_id, entry, stop, loss, ts, ts + 1),
    )
    conn.execute(
        """
        INSERT INTO assist_track (
            signal_id, symbol, side, entry, stop, target, qty, sent_at_ms,
            exit_at_ms, exit_px, exit_reason, gross_r, net_r, cohort, kind
        ) VALUES (?, 'ETHUSDT', 'long', ?, ?, 120, 1, ?, ?, 110, 'flat', ?, ?, ?, 'sent')
        """,
        (signal_id, entry, stop, ts, closed_at, net_r, net_r, TRACK_COHORT),
    )
    conn.commit()


def _journal_trade(
    conn: Any,
    *,
    oid: str,
    exit_ms: int,
    grade: str | None,
    net: float,
    with_journal: bool = True,
) -> None:
    conn.execute(
        """
        INSERT INTO assist_real_trade (
            symbol, side, entry_order_id, status, updated_at_ms,
            entry_time_ms, exit_time_ms, loss_at_stop, net_r_real
        ) VALUES ('ADAUSDT', 'long', ?, 'closed', ?, ?, ?, 1.0, ?)
        """,
        (oid, exit_ms, exit_ms - 1_000, exit_ms, net),
    )
    if with_journal:
        conn.execute(
            """
            INSERT INTO assist_journal (
                entry_order_id, auto_checks_json, grade, mistake_tags, note,
                card_message_id, created_at_ms, graded_at_ms
            ) VALUES (?, '{}', ?, NULL, NULL, 1, ?, NULL)
            """,
            (oid, grade, exit_ms),
        )
    conn.commit()
