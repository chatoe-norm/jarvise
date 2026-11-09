"""PR-C2: income status, exit-time windows, reconcile. Fakes only. No venue calls."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.test_assist import FakeFutures as AssistFutures
from tests.test_assist import _ms_bangkok
from tests.test_assist_real_trades import FakeFutures, _seed_jv_entry

from jarvise_assist.real_trades import (
    INCOME_PENDING_MS,
    INCOME_REFRESH_GAP_MS,
    INCOME_REFRESH_LOOKBACK_MS,
    SPEC_START_MS,
    _load_real_rows,
    build_income_reconcile,
    format_real_lines,
    refresh_incomplete_income,
    summarize_real_vs_virtual,
    sync_real_trades,
)
from jarvise_assist.store import ensure_settings, upsert_watch_position
from jarvise_assist.track import TRACK_COHORT
from jarvise_assist.watch import run_assist_watch
from jarvise_ingest.db import open_db, schema_version

HOUR = 3_600_000


class WatchFutures(FakeFutures):
    """History fake plus the reads watch and the week-loss check require."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.balance = Decimal("100000")

    def assert_key_safe(self) -> dict[str, Any]:
        return {"ok": True}

    def usdt_wallet_balance(self) -> Decimal:
        return self.balance

    def position_risk(self, *, symbol: str | None = None) -> list[Any]:
        del symbol
        return []

    def all_open_orders(self, *, symbol: str | None = None) -> list[dict[str, Any]]:
        del symbol
        return []


def _preview(signal_id: str, *, loss: str = "1") -> dict[str, str]:
    return {
        "entry": "100",
        "stop": "90",
        "target": "120",
        "qty": "1",
        "loss_at_stop": loss,
        "signal_id": signal_id,
        "leverage": "5",
    }


def _orders(client_id: str, t0: int, *, entry_id: int = 11) -> list[dict[str, Any]]:
    return [
        {
            "orderId": entry_id,
            "clientOrderId": client_id,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "time": t0,
            "status": "FILLED",
            "reduceOnly": False,
        },
        {
            "orderId": entry_id + 1,
            "clientOrderId": client_id.replace("-mkt", "-sl0"),
            "symbol": "BTCUSDT",
            "side": "SELL",
            "type": "MARKET",
            "stopPrice": "",
            "time": t0 + 1_000,
            "status": "FILLED",
            "reduceOnly": True,
        },
    ]


def _trades(
    t0: int,
    *,
    entry_id: int = 11,
    realized: str = "-1",
    commission: str = "0",
) -> list[dict[str, Any]]:
    return [
        {
            "id": 1,
            "orderId": entry_id,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "price": "100",
            "qty": "1",
            "time": t0,
            "commission": commission,
            "commissionAsset": "USDT",
            "realizedPnl": "0",
        },
        {
            "id": 2,
            "orderId": entry_id + 1,
            "symbol": "BTCUSDT",
            "side": "SELL",
            "price": "90",
            "qty": "1",
            "time": t0 + 1_000,
            "commission": "0",
            "commissionAsset": "USDT",
            "realizedPnl": realized,
        },
    ]


def _pnl(amount: str, t_ms: int, trade_id: int = 2) -> dict[str, Any]:
    return {
        "symbol": "BTCUSDT",
        "incomeType": "REALIZED_PNL",
        "income": amount,
        "time": t_ms,
        "tradeId": trade_id,
    }


def _row(conn: Any, entry_order_id: str = "11") -> Any:
    return conn.execute(
        """
        SELECT realized_pnl_usdt, commission_usdt, funding_usdt, net_r_real, income_status
        FROM assist_real_trade WHERE entry_order_id=?
        """,
        (entry_order_id,),
    ).fetchone()


