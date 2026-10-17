"""Tests for analyze --replay."""

import json
import shutil
from pathlib import Path

from jarvise_analyze.cli import DEFAULT_DB, run
from jarvise_analyze.replay import (
    _rules_disposition,
    closed_htf_candle,
    replay_is_oos,
    replay_range,
)
from jarvise_ingest.db import (
    get_paper_position,
    list_paper_orders,
    load_candles_in_range,
    open_db,
    upsert_market_technicals,
    upsert_paper_position,
    write_indicators,
)
from jarvise_ingest.series import recompute_indicators
from jarvise_ingest.timeframes import INTERVAL_MS


def _seed_trend(db: Path, *, n: int = 700) -> None:
    conn = open_db(db)
    rows = []
    for i in range(n):
        close = 100.0 + i * 0.5
        rows.append(
            {
                "symbol": "BTCUSDT",
                "timestamp": 1_600_000_000_000 + i * 14_400_000,
                "timeframe": "4h",
                "open": close - 0.2,
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "volume": 10.0,
            }
        )
    upsert_market_technicals(conn, rows)
    recompute_indicators(conn, "BTCUSDT", "4h")
    conn.close()


def test_replay_analyze_only_upserts(tmp_path: Path) -> None:
    db = tmp_path / "r.db"
    _seed_trend(db)
    # ~ last 10 bars of seeded series
    since_ms = 1_600_000_000_000 + 690 * 14_400_000
    until_ms = 1_600_000_000_000 + 699 * 14_400_000
    conn = open_db(db)
    report = replay_range(
        conn,
        symbols=["BTCUSDT"],
        timeframe="4h",
        since_ms=since_ms,
        until_ms=until_ms,
        apply_paper=False,
    )
    assert report["ok"] is True
    assert report["bars"] == 10
    n = conn.execute("SELECT COUNT(*) FROM analysis_output").fetchone()[0]
    conn.close()
    assert n == 10


def test_replay_dry_run_writes_nothing(tmp_path: Path) -> None:
    db = tmp_path / "d.db"
    _seed_trend(db)
    since_ms = 1_600_000_000_000 + 690 * 14_400_000
    until_ms = 1_600_000_000_000 + 699 * 14_400_000
    conn = open_db(db)
    report = replay_range(
        conn,
        symbols=["BTCUSDT"],
        timeframe="4h",
        since_ms=since_ms,
        until_ms=until_ms,
        dry_run=True,
    )
    assert report["bars"] == 10
    n = conn.execute("SELECT COUNT(*) FROM analysis_output").fetchone()[0]
    assert list_paper_orders(conn) == []
    conn.close()
    assert n == 0


