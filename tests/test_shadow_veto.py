"""Shadow veto track, parse, cap, and the pre-registered score."""

from __future__ import annotations

from pathlib import Path

from jarvise.jobs import JOB_TIMEOUTS_S, run_shadow_veto
from jarvise_analyze.replay import bar_range_after
from jarvise_ingest.db import open_db
from jarvise_paper.shadow_track import BAR_MS, build_track, sync_track
from jarvise_paper.shadow_veto import (
    brief_is_safe,
    build_veto_brief,
    parse_verdict,
    request_verdicts,
    run_shadow,
    score_records,
)
from jarvise_paper.stops import evaluate_stop

BAR = BAR_MS["4h"]
T0 = 1_700_000_000_000


def _db(tmp_path: Path):
    return open_db(tmp_path / "shadow.db")


def _member(conn, symbol: str = "BTCUSDT") -> None:
    conn.execute(
        """
        INSERT INTO universe_membership (universe_id, symbol, asset_class, listed_at, delisted_at)
        VALUES ('paper_core', ?, 'crypto', 0, NULL)
        """,
        (symbol,),
    )
    conn.commit()


def _candle(conn, ts: int, *, close: float, high: float, low: float, symbol: str = "BTCUSDT") -> None:
    conn.execute(
        """
        INSERT INTO market_technicals (
            symbol, timestamp, timeframe, open, high, low, close, atr_14, rsi_14, ema_20, ema_200
        ) VALUES (?, ?, '4h', ?, ?, ?, ?, 10, 50, 100, 90)
        """,
        (symbol, ts, close, high, low, close),
    )
    conn.commit()