def _sent_track(conn: Any, signal_id: str, exit_ms: int, *, net_r: float = 0.4) -> None:
    conn.execute(
        """
        INSERT INTO assist_signal (
            id, symbol, side, entry, stop, target, qty, notional, leverage, loss_at_stop,
            created_at_ms, expires_at_ms, status, sent_at_ms
        ) VALUES (?, 'BTCUSDT', 'long', 100, 90, 120, 1, 100, 2, 1, ?, ?, 'taken', ?)
        """,
        (signal_id, exit_ms - HOUR, exit_ms, exit_ms - HOUR),
    )
    conn.execute(
        """
        INSERT INTO assist_track (
            signal_id, symbol, side, entry, stop, target, qty, sent_at_ms,
            exit_at_ms, exit_px, exit_reason, gross_r, net_r, cohort, kind
        ) VALUES (?, 'BTCUSDT', 'long', 100, 90, 120, 1, ?, ?, 110, 'flat', ?, ?, ?, 'sent')
        """,
        (signal_id, exit_ms - HOUR, exit_ms, net_r, net_r, TRACK_COHORT),
    )
    conn.commit()


def test_pending_uses_fill_realized_then_income_completes(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "pending.db")
    _seed_jv_entry(
        conn,
        intent_id="aei_p",
        client_id="jv-p-mkt",
        preview=_preview("as_p"),
        ref_price="100",
    )
    t0 = SPEC_START_MS + 60_000
    exit_ms = t0 + 1_000
    until = exit_ms + HOUR
    fake = FakeFutures(trades=_trades(t0, realized="-1", commission="0.05"), orders=_orders("jv-p-mkt", t0))
    sync_real_trades(conn, fake, since_ms=SPEC_START_MS, until_ms=until)  # type: ignore[arg-type]
    first = _row(conn)
    assert first["income_status"] == "pending"
    assert first["realized_pnl_usdt"] == pytest.approx(-1.0)
    assert first["commission_usdt"] == pytest.approx(-0.05)
    assert first["funding_usdt"] == pytest.approx(0.0)
    assert first["net_r_real"] == pytest.approx(-1.05)
    after_first = len(fake.income_calls)

    sync_real_trades(conn, fake, since_ms=SPEC_START_MS, until_ms=until)  # type: ignore[arg-type]
    assert _row(conn)["income_status"] == "pending"
    assert len(fake.income_calls) > after_first

    fake.income.append(_pnl("-0.80", exit_ms))
    sync_real_trades(conn, fake, since_ms=SPEC_START_MS, until_ms=until)  # type: ignore[arg-type]
    done = _row(conn)
    assert done["income_status"] == "complete"
    assert done["realized_pnl_usdt"] == pytest.approx(-0.80)
    assert done["net_r_real"] == pytest.approx(-0.85)
    after_complete = len(fake.income_calls)

    sync_real_trades(conn, fake, since_ms=SPEC_START_MS, until_ms=until)  # type: ignore[arg-type]
    assert len(fake.income_calls) == after_complete
    assert _row(conn)["income_status"] == "complete"
    conn.close()


def test_missing_after_24h_is_listed_in_reconcile(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "missing.db")
    _seed_jv_entry(
        conn,
        intent_id="aei_m",
        client_id="jv-m-mkt",
        preview=_preview("as_m"),
        ref_price="100",
    )
    t0 = SPEC_START_MS + 60_000
    exit_ms = t0 + 1_000
    fake = FakeFutures(trades=_trades(t0, realized="-1"), orders=_orders("jv-m-mkt", t0))
    sync_real_trades(conn, fake, since_ms=SPEC_START_MS, until_ms=exit_ms + INCOME_PENDING_MS)  # type: ignore[arg-type]
    assert _row(conn)["income_status"] == "pending"

    end = exit_ms + INCOME_PENDING_MS + 1
    sync_real_trades(conn, fake, since_ms=SPEC_START_MS, until_ms=end)  # type: ignore[arg-type]
    assert _row(conn)["income_status"] == "missing"
    assert _row(conn)["realized_pnl_usdt"] == pytest.approx(-1.0)
    summary = summarize_real_vs_virtual(conn, since_ms=SPEC_START_MS, until_ms=end)
    summary["reconcile"] = build_income_reconcile(
        fake,  # type: ignore[arg-type]
        _load_real_rows(conn),
        since_ms=SPEC_START_MS,
        until_ms=end,
    )
    text = "\n".join(format_real_lines(summary))
    assert "income ขาด " in text
    assert "n=1" in text.split("income ขาด ", 1)[1].split("\n", 1)[0]
    assert any(
        line.startswith("· ") and "BTCUSDT" in line and "income ขาด" in line for line in text.splitlines()
    )
    conn.close()


