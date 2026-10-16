"""Tests for OpenClaw tactical post-mortem generator."""

from __future__ import annotations

import json
from pathlib import Path

from jarvise_ingest.db import (
    ensure_paper_account,
    open_db,
    upsert_market_technicals,
    upsert_paper_decision_outcome,
)
from jarvise_paper.cli import run
from jarvise_paper.engine import apply_signal
from jarvise_paper.post_mortem import (
    diagnose_trade,
    format_post_mortem_markdown,
    generate_post_mortems,
)


def test_diagnose_trade_long_overextended_loss() -> None:
    trade = {
        "symbol": "BTCUSDT",
        "side": "long",
        "entry_price": 68000.0,
        "exit_price": 66500.0,
        "pnl_usd": -150.0,
        "r_multiple": -0.022,
        "hold_ms": 7_200_000,
        "exit_reason": "stop_hit",
        "decision_source": "auto_claude",
        "close_ts": 1_700_000_000_000,
    }
    candle = {
        "close": 68000.0,
        "ema_20": 65000.0,
        "ema_200": 62000.0,
        "atr_14": 1000.0,  # dist = (68000 - 65000) / 1000 = 3.0x ATR (overextended)
        "rsi_14": 72.0,  # overbought
        "volume": 50.0,
    }
    avg_vol = 100.0  # ratio = 0.5x (low volume)

    diag = diagnose_trade(trade, candle, avg_vol)
    assert diag["is_loss"] is True
    assert diag["indicators"]["dist_ema20_atr"] == 3.0
    assert diag["indicators"]["volume_ratio"] == 0.5

    md = format_post_mortem_markdown(diag)
    assert "# Post-Mortem: BTCUSDT LONG Loss Lesson" in md
    assert "- **Category**: Post-Mortem"
    assert "EMA20" in md
    assert "กฎการเทรด" in md
    assert "1.5x ATR" in md


def test_diagnose_trade_short_loss() -> None:
    trade = {
        "symbol": "ETHUSDT",
        "side": "short",
        "entry_price": 2500.0,
        "exit_price": 2600.0,
        "pnl_usd": -80.0,
        "r_multiple": -0.04,
        "hold_ms": 3_600_000,
        "exit_reason": "stop_hit",
        "decision_source": "manual",
        "close_ts": 1_700_000_000_000,
    }
    candle = {
        "close": 2500.0,
        "ema_20": 2700.0,
        "ema_200": 2800.0,
        "atr_14": 80.0,  # dist = (2500 - 2700) / 80 = -2.5x ATR
        "rsi_14": 28.0,  # oversold
        "volume": 30.0,
    }
    avg_vol = 40.0

    diag = diagnose_trade(trade, candle, avg_vol)
    assert diag["is_loss"] is True
    assert diag["indicators"]["dist_ema20_atr"] == -2.5

    md = format_post_mortem_markdown(diag)
    assert "# Post-Mortem: ETHUSDT SHORT Loss Lesson" in md
    assert "Oversold" in md


def test_generate_post_mortems_from_db(tmp_path: Path) -> None:
    db_file = tmp_path / "test_pm.db"
    conn = open_db(db_file)
    ensure_paper_account(conn)

    # Seed technicals
    rows = [
        {
            "symbol": "BTCUSDT",
            "timestamp": 1_600_000_000_000 + i * 14_400_000,
            "timeframe": "4h",
            "open": 100.0,
            "high": 105.0,
            "low": 95.0,
            "close": 102.0,
            "volume": 50.0,
            "ema_20": 100.0,
            "ema_200": 90.0,
            "rsi_14": 55.0,
            "atr_14": 4.0,
        }
        for i in range(25)
    ]
    upsert_market_technicals(conn, rows)

    # Seed closed trade outcomes
    upsert_paper_decision_outcome(
        conn,
        {
            "approval_id": "app-1",
            "open_order_id": "ord-open-1",
            "close_order_id": "ord-close-1",
            "symbol": "BTCUSDT",
            "side": "long",
            "pnl_usd": -120.5,
            "r_multiple": -0.015,
            "hold_ms": 14_400_000,
            "decision_source": "auto_claude",
            "closed_at_ms": 1_600_000_000_000 + 20 * 14_400_000,
        },
    )
    conn.commit()

    out_dir = tmp_path / "tactics"
    res = generate_post_mortems(
        conn,
        output_dir=out_dir,
        limit=10,
        losses_only=True,
    )
    conn.close()

    assert res["ok"] is True
    assert len(res["written"]) == 1
    target_file = out_dir / "post-mortem-btcusdt-ord-clos.md"
    assert target_file.exists()
    content = target_file.read_text(encoding="utf-8")
    assert "BTCUSDT" in content
    assert "Loss Lesson" in content

    # Test idempotency (should skip on second run without force)
    conn2 = open_db(db_file)
    res2 = generate_post_mortems(
        conn2,
        output_dir=out_dir,
        limit=10,
        force=False,
    )
    conn2.close()
    assert len(res2["written"]) == 0
    assert len(res2["skipped"]) == 1


def test_post_mortem_cli(tmp_path: Path, capsys) -> None:
    db_file = tmp_path / "cli_pm.db"
    conn = open_db(db_file)
    ensure_paper_account(conn)

    # Seed an open and close order via apply_signal
    apply_signal(
        conn,
        analysis={
            "analysis_id": "a-open",
            "symbol": "ETHUSDT",
            "action": "long",
            "invalidation_price": 95.0,
            "size_pct_equity": 10.0,
            "regime_state": "trend_up",
            "confidence_score": 0.7,
        },
        mid_price=100.0,
        timeframe="4h",
        now_ms=1_000_000,
    )
    apply_signal(
        conn,
        analysis={
            "analysis_id": "a-close",
            "symbol": "ETHUSDT",
            "action": "flat",
            "invalidation_price": 95.0,
            "size_pct_equity": 0.0,
            "regime_state": "range",
            "confidence_score": 0.4,
        },
        mid_price=90.0,  # loss
        timeframe="4h",
        now_ms=1_050_000,
    )
    conn.close()

    out_dir = tmp_path / "exports" / "tactics"
    rc = run(
        [
            "post-mortem",
            "--db",
            str(db_file),
            "--output-dir",
            str(out_dir),
            "--json",
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is True
    assert payload["total_trades_analyzed"] == 1
    assert len(payload["written"]) == 1
