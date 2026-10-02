from pathlib import Path

import pytest

from jarvise_ingest.db import (
    ensure_paper_account,
    open_db,
    upsert_analysis_output,
    upsert_market_technicals,
    upsert_pending_approval,
    write_indicators,
)
from jarvise_paper.auto_decide import (
    DEFAULT_MODEL,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    UNPARSEABLE,
    AutoDecideConfig,
    brief_hash,
    build_brief,
    filter_candidates,
    load_auto_decide_config,
    parse_decision,
)
from jarvise_risk import load_risk_caps

NOW = 1_700_000_000_000 + 14_400_000 + 60_000
FAR = 9_999_999_999_999


def seed_db(path: Path) -> object:
    conn = open_db(path)
    ensure_paper_account(conn)
    for sym, close in (("BTCUSDT", 85000.0), ("ETHUSDT", 3000.0)):
        upsert_market_technicals(
            conn,
            [{"symbol": sym, "timestamp": 1_700_000_000_000, "timeframe": "4h", "open": close,
              "high": close, "low": close, "close": close, "volume": 1.0}],
        )
        write_indicators(
            conn,
            [{"symbol": sym, "timeframe": "4h", "timestamp": 1_700_000_000_000, "atr_14": close * 0.01,
              "rsi_14": 60.0, "ema_20": close * 0.99, "ema_200": close * 0.9}],
        )
        upsert_analysis_output(
            conn,
            {"analysis_id": f"an-{sym}", "timestamp": 1_700_000_000_000, "symbol": sym, "timeframe": "4h",
             "regime_state": "trend_up", "confidence_score": 0.75, "action": "long",
             "invalidation_price": close * 0.98, "size_pct_equity": 1.125, "thesis": "trend_up"},
        )
    return conn


def pending(conn, approval_id: str, symbol: str, **over) -> dict:
    row = {
        "id": approval_id,
        "created_at_ms": 1_000,
        "expires_at_ms": FAR,
        "symbol": symbol,
        "timeframe": "4h",
        "analysis_id": f"an-{symbol}",
        "action": "long",
        "regime_state": "trend_up",
        "confidence_score": 0.75,
        "size_pct_equity": 1.125,
        "status": "pending",
    }
    row.update(over)
    return upsert_pending_approval(conn, row)


def test_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "JARVISE_PAPER_AUTO_DECIDE", "JARVISE_AUTO_DECIDE_MODEL", "JARVISE_AUTO_DECIDE_MIN_CONF",
        "JARVISE_AUTO_DECIDE_MAX_PER_RUN", "JARVISE_AUTO_DECIDE_TIMEOUT_S", "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    cfg = load_auto_decide_config()
    assert cfg == AutoDecideConfig(
        enabled=False, model=DEFAULT_MODEL, min_conf=0.55, max_per_run=4, timeout_s=30.0, api_key=None
    )
    monkeypatch.setenv("JARVISE_PAPER_AUTO_DECIDE", "true")
    monkeypatch.setenv("JARVISE_AUTO_DECIDE_MAX_PER_RUN", "2")
    monkeypatch.setenv("OPENROUTER_API_KEY", " k ")
    cfg = load_auto_decide_config()
    assert cfg.enabled is True and cfg.max_per_run == 2 and cfg.api_key == "k"


def test_filter_candidates_reasons(tmp_path: Path) -> None:
    conn = seed_db(tmp_path / "f.db")
    # One pending row per symbol (unique index on pending symbol+timeframe). Filters never
    # need candles, so symbols without seeded OHLCV are fine here.
    pending(conn, "ok", "BTCUSDT", created_at_ms=5_000)
    pending(conn, "flat", "SOLUSDT", action="flat", size_pct_equity=0.0, created_at_ms=1_000)
    pending(conn, "zero", "ADAUSDT", size_pct_equity=0.0, created_at_ms=2_000)
    pending(conn, "low", "XRPUSDT", confidence_score=0.50, created_at_ms=3_000)
    pending(conn, "old", "ETHUSDT", expires_at_ms=NOW - 1, created_at_ms=4_000)
    rows = [dict(r) for r in conn.execute("SELECT * FROM approval_queue").fetchall()]
    eligible, filtered = filter_candidates(conn, rows, min_conf=0.55, now_ms=NOW)
    assert [r["id"] for r in eligible] == ["ok"]
    reasons = {f["id"]: f["reason"] for f in filtered}
    assert reasons == {"flat": "action_flat", "zero": "size_zero", "low": "low_conf", "old": "expired"}


def test_filter_force_flat(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    conn = seed_db(tmp_path / "ff.db")
    pending(conn, "ok", "BTCUSDT")

    class Unsafe:
        force_flat = True
        reasons = ["book_missing"]

    monkeypatch.setattr("jarvise_paper.auto_decide.evaluate_from_db", lambda *a, **k: Unsafe())
    rows = [dict(r) for r in conn.execute("SELECT * FROM approval_queue").fetchall()]
    eligible, filtered = filter_candidates(conn, rows, min_conf=0.55, now_ms=NOW)
    assert eligible == []
    assert filtered[0]["reason"] == "force_flat"


def test_build_brief_shape_and_hash(tmp_path: Path) -> None:
    conn = seed_db(tmp_path / "b.db")
    row = pending(conn, "ok", "BTCUSDT")
    brief = build_brief(conn, row, doctrine=["structure first"], now_ms=NOW,
                        caps=load_risk_caps(), min_conf=0.55)
    assert brief["candidate"]["symbol"] == "BTCUSDT"
    assert brief["candidate"]["invalidation_price"] == 85000.0 * 0.98
    assert brief["indicators"]["ema_200"] == 85000.0 * 0.9
    assert brief["ledger"] == {"equity": 10000.0, "cash": 10000.0, "open_positions": 0, "same_symbol_open": False}
    assert brief["market_safety"]["reasons"] == []
    assert brief["doctrine"] == ["structure first"]
    assert brief["policy"]["estimated_notional_usd"] == 112.5
    assert brief["policy"]["max_notional_per_order_usd"] == 2000.0
    assert brief["prompt_version"] == PROMPT_VERSION
    assert brief_hash(brief) == brief_hash(dict(brief))
    assert len(brief_hash(brief)) == 16


def test_parse_decision_strict() -> None:
    assert parse_decision({"decision": "approve", "reason": "ok"}) == ("approve", "ok")
    assert parse_decision({"decision": "REJECT", "reason": "x" * 300}) == ("reject", "x" * 280)
    assert parse_decision({"decision": "approve"}) == ("defer", UNPARSEABLE)
    assert parse_decision({"decision": "approve", "reason": "r", "extra": 1}) == ("defer", UNPARSEABLE)
    assert parse_decision({"decision": "maybe", "reason": "r"}) == ("defer", UNPARSEABLE)
    assert parse_decision("approve") == ("defer", UNPARSEABLE)


def test_system_prompt_pins_rules() -> None:
    assert "paper" in SYSTEM_PROMPT.lower()
    assert '"defer"' in SYSTEM_PROMPT
    assert "0.70" in SYSTEM_PROMPT
    assert PROMPT_VERSION in SYSTEM_PROMPT
