"""T2.6 Stooq equity OHLCV + paper_equity universe."""

from pathlib import Path

from jarvise_ingest.db import open_db, upsert_market_technicals
from jarvise_ingest.providers.stooq_ohlcv import (
    is_equity_symbol,
    parse_stooq_csv_text,
    stooq_ticker,
)
from jarvise_ingest.series import find_gaps, recompute_indicators
from jarvise_ingest.universe import PAPER_EQUITY, seed_paper_equity
from jarvise_paper.engine import load_paper_fee_bps
from jarvise_risk.market_safety import evaluate_from_db

CSV = """Date,Open,High,Low,Close,Volume
2024-01-02,470.0,475.0,469.0,474.0,1000
2024-01-03,474.0,476.0,472.0,475.0,1100
2024-01-04,475.0,478.0,474.0,477.0,1200
2024-01-05,477.0,480.0,476.0,479.0,1300
2024-01-08,479.0,482.0,478.0,481.0,1400
"""


def test_stooq_ticker_and_equity_detect() -> None:
    assert stooq_ticker("SPY") == "spy.us"
    assert is_equity_symbol("QQQ")
    assert not is_equity_symbol("BTCUSDT")


def test_parse_stooq_csv_and_weekend_gaps(tmp_path: Path) -> None:
    candles = parse_stooq_csv_text("SPY", "1d", CSV)
    assert len(candles) == 5
    assert candles[0]["symbol"] == "SPY"
    conn = open_db(tmp_path / "eq.db")
    upsert_market_technicals(conn, candles)
    recompute_indicators(conn, "SPY", "1d")
    # Fri→Mon is ≤3 calendar days — not a gap for equity 1d.
    gaps = find_gaps(conn, "SPY", "1d", asset_class="equity")
    assert gaps == []
    crypto_style = find_gaps(conn, "SPY", "1d", asset_class="crypto")
    assert len(crypto_style) >= 1
    conn.close()


def test_seed_paper_equity(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "u.db")
    n = seed_paper_equity(conn)
    assert n == 2
    rows = conn.execute(
        "SELECT symbol, asset_class FROM universe_membership WHERE universe_id=?",
        (PAPER_EQUITY,),
    ).fetchall()
    assert {r[0] for r in rows} == {"SPY", "QQQ"}
    assert all(r[1] == "equity" for r in rows)
    conn.close()


def test_equity_fee_and_market_safety(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("JARVISE_PAPER_EQUITY_FEE_BPS", raising=False)
    assert load_paper_fee_bps(symbol="SPY") == 2.0
    assert load_paper_fee_bps(symbol="BTCUSDT") == 10.0
    monkeypatch.setenv("JARVISE_MARKET_SAFETY", "1")
    conn = open_db(tmp_path / "ms.db")
    # No book/deriv for SPY — must not force flat from book_missing.
    result = evaluate_from_db(conn, "SPY")
    assert result.force_flat is False or "book_missing" not in result.reasons
    conn.close()


def test_equity_fee_env_override(monkeypatch) -> None:
    monkeypatch.setenv("JARVISE_PAPER_EQUITY_FEE_BPS", "1.5")
    assert load_paper_fee_bps(symbol="QQQ") == 1.5
