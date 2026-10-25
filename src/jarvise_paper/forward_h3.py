"""Forward hypothesis H3: daily donchian_long + trail on an isolated ledger copy.

Research measurement only. Never writes the live paper ledger.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jarvise_ingest.db import list_paper_positions, load_candles_in_range, open_db, universe_as_of
from jarvise_ingest.universe import PAPER_LIQUID

H3_SINCE = "2026-10-26"
H3_SINCE_MS = int(datetime(2026, 10, 26, tzinfo=UTC).timestamp() * 1000)
H3_WINDOW_DAYS = 120
H3_MIN_CLOSED = 20
H3_TIMEFRAME = "1d"
REDIS_KEY = "jarvise:forward_h3:last"


def forward_dir(live_db: Path) -> Path:
    """``data/forward`` beside ``data/analytics`` when live_db is ``.../analytics/jarvise.db``."""
    return live_db.resolve().parent.parent / "forward"


def h3_copy_path(live_db: Path) -> Path:
    return forward_dir(live_db) / "h3.db"


def h3_symbols_path(live_db: Path) -> Path:
    return forward_dir(live_db) / "h3_symbols.json"


def h3_latest_path(live_db: Path) -> Path:
    return forward_dir(live_db) / "h3_latest.json"


def resolve_live_db() -> Path:
    return Path(os.environ.get("JARVISE_DB") or "data/analytics/jarvise.db").resolve()


def assert_copy_not_live(live_db: Path, copy_db: Path) -> None:
    if copy_db.resolve() == live_db.resolve():
        raise ValueError("forward-h3 copy path must not equal JARVISE_DB")


def online_backup(live_db: Path, copy_db: Path) -> None:
    """SQLite online backup into ``copy_db``. Never a plain file copy of a WAL DB."""
    assert_copy_not_live(live_db, copy_db)
    copy_db.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        side = Path(str(copy_db) + suffix) if suffix else copy_db
        if side.exists():
            side.unlink()
    src = sqlite3.connect(f"file:{live_db.resolve().as_posix()}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(copy_db))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def load_or_freeze_symbols(live_db: Path, *, now_ms: int) -> list[str]:
    path = h3_symbols_path(live_db)
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
        symbols = [str(s).upper() for s in (raw.get("symbols") or [])]
        if not symbols:
            raise ValueError("h3_symbols.json has no symbols")
        return symbols
    conn = open_db(live_db)
    try:
        symbols = [str(s).upper() for s in universe_as_of(conn, PAPER_LIQUID, now_ms)]
    finally:
        conn.close()
    if not symbols:
        raise ValueError("paper_liquid membership is empty at freeze time")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"symbols": symbols, "universe": PAPER_LIQUID, "frozen_at_ms": now_ms},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return symbols


def build_replay_argv(*, copy_db: Path, symbols: list[str], until_iso: str) -> list[str]:
    cmd = [
        sys.executable,
        "-m",
        "jarvise",
        "analyze",
        "--replay",
        "--apply-paper",
        "--strategy",
        "donchian_long",
        "--stops",
        "trail",
        "--timeframe",
        H3_TIMEFRAME,
        "--paper-policy",
        "rules",
        "--since",
        H3_SINCE,
        "--until",
        until_iso,
        "--db",
        str(copy_db),
        "--json",
    ]
    for symbol in symbols:
        cmd.extend(["--symbol", symbol])
    return cmd


def days_elapsed(*, now_ms: int) -> float:
    return max(0.0, (int(now_ms) - H3_SINCE_MS) / 86_400_000.0)


def window_closed(*, closed_trades: int, days: float) -> bool:
    return int(closed_trades) >= H3_MIN_CLOSED or float(days) >= float(H3_WINDOW_DAYS)


def passed(*, closed: bool, closed_trades: int, expected_value_ev: float | None) -> bool | None:
    if not closed:
        return None
    if int(closed_trades) < H3_MIN_CLOSED:
        return False
    if expected_value_ev is None:
        return False
    return float(expected_value_ev) >= 0.0


def equal_weight_buy_and_hold(conn: Any, symbols: list[str], *, until_ms: int) -> float | None:
    """Mean simple return of 1d closes from the first bar on/after H3_SINCE through until_ms."""
    rets: list[float] = []
    for symbol in symbols:
        bars = load_candles_in_range(
            conn,
            symbol,
            H3_TIMEFRAME,
            since_ms=H3_SINCE_MS,
            until_ms=int(until_ms),
        )
        if len(bars) < 2:
            continue
        first = bars[0].get("close")
        last = bars[-1].get("close")
        if first is None or last is None:
            continue
        f = float(first)
        if f == 0:
            continue
        rets.append(float(last) / f - 1.0)
    if not rets:
        return None
    return round(sum(rets) / len(rets), 8)


def git_sha() -> str | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    sha = (proc.stdout or "").strip()
    return sha or None


def read_latest(live_db: Path | None = None) -> dict[str, Any]:
    path = h3_latest_path(live_db or resolve_live_db())
    if not path.exists():
        return {"ok": False, "error": "h3_latest.json missing", "paper_only": True}
    return json.loads(path.read_text(encoding="utf-8"))


def run_forward_h3_job(
    *,
    live_db: Path | None = None,
    now_ms: int | None = None,
    run_replay: Any | None = None,
) -> dict[str, Any]:
    """Backup live DB, freeze symbols, replay frozen args, write latest JSON."""
    live = (live_db or resolve_live_db()).resolve()
    copy = h3_copy_path(live)
    assert_copy_not_live(live, copy)
    ts = int(now_ms if now_ms is not None else time.time() * 1000)
    until_dt = datetime.fromtimestamp(ts / 1000.0, tz=UTC)
    until_iso = until_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    if not live.exists():
        missing = {
            "ok": False,
            "error": "no_database",
            "paper_only": True,
            "at_ms": ts,
        }
        _write_latest(live, missing)
        return missing

    online_backup(live, copy)
    symbols = load_or_freeze_symbols(live, now_ms=ts)
    days = days_elapsed(now_ms=ts)

    # Before the forward window opens, publish zeros without calling replay
    # (CLI refuses --since after --until).
    if ts < H3_SINCE_MS:
        payload = {
            "ok": True,
            "paper_only": True,
            "at_ms": ts,
            "git_sha": git_sha(),
            "symbols": symbols,
            "closed_trades": 0,
            "expected_value_ev": None,
            "win_rate": None,
            "max_drawdown_pct": None,
            "open_positions": 0,
            "days_elapsed": round(days, 6),
            "window_closed": False,
            "passed": None,
            "extension_requires_owner": True,
            "buy_and_hold_equal_weight": None,
            "since": H3_SINCE,
            "until": until_iso,
            "copy_db": str(copy),
            "replay_ok": True,
            "replay_skipped": "before_window",
        }
        _write_latest(live, payload)
        return payload

    argv = build_replay_argv(copy_db=copy, symbols=symbols, until_iso=until_iso)
    if run_replay is None:
        proc = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=280)
        replay_out = _parse_replay_stdout(proc)
    else:
        replay_out = run_replay(argv)

    metrics = replay_out.get("metrics") or {}
    closed_trades = int(metrics.get("closed_trades") or 0)
    ev = metrics.get("expected_value_ev")
    closed = window_closed(closed_trades=closed_trades, days=days)
    pass_flag = passed(closed=closed, closed_trades=closed_trades, expected_value_ev=ev)

    copy_conn = open_db(copy)
    try:
        open_n = len(list_paper_positions(copy_conn))
        bh = equal_weight_buy_and_hold(copy_conn, symbols, until_ms=ts)
    finally:
        copy_conn.close()

    payload = {
        "ok": True,
        "paper_only": True,
        "at_ms": ts,
        "git_sha": git_sha(),
        "symbols": symbols,
        "closed_trades": closed_trades,
        "expected_value_ev": ev,
        "win_rate": metrics.get("win_rate"),
        "max_drawdown_pct": metrics.get("max_drawdown_pct"),
        "open_positions": open_n,
        "days_elapsed": round(days, 6),
        "window_closed": closed,
        "passed": pass_flag,
        "extension_requires_owner": True,
        "buy_and_hold_equal_weight": bh,
        "since": H3_SINCE,
        "until": until_iso,
        "copy_db": str(copy),
        "replay_ok": bool(replay_out.get("ok")),
        "replay_error": replay_out.get("error"),
    }
    if replay_out.get("stderr"):
        payload["replay_stderr"] = str(replay_out["stderr"])[-1000:]
    _write_latest(live, payload)
    return payload


def _write_latest(live_db: Path, payload: dict[str, Any]) -> None:
    path = h3_latest_path(live_db)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def _parse_replay_stdout(proc: Any) -> dict[str, Any]:
    text = (getattr(proc, "stdout", None) or "").strip()
    if text:
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return {
        "ok": getattr(proc, "returncode", 1) == 0,
        "error": "replay_json_parse_failed",
        "stderr": (getattr(proc, "stderr", None) or "")[-2000:],
        "metrics": {},
    }
