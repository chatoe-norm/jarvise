"""Virtual track: stop exit labels, fee + slippage cost, and R-based weekly stats."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.test_assist import _ms_bangkok

from jarvise_assist.sizing import size_signal
from jarvise_assist.store import (
    count_closed_tracks_all,
    insert_signal,
    list_closed_tracks_since,
    mark_signal_sent,
)
from jarvise_assist.track import (
    FEE_BPS_PER_SIDE,
    FUNDING_INTERVAL_MS,
    SLIPPAGE_BPS_PER_SIDE,
    TRACK_COHORT,
    resolve_open_tracks,
)
from jarvise_assist.weekly import format_weekly_summary, summarize_virtual
from jarvise_ingest.db import append_derivatives, open_db, upsert_market_technicals

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
        {"exit_reason": "target_2r", "gross_r": 2.05, "net_r": 2.0, "pnl_usdt": 20.0, "funding_usdt": 0.1},
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
    assert v["funding_usdt"] == pytest.approx(0.1)
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
    assert "funding เสมือน +0.10 USDT" in text


def test_weekly_no_losses_shows_dash() -> None:
    v = summarize_virtual([{"exit_reason": "target_2r", "gross_r": 2.0, "net_r": 1.9, "pnl_usdt": 19.0}])
    assert v["avg_loss_r"] is None
    text = format_weekly_summary(counts={}, virtual=v, real={"universe_usdt": 0.0}, closed_all=1)
    assert "แพ้เฉลี่ย —" in text
    assert "—R" not in text
    assert "funding เสมือน 0.00 USDT" in text


def _sized(*, side: str = "long", symbol: str = "BTCUSDT") -> Any:
    sized = size_signal(
        symbol=symbol,
        side=side,
        entry=Decimal("100"),
        stop=Decimal("90") if side == "long" else Decimal("110"),
        balance=Decimal("1000"),
        risk_pct=Decimal("2"),
        step_size=Decimal("0.001"),
    )
    assert sized is not None
    return sized


def test_cohort_backfill_v1_and_new_rows_v2(tmp_path: Path) -> None:
    path = tmp_path / "cohort.db"
    conn = open_db(path)
    assert TRACK_COHORT == "core7_4h_v2"
    # Simulate a pre-migration row with NULL cohort (repair backfills on next open).
    sized = _sized()
    sid_old = insert_signal(conn, sized, status="sent", created_at_ms=1, expires_at_ms=2, sent_at_ms=1)
    conn.execute("UPDATE assist_track SET cohort=NULL WHERE signal_id=?", (sid_old,))
    conn.commit()
    conn.close()

    conn = open_db(path)
    row = conn.execute("SELECT cohort FROM assist_track WHERE signal_id=?", (sid_old,)).fetchone()
    assert row is not None and row[0] == "core7_4h_v1"
    sid_new = insert_signal(
        conn, _sized(symbol="ETHUSDT"), status="sent", created_at_ms=3, expires_at_ms=4, sent_at_ms=3
    )
    row2 = conn.execute("SELECT cohort, kind FROM assist_track WHERE signal_id=?", (sid_new,)).fetchone()
    assert row2 is not None
    assert row2[0] == "core7_4h_v2"
    assert row2[1] == "sent"
    conn.close()


def test_weekly_ignores_v1_cohort(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "v1.db")
    now = _ms_bangkok(2026, 10, 27, 12, 0)
    week_start = now - 7 * 24 * H
    # Closed v1 sent row must not enter the v2 weekly block or edge count.
    conn.execute(
        """
        INSERT INTO assist_signal (
            id, symbol, side, entry, stop, target, qty, notional, leverage, loss_at_stop,
            created_at_ms, expires_at_ms, status
        ) VALUES ('as_v1', 'BTCUSDT', 'long', 100, 90, 120, 0.2, 20, 5, 2, ?, ?, 'sent')
        """,
        (week_start + 1, week_start + 2),
    )
    conn.execute(
        """
        INSERT INTO assist_track (
            signal_id, symbol, side, entry, stop, target, qty, sent_at_ms,
            exit_at_ms, exit_px, exit_reason, gross_r, net_r, fee_usdt, pnl_usdt,
            cohort, kind, funding_usdt
        ) VALUES (
            'as_v1', 'BTCUSDT', 'long', 100, 90, 120, 0.2, ?,
            ?, 90, 'initial_stop', -1.0, -1.04, 0.1, -2.0,
            'core7_4h_v1', 'sent', 0.0
        )
        """,
        (week_start + 1, week_start + H),
    )
    conn.commit()
    assert list_closed_tracks_since(conn, since_ms=week_start, cohort=TRACK_COHORT, kind="sent") == []
    assert count_closed_tracks_all(conn, cohort=TRACK_COHORT, kind="sent") == 0
    assert count_closed_tracks_all(conn) == 1
    conn.close()


def test_funding_long_short_sign_and_missing_rate(tmp_path: Path) -> None:
    # Entry just before 08:00 UTC; exit after that settlement so one funding event applies.
    settle = int(datetime(2026, 10, 27, 8, 0, tzinfo=UTC).timestamp() * 1000)
    assert settle % FUNDING_INTERVAL_MS == 0
    entry_ms = settle - H
    exit_bar_open = settle + H  # bar after settlement; close_ms = settle + 2H
    rate = 0.0001

    def _run(*, side: str, seed_rate: bool) -> dict[str, Any]:
        conn = open_db(tmp_path / f"fund-{side}-{seed_rate}.db")
        stop = Decimal("90") if side == "long" else Decimal("110")
        sized = size_signal(
            symbol="BTCUSDT",
            side=side,
            entry=Decimal("100"),
            stop=stop,
            balance=Decimal("1000"),
            risk_pct=Decimal("2"),
            step_size=Decimal("0.001"),
        )
        assert sized is not None
        sid = insert_signal(
            conn,
            sized,
            status="sent",
            created_at_ms=entry_ms,
            expires_at_ms=entry_ms + 1,
            sent_at_ms=entry_ms,
        )
        if seed_rate:
            append_derivatives(
                conn,
                [
                    {
                        "symbol": "BTC",
                        "timestamp": settle - 60_000,
                        "funding_rate": rate,
                        "open_interest_usd": 1.0,
                        "long_short_ratio": None,
                        "liquidations_24h_usd": None,
                    },
                    # After settlement: must not be used for that t.
                    {
                        "symbol": "BTC",
                        "timestamp": settle + 1,
                        "funding_rate": 0.9,
                        "open_interest_usd": 1.0,
                        "long_short_ratio": None,
                        "liquidations_24h_usd": None,
                    },
                ],
            )
        # Gap through stop so exit is fast and after the settlement.
        low, high = (89.0, 99.0) if side == "long" else (101.0, 111.0)
        upsert_market_technicals(
            conn,
            [
                {
                    "timestamp": exit_bar_open,
                    "symbol": "BTCUSDT",
                    "timeframe": "1h",
                    "open": 100.0,
                    "high": high,
                    "low": low,
                    "close": 100.0,
                    "volume": 1.0,
                }
            ],
        )
        result = resolve_open_tracks(conn, now_ms=exit_bar_open + 2 * H)
        assert result["n"] == 1
        closed = result["closed"][0]
        row = conn.execute(
            "SELECT gross_r, net_r, pnl_usdt, funding_usdt, qty FROM assist_track WHERE signal_id=?",
            (sid,),
        ).fetchone()
        assert row is not None
        out = {
            "gross_r": float(row[0]),
            "net_r": float(row[1]),
            "pnl_usdt": float(row[2]),
            "funding_usdt": float(row[3] or 0.0),
            "qty": float(row[4]),
            "funding_missing": int(closed["funding_missing"]),
        }
        conn.close()
        return out

    long_ok = _run(side="long", seed_rate=True)
    qty = long_ok["qty"]
    expected_cost = 100.0 * qty * rate
    assert long_ok["funding_usdt"] == pytest.approx(expected_cost)
    assert long_ok["funding_missing"] == 0
    # Gross R ignores funding; net includes it as a cost.
    assert long_ok["gross_r"] == pytest.approx(-1.0)
    assert long_ok["pnl_usdt"] == pytest.approx(
        -10.0 * qty
        - (100.0 + 90.0) * qty * (FEE_BPS_PER_SIDE + SLIPPAGE_BPS_PER_SIDE) / 10_000
        - expected_cost
    )

    short_ok = _run(side="short", seed_rate=True)
    assert short_ok["funding_usdt"] == pytest.approx(-expected_cost)
    assert short_ok["funding_missing"] == 0

    missing = _run(side="long", seed_rate=False)
    assert missing["funding_usdt"] == 0.0
    assert missing["funding_missing"] == 1


def test_held_dedupe_and_weekly_hold_line(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "held.db")
    ts = _ms_bangkok(2026, 10, 27, 2, 0)
    sized = _sized()
    sid1 = insert_signal(conn, sized, status="held", created_at_ms=ts, expires_at_ms=ts + 10**9)
    sid2 = insert_signal(conn, sized, status="held", created_at_ms=ts + H, expires_at_ms=ts + 10**9)
    open_long = conn.execute(
        "SELECT signal_id, kind FROM assist_track WHERE symbol='BTCUSDT' AND side='long' AND exit_at_ms IS NULL"
    ).fetchall()
    assert len(open_long) == 1
    assert open_long[0][0] == sid1
    assert open_long[0][1] == "held"
    assert conn.execute("SELECT COUNT(*) FROM assist_track WHERE signal_id=?", (sid2,)).fetchone()[0] == 0

    short = _sized(side="short")
    sid_short = insert_signal(conn, short, status="held", created_at_ms=ts + 2 * H, expires_at_ms=ts + 10**9)
    assert (
        conn.execute("SELECT kind FROM assist_track WHERE signal_id=?", (sid_short,)).fetchone()[0] == "held"
    )

    # Promote the open held long to sent at send time.
    mark_signal_sent(conn, sid1, sized, sent_at_ms=ts + 3 * H, expires_at_ms=ts + 10**9)
    kind = conn.execute("SELECT kind FROM assist_track WHERE signal_id=?", (sid1,)).fetchone()
    assert kind is not None and kind[0] == "sent"

    # Fixture for the PR report: closed v1 sent + v2 sent + v2 held.
    since = ts - H
    for sid, cohort, kind_v, net_r in (
        ("as_fix_v1", "core7_4h_v1", "sent", -1.0),
        ("as_fix_v2s", "core7_4h_v2", "sent", 0.5),
        ("as_fix_v2h", "core7_4h_v2", "held", -0.2),
    ):
        conn.execute(
            """
            INSERT INTO assist_signal (
                id, symbol, side, entry, stop, target, qty, notional, leverage, loss_at_stop,
                created_at_ms, expires_at_ms, status
            ) VALUES (?, 'ETHUSDT', 'long', 100, 90, 120, 0.1, 10, 5, 1, ?, ?, 'sent')
            """,
            (sid, since, since + 1),
        )
        conn.execute(
            """
            INSERT INTO assist_track (
                signal_id, symbol, side, entry, stop, target, qty, sent_at_ms,
                exit_at_ms, exit_px, exit_reason, gross_r, net_r, fee_usdt, pnl_usdt,
                cohort, kind, funding_usdt
            ) VALUES (?, 'ETHUSDT', 'long', 100, 90, 120, 0.1, ?, ?, 100, 'flat', 0, ?, 0, ?, ?, ?, 0)
            """,
            (sid, since, since + H, net_r, net_r * 1.0, cohort, kind_v),
        )
    conn.commit()
    sent_rows = list_closed_tracks_since(conn, since_ms=since, cohort=TRACK_COHORT, kind="sent")
    held_rows = list_closed_tracks_since(conn, since_ms=since, cohort=TRACK_COHORT, kind="held")
    virtual = summarize_virtual(sent_rows)
    held = summarize_virtual(held_rows)
    text = format_weekly_summary(
        counts={"sent": 1, "skipped": 0, "taken": 0},
        virtual=virtual,
        real={"realized_pnl": 0.0, "funding": 0.0, "commission": 0.0, "universe_usdt": 0.0},
        closed_all=count_closed_tracks_all(conn, cohort=TRACK_COHORT, kind="sent"),
        held=held,
    )
    assert "ไม้เสมือน: n=1" in text
    assert "สัญญาณที่ถูก hold: 1 · Expectancy -0.20R" in text
    assert "funding เสมือน 0.00 USDT" in text
    # Bound for the ship report.
    print("FIXTURE_WEEKLY_SUMMARY:\n" + text)
    conn.close()
