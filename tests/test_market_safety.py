"""Unit tests for market-safety ingest providers and gate."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from jarvise_ingest.db import (
    append_derivatives,
    latest_macro_sentiment,
    latest_order_book,
    open_db,
    upsert_macro_sentiment,
    upsert_order_book,
)
from jarvise_ingest.providers.binance_book import (
    _depth_notional_within_pct,
    fetch_order_book_snapshot,
)
from jarvise_ingest.providers.coingecko_global import (
    fetch_global_macro,
    resolve_coingecko_global_request,
)
from jarvise_paper.engine import effective_slip_bps, fill_price
from jarvise_risk.market_safety import (
    MarketSafetyConfig,
    apply_safety_to_analysis,
    evaluate_market_safety,
    maybe_engage_kill_switch,
)


def test_depth_notional_within_1pct() -> None:
    mid = 100.0
    bids = [["99.5", "10"], ["98.5", "100"], ["97.0", "50"]]
    asks = [["100.5", "8"], ["101.5", "20"], ["103.0", "5"]]
    bid_n = _depth_notional_within_pct(bids, mid=mid, side="bid")
    ask_n = _depth_notional_within_pct(asks, mid=mid, side="ask")
    assert abs(bid_n - 99.5 * 10) < 1e-6
    assert abs(ask_n - 100.5 * 8) < 1e-6


def test_fetch_order_book_snapshot_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(path: str, params: dict, *, client=None):
        if path.endswith("/ticker/bookTicker") or path.endswith("bookTicker"):
            return {"bidPrice": "100.0", "askPrice": "100.2"}
        return {
            "bids": [["99.9", "5"], ["98.0", "1"]],
            "asks": [["100.1", "4"], ["102.0", "1"]],
        }

    monkeypatch.setattr("jarvise_ingest.providers.binance_book._get", fake_get)
    row = fetch_order_book_snapshot("BTCUSDT", now_ms=1_700_000_000_000)
    assert row["symbol"] == "BTCUSDT"
    assert abs(row["bid_ask_spread"] - (0.2 / 100.1)) < 1e-9
    assert row["bid_depth_1pct_usd"] > 0
    assert row["ask_depth_1pct_usd"] > 0
    assert row["spoof_wall_detected"] == 0


def test_fetch_global_macro_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COINGECKO_PRO_API_KEY", raising=False)
    monkeypatch.delenv("COINGECKO_DEMO_API_KEY", raising=False)
    monkeypatch.delenv("COINGECKO_API_KEY", raising=False)
    monkeypatch.delenv("CoinGecko_API_KEY", raising=False)

    class Resp:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "data": {
                    "market_cap_percentage": {"btc": 54.2},
                    "total_market_cap": {"usd": 2.5e12},
                }
            }

    client = MagicMock()
    client.get.return_value = Resp()
    row = fetch_global_macro(client=client, now_ms=1_700_000_000_000)
    assert row["btc_dominance_pct"] == 54.2
    assert row["global_market_cap_usd"] == 2.5e12
    client.get.assert_called_once()
    args, kwargs = client.get.call_args
    assert args[0] == "https://api.coingecko.com/api/v3/global"
    assert kwargs["headers"] == {"Accept": "application/json"}


def test_resolve_coingecko_demo_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COINGECKO_PRO_API_KEY", raising=False)
    monkeypatch.setenv("COINGECKO_API_KEY", "CG-test-demo")
    url, headers = resolve_coingecko_global_request()
    assert url == "https://api.coingecko.com/api/v3/global"
    assert headers["x-cg-demo-api-key"] == "CG-test-demo"


def test_resolve_coingecko_legacy_key_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COINGECKO_PRO_API_KEY", raising=False)
    monkeypatch.delenv("COINGECKO_DEMO_API_KEY", raising=False)
    monkeypatch.delenv("COINGECKO_API_KEY", raising=False)
    monkeypatch.setenv("CoinGecko_API_KEY", "CG-legacy")
    url, headers = resolve_coingecko_global_request()
    assert url.endswith("/api/v3/global")
    assert headers["x-cg-demo-api-key"] == "CG-legacy"


def test_resolve_coingecko_pro_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COINGECKO_PRO_API_KEY", "CG-pro")
    monkeypatch.setenv("COINGECKO_API_KEY", "CG-demo-ignored")
    url, headers = resolve_coingecko_global_request()
    assert url == "https://pro-api.coingecko.com/api/v3/global"
    assert headers["x-cg-pro-api-key"] == "CG-pro"
    assert "x-cg-demo-api-key" not in headers


def test_fetch_global_macro_sends_demo_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COINGECKO_PRO_API_KEY", raising=False)
    monkeypatch.setenv("COINGECKO_API_KEY", "CG-demo")

    class Resp:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "data": {
                    "market_cap_percentage": {"btc": 50.0},
                    "total_market_cap": {"usd": 1e12},
                }
            }

    client = MagicMock()
    client.get.return_value = Resp()
    fetch_global_macro(client=client, now_ms=1)
    _args, kwargs = client.get.call_args
    assert kwargs["headers"]["x-cg-demo-api-key"] == "CG-demo"


def test_db_writers_roundtrip(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "m.db")
    now = int(time.time() * 1000)
    upsert_order_book(
        conn,
        {
            "symbol": "BTCUSDT",
            "timestamp": now,
            "bid_ask_spread": 0.001,
            "bid_depth_1pct_usd": 100_000,
            "ask_depth_1pct_usd": 120_000,
            "largest_buy_wall_price": None,
            "largest_sell_wall_price": None,
            "spoof_wall_detected": 0,
        },
    )
    upsert_macro_sentiment(
        conn,
        {
            "timestamp": now,
            "btc_dominance_pct": 55.0,
            "global_market_cap_usd": 1e12,
        },
    )
    append_derivatives(
        conn,
        [
            {
                "symbol": "BTC",
                "timestamp": now,
                "open_interest_usd": 1e9,
                "funding_rate": 0.0001,
                "long_short_ratio": 1.1,
                "liquidations_24h_usd": 5e6,
            }
        ],
        ingested_at=now,
    )
    book = latest_order_book(conn, "BTCUSDT")
    macro = latest_macro_sentiment(conn)
    assert book is not None and book["bid_ask_spread"] == 0.001
    assert macro is not None and macro["btc_dominance_pct"] == 55.0
    assert macro["global_market_cap_usd"] == 1e12


def test_gate_wide_spread_flat_not_critical() -> None:
    now = int(time.time() * 1000)
    cfg = MarketSafetyConfig(
        enabled=True,
        max_spread_bps=50.0,
        min_depth_usd=10_000.0,
        max_age_min=90.0,
        require_book=True,
        require_derivatives=False,
        require_macro=False,
    )
    result = evaluate_market_safety(
        book={
            "timestamp": now,
            "bid_ask_spread": 0.01,  # 100 bps
            "bid_depth_1pct_usd": 50_000,
            "ask_depth_1pct_usd": 50_000,
        },
        config=cfg,
        now_ms=now,
    )
    assert result.force_flat is True
    assert result.critical is False
    assert any("spread_bps" in r for r in result.reasons)


def test_gate_illiquid_critical() -> None:
    now = int(time.time() * 1000)
    cfg = MarketSafetyConfig(
        enabled=True,
        require_book=True,
        require_derivatives=False,
        require_macro=False,
        min_depth_usd=25_000,
    )
    result = evaluate_market_safety(
        book={
            "timestamp": now,
            "bid_ask_spread": 0.0001,
            "bid_depth_1pct_usd": 100,
            "ask_depth_1pct_usd": 100,
        },
        config=cfg,
        now_ms=now,
    )
    assert result.force_flat and result.critical
    assert any("illiquid" in r for r in result.reasons)


def test_gate_provider_error_critical_and_ks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    called: list[str] = []

    def fake_ks(*, reason: str = "") -> bool:
        called.append(reason)
        return True

    monkeypatch.setattr("jarvise_risk.market_safety.engage_kill_switch", fake_ks)
    cfg = MarketSafetyConfig(
        enabled=True,
        require_book=True,
        require_derivatives=False,
        require_macro=False,
    )
    result = evaluate_market_safety(config=cfg, provider_errors=["book:boom"])
    assert result.critical
    result = maybe_engage_kill_switch(result)
    assert result.kill_switch_engaged
    assert called and called[0].startswith("market_safety:")


def test_optional_macro_provider_error_no_ks() -> None:
    """CoinGecko blip must not engage kill-switch when macro is not required."""
    cfg = MarketSafetyConfig(
        enabled=True,
        require_book=False,
        require_derivatives=False,
        require_macro=False,
    )
    result = evaluate_market_safety(
        config=cfg,
        provider_errors=["macro:Client error '400 Bad Request'"],
    )
    assert result.ok is True
    assert result.force_flat is False
    assert result.critical is False
    assert maybe_engage_kill_switch(result).kill_switch_engaged is False


def test_required_macro_provider_error_is_critical() -> None:
    cfg = MarketSafetyConfig(
        enabled=True,
        require_book=False,
        require_derivatives=False,
        require_macro=True,
    )
    result = evaluate_market_safety(
        config=cfg,
        provider_errors=["macro:timeout"],
    )
    assert result.force_flat and result.critical
    assert any("provider_error: macro:" in r for r in result.reasons)


def test_gate_disabled_ok() -> None:
    result = evaluate_market_safety(
        config=MarketSafetyConfig(enabled=False),
        provider_errors=["anything"],
    )
    assert result.ok and not result.force_flat


def test_skip_derivatives_no_require() -> None:
    now = int(time.time() * 1000)
    cfg = MarketSafetyConfig(
        enabled=True,
        require_book=True,
        require_derivatives=False,
        require_macro=False,
    )
    result = evaluate_market_safety(
        book={
            "timestamp": now,
            "bid_ask_spread": 0.0001,
            "bid_depth_1pct_usd": 100_000,
            "ask_depth_1pct_usd": 100_000,
        },
        derivatives=None,
        config=cfg,
        now_ms=now,
    )
    assert result.ok


def test_apply_safety_forces_flat() -> None:
    from jarvise_risk.market_safety import MarketSafetyResult

    analysis = {
        "action": "long",
        "size_pct_equity": 1.5,
        "confidence_score": 0.8,
        "thesis": "Trend up",
        "invalidation_price": 90.0,
    }
    safety = MarketSafetyResult(ok=False, force_flat=True, critical=False, reasons=["spread_bps=80"])
    out = apply_safety_to_analysis(analysis, safety)
    assert out["action"] == "flat"
    assert out["size_pct_equity"] == 0.0
    assert "Market safety veto" in out["thesis"]


def test_spread_aware_slip() -> None:
    assert effective_slip_bps(slip_bps=5.0, bid_ask_spread=None) == 5.0
    assert effective_slip_bps(slip_bps=5.0, bid_ask_spread=0.002) == 10.0  # half of 20bps
    buy_fixed = fill_price(100.0, side="buy", slip_bps=5.0)
    buy_wide = fill_price(100.0, side="buy", slip_bps=5.0, bid_ask_spread=0.002)
    assert buy_wide > buy_fixed


def test_apply_signal_uses_book_spread(tmp_path: Path) -> None:
    from jarvise_ingest.db import ensure_paper_account
    from jarvise_paper.engine import apply_signal

    conn = open_db(tmp_path / "p.db")
    ensure_paper_account(conn)
    now = int(time.time() * 1000)
    upsert_order_book(
        conn,
        {
            "symbol": "BTCUSDT",
            "timestamp": now,
            "bid_ask_spread": 0.002,
            "bid_depth_1pct_usd": 1e6,
            "ask_depth_1pct_usd": 1e6,
            "spoof_wall_detected": 0,
        },
    )
    res = apply_signal(
        conn,
        analysis={
            "analysis_id": "a1",
            "symbol": "BTCUSDT",
            "action": "long",
            "size_pct_equity": 10.0,
            "regime_state": "trend_up",
            "confidence_score": 0.7,
        },
        mid_price=100.0,
        timeframe="4h",
    )
    assert res["fills"]
    assert res["fills"][0]["slip_bps"] == 10.0
