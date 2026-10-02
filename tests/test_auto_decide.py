import sqlite3
from pathlib import Path

import pytest

from jarvise_ingest.db import (
    ensure_paper_account,
    get_approval,
    get_latest_llm_review,
    get_paper_position,
    list_paper_orders,
    open_db,
    upsert_analysis_output,
    upsert_market_technicals,
    upsert_pending_approval,
    write_indicators,
)
from jarvise_paper.approval import approve_approval
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
    run_auto_decide,
)
from jarvise_paper.llm_openrouter import OpenRouterError
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


CFG = AutoDecideConfig(
    enabled=True, model="test/model", min_conf=0.55, max_per_run=4, timeout_s=1.0, api_key="k"
)
NO_DOCTRINE = lambda _q: []  # noqa: E731
DOCTRINE = lambda _q: [{"text": "structure first", "source": "d.md", "score": 0.9}]  # noqa: E731


@pytest.fixture(autouse=True)
def _live_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVISE_LIVE_TRADING", raising=False)


def _chat(decision: str, reason: str = "เหตุผล"):
    calls: list[dict] = []

    def fn(messages, **kwargs):
        calls.append({"messages": messages, **kwargs})
        return {"decision": decision, "reason": reason}

    fn.calls = calls  # type: ignore[attr-defined]
    return fn


def test_flag_off_skips_and_touches_nothing(tmp_path: Path) -> None:
    conn = seed_db(tmp_path / "a.db")
    pending(conn, "ok", "BTCUSDT")
    chat = _chat("approve")
    out = run_auto_decide(conn, now_ms=NOW, config=AutoDecideConfig(**{**CFG.__dict__, "enabled": False}),
                          chat=chat, doctrine_lookup=DOCTRINE)
    assert out["ok"] is True and out["skipped"] is True
    assert out["reason"] == "auto_decide_disabled" and out["paper_only"] is True
    assert chat.calls == []
    assert get_approval(conn, "ok")["status"] == "pending"


