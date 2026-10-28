"""Explore list: Telegram membership, separate DB, analysis. No live calls."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from jarvise_assist import ASSIST_TIMEFRAME
from jarvise_assist.explore import (
    EXPLORE_BUDGET_S,
    REASON_CONTRACT,
    REASON_CORE,
    REASON_DUP,
    REASON_FORMAT,
    REASON_FULL,
    REASON_KLINE,
    REASON_UNKNOWN,
    REFRESH_LIMIT,
    SEED_LIMIT,
    execute_explore,
    explore_db_path,
    latest_explore_analysis,
    remove_explore_symbol,
    run_assist_explore,
    seed_explore_symbol,
    submit_explore_symbol,
)
from jarvise_assist.keyboard import BTN_EXPLORE, EXPLORE_RM_PREFIX, reply_keyboard
from jarvise_assist.listen import process_update
from jarvise_assist.store import ensure_settings, insert_explore_symbol, list_explore_symbols
from jarvise_ingest.db import (
    load_latest_candle,
    open_db,
    upsert_analysis_output,
    upsert_market_technicals,
)


@pytest.fixture(autouse=True)
def _mtf_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ANALYZE_MTF", "false")


def _perp(symbol: str = "LINKUSDT") -> dict[str, str]:
    return {
        "symbol": symbol,
        "contractType": "PERPETUAL",
        "quoteAsset": "USDT",
        "status": "TRADING",
    }


def _candles(symbol: str, timeframe: str, n: int) -> list[dict[str, Any]]:
    step = 14_400_000 if timeframe == "4h" else 86_400_000
    start = 1_700_000_000_000
    rows: list[dict[str, Any]] = []
    for i in range(n):
        px = 100.0 + i * 0.05
        rows.append(
            {
                "symbol": symbol,
                "timestamp": start + i * step,
                "timeframe": timeframe,
                "open": px,
                "high": px + 1.0,
                "low": px - 1.0,
                "close": px + 0.4,
                "volume": 10.0,
            }
        )
    return rows


def _fetch_n(n: int):
    def fetch(symbol: str, interval: str, limit: int) -> list[dict[str, Any]]:
        return _candles(symbol, interval, min(limit, n))

    return fetch


def _pair(tmp_path: Path):
    main = open_db(tmp_path / "jarvise.db")
    explore = open_db(explore_db_path(tmp_path / "jarvise.db"))
    return main, explore


def _count(conn: Any, table: str, symbol: str | None = None) -> int:
    if symbol is None:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    else:
        row = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE symbol=?", (symbol,)).fetchone()
    return int(row[0])


def test_add_remove_and_duplicate(tmp_path: Path) -> None:
    main, explore = _pair(tmp_path)
    added = submit_explore_symbol(
        main,
        "link/usdt",
        lookup=lambda symbol: _perp(symbol),
        fetch=_fetch_n(5),
        now_ms_value=10,
        explore_conn=explore,
    )
    assert added["ok"] is True
    assert added["symbol"] == "LINKUSDT"
    assert REASON_FORMAT in added["text"]
    assert list_explore_symbols(main) == ["LINKUSDT"]
    assert _count(main, "market_technicals") == 0
    assert _count(explore, "market_technicals", "LINKUSDT") > 0

    again = submit_explore_symbol(
        main,
        "LINKUSDT",
        lookup=lambda symbol: _perp(symbol),
        fetch=_fetch_n(5),
        now_ms_value=11,
        explore_conn=explore,
    )
    assert again["text"] == REASON_DUP

    removed = remove_explore_symbol(main, "LINKUSDT")
    assert list_explore_symbols(main) == []
    assert "ยังไม่มี" in removed["text"]
    main.close()
    explore.close()


def test_reject_core_full_contract_unknown_and_kline(tmp_path: Path) -> None:
    main, explore = _pair(tmp_path)

    def unused(symbol: str) -> dict[str, str]:
        raise AssertionError(symbol)

    core = submit_explore_symbol(
        main, "BTCUSDT", lookup=unused, fetch=_fetch_n(1), now_ms_value=1, explore_conn=explore
    )
    assert core["text"] == REASON_CORE

    bad_form = submit_explore_symbol(
        main, "link", lookup=unused, fetch=_fetch_n(1), now_ms_value=1, explore_conn=explore
    )
    assert bad_form["text"] == REASON_FORMAT

    for i in range(10):
        insert_explore_symbol(main, f"A{i}USDT", i)
    full = submit_explore_symbol(
        main, "LINKUSDT", lookup=unused, fetch=_fetch_n(1), now_ms_value=1, explore_conn=explore
    )
    assert full["text"] == REASON_FULL
    for i in range(10):
        remove_explore_symbol(main, f"A{i}USDT")

    quarterly = _perp("LINKUSDT")
    quarterly["contractType"] = "CURRENT_QUARTER"
    not_perp = submit_explore_symbol(
        main,
        "LINKUSDT",
        lookup=lambda _symbol: quarterly,
        fetch=_fetch_n(1),
        now_ms_value=1,
        explore_conn=explore,
    )
    assert not_perp["text"] == REASON_CONTRACT

    unknown = submit_explore_symbol(
        main,
        "LINKUSDT",
        lookup=lambda _symbol: None,
        fetch=_fetch_n(1),
        now_ms_value=1,
        explore_conn=explore,
    )
    assert unknown["text"] == REASON_UNKNOWN

    def down(_symbol: str, _interval: str, _limit: int) -> list[dict[str, Any]]:
        raise RuntimeError("no kline")

    missing = submit_explore_symbol(
        main,
        "LINKUSDT",
        lookup=lambda symbol: _perp(symbol),
        fetch=down,
        now_ms_value=1,
        explore_conn=explore,
    )
    assert missing["text"] == REASON_KLINE
    assert list_explore_symbols(main) == []
    assert _count(main, "market_technicals") == 0
    assert _count(main, "analysis_output") == 0
    main.close()
    explore.close()


def test_failed_seed_removes_the_row(tmp_path: Path) -> None:
    main, explore = _pair(tmp_path)

    def fetch(symbol: str, interval: str, limit: int) -> list[dict[str, Any]]:
        if limit >= 1000:
            raise RuntimeError("seed failed")
        return _candles(symbol, interval, 1)

    result = submit_explore_symbol(
        main,
        "LINKUSDT",
        lookup=lambda symbol: _perp(symbol),
        fetch=fetch,
        now_ms_value=1,
        explore_conn=explore,
    )
    assert result["text"] == REASON_KLINE
    assert list_explore_symbols(main) == []
    main.close()
    explore.close()


def test_listener_fails_closed_without_chat_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    conn = open_db(tmp_path / "jarvise.db")
    sent: list[str] = []
    update = {"message": {"chat": {"id": 1}, "text": BTN_EXPLORE}}
    result = process_update(conn, update, send=lambda text, **_kw: sent.append(text))
    assert result == {"ignored": "no_owner_chat"}
    assert sent == []
    conn.close()


def test_listener_add_and_remove(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    main = open_db(tmp_path / "jarvise.db")
    sent: list[str] = []

    def send(text: str, **_kw: Any) -> int:
        sent.append(text)
        return 1

    opened = process_update(
        main,
        {"message": {"chat": {"id": 42}, "text": BTN_EXPLORE}},
        send=send,
        answer=lambda *_a, **_k: None,
    )
    assert opened["kind"] == "explore"
    assert ensure_settings(main)["awaiting_explore"] is True
    assert BTN_EXPLORE in reply_keyboard()["keyboard"][3][0]["text"]

    added = process_update(
        main,
        {"message": {"chat": {"id": 42}, "text": "link-usdt"}},
        send=send,
        explore_lookup=lambda symbol: _perp(symbol),
        explore_fetch=_fetch_n(4),
    )
    assert added["ok"] is True
    assert list_explore_symbols(main) == ["LINKUSDT"]
    explore = open_db(explore_db_path(tmp_path / "jarvise.db"))
    assert _count(explore, "market_technicals", "LINKUSDT") > 0
    assert _count(main, "market_technicals", "LINKUSDT") == 0

    removed = process_update(
        main,
        {
            "callback_query": {
                "id": "cq",
                "data": f"{EXPLORE_RM_PREFIX}LINKUSDT",
                "message": {"chat": {"id": 42}, "message_id": 1, "text": "list"},
            }
        },
        send=send,
        answer=lambda *_a, **_k: None,
    )
    assert removed["symbol"] == "LINKUSDT"
    assert list_explore_symbols(main) == []
    explore.close()
    main.close()


def test_warmup_is_flat_and_main_db_stays_clean(tmp_path: Path) -> None:
    main, explore = _pair(tmp_path)
    upsert_market_technicals(main, _candles("BTCUSDT", ASSIST_TIMEFRAME, 1))
    insert_explore_symbol(main, "LINKUSDT", 1)
    before_market = _count(main, "market_technicals")
    before_analysis = _count(main, "analysis_output")

    payload = run_assist_explore(main, explore, fetch=_fetch_n(20), now_ms_value=50)
    assert payload["ok"] is True
    assert payload["wrote"] == ["LINKUSDT"]
    assert payload["failed"] == []

    candle = load_latest_candle(explore, "LINKUSDT", ASSIST_TIMEFRAME)
    assert candle is not None
    assert candle["ema_200"] is None
    row = latest_explore_analysis(explore, "LINKUSDT")
    assert row is not None
    assert row["action"] == "flat"
    assert _count(main, "market_technicals") == before_market
    assert _count(main, "analysis_output") == before_analysis
    assert _count(main, "market_technicals", "LINKUSDT") == 0
    assert _count(main, "analysis_output", "LINKUSDT") == 0
    assert _count(explore, "analysis_output", "LINKUSDT") == 1
    main.close()
    explore.close()


def test_warm_series_writes_analysis(tmp_path: Path) -> None:
    main, explore = _pair(tmp_path)
    insert_explore_symbol(main, "LINKUSDT", 1)
    seed_explore_symbol(
        explore, "LINKUSDT", fetch=lambda symbol, interval, _limit: _candles(symbol, interval, 700)
    )
    payload = run_assist_explore(main, explore, fetch=_fetch_n(30), now_ms_value=60)
    assert payload["wrote"] == ["LINKUSDT"]
    candle = load_latest_candle(explore, "LINKUSDT", ASSIST_TIMEFRAME)
    assert candle is not None
    assert candle["ema_200"] is not None
    row = latest_explore_analysis(explore, "LINKUSDT")
    assert row is not None
    assert row["symbol"] == "LINKUSDT"
    assert row["timeframe"] == ASSIST_TIMEFRAME
    main.close()
    explore.close()


def test_one_symbol_fetch_failure_skips_only_that_symbol(tmp_path: Path) -> None:
    main, explore = _pair(tmp_path)
    insert_explore_symbol(main, "LINKUSDT", 1)
    insert_explore_symbol(main, "AVAXUSDT", 2)

    def fetch(symbol: str, interval: str, limit: int) -> list[dict[str, Any]]:
        if symbol == "AVAXUSDT":
            raise RuntimeError("boom")
        return _candles(symbol, interval, min(limit, 30))

    payload = run_assist_explore(main, explore, fetch=fetch, now_ms_value=70)
    assert payload["ok"] is True
    assert payload["wrote"] == ["LINKUSDT"]
    assert payload["failed"] == [{"symbol": "AVAXUSDT", "error": "RuntimeError: boom"}]
    assert latest_explore_analysis(explore, "LINKUSDT") is not None
    assert latest_explore_analysis(explore, "AVAXUSDT") is None
    assert _count(main, "market_technicals", "AVAXUSDT") == 0
    assert _count(main, "analysis_output", "AVAXUSDT") == 0
    main.close()
    explore.close()


def test_mtf_fetches_htf_and_execute_uses_the_sibling_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ANALYZE_MTF", "true")
    monkeypatch.setenv("JARVISE_DB", str(tmp_path / "jarvise.db"))
    main = open_db(tmp_path / "jarvise.db")
    insert_explore_symbol(main, "LINKUSDT", 1)
    main.close()
    seen: list[str] = []

    def fetch(symbol: str, interval: str, limit: int) -> list[dict[str, Any]]:
        seen.append(interval)
        return _candles(symbol, interval, min(limit, 25))

    payload = execute_explore(main_path=tmp_path / "jarvise.db", fetch=fetch, now_ms_value=80)
    assert payload["ok"] is True
    assert payload["wrote"] == ["LINKUSDT"]
    assert seen == ["4h", "1d"]
    explore = open_db(explore_db_path(tmp_path / "jarvise.db"))
    assert load_latest_candle(explore, "LINKUSDT", "1d") is not None
    assert latest_explore_analysis(explore, "LINKUSDT") is not None
    main = open_db(tmp_path / "jarvise.db")
    assert _count(main, "market_technicals", "LINKUSDT") == 0
    assert _count(main, "analysis_output", "LINKUSDT") == 0
    explore.close()
    main.close()


def _analysis(symbol: str, timestamp: int) -> dict[str, Any]:
    return {
        "analysis_id": f"{symbol}-{timestamp}",
        "timestamp": timestamp,
        "symbol": symbol,
        "timeframe": "4h",
        "regime_state": "range",
        "confidence_score": 0.1,
        "action": "flat",
        "invalidation_price": None,
        "size_pct_equity": 0.0,
        "thesis": "test",
    }


def test_budget_skips_symbols_not_yet_started(tmp_path: Path) -> None:
    main, explore = _pair(tmp_path)
    insert_explore_symbol(main, "LINKUSDT", 1)
    insert_explore_symbol(main, "AVAXUSDT", 2)
    insert_explore_symbol(main, "DOTUSDT", 3)
    clock = {"t": 0.0}
    called: list[str] = []

    def fetch(symbol: str, interval: str, limit: int) -> list[dict[str, Any]]:
        called.append(symbol)
        clock["t"] = EXPLORE_BUDGET_S + 1
        return _candles(symbol, interval, min(limit, 4))

    payload = run_assist_explore(
        main,
        explore,
        fetch=fetch,
        now_ms_value=1_800_000_000_000,
        clock=lambda: clock["t"],
    )
    assert called == ["LINKUSDT"]
    assert payload["wrote"] == ["LINKUSDT"]
    assert payload["failed"] == [
        {"symbol": "AVAXUSDT", "error": "budget"},
        {"symbol": "DOTUSDT", "error": "budget"},
    ]
    main.close()
    explore.close()


def test_remove_deletes_only_explore_rows_for_that_symbol(tmp_path: Path) -> None:
    main, explore = _pair(tmp_path)
    insert_explore_symbol(main, "LINKUSDT", 1)
    insert_explore_symbol(main, "AVAXUSDT", 2)
    link = _candles("LINKUSDT", "4h", 1)
    avax = _candles("AVAXUSDT", "4h", 1)
    upsert_market_technicals(main, link)
    upsert_market_technicals(explore, link)
    upsert_market_technicals(explore, avax)
    upsert_analysis_output(main, _analysis("LINKUSDT", link[0]["timestamp"]))
    upsert_analysis_output(explore, _analysis("LINKUSDT", link[0]["timestamp"]))
    upsert_analysis_output(explore, _analysis("AVAXUSDT", avax[0]["timestamp"]))
    explore.close()

    removed = remove_explore_symbol(main, "LINKUSDT")
    assert removed["symbol"] == "LINKUSDT"
    assert list_explore_symbols(main) == ["AVAXUSDT"]

    explore = open_db(explore_db_path(tmp_path / "jarvise.db"))
    assert _count(explore, "market_technicals", "LINKUSDT") == 0
    assert _count(explore, "analysis_output", "LINKUSDT") == 0
    assert _count(explore, "market_technicals", "AVAXUSDT") == 1
    assert _count(explore, "analysis_output", "AVAXUSDT") == 1
    assert _count(main, "market_technicals", "LINKUSDT") == 1
    assert _count(main, "analysis_output", "LINKUSDT") == 1
    explore.close()
    main.close()


def test_missing_or_stale_series_fetches_seed_limit(tmp_path: Path) -> None:
    main, explore = _pair(tmp_path)
    insert_explore_symbol(main, "LINKUSDT", 1)
    insert_explore_symbol(main, "AVAXUSDT", 2)
    insert_explore_symbol(main, "DOTUSDT", 3)
    step = 14_400_000
    stale_ts = 1_700_000_000_000
    now = stale_ts + (REFRESH_LIMIT + 1) * step
    fresh_ts = now - 10 * step
    upsert_market_technicals(explore, _candles("AVAXUSDT", "4h", 1))
    fresh = _candles("DOTUSDT", "4h", 1)
    fresh[0]["timestamp"] = fresh_ts
    upsert_market_technicals(explore, fresh)
    seen: list[tuple[str, int]] = []

    def fetch(symbol: str, interval: str, limit: int) -> list[dict[str, Any]]:
        seen.append((symbol, limit))
        return _candles(symbol, interval, 3)

    run_assist_explore(main, explore, fetch=fetch, now_ms_value=now)
    assert seen == [
        ("LINKUSDT", SEED_LIMIT),
        ("AVAXUSDT", SEED_LIMIT),
        ("DOTUSDT", REFRESH_LIMIT),
    ]
    main.close()
    explore.close()