def test_missing_refreshed_within_7_days_not_after(tmp_path: Path) -> None:
    old = open_db(tmp_path / "old.db")
    now = SPEC_START_MS + 30 * 24 * HOUR
    exit_ms = now - INCOME_REFRESH_LOOKBACK_MS - 1
    old.execute(
        """
        INSERT INTO assist_real_trade (
            symbol, side, entry_order_id, status, updated_at_ms, intent_id,
            entry_time_ms, exit_time_ms, loss_at_stop, realized_pnl_usdt,
            commission_usdt, funding_usdt, net_r_real, income_status
        ) VALUES (
            'BTCUSDT', 'long', 'old', 'closed', ?, 'intent-old',
            ?, ?, 1, -1.25, 0, 0, -1.25, 'missing'
        )
        """,
        (now, exit_ms - HOUR, exit_ms),
    )
    old.commit()
    quiet = FakeFutures()
    skipped = refresh_incomplete_income(old, quiet, now_ms=now)  # type: ignore[arg-type]
    assert skipped["reason"] == "none"
    assert quiet.income_calls == []
    assert quiet.user_trade_calls == []
    assert _row(old, "old")["realized_pnl_usdt"] == pytest.approx(-1.25)
    old.close()

    recent = open_db(tmp_path / "recent.db")
    _seed_jv_entry(
        recent,
        intent_id="aei_r",
        client_id="jv-r-mkt",
        preview=_preview("as_r"),
        ref_price="100",
    )
    fill_exit = now - INCOME_REFRESH_LOOKBACK_MS + HOUR
    t0 = fill_exit - 1_000
    recent.execute(
        """
        INSERT INTO assist_real_trade (
            symbol, side, entry_order_id, status, updated_at_ms, intent_id,
            entry_time_ms, exit_time_ms, loss_at_stop, realized_pnl_usdt,
            net_r_real, income_status
        ) VALUES ('BTCUSDT', 'long', '11', 'closed', ?, 'aei_r', ?, ?, 1, 0, 0, 'missing')
        """,
        (now, t0, fill_exit),
    )
    recent.commit()
    fake = FakeFutures(trades=_trades(t0, realized="-1"), orders=_orders("jv-r-mkt", t0))
    refreshed = refresh_incomplete_income(recent, fake, now_ms=now)  # type: ignore[arg-type]
    assert refreshed["refreshed"] is True
    assert any(call["income_type"] == "REALIZED_PNL" for call in fake.income_calls)
    assert _row(recent)["income_status"] == "missing"
    assert _row(recent)["realized_pnl_usdt"] == pytest.approx(-1.0)
    recent.close()


def test_full_sync_keeps_old_missing_without_income_refetch(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "frozen.db")
    _seed_jv_entry(
        conn,
        intent_id="aei_old",
        client_id="jv-old-mkt",
        preview=_preview("as_old"),
        ref_price="100",
    )
    now = SPEC_START_MS + 40 * 24 * HOUR
    exit_ms = now - INCOME_REFRESH_LOOKBACK_MS - HOUR
    t0 = exit_ms - 1_000
    conn.execute(
        """
        INSERT INTO assist_real_trade (
            symbol, side, entry_order_id, status, updated_at_ms, intent_id,
            entry_time_ms, exit_time_ms, loss_at_stop, realized_pnl_usdt,
            commission_usdt, funding_usdt, net_r_real, income_status
        ) VALUES (
            'BTCUSDT', 'long', '11', 'closed', ?, 'aei_old',
            ?, ?, 1, -1.25, -0.05, 0, -1.30, 'missing'
        )
        """,
        (now, t0, exit_ms),
    )
    conn.commit()
    fake = FakeFutures(
        trades=_trades(t0, realized="-1", commission="0.05"),
        orders=_orders("jv-old-mkt", t0),
        income=[_pnl("-9", exit_ms)],
    )
    sync_real_trades(conn, fake, since_ms=SPEC_START_MS, until_ms=now)  # type: ignore[arg-type]
    row = _row(conn)
    assert row["income_status"] == "missing"
    assert row["realized_pnl_usdt"] == pytest.approx(-1.25)
    assert not any(call["income_type"] == "REALIZED_PNL" for call in fake.income_calls)
    conn.close()