def test_live_on_refuses(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("JARVISE_LIVE_TRADING", "true")
    conn = seed_db(tmp_path / "l.db")
    pending(conn, "ok", "BTCUSDT")
    chat = _chat("approve")
    out = run_auto_decide(conn, now_ms=NOW, config=CFG, chat=chat, doctrine_lookup=DOCTRINE)
    assert out["ok"] is False and out["skipped"] is True and out["reason"] == "live_trading_enabled"
    assert chat.calls == []
    assert get_approval(conn, "ok")["status"] == "pending"


def test_approve_path_fills_paper_and_prefixes_reason(tmp_path: Path) -> None:
    conn = seed_db(tmp_path / "ap.db")
    pending(conn, "ok", "BTCUSDT")
    chat = _chat("approve", "แนวโน้มชัด")
    out = run_auto_decide(conn, now_ms=NOW, config=CFG, chat=chat, doctrine_lookup=DOCTRINE)
    assert out["ok"] is True and out["processed"] == 1
    assert [a["id"] for a in out["approved"]] == ["ok"]
    assert out["rejected"] == [] and out["deferred"] == [] and out["apply_failed"] == []
    assert out["doctrine_unavailable"] is False
    row = get_approval(conn, "ok")
    assert row["status"] == "approved"
    assert row["resolve_reason"] == "auto:claude:approve"
    assert get_paper_position(conn, "BTCUSDT") is not None
    assert list_paper_orders(conn)
    review = get_latest_llm_review(conn, "ok")
    assert review["decision"] == "approve" and review["model"] == "test/model"
    assert len(chat.calls) == 1
    assert chat.calls[0]["model"] == "test/model"
    assert chat.calls[0]["messages"][0]["role"] == "system"


def test_reject_path_prefixes_reason_and_no_fill(tmp_path: Path) -> None:
    conn = seed_db(tmp_path / "rj.db")
    pending(conn, "ok", "BTCUSDT")
    out = run_auto_decide(conn, now_ms=NOW, config=CFG, chat=_chat("reject", "ขัดกับ doctrine"),
                          doctrine_lookup=DOCTRINE)
    assert [r["id"] for r in out["rejected"]] == ["ok"]
    row = get_approval(conn, "ok")
    assert row["status"] == "rejected"
    assert row["resolve_reason"] == "auto:claude:reject:ขัดกับ doctrine"
    assert get_paper_position(conn, "BTCUSDT") is None


def test_defer_error_and_garbage_leave_pending(tmp_path: Path) -> None:
    conn = seed_db(tmp_path / "df.db")
    pending(conn, "a", "BTCUSDT", created_at_ms=1)
    pending(conn, "b", "ETHUSDT", created_at_ms=2)
    answers = iter([OpenRouterError("status 503"), {"decision": "yes"}])

    def chat(messages, **kwargs):
        nxt = next(answers)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    out = run_auto_decide(conn, now_ms=NOW, config=CFG, chat=chat, doctrine_lookup=NO_DOCTRINE)
    reasons = {d["id"]: d["reason"] for d in out["deferred"]}
    assert reasons["a"].startswith("auto:claude:error:")
    assert reasons["b"] == "auto:claude:unparseable"
    assert out["doctrine_unavailable"] is True
    assert get_approval(conn, "a")["status"] == "pending"
    assert get_approval(conn, "b")["status"] == "pending"
    assert get_latest_llm_review(conn, "b")["decision"] == "defer"
    assert list_paper_orders(conn) == []


def test_cap_and_missing_key_defer_without_calls(tmp_path: Path) -> None:
    conn = seed_db(tmp_path / "cap.db")
    pending(conn, "a", "BTCUSDT", created_at_ms=1)
    pending(conn, "b", "ETHUSDT", created_at_ms=2)
    chat = _chat("approve")
    out = run_auto_decide(conn, now_ms=NOW, config=AutoDecideConfig(**{**CFG.__dict__, "max_per_run": 1}),
                          chat=chat, doctrine_lookup=DOCTRINE)
    assert [a["id"] for a in out["approved"]] == ["a"]
    assert out["deferred"] == [{"id": "b", "symbol": "ETHUSDT", "reason": "deferred_cap"}]
    assert len(chat.calls) == 1

    conn2 = seed_db(tmp_path / "key.db")
    pending(conn2, "a", "BTCUSDT")
    chat2 = _chat("approve")
    out2 = run_auto_decide(conn2, now_ms=NOW, config=AutoDecideConfig(**{**CFG.__dict__, "api_key": None}),
                           chat=chat2, doctrine_lookup=DOCTRINE)
    assert out2["deferred"] == [{"id": "a", "symbol": "BTCUSDT", "reason": "missing_api_key"}]
    assert chat2.calls == []


def test_filtered_rows_never_reach_claude(tmp_path: Path) -> None:
    conn = seed_db(tmp_path / "fl.db")
    pending(conn, "flat", "BTCUSDT", action="flat", size_pct_equity=0.0)
    chat = _chat("approve")
    out = run_auto_decide(conn, now_ms=NOW, config=CFG, chat=chat, doctrine_lookup=DOCTRINE)
    assert out["filtered_out"] == [{"id": "flat", "symbol": "BTCUSDT", "reason": "action_flat"}]
    assert chat.calls == [] and out["processed"] == 0


def test_apply_failure_recorded_with_prefix(tmp_path: Path) -> None:
    conn = seed_db(tmp_path / "af.db")
    pending(conn, "big", "BTCUSDT", size_pct_equity=50.0)  # 5000 USD > 2000 cap → risk breach
    out = run_auto_decide(conn, now_ms=NOW, config=CFG, chat=_chat("approve"), doctrine_lookup=DOCTRINE)
    assert out["approved"] == []
    assert out["apply_failed"][0]["id"] == "big"
    assert "max_notional" in out["apply_failed"][0]["error"]
    row = get_approval(conn, "big")
    assert row["status"] == "failed"
    assert row["resolve_reason"].startswith("auto:apply_failed:max_notional")


def test_lookup_exception_defers_candidate_and_batch_continues(tmp_path: Path) -> None:
    conn = seed_db(tmp_path / "ex.db")
    pending(conn, "a", "BTCUSDT", created_at_ms=1)
    pending(conn, "b", "ETHUSDT", created_at_ms=2)
    calls: list[str] = []

    def lookup(query: str):
        calls.append(query)
        if len(calls) == 1:
            raise RuntimeError("qdrant exploded")
        return [{"text": "structure first", "source": "d.md", "score": 0.9}]

    out = run_auto_decide(conn, now_ms=NOW, config=CFG, chat=_chat("approve"), doctrine_lookup=lookup)
    assert len(calls) == 2
    reasons = {d["id"]: d["reason"] for d in out["deferred"]}
    assert reasons["a"].startswith("auto:error:RuntimeError:")
    assert [x["id"] for x in out["approved"]] == ["b"]
    assert get_approval(conn, "a")["status"] == "pending"
    assert get_approval(conn, "b")["status"] == "approved"


def test_reject_failure_is_recorded_not_claimed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    conn = seed_db(tmp_path / "rf.db")
    pending(conn, "ok", "BTCUSDT")
    monkeypatch.setattr(
        "jarvise_paper.auto_decide.reject_approval",
        lambda *a, **k: {"ok": False, "approval": None, "error": "approval not pending", "paper_only": True},
    )
    out = run_auto_decide(conn, now_ms=NOW, config=CFG, chat=_chat("reject", "x"), doctrine_lookup=DOCTRINE)
    assert out["rejected"] == []
    assert out["apply_failed"] == [{"id": "ok", "symbol": "BTCUSDT", "reason": "x", "error": "approval not pending"}]


def test_apply_exception_is_recorded_and_batch_continues(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    conn = seed_db(tmp_path / "ae.db")
    pending(conn, "a", "BTCUSDT", created_at_ms=1)
    pending(conn, "b", "ETHUSDT", created_at_ms=2)
    real = approve_approval

    def flaky(conn_, approval_id, **kwargs):
        if approval_id == "a":
            raise sqlite3.OperationalError("database is locked")
        return real(conn_, approval_id, **kwargs)

    monkeypatch.setattr("jarvise_paper.auto_decide.approve_approval", flaky)
    out = run_auto_decide(conn, now_ms=NOW, config=CFG, chat=_chat("approve"), doctrine_lookup=DOCTRINE)
    assert out["apply_failed"][0]["id"] == "a"
    assert out["apply_failed"][0]["error"].startswith("OperationalError: ")
    assert [x["id"] for x in out["approved"]] == ["b"]
