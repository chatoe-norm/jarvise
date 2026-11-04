"""Virtual track: stop exit labels, fee + slippage cost, and R-based weekly stats."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.test_assist import _ms_bangkok

from jarvise_assist.sizing import size_signal
from jarvise_assist.store import insert_signal
from jarvise_assist.track import FEE_BPS_PER_SIDE, SLIPPAGE_BPS_PER_SIDE, resolve_open_tracks
from jarvise_assist.weekly import format_weekly_summary, summarize_virtual
from jarvise_ingest.db import open_db, upsert_market_technicals

H = 3_600_000


def _track(tmp_path: Path, bars: list[tuple[float, float, float, float]]) -> tuple[Any, str, int]:
    """Long BTC entry 100, stop 90 (R = 10), target 120; one 1h bar per tuple (o, h, l, c)."""
    conn = open_db(tmp_path / "track.db")
    sent_at = _ms_bangkok(2026, 10, 27, 8, 0)
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
    sid = insert_signal(
        conn, sized, status="sent", created_at_ms=sent_at, expires_at_ms=sent_at + 1, sent_at_ms=sent_at
    )
    upsert_market_technicals(
        conn,
        [
            {
                "timestamp": sent_at + (i + 1) * H,
                "symbol": "BTCUSDT",
                "timeframe": "1h",
                "open": o,
                "high": h,
                "low": low,
                "close": c,
                "volume": 1.0,
            }
            for i, (o, h, low, c) in enumerate(bars)
        ],
    )
    now = sent_at + (len(bars) + 2) * H
    return conn, sid, now


def _row(conn: Any, sid: str) -> dict[str, Any]:
    cur = conn.execute(
        "SELECT t.exit_reason, t.exit_px, t.gross_r, t.net_r, t.fee_usdt, t.pnl_usdt, t.qty, s.loss_at_stop "
        "FROM assist_track t JOIN assist_signal s ON s.id = t.signal_id WHERE t.signal_id=?",
        (sid,),
    )
    names = [d[0] for d in cur.description]
    return dict(zip(names, cur.fetchone(), strict=True))


def test_initial_stop_label_and_cost_includes_fee_and_slippage(tmp_path: Path) -> None:
    conn, sid, now = _track(tmp_path, [(99.0, 99.5, 89.0, 89.5)])
    assert resolve_open_tracks(conn, now_ms=now)["n"] == 1
    row = _row(conn, sid)
    assert row["exit_reason"] == "initial_stop"
    assert row["exit_px"] == 90.0
    qty = float(row["qty"])
    notional_both = (100.0 + 90.0) * qty
    fee = notional_both * FEE_BPS_PER_SIDE / 10_000
    slip = notional_both * SLIPPAGE_BPS_PER_SIDE / 10_000
    assert FEE_BPS_PER_SIDE == 5.0
    assert row["fee_usdt"] == pytest.approx(fee)
    assert row["pnl_usdt"] == pytest.approx(-10.0 * qty - fee - slip)
    assert row["net_r"] == pytest.approx((-10.0 * qty - fee - slip) / float(row["loss_at_stop"]))
    assert row["net_r"] < row["gross_r"]
    conn.close()


def test_breakeven_stop_label(tmp_path: Path) -> None:
    # +0.6R moves the stop to breakeven (entry + fee buffer); the next bar fills it.
    conn, sid, now = _track(tmp_path, [(100.5, 106.0, 101.0, 105.0), (101.0, 101.5, 99.0, 99.5)])
    resolve_open_tracks(conn, now_ms=now)
    row = _row(conn, sid)
    assert row["exit_reason"] == "be_stop"
    assert row["exit_px"] == pytest.approx(100.2)
    conn.close()


def test_trail_stop_label(tmp_path: Path) -> None:
    # +1.1R trails the stop to best_high - 1R = 101; the next bar fills it.
    conn, sid, now = _track(tmp_path, [(100.5, 111.0, 101.5, 110.0), (104.0, 104.5, 100.5, 101.0)])
    resolve_open_tracks(conn, now_ms=now)
    row = _row(conn, sid)
    assert row["exit_reason"] == "trail_stop"
    assert row["exit_px"] == pytest.approx(101.0)
    assert row["gross_r"] == pytest.approx(0.1)
    conn.close()


def test_weekly_r_stats_and_labels() -> None:
    rows = [
        {"exit_reason": "target_2r", "gross_r": 2.05, "net_r": 2.0, "pnl_usdt": 20.0},
        {"exit_reason": "initial_stop", "gross_r": -0.95, "net_r": -1.0, "pnl_usdt": -10.0},
        {"exit_reason": "be_stop", "gross_r": 0.0, "net_r": -1.0, "pnl_usdt": -10.0},
        {"exit_reason": "trail_stop", "gross_r": 0.65, "net_r": 0.6, "pnl_usdt": 6.0},
        {"exit_reason": "flat", "gross_r": 0.0, "net_r": 0.0, "pnl_usdt": 0.0},
    ]
    v = summarize_virtual(rows)
    assert v["mean_r"] == pytest.approx(0.12)
    assert v["median_r"] == pytest.approx(0.0)
    assert v["avg_win_r"] == pytest.approx(1.3)
    assert v["avg_loss_r"] == pytest.approx(-1.0)
    assert (v["exit_stop_original"], v["exit_stop_be"], v["exit_stop_trail"]) == (1, 1, 1)
    assert (v["exit_target_2r"], v["exit_analysis"]) == (1, 1)
    text = format_weekly_summary(
        counts={"sent": 5, "skipped": 0, "taken": 0},
        virtual=v,
        real={"realized_pnl": 0.0, "funding": 0.0, "commission": 0.0, "universe_usdt": 0.0},
        closed_all=5,
    )
    assert "Expectancy +0.12R/ไม้ · มัธยฐาน 0.00R · ชนะเฉลี่ย +1.30R · แพ้เฉลี่ย -1.00R" in text
    assert "ปิดที่ stop เดิม 1 · stop ที่ทุน 1 · stop ที่ trail 1 · ถึง 2R 1 · ปิดตามสัญญาณ 1" in text


def test_weekly_no_losses_shows_dash() -> None:
    v = summarize_virtual([{"exit_reason": "target_2r", "gross_r": 2.0, "net_r": 1.9, "pnl_usdt": 19.0}])
    assert v["avg_loss_r"] is None
    text = format_weekly_summary(counts={}, virtual=v, real={"universe_usdt": 0.0}, closed_all=1)
    assert "แพ้เฉลี่ย —" in text
    assert "—R" not in text
