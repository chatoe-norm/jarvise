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
