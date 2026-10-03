"""Shared GET-only HTTP layer for market-data providers and exchange reads.

Retries transient failures (429 / 418 / 5xx / timeouts / transport errors) with
exponential backoff + jitter and honors ``Retry-After``. Never retries 4xx auth or
client errors. Never used for order placement: the trade POST stays single-shot.
"""

from __future__ import annotations

import os
import random
import time
from collections.abc import Callable
from typing import Any

import httpx

RETRY_STATUSES = frozenset({418, 429, 500, 502, 503, 504})
DEFAULT_RETRIES = 3
DEFAULT_TIMEOUT_S = 30.0
BACKOFF_BASE_S = 0.5
BACKOFF_CAP_S = 8.0
RETRY_AFTER_CAP_S = 60.0
# Circuit breaker: after this many consecutive exhausted failures per provider, skip the
# provider for CIRCUIT_OPEN_S so a dead upstream cannot stall every ingest run.
CIRCUIT_FAILURE_THRESHOLD = 5
CIRCUIT_OPEN_S = 15 * 60
CIRCUIT_FAILURE_WINDOW_S = 30 * 60

UrlLike = str | Callable[[], str]


class ProviderError(RuntimeError):
    """A GET failed after retries (or immediately on a non-retryable status)."""

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        status: int | None = None,
        retryable: bool = False,
        attempts: list[str] | None = None,
        circuit_open: bool = False,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status = status
        self.retryable = retryable
        self.attempts = list(attempts or [])
        self.circuit_open = circuit_open


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


# ---- circuit breaker state (Redis when available, else in-process) -------------------

_LOCAL_FAILURES: dict[str, int] = {}
_LOCAL_OPEN_UNTIL: dict[str, float] = {}


def _breaker_redis() -> Any | None:
    url = os.environ.get("REDIS_URL")
    if not url:
        return None
    try:
        import redis

        return redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=2, socket_timeout=2)
    except Exception:  # noqa: BLE001 — breaker is best-effort; never block a fetch on Redis
        return None


def circuit_open_until(provider: str) -> float | None:
    """Unix seconds until which the provider circuit is open, or None when closed."""
    if not provider:
        return None
    client = _breaker_redis()
    if client is not None:
        try:
            raw = client.get(f"jarvise:circuit:{provider}:open_until")
            if raw:
                until = float(raw)
                return until if until > time.time() else None
            return None
        except Exception:  # noqa: BLE001
            pass
    until = _LOCAL_OPEN_UNTIL.get(provider)
    if until is None or until <= time.time():
        return None
    return until


def record_provider_success(provider: str) -> None:
    if not provider:
        return
    client = _breaker_redis()
    if client is not None:
        try:
            client.delete(f"jarvise:circuit:{provider}:failures")
            return
        except Exception:  # noqa: BLE001
            pass
    _LOCAL_FAILURES.pop(provider, None)


def record_provider_failure(provider: str) -> int:
    """Count a consecutive exhausted failure; open the circuit at the threshold. Returns count."""
    if not provider:
        return 0
    client = _breaker_redis()
    count: int
    if client is not None:
        try:
            key = f"jarvise:circuit:{provider}:failures"
            count = int(client.incr(key))
            client.expire(key, CIRCUIT_FAILURE_WINDOW_S)
            if count >= CIRCUIT_FAILURE_THRESHOLD:
                until = time.time() + CIRCUIT_OPEN_S
                client.set(f"jarvise:circuit:{provider}:open_until", f"{until:.3f}", ex=CIRCUIT_OPEN_S)
                client.delete(key)
            return count
        except Exception:  # noqa: BLE001
            pass
    count = _LOCAL_FAILURES.get(provider, 0) + 1
    _LOCAL_FAILURES[provider] = count
    if count >= CIRCUIT_FAILURE_THRESHOLD:
        _LOCAL_OPEN_UNTIL[provider] = time.time() + CIRCUIT_OPEN_S
        _LOCAL_FAILURES.pop(provider, None)
    return count


