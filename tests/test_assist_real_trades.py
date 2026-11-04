"""Real-vs-virtual report: window walk, matching, slippage, weekly lines (fakes only)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.test_assist import _ms_bangkok

from jarvise_assist.futures import FAPI_HISTORY_WINDOW_MS
from jarvise_assist.real_trades import (
    SPEC_START_MS,
    adverse_bps,
    classify_exit_kind,
    fetch_all_orders,
    fetch_all_user_trades,
    format_real_lines,
    summarize_real_vs_virtual,
    sync_real_trades,
    time_windows,
)
from jarvise_assist.store import insert_exec_intent, insert_exec_log, set_exec_intent_ref
from jarvise_assist.track import TRACK_COHORT
from jarvise_assist.weekly import format_weekly_summary
from jarvise_ingest.db import open_db


class FakeFutures:
    """In-memory stand-in for BinanceFuturesClient history GETs."""

    def __init__(
        self,
        *,
        trades: list[dict[str, Any]] | None = None,
        orders: list[dict[str, Any]] | None = None,
        income: list[dict[str, Any]] | None = None,
    ) -> None:
        self.trades = list(trades or [])
        self.orders = list(orders or [])
        self.income = list(income or [])
        self.user_trade_calls: list[dict[str, Any]] = []
        self.all_order_calls: list[dict[str, Any]] = []

    def user_trades(
        self,
        symbol: str,
        *,
        start_ms: int,
        end_ms: int,
        from_id: int | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        self.user_trade_calls.append(
            {"symbol": symbol, "start_ms": start_ms, "end_ms": end_ms, "from_id": from_id, "limit": limit}
        )
        rows = [
            t
            for t in self.trades
            if str(t.get("symbol") or "").upper() == symbol.upper()
            and int(start_ms) <= int(t.get("time") or 0) <= int(end_ms)
            and (from_id is None or int(t.get("id") or 0) >= int(from_id))
        ]
        rows.sort(key=lambda r: int(r.get("id") or 0))
        return rows[: int(limit)]

    def all_orders(
        self,
        symbol: str,
        *,
        start_ms: int,
        end_ms: int,
        order_id: int | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        self.all_order_calls.append(
            {
                "symbol": symbol,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "order_id": order_id,
                "limit": limit,
            }
        )
        rows = [
            o
            for o in self.orders
            if str(o.get("symbol") or "").upper() == symbol.upper()
            and int(start_ms) <= int(o.get("time") or 0) <= int(end_ms)
            and (order_id is None or int(o.get("orderId") or 0) >= int(order_id))
        ]
        rows.sort(key=lambda r: int(r.get("orderId") or 0))
        return rows[: int(limit)]

    def income_history(
        self,
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
        income_type: str | None = None,
        limit: int = 1000,
        symbol: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = []
        for row in self.income:
            if income_type and str(row.get("incomeType") or "") != income_type:
                continue
            if symbol and str(row.get("symbol") or "").upper() != symbol.upper():
                continue
            tms = int(row.get("time") or 0)
            if start_ms is not None and tms < int(start_ms):
                continue
            if end_ms is not None and tms > int(end_ms):
                continue
            rows.append(row)
        return rows[: int(limit)]


def test_time_windows_20_day_span_three_windows() -> None:
    start = 1_000_000_000_000
    end = start + 20 * 24 * 3_600_000
    wins = time_windows(start, end)
    assert len(wins) == 3
    assert wins[0][0] == start
    assert wins[0][1] - wins[0][0] == FAPI_HISTORY_WINDOW_MS
    assert wins[1][0] == wins[0][1]
    assert wins[2][1] == end
    # No gap or overlap.
    for i in range(len(wins) - 1):
        assert wins[i][1] == wins[i + 1][0]


def test_paging_at_1000_rows() -> None:
    start = SPEC_START_MS
    end = start + 3_600_000
    trades = [
        {
            "id": i + 1,
            "orderId": 100 + i,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "price": "100",
            "qty": "0.01",
            "time": start + 1000 + i,
            "commission": "0",
            "commissionAsset": "USDT",
        }
        for i in range(1005)
    ]
    fake = FakeFutures(trades=trades)
    rows = fetch_all_user_trades(fake, "BTCUSDT", start_ms=start, end_ms=end)  # type: ignore[arg-type]
    assert len(rows) == 1005
    assert fake.user_trade_calls[0]["from_id"] is None
    assert fake.user_trade_calls[1]["from_id"] == 1001


def test_adverse_bps_long_short_sign() -> None:
    assert adverse_bps(fill=100.1, ref=100.0, side="long") == pytest.approx(10.0)
    assert adverse_bps(fill=99.9, ref=100.0, side="short") == pytest.approx(10.0)


def test_classify_exit_uses_client_suffix() -> None:
    kind, trigger = classify_exit_kind(
        {"clientOrderId": "jv-abc-sl0", "type": "MARKET", "origType": "MARKET", "stopPrice": "0"}
    )
    assert kind == "stop"
    assert trigger is None
    kind2, _ = classify_exit_kind({"clientOrderId": "manual-1", "type": "MARKET", "origType": "MARKET"})
    assert kind2 == "manual"
    kind3, _ = classify_exit_kind({"clientOrderId": "jv-abc-xx", "type": "LIMIT", "origType": "LIMIT"})
    assert kind3 == "unknown"


def _seed_jv_entry(
    conn: Any,
    *,
    intent_id: str,
    client_id: str,
    preview: dict[str, Any],
    ref_price: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO assist_exec_intent (
            id, kind, symbol, side, preview_json, created_at_ms, expires_at_ms, status
        ) VALUES (?, 'entry', 'BTCUSDT', 'long', ?, 1, 2, 'done')
        """,
        (intent_id, json.dumps(preview)),
    )
    if ref_price is not None:
        set_exec_intent_ref(conn, intent_id, ref_price=ref_price, ref_source="confirm_mark", ref_at_ms=10)
    insert_exec_log(
        conn,
        intent_id=intent_id,
        step="market",
        endpoint="/fapi/v1/order",
        client_order_id=client_id,
        http_status=200,
        created_at_ms=SPEC_START_MS + 1000,
    )
    conn.execute(
        """
        INSERT INTO assist_signal (
            id, symbol, side, entry, stop, target, qty, notional, leverage, loss_at_stop,
            created_at_ms, expires_at_ms, status
        ) VALUES (?, 'BTCUSDT', 'long', 100, 90, 120, 0.2, 20, 5, 2, ?, ?, 'taken')
        """,
        (preview["signal_id"], SPEC_START_MS, SPEC_START_MS + 1),
    )
    conn.execute(
        """
        INSERT INTO assist_track (
            signal_id, symbol, side, entry, stop, target, qty, sent_at_ms,
            exit_at_ms, exit_px, exit_reason, gross_r, net_r, fee_usdt, pnl_usdt,
            cohort, kind, funding_usdt
        ) VALUES (?, 'BTCUSDT', 'long', 100, 90, 120, 0.2, ?, ?, 110, 'flat', 1.0, 0.8, 0.1, 1.6, ?, 'sent', 0)
        """,
        (preview["signal_id"], SPEC_START_MS, SPEC_START_MS + 8 * 3_600_000, TRACK_COHORT),
    )
    conn.commit()


