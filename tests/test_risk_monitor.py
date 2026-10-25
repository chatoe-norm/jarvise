"""Risk monitor candle freshness guard."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jarvise_ingest.db import (
    ensure_paper_account,
    get_paper_position,
    list_paper_orders,
    open_db,
    upsert_market_technicals,
    upsert_paper_position,
)
from jarvise_paper.risk_monitor import run_risk_monitor


def _seed_long(conn: Any, *, entry_ts: int = 1_000, stop: float = 97.0) -> None:
    ensure_paper_account(conn)
    upsert_paper_position(
        conn,
        {
            "symbol": "BTCUSDT",
            "side": "long",
            "qty": 1.0,
            "entry_price": 100.0,
            "entry_ts": entry_ts,
            "unrealized_pnl": 0.0,
            "realized_pnl": 0.0,
            "stop_price": stop,
            "stop_source": "analysis",
            "initial_stop_price": stop,
            "stop_moved_ts": None,
        },
    )
    conn.commit()


def _candle(ts: int, *, low: float = 96.0, close: float = 99.0) -> dict[str, Any]:
    return {
        "symbol": "BTCUSDT",
        "timestamp": ts,
        "timeframe": "1h",
        "open": 100.0,
        "high": 101.0,
        "low": low,
        "close": close,
        "volume": 1.0,
    }


def _snapshot_count(conn: Any) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM paper_equity_snapshots").fetchone()
    return int(row["n"] if row is not None else 0)


def test_risk_monitor_skips_when_open_candle_stale(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JARVISE_MARKET_SAFETY", "0")
    monkeypatch.delenv("JARVISE_RISK_STALE_MS", raising=False)
    monkeypatch.setattr("jarvise_paper.risk_monitor._read_candle_stale_flag", lambda: False)
    monkeypatch.setattr("jarvise_paper.risk_monitor._write_candle_stale_flag", lambda _s: None)
    monkeypatch.setattr("jarvise_paper.risk_monitor.notify_risk_candle_stale", lambda *_a, **_k: True)
    monkeypatch.setattr("jarvise_paper.risk_monitor.notify_risk_candle_fresh", lambda *_a, **_k: True)

    conn = open_db(tmp_path / "stale.db")
    _seed_long(conn)
    # Newest 1h bar is 3h old → over default 2×1h = 2h.
    upsert_market_technicals(conn, [_candle(1_000)])
    conn.commit()
    now = 1_000 + 3 * 3_600_000
    before = _snapshot_count(conn)
    out = run_risk_monitor(conn, now_ms=now)
    assert out["ok"] is True
    assert out["skipped"] == "stale_data"
    assert out["stale"][0]["symbol"] == "BTCUSDT"
    assert out["stale"][0]["threshold_ms"] == 7_200_000
    assert _snapshot_count(conn) == before
    assert get_paper_position(conn, "BTCUSDT") is not None
    assert "stop_hit" not in {o.get("reason") for o in list_paper_orders(conn)}


def test_risk_monitor_runs_when_open_candle_fresh(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JARVISE_MARKET_SAFETY", "0")
    monkeypatch.delenv("JARVISE_RISK_STALE_MS", raising=False)
    monkeypatch.setattr("jarvise_paper.risk_monitor._read_candle_stale_flag", lambda: False)
    monkeypatch.setattr("jarvise_paper.risk_monitor._write_candle_stale_flag", lambda _s: None)
    monkeypatch.setattr("jarvise_paper.risk_monitor.notify_risk_candle_stale", lambda *_a, **_k: True)
    monkeypatch.setattr("jarvise_paper.risk_monitor.notify_risk_candle_fresh", lambda *_a, **_k: True)

    conn = open_db(tmp_path / "fresh.db")
    _seed_long(conn)
    now = 10_000_000
    upsert_market_technicals(conn, [_candle(now - 60_000, low=96.0, close=99.0)])
    conn.commit()
    before = _snapshot_count(conn)
    out = run_risk_monitor(conn, now_ms=now)
    assert out.get("skipped") is None
    assert out["ok"] is True
    assert "stops" in out
    assert _snapshot_count(conn) > before
    assert get_paper_position(conn, "BTCUSDT") is None
    assert "stop_hit" in {o.get("reason") for o in list_paper_orders(conn)}


def test_risk_monitor_alerts_once_on_stale_and_recovery(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JARVISE_MARKET_SAFETY", "0")
    monkeypatch.delenv("JARVISE_RISK_STALE_MS", raising=False)
    state: dict[str, bool | None] = {"flag": None}
    alerts: list[str] = []

    monkeypatch.setattr(
        "jarvise_paper.risk_monitor._read_candle_stale_flag",
        lambda: state["flag"],
    )

    def _write(stale: bool) -> None:
        state["flag"] = stale

    monkeypatch.setattr("jarvise_paper.risk_monitor._write_candle_stale_flag", _write)
    monkeypatch.setattr(
        "jarvise_paper.risk_monitor.notify_risk_candle_stale",
        lambda *_a, **_k: alerts.append("stale") or True,
    )
    monkeypatch.setattr(
        "jarvise_paper.risk_monitor.notify_risk_candle_fresh",
        lambda *_a, **_k: alerts.append("fresh") or True,
    )

    conn = open_db(tmp_path / "edge.db")
    _seed_long(conn)
    stale_ts = 1_000
    upsert_market_technicals(conn, [_candle(stale_ts)])
    conn.commit()
    now_stale = stale_ts + 3 * 3_600_000
    assert run_risk_monitor(conn, now_ms=now_stale)["skipped"] == "stale_data"
    assert run_risk_monitor(conn, now_ms=now_stale + 1)["skipped"] == "stale_data"
    assert alerts == ["stale"]

    now_fresh = now_stale + 10_000
    upsert_market_technicals(conn, [_candle(now_fresh - 60_000, low=98.0, close=100.0)])
    conn.commit()
    out = run_risk_monitor(conn, now_ms=now_fresh)
    assert out.get("skipped") is None
    out2 = run_risk_monitor(conn, now_ms=now_fresh + 2)
    assert out2.get("skipped") is None
    assert alerts == ["stale", "fresh"]
