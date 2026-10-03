"""Unit tests for free-first derivatives router (Binance Futures + CoinGlass)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from jarvise_ingest.providers import binance_futures_deriv, derivatives
from jarvise_ingest.providers.binance_futures_deriv import fetch_derivatives as bn_fetch


def test_binance_futures_deriv_parses_funding_and_oi(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(path: str, params: dict, *, client=None):
        if path.endswith("/fundingRate"):
            return [
                {"fundingTime": 1_700_000_000_000, "fundingRate": "0.0001"},
                {"fundingTime": 1_700_008_000_000, "fundingRate": "0.0002"},
            ]
        if path.endswith("/openInterestHist"):
            return [
                {
                    "timestamp": 1_700_000_000_000,
                    "sumOpenInterest": "100",
                    "sumOpenInterestValue": "5000000",
                },
                {
                    "timestamp": 1_700_003_600_000,
                    "sumOpenInterest": "110",
                    "sumOpenInterestValue": "5500000",
                },
            ]
        raise AssertionError(path)

    monkeypatch.setattr(binance_futures_deriv, "_get", fake_get)
    rows = bn_fetch("BTCUSDT", "1h", limit=10)
    assert rows
    assert rows[0]["symbol"] == "BTC"
    assert rows[-1]["funding_rate"] == 0.0002
    assert rows[-1]["open_interest_usd"] == 5_500_000.0
    assert rows[-1]["liquidations_24h_usd"] is None
    assert rows[-1]["long_short_ratio"] is None


def test_router_uses_binance_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COINGLASS_API_KEY", raising=False)
    monkeypatch.delenv("CG-API-KEY", raising=False)

    def fake_bn(symbol, interval, limit=30, *, client=None):
        return [
            {
                "symbol": "BTC",
                "timestamp": 1,
                "open_interest_usd": 1.0,
                "funding_rate": 0.001,
                "long_short_ratio": None,
                "liquidations_24h_usd": None,
            }
        ]

    monkeypatch.setattr(binance_futures_deriv, "fetch_derivatives", fake_bn)
    rows, provider = derivatives.fetch_derivatives("BTCUSDT", "1h", limit=5)
    assert provider == derivatives.PROVIDER_BINANCE_FUTURES
    assert rows[0]["funding_rate"] == 0.001


def test_router_uses_coinglass_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COINGLASS_API_KEY", "test-key")

    def fake_cg(symbol, interval, limit=30, *, api_key=None, client=None):
        assert api_key == "test-key"
        return [
            {
                "symbol": "BTC",
                "timestamp": 1,
                "open_interest_usd": 2.0,
                "funding_rate": 0.002,
                "long_short_ratio": 1.1,
                "liquidations_24h_usd": 9.0,
            }
        ]

    monkeypatch.setattr("jarvise_ingest.providers.coinglass.fetch_derivatives", fake_cg)
    rows, provider = derivatives.fetch_derivatives("BTCUSDT", "1h", limit=5)
    assert provider == derivatives.PROVIDER_COINGLASS
    assert rows[0]["liquidations_24h_usd"] == 9.0


def test_cli_no_key_does_not_exit_2_before_fetch(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("COINGLASS_API_KEY", raising=False)
    monkeypatch.delenv("CG-API-KEY", raising=False)

    from jarvise_ingest import cli as ingest_cli

    monkeypatch.setattr(
        ingest_cli,
        "fetch_klines",
        lambda *a, **k: [
            {
                "symbol": "BTCUSDT",
                "timestamp": 1_700_000_000_000,
                "timeframe": "1h",
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume": 1.0,
            }
        ],
    )
    monkeypatch.setattr(ingest_cli, "recompute_indicators", lambda *a, **k: 1)
    monkeypatch.setattr(ingest_cli, "find_gaps", lambda *a, **k: [])
    monkeypatch.setattr(
        ingest_cli,
        "fetch_order_book_snapshot",
        lambda *a, **k: {
            "symbol": "BTCUSDT",
            "timestamp": 1_700_000_000_000,
            "bid_ask_spread": 0.0001,
            "bid_depth_1pct_usd": 100_000,
            "ask_depth_1pct_usd": 100_000,
            "spoof_wall_detected": 0,
        },
    )
    monkeypatch.setattr(
        ingest_cli,
        "fetch_global_macro",
        lambda *a, **k: {
            "timestamp": 1_700_000_000_000,
            "btc_dominance_pct": 50.0,
            "global_market_cap_usd": 1e12,
        },
    )
    monkeypatch.setattr(
        ingest_cli,
        "fetch_derivatives",
        lambda *a, **k: (
            [
                {
                    "symbol": "BTC",
                    "timestamp": 1_700_000_000_000,
                    "open_interest_usd": 1.0,
                    "funding_rate": 0.0001,
                    "long_short_ratio": None,
                    "liquidations_24h_usd": None,
                }
            ],
            "binance_futures",
        ),
    )
    monkeypatch.setattr(
        ingest_cli,
        "evaluate_from_db",
        lambda *a, **k: MagicMock(
            as_dict=lambda: {
                "ok": True,
                "force_flat": False,
                "critical": False,
                "reasons": [],
                "kill_switch_engaged": False,
            },
            force_flat=False,
            critical=False,
            reasons=[],
            kill_switch_engaged=False,
        ),
    )

    code = ingest_cli.run(
        [
            "--symbol",
            "BTCUSDT",
            "--timeframe",
            "1h",
            "--limit",
            "5",
            "--db",
            str(tmp_path / "t.db"),
            "--json",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "binance_futures" in out
