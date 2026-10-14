"""Shared GET layer: retry/backoff on transient failures, never on auth/client errors."""

from __future__ import annotations

import json

import httpx
import pytest

from jarvise_ingest import http as jhttp
from jarvise_ingest.http import ProviderError, backoff_delay, get_json, retry_after_seconds


def _client(responses: list[httpx.Response | Exception]) -> tuple[httpx.Client, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def test_retry_after_honored_on_429(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(jhttp, "_sleep", lambda s: sleeps.append(s))
    client, seen = _client(
        [
            httpx.Response(429, headers={"Retry-After": "2"}, content=b"{}"),
            httpx.Response(200, content=json.dumps({"ok": 1}).encode()),
        ]
    )
    assert get_json("https://x/api", client=client, provider="t") == {"ok": 1}
    assert len(seen) == 2
    assert sleeps == [2.0]


def test_5xx_then_200_succeeds_with_backoff(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(jhttp, "_sleep", lambda s: sleeps.append(s))
    client, seen = _client(
        [
            httpx.Response(503, content=b"busy"),
            httpx.Response(502, content=b"bad gateway"),
            httpx.Response(200, content=b'{"v": 2}'),
        ]
    )
    assert get_json("https://x/api", client=client, retries=3) == {"v": 2}
    assert len(seen) == 3
    assert len(sleeps) == 2
    # exponential: attempt 0 → ~0.5s, attempt 1 → ~1.0s (plus ≤25% jitter)
    assert 0.5 <= sleeps[0] <= 0.625
    assert 1.0 <= sleeps[1] <= 1.25


def test_401_is_not_retried() -> None:
    client, seen = _client([httpx.Response(401, content=b'{"code":-2015}')])
    with pytest.raises(ProviderError) as info:
        get_json("https://x/api", client=client, retries=3, provider="binance_account")
    assert len(seen) == 1
    assert info.value.status == 401 and info.value.retryable is False


def test_404_is_not_retried() -> None:
    client, seen = _client([httpx.Response(404, content=b"{}")])
    with pytest.raises(ProviderError) as info:
        get_json("https://x/api", client=client, retries=3)
    assert len(seen) == 1 and info.value.status == 404


def test_timeout_retried_then_exhausted() -> None:
    client, seen = _client([httpx.ReadTimeout("t"), httpx.ConnectError("c"), httpx.ReadTimeout("t")])
    with pytest.raises(ProviderError) as info:
        get_json("https://x/api", client=client, retries=2, provider="p")
    assert len(seen) == 3
    assert info.value.retryable is True
    assert info.value.attempts == ["ReadTimeout", "ConnectError", "ReadTimeout"]


def test_retries_zero_is_single_shot() -> None:
    client, seen = _client([httpx.Response(500, content=b"x")])
    with pytest.raises(ProviderError):
        get_json("https://x/api", client=client, retries=0)
    assert len(seen) == 1


def test_env_default_retries(monkeypatch) -> None:
    monkeypatch.setenv("JARVISE_HTTP_RETRIES", "1")
    client, seen = _client([httpx.Response(500, content=b"x"), httpx.Response(500, content=b"x")])
    with pytest.raises(ProviderError):
        get_json("https://x/api", client=client)
    assert len(seen) == 2


def test_url_factory_rebuilt_per_attempt() -> None:
    calls = {"n": 0}

    def url() -> str:
        calls["n"] += 1
        return f"https://x/api?ts={calls['n']}"

    client, seen = _client([httpx.Response(503, content=b""), httpx.Response(200, content=b"{}")])
    assert get_json(url, client=client, retries=1) == {}
    assert [str(r.url) for r in seen] == ["https://x/api?ts=1", "https://x/api?ts=2"]


def test_invalid_json_is_provider_error() -> None:
    client, _ = _client([httpx.Response(200, content=b"<html>")])
    with pytest.raises(ProviderError) as info:
        get_json("https://x/api", client=client)
    assert info.value.retryable is False


def test_retry_after_parse_and_cap() -> None:
    assert retry_after_seconds(httpx.Response(429, headers={"Retry-After": "5"})) == 5.0
    assert retry_after_seconds(httpx.Response(429, headers={"Retry-After": "999"})) == 60.0
    assert retry_after_seconds(httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct"})) is None
    assert retry_after_seconds(httpx.Response(429)) is None
    assert 8.0 <= backoff_delay(10) <= 10.0  # capped at 8s + jitter


def test_provider_wraps_error_with_retry_hint() -> None:
    from jarvise_ingest.providers.binance_klines import fetch_klines

    client, seen = _client([httpx.Response(500, content=b"x")] * 4)
    with pytest.raises(RuntimeError, match="binance klines failed.*--skip-derivatives"):
        fetch_klines("BTCUSDT", "1h", 5, client=client)
    assert len(seen) == 4  # 1 + 3 retries


def test_circuit_opens_after_threshold_and_skips(monkeypatch) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)
    provider = "flaky"
    for _ in range(jhttp.CIRCUIT_FAILURE_THRESHOLD):
        client, _ = _client([httpx.Response(503, content=b"")])
        with pytest.raises(ProviderError) as info:
            get_json("https://x/api", client=client, retries=0, provider=provider)
        assert info.value.circuit_open is False
    assert jhttp.circuit_open_until(provider) is not None
    # Open circuit: no HTTP call at all, distinct error.
    client, seen = _client([httpx.Response(200, content=b"{}")])
    with pytest.raises(ProviderError) as info:
        get_json("https://x/api", client=client, retries=0, provider=provider)
    assert info.value.circuit_open is True and "circuit_open" in str(info.value)
    assert seen == []
    jhttp.reset_circuit(provider)
    assert jhttp.circuit_open_until(provider) is None
    assert get_json("https://x/api", client=client, retries=0, provider=provider) == {}


def test_success_resets_failure_count(monkeypatch) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)
    provider = "recovering"
    for _ in range(jhttp.CIRCUIT_FAILURE_THRESHOLD - 1):
        client, _ = _client([httpx.ReadTimeout("t")])
        with pytest.raises(ProviderError):
            get_json("https://x/api", client=client, retries=0, provider=provider)
    client, _ = _client([httpx.Response(200, content=b"{}")])
    assert get_json("https://x/api", client=client, retries=0, provider=provider) == {}
    client, _ = _client([httpx.ReadTimeout("t")])
    with pytest.raises(ProviderError):
        get_json("https://x/api", client=client, retries=0, provider=provider)
    assert jhttp.circuit_open_until(provider) is None


def test_4xx_does_not_trip_circuit(monkeypatch) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)
    for _ in range(jhttp.CIRCUIT_FAILURE_THRESHOLD + 1):
        client, _ = _client([httpx.Response(404, content=b"{}")])
        with pytest.raises(ProviderError):
            get_json("https://x/api", client=client, retries=0, provider="auth")
    assert jhttp.circuit_open_until("auth") is None


def test_exchange_signed_get_retries_with_fresh_signature() -> None:
    from jarvise_exchange.binance_spot import BinanceAuth, BinanceSpotClient

    auth = BinanceAuth(api_key="k", hmac_secret="s")
    client, seen = _client(
        [
            httpx.Response(503, content=b""),
            httpx.Response(
                200, content=json.dumps({"balances": [{"asset": "BTC", "free": "1", "locked": "0"}]}).encode()
            ),
        ]
    )
    balances = BinanceSpotClient(auth, client=client).list_spot_balances()
    assert [b.asset for b in balances] == ["BTC"]
    assert len(seen) == 2
    assert all(r.headers.get("X-MBX-APIKEY") == "k" for r in seen)
    assert all("signature=" in str(r.url) for r in seen)
