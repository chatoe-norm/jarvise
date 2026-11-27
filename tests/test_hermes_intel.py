"""Hermes ingest adapter, egress policy, and publication tests. No network."""

from __future__ import annotations

import io
import ipaddress
import json
import os
import socket
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

import pytest

from jarvise_intel.app import AdapterConfig, handle_ingest, redact
from jarvise_intel.digest import (
    BANGKOK,
    assert_current_slot,
    extract_json_object,
    parse_slot,
    publish_digest,
    release_inflight,
    render_markdown,
    try_acquire_inflight,
    validate_items,
)
from jarvise_intel.egress import connect_validated
from jarvise_intel.gateway import (
    GatewayConfig,
    GatewayError,
    assert_runtime_identity,
    create_run,
    wait_for_run,
)
from jarvise_intel.policy import (
    allow_connect_target,
    allow_https_url,
    allow_redirect_url,
    allow_source_url,
    is_blocked_ip,
    validate_host_addrs,
)

# Stage B regressions appended below existing cases.

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "hermes_v0.23.4_run.json"
SOURCE_POLICY = ROOT / "config" / "hermes" / "source-policy.json"


def _public_resolve(_host: str) -> list[str]:
    return ["8.8.8.8"]


def _footer_in(md: str) -> bool:
    return "Not a trade signal. Research context only." in md


def _slot_now(label: str) -> datetime:
    """Wall clock inside the Bangkok window for *label* (YYYY-MM-DDTHH)."""
    day, hour_s = label.split("T", 1)
    hour = int(hour_s)
    # Mid-window: 07→12:00, 19→22:00 Bangkok.
    mid = 12 if hour == 7 else 22
    return datetime.fromisoformat(f"{day}T{mid:02d}:00:00").replace(tzinfo=BANGKOK)


def test_policy_blocks_private_and_metadata() -> None:
    assert allow_https_url("http://example.com/")[0] is False
    assert allow_https_url("https://user:pass@example.com/")[0] is False
    assert allow_https_url("https://example.com:8443/")[0] is False
    assert allow_https_url("https://127.0.0.1/")[0] is False
    assert allow_https_url("https://10.0.0.5/")[0] is False
    assert allow_https_url("https://169.254.169.254/")[0] is False
    assert allow_https_url("https://76.13.218.48/")[0] is False
    assert allow_https_url("https://100.89.171.36/")[0] is False
    assert is_blocked_ip(ipaddress.ip_address("fc00::1")) is True


def test_policy_dns_rebinding_denied() -> None:
    def resolve(_host: str) -> list[str]:
        return ["8.8.8.8", "127.0.0.1"]

    ok, reason = allow_https_url("https://evil.example/", resolve=resolve)
    assert ok is False
    assert reason == "blocked_dns"
    assert allow_connect_target("evil.example", 443, resolve=resolve)[0] is False
    assert allow_connect_target("evil.example", 80, resolve=lambda h: ["8.8.8.8"])[0] is False


def test_policy_ipv6_unique_local_denied() -> None:
    ok, reason, addrs = allow_connect_target("evil6.example", 443, resolve=lambda _h: ["fc00::1"])
    assert ok is False
    assert reason == "blocked_dns"
    assert addrs == []


def test_policy_redirect_checker() -> None:
    ok, reason = allow_redirect_url("https://nvidia.com/x", resolve=_public_resolve)
    assert ok is True
    assert reason == "ok"
    bad, why = allow_redirect_url("https://127.0.0.1/x")
    assert bad is False
    assert why == "blocked_ip"


def test_policy_allows_public_https() -> None:
    ok, reason = allow_https_url("https://nvidia.com/news", resolve=_public_resolve)
    assert ok is True
    assert reason == "ok"


def test_connect_validated_uses_first_lookup_not_rebinding() -> None:
    """R2: first resolve 8.8.8.8; a later resolve must not be consulted for connect."""
    lookups = {"n": 0}

    def resolve(_host: str) -> list[str]:
        lookups["n"] += 1
        if lookups["n"] == 1:
            return ["8.8.8.8"]
        return ["127.0.0.1"]

    addrs, reason = validate_host_addrs("rebinding.example", resolve=resolve)
    assert reason == "ok"
    assert addrs == ["8.8.8.8"]

    connected: list[tuple[str, int]] = []

    class _FakeSock:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            self._peer = ("8.8.8.8", 443)

        def settimeout(self, _t: float) -> None:
            return None

        def connect(self, endpoint: tuple[str, int]) -> None:
            connected.append(endpoint)
            if endpoint[0] != "8.8.8.8":
                raise OSError("refused")
            self._peer = (endpoint[0], endpoint[1])

        def getpeername(self) -> tuple[str, int]:
            return self._peer

        def close(self) -> None:
            return None

    orig = socket.socket
    try:
        socket.socket = lambda *a, **k: _FakeSock()  # type: ignore[assignment, misc]
        sock = connect_validated(addrs, 443, timeout=1.0)
        sock.close()
    finally:
        socket.socket = orig  # type: ignore[misc]
    assert connected == [("8.8.8.8", 443)]
    # A later lookup that returns only loopback is denied; connect must never use it.
    later, later_reason = validate_host_addrs("rebinding.example", resolve=resolve)
    assert later == []
    assert later_reason == "blocked_dns"