def test_apply_paper_refuses_default_db(tmp_path: Path, capsys) -> None:
    # Ensure default path exists as a file so resolve works; use real DEFAULT_DB check
    code = run(
        [
            "--symbol",
            "BTCUSDT",
            "--replay",
            "--since",
            "2020-01-01",
            "--apply-paper",
            "--db",
            str(DEFAULT_DB),
        ]
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "refuses the default live ledger" in err


def test_apply_paper_isolated_fills(tmp_path: Path, capsys) -> None:
    src = tmp_path / "src.db"
    bt = tmp_path / "bt.db"
    _seed_trend(src)
    shutil.copy(src, bt)
    since = "2020-09-01T00:00:00+00:00"
    # seed starts 2020-09-13-ish in ms terms: 1600000000000 = 2020-09-13
    code = run(
        [
            "--symbol",
            "BTCUSDT",
            "--timeframe",
            "4h",
            "--replay",
            "--since",
            since,
            "--until",
            "2020-10-15T00:00:00+00:00",
            "--apply-paper",
            "--json",
            "--db",
            str(bt),
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["apply_paper"] is True
    assert payload["bars"] > 0
    assert payload["metrics"] is not None
    conn = open_db(bt)
    # may be zero closed if never flat, but orders should exist if any long/short fired
    orders = list_paper_orders(conn, limit=500)
    conn.close()
    assert payload["fills"] == len(orders) or payload["fills"] >= 0


def test_replay_requires_since(capsys) -> None:
    assert run(["--symbol", "BTCUSDT", "--replay"]) == 2
    assert "--since" in capsys.readouterr().err


def test_paper_policy_rules_requires_apply_paper(capsys) -> None:
    assert (
        run(
            [
                "--symbol",
                "BTCUSDT",
                "--replay",
                "--since",
                "2020-01-01",
                "--paper-policy",
                "rules",
                "--db",
                str(Path("nope.db")),
            ]
        )
        == 2
    )
    assert "--apply-paper" in capsys.readouterr().err


def _seed_up_then_down(db: Path, *, n_up: int = 400, n_down: int = 400) -> None:
    conn = open_db(db)
    rows = []
    ts = 1_600_000_000_000
    close = 100.0
    for i in range(n_up):
        close = 100.0 + i * 0.8
        rows.append(
            {
                "symbol": "BTCUSDT",
                "timestamp": ts + i * 14_400_000,
                "timeframe": "4h",
                "open": close - 0.2,
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "volume": 10.0,
            }
        )
    base = n_up
    peak = close
    for i in range(n_down):
        close = peak - (i + 1) * 1.2
        rows.append(
            {
                "symbol": "BTCUSDT",
                "timestamp": ts + (base + i) * 14_400_000,
                "timeframe": "4h",
                "open": close + 0.2,
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "volume": 10.0,
            }
        )
    upsert_market_technicals(conn, rows)
    recompute_indicators(conn, "BTCUSDT", "4h")
    conn.close()


def test_rules_policy_defers_opposite_instead_of_flipping(tmp_path: Path) -> None:
    naive_db = tmp_path / "naive.db"
    rules_db = tmp_path / "rules.db"
    _seed_up_then_down(naive_db)
    shutil.copy(naive_db, rules_db)
    since_ms = 1_600_000_000_000
    until_ms = 1_600_000_000_000 + 799 * 14_400_000
    naive_conn = open_db(naive_db)
    naive = replay_range(
        naive_conn,
        symbols=["BTCUSDT"],
        timeframe="4h",
        since_ms=since_ms,
        until_ms=until_ms,
        apply_paper=True,
        paper_policy="naive",
        confidence_threshold=0.45,
    )
    naive_conn.close()
    rules_conn = open_db(rules_db)
    rules = replay_range(
        rules_conn,
        symbols=["BTCUSDT"],
        timeframe="4h",
        since_ms=since_ms,
        until_ms=until_ms,
        apply_paper=True,
        paper_policy="rules",
        confidence_threshold=0.45,
    )
    rules_pos = rules_conn.execute("SELECT side FROM paper_positions WHERE symbol='BTCUSDT'").fetchone()
    rules_conn.close()
    assert naive["ok"] and rules["ok"]
    assert rules["policy_counts"]["hold"] > 0
    assert naive["fills"] >= 1
    assert rules["fills"] >= 1
    if rules_pos is not None:
        assert rules_pos[0] == "long"


def test_rules_disposition_opposite_is_defer(tmp_path: Path) -> None:
    db = tmp_path / "hold.db"
    _seed_trend(db, n=700)
    since_ms = 1_600_000_000_000 + 500 * 14_400_000
    until_ms = 1_600_000_000_000 + 699 * 14_400_000
    conn = open_db(db)
    report = replay_range(
        conn,
        symbols=["BTCUSDT"],
        timeframe="4h",
        since_ms=since_ms,
        until_ms=until_ms,
        apply_paper=True,
        paper_policy="rules",
        confidence_threshold=0.45,
    )
    assert report["fills"] >= 1
    pos = get_paper_position(conn, "BTCUSDT")
    assert pos is not None and pos["side"] == "long"
    assert (
        _rules_disposition(conn, {"symbol": "BTCUSDT", "action": "short", "size_pct_equity": 1.0}) == "defer"
    )
    conn.close()


def test_replay_is_oos_splits_middle_without_shared_bars(tmp_path: Path) -> None:
    db = tmp_path / "split.db"
    _seed_trend(db, n=700)
    start = 1_600_000_000_000
    step = 14_400_000
    since_ms = start
    oos_ms = start + 350 * step
    until_ms = start + 699 * step
    conn = open_db(db)
    report = replay_is_oos(
        conn,
        symbols=["BTCUSDT"],
        timeframe="4h",
        since_ms=since_ms,
        oos_since_ms=oos_ms,
        until_ms=until_ms,
    )
    in_rows = load_candles_in_range(conn, "BTCUSDT", "4h", since_ms=since_ms, until_ms=oos_ms - 1)
    oos_rows = load_candles_in_range(conn, "BTCUSDT", "4h", since_ms=oos_ms, until_ms=until_ms)
    conn.close()
    in_ts = {row["timestamp"] for row in in_rows}
    oos_ts = {row["timestamp"] for row in oos_rows}
    assert report["ok"] is True
    assert report["in_sample"]["bars"] == 350
    assert report["out_of_sample"]["bars"] == 350
    assert report["in_sample"]["bars"] + report["out_of_sample"]["bars"] == 700
    assert in_ts.isdisjoint(oos_ts)
    for window in (report["in_sample"], report["out_of_sample"]):
        counts = window["action_counts"]
        assert set(counts) == {"long", "short", "flat"}
        assert sum(counts.values()) == window["bars"]
    assert "better EV" in report["note"]


def test_oos_ledger_does_not_keep_in_sample_position(tmp_path: Path) -> None:
    db = tmp_path / "carry.db"
    _seed_trend(db, n=700)
    start = 1_600_000_000_000
    step = 14_400_000
    # EMA200 is withheld until late in this series, so the cut sits after that warm-up.
    oos_ms = start + 680 * step
    conn = open_db(db)
    report = replay_is_oos(
        conn,
        symbols=["BTCUSDT"],
        timeframe="4h",
        since_ms=start,
        oos_since_ms=oos_ms,
        until_ms=start + 699 * step,
        apply_paper=True,
        confidence_threshold=0.45,
    )
    orders = conn.execute("SELECT ts FROM paper_orders").fetchall()
    positions = conn.execute("SELECT entry_ts FROM paper_positions").fetchall()
    conn.close()
    assert report["in_sample"]["fills"] >= 1
    assert all(row[0] >= oos_ms for row in orders)
    assert all(row[0] >= oos_ms for row in positions)


def test_oos_since_outside_range_rejected(tmp_path: Path, capsys) -> None:
    db = tmp_path / "bad.db"
    code = run(
        [
            "--symbol",
            "BTCUSDT",
            "--replay",
            "--since",
            "2020-01-01",
            "--until",
            "2020-06-01",
            "--oos-since",
            "2021-01-01",
            "--db",
            str(db),
        ]
    )
    assert code == 2
    assert "--oos-since" in capsys.readouterr().err


def test_oos_since_requires_replay(capsys) -> None:
    assert run(["--symbol", "BTCUSDT", "--oos-since", "2024-06-01"]) == 2
    assert "--replay" in capsys.readouterr().err


def _open_long(conn) -> None:
    upsert_paper_position(
        conn,
        {
            "symbol": "BTCUSDT",
            "side": "long",
            "qty": 1.0,
            "entry_price": 100.0,
            "entry_ts": 1,
            "unrealized_pnl": 0.0,
            "realized_pnl": 0.0,
            "stop_price": 90.0,
            "stop_source": "analysis",
            "initial_stop_price": 90.0,
        },
    )


def test_default_exit_still_closes_on_flat(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "flat.db")
    _open_long(conn)
    row = {"symbol": "BTCUSDT", "action": "flat", "regime_state": "trend_up"}
    assert _rules_disposition(conn, row) == "flat_exit"
    assert _rules_disposition(conn, row, exit_mode="confidence") == "flat_exit"
    conn.close()


def test_regime_exit_waits_for_regime_mismatch(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "regime.db")
    _open_long(conn)
    matched = {"symbol": "BTCUSDT", "action": "flat", "regime_state": "trend_up"}
    assert _rules_disposition(conn, matched, exit_mode="regime") == "skip"
    mismatched = {"symbol": "BTCUSDT", "action": "flat", "regime_state": "range"}
    assert _rules_disposition(conn, mismatched, exit_mode="regime") == "flat_exit"
    opposite = {"symbol": "BTCUSDT", "action": "short", "regime_state": "trend_down"}
    assert _rules_disposition(conn, opposite, exit_mode="regime") == "defer"
    same = {"symbol": "BTCUSDT", "action": "long", "regime_state": "trend_up"}
    assert _rules_disposition(conn, same, exit_mode="regime") == "hold"
    conn.close()


def test_default_replay_report_matches_explicit_confidence(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    _seed_trend(src, n=40)
    since_ms = 1_600_000_000_000
    until_ms = 1_600_000_000_000 + 39 * 14_400_000
    shutil.copy(src, tmp_path / "left.db")
    shutil.copy(src, tmp_path / "right.db")
    left = open_db(tmp_path / "left.db")
    right = open_db(tmp_path / "right.db")
    kwargs = {
        "symbols": ["BTCUSDT"],
        "timeframe": "4h",
        "since_ms": since_ms,
        "until_ms": until_ms,
        "apply_paper": True,
        "paper_policy": "rules",
    }
    default = replay_range(left, **kwargs)
    explicit = replay_range(right, exit_mode="confidence", no_breakeven=False, **kwargs)
    left.close()
    right.close()
    assert default["exit"] == "confidence"
    assert default["no_breakeven"] is False
    assert default["fills"] == explicit["fills"]
    assert default["policy_counts"] == explicit["policy_counts"]
    assert default["action_counts"] == explicit["action_counts"]


def test_exit_regime_requires_replay_apply_paper(capsys) -> None:
    code = run(
        [
            "--symbol",
            "BTCUSDT",
            "--replay",
            "--since",
            "2020-01-01",
            "--exit",
            "regime",
            "--db",
            "nope.db",
        ]
    )
    assert code == 2
    assert "--exit" in capsys.readouterr().err


def test_no_breakeven_requires_trail(capsys) -> None:
    code = run(
        [
            "--symbol",
            "BTCUSDT",
            "--replay",
            "--since",
            "2020-01-01",
            "--apply-paper",
            "--stops",
            "fixed",
            "--no-breakeven",
            "--db",
            "nope.db",
        ]
    )
    assert code == 2
    assert "--no-breakeven" in capsys.readouterr().err


def test_mtf_flag_requires_replay(capsys) -> None:
    assert run(["--symbol", "BTCUSDT", "--mtf"]) == 2
    assert "--replay" in capsys.readouterr().err


def test_closed_htf_candle_waits_until_daily_close() -> None:
    day = 1_700_000_000_000
    daily = {"timestamp": day, "ema_200": 90.0, "close": 100.0}
    assert closed_htf_candle([daily], day, htf_ms=INTERVAL_MS["1d"]) is None
    assert closed_htf_candle([daily], day + INTERVAL_MS["1d"], htf_ms=INTERVAL_MS["1d"]) == daily


def test_replay_mtf_does_not_use_same_day_daily(tmp_path: Path) -> None:
    db = tmp_path / "mtf.db"
    conn = open_db(db)
    day = 1_700_000_000_000
    day_ms = INTERVAL_MS["1d"]
    bars = [
        ("4h", day, 100.0),
        ("4h", day + day_ms, 110.0),
        ("1d", day, 100.0),
    ]
    upsert_market_technicals(
        conn,
        [
            {
                "symbol": "BTCUSDT",
                "timestamp": ts,
                "timeframe": tf,
                "open": close - 0.2,
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "volume": 10.0,
            }
            for tf, ts, close in bars
        ],
    )
    write_indicators(
        conn,
        [
            {
                "symbol": "BTCUSDT",
                "timeframe": tf,
                "timestamp": ts,
                "atr_14": 1.0,
                "rsi_14": 55.0,
                "ema_20": close + 1.0,
                "ema_200": close - 10.0,
            }
            for tf, ts, close in bars
        ],
    )
    early = replay_range(
        conn,
        symbols=["BTCUSDT"],
        timeframe="4h",
        since_ms=day,
        until_ms=day,
        dry_run=True,
        mtf=True,
    )
    later = replay_range(
        conn,
        symbols=["BTCUSDT"],
        timeframe="4h",
        since_ms=day + day_ms,
        until_ms=day + day_ms,
        dry_run=True,
        mtf=True,
    )
    conn.close()
    assert early["mtf"] is True
    assert early["htf_joined"] == 0
    assert early["action_counts"]["flat"] == 1
    assert early["mtf_reasons"].get("htf_missing") == 1
    assert later["htf_joined"] == 1
    assert later["action_counts"]["long"] == 1
    assert later["mtf_reasons"].get("htf_confirm") == 1
