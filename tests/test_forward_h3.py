"""Forward H3 daily replay job — isolated copy, frozen args, pass rule."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from jarvise_ingest.db import open_db, remove_universe_symbol
from jarvise_ingest.universe import PAPER_LIQUID, seed_named_universe
from jarvise_paper.forward_h3 import (
    H3_SINCE,
    H3_SINCE_MS,
    assert_copy_not_live,
    build_replay_argv,
    h3_copy_path,
    h3_symbols_path,
    passed,
    run_forward_h3_job,
    window_closed,
)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _live_tree(tmp_path: Path) -> Path:
    analytics = tmp_path / "analytics"
    analytics.mkdir(parents=True)
    live = analytics / "jarvise.db"
    conn = open_db(live)
    try:
        seed_named_universe(conn, PAPER_LIQUID)
    finally:
        conn.close()
    return live


def test_copy_path_equal_to_live_refused(tmp_path: Path) -> None:
    live = tmp_path / "jarvise.db"
    live.write_bytes(b"x")
    with pytest.raises(ValueError, match="must not equal"):
        assert_copy_not_live(live, live)


def test_build_replay_argv_matches_frozen_flags(tmp_path: Path) -> None:
    copy = tmp_path / "h3.db"
    argv = build_replay_argv(
        copy_db=copy,
        symbols=["BTCUSDT", "ETHUSDT"],
        until_iso="2026-10-26T12:00:00Z",
    )
    assert "--replay" in argv
    assert "--apply-paper" in argv
    assert argv[argv.index("--strategy") + 1] == "donchian_long"
    assert argv[argv.index("--stops") + 1] == "trail"
    assert argv[argv.index("--timeframe") + 1] == "1d"
    assert argv[argv.index("--paper-policy") + 1] == "rules"
    assert argv[argv.index("--since") + 1] == H3_SINCE
    assert argv[argv.index("--until") + 1] == "2026-10-26T12:00:00Z"
    assert argv[argv.index("--db") + 1] == str(copy)
    assert "--json" in argv
    assert "--mtf" not in argv
    assert "--no-breakeven" not in argv
    assert "--exit" not in argv
    assert argv.count("--symbol") == 2
    assert "BTCUSDT" in argv
    assert "ETHUSDT" in argv


def test_second_run_keeps_first_symbol_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    live = _live_tree(tmp_path)
    monkeypatch.setenv("JARVISE_DB", str(live))
    first_ms = H3_SINCE_MS + 86_400_000  # day after window opens
    symbols_path = h3_symbols_path(live)

    def stub_replay(_argv: list[str]) -> dict:
        return {"ok": True, "metrics": {"closed_trades": 0, "expected_value_ev": None}}

    payload1 = run_forward_h3_job(live_db=live, now_ms=first_ms, run_replay=stub_replay)
    assert payload1["ok"] is True
    frozen = json.loads(symbols_path.read_text(encoding="utf-8"))
    assert frozen["symbols"]
    original = list(frozen["symbols"])

    # Mutate live universe membership so a re-freeze would differ.
    conn = open_db(live)
    try:
        assert remove_universe_symbol(
            conn,
            universe_id=PAPER_LIQUID,
            symbol=original[0],
            now_ms=first_ms + 1,
        )
    finally:
        conn.close()

    payload2 = run_forward_h3_job(live_db=live, now_ms=first_ms + 86_400_000, run_replay=stub_replay)
    assert payload2["ok"] is True
    again = json.loads(symbols_path.read_text(encoding="utf-8"))
    assert again["symbols"] == original
    assert payload2["symbols"] == original


def test_window_and_pass_rules() -> None:
    assert window_closed(closed_trades=19, days=119.0) is False
    assert passed(closed=False, closed_trades=19, expected_value_ev=-0.01) is None

    assert window_closed(closed_trades=20, days=10.0) is True
    assert passed(closed=True, closed_trades=20, expected_value_ev=-0.01) is False

    assert window_closed(closed_trades=20, days=10.0) is True
    assert passed(closed=True, closed_trades=20, expected_value_ev=0.0) is True

    assert window_closed(closed_trades=5, days=120.0) is True
    assert passed(closed=True, closed_trades=5, expected_value_ev=0.1) is False


def test_before_window_skips_replay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    live = _live_tree(tmp_path)
    monkeypatch.setenv("JARVISE_DB", str(live))
    called: list[list[str]] = []

    def stub_replay(argv: list[str]) -> dict:
        called.append(argv)
        raise AssertionError("replay must not run before H3_SINCE")

    before_ms = H3_SINCE_MS - 1
    payload = run_forward_h3_job(live_db=live, now_ms=before_ms, run_replay=stub_replay)
    assert payload["ok"] is True
    assert payload["closed_trades"] == 0
    assert payload["replay_skipped"] == "before_window"
    assert payload["replay_ok"] is True
    assert called == []


def test_live_db_unchanged_after_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    live = _live_tree(tmp_path)
    monkeypatch.setenv("JARVISE_DB", str(live))
    before = _sha256(live)

    def stub_replay(argv: list[str]) -> dict:
        assert "--db" in argv
        copy = Path(argv[argv.index("--db") + 1])
        assert copy.resolve() != live.resolve()
        assert copy.exists()
        return {
            "ok": True,
            "metrics": {
                "closed_trades": 0,
                "expected_value_ev": None,
                "win_rate": None,
                "max_drawdown_pct": None,
            },
        }

    payload = run_forward_h3_job(live_db=live, now_ms=H3_SINCE_MS + 86_400_000, run_replay=stub_replay)
    assert payload["ok"] is True
    assert _sha256(live) == before
    assert h3_copy_path(live).exists()