def test_parse_slot_bangkok_to_utc_filename() -> None:
    morning = parse_slot("2026-11-26T07")
    assert morning.filename == "2026-11-26-00.md"
    evening = parse_slot("2026-11-26T19")
    assert morning.filename == "2026-11-26-00.md"
    assert evening.filename == "2026-11-26-12.md"
    with pytest.raises(ValueError):
        parse_slot("2026-11-26T08")
    with pytest.raises(ValueError):
        parse_slot("smoke-2026-11-26T07")
    smoke = parse_slot("smoke-2026-11-26T07", smoke_allowed=True)
    assert smoke.smoke is True
    assert smoke.filename.startswith("smoke-")


def test_assert_current_slot_window() -> None:
    morning = parse_slot("2026-11-26T07")
    assert_current_slot(morning, now=_slot_now("2026-11-26T07"))
    with pytest.raises(ValueError, match="slot_not_current"):
        assert_current_slot(morning, now=_slot_now("2026-11-26T19"))
    with pytest.raises(ValueError, match="slot_not_current"):
        assert_current_slot(parse_slot("2000-01-01T07"), now=_slot_now("2026-11-26T07"))
    with pytest.raises(ValueError, match="slot_not_current"):
        assert_current_slot(parse_slot("2099-01-01T19"), now=_slot_now("2026-11-26T19"))


def test_validate_and_render_matches_parser() -> None:
    from jarvise_context.news_sources import parse_digest_text

    payload = {
        "items": [
            {
                "title": "NVIDIA — $250M UK compute donation",
                "day": "2026-10-14",
                "url": "https://nvidia.com/news",
                "comment": "Primary release: UK AI research safety spend.",
            },
            {
                "title": "US dollar soft as focus turns to CPI and Fed speakers",
                "day": "2026-10-14",
                "url": "https://cnbc.com/news",
                "comment": "CNBC: markets await CPI.",
            },
        ]
    }
    items, no_news = validate_items(
        payload,
        resolve=_public_resolve,
        policy_path=SOURCE_POLICY,
        slot=parse_slot("2026-10-15T07"),
    )
    assert no_news is False
    md = render_markdown(items)
    parsed = parse_digest_text(md)
    assert len(parsed) == 2
    assert "NVIDIA" in parsed[0]["title"]
    assert parsed[0]["url"] == "https://nvidia.com/news"
    assert _footer_in(md)


def test_validate_rejects_bad_url_and_empty() -> None:
    with pytest.raises(ValueError, match="empty_items"):
        validate_items({"items": []}, policy_path=SOURCE_POLICY)
    with pytest.raises(ValueError, match="url_denied"):
        validate_items(
            {
                "items": [
                    {
                        "title": "x",
                        "day": "2026-11-26",
                        "url": "https://127.0.0.1/x",
                        "comment": "c",
                    }
                ]
            },
            policy_path=SOURCE_POLICY,
            slot=parse_slot("2026-11-26T07"),
        )
    with pytest.raises(ValueError, match="bad_timestamp"):
        validate_items(
            {
                "items": [
                    {
                        "title": "x",
                        "day": "2026-99-99",
                        "url": "https://nvidia.com/x",
                        "comment": "c",
                    }
                ]
            },
            resolve=_public_resolve,
            policy_path=SOURCE_POLICY,
        )


def test_validate_no_news_must_be_bool() -> None:
    with pytest.raises(ValueError, match="no_news_type"):
        validate_items({"no_news": "false", "items": []}, policy_path=SOURCE_POLICY)
    items, no_news = validate_items({"no_news": True, "items": []}, policy_path=SOURCE_POLICY)
    assert no_news is True
    assert items == []


def test_validate_unknown_source_and_shortener() -> None:
    with pytest.raises(ValueError, match="unknown_source"):
        validate_items(
            {
                "items": [
                    {
                        "title": "x",
                        "day": "2026-10-14",
                        "url": "https://example.com/x",
                        "comment": "c",
                    }
                ]
            },
            resolve=_public_resolve,
            policy_path=SOURCE_POLICY,
            slot=parse_slot("2026-10-15T07"),
        )
    with pytest.raises(ValueError, match="denied"):
        validate_items(
            {
                "items": [
                    {
                        "title": "x",
                        "day": "2026-10-14",
                        "url": "https://bit.ly/abc",
                        "comment": "c",
                    }
                ]
            },
            resolve=_public_resolve,
            policy_path=SOURCE_POLICY,
            slot=parse_slot("2026-10-15T07"),
        )
    assert allow_source_url(
        "https://federalreserve.gov/a", resolve=_public_resolve, policy_path=SOURCE_POLICY
    )[0]


def test_no_news_digest() -> None:
    items, no_news = validate_items({"no_news": True, "items": []}, policy_path=SOURCE_POLICY)
    assert no_news is True
    md = render_markdown(items, no_news=True)
    assert "No confirmed watchlist coverage" in md
    assert _footer_in(md)