def test_null_status_zero_realized_is_reevaluated(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "null.db")
    _seed_jv_entry(
        conn,
        intent_id="aei_z",
        client_id="jv-z-mkt",
        preview=_preview("as_z"),
        ref_price="100",
    )
    t0 = SPEC_START_MS + 60_000
    exit_ms = t0 + 1_000
    conn.execute(
        """
        INSERT INTO assist_real_trade (
            symbol, side, entry_order_id, status, updated_at_ms, intent_id,
            entry_time_ms, exit_time_ms, entry_qty, entry_fill_avg, loss_at_stop,
            realized_pnl_usdt, commission_usdt, funding_usdt, net_r_real, income_status
        ) VALUES (
            'BTCUSDT', 'long', '11', 'closed', ?, 'aei_z',
            ?, ?, 1, 100, 1,
            0, -0.05, 0, -0.05, NULL
        )
        """,
        (exit_ms, t0, exit_ms),
    )
    conn.commit()
    fake = FakeFutures(
        trades=_trades(t0, realized="-1", commission="0.05"),
        orders=_orders("jv-z-mkt", t0),
        income=[_pnl("-1", exit_ms)],
    )
    sync_real_trades(conn, fake, since_ms=SPEC_START_MS, until_ms=exit_ms + HOUR)  # type: ignore[arg-type]
    row = _row(conn)
    assert row["income_status"] == "complete"
    assert row["realized_pnl_usdt"] == pytest.approx(-1.0)
    assert row["net_r_real"] == pytest.approx(-1.05)
    assert row["net_r_real"] != pytest.approx(-0.05)
    conn.close()


def test_income_refresh_at_most_once_per_30_min(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "gap.db")
    _seed_jv_entry(
        conn,
        intent_id="aei_g",
        client_id="jv-g-mkt",
        preview=_preview("as_g"),
        ref_price="100",
    )
    now = SPEC_START_MS + 10 * 24 * HOUR
    t0 = now - 2 * HOUR
    conn.execute(
        """
        INSERT INTO assist_real_trade (
            symbol, side, entry_order_id, status, updated_at_ms, intent_id,
            entry_time_ms, exit_time_ms, loss_at_stop, realized_pnl_usdt,
            net_r_real, income_status
        ) VALUES ('BTCUSDT', 'long', '11', 'closed', ?, 'aei_g', ?, ?, 1, -1, -1, 'pending')
        """,
        (now, t0, t0 + 1_000),
    )
    conn.commit()
    fake = FakeFutures(trades=_trades(t0, realized="-1"), orders=_orders("jv-g-mkt", t0))
    first = refresh_incomplete_income(conn, fake, now_ms=now)  # type: ignore[arg-type]
    assert first["refreshed"] is True
    calls = len(fake.income_calls)
    assert calls > 0
    second = refresh_incomplete_income(conn, fake, now_ms=now + 60_000)  # type: ignore[arg-type]
    assert second["reason"] == "throttled"
    assert len(fake.income_calls) == calls
    third = refresh_incomplete_income(conn, fake, now_ms=now + INCOME_REFRESH_GAP_MS)  # type: ignore[arg-type]
    assert third["refreshed"] is True
    assert len(fake.income_calls) > calls
    conn.close()