def _analysis(
    conn,
    ts: int,
    action: str,
    *,
    invalidation: float | None = 90.0,
    symbol: str = "BTCUSDT",
    analysis_id: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO analysis_output (
            analysis_id, timestamp, symbol, timeframe, regime_state, confidence_score,
            action, invalidation_price, size_pct_equity
        ) VALUES (?, ?, ?, '4h', 'trend_up', 0.75, ?, ?, 0.01)
        """,
        (analysis_id or f"{symbol}-{ts}", ts, symbol, action, invalidation),
    )
    conn.commit()


def _ok_chat(messages, *, model, timeout_s):
    return {
        "verdict": "no_objection",
        "reason_category": "none",
        "inputs_cited": ["symbol"],
        "reason": "ไม่ขัดกฎการเทรด",
    }


def test_brief_has_no_secrets_or_order_fields(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _member(conn)
    _candle(conn, T0, close=100, high=101, low=99)
    _analysis(conn, T0, "long")
    rows = build_track(conn, as_of_ms=T0 + BAR, timeframe="4h", universe_id="paper_core")
    brief = build_veto_brief(conn, rows[0], digest_text="context", timeframe="4h")
    assert brief_is_safe(brief)
    assert "size_pct_equity" not in brief
    assert brief["fear_greed"]["role"] == "context"
    conn.close()


def test_strict_parse_abstains() -> None:
    parsed = parse_verdict({"verdict": "buy"}, brief_keys={"symbol"})
    assert parsed["verdict"] == "abstain"
    parsed = parse_verdict("nope", brief_keys=set())
    assert parsed["verdict"] == "abstain"


def test_future_candle_does_not_change_entry(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _member(conn)
    _candle(conn, T0, close=100, high=110, low=99)
    _analysis(conn, T0, "long", invalidation=90)
    as_of = T0 + BAR
    first = build_track(conn, as_of_ms=as_of, timeframe="4h", universe_id="paper_core")
    _candle(conn, T0 + BAR, close=50, high=200, low=1)
    second = build_track(conn, as_of_ms=as_of, timeframe="4h", universe_id="paper_core")
    assert first[0]["entry_px"] == second[0]["entry_px"]
    assert first[0]["resolved_at"] is None
    conn.close()


def test_stop_matches_evaluate_stop(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _member(conn)
    _candle(conn, T0, close=100, high=101, low=99)
    _candle(conn, T0 + BAR, close=95, high=101, low=80)
    _analysis(conn, T0, "long", invalidation=90)
    rows = build_track(conn, as_of_ms=T0 + 2 * BAR, timeframe="4h", universe_id="paper_core")
    low, high = bar_range_after(conn, "BTCUSDT", "4h", after_ts=T0, until_ms=T0 + BAR)
    hit = evaluate_stop(
        {"symbol": "BTCUSDT", "side": "long", "stop_price": 90.0},
        mid=95.0,
        low_since_entry=low,
        high_since_entry=high,
    )
    assert hit is not None
    assert rows[0]["exit_reason"] == "stop"
    assert rows[0]["exit_px"] == hit.fill_mid
    assert rows[0]["resolved_at"] == T0 + BAR
    conn.close()


def test_no_flip_on_the_same_bar(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _member(conn)
    _candle(conn, T0, close=100, high=101, low=99)
    _candle(conn, T0 + BAR, close=110, high=112, low=100)
    _candle(conn, T0 + 2 * BAR, close=111, high=113, low=108)
    _analysis(conn, T0, "long", invalidation=90)
    _analysis(conn, T0 + BAR, "short", invalidation=120)
    _analysis(conn, T0 + 2 * BAR, "short", invalidation=120, analysis_id="BTCUSDT-later")
    rows = build_track(conn, as_of_ms=T0 + 3 * BAR, timeframe="4h", universe_id="paper_core")
    by_id = {row["analysis_id"]: row for row in rows}
    assert by_id["BTCUSDT-" + str(T0)]["exit_reason"] == "opposite"
    assert by_id["BTCUSDT-" + str(T0)]["exit_ts"] == T0 + BAR
    assert "BTCUSDT-" + str(T0 + BAR) not in by_id
    assert by_id["BTCUSDT-later"]["side"] == "short"
    assert by_id["BTCUSDT-later"]["resolved_at"] is None
    conn.close()


def test_outcome_unresolved_until_exit_bar(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _member(conn)
    _candle(conn, T0, close=100, high=101, low=99)
    _candle(conn, T0 + BAR, close=110, high=112, low=100)
    _analysis(conn, T0, "long", invalidation=90)
    _analysis(conn, T0 + BAR, "flat", invalidation=None)
    early = build_track(conn, as_of_ms=T0 + BAR, timeframe="4h", universe_id="paper_core")
    assert early[0]["resolved_at"] is None
    later = build_track(conn, as_of_ms=T0 + 2 * BAR, timeframe="4h", universe_id="paper_core")
    assert later[0]["exit_reason"] == "flat"
    assert later[0]["resolved_at"] == T0 + BAR
    conn.close()


def test_call_cap_and_late_verdicts(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    calls: list[int] = []

    def _chat(messages, *, model, timeout_s):
        calls.append(1)
        return _ok_chat(messages, model=model, timeout_s=timeout_s)

    for index in range(8):
        symbol = f"S{index}USDT"
        _member(conn, symbol)
        _candle(conn, T0, close=100, high=101, low=99, symbol=symbol)
        _analysis(conn, T0, "long", symbol=symbol, analysis_id=f"id-{index}")
    rows = build_track(conn, as_of_ms=T0 + BAR, timeframe="4h", universe_id="paper_core")
    assert len(rows) == 8
    sync_track(conn, rows)
    summary = request_verdicts(
        conn,
        rows,
        now_ms=T0 + BAR + 60_000,
        timeframe="4h",
        directory=tmp_path / "missing-digests",
        chat=_chat,
    )
    assert summary["called"] == 6
    assert summary["waiting"] == 2
    assert len(calls) == 6
    old = [row for row in rows if row["analysis_id"] == "id-0"]
    conn.execute("DELETE FROM paper_shadow_verdicts WHERE analysis_id='id-0'")
    conn.commit()
    late = request_verdicts(
        conn,
        old,
        now_ms=T0 + 3 * BAR,
        timeframe="4h",
        directory=tmp_path / "missing-digests",
        chat=_chat,
    )
    assert late["late"] == 1
    assert len(calls) == 6
    stored = conn.execute(
        "SELECT timing, verdict FROM paper_shadow_verdicts WHERE analysis_id='id-0'"
    ).fetchone()
    assert stored["timing"] == "late"
    scored = score_records(
        [{"verdict": "abstain", "timing": "late", "net_r": -1, "resolved_at": 1, "created_at": 1}],
        now_ms=T0,
    )
    assert scored["n_scored"] == 0
    assert scored["n_late"] == 1
    conn.close()


def test_kill_switch_returns_409_code(monkeypatch) -> None:
    monkeypatch.setattr("jarvise.jobs.kill_switch_engaged", lambda: True)
    code, payload = run_shadow_veto()
    assert code == 3
    assert payload["skipped"] is True


def test_auto_decide_does_not_reference_shadow() -> None:
    text = Path("src/jarvise_paper/auto_decide.py").read_text(encoding="utf-8")
    assert "shadow_track" not in text
    assert "shadow_veto" not in text
    assert "paper_shadow_" not in text


def test_score_pass_and_fail() -> None:
    def row(verdict: str, net_r: float, index: int) -> dict:
        return {
            "verdict": verdict,
            "timing": "on_time",
            "net_r": net_r,
            "resolved_at": index,
            "created_at": 1_000,
        }

    passing = [row("no_objection", 0.2, i) for i in range(20)] + [row("veto", -0.3, i) for i in range(10)]
    good = score_records(passing, now_ms=1_000)
    assert good["passed"] is True
    assert good["win_rate_decides"] is False
    failing = [row("no_objection", 0.1, i) for i in range(20)] + [row("veto", 0.0, i) for i in range(10)]
    bad = score_records(failing, now_ms=1_000)
    assert bad["passed"] is False


def test_run_shadow_does_not_call_model_for_late_history(tmp_path: Path, monkeypatch) -> None:
    conn = _db(tmp_path)
    _member(conn)
    _candle(conn, T0, close=100, high=101, low=99)
    _analysis(conn, T0, "long")
    monkeypatch.setenv("JARVISE_PAPER_UNIVERSE", "paper_core")
    monkeypatch.setenv("JARVISE_PAPER_TIMEFRAME", "4h")

    def _boom(messages, *, model, timeout_s):
        raise AssertionError("model must not be called")

    payload = run_shadow(
        conn,
        now_ms=T0 + 3 * BAR,
        directory=tmp_path / "digests",
        chat=_boom,
        kill_switch_check=lambda: False,
    )
    assert payload["called"] == 0
    assert payload["late"] == 1
    assert JOB_TIMEOUTS_S["shadow_veto"] == 180.0
    conn.close()