def test_atomic_publish_idempotent_and_conflict(tmp_path: Path) -> None:
    slot = parse_slot("2026-11-26T07")
    digest_dir = tmp_path / "digests"
    state_dir = tmp_path / "state"
    md = render_markdown(
        validate_items(
            {
                "items": [
                    {
                        "title": "One",
                        "day": "2026-11-26",
                        "url": "https://nvidia.com/a",
                        "comment": "note",
                    }
                ]
            },
            resolve=_public_resolve,
            policy_path=SOURCE_POLICY,
            slot=slot,
        )[0]
    )
    first = publish_digest(
        digest_dir=digest_dir,
        state_dir=state_dir,
        slot=slot,
        markdown=md,
        meta={"provider": "openrouter", "model": "test"},
    )
    assert first["status"] == "completed"
    assert (digest_dir / slot.filename).is_file()
    incumbent_mtime = (digest_dir / slot.filename).stat().st_mtime
    second = publish_digest(
        digest_dir=digest_dir,
        state_dir=state_dir,
        slot=slot,
        markdown=md,
        meta={"provider": "openrouter", "model": "test"},
    )
    assert second.get("replay") is True
    third = publish_digest(
        digest_dir=digest_dir,
        state_dir=state_dir,
        slot=slot,
        markdown=md + "\nextra\n",
        meta={},
    )
    assert third.get("replay") is True
    assert third["output_hash"] == first["output_hash"]
    assert (digest_dir / slot.filename).stat().st_mtime == incumbent_mtime

    other = parse_slot("2026-11-27T07")
    (digest_dir / other.filename).write_text("foreign digest\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        publish_digest(
            digest_dir=digest_dir,
            state_dir=state_dir,
            slot=other,
            markdown=md,
            meta={},
        )


def test_concurrent_publish_once(tmp_path: Path) -> None:
    slot = parse_slot("2026-11-26T19")
    digest_dir = tmp_path / "digests"
    state_dir = tmp_path / "state"
    md = "# Digest\n\nNo confirmed watchlist coverage for this slot.\n\nNot a trade signal. Research context only.\n"
    results: list[dict[str, Any]] = []

    def worker() -> None:
        results.append(
            publish_digest(
                digest_dir=digest_dir,
                state_dir=state_dir,
                slot=slot,
                markdown=md,
                meta={},
            )
        )

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 8
    assert all(r["output_hash"] == results[0]["output_hash"] for r in results)
    assert len(list(digest_dir.glob("*.md"))) == 1


def test_path_traversal_rejected(tmp_path: Path) -> None:
    class Evil:
        label = "evil"
        filename = "../escape.md"
        idempotency_key = "x"

    with pytest.raises(ValueError, match="path_traversal"):
        publish_digest(
            digest_dir=tmp_path / "d",
            state_dir=tmp_path / "s",
            slot=Evil(),  # type: ignore[arg-type]
            markdown="x",
            meta={},
        )


def test_newest_digest_prefers_mtime_not_name(tmp_path: Path) -> None:
    from jarvise_paper.shadow_veto import newest_digest

    digest = tmp_path / "digests"
    digest.mkdir()
    older_name = digest / "2026-11-26-13.md"
    newer_name = digest / "2026-11-26-12.md"
    older_name.write_text("old name newer mtime\n", encoding="utf-8")
    newer_name.write_text("new name older mtime\n", encoding="utf-8")
    now = time.time()
    os.utime(newer_name, (now - 100, now - 100))
    os.utime(older_name, (now - 10, now - 10))
    name, _sha, _text = newest_digest(digest, as_of_ms=int(now * 1000))
    assert name == "2026-11-26-13.md"


class _Resp:
    def __init__(self, body: dict[str, Any], status: int = 200) -> None:
        self.status = status
        self._raw = json.dumps(body).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *args: Any) -> None:
        return None


def test_gateway_auth_and_incomplete() -> None:
    cfg = GatewayConfig(
        base_url="http://hermes:8642",
        api_key="k",
        provider="openrouter",
        model="test-model",
        deadline_s=1.0,
        poll_s=0.01,
    )

    def open_401(request: Any, timeout: float) -> Any:
        raise HTTPError(request.full_url, 401, "no", hdrs=None, fp=io.BytesIO(b"{}"))  # type: ignore[arg-type]

    with pytest.raises(GatewayError) as exc:
        create_run(cfg, instructions="i", input_text="u", idempotency_key="id", open_url=open_401)
    assert exc.value.category == "auth_error"

    def open_incomplete(request: Any, timeout: float) -> Any:
        if "POST" in str(getattr(request, "method", "GET")) or request.get_method() == "POST":
            return _Resp({"run_id": "r1"})
        return _Resp(
            {
                "run_id": "r1",
                "status": "completed",
                "output": "{}",
                "finish_reason": "length",
                "hermes.error_code": "iteration_budget_exhausted",
                "runtime": {"provider": "openrouter", "model": "test-model"},
            }
        )

    run_id = create_run(
        cfg, instructions="i", input_text="u", idempotency_key="id2", open_url=open_incomplete
    )
    assert run_id == "r1"
    with pytest.raises(GatewayError) as incomplete:
        wait_for_run(cfg, run_id, open_url=open_incomplete, sleep=lambda _s: None)
    assert incomplete.value.category == "incomplete_run"


def test_gateway_timeout() -> None:
    cfg = GatewayConfig(
        base_url="http://hermes:8642",
        api_key="k",
        provider="openrouter",
        model="m",
        deadline_s=0.05,
        poll_s=0.01,
    )

    def open_running(request: Any, timeout: float) -> Any:
        url = str(getattr(request, "full_url", "") or request.get_full_url())
        method = request.get_method()
        if method == "POST" and url.rstrip("/").endswith("/v1/runs"):
            return _Resp({"run_id": "r2"})
        if method == "POST" and url.rstrip("/").endswith("/stop"):
            return _Resp({"run_id": "r2", "status": "running"})
        return _Resp({"run_id": "r2", "status": "running", "output": ""})

    with pytest.raises(GatewayError) as exc:
        wait_for_run(
            cfg,
            create_run(cfg, instructions="i", input_text="u", idempotency_key="t", open_url=open_running),
            open_url=open_running,
            sleep=lambda _s: None,
        )
    # Deadline stop leaves a non-terminal run → unknown_run (no silent second create).
    assert exc.value.category == "unknown_run"