def test_jv_entry_match_and_stop_exit_avg(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "real.db")
    sid = "as_real_1"
    intent = "aei_real001"
    cid = "jv-real001-mkt"
    preview = {
        "entry": "100",
        "stop": "90",
        "target": "120",
        "qty": "0.2",
        "loss_at_stop": "2",
        "signal_id": sid,
        "leverage": 5,
    }
    _seed_jv_entry(conn, intent_id=intent, client_id=cid, preview=preview, ref_price="100.05")
    t0 = SPEC_START_MS + 60_000
    orders = [
        {
            "orderId": 11,
            "clientOrderId": cid,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "origType": "MARKET",
            "time": t0,
            "status": "FILLED",
        },
        {
            "orderId": 12,
            "clientOrderId": "jv-real001-sl0",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "type": "MARKET",
            "origType": "MARKET",
            "stopPrice": "90.0",
            "time": t0 + 3_600_000,
            "status": "FILLED",
        },
    ]
    trades = [
        {
            "id": 1,
            "orderId": 11,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "price": "100.1",
            "qty": "0.1",
            "time": t0,
            "commission": "0.01",
            "commissionAsset": "USDT",
        },
        {
            "id": 2,
            "orderId": 11,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "price": "100.3",
            "qty": "0.1",
            "time": t0 + 1,
            "commission": "0.01",
            "commissionAsset": "USDT",
        },
        {
            "id": 3,
            "orderId": 12,
            "symbol": "BTCUSDT",
            "side": "SELL",
            "price": "89.5",
            "qty": "0.1",
            "time": t0 + 3_600_000,
            "commission": "0.01",
            "commissionAsset": "USDT",
        },
        {
            "id": 4,
            "orderId": 12,
            "symbol": "BTCUSDT",
            "side": "SELL",
            "price": "89.7",
            "qty": "0.1",
            "time": t0 + 3_600_001,
            "commission": "0.01",
            "commissionAsset": "USDT",
        },
    ]
    income = [
        {
            "symbol": "BTCUSDT",
            "incomeType": "REALIZED_PNL",
            "income": "-2.1",
            "time": t0 + 3_600_000,
            "tradeId": 3,
        },
        {
            "symbol": "BTCUSDT",
            "incomeType": "REALIZED_PNL",
            "income": "-2.0",
            "time": t0 + 3_600_001,
            "tradeId": 4,
        },
        {
            "symbol": "BTCUSDT",
            "incomeType": "FUNDING_FEE",
            "income": "-0.05",
            "time": t0 + 1_800_000,
        },
    ]
    fake = FakeFutures(trades=trades, orders=orders, income=income)
    sync_real_trades(conn, fake, since_ms=SPEC_START_MS, until_ms=t0 + 10_000_000)  # type: ignore[arg-type]
    row = conn.execute(
        """
        SELECT entry_fill_avg, exit_fill_avg, exit_kind, stop_trigger, ref_source,
               slip_entry_bps, slip_stop_bps, commission_usdt, status, net_r_virtual
        FROM assist_real_trade WHERE entry_order_id='11'
        """
    ).fetchone()
    assert row is not None
    assert row[0] == pytest.approx(100.2)
    assert row[1] == pytest.approx(89.6)
    assert row[2] == "stop"
    assert row[3] == pytest.approx(90.0)
    assert row[4] == "confirm_mark"
    assert row[5] == pytest.approx(adverse_bps(fill=100.2, ref=100.05, side="long"))
    assert row[6] == pytest.approx(adverse_bps(fill=89.6, ref=90.0, side="long"))
    assert row[7] == pytest.approx(-0.04)
    assert row[8] == "closed"
    assert row[9] == pytest.approx(0.8)
    conn.close()


