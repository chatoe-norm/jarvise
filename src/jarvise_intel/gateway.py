"""Authenticated Hermes /v1/runs client for the ingest adapter."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

TERMINAL_OK = frozenset({"completed"})
# Verified terminal failures only. Gateway status "unknown" is non-terminal (F2).
TERMINAL_FAIL = frozenset(
    {
        "failed",
        "cancelled",
        "interrupted",
        "incomplete",
        "error",
    }
)
DEFAULT_POLL_S = 2.0


@dataclass(frozen=True)
class GatewayConfig:
    base_url: str
    api_key: str
    provider: str
    model: str
    deadline_s: float = 600.0
    poll_s: float = DEFAULT_POLL_S


@dataclass(frozen=True)
class RunResult:
    run_id: str
    status: str
    output: str
    usage: dict[str, Any]
    runtime: dict[str, Any]
    raw: dict[str, Any]


class GatewayError(Exception):
    def __init__(self, category: str, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.category = category
        self.status_code = status_code


HttpOpener = Callable[[urllib.request.Request, float], Any]


def _default_open(request: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.urlopen(request, timeout=timeout)


def _auth_headers(api_key: str, *, idempotency_key: str | None = None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def create_run(
    cfg: GatewayConfig,
    *,
    instructions: str,
    input_text: str,
    idempotency_key: str,
    open_url: HttpOpener | None = None,
) -> str:
    opener = open_url or _default_open
    body = {
        "input": input_text,
        "instructions": instructions,
        "provider": cfg.provider,
        "model": cfg.model,
    }
    request = urllib.request.Request(
        f"{cfg.base_url.rstrip('/')}/v1/runs",
        data=json.dumps(body).encode("utf-8"),
        headers=_auth_headers(cfg.api_key, idempotency_key=idempotency_key),
        method="POST",
    )
    try:
        with opener(request, min(30.0, cfg.deadline_s)) as resp:
            status = getattr(resp, "status", 200)
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise GatewayError("auth_error", f"HTTP {exc.code}", status_code=exc.code) from exc
        raise GatewayError("provider_error", f"HTTP {exc.code}", status_code=exc.code) from exc
    except urllib.error.URLError as exc:
        raise GatewayError("provider_error", type(exc).__name__) from exc
    if status >= 400:
        raise GatewayError("provider_error", f"HTTP {status}", status_code=status)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GatewayError("malformed_response", "create_run_json") from exc
    run_id = str(payload.get("run_id") or "").strip()
    if not run_id:
        raise GatewayError("malformed_response", "missing_run_id")
    return run_id


def get_run(
    cfg: GatewayConfig,
    run_id: str,
    *,
    open_url: HttpOpener | None = None,
    timeout: float = 30.0,
) -> RunResult:
    opener = open_url or _default_open
    request = urllib.request.Request(
        f"{cfg.base_url.rstrip('/')}/v1/runs/{run_id}",
        headers=_auth_headers(cfg.api_key),
        method="GET",
    )
    try:
        with opener(request, timeout) as resp:
            status_code = getattr(resp, "status", 200)
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise GatewayError("auth_error", f"HTTP {exc.code}", status_code=exc.code) from exc
        raise GatewayError("provider_error", f"HTTP {exc.code}", status_code=exc.code) from exc
    except urllib.error.URLError as exc:
        raise GatewayError("provider_error", type(exc).__name__) from exc
    if status_code >= 400:
        raise GatewayError("provider_error", f"HTTP {status_code}", status_code=status_code)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GatewayError("malformed_response", "get_run_json") from exc
    if not isinstance(payload, dict):
        raise GatewayError("malformed_response", "get_run_not_object")
    status = str(payload.get("status") or "").strip().lower()
    output = payload.get("output")
    if output is None:
        output_text = ""
    elif isinstance(output, str):
        output_text = output
    else:
        output_text = json.dumps(output)
    usage_raw = payload.get("usage")
    runtime_raw = payload.get("runtime")
    usage: dict[str, Any] = usage_raw if isinstance(usage_raw, dict) else {}
    runtime: dict[str, Any] = runtime_raw if isinstance(runtime_raw, dict) else {}
    return RunResult(
        run_id=str(payload.get("run_id") or run_id),
        status=status,
        output=output_text,
        usage=usage,
        runtime=runtime,
        raw=payload,
    )


def stop_run(
    cfg: GatewayConfig,
    run_id: str,
    *,
    open_url: HttpOpener | None = None,
    timeout: float = 30.0,
) -> RunResult | None:
    """Request stop via POST /v1/runs/{id}/stop. Returns parsed body when available."""
    opener = open_url or _default_open
    request = urllib.request.Request(
        f"{cfg.base_url.rstrip('/')}/v1/runs/{run_id}/stop",
        data=b"{}",
        headers=_auth_headers(cfg.api_key),
        method="POST",
    )
    try:
        with opener(request, timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise GatewayError("auth_error", f"HTTP {exc.code}", status_code=exc.code) from exc
        # Stop is best-effort; caller will GET to verify.
        return None
    except urllib.error.URLError:
        return None
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    status = str(payload.get("status") or "").strip().lower()
    return RunResult(
        run_id=str(payload.get("run_id") or run_id),
        status=status or "unknown",
        output="",
        usage={},
        runtime={},
        raw=payload,
    )


def normalize_runtime_identity(provider: str, model: str) -> tuple[str, str]:
    """Exact normalized identity: provider + model, or provider/model form of the pin."""
    prov = (provider or "").strip().lower()
    mod = (model or "").strip()
    return prov, mod


def assert_runtime_identity(cfg: GatewayConfig, runtime: dict[str, Any]) -> None:
    """Fail closed when actual provider/model evidence is missing or mismatched."""
    if not isinstance(runtime, dict) or not runtime:
        raise GatewayError("provider_mismatch", "runtime_missing")
    provider = str(runtime.get("provider") or "").strip()
    model = str(runtime.get("model") or "").strip()
    if not provider:
        raise GatewayError("provider_mismatch", "provider_missing")
    if not model:
        raise GatewayError("model_mismatch", "model_missing")
    want_p, want_m = normalize_runtime_identity(cfg.provider, cfg.model)
    got_p, got_m = normalize_runtime_identity(provider, model)
    if got_p != want_p:
        raise GatewayError("provider_mismatch", provider)
    allowed_models = {want_m, f"{want_p}/{want_m}"}
    if got_m not in allowed_models and got_m.lower() not in {m.lower() for m in allowed_models}:
        raise GatewayError("model_mismatch", model)
    requested = runtime.get("requested")
    if isinstance(requested, dict) and requested:
        req_p = str(requested.get("provider") or "").strip()
        req_m = str(requested.get("model") or "").strip()
        if req_p and normalize_runtime_identity(req_p, "")[0] != want_p:
            raise GatewayError("provider_mismatch", f"requested:{req_p}")
        if req_m and req_m not in allowed_models and req_m.lower() not in {m.lower() for m in allowed_models}:
            raise GatewayError("model_mismatch", f"requested:{req_m}")


def finalize_completed(cfg: GatewayConfig, run: RunResult) -> RunResult:
    """Shared completed-result gate for wait_for_run and resume (F1 / R8).

    Rejects budget-exhausted and incomplete results before identity checks.
    Never substitutes configured/requested identity for missing actual runtime.
    """
    if run.status not in TERMINAL_OK:
        raise GatewayError("incomplete_run", run.status or "not_completed")
    error_code = str(run.raw.get("hermes.error_code") or run.raw.get("error_code") or "")
    if error_code == "iteration_budget_exhausted":
        raise GatewayError("incomplete_run", "iteration_budget_exhausted")
    assert_runtime_identity(cfg, run.runtime)
    return run


def wait_for_run(
    cfg: GatewayConfig,
    run_id: str,
    *,
    open_url: HttpOpener | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> RunResult:
    deadline = monotonic() + cfg.deadline_s
    last: RunResult | None = None
    while monotonic() < deadline:
        last = get_run(cfg, run_id, open_url=open_url, timeout=min(30.0, max(1.0, deadline - monotonic())))
        if last.status in TERMINAL_OK:
            return finalize_completed(cfg, last)
        if last.status in TERMINAL_FAIL:
            raise GatewayError("incomplete_run", last.status)
        sleep(cfg.poll_s)
    # Deadline: request stop, then verify resulting state.
    stop_run(cfg, run_id, open_url=open_url, timeout=min(15.0, cfg.deadline_s))
    try:
        final = get_run(cfg, run_id, open_url=open_url, timeout=min(15.0, cfg.deadline_s))
    except GatewayError:
        raise GatewayError("unknown_run", "stop_unverified") from None
    if final.status in TERMINAL_OK:
        return finalize_completed(cfg, final)
    if final.status in TERMINAL_FAIL:
        raise GatewayError("incomplete_run", final.status)
    raise GatewayError("unknown_run", final.status or "deadline_exceeded")


def run_research(
    cfg: GatewayConfig,
    *,
    instructions: str,
    input_text: str,
    idempotency_key: str,
    open_url: HttpOpener | None = None,
    sleep: Callable[[float], None] = time.sleep,
    on_created: Callable[[str], None] | None = None,
) -> RunResult:
    run_id = create_run(
        cfg,
        instructions=instructions,
        input_text=input_text,
        idempotency_key=idempotency_key,
        open_url=open_url,
    )
    if on_created is not None:
        on_created(run_id)
    return wait_for_run(cfg, run_id, open_url=open_url, sleep=sleep)