def test_runtime_identity_exact_and_fixture() -> None:
    cfg = GatewayConfig(
        base_url="http://hermes:8642",
        api_key="k",
        provider="openrouter",
        model="anthropic/claude-sonnet-5.5",
    )
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert_runtime_identity(cfg, fixture["runtime"])
    with pytest.raises(GatewayError) as empty:
        assert_runtime_identity(cfg, {})
    assert empty.value.category == "provider_mismatch"
    with pytest.raises(GatewayError) as missing_model:
        assert_runtime_identity(cfg, {"provider": "openrouter"})
    assert missing_model.value.category == "model_mismatch"
    # endswith must not pass: pin is anthropic/claude-sonnet-5.5, not suffix match on a longer id.
    with pytest.raises(GatewayError) as suffix:
        assert_runtime_identity(
            cfg,
            {
                "provider": "openrouter",
                "model": "prefix/anthropic/claude-sonnet-5.5",
            },
        )
    assert suffix.value.category == "model_mismatch"
    with pytest.raises(GatewayError) as subst:
        assert_runtime_identity(
            cfg,
            {
                "provider": "openrouter",
                "model": "anthropic/claude-sonnet-5.5",
                "requested": {"provider": "openrouter", "model": "other/model"},
            },
        )
    assert subst.value.category == "model_mismatch"


def test_handle_ingest_success_and_replay(tmp_path: Path, monkeypatch: Any) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("fixed prompt", encoding="utf-8")
    digest_dir = tmp_path / "pilot-digests"
    state_dir = tmp_path / "adapter"
    monkeypatch.setenv("JARVISE_HERMES_INGEST_TOKEN", "ingest-token-12345678")
    monkeypatch.setenv("JARVISE_HERMES_API_KEY", "api-key-12345678")
    monkeypatch.setenv("HERMES_PROVIDER", "openrouter")
    monkeypatch.setenv("HERMES_MODEL", "test-model")
    monkeypatch.setenv("JARVISE_HERMES_DIGEST_DIR", str(digest_dir))
    monkeypatch.setenv("JARVISE_HERMES_STATE_DIR", str(state_dir))
    monkeypatch.setenv("JARVISE_HERMES_PROMPT_PATH", str(prompt))
    monkeypatch.setenv("JARVISE_HERMES_SOURCE_POLICY", str(SOURCE_POLICY))

    payload = {
        "items": [
            {
                "title": "Fed holds",
                "day": "2026-11-26",
                "url": "https://federalreserve.gov/a",
                "comment": "Primary statement.",
            }
        ]
    }

    def fake_run(*_a: Any, **_k: Any) -> Any:
        from jarvise_intel.gateway import RunResult

        return RunResult(
            run_id="run-1",
            status="completed",
            output=json.dumps(payload),
            usage={"total_tokens": 10},
            runtime={"provider": "openrouter", "model": "test-model"},
            raw={"status": "completed"},
        )

    monkeypatch.setattr("jarvise_intel.app.run_research", fake_run)
    cfg = AdapterConfig()
    cfg.now = _slot_now("2026-11-26T07")
    code, body = handle_ingest(cfg, {"slot": "2026-11-26T07"})
    assert code == 200
    assert body["ok"] is True
    assert (digest_dir / "2026-11-26-00.md").is_file()
    code2, body2 = handle_ingest(cfg, {"slot": "2026-11-26T07"})
    assert code2 == 200
    assert body2["replay"] is True


def test_handle_ingest_rejects_non_current_and_inflight(tmp_path: Path, monkeypatch: Any) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("p", encoding="utf-8")
    monkeypatch.setenv("JARVISE_HERMES_INGEST_TOKEN", "ingest-token-12345678")
    monkeypatch.setenv("JARVISE_HERMES_API_KEY", "api-key-12345678")
    monkeypatch.setenv("HERMES_PROVIDER", "openrouter")
    monkeypatch.setenv("HERMES_MODEL", "test-model")
    state_dir = tmp_path / "s"
    monkeypatch.setenv("JARVISE_HERMES_DIGEST_DIR", str(tmp_path / "pilot-digests"))
    monkeypatch.setenv("JARVISE_HERMES_STATE_DIR", str(state_dir))
    monkeypatch.setenv("JARVISE_HERMES_PROMPT_PATH", str(prompt))
    monkeypatch.setenv("JARVISE_HERMES_SOURCE_POLICY", str(SOURCE_POLICY))
    called = {"n": 0}

    def fake_run(*_a: Any, **_k: Any) -> Any:
        called["n"] += 1
        time.sleep(0.2)
        from jarvise_intel.gateway import RunResult

        return RunResult(
            run_id="r",
            status="completed",
            output=json.dumps({"no_news": True, "items": []}),
            usage={},
            runtime={"provider": "openrouter", "model": "test-model"},
            raw={},
        )

    monkeypatch.setattr("jarvise_intel.app.run_research", fake_run)
    cfg = AdapterConfig()
    cfg.now = _slot_now("2026-11-26T07")
    code, body = handle_ingest(cfg, {"slot": "2000-01-01T07"})
    assert code == 400
    assert body["error"] == "slot_not_current"
    assert called["n"] == 0

    assert try_acquire_inflight(state_dir) is True
    try:
        code2, body2 = handle_ingest(cfg, {"slot": "2026-11-26T07"})
        assert code2 == 429
        assert body2["error"] == "inflight_busy"
        assert called["n"] == 0
    finally:
        release_inflight()