def test_preview_entry_fallback_and_unmatched(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "prev.db")
    sid = "as_prev"
    intent = "aei_prev001"
    cid = "jv-prev001-mkt"
    preview = {
        "entry": "100",
        "stop": "90",
        "target": "120",
        "qty": "0.1",
        "loss_at_stop": "1",
        "signal_id": sid,
        "leverage": 5,
    }
    _seed_jv_entry(conn, intent_id=intent, client_id=cid, preview=preview, ref_price=None)
    t0 = SPEC_START_MS + 60_000
    orders = [
        {
            "orderId": 21,
            "clientOrderId": cid,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "origType": "MARKET",
            "time": t0,
            "status": "FILLED",
        },
        {
            "orderId": 99,
            "clientOrderId": "human-1",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "origType": "MARKET",
            "time": t0 + 1000,
            "status": "FILLED",
        },
    ]
    trades = [
        {
            "id": 10,
            "orderId": 21,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "price": "100",
            "qty": "0.1",
            "time": t0,
            "commission": "0.01",
            "commissionAsset": "USDT",
        },
        {
            "id": 11,
            "orderId": 99,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "price": "101",
            "qty": "0.05",
            "time": t0 + 1000,
            "commission": "0.01",
            "commissionAsset": "USDT",
        },
    ]
    fake = FakeFutures(trades=trades, orders=orders)
    sync_real_trades(conn, fake, since_ms=SPEC_START_MS, until_ms=t0 + 10_000)  # type: ignore[arg-type]
    ref_src = conn.execute(
        "SELECT ref_source, status FROM assist_real_trade WHERE entry_order_id='21'"
    ).fetchone()
    assert ref_src is not None and ref_src[0] == "preview_entry"
    unmatched = conn.execute("SELECT status FROM assist_real_trade WHERE entry_order_id='99'").fetchone()
    assert unmatched is not None and unmatched[0] == "unmatched"
    conn.close()