def test_absent_funding_is_zero_and_not_pending(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "fund.db")
    _seed_jv_entry(
        conn,
        intent_id="aei_f",
        client_id="jv-f-mkt",
        preview=_preview("as_f"),
        ref_price="100",
    )
    t0 = SPEC_START_MS + 60_000
    exit_ms = t0 + 1_000
    fake = FakeFutures(
        trades=_trades(t0, realized="-1"),
        orders=_orders("jv-f-mkt", t0),
        income=[_pnl("-1", exit_ms)],
    )
    sync_real_trades(conn, fake, since_ms=SPEC_START_MS, until_ms=exit_ms + HOUR)  # type: ignore[arg-type]
    row = _row(conn)
    assert row["income_status"] == "complete"
    assert row["funding_usdt"] == pytest.approx(0.0)
    assert row["realized_pnl_usdt"] == pytest.approx(-1.0)
    conn.close()


def _bad_row(conn: Any, *, entry_ms: int, exit_ms: int, intent_id: str) -> None:
    conn.execute(
        """
        INSERT INTO assist_real_trade (
            symbol, side, entry_order_id, status, updated_at_ms, intent_id,
            entry_time_ms, exit_time_ms, loss_at_stop, realized_pnl_usdt,
            commission_usdt, funding_usdt, net_r_real, income_status
        ) VALUES (
            'BTCUSDT', 'long', '11', 'closed', ?, ?,
            ?, ?, 1, 0, 0, 0, 0, NULL
        )
        """,
        (exit_ms, intent_id, entry_ms, exit_ms),
    )
    conn.commit()


def test_today_correction_pauses_in_the_same_watch_pass(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "today.db")
    day = _ms_bangkok(2026, 11, 9, 12, 0)
    exit_ms = day - 60_000
    entry_ms = day - HOUR
    _seed_jv_entry(
        conn,
        intent_id="aei_today",
        client_id="jv-today-mkt",
        preview=_preview("as_today"),
        ref_price="100",
    )
    _bad_row(conn, entry_ms=entry_ms, exit_ms=exit_ms, intent_id="aei_today")
    fake = WatchFutures(
        trades=_trades(entry_ms, realized="-3"),
        orders=_orders("jv-today-mkt", entry_ms),
        income=[_pnl("-3", entry_ms + 1_000)],
    )
    payload = run_assist_watch(conn, futures=fake, now_ms_value=day, send=lambda _t: True)  # type: ignore[arg-type]
    assert payload["ok"] is True
    assert payload["pause"]["reason"] == "day_loss"
    assert _row(conn)["net_r_real"] == pytest.approx(-3.0)
    assert _row(conn)["income_status"] == "complete"
    conn.close()


def test_yesterday_correction_does_not_pause(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "yday.db")
    today = _ms_bangkok(2026, 11, 9, 12, 0)
    exit_ms = _ms_bangkok(2026, 11, 8, 12, 0)
    entry_ms = exit_ms - HOUR
    _seed_jv_entry(
        conn,
        intent_id="aei_yday",
        client_id="jv-yday-mkt",
        preview=_preview("as_yday"),
        ref_price="100",
    )
    _bad_row(conn, entry_ms=entry_ms, exit_ms=exit_ms, intent_id="aei_yday")
    fake = WatchFutures(
        trades=_trades(entry_ms, realized="-3"),
        orders=_orders("jv-yday-mkt", entry_ms),
        income=[_pnl("-3", exit_ms)],
    )
    payload = run_assist_watch(conn, futures=fake, now_ms_value=today, send=lambda _t: True)  # type: ignore[arg-type]
    assert payload["ok"] is True
    assert payload["pause"]["paused"] is False
    assert payload["pause"]["net_r"] == pytest.approx(0.0)
    assert _row(conn)["net_r_real"] == pytest.approx(-3.0)
    settings = ensure_settings(conn)
    assert settings["paused_until_ms"] is None
    conn.close()