def test_handle_ingest_unconfigured_and_auth_fields(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.delenv("JARVISE_HERMES_INGEST_TOKEN", raising=False)
    monkeypatch.delenv("JARVISE_HERMES_API_KEY", raising=False)
    monkeypatch.delenv("HERMES_PROVIDER", raising=False)
    monkeypatch.delenv("HERMES_MODEL", raising=False)
    cfg = AdapterConfig()
    code, body = handle_ingest(cfg, {"slot": "2026-11-26T07"})
    assert code == 503
    assert body["error"] == "provider_unconfigured"


def test_extract_json_and_redact() -> None:
    assert extract_json_object('```json\n{"no_news": true, "items": []}\n```')["no_news"] is True
    with pytest.raises(ValueError):
        extract_json_object("")
    assert "[REDACTED]" in redact("Authorization: Bearer supersecret")
    assert "[REDACTED]" in redact("api_key=sk-abc")


def test_malformed_gateway_output(tmp_path: Path, monkeypatch: Any) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("p", encoding="utf-8")
    monkeypatch.setenv("JARVISE_HERMES_INGEST_TOKEN", "ingest-token-12345678")
    monkeypatch.setenv("JARVISE_HERMES_API_KEY", "api-key-12345678")
    monkeypatch.setenv("HERMES_PROVIDER", "openrouter")
    monkeypatch.setenv("HERMES_MODEL", "test-model")
    monkeypatch.setenv("JARVISE_HERMES_DIGEST_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("JARVISE_HERMES_STATE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("JARVISE_HERMES_PROMPT_PATH", str(prompt))
    monkeypatch.setenv("JARVISE_HERMES_SOURCE_POLICY", str(SOURCE_POLICY))

    def fake_run(*_a: Any, **_k: Any) -> Any:
        from jarvise_intel.gateway import RunResult

        return RunResult(
            run_id="r",
            status="completed",
            output="not json at all",
            usage={},
            runtime={"provider": "openrouter", "model": "test-model"},
            raw={},
        )

    monkeypatch.setattr("jarvise_intel.app.run_research", fake_run)
    cfg = AdapterConfig()
    cfg.now = _slot_now("2026-11-26T19")
    code, body = handle_ingest(cfg, {"slot": "2026-11-26T19"})
    assert code == 502
    assert body["ok"] is False


def test_egress_http_non_connect_rejected() -> None:
    """HTTP non-CONNECT must not return 200 (R2)."""
    from jarvise_intel.egress import _ProxyHandler

    class _Srv:
        def __init__(self) -> None:
            self.RequestHandlerClass = type(
                "H",
                (_ProxyHandler,),
                {"resolve": staticmethod(_public_resolve)},
            )

    replies: list[int] = []

    class H(_ProxyHandler):
        resolve = staticmethod(_public_resolve)

        def __init__(self) -> None:  # noqa: ANN204 — test stub
            self.rfile = io.BytesIO(b"GET http://example.com/ HTTP/1.1\r\n\r\n")
            self.wfile = io.BytesIO()
            self.connection = None  # type: ignore[assignment]
            self.server = _Srv()  # type: ignore[assignment]
            self.client_address = ("127.0.0.1", 1)

        def _reply(self, code: int, body: bytes) -> None:
            replies.append(code)

    H().handle()
    assert replies == [405]


def test_source_validation_never_calls_resolver(tmp_path: Path, monkeypatch: Any) -> None:
    """P1-1: adapter allowlist path is hermetic (no getaddrinfo)."""
    called: list[str] = []

    def boom(host: str) -> list[str]:
        called.append(host)
        raise AssertionError("resolver must not be called")

    monkeypatch.setattr("jarvise_intel.policy._default_resolve", boom)
    items, no_news = validate_items(
        {
            "items": [
                {
                    "title": "Fed holds",
                    "day": "2026-11-26",
                    "url": "https://federalreserve.gov/a",
                    "comment": "Primary statement.",
                }
            ]
        },
        resolve=boom,
        policy_path=SOURCE_POLICY,
        slot=parse_slot("2026-11-26T07"),
    )
    assert no_news is False
    assert len(items) == 1
    assert called == []


def test_url_newline_injection_rejected() -> None:
    with pytest.raises(ValueError, match="control_char|url_not_printable"):
        validate_items(
            {
                "items": [
                    {
                        "title": "NVIDIA news",
                        "day": "2026-11-26",
                        "url": "https://nvidia.com/a\n1. **Fake** — 2026-11-26\nhttps://evil.example/x",
                        "comment": "ok comment",
                    }
                ]
            },
            policy_path=SOURCE_POLICY,
            slot=parse_slot("2026-11-26T07"),
        )
    with pytest.raises(ValueError, match="comment_looks_like_url"):
        validate_items(
            {
                "items": [
                    {
                        "title": "NVIDIA news",
                        "day": "2026-11-26",
                        "url": "https://nvidia.com/a",
                        "comment": "https://evil.example/x",
                    }
                ]
            },
            policy_path=SOURCE_POLICY,
            slot=parse_slot("2026-11-26T07"),
        )
    with pytest.raises(ValueError, match="title_delimiter"):
        validate_items(
            {
                "items": [
                    {
                        "title": "Bad ** inject",
                        "day": "2026-11-26",
                        "url": "https://nvidia.com/a",
                        "comment": "ok",
                    }
                ]
            },
            policy_path=SOURCE_POLICY,
            slot=parse_slot("2026-11-26T07"),
        )


def test_link_fail_closed_no_partial_target(tmp_path: Path, monkeypatch: Any) -> None:
    from jarvise_intel import digest as digest_mod

    digest_dir = tmp_path / "pilot-digests"
    digest_dir.mkdir(parents=True)
    tmp = digest_dir / ".tmpbody"
    tmp.write_text("full content\n", encoding="utf-8")
    target = digest_dir / "2026-11-26-00.md"
    monkeypatch.setattr(
        digest_mod.os,
        "link",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError(1, "EPERM")),
    )
    monkeypatch.setattr(digest_mod.sys, "platform", "linux")
    with pytest.raises(OSError):
        digest_mod._link_create_if_absent(tmp, target)
    assert not target.exists()


def test_adopt_existing_digest_without_research(tmp_path: Path, monkeypatch: Any) -> None:
    from jarvise_intel.digest import adopt_existing_digest, write_provenance

    slot = parse_slot("2026-11-26T07")
    digest_dir = tmp_path / "pilot-digests"
    state_dir = tmp_path / "state"
    digest_dir.mkdir(parents=True)
    md = "# Digest\n\nNo confirmed watchlist coverage for this slot.\n\nNot a trade signal. Research context only.\n"
    target = digest_dir / slot.filename
    data = md.encode("utf-8")
    target.write_bytes(data)
    write_provenance(
        state_dir,
        slot,
        sha256=__import__("hashlib").sha256(data).hexdigest(),
        byte_length=len(data),
    )
    called = {"n": 0}

    def fake_run(*_a: Any, **_k: Any) -> Any:
        called["n"] += 1
        raise AssertionError("must not call Hermes")

    monkeypatch.setenv("JARVISE_HERMES_INGEST_TOKEN", "ingest-token-12345678")
    monkeypatch.setenv("JARVISE_HERMES_API_KEY", "api-key-12345678")
    monkeypatch.setenv("HERMES_PROVIDER", "openrouter")
    monkeypatch.setenv("HERMES_MODEL", "test-model")
    monkeypatch.setenv("JARVISE_HERMES_DIGEST_DIR", str(digest_dir))
    monkeypatch.setenv("JARVISE_HERMES_STATE_DIR", str(state_dir))
    prompt = tmp_path / "p.md"
    prompt.write_text("p", encoding="utf-8")
    monkeypatch.setenv("JARVISE_HERMES_PROMPT_PATH", str(prompt))
    monkeypatch.setenv("JARVISE_HERMES_SOURCE_POLICY", str(SOURCE_POLICY))
    monkeypatch.setattr("jarvise_intel.app.run_research", fake_run)
    cfg = AdapterConfig()
    cfg.now = _slot_now("2026-11-26T07")
    code, body = handle_ingest(cfg, {"slot": "2026-11-26T07"})
    assert code == 200
    assert body.get("replay") is True
    assert called["n"] == 0
    record = adopt_existing_digest(digest_dir=digest_dir, state_dir=state_dir, slot=slot)
    assert record is not None
    assert record.get("status") == "completed"
    assert record.get("recovered") is True


def test_smoke_refuses_non_pilot_dir(tmp_path: Path, monkeypatch: Any) -> None:
    from jarvise_intel.digest import assert_smoke_confined

    slot = parse_slot("smoke-2026-11-26T07", smoke_allowed=True)
    with pytest.raises(ValueError, match="smoke_not_pilot_dir"):
        assert_smoke_confined(slot, tmp_path / "canonical" / "digests")
    assert_smoke_confined(slot, tmp_path / "data" / "hermes" / "pilot-digests")


def test_attempt_key_and_budget(tmp_path: Path, monkeypatch: Any) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("p", encoding="utf-8")
    digest_dir = tmp_path / "pilot-digests"
    state_dir = tmp_path / "adapter"
    monkeypatch.setenv("JARVISE_HERMES_INGEST_TOKEN", "ingest-token-12345678")
    monkeypatch.setenv("JARVISE_HERMES_API_KEY", "api-key-12345678")
    monkeypatch.setenv("HERMES_PROVIDER", "openrouter")
    monkeypatch.setenv("HERMES_MODEL", "test-model")
    monkeypatch.setenv("JARVISE_HERMES_DIGEST_DIR", str(digest_dir))
    monkeypatch.setenv("JARVISE_HERMES_STATE_DIR", str(state_dir))
    monkeypatch.setenv("JARVISE_HERMES_PROMPT_PATH", str(prompt))
    monkeypatch.setenv("JARVISE_HERMES_SOURCE_POLICY", str(SOURCE_POLICY))
    keys: list[str] = []

    def fake_run(*_a: Any, **kwargs: Any) -> Any:
        from jarvise_intel.gateway import GatewayError

        keys.append(str(kwargs.get("idempotency_key")))
        raise GatewayError("incomplete_run", "failed")

    monkeypatch.setattr("jarvise_intel.app.run_research", fake_run)
    cfg = AdapterConfig()
    cfg.now = _slot_now("2026-11-26T07")
    code1, body1 = handle_ingest(cfg, {"slot": "2026-11-26T07"})
    assert code1 == 502
    code2, body2 = handle_ingest(cfg, {"slot": "2026-11-26T07"})
    assert code2 == 502
    code3, body3 = handle_ingest(cfg, {"slot": "2026-11-26T07"})
    assert code3 == 409
    assert body3["error"] == "attempt_budget"
    assert keys[0].endswith("-a1")
    assert keys[1].endswith("-a2")
    assert len(keys) == 2


def test_ipv4_mapped_blocked() -> None:
    assert is_blocked_ip(ipaddress.ip_address("::ffff:127.0.0.1")) is True
    assert is_blocked_ip(ipaddress.ip_address("8.8.8.8")) is False


def _cfg_env(tmp_path: Path, monkeypatch: Any) -> tuple[Path, Path]:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("p", encoding="utf-8")
    digest_dir = tmp_path / "pilot-digests"
    state_dir = tmp_path / "adapter"
    monkeypatch.setenv("JARVISE_HERMES_INGEST_TOKEN", "ingest-token-12345678")
    monkeypatch.setenv("JARVISE_HERMES_API_KEY", "api-key-12345678")
    monkeypatch.setenv("HERMES_PROVIDER", "openrouter")
    monkeypatch.setenv("HERMES_MODEL", "test-model")
    monkeypatch.setenv("JARVISE_HERMES_DIGEST_DIR", str(digest_dir))
    monkeypatch.setenv("JARVISE_HERMES_STATE_DIR", str(state_dir))
    monkeypatch.setenv("JARVISE_HERMES_PROMPT_PATH", str(prompt))
    monkeypatch.setenv("JARVISE_HERMES_SOURCE_POLICY", str(SOURCE_POLICY))
    return digest_dir, state_dir


def test_authority_backslash_percent_semicolon_rejected() -> None:
    """F3: parser-differential authority must not pass the allowlist."""
    for url in (
        r"https://evil.com\.nvidia.com/x",
        "https://evil.com;.nvidia.com/x",
        "https://evil.com%2f.nvidia.com/x",
    ):
        ok, reason = allow_source_url(url, policy_path=SOURCE_POLICY)
        assert ok is False, (url, reason)
    ok_good, reason_good = allow_source_url("https://www.nvidia.com/a", policy_path=SOURCE_POLICY)
    assert ok_good is True, reason_good
    ok_fed, reason_fed = allow_source_url(
        "https://federalreserve.gov/newsevents/pressreleases.htm",
        policy_path=SOURCE_POLICY,
    )
    assert ok_fed is True, reason_fed


def test_resume_rejects_missing_runtime_no_publish(tmp_path: Path, monkeypatch: Any) -> None:
    """F1: resume completed without runtime must not publish and must not create."""
    from jarvise_intel.digest import write_slot_record
    from jarvise_intel.gateway import RunResult

    digest_dir, state_dir = _cfg_env(tmp_path, monkeypatch)
    slot = parse_slot("2026-11-26T07")
    write_slot_record(
        state_dir,
        slot,
        {
            "status": "running",
            "attempt": 1,
            "run_id": "run-resume",
            "idempotency_key": "2026-11-26T07-a1",
            "started_at": "2026-11-26T05:00:00+00:00",
        },
    )
    creates = {"n": 0}

    def fake_get(_cfg: Any, run_id: str, **_k: Any) -> RunResult:
        return RunResult(
            run_id=run_id,
            status="completed",
            output=json.dumps({"no_news": True, "items": []}),
            usage={},
            runtime={},
            raw={"status": "completed"},
        )

    def fake_create(*_a: Any, **_k: Any) -> str:
        creates["n"] += 1
        raise AssertionError("must not create")

    monkeypatch.setattr("jarvise_intel.app.get_run", fake_get)
    monkeypatch.setattr(
        "jarvise_intel.app.run_research", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError())
    )
    monkeypatch.setattr("jarvise_intel.gateway.create_run", fake_create)
    cfg = AdapterConfig()
    cfg.now = _slot_now("2026-11-26T07")
    code, body = handle_ingest(cfg, {"slot": "2026-11-26T07"})
    assert code == 502
    assert body["error"] == "provider_mismatch"
    assert not list(digest_dir.glob("*.md"))
    assert creates["n"] == 0
    # Second call blocked as invalid_result — no paid replacement.
    code2, body2 = handle_ingest(cfg, {"slot": "2026-11-26T07"})
    assert code2 == 409
    assert body2["error"] == "invalid_result"