def test_unknown_exit_and_idempotent_rerun(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "unk.db")
    sid = "as_unk"
    intent = "aei_unk001"
    cid = "jv-unk001-mkt"
    preview = {
        "entry": "100",
        "stop": "90",
        "target": "120",
        "qty": "0.1",
        "loss_at_stop": "1",
        "signal_id": sid,
        "leverage": 5,
    }
    _seed_jv_entry(conn, intent_id=intent, client_id=cid, preview=preview, ref_price="100")
    t0 = SPEC_START_MS + 60_000
    orders = [
        {
            "orderId": 31,
            "clientOrderId": cid,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "origType": "MARKET",
            "time": t0,
            "status": "FILLED",
        },
        {
            "orderId": 32,
            "clientOrderId": "jv-unk001-weird",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "type": "LIMIT",
            "origType": "LIMIT",
            "time": t0 + 1000,
            "status": "FILLED",
        },
    ]
    trades = [
        {
            "id": 20,
            "orderId": 31,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "price": "100",
            "qty": "0.1",
            "time": t0,
            "commission": "0",
            "commissionAsset": "USDT",
        },
        {
            "id": 21,
            "orderId": 32,
            "symbol": "BTCUSDT",
            "side": "SELL",
            "price": "101",
            "qty": "0.1",
            "time": t0 + 1000,
            "commission": "0",
            "commissionAsset": "USDT",
        },
    ]
    income = [
        {
            "symbol": "BTCUSDT",
            "incomeType": "REALIZED_PNL",
            "income": "0.1",
            "time": t0 + 1000,
            "tradeId": 21,
        }
    ]
    fake = FakeFutures(trades=trades, orders=orders, income=income)
    sync_real_trades(conn, fake, since_ms=SPEC_START_MS, until_ms=t0 + 5000)  # type: ignore[arg-type]
    kind = conn.execute(
        "SELECT exit_kind, status FROM assist_real_trade WHERE entry_order_id='31'"
    ).fetchone()
    assert kind is not None and kind[0] == "unknown" and kind[1] == "closed"
    # Mutate then re-run: closed row stays.
    conn.execute("UPDATE assist_real_trade SET net_r_real=99 WHERE entry_order_id='31'")
    conn.commit()
    sync_real_trades(conn, fake, since_ms=SPEC_START_MS, until_ms=t0 + 5000)  # type: ignore[arg-type]
    again = conn.execute(
        "SELECT net_r_real, COUNT(*) FROM assist_real_trade WHERE entry_order_id='31'"
    ).fetchone()
    assert again is not None and again[0] == 99 and again[1] == 1
    n = conn.execute("SELECT COUNT(*) FROM assist_real_trade").fetchone()[0]
    sync_real_trades(conn, fake, since_ms=SPEC_START_MS, until_ms=t0 + 5000)  # type: ignore[arg-type]
    assert conn.execute("SELECT COUNT(*) FROM assist_real_trade").fetchone()[0] == n
    conn.close()


def test_non_usdt_commission_flagged(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "bnb.db")
    sid = "as_bnb"
    intent = "aei_bnb001"
    cid = "jv-bnb001-mkt"
    preview = {
        "entry": "100",
        "stop": "90",
        "target": "120",
        "qty": "0.1",
        "loss_at_stop": "1",
        "signal_id": sid,
        "leverage": 5,
    }
    _seed_jv_entry(conn, intent_id=intent, client_id=cid, preview=preview, ref_price="100")
    t0 = SPEC_START_MS + 60_000
    orders = [
        {
            "orderId": 41,
            "clientOrderId": cid,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "origType": "MARKET",
            "time": t0,
            "status": "FILLED",
        }
    ]
    trades = [
        {
            "id": 30,
            "orderId": 41,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "price": "100",
            "qty": "0.1",
            "time": t0,
            "commission": "0.001",
            "commissionAsset": "BNB",
        }
    ]
    fake = FakeFutures(trades=trades, orders=orders)
    sync_real_trades(conn, fake, since_ms=SPEC_START_MS, until_ms=t0 + 1000)  # type: ignore[arg-type]
    row = conn.execute(
        "SELECT commission_usdt, commission_other_asset, status FROM assist_real_trade WHERE entry_order_id='41'"
    ).fetchone()
    assert row is not None
    assert row[0] == pytest.approx(0.0)
    assert row[1] == "BNB:0.001"
    assert row[2] == "open"
    conn.close()