def reset_circuit(provider: str) -> None:
    """Close the circuit and clear counters (ops / tests)."""
    client = _breaker_redis()
    if client is not None:
        try:
            client.delete(f"jarvise:circuit:{provider}:failures", f"jarvise:circuit:{provider}:open_until")
        except Exception:  # noqa: BLE001
            pass
    _LOCAL_FAILURES.pop(provider, None)
    _LOCAL_OPEN_UNTIL.pop(provider, None)


def default_retries() -> int:
    raw = os.environ.get("JARVISE_HTTP_RETRIES")
    if raw is None or not raw.strip():
        return DEFAULT_RETRIES
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_RETRIES


def retry_after_seconds(resp: httpx.Response) -> float | None:
    """Parse Retry-After (delta-seconds only; HTTP-date is ignored → backoff)."""
    raw = resp.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return min(RETRY_AFTER_CAP_S, max(0.0, float(raw.strip())))
    except ValueError:
        return None


def backoff_delay(attempt: int, *, base: float = BACKOFF_BASE_S, cap: float = BACKOFF_CAP_S) -> float:
    """Exponential backoff with up to 25% jitter; attempt is 0-based."""
    delay = min(cap, base * (2**attempt))
    return delay + random.uniform(0.0, delay * 0.25)


def get_json(
    url: UrlLike,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    client: httpx.Client | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    retries: int | None = None,
    provider: str = "",
) -> Any:
    """GET and parse JSON with retry/backoff.

    ``url`` may be a callable so signed URLs (timestamp + signature) are rebuilt per
    attempt and never fall outside the venue's recvWindow after a backoff sleep.
    """
    max_retries = default_retries() if retries is None else max(0, int(retries))
    open_until = circuit_open_until(provider)
    if open_until is not None:
        remaining = max(0, int(open_until - time.time()))
        raise ProviderError(
            f"{provider} circuit_open: skipping for {remaining}s after repeated failures",
            provider=provider,
            retryable=True,
            circuit_open=True,
        )
    own = client is None
    http = client or httpx.Client(timeout=timeout)
    attempts: list[str] = []
    try:
        for attempt in range(max_retries + 1):
            target = url() if callable(url) else url
            try:
                resp = http.get(target, params=params, headers=headers)
            except httpx.HTTPError as exc:
                attempts.append(f"{type(exc).__name__}")
                if attempt >= max_retries:
                    record_provider_failure(provider)
                    raise ProviderError(
                        f"{provider or 'http'} GET failed after {attempt + 1} attempt(s): {exc}",
                        provider=provider,
                        retryable=True,
                        attempts=attempts,
                    ) from exc
                _sleep(backoff_delay(attempt))
                continue

            status = resp.status_code
            if status in RETRY_STATUSES:
                attempts.append(f"HTTP {status}")
                if attempt >= max_retries:
                    record_provider_failure(provider)
                    raise ProviderError(
                        f"{provider or 'http'} GET failed after {attempt + 1} attempt(s): HTTP {status}",
                        provider=provider,
                        status=status,
                        retryable=True,
                        attempts=attempts,
                    )
                delay = retry_after_seconds(resp)
                _sleep(delay if delay is not None else backoff_delay(attempt))
                continue
            if status >= 400:
                # Client/auth errors are not upstream outages: no breaker count.
                attempts.append(f"HTTP {status}")
                raise ProviderError(
                    f"{provider or 'http'} GET failed: HTTP {status} {resp.text[:160]}",
                    provider=provider,
                    status=status,
                    retryable=False,
                    attempts=attempts,
                )
            try:
                payload = resp.json()
            except ValueError as exc:
                raise ProviderError(
                    f"{provider or 'http'} GET returned invalid JSON",
                    provider=provider,
                    status=status,
                    retryable=False,
                    attempts=attempts,
                ) from exc
            record_provider_success(provider)
            return payload
    finally:
        if own:
            http.close()
    raise ProviderError(f"{provider or 'http'} GET exhausted retries", provider=provider, attempts=attempts)