def test_resume_rejects_budget_exhausted(tmp_path: Path, monkeypatch: Any) -> None:
    from jarvise_intel.digest import write_slot_record
    from jarvise_intel.gateway import RunResult

    digest_dir, state_dir = _cfg_env(tmp_path, monkeypatch)
    slot = parse_slot("2026-11-26T07")
    write_slot_record(
        state_dir,
        slot,
        {"status": "unknown", "attempt": 1, "run_id": "run-b", "idempotency_key": "2026-11-26T07-a1"},
    )

    def fake_get(_cfg: Any, run_id: str, **_k: Any) -> RunResult:
        return RunResult(
            run_id=run_id,
            status="completed",
            output=json.dumps({"no_news": True, "items": []}),
            usage={},
            runtime={"provider": "openrouter", "model": "test-model"},
            raw={"status": "completed", "hermes.error_code": "iteration_budget_exhausted"},
        )

    monkeypatch.setattr("jarvise_intel.app.get_run", fake_get)
    monkeypatch.setattr(
        "jarvise_intel.app.run_research",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no create")),
    )
    cfg = AdapterConfig()
    cfg.now = _slot_now("2026-11-26T07")
    code, body = handle_ingest(cfg, {"slot": "2026-11-26T07"})
    assert code == 502
    assert body["error"] == "incomplete_run"
    assert not list(digest_dir.glob("*.md"))


