"""Explore-list membership, separate candle store, and analysis. Sends nothing."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from jarvise_analyze.engine import (
    CONFIDENCE_THRESHOLD,
    analyze_mtf,
    analyze_snapshot,
    htf_timeframe,
    mtf_enabled,
)
from jarvise_assist import ASSIST_SYMBOLS, ASSIST_TIMEFRAME
from jarvise_assist.keyboard import explore_remove_keyboard
from jarvise_assist.store import (
    delete_explore_symbol,
    insert_explore_symbol,
    list_explore_symbols,
    now_ms,
)
from jarvise_ingest.db import (
    load_latest_analysis,
    load_latest_candle,
    open_db,
    upsert_analysis_output,
    upsert_market_technicals,
)
from jarvise_ingest.providers.binance_klines import fetch_klines
from jarvise_ingest.series import recompute_indicators
from jarvise_ingest.timeframes import INTERVAL_MS

logger = logging.getLogger(__name__)

EXPLORE_LIMIT = 10
SEED_LIMIT = 1000
REFRESH_LIMIT = 200
EXPLORE_BUDGET_S = 60.0
KLINE_TIMEOUT_S = 10.0

REASON_FORMAT = "พิมพ์สัญลักษณ์เช่น LINKUSDT"
REASON_CORE = "เหรียญนี้อยู่ในชุดหลักแล้ว"
REASON_FULL = "รายการทดลองเต็ม 10 เหรียญแล้ว"
REASON_DUP = "อยู่ในรายการทดลองแล้ว"
REASON_UNKNOWN = "ไม่พบสัญลักษณ์นี้บน Binance USDⓈ-M"
REASON_CONTRACT = "ต้องเป็น USDⓈ-M PERPETUAL สถานะ TRADING คู่ USDT"
REASON_KLINE = "แหล่งเทียนหลักไม่มีข้อมูลเหรียญนี้"

LookupFn = Callable[[str], dict[str, Any] | None]
FetchFn = Callable[[str, str, int], list[dict[str, Any]]]


def main_db_path() -> Path:
    raw = (os.environ.get("JARVISE_DB") or "").strip()
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parents[2] / "data" / "analytics" / "jarvise.db"


def explore_db_path(main_db: Path) -> Path:
    return main_db.parent / "assist-explore.db"


def connection_path(conn: sqlite3.Connection) -> Path:
    for _seq, name, file in conn.execute("PRAGMA database_list"):
        if name == "main" and file:
            return Path(file)
    raise RuntimeError("sqlite connection has no file path")


def normalize_explore_symbol(raw: str) -> str | None:
    text = (raw or "").strip().upper().replace("/", "").replace("-", "").replace(" ", "")
    if not text.endswith("USDT"):
        return None
    base = text[:-4]
    if not base.isalnum():
        return None
    return text


def contract_rejection(row: dict[str, Any] | None) -> str | None:
    if row is None:
        return REASON_UNKNOWN
    contract = str(row.get("contractType") or "").upper()
    quote = str(row.get("quoteAsset") or "").upper()
    status = str(row.get("status") or "").upper()
    if contract == "PERPETUAL" and quote == "USDT" and status == "TRADING":
        return None
    return REASON_CONTRACT


def render_explore_message(conn: sqlite3.Connection) -> tuple[str, dict[str, Any] | None]:
    symbols = list_explore_symbols(conn)
    count = len(symbols)
    if not symbols:
        text = f"🧪 เหรียญทดลอง ({count}/{EXPLORE_LIMIT})\nยังไม่มี\n{REASON_FORMAT} เพื่อเพิ่ม"
        return text, None
    lines = "\n".join(symbols)
    text = f"🧪 เหรียญทดลอง ({count}/{EXPLORE_LIMIT})\n{lines}\n{REASON_FORMAT} เพื่อเพิ่ม"
    return text, explore_remove_keyboard(symbols)


def _timeframes() -> list[str]:
    frames = [ASSIST_TIMEFRAME]
    htf = htf_timeframe()
    if mtf_enabled() and htf != ASSIST_TIMEFRAME:
        frames.append(htf)
    return frames


def _store_bars(conn: sqlite3.Connection, candles: list[dict[str, Any]]) -> None:
    if not candles:
        raise RuntimeError("no closed candles")
    upsert_market_technicals(conn, candles)
    symbol = str(candles[0]["symbol"]).upper()
    timeframe = str(candles[0]["timeframe"])
    recompute_indicators(conn, symbol, timeframe)


def _bind_fetch(fetch: FetchFn) -> tuple[FetchFn, httpx.Client | None]:
    """Use a 10s client for the real kline source. Injected fetches stay untouched."""
    if fetch is not fetch_klines:
        return fetch, None
    client = httpx.Client(timeout=KLINE_TIMEOUT_S)

    def bound(symbol: str, interval: str, limit: int) -> list[dict[str, Any]]:
        return fetch_klines(symbol, interval, limit, client=client)

    return bound, client


def _close_client(client: httpx.Client | None) -> None:
    if client is not None:
        client.close()


def _fetch_limit(
    conn: sqlite3.Connection,
    symbol: str,
    timeframe: str,
    now_ms_value: int,
) -> int:
    candle = load_latest_candle(conn, symbol, timeframe)
    if candle is None:
        return SEED_LIMIT
    newest = int(candle["timestamp"])
    horizon = int(now_ms_value) - REFRESH_LIMIT * INTERVAL_MS[timeframe]
    if newest < horizon:
        return SEED_LIMIT
    return REFRESH_LIMIT


def _delete_symbol_rows(conn: sqlite3.Connection, symbol: str) -> None:
    conn.execute("DELETE FROM market_technicals WHERE symbol=?", (symbol,))
    conn.execute("DELETE FROM analysis_output WHERE symbol=?", (symbol,))
    conn.commit()


def _fetch_into(
    conn: sqlite3.Connection,
    symbol: str,
    timeframe: str,
    limit: int,
    fetch: FetchFn,
) -> None:
    _store_bars(conn, fetch(symbol, timeframe, limit))


def seed_explore_symbol(
    explore_conn: sqlite3.Connection,
    symbol: str,
    *,
    fetch: FetchFn = fetch_klines,
) -> None:
    for timeframe in _timeframes():
        _fetch_into(explore_conn, symbol, timeframe, SEED_LIMIT, fetch)


def _kline_serves(symbol: str, fetch: FetchFn) -> bool:
    try:
        candles = fetch(symbol, ASSIST_TIMEFRAME, 1)
    except Exception:  # noqa: BLE001 — a missing symbol is a rejection, not a crash
        logger.warning("assist explore kline probe failed for %s", symbol)
        return False
    return bool(candles)


def submit_explore_symbol(
    conn: sqlite3.Connection,
    raw: str,
    *,
    lookup: LookupFn,
    fetch: FetchFn = fetch_klines,
    now_ms_value: int | None = None,
    explore_conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Validate, insert, and seed. A failed seed removes the row."""
    symbol = normalize_explore_symbol(raw)
    if symbol is None:
        return _reject(REASON_FORMAT)
    if symbol in ASSIST_SYMBOLS:
        return _reject(REASON_CORE)
    current = list_explore_symbols(conn)
    if symbol in current:
        return _reject(REASON_DUP)
    if len(current) >= EXPLORE_LIMIT:
        return _reject(REASON_FULL)
    reason = contract_rejection(lookup(symbol))
    if reason is not None:
        return _reject(reason)
    fetch_fn, client = _bind_fetch(fetch)
    try:
        if not _kline_serves(symbol, fetch_fn):
            return _reject(REASON_KLINE)

        added = int(now_ms_value if now_ms_value is not None else now_ms())
        insert_explore_symbol(conn, symbol, added)
        own = explore_conn is None
        explore = explore_conn
        try:
            if explore is None:
                explore = open_db(explore_db_path(connection_path(conn)))
            seed_explore_symbol(explore, symbol, fetch=fetch_fn)
        except Exception:  # noqa: BLE001 — roll the membership back; the owner can retry
            logger.warning("assist explore seed failed for %s", symbol, exc_info=True)
            delete_explore_symbol(conn, symbol)
            if explore is not None:
                _delete_symbol_rows(explore, symbol)
            return _reject(REASON_KLINE)
        finally:
            if own and explore is not None:
                explore.close()
    finally:
        _close_client(client)

    text, markup = render_explore_message(conn)
    return {
        "handled": True,
        "ok": True,
        "symbol": symbol,
        "text": f"เพิ่ม {symbol} แล้ว\n{text}",
        "reply_markup": markup,
    }