def test_report_excludes_pending_and_missing_from_r_stats(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "stats.db")
    exit_ms = SPEC_START_MS + 5 * 24 * HOUR
    since = exit_ms - HOUR
    until = exit_ms + HOUR
    specs = (
        ("c", 1.0, 0.4, 102.0, "complete"),
        ("p", -3.0, 0.0, 200.0, "pending"),
        ("m", 5.0, 1.0, 50.0, "missing"),
    )
    for suffix, net, virtual, fill, income_status in specs:
        sid = f"as_{suffix}"
        _sent_track(conn, sid, exit_ms, net_r=virtual)
        conn.execute(
            """
            INSERT INTO assist_real_trade (
                symbol, side, entry_order_id, entry_client_id, status, updated_at_ms,
                entry_time_ms, exit_time_ms, signal_id, signal_entry, planned_stop,
                entry_fill_avg, loss_at_stop, net_r_real, net_r_actual, net_r_virtual,
                income_status
            ) VALUES (
                'BTCUSDT', 'long', ?, ?, 'closed', ?,
                ?, ?, ?, 100, 90,
                ?, 1, ?, ?, ?,
                ?
            )
            """,
            (
                f"ord-{suffix}",
                f"jv-{suffix}-mkt",
                exit_ms,
                exit_ms - 10_000,
                exit_ms,
                sid,
                fill,
                net,
                net,
                virtual,
                income_status,
            ),
        )
    conn.execute(
        """
        INSERT INTO assist_real_trade (
            symbol, side, entry_order_id, status, updated_at_ms,
            entry_time_ms, net_r_real, income_status
        ) VALUES ('BTCUSDT', 'long', 'open-1', 'open', ?, ?, 9, NULL)
        """,
        (exit_ms, exit_ms - 5_000),
    )
    conn.commit()
    summary = summarize_real_vs_virtual(conn, since_ms=since, until_ms=until)
    assert summary["closed_n"] == 1
    assert summary["mean_net_r"] == pytest.approx(1.0)
    assert summary["mean_net_r_actual"] == pytest.approx(1.0)
    assert summary["mean_diff_r"] == pytest.approx(0.6)
    assert summary["n_pairs"] == 1
    assert summary["mean_slip_entry_r"] == pytest.approx(0.2)
    assert summary["slip_entry_r_n"] == 1
    assert summary["income_pending_n"] == 1
    assert summary["income_missing_n"] == 1
    assert summary["open_n"] == 1
    text = "\n".join(format_real_lines(summary))
    assert "รอข้อมูล income " in text and "n=1" in text.split("รอข้อมูล income ", 1)[1].split("\n", 1)[0]
    assert "income ขาด " in text and "n=1" in text.split("income ขาด ", 1)[1].split("\n", 1)[0]
    conn.close()


