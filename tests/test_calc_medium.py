"""JARV-11 Medium CALC-4 / 5 / 6 / 7 / 9 pins."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from tests.test_assist import FakeFutures

from jarvise_assist.pause import evaluate_pause, monday_bangkok_ms
from jarvise_assist.real_trades import _fee_usdt_and_other, fetch_all_income
from jarvise_assist.sizing import (
    ASSIST_FEE_BPS_PER_SIDE,
    MAX_LEVERAGE,
    SignalCandidate,
    assign_shared_leverage,
    shared_leverage_needed,
    size_signal,
)
from jarvise_assist.store import ensure_settings, write_settings
from jarvise_ingest.db import (
    STARTING_PAPER_EQUITY,
    ensure_paper_account,
    insert_equity_snapshot,
    open_db,
    set_paper_account_value,
)
from jarvise_paper.metrics import compute_paper_metrics
from jarvise_risk.caps import bangkok_day_bounds_ms, utc_day_bounds_ms

BANGKOK = timezone(timedelta(hours=7), name="Asia/Bangkok")


def _ms_bangkok(y: int, m: int, d: int, hh: int = 0, mm: int = 0) -> int:
    return int(datetime(y, m, d, hh, mm, tzinfo=BANGKOK).timestamp() * 1000)


# ---------------------------------------------------------------------------
# CALC-4 — shrink buffer + opening-fee reserve
# ---------------------------------------------------------------------------


def test_calc4_shrink_respects_buffer_and_open_fee() -> None:
    sized = size_signal(
        symbol="BTCUSDT",
        side="long",
        entry=Decimal("100"),
        stop=Decimal("99"),
        balance=Decimal("200"),
        risk_pct=Decimal("3"),
        step_size=Decimal("1"),
    )
    assert sized is not None
    # Uncapped risk size is larger than available×5; assign_shared_leverage shrinks.
    cand = SignalCandidate(sized=sized, confidence=0.8, symbol_rank=0)
    available = Decimal("100")
    kept, dropped = assign_shared_leverage([cand], available_balance=available)
    open_fee_rate = ASSIST_FEE_BPS_PER_SIDE / Decimal("10000")
    max_notional = (available * Decimal("0.95")) / ((Decimal("1") / Decimal(MAX_LEVERAGE)) + open_fee_rate)
    # With step 1 the buffer+fee formula keeps qty=4 (notional 400, L=5).
    # The old available×5 path kept qty=5 (notional 500, L=6) — that must not return.
    assert dropped == []
    assert len(kept) == 1
    qty = kept[0].sized.qty
    notional = kept[0].sized.notional
    assert qty == Decimal("4")
    assert notional == Decimal("400")
    assert notional <= max_notional
    assert shared_leverage_needed(total_notional=notional, available_balance=available) <= MAX_LEVERAGE
    assert qty != Decimal("5")


def test_calc4_available_zero_drops() -> None:
    sized = size_signal(
        symbol="BTCUSDT",
        side="long",
        entry=Decimal("100"),
        stop=Decimal("99"),
        balance=Decimal("200"),
        risk_pct=Decimal("3"),
        step_size=Decimal("1"),
    )
    assert sized is not None
    cand = SignalCandidate(sized=sized, confidence=0.8, symbol_rank=0)
    kept, dropped = assign_shared_leverage([cand], available_balance=Decimal("0"))
    assert kept == []
    assert dropped == [cand]


# ---------------------------------------------------------------------------
# CALC-5 — paginate income
# ---------------------------------------------------------------------------


class _PagingIncomeFutures(FakeFutures):
    """1000 early rows + one leftover realized loss; income_history pages by startTime."""

    def __init__(self, *, leftover_loss: Decimal, leftover_ms: int, week_start: int) -> None:
        # Wallet includes the leftover so week-start reconstruction stays positive.
        super().__init__(balance=Decimal("1000") + leftover_loss, income=[])
        self.leftover_loss = leftover_loss
        self.leftover_ms = leftover_ms
        self.calls: list[dict[str, Any]] = []
        rows: list[dict[str, Any]] = []
        for i in range(1000):
            rows.append(
                {
                    "incomeType": "COMMISSION",
                    "income": "-0.01",
                    "asset": "USDT",
                    "symbol": "BTCUSDT",
                    "time": week_start + 1_000 + i,
                    "tranId": 10_000 + i,
                }
            )
        rows.append(
            {
                "incomeType": "REALIZED_PNL",
                "income": str(leftover_loss),
                "asset": "USDT",
                "symbol": "BTCUSDT",
                "time": leftover_ms,
                "tranId": 99_999,
            }
        )
        self._rows = sorted(rows, key=lambda r: (int(r["time"]), int(r["tranId"])))

    def income_history(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(dict(kwargs))
        start = int(kwargs.get("start_ms") or 0)
        end = int(kwargs.get("end_ms") or 0)
        limit = int(kwargs.get("limit") or 1000)
        out = [r for r in self._rows if start <= int(r["time"]) <= end]
        return out[:limit]


class _NeverShortIncomeFutures(FakeFutures):
    """Always returns a full page so the pager hits the safety cap."""

    def __init__(self) -> None:
        super().__init__(balance=Decimal("1000"), income=[])
        self.calls = 0

    def income_history(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls += 1
        start = int(kwargs.get("start_ms") or 0)
        limit = int(kwargs.get("limit") or 1000)
        return [
            {
                "incomeType": "COMMISSION",
                "income": "-0.01",
                "asset": "USDT",
                "symbol": "BTCUSDT",
                "time": start + i + 1,
                "tranId": self.calls * 10_000 + i,
            }
            for i in range(limit)
        ]


def test_calc5_fetch_all_income_includes_leftover_page() -> None:
    week_start = _ms_bangkok(2026, 11, 23, 0, 0)
    leftover_ms = _ms_bangkok(2026, 11, 24, 10, 0)
    now = _ms_bangkok(2026, 11, 25, 12, 0)
    leftover = Decimal("-50")
    fake = _PagingIncomeFutures(leftover_loss=leftover, leftover_ms=leftover_ms, week_start=week_start)
    rows = fetch_all_income(fake, start_ms=week_start, end_ms=now)
    assert len(rows) == 1001
    assert len(fake.calls) >= 2
    realized = [r for r in rows if r["incomeType"] == "REALIZED_PNL"]
    assert len(realized) == 1
    assert Decimal(str(realized[0]["income"])) == leftover


def test_calc5_evaluate_pause_sees_paginated_leftover(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "p5.db")
    ensure_settings(conn)
    write_settings(conn, risk_pct=1.0)
    now = _ms_bangkok(2026, 11, 25, 12, 0)
    week_start = monday_bangkok_ms(now)
    leftover_ms = _ms_bangkok(2026, 11, 24, 10, 0)
    leftover = Decimal("-900")
    fake = _PagingIncomeFutures(leftover_loss=leftover, leftover_ms=leftover_ms, week_start=week_start)
    sent: list[str] = []
    result = evaluate_pause(conn, futures=fake, now_ms=now, send=lambda t: sent.append(str(t)) or True)
    assert result.get("error") != "income_incomplete"
    assert len(fake.calls) >= 2
    # Leftover -900 on ~1000 commissions trips week_loss when risk is 1%.
    assert result.get("paused") is True
    assert result.get("reason") == "week_loss"
    conn.close()


def test_calc5_page_cap_returns_income_incomplete(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "p5cap.db")
    ensure_settings(conn)
    write_settings(conn, risk_pct=1.0)
    now = _ms_bangkok(2026, 11, 25, 12, 0)
    fake = _NeverShortIncomeFutures()
    sent: list[str] = []
    result = evaluate_pause(conn, futures=fake, now_ms=now, send=lambda t: sent.append(str(t)) or True)
    assert result == {"paused": False, "error": "income_incomplete"}
    assert not sent  # no week_loss / streak decision from a truncated book
    assert fake.calls >= 100
    conn.close()


class _SharedTranIdThenOverlapFutures:
    """Two COMMISSION rows share tranId=777; page 2 re-returns the second row."""

    def __init__(self, *, start_ms: int) -> None:
        self.start_ms = start_ms
        self.calls: list[dict[str, Any]] = []
        self._rows = [
            {
                "incomeType": "COMMISSION",
                "income": "-0.30",
                "asset": "USDT",
                "symbol": "BTCUSDT",
                "time": start_ms + 1_000,
                "tradeId": 11,
                "tranId": 777,
            },
            {
                "incomeType": "COMMISSION",
                "income": "-0.25",
                "asset": "USDT",
                "symbol": "ETHUSDT",
                "time": start_ms + 2_000,
                "tradeId": 12,
                "tranId": 777,
            },
            {
                "incomeType": "COMMISSION",
                "income": "-0.10",
                "asset": "USDT",
                "symbol": "BTCUSDT",
                "time": start_ms + 3_000,
                "tradeId": 13,
                "tranId": 778,
            },
        ]

    def income_history(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(dict(kwargs))
        start = int(kwargs.get("start_ms") or 0)
        end = int(kwargs.get("end_ms") or 0)
        limit = int(kwargs.get("limit") or 1000)
        out = [r for r in self._rows if start <= int(r["time"]) <= end]
        return out[:limit]


def test_calc5_dedupe_keeps_shared_tranid_and_drops_page_overlap() -> None:
    """Shared (incomeType, tranId) stays distinct; identical overlap drops once."""
    start = _ms_bangkok(2026, 11, 23, 0, 0)
    end = _ms_bangkok(2026, 11, 23, 1, 0)
    fake = _SharedTranIdThenOverlapFutures(start_ms=start)
    rows = fetch_all_income(fake, start_ms=start, end_ms=end, limit=2)
    assert len(rows) == 3
    assert len(fake.calls) >= 2
    by_trade = {int(r["tradeId"]): Decimal(str(r["income"])) for r in rows}
    assert by_trade[11] == Decimal("-0.30")
    assert by_trade[12] == Decimal("-0.25")
    assert by_trade[13] == Decimal("-0.10")
    assert sum(by_trade.values()) == Decimal("-0.65")


# ---------------------------------------------------------------------------
# CALC-6 — paper day = Bangkok 00:00
# ---------------------------------------------------------------------------


def test_calc6_bangkok_day_bounds_differ_from_utc() -> None:
    now_ms = _ms_bangkok(2026, 11, 26, 1, 0)
    bkk_start, bkk_end = bangkok_day_bounds_ms(now_ms)
    utc_start, _utc_end = utc_day_bounds_ms(now_ms)
    assert bkk_start == _ms_bangkok(2026, 11, 26, 0, 0)
    assert bkk_end == _ms_bangkok(2026, 11, 27, 0, 0)
    # At 01:00 Bangkok on the 26th, UTC calendar day is still the 25th
    # (25 Nov 00:00 UTC = 25 Nov 07:00 Bangkok). They differ from Bangkok midnight.
    assert utc_start == int(datetime(2026, 11, 25, 0, 0, tzinfo=UTC).timestamp() * 1000)
    assert utc_start == _ms_bangkok(2026, 11, 25, 7, 0)
    # Next UTC midnight after Bangkok midnight is 26 Nov 07:00 Bangkok.
    assert _ms_bangkok(2026, 11, 26, 7, 0) == int(datetime(2026, 11, 26, 0, 0, tzinfo=UTC).timestamp() * 1000)
    assert bkk_start != utc_start
    # 01:00 on the 26th is in the 26 Nov Bangkok bucket (cap can reset at midnight).
    assert bkk_start <= now_ms < bkk_end
    # 25 Nov 08:00 Bangkok is NOT in the 26 Nov Bangkok day.
    prev = _ms_bangkok(2026, 11, 25, 8, 0)
    assert not (bkk_start <= prev < bkk_end)


# ---------------------------------------------------------------------------
# CALC-7 — daily_pnl_usd truly daily
# ---------------------------------------------------------------------------


def test_calc7_daily_pnl_vs_total(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "d7.db")
    ensure_paper_account(conn)
    day1 = _ms_bangkok(2026, 11, 25, 12, 0)
    day2 = _ms_bangkok(2026, 11, 26, 12, 0)
    # Day-1 close equity 10,100; day-2 open snapshot then +50 → 10,150.
    insert_equity_snapshot(conn, ts=day1, equity=10_100.0, cash=10_100.0, source="test")
    insert_equity_snapshot(conn, ts=day2, equity=10_100.0, cash=10_100.0, source="test")
    set_paper_account_value(conn, "equity", 10_150.0)
    set_paper_account_value(conn, "cash", 10_150.0)
    report = compute_paper_metrics(conn, now_ms=day2 + 3_600_000)
    assert report["daily_pnl_usd"] == 50.0
    assert report["total_pnl_usd"] == 150.0
    assert report["daily_pnl_usd"] != report["total_pnl_usd"]
    assert STARTING_PAPER_EQUITY == 10_000.0
    conn.close()


# ---------------------------------------------------------------------------
# CALC-9 — non-USDT commission conversion
# ---------------------------------------------------------------------------


def test_calc9_bnb_fee_converts_with_price() -> None:
    usdt, other = _fee_usdt_and_other(
        {"commissionAsset": "BNB", "commission": "0.001"},
        asset_prices={"BNB": 600.0},
    )
    assert abs(usdt - (-0.6)) < 1e-12
    assert other is None


def test_calc9_bnb_fee_without_price_stays_other() -> None:
    usdt, other = _fee_usdt_and_other(
        {"commissionAsset": "BNB", "commission": "0.001"},
        asset_prices=None,
    )
    assert usdt == 0.0
    assert other == "BNB:0.001"


def test_calc9_bnfcr_pegged_one_to_one() -> None:
    usdt, other = _fee_usdt_and_other(
        {"commissionAsset": "BNFCR", "commission": "2"},
    )
    assert usdt == -2.0
    assert other is None


def test_calc9_usdc_pegged_one_to_one() -> None:
    usdt, other = _fee_usdt_and_other(
        {"commissionAsset": "USDC", "commission": "2"},
    )
    assert usdt == -2.0
    assert other is None
