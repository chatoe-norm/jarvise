"""Risk monitor candle freshness guard (close-age + SQLite edge)."""

from __future__ import annotations

from datetime import UTC, datetime
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
from jarvise_paper.risk_monitor import CANDLE_STALE_KEY, run_risk_monitor


def _ms(year: int, month: int, day: int, hour: int, minute: int = 0, second: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, second, tzinfo=UTC).timestamp() * 1000)


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


def _flag(conn: Any) -> float | None:
    row = conn.execute(
        "SELECT value FROM paper_account WHERE key=?",
        (CANDLE_STALE_KEY,),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return float(row[0])


def test_risk_monitor_skips_when_open_candle_stale(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JARVISE_MARKET_SAFETY", "0")
    monkeypatch.delenv("JARVISE_RISK_STALE_MS", raising=False)
    monkeypatch.setattr("jarvise_paper.risk_monitor.notify_risk_candle_stale", lambda *_a, **_k: True)
    monkeypatch.setattr("jarvise_paper.risk_monitor.notify_risk_candle_fresh", lambda *_a, **_k: True)

    conn = open_db(tmp_path / "stale.db")
    _seed_long(conn)
    # Open 06:00 → close 07:00; at 10:00:19 close-age > 2h → stale.
    open_ts = _ms(2026, 10, 6, 6, 0, 0)
    now = _ms(2026, 10, 6, 10, 0, 19)
    upsert_market_technicals(conn, [_candle(open_ts)])
    conn.commit()
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
    monkeypatch.setattr("jarvise_paper.risk_monitor.notify_risk_candle_stale", lambda *_a, **_k: True)
    monkeypatch.setattr("jarvise_paper.risk_monitor.notify_risk_candle_fresh", lambda *_a, **_k: True)

    conn = open_db(tmp_path / "fresh.db")
    _seed_long(conn)
    now = _ms(2026, 10, 6, 10, 0, 19)
    # Open 09:00 → close 10:00; close-age 19s.
    upsert_market_technicals(conn, [_candle(_ms(2026, 10, 6, 9, 0, 0), low=96.0, close=99.0)])
    conn.commit()
    before = _snapshot_count(conn)
    out = run_risk_monitor(conn, now_ms=now)
    assert out.get("skipped") is None
    assert out["ok"] is True
    assert "stops" in out
    assert _snapshot_count(conn) > before
    assert get_paper_position(conn, "BTCUSDT") is None
    assert "stop_hit" in {o.get("reason") for o in list_paper_orders(conn)}


def test_risk_monitor_hour_top_race_not_stale(tmp_path: Path, monkeypatch) -> None:
    """Ingest has not yet written the 09:00 bar; newest open is 08:00 → still not stale."""
    monkeypatch.setenv("JARVISE_MARKET_SAFETY", "0")
    monkeypatch.delenv("JARVISE_RISK_STALE_MS", raising=False)
    monkeypatch.setattr("jarvise_paper.risk_monitor.notify_risk_candle_stale", lambda *_a, **_k: True)
    monkeypatch.setattr("jarvise_paper.risk_monitor.notify_risk_candle_fresh", lambda *_a, **_k: True)

    conn = open_db(tmp_path / "race.db")
    _seed_long(conn)
    now = _ms(2026, 10, 6, 10, 0, 19)
    upsert_market_technicals(conn, [_candle(_ms(2026, 10, 6, 8, 0, 0), low=96.0, close=99.0)])
    conn.commit()
    out = run_risk_monitor(conn, now_ms=now)
    assert out.get("skipped") is None
    assert out["ok"] is True
    assert "stops" in out
    assert get_paper_position(conn, "BTCUSDT") is None
    assert "stop_hit" in {o.get("reason") for o in list_paper_orders(conn)}


def test_risk_monitor_real_outage_skips_once(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JARVISE_MARKET_SAFETY", "0")
    monkeypatch.delenv("JARVISE_RISK_STALE_MS", raising=False)
    alerts: list[str] = []
    monkeypatch.setattr(
        "jarvise_paper.risk_monitor.notify_risk_candle_stale",
        lambda *_a, **_k: alerts.append("stale") or True,
    )
    monkeypatch.setattr(
        "jarvise_paper.risk_monitor.notify_risk_candle_fresh",
        lambda *_a, **_k: alerts.append("fresh") or True,
    )

    conn = open_db(tmp_path / "outage.db")
    _seed_long(conn)
    now = _ms(2026, 10, 6, 10, 0, 19)
    upsert_market_technicals(conn, [_candle(_ms(2026, 10, 6, 6, 0, 0))])
    conn.commit()
    assert run_risk_monitor(conn, now_ms=now)["skipped"] == "stale_data"
    assert run_risk_monitor(conn, now_ms=now + 1)["skipped"] == "stale_data"
    assert alerts == ["stale"]
    assert _flag(conn) == 1.0


def test_risk_monitor_alerts_once_on_stale_and_recovery(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JARVISE_MARKET_SAFETY", "0")
    monkeypatch.delenv("JARVISE_RISK_STALE_MS", raising=False)
    alerts: list[str] = []
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
    now_stale = _ms(2026, 10, 6, 10, 0, 19)
    upsert_market_technicals(conn, [_candle(_ms(2026, 10, 6, 6, 0, 0))])
    conn.commit()
    assert run_risk_monitor(conn, now_ms=now_stale)["skipped"] == "stale_data"
    assert run_risk_monitor(conn, now_ms=now_stale + 1)["skipped"] == "stale_data"
    assert alerts == ["stale"]

    upsert_market_technicals(
        conn,
        [_candle(_ms(2026, 10, 6, 9, 0, 0), low=98.0, close=100.0)],
    )
    conn.commit()
    out = run_risk_monitor(conn, now_ms=now_stale)
    assert out.get("skipped") is None
    out2 = run_risk_monitor(conn, now_ms=now_stale + 2)
    assert out2.get("skipped") is None
    assert alerts == ["stale", "fresh"]
    assert _flag(conn) == 0.0


def test_risk_monitor_edge_state_survives_without_redis(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JARVISE_MARKET_SAFETY", "0")
    monkeypatch.delenv("JARVISE_RISK_STALE_MS", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    alerts: list[str] = []
    monkeypatch.setattr(
        "jarvise_paper.risk_monitor.notify_risk_candle_stale",
        lambda *_a, **_k: alerts.append("stale") or True,
    )
    monkeypatch.setattr(
        "jarvise_paper.risk_monitor.notify_risk_candle_fresh",
        lambda *_a, **_k: True,
    )

    db = tmp_path / "persist.db"
    conn = open_db(db)
    _seed_long(conn)
    now = _ms(2026, 10, 6, 10, 0, 19)
    upsert_market_technicals(conn, [_candle(_ms(2026, 10, 6, 6, 0, 0))])
    conn.commit()
    assert run_risk_monitor(conn, now_ms=now)["skipped"] == "stale_data"
    assert alerts == ["stale"]
    conn.close()

    conn2 = open_db(db)
    assert _flag(conn2) == 1.0
    assert run_risk_monitor(conn2, now_ms=now + 15 * 60_000)["skipped"] == "stale_data"
    assert alerts == ["stale"]
