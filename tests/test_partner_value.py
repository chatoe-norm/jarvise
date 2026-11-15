"""Read-only futures account value for the partner ledger (P2)."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

import httpx
import pytest

from jarvise_assist.futures import BinanceFuturesClient
from jarvise_assist.partner_value import (
    AccountValue,
    AccountValueError,
    parse_account_value,
    read_account_value,
)
from jarvise_exchange.binance_spot import BinanceAuth

D = Decimal


def _av(
    *,
    wallet: str = "1000",
    unrealized: str = "0",
    value: str | None = None,
    open_positions: int = 0,
    read_at_ms: int = 1,
) -> AccountValue:
    w = D(wallet)
    u = D(unrealized)
    return AccountValue(
        wallet=w,
        unrealized=u,
        value=D(value) if value is not None else w + u,
        open_positions=open_positions,
        read_at_ms=read_at_ms,
    )


def test_parse_happy_usdt_only() -> None:
    bal = [{"asset": "USDT", "balance": "1000.5"}, {"asset": "BNB", "balance": "0"}]
    pos = [
        {
            "symbol": "BTCUSDT",
            "positionAmt": "0.1",
            "unRealizedProfit": "10.25",
            "marginAsset": "USDT",
        },
        {"symbol": "ETHUSDT", "positionAmt": "0", "unRealizedProfit": "99"},
    ]
    out = parse_account_value(bal, pos, read_at_ms=42)
    assert isinstance(out, AccountValue)
    assert out.wallet == D("1000.5")
    assert out.unrealized == D("10.25")
    assert out.value == D("1010.75")
    assert out.open_positions == 1
    assert out.read_at_ms == 42


def test_parse_absent_margin_asset_treated_as_usdt() -> None:
    bal = [{"asset": "USDT", "balance": "100"}]
    pos = [{"symbol": "BTCUSDT", "positionAmt": "1", "unRealizedProfit": "1"}]
    out = parse_account_value(bal, pos, read_at_ms=1)
    assert isinstance(out, AccountValue)
    assert out.open_positions == 1
    assert out.unrealized == D("1")


def test_parse_no_usdt_row() -> None:
    out = parse_account_value([{"asset": "BNB", "balance": "0"}], [], read_at_ms=1)
    assert isinstance(out, AccountValueError)
    assert out.reason == "no_usdt_row"


def test_parse_non_usdt_asset() -> None:
    bal = [
        {"asset": "USDT", "balance": "100"},
        {"asset": "BNB", "balance": "0.01"},
        {"asset": "ETH", "balance": "1"},
    ]
    out = parse_account_value(bal, [], read_at_ms=1)
    assert isinstance(out, AccountValueError)
    assert out.reason == "non_usdt_asset"
    assert out.detail == "BNB,ETH"


def test_parse_non_usdt_position() -> None:
    bal = [{"asset": "USDT", "balance": "100"}]
    pos = [
        {
            "symbol": "ETHBTC",
            "positionAmt": "1",
            "unRealizedProfit": "0",
            "marginAsset": "BTC",
        },
        {
            "symbol": "BNBBTC",
            "positionAmt": "-2",
            "unRealizedProfit": "0",
            "marginAsset": "BTC",
        },
    ]
    out = parse_account_value(bal, pos, read_at_ms=1)
    assert isinstance(out, AccountValueError)
    assert out.reason == "non_usdt_position"
    assert out.detail == "BNBBTC,ETHBTC"


@pytest.mark.parametrize(
    "bal,pos",
    [
        ([{"asset": "USDT", "balance": "NaN"}], []),
        ([{"asset": "USDT", "balance": "Infinity"}], []),
        ([{"asset": "USDT"}], []),
        (
            [{"asset": "USDT", "balance": "100"}],
            [{"symbol": "X", "positionAmt": "1", "unRealizedProfit": "bad"}],
        ),
        (
            [{"asset": "USDT", "balance": "100"}],
            [{"symbol": "X", "positionAmt": "1"}],
        ),
    ],
)
def test_parse_bad_payload(bal: list[dict[str, Any]], pos: list[dict[str, Any]]) -> None:
    out = parse_account_value(bal, pos, read_at_ms=1)
    assert isinstance(out, AccountValueError)
    assert out.reason == "bad_payload"


def test_parse_value_not_positive() -> None:
    bal = [{"asset": "USDT", "balance": "10"}]
    pos = [
        {
            "symbol": "BTCUSDT",
            "positionAmt": "1",
            "unRealizedProfit": "-10",
            "marginAsset": "USDT",
        }
    ]
    out = parse_account_value(bal, pos, read_at_ms=1)
    assert isinstance(out, AccountValueError)
    assert out.reason == "value_not_positive"


def test_read_no_client() -> None:
    out = read_account_value(None, now_ms=1)
    assert isinstance(out, AccountValueError)
    assert out.reason == "no_client"


class _ScriptedClient:
    def __init__(self, balance_seq: list[Any], position_payload: Any) -> None:
        self._balances = list(balance_seq)
        self._positions = position_payload
        self.balance_calls = 0
        self.position_calls = 0

    def balance_rows(self) -> list[dict[str, Any]]:
        self.balance_calls += 1
        item = self._balances.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def position_rows(self) -> list[dict[str, Any]]:
        self.position_calls += 1
        if isinstance(self._positions, Exception):
            raise self._positions
        return self._positions


def _usdt_bal(n: str) -> list[dict[str, Any]]:
    return [{"asset": "USDT", "balance": n}]


def test_read_bracket_equal_three_calls() -> None:
    client = _ScriptedClient(
        [_usdt_bal("100"), _usdt_bal("100")],
        [],
    )
    out = read_account_value(client, now_ms=9, attempts=3)
    assert isinstance(out, AccountValue)
    assert out.wallet == D("100")
    assert out.value == D("100")
    assert client.balance_calls == 2
    assert client.position_calls == 1
    assert client.balance_calls + client.position_calls == 3


def test_read_bracket_retry_then_ok() -> None:
    client = _ScriptedClient(
        [
            _usdt_bal("100"),
            _usdt_bal("101"),
            _usdt_bal("101"),
            _usdt_bal("101"),
        ],
        [],
    )
    out = read_account_value(client, now_ms=9, attempts=3)
    assert isinstance(out, AccountValue)
    assert out.wallet == D("101")
    assert client.balance_calls == 4
    assert client.position_calls == 2


def test_read_wallet_moving() -> None:
    client = _ScriptedClient(
        [
            _usdt_bal("1"),
            _usdt_bal("2"),
            _usdt_bal("3"),
            _usdt_bal("4"),
            _usdt_bal("5"),
            _usdt_bal("6"),
        ],
        [],
    )
    out = read_account_value(client, now_ms=9, attempts=3)
    assert isinstance(out, AccountValueError)
    assert out.reason == "wallet_moving"
    assert client.balance_calls == 6
    assert client.position_calls == 3


def test_read_failed_hides_exception_text() -> None:
    client = _ScriptedClient(
        [RuntimeError("SECRET_URL https://evil.example/key")],
        [],
    )
    out = read_account_value(client, now_ms=1)
    assert isinstance(out, AccountValueError)
    assert out.reason == "read_failed"
    assert out.detail == "RuntimeError"
    blob = repr(out) + out.detail + out.reason
    assert "SECRET_URL" not in blob
    assert "evil" not in blob


def test_read_empty_balance_no_usdt_row_three_calls() -> None:
    client = _ScriptedClient([[], []], [])
    out = read_account_value(client, now_ms=9, attempts=3)
    assert isinstance(out, AccountValueError)
    assert out.reason == "no_usdt_row"
    assert client.balance_calls == 2
    assert client.position_calls == 1
    assert client.balance_calls + client.position_calls == 3


def test_read_bad_usdt_balance_three_calls() -> None:
    bad = [{"asset": "USDT", "balance": "abc"}]
    client = _ScriptedClient([bad, bad], [])
    out = read_account_value(client, now_ms=9, attempts=3)
    assert isinstance(out, AccountValueError)
    assert out.reason == "bad_payload"
    assert client.balance_calls == 2
    assert client.position_calls == 1
    assert client.balance_calls + client.position_calls == 3


def test_balance_and_position_rows_get_only() -> None:
    paths: list[str] = []
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        paths.append(urlparse(str(request.url)).path)
        if request.url.path.endswith("/balance"):
            return httpx.Response(
                200,
                content=json.dumps([{"asset": "USDT", "balance": "10", "availableBalance": "10"}]),
            )
        if request.url.path.endswith("/positionRisk"):
            return httpx.Response(200, content=json.dumps([]))
        return httpx.Response(404, content=b"{}")

    http = httpx.Client(transport=httpx.MockTransport(handler))
    auth = BinanceAuth(api_key="k", hmac_secret="s")
    client = BinanceFuturesClient(auth, client=http, base_url="https://fapi.binance.com")
    bal = client.balance_rows()
    pos = client.position_rows()
    assert bal[0]["asset"] == "USDT"
    assert pos == []
    assert methods == ["GET", "GET"]
    assert paths == ["/fapi/v3/balance", "/fapi/v3/positionRisk"]
    http.close()


def test_balance_rows_rejects_non_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps({"asset": "USDT"}))

    http = httpx.Client(transport=httpx.MockTransport(handler))
    auth = BinanceAuth(api_key="k", hmac_secret="s")
    client = BinanceFuturesClient(auth, client=http)
    with pytest.raises(ValueError):
        client.balance_rows()
    http.close()


def test_position_rows_rejects_non_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps({"symbol": "BTCUSDT"}))

    http = httpx.Client(transport=httpx.MockTransport(handler))
    auth = BinanceAuth(api_key="k", hmac_secret="s")
    client = BinanceFuturesClient(auth, client=http)
    with pytest.raises(ValueError):
        client.position_rows()
    http.close()