def test_reconcile_uses_assist_symbols_and_caps_unattributed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = open_db(tmp_path / "recon.db")
    monkeypatch.setattr("jarvise_assist.real_trades.ASSIST_SYMBOLS", ("BTCUSDT",))
    exit_ms = SPEC_START_MS + 2 * 24 * HOUR
    meta = {"order_ids": ["12"], "trade_ids": [2], "entry_trade_ids": [1]}
    conn.execute(
        """
        INSERT INTO assist_real_trade (
            symbol, side, entry_order_id, status, updated_at_ms,
            entry_time_ms, exit_time_ms, exit_order_ids,
            realized_pnl_usdt, commission_usdt, funding_usdt, income_status
        ) VALUES (
            'BTCUSDT', 'long', '11', 'closed', ?, ?, ?, ?, -1, -0.05, 0, 'complete'
        )
        """,
        (exit_ms, exit_ms - HOUR, exit_ms, json.dumps(meta)),
    )
    conn.execute(
        """
        INSERT INTO assist_real_trade (
            symbol, side, entry_order_id, status, updated_at_ms,
            entry_time_ms, exit_time_ms, income_status
        ) VALUES ('BTCUSDT', 'short', 'miss', 'closed', ?, ?, ?, 'missing')
        """,
        (exit_ms, exit_ms - 10_000, exit_ms - 5_000),
    )
    conn.commit()
    income: list[dict[str, Any]] = [
        _pnl("-1", exit_ms),
        {
            "symbol": "ETHUSDT",
            "incomeType": "REALIZED_PNL",
            "income": "-50",
            "time": exit_ms,
            "tradeId": 77,
        },
    ]
    for i in range(12):
        income.append(
            {
                "symbol": "BTCUSDT",
                "incomeType": "COMMISSION",
                "income": "-0.02",
                "time": exit_ms + i + 1,
                "tradeId": 9000 + i,
            }
        )
    fake = FakeFutures(income=income)
    rows = _load_real_rows(conn)
    reconcile = build_income_reconcile(
        fake,  # type: ignore[arg-type]
        rows,
        since_ms=SPEC_START_MS,
        until_ms=exit_ms + HOUR,
    )
    called = {str(call["symbol"]) for call in fake.income_calls}
    assert called == {"BTCUSDT"}
    assert reconcile["totals"]["REALIZED_PNL"]["binance"] == pytest.approx(-1.0)
    assert reconcile["totals"]["REALIZED_PNL"]["trades"] == pytest.approx(-1.0)
    assert reconcile["totals"]["COMMISSION"]["binance"] == pytest.approx(-0.24)
    assert reconcile["totals"]["COMMISSION"]["trades"] == pytest.approx(-0.05)
    assert reconcile["totals"]["FUNDING_FEE"]["binance"] == pytest.approx(0.0)
    assert reconcile["unattributed_n"] == 12
    assert len(reconcile["missing"]) == 1
    summary = summarize_real_vs_virtual(conn, since_ms=SPEC_START_MS, until_ms=exit_ms + HOUR)
    summary["reconcile"] = reconcile
    lines = format_real_lines(summary)
    detail = [line for line in lines if line.startswith("· ") and "COMMISSION" in line]
    assert len(detail) == 10
    assert "และอีก 2 รายการ" in lines
    assert any(line.startswith("· ") and "income ขาด" in line for line in lines)
    assert all("ETHUSDT" not in line for line in lines)
    conn.close()


