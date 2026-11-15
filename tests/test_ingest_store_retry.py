"""Ingest book/derivatives store retries once on SQLite locked/busy."""

from __future__ import annotations

import json
import sqlite3
import time
from functools import partial
from pathlib import Path

import pytest

from jarvise_ingest.db import append_derivatives, open_db, upsert_order_book


def _candle(symbol: str = "BTCUSDT") -> list[dict]:
    return [
        {
            "symbol": symbol,
            "timestamp": 1_700_000_000_000,
            "timeframe": "1h",
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
            "volume": 1.0,
        }
    ]


def _healthy_book(symbol: str = "BTCUSDT", *, ts: int | None = None) -> dict:
    return {
        "symbol": symbol,
        "timestamp": int(ts if ts is not None else time.time() * 1000),
        "bid_ask_spread": 0.0001,
        "bid_depth_1pct_usd": 100_000.0,
        "ask_depth_1pct_usd": 100_000.0,
        "largest_buy_wall_price": None,
        "largest_sell_wall_price": None,
        "spoof_wall_detected": 0,
    }


def _deriv_rows(symbol: str = "BTC", *, ts: int | None = None) -> list[dict]:
    return [
        {
            "symbol": symbol,
            "timestamp": int(ts if ts is not None else time.time() * 1000),
            "open_interest_usd": 1.0,
            "funding_rate": 0.0001,
            "long_short_ratio": None,
            "liquidations_24h_usd": None,
        }
    ]


def _stub_common(ingest_cli, monkeypatch: pytest.MonkeyPatch) -> list[float]:
    sleeps: list[float] = []
    monkeypatch.setattr(ingest_cli, "_sleep", lambda s: sleeps.append(float(s)))
    monkeypatch.setattr(ingest_cli, "fetch_klines", lambda *a, **k: _candle())
    monkeypatch.setattr(ingest_cli, "recompute_indicators", lambda *a, **k: 1)
    monkeypatch.setattr(ingest_cli, "find_gaps", lambda *a, **k: [])
    return sleeps


