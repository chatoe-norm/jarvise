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
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status = status
        self.retryable = retryable
        self.attempts = list(attempts or [])


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


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
                attempts.append(f"HTTP {status}")
                raise ProviderError(
                    f"{provider or 'http'} GET failed: HTTP {status} {resp.text[:160]}",
                    provider=provider,
                    status=status,
                    retryable=False,
                    attempts=attempts,
                )
            try:
                return resp.json()
            except ValueError as exc:
                raise ProviderError(
                    f"{provider or 'http'} GET returned invalid JSON",
                    provider=provider,
                    status=status,
                    retryable=False,
                    attempts=attempts,
                ) from exc
    finally:
        if own:
            http.close()
    raise ProviderError(f"{provider or 'http'} GET exhausted retries", provider=provider, attempts=attempts)
