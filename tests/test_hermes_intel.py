"""Hermes ingest adapter, egress policy, and publication tests. No network."""

from __future__ import annotations

import io
import ipaddress
import json
import os
import threading
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

import pytest

from jarvise_intel.app import AdapterConfig, handle_ingest, redact
from jarvise_intel.digest import (
    extract_json_object,
    parse_slot,
    publish_digest,
    render_markdown,
    validate_items,
)
from jarvise_intel.gateway import GatewayConfig, GatewayError, create_run, wait_for_run
from jarvise_intel.policy import allow_connect_target, allow_https_url, is_blocked_ip


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


def test_policy_allows_public_https() -> None:
    ok, reason = allow_https_url("https://nvidia.com/news", resolve=lambda h: ["8.8.8.8"])
    assert ok is True
    assert reason == "ok"


def test_parse_slot_bangkok_to_utc_filename() -> None:
    morning = parse_slot("2026-11-26T07")
    assert morning.filename == "2026-11-26-00.md"
    evening = parse_slot("2026-11-26T19")
    assert evening.filename == "2026-11-26-12.md"
    with pytest.raises(ValueError):
        parse_slot("2026-11-26T08")
    with pytest.raises(ValueError):
        parse_slot("smoke-2026-11-26T07")
    smoke = parse_slot("smoke-2026-11-26T07", smoke_allowed=True)
    assert smoke.smoke is True


def _public_resolve(_host: str) -> list[str]:
    return ["8.8.8.8"]


def _footer_in(md: str) -> bool:
    return "Not a trade signal. Research context only." in md


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
    items, no_news = validate_items(payload, resolve=_public_resolve)
    assert no_news is False
    md = render_markdown(items)
    parsed = parse_digest_text(md)
    assert len(parsed) == 2
    assert "NVIDIA" in parsed[0]["title"]
    assert parsed[0]["url"] == "https://nvidia.com/news"
    assert _footer_in(md)


def test_validate_rejects_bad_url_and_empty() -> None:
    with pytest.raises(ValueError, match="empty_items"):
        validate_items({"items": []})
    with pytest.raises(ValueError, match="url_denied"):
        validate_items(
            {
                "items": [
                    {
                        "title": "x",
                        "day": "2026-10-14",
                        "url": "https://127.0.0.1/x",
                        "comment": "c",
                    }
                ]
            }
        )
    with pytest.raises(ValueError, match="bad_timestamp"):
        validate_items(
            {
                "items": [
                    {
                        "title": "x",
                        "day": "14/10/2026",
                        "url": "https://example.com/x",
                        "comment": "c",
                    }
                ]
            },
            resolve=_public_resolve,
        )


def test_no_news_digest() -> None:
    items, no_news = validate_items({"no_news": True, "items": []})
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
                        "url": "https://example.com/a",
                        "comment": "note",
                    }
                ]
            },
            resolve=_public_resolve,
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
    second = publish_digest(
        digest_dir=digest_dir,
        state_dir=state_dir,
        slot=slot,
        markdown=md,
        meta={"provider": "openrouter", "model": "test"},
    )
    assert second.get("replay") is True
    # Immutable completed slot: different markdown still returns the original.
    third = publish_digest(
        digest_dir=digest_dir,
        state_dir=state_dir,
        slot=slot,
        markdown=md + "\nextra\n",
        meta={},
    )
    assert third.get("replay") is True
    assert third["output_hash"] == first["output_hash"]

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

    calls = {"n": 0}

    def open_incomplete(request: Any, timeout: float) -> Any:
        calls["n"] += 1
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
        if request.get_method() == "POST":
            return _Resp({"run_id": "r2"})
        return _Resp({"run_id": "r2", "status": "running", "output": ""})

    with pytest.raises(GatewayError) as exc:
        wait_for_run(
            cfg,
            create_run(cfg, instructions="i", input_text="u", idempotency_key="t", open_url=open_running),
            open_url=open_running,
            sleep=lambda _s: None,
        )
    assert exc.value.category == "timeout"


def test_handle_ingest_success_and_replay(tmp_path: Path, monkeypatch: Any) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("fixed prompt", encoding="utf-8")
    digest_dir = tmp_path / "pilot"
    state_dir = tmp_path / "adapter"
    monkeypatch.setenv("JARVISE_HERMES_INGEST_TOKEN", "ingest-token-12345678")
    monkeypatch.setenv("JARVISE_HERMES_API_KEY", "api-key-12345678")
    monkeypatch.setenv("HERMES_PROVIDER", "openrouter")
    monkeypatch.setenv("HERMES_MODEL", "test-model")
    monkeypatch.setenv("JARVISE_HERMES_DIGEST_DIR", str(digest_dir))
    monkeypatch.setenv("JARVISE_HERMES_STATE_DIR", str(state_dir))
    monkeypatch.setenv("JARVISE_HERMES_PROMPT_PATH", str(prompt))

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
    monkeypatch.setattr(
        "jarvise_intel.digest.allow_https_url",
        lambda url, resolve=None, require_port_443=True: (True, "ok"),
    )
    cfg = AdapterConfig()
    code, body = handle_ingest(cfg, {"slot": "2026-11-26T07"})
    assert code == 200
    assert body["ok"] is True
    assert (digest_dir / "2026-11-26-00.md").is_file()
    code2, body2 = handle_ingest(cfg, {"slot": "2026-11-26T07"})
    assert code2 == 200
    assert body2["replay"] is True


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
    code, body = handle_ingest(AdapterConfig(), {"slot": "2026-11-26T19"})
    assert code == 502
    assert body["ok"] is False