def test_provider_error_mid_poll_keeps_unknown_no_second_create(tmp_path: Path, monkeypatch: Any) -> None:
    """F2: transport failure after create stores unknown; next ingest does not mint -a2."""
    from jarvise_intel.gateway import RunResult

    digest_dir, state_dir = _cfg_env(tmp_path, monkeypatch)
    keys: list[str] = []
    calls = {"create": 0}

    def fake_run(*_a: Any, **kwargs: Any) -> Any:

        keys.append(str(kwargs.get("idempotency_key")))
        on_created = kwargs.get("on_created")
        calls["create"] += 1
        if on_created:
            on_created("run-alive")
        raise GatewayError("provider_error", "HTTP 502", status_code=502)

    monkeypatch.setattr("jarvise_intel.app.run_research", fake_run)
    cfg = AdapterConfig()
    cfg.now = _slot_now("2026-11-26T07")
    code1, body1 = handle_ingest(cfg, {"slot": "2026-11-26T07"})
    assert code1 == 409
    assert body1["error"] == "unknown_run"
    assert keys[0].endswith("-a1")

    def fake_get(_cfg: Any, run_id: str, **_k: Any) -> RunResult:
        return RunResult(
            run_id=run_id,
            status="running",
            output="",
            usage={},
            runtime={},
            raw={"status": "running"},
        )

    monkeypatch.setattr("jarvise_intel.app.get_run", fake_get)
    code2, body2 = handle_ingest(cfg, {"slot": "2026-11-26T07"})
    assert code2 == 409
    assert body2["error"] == "unknown_run"
    assert calls["create"] == 1
    assert len(keys) == 1