def remove_explore_symbol(
    conn: sqlite3.Connection,
    symbol: str,
    *,
    explore_conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    sym = symbol.upper()
    delete_explore_symbol(conn, sym)
    own = explore_conn is None
    explore = explore_conn
    try:
        if explore is None:
            explore = open_db(explore_db_path(connection_path(conn)))
        _delete_symbol_rows(explore, sym)
    finally:
        if own and explore is not None:
            explore.close()
    text, markup = render_explore_message(conn)
    return {"handled": True, "ok": True, "text": text, "reply_markup": markup, "symbol": sym}


def _reject(reason: str) -> dict[str, Any]:
    return {"handled": True, "ok": False, "text": reason, "reply_markup": None, "reason": reason}


def _write_analysis(conn: sqlite3.Connection, symbol: str) -> dict[str, Any]:
    candle = load_latest_candle(conn, symbol, ASSIST_TIMEFRAME)
    if candle is None:
        raise RuntimeError(f"no {ASSIST_TIMEFRAME} candle for {symbol}")
    htf = htf_timeframe()
    if mtf_enabled() and ASSIST_TIMEFRAME != htf:
        result = analyze_mtf(
            candle,
            load_latest_candle(conn, symbol, htf),
            confidence_threshold=CONFIDENCE_THRESHOLD,
        )
    else:
        result = analyze_snapshot(candle, confidence_threshold=CONFIDENCE_THRESHOLD)
    upsert_analysis_output(conn, result)
    return result


def run_assist_explore(
    conn: sqlite3.Connection,
    explore_conn: sqlite3.Connection,
    *,
    fetch: FetchFn = fetch_klines,
    now_ms_value: int | None = None,
    clock: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Refresh explore candles and analysis. Never sends, never touches the kill switch."""
    at_ms = int(now_ms_value if now_ms_value is not None else now_ms())
    tick = clock or time.monotonic
    started = tick()
    wrote: list[str] = []
    failed: list[dict[str, str]] = []
    fetch_fn, client = _bind_fetch(fetch)
    try:
        for symbol in list_explore_symbols(conn):
            if tick() - started >= EXPLORE_BUDGET_S:
                failed.append({"symbol": symbol, "error": "budget"})
                continue
            try:
                for timeframe in _timeframes():
                    limit = _fetch_limit(explore_conn, symbol, timeframe, at_ms)
                    _fetch_into(explore_conn, symbol, timeframe, limit, fetch_fn)
                _write_analysis(explore_conn, symbol)
            except Exception as exc:  # noqa: BLE001 — one coin must not stop the others or the signal job
                logger.warning("assist explore skip %s: %s", symbol, exc)
                failed.append({"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"})
                continue
            wrote.append(symbol)
    finally:
        _close_client(client)
    return {
        "ok": True,
        "paper_only": True,
        "symbols": list_explore_symbols(conn),
        "wrote": wrote,
        "failed": failed,
        "at_ms": at_ms,
        "db": str(connection_path(explore_conn)),
    }


def execute_explore(
    *,
    main_path: Path,
    fetch: FetchFn = fetch_klines,
    now_ms_value: int | None = None,
) -> dict[str, Any]:
    at_ms = int(now_ms_value if now_ms_value is not None else now_ms())
    if not main_path.exists():
        return {"ok": False, "error": "no_database", "paper_only": True, "at_ms": at_ms}
    main = open_db(main_path)
    explore = open_db(explore_db_path(main_path))
    try:
        return run_assist_explore(main, explore, fetch=fetch, now_ms_value=at_ms)
    finally:
        main.close()
        explore.close()


def run_explore_cli(*, json_output: bool) -> int:
    path = main_db_path()
    if not path.exists():
        payload: dict[str, Any] = {"ok": False, "error": "no_database", "paper_only": True}
    else:
        payload = execute_explore(main_path=path)
    if json_output:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        wrote = ",".join(payload.get("wrote") or []) or "-"
        failed_rows = payload.get("failed") or []
        failed = ",".join(str(item.get("symbol")) for item in failed_rows) or "-"
        print(f"explore wrote={wrote} failed={failed}")
    return 0 if payload.get("ok") else 1


def latest_explore_analysis(
    explore_conn: sqlite3.Connection,
    symbol: str,
) -> dict[str, Any] | None:
    return load_latest_analysis(explore_conn, symbol, ASSIST_TIMEFRAME)