def _kill_recorder(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    import jarvise_risk.market_safety as ms

    reasons: list[str] = []

    def _engage(*, reason: str = "risk_cap_breach") -> bool:
        reasons.append(reason)
        return True

    monkeypatch.setattr(ms, "engage_kill_switch", _engage)
    return reasons


def test_book_store_locked_once_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("JARVISE_MARKET_SAFETY", "1")
    from jarvise_ingest import cli as ingest_cli

    sleeps = _stub_common(ingest_cli, monkeypatch)
    kills = _kill_recorder(monkeypatch)
    book = _healthy_book()
    monkeypatch.setattr(ingest_cli, "fetch_order_book_snapshot", lambda *a, **k: book)

    calls = {"n": 0}

    def flaky_upsert(conn: sqlite3.Connection, row: dict) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return upsert_order_book(conn, row)

    monkeypatch.setattr(ingest_cli, "upsert_order_book", flaky_upsert)

    db = tmp_path / "book-once.db"
    code = ingest_cli.run(
        [
            "--symbol",
            "BTCUSDT",
            "--timeframe",
            "1h",
            "--limit",
            "5",
            "--db",
            str(db),
            "--skip-macro",
            "--skip-derivatives",
            "--json",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert calls["n"] == 2
    assert sleeps == [1.0]
    assert kills == []
    assert not any(e.startswith("book:") for e in out.get("errors", []))
    safety = out["market_safety"]["BTCUSDT"]
    assert safety["critical"] is False
    assert not any(r.startswith("provider_error:") for r in safety["reasons"])
    with open_db(db) as check:
        n = check.execute(
            "SELECT COUNT(*) FROM order_book_microstructure WHERE symbol=?",
            ("BTCUSDT",),
        ).fetchone()[0]
    assert n == 1


def test_derivatives_store_locked_once_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("JARVISE_MARKET_SAFETY", "1")
    from jarvise_ingest import cli as ingest_cli
    from jarvise_ingest.db import append_derivatives

    sleeps = _stub_common(ingest_cli, monkeypatch)
    kills = _kill_recorder(monkeypatch)
    rows = _deriv_rows()
    monkeypatch.setattr(ingest_cli, "fetch_derivatives", lambda *a, **k: (rows, "binance_futures"))

    calls = {"n": 0}

    def flaky_append(conn: sqlite3.Connection, payload: list[dict], **kw: object) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return append_derivatives(conn, payload, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(ingest_cli, "append_derivatives", flaky_append)

    db = tmp_path / "deriv-once.db"
    code = ingest_cli.run(
        [
            "--symbol",
            "BTCUSDT",
            "--timeframe",
            "1h",
            "--limit",
            "5",
            "--db",
            str(db),
            "--skip-macro",
            "--skip-book",
            "--json",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert calls["n"] == 2
    assert sleeps == [1.0]
    assert kills == []
    assert not any(e.startswith("derivatives:") for e in out.get("errors", []))
    safety = out["market_safety"]["BTCUSDT"]
    assert safety["critical"] is False
    assert not any(r.startswith("provider_error:") for r in safety["reasons"])
    with open_db(db) as check:
        n = check.execute(
            "SELECT COUNT(*) FROM derivatives_analytics WHERE symbol=?",
            ("BTC",),
        ).fetchone()[0]
    assert n == 1


def test_book_fetch_http_error_still_critical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("JARVISE_MARKET_SAFETY", "1")
    from jarvise_ingest import cli as ingest_cli

    sleeps = _stub_common(ingest_cli, monkeypatch)
    kills = _kill_recorder(monkeypatch)

    def boom(*_a: object, **_k: object) -> dict:
        raise RuntimeError("HTTP 503")

    monkeypatch.setattr(ingest_cli, "fetch_order_book_snapshot", boom)

    db = tmp_path / "book-http.db"
    conn = open_db(db)
    upsert_order_book(conn, _healthy_book())
    conn.commit()
    conn.close()

    code = ingest_cli.run(
        [
            "--symbol",
            "BTCUSDT",
            "--timeframe",
            "1h",
            "--limit",
            "5",
            "--db",
            str(db),
            "--skip-macro",
            "--skip-derivatives",
            "--json",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert code == 1
    assert sleeps == []
    assert "HTTP 503" in out["errors"]
    safety = out["market_safety"]["BTCUSDT"]
    assert safety["critical"] is True
    assert any("provider_error: book:HTTP 503" in r for r in safety["reasons"])
    assert kills and kills[0].startswith("market_safety:")


def test_book_store_locked_twice_provider_error_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("JARVISE_MARKET_SAFETY", "1")
    from jarvise_ingest import cli as ingest_cli

    sleeps = _stub_common(ingest_cli, monkeypatch)
    kills = _kill_recorder(monkeypatch)
    book = _healthy_book()
    monkeypatch.setattr(ingest_cli, "fetch_order_book_snapshot", lambda *a, **k: book)

    calls = {"n": 0}

    def always_locked(*_a: object, **_k: object) -> int:
        calls["n"] += 1
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(ingest_cli, "upsert_order_book", always_locked)

    db = tmp_path / "book-twice.db"
    conn = open_db(db)
    upsert_order_book(conn, _healthy_book())
    conn.commit()
    conn.close()

    code = ingest_cli.run(
        [
            "--symbol",
            "BTCUSDT",
            "--timeframe",
            "1h",
            "--limit",
            "5",
            "--db",
            str(db),
            "--skip-macro",
            "--skip-derivatives",
            "--json",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert code == 1
    assert calls["n"] == 2
    assert sleeps == [1.0]
    assert "database is locked" in out["errors"]
    safety = out["market_safety"]["BTCUSDT"]
    assert safety["critical"] is True
    assert any("provider_error: book:database is locked" in r for r in safety["reasons"])
    assert kills and kills[0].startswith("market_safety:")


def test_book_store_other_operational_error_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("JARVISE_MARKET_SAFETY", "1")
    from jarvise_ingest import cli as ingest_cli

    sleeps = _stub_common(ingest_cli, monkeypatch)
    kills = _kill_recorder(monkeypatch)
    book = _healthy_book()
    monkeypatch.setattr(ingest_cli, "fetch_order_book_snapshot", lambda *a, **k: book)

    def disk_io(*_a: object, **_k: object) -> int:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(ingest_cli, "upsert_order_book", disk_io)

    db = tmp_path / "book-io.db"
    conn = open_db(db)
    upsert_order_book(conn, _healthy_book())
    conn.commit()
    conn.close()

    code = ingest_cli.run(
        [
            "--symbol",
            "BTCUSDT",
            "--timeframe",
            "1h",
            "--limit",
            "5",
            "--db",
            str(db),
            "--skip-macro",
            "--skip-derivatives",
            "--json",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert code == 1
    assert sleeps == []
    safety = out["market_safety"]["BTCUSDT"]
    assert safety["critical"] is True
    assert any("provider_error: book:disk I/O error" in r for r in safety["reasons"])
    assert kills and kills[0].startswith("market_safety:")


def test_store_retry_rolls_back_before_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A locked DML leaves an implicit BEGIN; rollback before retry so the busy handler can run."""
    import jarvise_ingest.db as db_mod
    from jarvise_ingest import cli as ingest_cli

    path = tmp_path / "retry-rollback.db"
    open_db(path).close()
    monkeypatch.setattr(db_mod, "BUSY_TIMEOUT_MS", 50)
    conn = open_db(path)
    holder = sqlite3.connect(str(path), isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    seen: list[bool] = []

    def _release_during_sleep(_s: float) -> None:
        seen.append(conn.in_transaction)
        holder.execute("ROLLBACK")

    monkeypatch.setattr(ingest_cli, "_sleep", _release_during_sleep)
    try:
        n = ingest_cli._store_with_lock_retry(
            "derivatives",
            "BTCUSDT",
            conn,
            partial(append_derivatives, conn, _deriv_rows()),
        )
        assert n == 1
        assert seen == [False]
        count = conn.execute("SELECT COUNT(*) FROM derivatives_analytics").fetchone()[0]
        assert count == 1
    finally:
        holder.close()
        conn.close()


def test_store_failed_twice_leaves_no_open_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After two locked attempts the conn must not pin a snapshot for the next symbol."""
    import jarvise_ingest.db as db_mod
    from jarvise_ingest import cli as ingest_cli

    path = tmp_path / "double-fail-clean.db"
    open_db(path).close()
    monkeypatch.setattr(db_mod, "BUSY_TIMEOUT_MS", 50)
    conn = open_db(path)
    holder = sqlite3.connect(str(path), isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    monkeypatch.setattr(ingest_cli, "_sleep", lambda _s: None)
    try:
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            ingest_cli._store_with_lock_retry(
                "book",
                "BTCUSDT",
                conn,
                partial(upsert_order_book, conn, _healthy_book("BTCUSDT")),
            )
        assert conn.in_transaction is False

        holder.execute("ROLLBACK")
        conn.execute("SELECT COUNT(*) FROM order_book_microstructure").fetchone()
        holder.execute("UPDATE assist_settings SET risk_pct = risk_pct WHERE id = 1")

        n = ingest_cli._store_with_lock_retry(
            "book",
            "ETHUSDT",
            conn,
            partial(upsert_order_book, conn, _healthy_book("ETHUSDT")),
        )
        assert n == 1
    finally:
        holder.close()
        conn.close()