def test_lost_create_reuses_same_idempotency_key(tmp_path: Path, monkeypatch: Any) -> None:
    """F2: create with no run_id → create_unconfirmed → same key on retry."""
    digest_dir, state_dir = _cfg_env(tmp_path, monkeypatch)
    keys: list[str] = []
    phase = {"n": 0}

    def fake_run(*_a: Any, **kwargs: Any) -> Any:
        from jarvise_intel.gateway import RunResult

        keys.append(str(kwargs.get("idempotency_key")))
        phase["n"] += 1
        if phase["n"] == 1:
            raise GatewayError("provider_error", "URLError")
        return RunResult(
            run_id="run-replay",
            status="completed",
            output=json.dumps({"no_news": True, "items": []}),
            usage={},
            runtime={"provider": "openrouter", "model": "test-model"},
            raw={"status": "completed"},
        )

    monkeypatch.setattr("jarvise_intel.app.run_research", fake_run)
    cfg = AdapterConfig()
    cfg.now = _slot_now("2026-11-26T07")
    code1, _body1 = handle_ingest(cfg, {"slot": "2026-11-26T07"})
    assert code1 == 502
    code2, body2 = handle_ingest(cfg, {"slot": "2026-11-26T07"})
    assert code2 == 200
    assert keys[0] == keys[1]
    assert keys[0].endswith("-a1")
    assert (digest_dir / "2026-11-26-00.md").is_file()


def test_adopt_rejects_empty_hash_mismatch_missing_sidecar(tmp_path: Path) -> None:
    from jarvise_intel.digest import adopt_existing_digest, write_provenance

    slot = parse_slot("2026-11-26T07")
    digest_dir = tmp_path / "pilot-digests"
    state_dir = tmp_path / "state"
    digest_dir.mkdir(parents=True)
    target = digest_dir / slot.filename

    target.write_bytes(b"")
    with pytest.raises(ValueError, match="recovery_empty_file"):
        adopt_existing_digest(digest_dir=digest_dir, state_dir=state_dir, slot=slot)

    target.write_bytes(b"# Digest\n\nNot a trade signal. Research context only.\n")
    with pytest.raises(ValueError, match="recovery_missing_provenance"):
        adopt_existing_digest(digest_dir=digest_dir, state_dir=state_dir, slot=slot)

    write_provenance(state_dir, slot, sha256="deadbeef", byte_length=4)
    with pytest.raises(ValueError, match="recovery_hash_mismatch"):
        adopt_existing_digest(digest_dir=digest_dir, state_dir=state_dir, slot=slot)

    other = parse_slot("2026-11-26T19")
    good = target.read_bytes()
    write_provenance(
        state_dir,
        slot,
        sha256=__import__("hashlib").sha256(good).hexdigest(),
        byte_length=len(good),
    )
    # Overwrite provenance with wrong slot label via load path: write other slot's file
    # into this slot's sidecar path by calling write_provenance for other then swapping.
    from jarvise_intel.digest import load_provenance

    write_provenance(
        state_dir,
        other,
        sha256=__import__("hashlib").sha256(good).hexdigest(),
        byte_length=len(good),
    )
    # Malformed byte_length type
    import json as _json

    from jarvise_intel.digest import _provenance_path

    path = _provenance_path(state_dir, slot)
    path.write_text(
        _json.dumps(
            {
                "slot": slot.label,
                "filename": slot.filename,
                "sha256": __import__("hashlib").sha256(good).hexdigest(),
                "byte_length": "not-an-int",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="recovery_length_mismatch"):
        adopt_existing_digest(digest_dir=digest_dir, state_dir=state_dir, slot=slot)
    _ = load_provenance  # silence unused if optimized away


def test_smoke_rejects_substring_pilot_path(tmp_path: Path) -> None:
    from jarvise_intel.digest import assert_smoke_confined

    slot = parse_slot("smoke-2026-11-26T07", smoke_allowed=True)
    # Final component is not pilot-digests even though the marker appears earlier.
    with pytest.raises(ValueError, match="smoke_not_pilot_dir"):
        assert_smoke_confined(slot, tmp_path / "pilot-digests-backup" / "canonical")