def test_exit_time_window_for_real_and_virtual(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "window.db")
    start, end = 1_000, 2_000

    def add_real(
        oid: str,
        *,
        entry: int,
        exit_ms: int | None,
        status: str,
        signal_id: str | None,
        net: float | None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO assist_real_trade (
                symbol, side, entry_order_id, status, updated_at_ms,
                entry_time_ms, exit_time_ms, signal_id, loss_at_stop,
                net_r_real, net_r_virtual, income_status
            ) VALUES ('BTCUSDT', 'long', ?, ?, ?, ?, ?, ?, 1, ?, ?, 'complete')
            """,
            (oid, status, end, entry, exit_ms, signal_id, net, net),
        )

    add_real("in", entry=100, exit_ms=1_500, status="closed", signal_id="as_in", net=0.5)
    add_real("out", entry=1_500, exit_ms=2_500, status="closed", signal_id="as_out", net=-9)
    add_real("held", entry=1_500, exit_ms=None, status="open", signal_id="as_held", net=None)
    _sent_track(conn, "as_in", 1_500, net_r=0.2)
    _sent_track(conn, "as_virt", 1_600, net_r=0.3)
    _sent_track(conn, "as_early", 500, net_r=0.3)
    conn.commit()
    summary = summarize_real_vs_virtual(conn, since_ms=start, until_ms=end)
    assert summary["closed_n"] == 1
    assert summary["mean_net_r"] == pytest.approx(0.5)
    assert summary["open_n"] == 1
    assert summary["n_virtual_without_real"] == 1
    conn.close()


def test_re_entry_when_first_trade_exited_before_the_window(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "role.db")
    conn.execute(
        """
        INSERT INTO assist_real_trade (
            symbol, side, entry_order_id, status, updated_at_ms,
            entry_time_ms, exit_time_ms, signal_id, loss_at_stop,
            net_r_real, income_status
        ) VALUES
            ('BTCUSDT', 'long', 'first', 'closed', 1, 100, 200, 'as_same', 1, 0.2, 'complete'),
            ('BTCUSDT', 'long', 'second', 'closed', 1, 1100, 1500, 'as_same', 1, -0.4, 'complete')
        """
    )
    conn.commit()
    summary = summarize_real_vs_virtual(conn, since_ms=1_000, until_ms=2_000)
    assert summary["n_re_entry"] == 1
    assert summary["closed_n"] == 0
    conn.close()


def test_income_status_migration_is_additive(tmp_path: Path) -> None:
    path = tmp_path / "mig.db"
    conn = open_db(path)
    conn.execute(
        """
        INSERT INTO assist_real_trade (
            symbol, side, entry_order_id, status, updated_at_ms, net_r_real, entry_time_ms,
            income_status
        ) VALUES ('BTCUSDT', 'long', 'keep', 'closed', 1, 1.25, 10, 'complete')
        """
    )
    conn.execute("UPDATE assist_real_trade SET income_status=NULL WHERE entry_order_id='keep'")
    conn.execute("ALTER TABLE assist_real_trade DROP COLUMN income_status")
    conn.execute("PRAGMA user_version = 24")
    conn.commit()
    conn.close()

    conn = open_db(path)
    assert schema_version(conn) == 25
    row = conn.execute(
        """
        SELECT net_r_real, entry_time_ms, income_status
        FROM assist_real_trade WHERE entry_order_id='keep'
        """
    ).fetchone()
    assert row["net_r_real"] == pytest.approx(1.25)
    assert row["entry_time_ms"] == 10
    assert row["income_status"] is None
    conn.close()


def test_refresh_failure_does_not_block_watch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = open_db(tmp_path / "fail.db")
    day = _ms_bangkok(2026, 11, 9, 12, 0)
    upsert_watch_position(
        conn,
        {
            "symbol": "BTCUSDT",
            "position_side": "BOTH",
            "side": "long",
            "entry": 100.0,
            "qty": 1.0,
            "in_universe": 1,
            "signal_id": None,
            "initial_stop": 90.0,
            "initial_stop_source": "signal",
            "entry_time_ms": day - HOUR,
            "no_stop_latched": 0,
            "last_seen_ms": day - HOUR,
            "closed_at_ms": None,
        },
    )
    conn.execute(
        """
        INSERT INTO assist_real_trade (
            symbol, side, entry_order_id, status, updated_at_ms, intent_id,
            entry_time_ms, exit_time_ms, loss_at_stop, net_r_real, income_status
        ) VALUES ('ETHUSDT', 'long', 'loss', 'closed', ?, 'intent-bot', ?, ?, 1, -3, NULL)
        """,
        (day, day - HOUR, day - 1_000),
    )
    conn.commit()
    order: list[str] = []

    def cancel(_trade: Any, _symbol: str) -> tuple[list[str], bool]:
        order.append("cancel")
        return (["algo-1"], True)

    def journal(*_a: Any, **_k: Any) -> dict[str, Any]:
        order.append("journal")
        return {"ok": True}

    def refresh(*_a: Any, **_k: Any) -> dict[str, Any]:
        order.append("refresh")
        raise RuntimeError("income")

    monkeypatch.setattr("jarvise_assist.watch.assist_execute_mode", lambda: "live")
    monkeypatch.setattr("jarvise_assist.watch.build_trade_client", lambda: (object(), None))
    monkeypatch.setattr("jarvise_assist.watch.cancel_jv_orders_for_symbol", cancel)
    monkeypatch.setattr("jarvise_assist.watch.journal_on_close", journal)
    monkeypatch.setattr("jarvise_assist.watch.refresh_incomplete_income", refresh)
    payload = run_assist_watch(conn, futures=AssistFutures(), now_ms_value=day, send=lambda _t: True)
    assert order == ["cancel", "journal", "refresh"]
    assert payload["ok"] is True
    assert payload["orphan_cancelled"] == [{"symbol": "BTCUSDT", "algo_id": "algo-1"}]
    assert payload["pause"]["reason"] == "day_loss"
    kept = conn.execute("SELECT net_r_real FROM assist_real_trade WHERE entry_order_id='loss'").fetchone()
    assert kept["net_r_real"] == pytest.approx(-3.0)
    conn.close()
