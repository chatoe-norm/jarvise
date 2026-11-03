"""Tests for Binance API key permission audit."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
from typer.testing import CliRunner

from jarvise.cli import app
from jarvise_exchange.binance_spot import BinanceAuth
from jarvise_exchange.permissions import audit_key_permissions, fetch_api_restrictions

runner = CliRunner()


def test_audit_trade_ok() -> None:
    out = audit_key_permissions(
        {
            "enableWithdrawals": False,
            "enableInternalTransfer": False,
            "permitsUniversalTransfer": False,
            "enableSpotAndMarginTrading": True,
            "enableReading": True,
        }
    )
    assert out["ok_for_trade"] is True
    assert out["ok_for_read"] is True


def test_audit_blocks_withdraw() -> None:
    out = audit_key_permissions({"enableWithdrawals": True, "enableSpotAndMarginTrading": True})
    assert out["ok_for_trade"] is False
    assert "enableWithdrawals=true" in out["block_reasons"]


def test_fetch_api_restrictions_mocked() -> None:
    auth = BinanceAuth(api_key="k", hmac_secret="s")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"enableWithdrawals": False, "enableSpotAndMarginTrading": True}
    client = MagicMock(spec=httpx.Client)
    client.get.return_value = mock_resp
    payload = fetch_api_restrictions(auth, client=client, timestamp_ms=1)
    assert payload["enableSpotAndMarginTrading"] is True
    assert "/sapi/v1/account/apiRestrictions" in client.get.call_args.args[0]


def test_check_key_cli_json(monkeypatch) -> None:
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    with patch(
        "jarvise_exchange.cli.fetch_api_restrictions",
        return_value={
            "enableWithdrawals": False,
            "enableSpotAndMarginTrading": False,
            "enableReading": True,
        },
    ):
        result = runner.invoke(app, ["exchange", "check-key", "--json"])
    assert result.exit_code == 0
    assert '"ok_for_read": true' in result.output
    assert '"ok_for_trade": false' in result.output


def test_check_key_assist_trade_pass(monkeypatch, tmp_path) -> None:
    from jarvise_exchange.binance_spot import BinanceAuth

    pem = tmp_path / "trade.pem"
    pem.write_text("placeholder", encoding="utf-8")
    monkeypatch.setenv("BINANCE_TRADE_API_KEY", "T" * 64)
    monkeypatch.setenv("BINANCE_TRADE_PRIVATE_KEY_PATH", str(pem))
    monkeypatch.setenv("BINANCE_API_KEY", "R" * 64)
    monkeypatch.setenv("BINANCE_API_PRIVATE_KEY_PATH", "/other.pem")
    auth = BinanceAuth(api_key="T" * 64, hmac_secret="s")
    with (
        patch(
            "jarvise_exchange.cli.resolve_assist_trade_auth",
            return_value=(auth, None),
        ),
        patch(
            "jarvise_exchange.cli.fetch_api_restrictions",
            return_value={
                "enableFutures": True,
                "enableWithdrawals": False,
                "ipRestrict": True,
            },
        ),
    ):
        result = runner.invoke(app, ["exchange", "check-key", "--assist-trade", "--json"])
    assert result.exit_code == 0
    assert '"ok": true' in result.output
    assert '"enableFutures": true' in result.output
    assert '"ipRestrict": true' in result.output
    assert '"assist_trade_key": true' in result.output


def test_check_key_assist_trade_fail_missing(monkeypatch) -> None:
    monkeypatch.delenv("BINANCE_TRADE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_TRADE_PRIVATE_KEY_PATH", raising=False)
    with patch(
        "jarvise_exchange.cli.resolve_assist_trade_auth",
        return_value=(None, "no_key"),
    ):
        result = runner.invoke(app, ["exchange", "check-key", "--assist-trade", "--json"])
    assert result.exit_code == 2
    assert '"ok": false' in result.output
    assert '"reason": "no_key"' in result.output


def test_check_key_assist_trade_fail_unsafe(monkeypatch) -> None:
    from jarvise_exchange.binance_spot import BinanceAuth

    auth = BinanceAuth(api_key="T" * 64, hmac_secret="s")
    with (
        patch(
            "jarvise_exchange.cli.resolve_assist_trade_auth",
            return_value=(auth, None),
        ),
        patch(
            "jarvise_exchange.cli.fetch_api_restrictions",
            return_value={
                "enableFutures": True,
                "enableWithdrawals": True,
                "ipRestrict": False,
            },
        ),
    ):
        result = runner.invoke(app, ["exchange", "check-key", "--assist-trade", "--json"])
    assert result.exit_code == 3
    assert '"ok": false' in result.output
    assert "enableWithdrawals=true" in result.output


def test_check_key_trade_unchanged(monkeypatch) -> None:
    """Existing --trade path still uses jarvise_trade.auth + ok_for_trade."""
    monkeypatch.setenv("BINANCE_TRADE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_TRADE_API_SECRET", "s")
    with (
        patch(
            "jarvise_exchange.cli.resolve_trade_auth",
            return_value=BinanceAuth(api_key="k", hmac_secret="s"),
        ),
        patch(
            "jarvise_exchange.cli.fetch_api_restrictions",
            return_value={
                "enableWithdrawals": False,
                "enableInternalTransfer": False,
                "permitsUniversalTransfer": False,
                "enableSpotAndMarginTrading": True,
                "enableReading": True,
            },
        ),
    ):
        result = runner.invoke(app, ["exchange", "check-key", "--trade", "--json"])
    assert result.exit_code == 0
    assert '"ok_for_trade": true' in result.output
    assert '"trade_key": true' in result.output