def test_weekly_lines_include_zero_counts() -> None:
    summary = {
        "closed_n": 0,
        "mean_net_r": None,
        "mean_diff_r": None,
        "mean_slip_entry_bps": None,
        "p90_slip_entry_bps": None,
        "confirm_n": 0,
        "preview_n": 0,
        "mean_drift_bps": None,
        "mean_slip_stop_bps": None,
        "p90_slip_stop_bps": None,
        "stop_n": 0,
        "unmatched_n": 0,
        "unknown_n": 0,
        "target_n": 20,
    }
    lines = format_real_lines(summary)
    text = format_weekly_summary(
        counts={"sent": 0, "skipped": 0, "taken": 0},
        virtual={"n": 0},
        real={"universe_usdt": 0.0},
        closed_all=0,
        held={"n": 0, "mean_r": 0.0},
        real_vs_virtual=summary,
    )
    assert "ไม้จริง n=0 (ต้องการ 20) · net R เฉลี่ย — · ต่างจากไม้จำลอง —" in text
    assert "จับคู่ไม่ได้: 0 ไม้ · exit ไม่ทราบชนิด: 0" in text
    assert lines[0].startswith("ไม้จริง")


def test_confirm_guards_stores_ref_and_write_failure_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_assist_execute import RecordingTransport, _client, _exchange_info, _seed_doge_short

    from jarvise_assist.execute import _confirm_guards_entry
    from jarvise_assist.sizing import size_signal
    from jarvise_assist.store import insert_signal

    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    conn = open_db(tmp_path / "ref.db")
    bar = _ms_bangkok(2026, 11, 2, 3, 0)
    now = _ms_bangkok(2026, 11, 2, 8, 20)
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
    insert_signal(
        conn,
        first,
        status="sent",
        created_at_ms=now - 60_000,
        expires_at_ms=now + 3_600_000,
        sent_at_ms=now - 60_000,
    )
    intent_id = insert_exec_intent(
        conn,
        kind="entry",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "qty": "100",
            "stop": "0.083",
            "target": "0.074",
            "leverage": 1,
            "entry": "0.08",
            "loss_at_stop": "0.3",
            "r_distance": "0.003",
        },
        created_at_ms=now,
        expires_at_ms=now + 120_000,
    )
    intent = conn.execute("SELECT id FROM assist_exec_intent WHERE id=?", (intent_id,)).fetchone()
    assert intent is not None
    from jarvise_assist.store import get_exec_intent

    intent_row = get_exec_intent(conn, intent_id)
    assert intent_row is not None
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v3/balance"): (
            200,
            [{"asset": "USDT", "balance": "1000", "availableBalance": "1000"}],
        ),
        ("GET", "/fapi/v3/positionRisk"): (200, []),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.08"}),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
    }
    trade = _client(RecordingTransport(handlers))
    assert _confirm_guards_entry(conn, intent_row, trade, now_ms_value=now + 1000) is None
    stored = get_exec_intent(conn, intent_id)
    assert stored is not None
    assert stored["ref_price"] == "0.08"
    assert stored["ref_source"] == "confirm_mark"
    assert stored["ref_at_ms"] == now + 1000

    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("disk")

    monkeypatch.setattr("jarvise_assist.execute.set_exec_intent_ref", boom)
    assert _confirm_guards_entry(conn, intent_row, trade, now_ms_value=now + 2000) is None
    conn.close()


def test_real_trades_module_has_no_trade_client() -> None:
    src = Path("src/jarvise_assist/real_trades.py").read_text(encoding="utf-8")
    assert "BinanceTradeClient" not in src


def test_fetch_all_orders_pages(tmp_path: Path) -> None:
    start = SPEC_START_MS
    end = start + 3_600_000
    orders = [
        {
            "orderId": i + 1,
            "clientOrderId": f"jv-x-{i}",
            "symbol": "ETHUSDT",
            "side": "BUY",
            "type": "MARKET",
            "origType": "MARKET",
            "time": start + i,
            "status": "FILLED",
        }
        for i in range(1005)
    ]
    fake = FakeFutures(orders=orders)
    rows = fetch_all_orders(fake, "ETHUSDT", start_ms=start, end_ms=end)  # type: ignore[arg-type]
    assert len(rows) == 1005
    assert fake.all_order_calls[1]["order_id"] == 1001


def test_summarize_reads_empty_db(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "empty.db")
    s = summarize_real_vs_virtual(conn)
    assert s["closed_n"] == 0
    assert s["unmatched_n"] == 0
    assert s["unknown_n"] == 0
    conn.close()
