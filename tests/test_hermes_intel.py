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
    DEFAULT_CONNECT_ALLOWLIST,
    allow_connect_target,
    allow_https_url,
    allow_redirect_url,
    allow_source_url,
    is_blocked_ip,
    load_connect_allowlist,
    load_source_policy,
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
    policy = {"allow_suffixes": ["evil.example"], "deny_hosts": [], "deny_suffixes": []}
    assert allow_connect_target("evil.example", 443, resolve=resolve, policy=policy)[0] is False
    assert allow_connect_target("evil.example", 80, resolve=lambda h: ["8.8.8.8"], policy=policy)[0] is False


def test_policy_ipv6_unique_local_denied() -> None:
    ok, reason, addrs = allow_connect_target(
        "evil6.example",
        443,
        resolve=lambda _h: ["fc00::1"],
        policy={"allow_suffixes": ["evil6.example"], "deny_hosts": [], "deny_suffixes": []},
    )
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


def test_connect_allowlist_allows_configured_host() -> None:
    ok, reason, addrs = allow_connect_target(
        "www.nvidia.com",
        443,
        resolve=_public_resolve,
        policy_path=SOURCE_POLICY,
    )
    assert ok is True
    assert reason == "ok"
    assert addrs == ["8.8.8.8"]
    upper, upper_reason, _ = allow_connect_target(
        "NVIDIA.com",
        443,
        resolve=_public_resolve,
        policy_path=SOURCE_POLICY,
    )
    assert upper is True
    assert upper_reason == "ok"
    blocked, why, blocked_addrs = allow_connect_target(
        "nvidia.com",
        443,
        resolve=lambda _h: ["169.254.169.254"],
        policy_path=SOURCE_POLICY,
    )
    assert blocked is False
    assert why == "blocked_dns"
    assert blocked_addrs == []
    assert (
        allow_connect_target(
            "nvidia.com",
            80,
            resolve=_public_resolve,
            policy_path=SOURCE_POLICY,
        )[1]
        == "port_not_443"
    )


def test_connect_allowlist_denies_other_public_host() -> None:
    ok, reason, addrs = allow_connect_target(
        "example.com",
        443,
        resolve=_public_resolve,
        policy_path=SOURCE_POLICY,
    )
    assert ok is False
    assert reason == "connect_not_allowlisted"
    assert addrs == []
    sibling, sibling_reason, _ = allow_connect_target(
        "notnvidia.com",
        443,
        resolve=_public_resolve,
        policy_path=SOURCE_POLICY,
    )
    assert sibling is False
    assert sibling_reason == "connect_not_allowlisted"
    literal, literal_reason, _ = allow_connect_target(
        "8.8.8.8",
        443,
        resolve=_public_resolve,
        policy_path=SOURCE_POLICY,
    )
    assert literal is False
    assert literal_reason == "connect_not_allowlisted"
    denied, denied_reason, _ = allow_connect_target(
        "a.tinyurl.com",
        443,
        resolve=_public_resolve,
        policy={
            "allow_suffixes": ["tinyurl.com"],
            "deny_hosts": [],
            "deny_suffixes": ["tinyurl.com"],
        },
    )
    assert denied is False
    assert denied_reason == "denied_suffix"


def test_connect_allowlist_deny_hosts_and_no_dns_on_deny() -> None:
    lookups = {"n": 0}

    def resolve(_host: str) -> list[str]:
        lookups["n"] += 1
        return ["8.8.8.8"]

    denied, reason, addrs = allow_connect_target(
        "nvidia.com",
        443,
        resolve=resolve,
        policy={
            "allow_suffixes": ["nvidia.com"],
            "deny_hosts": ["nvidia.com"],
            "deny_suffixes": [],
        },
    )
    assert denied is False
    assert reason == "denied_host"
    assert addrs == []
    assert lookups["n"] == 0

    other, other_reason, _ = allow_connect_target(
        "example.com",
        443,
        resolve=resolve,
        policy={"allow_suffixes": ["nvidia.com"], "deny_hosts": [], "deny_suffixes": []},
    )
    assert other is False
    assert other_reason == "connect_not_allowlisted"
    assert lookups["n"] == 0

    closed, closed_reason, _ = allow_connect_target(
        "nvidia.com",
        443,
        resolve=resolve,
        policy_path=Path("/nonexistent/connect-allowlist.json"),
    )
    assert closed is False
    assert closed_reason == "connect_allowlist_closed"
    assert lookups["n"] == 0


def test_suffix_list_strips_trailing_dots_and_rejects_non_strings() -> None:
    lookups = {"n": 0}

    def resolve(_host: str) -> list[str]:
        lookups["n"] += 1
        return ["8.8.8.8"]

    for host in ("nvidia.com", "nvidia.com."):
        denied, reason, addrs = allow_connect_target(
            host,
            443,
            resolve=resolve,
            policy={
                "allow_suffixes": ["nvidia.com"],
                "deny_hosts": ["nvidia.com."],
                "deny_suffixes": [],
            },
        )
        assert denied is False
        assert reason == "denied_host"
        assert addrs == []
    assert lookups["n"] == 0

    suffix_denied, suffix_reason, suffix_addrs = allow_connect_target(
        "a.tinyurl.com",
        443,
        resolve=resolve,
        policy={
            "allow_suffixes": ["tinyurl.com"],
            "deny_hosts": [],
            "deny_suffixes": ["tinyurl.com."],
        },
    )
    assert suffix_denied is False
    assert suffix_reason == "denied_suffix"
    assert suffix_addrs == []
    assert lookups["n"] == 0

    # Dots-only and blank items are skipped, so they are not a wildcard deny or allow.
    allowed, allow_reason, allow_addrs = allow_connect_target(
        "nvidia.com",
        443,
        resolve=resolve,
        policy={
            "allow_suffixes": ["nvidia.com.", ".", ""],
            "deny_hosts": ["."],
            "deny_suffixes": ["."],
        },
    )
    assert allowed is True
    assert allow_reason == "ok"
    assert allow_addrs == ["8.8.8.8"]
    assert lookups["n"] == 1
    other, other_reason, other_addrs = allow_connect_target(
        "example.com",
        443,
        resolve=resolve,
        policy={
            "allow_suffixes": ["nvidia.com.", ".", ""],
            "deny_hosts": ["."],
            "deny_suffixes": ["."],
        },
    )
    assert other is False
    assert other_reason == "connect_not_allowlisted"
    assert other_addrs == []
    assert lookups["n"] == 1

    closed_policies: tuple[dict[str, object], ...] = (
        {"allow_suffixes": ["nvidia.com", 1], "deny_hosts": [], "deny_suffixes": []},
        {"allow_suffixes": ["nvidia.com"], "deny_hosts": [1], "deny_suffixes": []},
        {"allow_suffixes": ["nvidia.com"], "deny_hosts": [], "deny_suffixes": [None]},
    )
    for policy in closed_policies:
        closed, why, got = allow_connect_target("nvidia.com", 443, resolve=resolve, policy=policy)
        assert closed is False
        assert why == "connect_allowlist_closed"
        assert got == []
    assert lookups["n"] == 1


def test_connect_allowlist_rejects_malformed_hosts_without_dns() -> None:
    lookups = {"n": 0}

    def resolve(_host: str) -> list[str]:
        lookups["n"] += 1
        return ["8.8.8.8"]

    policy = {"allow_suffixes": ["nvidia.com"], "deny_hosts": [], "deny_suffixes": []}
    cases = (
        (".nvidia.com", "hostname_not_dns"),
        ("foo..nvidia.com", "hostname_not_dns"),
        ("evil;.nvidia.com", "bad_host"),
        ("evil%2f.nvidia.com", "bad_host"),
        (r"evil\\.nvidia.com", "bad_host"),
    )
    for host, expected in cases:
        ok, reason, addrs = allow_connect_target(host, 443, resolve=resolve, policy=policy)
        assert ok is False, host
        assert reason == expected, (host, reason)
        assert addrs == []
    assert lookups["n"] == 0


def test_default_connect_allowlist_allows_openrouter_not_digest_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default CONNECT file is the shipped path, not CWD or a leftover env value."""
    monkeypatch.setenv("JARVISE_HERMES_CONNECT_ALLOWLIST", str(tmp_path / "absent.json"))
    monkeypatch.delenv("JARVISE_HERMES_CONNECT_ALLOWLIST", raising=False)
    monkeypatch.chdir(tmp_path)
    shipped = Path(__file__).resolve().parents[1] / "config" / "hermes" / "connect-allowlist.json"
    assert DEFAULT_CONNECT_ALLOWLIST.is_absolute()
    assert DEFAULT_CONNECT_ALLOWLIST == shipped
    ok, reason, addrs = allow_connect_target("openrouter.ai", 443, resolve=_public_resolve)
    assert ok is True
    assert reason == "ok"
    assert addrs == ["8.8.8.8"]
    src_ok, src_reason = allow_source_url("https://openrouter.ai/models", policy_path=SOURCE_POLICY)
    assert src_ok is False
    assert src_reason == "unknown_source"


def test_connect_allowlist_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = tmp_path / "missing.json"
    ok, reason, addrs = allow_connect_target(
        "nvidia.com",
        443,
        resolve=_public_resolve,
        policy_path=missing,
    )
    assert ok is False
    assert reason == "connect_allowlist_closed"
    assert addrs == []

    empty = tmp_path / "empty.json"
    empty.write_text("{}", encoding="utf-8")
    blank = tmp_path / "blank.json"
    blank.write_text('{"allow_suffixes": []}', encoding="utf-8")
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    array = tmp_path / "array.json"
    array.write_text("[]", encoding="utf-8")
    for path in (empty, blank, broken, array):
        closed, why, _ = allow_connect_target(
            "nvidia.com",
            443,
            resolve=_public_resolve,
            policy_path=path,
        )
        assert closed is False
        assert why == "connect_allowlist_closed"

    monkeypatch.setenv("JARVISE_HERMES_CONNECT_ALLOWLIST", str(missing))
    from_env, env_reason, _ = allow_connect_target(
        "nvidia.com",
        443,
        resolve=_public_resolve,
    )
    assert from_env is False
    assert env_reason == "connect_allowlist_closed"

    monkeypatch.setenv("JARVISE_HERMES_CONNECT_ALLOWLIST", "  ")
    empty_env, empty_reason, _ = allow_connect_target(
        "nvidia.com",
        443,
        resolve=_public_resolve,
    )
    assert empty_env is False
    assert empty_reason == "connect_allowlist_closed"

    monkeypatch.delenv("JARVISE_HERMES_CONNECT_ALLOWLIST", raising=False)
    monkeypatch.chdir(tmp_path)
    # The shipped default is an absolute path, so a different CWD still loads it.
    default_ok, default_ok_reason, _ = allow_connect_target(
        "reuters.com",
        443,
        resolve=_public_resolve,
    )
    assert default_ok is True
    assert default_ok_reason == "ok"


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
            "slot": slot.label,
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
        {
            "status": "unknown",
            "attempt": 1,
            "run_id": "run-b",
            "idempotency_key": "2026-11-26T07-a1",
            "started_at": "2026-11-26T05:00:00+00:00",
            "slot": slot.label,
        },
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


def test_shared_parser_matches_parse_digest_text_fields() -> None:
    """R1: stdlib parser title/url/comment match downstream parse_digest_text."""
    from jarvise_context.news_sources import parse_digest_text
    from jarvise_intel.digest_parse import parse_digest_items

    md = (
        "# Digest\n\n"
        "1. **Fed holds rates** — 2026-11-26\n"
        "   https://federalreserve.gov/a\n"
        "   Primary statement.\n\n"
        "Not a trade signal. Research context only.\n"
    )
    shared = parse_digest_items(md)
    themed = parse_digest_text(md)
    assert len(shared) == len(themed) == 1
    assert shared[0]["title"] == themed[0]["title"]
    assert shared[0]["url"] == themed[0]["url"]
    assert shared[0]["comment"] == themed[0]["comment"]
    assert shared[0].get("day") == "2026-11-26"
    assert "theme" in themed[0]


def test_lost_create_attempt2_does_not_reuse_attempt1_run_id(tmp_path: Path, monkeypatch: Any) -> None:
    """R4: lost attempt-2 create must not bind old-run-1 or mint -a3."""
    from jarvise_intel.digest import write_slot_record

    digest_dir, state_dir = _cfg_env(tmp_path, monkeypatch)
    slot = parse_slot("2026-11-26T07")
    write_slot_record(
        state_dir,
        slot,
        {
            "status": "failed_terminal",
            "attempt": 1,
            "run_id": "old-run-1",
            "idempotency_key": "2026-11-26T07-a1",
        },
    )
    keys: list[str] = []
    gets: list[str] = []
    phase = {"n": 0}

    def fake_run(*_a: Any, **kwargs: Any) -> Any:
        keys.append(str(kwargs.get("idempotency_key")))
        phase["n"] += 1
        # Lose create response before on_created — no run_id bound for this attempt.
        raise GatewayError("provider_error", "URLError")

    def fake_get(_cfg: Any, run_id: str, **_k: Any) -> Any:
        gets.append(run_id)
        from jarvise_intel.gateway import RunResult

        return RunResult(
            run_id=run_id,
            status="failed",
            output="",
            usage={},
            runtime={},
            raw={"status": "failed"},
        )

    monkeypatch.setattr("jarvise_intel.app.run_research", fake_run)
    monkeypatch.setattr("jarvise_intel.app.get_run", fake_get)
    cfg = AdapterConfig()
    cfg.now = _slot_now("2026-11-26T07")
    code1, _ = handle_ingest(cfg, {"slot": "2026-11-26T07"})
    assert code1 == 502
    assert keys[0].endswith("-a2")
    from jarvise_intel.digest import load_slot_record

    rec = load_slot_record(state_dir, slot)
    assert rec is not None
    assert rec.get("status") == "create_unconfirmed"
    assert rec.get("attempt") == 2
    assert not rec.get("run_id")
    assert rec.get("idempotency_key", "").endswith("-a2")

    # Replay same key; still no GET of old-run-1 for recovery of this admission.
    code2, _ = handle_ingest(cfg, {"slot": "2026-11-26T07"})
    assert code2 == 502
    assert keys[1] == keys[0]
    assert "old-run-1" not in gets
    assert len(keys) == 2


def test_active_admission_blocks_other_slot(tmp_path: Path, monkeypatch: Any) -> None:
    """R5: unresolved 07 admission blocks 19 create."""
    from jarvise_intel.digest import write_active_admission, write_slot_record

    digest_dir, state_dir = _cfg_env(tmp_path, monkeypatch)
    slot07 = parse_slot("2026-11-27T07")
    write_slot_record(
        state_dir,
        slot07,
        {
            "status": "unknown",
            "attempt": 1,
            "run_id": "run-07",
            "idempotency_key": "2026-11-27T07-a1",
            "started_at": "2026-11-27T01:00:00+00:00",
            "slot": slot07.label,
        },
    )
    write_active_admission(
        state_dir,
        {
            "status": "unknown",
            "attempt": 1,
            "run_id": "run-07",
            "idempotency_key": "2026-11-27T07-a1",
            "started_at": "2026-11-27T01:00:00+00:00",
            "slot": slot07.label,
        },
    )
    creates = {"n": 0}

    def fake_run(*_a: Any, **_k: Any) -> Any:
        creates["n"] += 1
        raise AssertionError("must not create for other slot")

    def fake_get(_cfg: Any, run_id: str, **_k: Any) -> Any:
        from jarvise_intel.gateway import RunResult

        return RunResult(
            run_id=run_id,
            status="running",
            output="",
            usage={},
            runtime={},
            raw={"status": "running"},
        )

    monkeypatch.setattr("jarvise_intel.app.run_research", fake_run)
    monkeypatch.setattr("jarvise_intel.app.get_run", fake_get)
    cfg = AdapterConfig()
    cfg.now = _slot_now("2026-11-27T19")
    code, body = handle_ingest(cfg, {"slot": "2026-11-27T19"})
    assert code == 409
    assert body.get("error") == "admission_busy"
    assert creates["n"] == 0


def test_active_admission_blocks_smoke(tmp_path: Path, monkeypatch: Any) -> None:
    from jarvise_intel.digest import write_active_admission, write_slot_record

    digest_dir, state_dir = _cfg_env(tmp_path, monkeypatch)
    monkeypatch.setenv("JARVISE_HERMES_SMOKE", "1")
    slot = parse_slot("2026-11-26T07")
    write_slot_record(
        state_dir,
        slot,
        {
            "status": "create_unconfirmed",
            "attempt": 1,
            "idempotency_key": "2026-11-26T07-a1",
            "started_at": "2026-11-26T05:00:00+00:00",
            "slot": slot.label,
        },
    )
    write_active_admission(
        state_dir,
        {
            "status": "create_unconfirmed",
            "attempt": 1,
            "idempotency_key": "2026-11-26T07-a1",
            "started_at": "2026-11-26T05:00:00+00:00",
            "slot": slot.label,
        },
    )
    creates = {"n": 0}

    def fake_run(*_a: Any, **_k: Any) -> Any:
        creates["n"] += 1
        raise AssertionError("no smoke create while admission active")

    monkeypatch.setattr("jarvise_intel.app.run_research", fake_run)
    cfg = AdapterConfig()
    cfg.smoke = True
    cfg.now = _slot_now("2026-11-26T07")
    code, body = handle_ingest(cfg, {"slot": "smoke-2026-11-26T07"})
    assert code == 409
    # Missing frozen request hash → owner recovery; never a second smoke create.
    assert body.get("error") in {"admission_busy", "owner_recovery_required"}
    assert creates["n"] == 0


def test_s2_adopt_clears_stale_active_marker(tmp_path: Path, monkeypatch: Any) -> None:
    """S2: adoption/completed must clear matching active marker so later slots proceed."""
    import hashlib

    from jarvise_intel.digest import (
        load_active_admission,
        render_markdown,
        write_active_admission,
        write_provenance,
        write_slot_record,
    )

    digest_dir, state_dir = _cfg_env(tmp_path, monkeypatch)
    digest_dir.mkdir(parents=True, exist_ok=True)
    slot = parse_slot("2026-11-26T07")
    md = render_markdown([])
    data = md.encode("utf-8")
    (digest_dir / slot.filename).write_bytes(data)
    write_provenance(state_dir, slot, sha256=hashlib.sha256(data).hexdigest(), byte_length=len(data))
    # Crash boundary: artifact+provenance exist, marker still running.
    write_slot_record(
        state_dir,
        slot,
        {
            "status": "running",
            "attempt": 1,
            "run_id": "stale-run",
            "idempotency_key": "2026-11-26T07-a1",
            "started_at": "2026-11-26T00:00:00+00:00",
            "slot": slot.label,
        },
    )
    write_active_admission(
        state_dir,
        {
            "status": "running",
            "attempt": 1,
            "run_id": "stale-run",
            "idempotency_key": "2026-11-26T07-a1",
            "started_at": "2026-11-26T00:00:00+00:00",
            "slot": slot.label,
        },
    )
    creates = {"n": 0}

    def fake_run(*_a: Any, **_k: Any) -> Any:
        creates["n"] += 1
        raise AssertionError("must not create after adopt")

    monkeypatch.setattr("jarvise_intel.app.run_research", fake_run)
    cfg = AdapterConfig()
    cfg.now = _slot_now("2026-11-26T07")
    code, body = handle_ingest(cfg, {"slot": "2026-11-26T07"})
    assert code == 200
    assert body.get("result", {}).get("recovered") is True
    assert load_active_admission(state_dir) is None

    # Later slot must not see admission_busy from the cleared marker.
    cfg.now = _slot_now("2026-11-26T19")
    # No unresolved work; 19 may attempt create — stub it.
    monkeypatch.setattr(
        "jarvise_intel.app.run_research",
        lambda *_a, **_k: (_ for _ in ()).throw(GatewayError("provider_error", "x")),
    )
    code19, body19 = handle_ingest(cfg, {"slot": "2026-11-26T19"})
    assert body19.get("error") != "admission_busy"
    assert code19 in {502, 409, 500}


def test_s2_completed_record_clears_marker_on_replay(tmp_path: Path, monkeypatch: Any) -> None:
    from jarvise_intel.digest import load_active_admission, write_active_admission, write_slot_record

    digest_dir, state_dir = _cfg_env(tmp_path, monkeypatch)
    slot = parse_slot("2026-11-26T07")
    write_slot_record(
        state_dir,
        slot,
        {"status": "completed", "attempt": 1, "slot": slot.label, "filename": slot.filename},
    )
    write_active_admission(
        state_dir,
        {
            "status": "running",
            "attempt": 1,
            "run_id": "x",
            "idempotency_key": "2026-11-26T07-a1",
            "started_at": "2026-11-26T00:00:00+00:00",
            "slot": slot.label,
        },
    )
    cfg = AdapterConfig()
    cfg.now = _slot_now("2026-11-26T07")
    code, _ = handle_ingest(cfg, {"slot": "2026-11-26T07"})
    assert code == 200
    assert load_active_admission(state_dir) is None
    _ = digest_dir


def test_s3_lost_create_cross_window_owner_recovery(tmp_path: Path, monkeypatch: Any) -> None:
    """S3: 07 create_unconfirmed at 19 must not mint a new run; past deadline → owner recovery."""
    from jarvise_intel.digest import write_active_admission, write_slot_record

    digest_dir, state_dir = _cfg_env(tmp_path, monkeypatch)
    slot07 = parse_slot("2026-11-26T07")
    started = "2026-11-26T00:00:00+00:00"  # well past 600s by 19:00 Bangkok
    write_slot_record(
        state_dir,
        slot07,
        {
            "status": "create_unconfirmed",
            "attempt": 1,
            "idempotency_key": "2026-11-26T07-a1",
            "started_at": started,
            "slot": slot07.label,
        },
    )
    write_active_admission(
        state_dir,
        {
            "status": "create_unconfirmed",
            "attempt": 1,
            "idempotency_key": "2026-11-26T07-a1",
            "started_at": started,
            "slot": slot07.label,
        },
    )
    creates = {"n": 0}

    def fake_create(*_a: Any, **_k: Any) -> str:
        creates["n"] += 1
        raise GatewayError("provider_error", "URLError")

    monkeypatch.setattr("jarvise_intel.app.create_run", fake_create)
    monkeypatch.setattr(
        "jarvise_intel.app.run_research",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no new research")),
    )
    cfg = AdapterConfig()
    cfg.deadline_s = 600.0
    cfg.now = _slot_now("2026-11-26T19")
    code, body = handle_ingest(cfg, {"slot": "2026-11-26T19"})
    assert code == 409
    assert body.get("error") == "owner_recovery_required"
    assert creates["n"] == 0  # T1: no POST after retention / missing identity
    _ = digest_dir


def test_s4_expired_create_replay_uses_zero_remaining(tmp_path: Path, monkeypatch: Any) -> None:
    """S4: expired create_unconfirmed must stop-and-verify, not wait another 600s on that run."""
    from jarvise_intel.app import AdapterConfig as _AC
    from jarvise_intel.app import request_sha256, scope_sha256
    from jarvise_intel.digest import write_slot_record
    from jarvise_intel.gateway import RunResult

    digest_dir, state_dir = _cfg_env(tmp_path, monkeypatch)
    slot = parse_slot("2026-11-26T07")
    instructions = (tmp_path / "prompt.md").read_text(encoding="utf-8")
    input_text = (
        f"Produce the news digest JSON for slot {slot.label} "
        f"(Bangkok {slot.bangkok.isoformat()}, UTC file {slot.filename})."
    )
    req_hash = request_sha256(
        instructions=instructions,
        input_text=input_text,
        provider="openrouter",
        model="test-model",
    )
    # Scope matches AdapterConfig defaults after _cfg_env.
    probe = _AC()
    sc_hash = scope_sha256(gateway_url=probe.gateway_url, api_key=probe.api_key)
    write_slot_record(
        state_dir,
        slot,
        {
            "status": "create_unconfirmed",
            "attempt": 1,
            "idempotency_key": "2026-11-26T07-a1",
            "started_at": "2026-11-26T00:00:00+00:00",
            "slot": slot.label,
            "request_sha256": req_hash,
            "scope_sha256": sc_hash,
            "provider": "openrouter",
            "model": "test-model",
        },
    )
    stops: list[str] = []
    research_deadlines: list[float | None] = []

    def fake_create(*_a: Any, **_k: Any) -> str:
        return "recovered-run"

    def fake_stop(_cfg: Any, run_id: str, **_k: Any) -> None:
        stops.append(run_id)

    def fake_get(_cfg: Any, run_id: str, **_k: Any) -> Any:
        return RunResult(
            run_id=run_id,
            status="failed",
            output="",
            usage={},
            runtime={},
            raw={"status": "failed"},
        )

    def fake_run(*_a: Any, **kwargs: Any) -> Any:
        research_deadlines.append(kwargs.get("deadline_s"))
        raise GatewayError("provider_error", "blocked_after_verified_fail")

    monkeypatch.setattr("jarvise_intel.app.create_run", fake_create)
    monkeypatch.setattr("jarvise_intel.app.stop_run", fake_stop)
    monkeypatch.setattr("jarvise_intel.app.get_run", fake_get)
    monkeypatch.setattr("jarvise_intel.app.run_research", fake_run)
    cfg = AdapterConfig()
    cfg.deadline_s = 600.0
    cfg.now = _slot_now("2026-11-26T07")
    code, body = handle_ingest(cfg, {"slot": "2026-11-26T07"})
    # Recovered expired run must be stop-verified before any new attempt.
    assert "recovered-run" in stops
    assert code == 502
    assert body.get("error") == "provider_error"
    # A subsequent attempt-2 (new admission) may use a full budget; attempt-1 did not wait 600s.
    assert all(d is None or d <= 600.0 for d in research_deadlines)
    _ = digest_dir


def test_s4_wait_for_run_zero_deadline_stops_immediately() -> None:
    cfg = GatewayConfig(
        base_url="http://hermes:8642",
        api_key="k",
        provider="openrouter",
        model="m",
        deadline_s=600.0,
        poll_s=0.01,
    )
    stops = {"n": 0}

    def open_url(request: Any, timeout: float) -> Any:
        method = request.get_method()
        url = str(getattr(request, "full_url", "") or request.get_full_url())
        if method == "POST" and url.rstrip("/").endswith("/stop"):
            stops["n"] += 1
            return _Resp({"run_id": "r", "status": "running"})
        return _Resp({"run_id": "r", "status": "running", "output": ""})

    with pytest.raises(GatewayError) as exc:
        wait_for_run(cfg, "r", open_url=open_url, sleep=lambda _s: None, deadline_s=0.0)
    assert exc.value.category == "unknown_run"
    assert stops["n"] == 1


def test_s5_malformed_active_blocks_without_second_create(tmp_path: Path, monkeypatch: Any) -> None:
    """S5: corrupt marker must fail closed; unresolved slot still blocks new paid work."""
    from jarvise_intel.digest import write_slot_record

    digest_dir, state_dir = _cfg_env(tmp_path, monkeypatch)
    slot07 = parse_slot("2026-11-27T07")
    write_slot_record(
        state_dir,
        slot07,
        {
            "status": "unknown",
            "attempt": 1,
            "run_id": "run-07",
            "idempotency_key": "2026-11-27T07-a1",
            "started_at": "2026-11-27T01:00:00+00:00",
            "slot": slot07.label,
        },
    )
    (state_dir / "active_admission.json").write_text("{not-json", encoding="utf-8")
    creates = {"n": 0}

    def fake_run(*_a: Any, **_k: Any) -> Any:
        creates["n"] += 1
        raise AssertionError("must not create while admission state invalid")

    monkeypatch.setattr("jarvise_intel.app.run_research", fake_run)
    cfg = AdapterConfig()
    cfg.now = _slot_now("2026-11-27T19")
    code, body = handle_ingest(cfg, {"slot": "2026-11-27T19"})
    assert code == 409
    assert body.get("error") == "admission_state_invalid"
    assert creates["n"] == 0
    _ = digest_dir


def test_s5_missing_index_rebuilds_from_unresolved_slot(tmp_path: Path, monkeypatch: Any) -> None:
    from jarvise_intel.digest import write_slot_record

    digest_dir, state_dir = _cfg_env(tmp_path, monkeypatch)
    slot07 = parse_slot("2026-11-27T07")
    write_slot_record(
        state_dir,
        slot07,
        {
            "status": "unknown",
            "attempt": 1,
            "run_id": "run-07",
            "idempotency_key": "2026-11-27T07-a1",
            "started_at": "2026-11-27T01:00:00+00:00",
            "slot": slot07.label,
        },
    )
    # No active_admission.json — rebuild from slot file.
    creates = {"n": 0}

    def fake_run(*_a: Any, **_k: Any) -> Any:
        creates["n"] += 1
        raise AssertionError("no second run")

    def fake_get(_cfg: Any, run_id: str, **_k: Any) -> Any:
        from jarvise_intel.gateway import RunResult

        return RunResult(
            run_id=run_id,
            status="running",
            output="",
            usage={},
            runtime={},
            raw={"status": "running"},
        )

    monkeypatch.setattr("jarvise_intel.app.run_research", fake_run)
    monkeypatch.setattr("jarvise_intel.app.get_run", fake_get)
    cfg = AdapterConfig()
    cfg.now = _slot_now("2026-11-27T19")
    code, body = handle_ingest(cfg, {"slot": "2026-11-27T19"})
    assert code == 409
    assert body.get("error") in {"admission_busy", "unknown_run"}
    assert creates["n"] == 0
    _ = digest_dir


def test_t1_expired_idempotency_never_posts_even_if_create_would_succeed(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """T1: three-day-old create_unconfirmed must not POST even when transport returns NEW-PAID-RUN."""
    from jarvise_intel.app import AdapterConfig as _AC
    from jarvise_intel.app import request_sha256, scope_sha256
    from jarvise_intel.digest import load_active_admission, write_active_admission, write_slot_record

    digest_dir, state_dir = _cfg_env(tmp_path, monkeypatch)
    slot07 = parse_slot("2026-11-23T07")
    instructions = (tmp_path / "prompt.md").read_text(encoding="utf-8")
    input_text = (
        f"Produce the news digest JSON for slot {slot07.label} "
        f"(Bangkok {slot07.bangkok.isoformat()}, UTC file {slot07.filename})."
    )
    req_hash = request_sha256(
        instructions=instructions,
        input_text=input_text,
        provider="openrouter",
        model="test-model",
    )
    probe = _AC()
    sc_hash = scope_sha256(gateway_url=probe.gateway_url, api_key=probe.api_key)
    started = "2026-11-23T00:00:00+00:00"
    rec = {
        "status": "create_unconfirmed",
        "attempt": 1,
        "idempotency_key": "2026-11-23T07-a1",
        "started_at": started,
        "slot": slot07.label,
        "request_sha256": req_hash,
        "scope_sha256": sc_hash,
        "provider": "openrouter",
        "model": "test-model",
    }
    write_slot_record(state_dir, slot07, rec)
    write_active_admission(state_dir, rec)
    creates = {"n": 0}

    def fake_create(*_a: Any, **_k: Any) -> str:
        creates["n"] += 1
        return "NEW-PAID-RUN"

    monkeypatch.setattr("jarvise_intel.app.create_run", fake_create)
    monkeypatch.setattr(
        "jarvise_intel.app.run_research",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no research")),
    )
    cfg = AdapterConfig()
    cfg.now = _slot_now("2026-11-26T19")
    code, body = handle_ingest(cfg, {"slot": "2026-11-26T19"})
    assert code == 409
    assert body.get("error") == "owner_recovery_required"
    assert creates["n"] == 0
    active = load_active_admission(state_dir)
    assert active is not None
    assert active.get("status") == "owner_recovery_required"
    _ = digest_dir


def test_t2_malformed_slot_blocks_index_rebuild(tmp_path: Path, monkeypatch: Any) -> None:
    """T2: absent index + malformed slot JSON must fail closed with zero creates."""
    digest_dir, state_dir = _cfg_env(tmp_path, monkeypatch)
    slots = state_dir / "slots"
    slots.mkdir(parents=True)
    (slots / "2026-11-27T07.json").write_text("{not-json", encoding="utf-8")
    creates = {"n": 0}

    def fake_run(*_a: Any, **_k: Any) -> Any:
        creates["n"] += 1
        raise AssertionError("must not create")

    monkeypatch.setattr("jarvise_intel.app.run_research", fake_run)
    cfg = AdapterConfig()
    cfg.now = _slot_now("2026-11-27T19")
    code, body = handle_ingest(cfg, {"slot": "2026-11-27T19"})
    assert code == 409
    assert body.get("error") == "admission_state_invalid"
    assert creates["n"] == 0
    _ = digest_dir


def test_t2_valid_marker_corrupt_slot_keeps_attempt2(tmp_path: Path, monkeypatch: Any) -> None:
    """T2: valid unknown attempt-2 marker + damaged slot must not restart at -a1."""
    from jarvise_intel.digest import load_active_admission, write_active_admission

    digest_dir, state_dir = _cfg_env(tmp_path, monkeypatch)
    slot = parse_slot("2026-11-26T07")
    marker = {
        "status": "unknown",
        "attempt": 2,
        "run_id": "run-a2",
        "idempotency_key": "2026-11-26T07-a2",
        "started_at": "2026-11-26T05:00:00+00:00",
        "slot": slot.label,
    }
    write_active_admission(state_dir, marker)
    slots = state_dir / "slots"
    slots.mkdir(parents=True)
    (slots / "2026-11-26T07.json").write_text("{broken", encoding="utf-8")
    creates = {"n": 0}
    keys: list[str] = []

    def fake_run(*_a: Any, **kwargs: Any) -> Any:
        creates["n"] += 1
        keys.append(str(kwargs.get("idempotency_key")))
        raise AssertionError("must not create")

    monkeypatch.setattr("jarvise_intel.app.run_research", fake_run)
    cfg = AdapterConfig()
    cfg.now = _slot_now("2026-11-26T07")
    code, body = handle_ingest(cfg, {"slot": "2026-11-26T07"})
    assert code == 409
    assert body.get("error") == "admission_state_invalid"
    assert creates["n"] == 0
    assert keys == []
    still = load_active_admission(state_dir)
    assert still is not None
    assert still.get("attempt") == 2
    assert still.get("status") == "unknown"
    assert still.get("run_id") == "run-a2"
    _ = digest_dir


def test_t2_invalid_utf8_slot_blocks_list(tmp_path: Path, monkeypatch: Any) -> None:
    """T2: invalid UTF-8 slot bytes fail closed as slot_unreadable on the index scan."""
    from jarvise_intel.digest import ActiveAdmissionError, list_unresolved_slot_records

    _digest_dir, state_dir = _cfg_env(tmp_path, monkeypatch)
    slots = state_dir / "slots"
    slots.mkdir(parents=True)
    name = "2026-11-27T07.json"
    (slots / name).write_bytes(b"\xff\xfe{")
    with pytest.raises(ActiveAdmissionError, match=r"^slot_unreadable:") as exc:
        list_unresolved_slot_records(state_dir)
    assert str(exc.value).startswith("slot_unreadable:")
    assert name in str(exc.value)
    assert isinstance(exc.value.__cause__, UnicodeError)


def test_t2_invalid_utf8_slot_blocks_load(tmp_path: Path, monkeypatch: Any) -> None:
    """T2: the same invalid UTF-8 slot file fails closed on a direct slot load."""
    from jarvise_intel.digest import ActiveAdmissionError, load_slot_record

    _digest_dir, state_dir = _cfg_env(tmp_path, monkeypatch)
    slots = state_dir / "slots"
    slots.mkdir(parents=True)
    name = "2026-11-27T07.json"
    (slots / name).write_bytes(b"\xff\xfe{")
    slot = parse_slot("2026-11-27T07")
    with pytest.raises(ActiveAdmissionError, match=r"^slot_unreadable:") as exc:
        load_slot_record(state_dir, slot)
    assert str(exc.value).startswith("slot_unreadable:")
    assert name in str(exc.value)
    assert isinstance(exc.value.__cause__, UnicodeError)


def test_t2_invalid_utf8_slot_blocks_index_rebuild(tmp_path: Path, monkeypatch: Any) -> None:
    """T2: absent index + invalid UTF-8 slot must fail closed with zero creates."""
    digest_dir, state_dir = _cfg_env(tmp_path, monkeypatch)
    slots = state_dir / "slots"
    slots.mkdir(parents=True)
    (slots / "2026-11-27T07.json").write_bytes(b"\xff\xfe{")
    creates = {"n": 0}

    def fake_run(*_a: Any, **_k: Any) -> Any:
        creates["n"] += 1
        raise AssertionError("must not create")

    def fake_create(*_a: Any, **_k: Any) -> str:
        creates["n"] += 1
        raise AssertionError("must not create")

    monkeypatch.setattr("jarvise_intel.app.run_research", fake_run)
    monkeypatch.setattr("jarvise_intel.app.create_run", fake_create)
    cfg = AdapterConfig()
    cfg.now = _slot_now("2026-11-27T19")
    code, body = handle_ingest(cfg, {"slot": "2026-11-27T19"})
    assert code == 409
    assert body.get("error") == "admission_state_invalid"
    assert creates["n"] == 0
    _ = digest_dir


def test_t2_invalid_utf8_admission_marker_unreadable(tmp_path: Path, monkeypatch: Any) -> None:
    """T2: invalid UTF-8 active-admission marker fails closed as admission_unreadable."""
    from jarvise_intel.digest import ActiveAdmissionError, load_active_admission

    _digest_dir, state_dir = _cfg_env(tmp_path, monkeypatch)
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "active_admission.json").write_bytes(b"\xff\xfe{")
    with pytest.raises(ActiveAdmissionError, match=r"^admission_unreadable$") as exc:
        load_active_admission(state_dir)
    assert str(exc.value) == "admission_unreadable"
    assert isinstance(exc.value.__cause__, UnicodeError)


def test_t3_create_time_deducted_from_wait(monkeypatch: Any) -> None:
    """T3: 1s budget + 10s create → first post-id action is stop, not a poll GET."""
    from jarvise_intel.gateway import GatewayConfig, run_research

    cfg = GatewayConfig(
        base_url="http://hermes:8642",
        api_key="k",
        provider="openrouter",
        model="m",
        deadline_s=600.0,
        poll_s=0.01,
    )
    clock = {"t": 0.0}
    events: list[str] = []

    def mono() -> float:
        return clock["t"]

    def open_url(request: Any, timeout: float) -> Any:
        method = request.get_method()
        url = str(getattr(request, "full_url", "") or request.get_full_url())
        if method == "POST" and url.rstrip("/").endswith("/v1/runs"):
            events.append("POST")
            clock["t"] += 10.0
            return _Resp({"run_id": "r-x"})
        if method == "POST" and url.rstrip("/").endswith("/stop"):
            events.append("STOP")
            return _Resp({"run_id": "r-x", "status": "running"})
        events.append("GET")
        return _Resp({"run_id": "r-x", "status": "running", "output": ""})

    persisted: list[str] = []

    with pytest.raises(GatewayError) as exc:
        run_research(
            cfg,
            instructions="i",
            input_text="u",
            idempotency_key="k1",
            open_url=open_url,
            sleep=lambda _s: None,
            on_created=persisted.append,
            deadline_s=1.0,
            monotonic=mono,
        )
    assert exc.value.category == "unknown_run"
    assert persisted == ["r-x"]
    assert events[0] == "POST"
    assert "STOP" in events
    # First action after learning the id must be stop (budget already gone), not a poll.
    after_post = events[1:]
    assert after_post[0] == "STOP"


def test_t3_partial_create_shortens_poll(monkeypatch: Any) -> None:
    from jarvise_intel.gateway import GatewayConfig, run_research

    cfg = GatewayConfig(
        base_url="http://hermes:8642",
        api_key="k",
        provider="openrouter",
        model="m",
        deadline_s=600.0,
        poll_s=1.0,
    )
    clock = {"t": 0.0}
    poll_windows: list[float] = []

    def mono() -> float:
        return clock["t"]

    def open_url(request: Any, timeout: float) -> Any:
        method = request.get_method()
        url = str(getattr(request, "full_url", "") or request.get_full_url())
        if method == "POST" and url.rstrip("/").endswith("/v1/runs"):
            clock["t"] += 3.0
            return _Resp({"run_id": "r-y"})
        if method == "POST" and url.rstrip("/").endswith("/stop"):
            return _Resp({"run_id": "r-y", "status": "completed"})
        poll_windows.append(timeout)
        clock["t"] += 1.0
        return _Resp(
            {
                "run_id": "r-y",
                "status": "completed",
                "output": '{"items":[]}',
                "runtime": {"provider": "openrouter", "model": "m"},
            }
        )

    result = run_research(
        cfg,
        instructions="i",
        input_text="u",
        idempotency_key="k2",
        open_url=open_url,
        sleep=lambda _s: None,
        deadline_s=10.0,
        monotonic=mono,
    )
    assert result.run_id == "r-y"
    # Remaining after 3s create of a 10s budget must be < 10.
    assert poll_windows
    assert poll_windows[0] <= 7.0 + 1e-6


def test_u1_api_key_rotation_blocks_create(tmp_path: Path, monkeypatch: Any) -> None:
    """U1: rotated gateway API key must not create under a new idempotency scope."""
    from jarvise_intel.app import request_sha256, scope_sha256
    from jarvise_intel.digest import load_active_admission, write_active_admission, write_slot_record

    digest_dir, state_dir = _cfg_env(tmp_path, monkeypatch)
    slot = parse_slot("2026-11-26T07")
    instructions = (tmp_path / "prompt.md").read_text(encoding="utf-8")
    input_text = (
        f"Produce the news digest JSON for slot {slot.label} "
        f"(Bangkok {slot.bangkok.isoformat()}, UTC file {slot.filename})."
    )
    req_hash = request_sha256(
        instructions=instructions,
        input_text=input_text,
        provider="openrouter",
        model="test-model",
    )
    old_scope = scope_sha256(gateway_url="http://hermes:8642", api_key="api-key-12345678")
    rec = {
        "status": "create_unconfirmed",
        "attempt": 1,
        "idempotency_key": "2026-11-26T07-a1",
        "started_at": "2026-11-26T05:00:00+00:00",
        "slot": slot.label,
        "request_sha256": req_hash,
        "scope_sha256": old_scope,
        "provider": "openrouter",
        "model": "test-model",
    }
    write_slot_record(state_dir, slot, rec)
    write_active_admission(state_dir, rec)
    creates = {"n": 0}

    def fake_run(*_a: Any, **_k: Any) -> Any:
        creates["n"] += 1
        raise AssertionError("must not create after key rotation")

    monkeypatch.setattr("jarvise_intel.app.run_research", fake_run)
    monkeypatch.setattr("jarvise_intel.app.create_run", fake_run)
    monkeypatch.setenv("JARVISE_HERMES_API_KEY", "rotated-api-key-99999999")
    cfg = AdapterConfig()
    cfg.now = _slot_now("2026-11-26T07")
    code, body = handle_ingest(cfg, {"slot": "2026-11-26T07"})
    assert code == 409
    assert body.get("error") == "owner_recovery_required"
    assert creates["n"] == 0
    active = load_active_admission(state_dir)
    assert active is not None
    assert active.get("status") == "owner_recovery_required"
    _ = digest_dir


def test_u2_unknown_status_blocks_rebuild(tmp_path: Path, monkeypatch: Any) -> None:
    digest_dir, state_dir = _cfg_env(tmp_path, monkeypatch)
    slots = state_dir / "slots"
    slots.mkdir(parents=True)
    (slots / "2026-11-27T07.json").write_text(
        json.dumps(
            {
                "status": "unkn0wn",
                "attempt": 1,
                "run_id": "run-07",
                "idempotency_key": "2026-11-27T07-a1",
                "started_at": "2026-11-27T01:00:00+00:00",
                "slot": "2026-11-27T07",
            }
        ),
        encoding="utf-8",
    )
    creates = {"n": 0}

    def fake_run(*_a: Any, **_k: Any) -> Any:
        creates["n"] += 1
        raise AssertionError("must not create")

    monkeypatch.setattr("jarvise_intel.app.run_research", fake_run)
    cfg = AdapterConfig()
    cfg.now = _slot_now("2026-11-27T19")
    code, body = handle_ingest(cfg, {"slot": "2026-11-27T19"})
    assert code == 409
    assert body.get("error") == "admission_state_invalid"
    assert creates["n"] == 0
    _ = digest_dir


def test_u2_whitespace_status_canonicalizes_and_blocks(tmp_path: Path, monkeypatch: Any) -> None:
    """Whitespace-suffixed unknown must not be skipped as terminal/absent."""
    from jarvise_intel.digest import list_unresolved_slot_records

    digest_dir, state_dir = _cfg_env(tmp_path, monkeypatch)
    slots = state_dir / "slots"
    slots.mkdir(parents=True)
    (slots / "2026-11-27T07.json").write_text(
        json.dumps(
            {
                "status": "unknown ",
                "attempt": 1,
                "run_id": "run-07",
                "idempotency_key": "2026-11-27T07-a1",
                "started_at": "2026-11-27T01:00:00+00:00",
                "slot": "2026-11-27T07",
            }
        ),
        encoding="utf-8",
    )
    # Canonical strip → valid unresolved row; rebuild must block new work.
    rows = list_unresolved_slot_records(state_dir)
    assert len(rows) == 1
    assert rows[0]["status"] == "unknown"
    creates = {"n": 0}

    def fake_run(*_a: Any, **_k: Any) -> Any:
        creates["n"] += 1
        raise AssertionError("must not create")

    def fake_get(_cfg: Any, run_id: str, **_k: Any) -> Any:
        from jarvise_intel.gateway import RunResult

        return RunResult(
            run_id=run_id,
            status="running",
            output="",
            usage={},
            runtime={},
            raw={"status": "running"},
        )

    monkeypatch.setattr("jarvise_intel.app.run_research", fake_run)
    monkeypatch.setattr("jarvise_intel.app.get_run", fake_get)
    cfg = AdapterConfig()
    cfg.now = _slot_now("2026-11-27T19")
    code, body = handle_ingest(cfg, {"slot": "2026-11-27T19"})
    assert code == 409
    assert body.get("error") in {"admission_busy", "unknown_run"}
    assert creates["n"] == 0
    _ = digest_dir


def test_u3_crash_between_slot_and_marker_recovers_run(tmp_path: Path, monkeypatch: Any) -> None:
    """U3: running slot + stale create_unconfirmed marker → repair and resume."""
    from jarvise_intel.digest import load_active_admission, write_active_admission, write_slot_record
    from jarvise_intel.gateway import RunResult

    digest_dir, state_dir = _cfg_env(tmp_path, monkeypatch)
    slot = parse_slot("2026-11-26T07")
    write_slot_record(
        state_dir,
        slot,
        {
            "status": "running",
            "attempt": 1,
            "run_id": "accepted-run",
            "idempotency_key": "2026-11-26T07-a1",
            "started_at": "2026-11-26T05:00:00+00:00",
            "slot": slot.label,
        },
    )
    write_active_admission(
        state_dir,
        {
            "status": "create_unconfirmed",
            "attempt": 1,
            "idempotency_key": "2026-11-26T07-a1",
            "started_at": "2026-11-26T05:00:00+00:00",
            "slot": slot.label,
        },
    )
    gets: list[str] = []
    creates = {"n": 0}

    def fake_get(_cfg: Any, run_id: str, **_k: Any) -> Any:
        gets.append(run_id)
        return RunResult(
            run_id=run_id,
            status="running",
            output="",
            usage={},
            runtime={},
            raw={"status": "running"},
        )

    def fake_run(*_a: Any, **_k: Any) -> Any:
        creates["n"] += 1
        raise AssertionError("must not create")

    monkeypatch.setattr("jarvise_intel.app.get_run", fake_get)
    monkeypatch.setattr("jarvise_intel.app.run_research", fake_run)
    cfg = AdapterConfig()
    cfg.now = _slot_now("2026-11-26T07")
    code, body = handle_ingest(cfg, {"slot": "2026-11-26T07"})
    assert code == 409
    assert body.get("error") == "unknown_run"
    assert gets == ["accepted-run"]
    assert creates["n"] == 0
    # Marker repaired from authoritative running slot (no longer create_unconfirmed).
    active = load_active_admission(state_dir)
    assert active is not None
    assert active.get("status") == "unknown"
    assert active.get("run_id") == "accepted-run"
    _ = digest_dir


def test_v1_second_unresolved_blocks_create_after_first_terminal(tmp_path: Path, monkeypatch: Any) -> None:
    """V1: reconciling oldest A must not create while B remains unresolved."""
    from jarvise_intel.digest import (
        list_unresolved_slot_records,
        load_slot_record,
        write_slot_record,
    )
    from jarvise_intel.gateway import RunResult

    digest_dir, state_dir = _cfg_env(tmp_path, monkeypatch)
    slot_a = parse_slot("2026-11-26T19")
    slot_b = parse_slot("2026-11-27T07")
    write_slot_record(
        state_dir,
        slot_a,
        {
            "status": "unknown",
            "attempt": 1,
            "run_id": "run-a",
            "idempotency_key": "2026-11-26T19-a1",
            "started_at": "2026-11-26T12:00:00+00:00",
            "slot": slot_a.label,
        },
    )
    write_slot_record(
        state_dir,
        slot_b,
        {
            "status": "unknown",
            "attempt": 1,
            "run_id": "run-b",
            "idempotency_key": "2026-11-27T07-a1",
            "started_at": "2026-11-27T01:00:00+00:00",
            "slot": slot_b.label,
        },
    )
    creates = {"n": 0}
    gets: list[str] = []

    def fake_get(_cfg: Any, run_id: str, **_k: Any) -> Any:
        gets.append(run_id)
        # A terminals so reconstruction advances; B stays running/unresolved.
        status = "failed" if run_id == "run-a" else "running"
        return RunResult(
            run_id=run_id,
            status=status,
            output="",
            usage={},
            runtime={},
            raw={"status": status},
        )

    def fake_run(*_a: Any, **_k: Any) -> Any:
        creates["n"] += 1
        raise AssertionError("must not create while B unresolved")

    monkeypatch.setattr("jarvise_intel.app.get_run", fake_get)
    monkeypatch.setattr("jarvise_intel.app.run_research", fake_run)
    monkeypatch.setattr("jarvise_intel.app.create_run", fake_run)
    cfg = AdapterConfig()
    cfg.deadline_s = 600.0
    cfg.now = _slot_now("2026-11-27T19")
    code, body = handle_ingest(cfg, {"slot": "2026-11-27T19"})
    assert creates["n"] == 0
    assert code == 409
    assert body.get("error") in {"admission_busy", "unknown_run"}
    # A reconciled to terminal; B remains durable and unresolved.
    rec_a = load_slot_record(state_dir, slot_a)
    assert rec_a is not None
    assert rec_a.get("status") == "failed_terminal"
    unresolved = list_unresolved_slot_records(state_dir)
    assert any(str(r.get("slot")) == slot_b.label for r in unresolved)
    assert "run-a" in gets
    assert "run-b" in gets
    _ = digest_dir


def test_v1_terminals_plus_later_unknown_block_create(tmp_path: Path, monkeypatch: Any) -> None:
    """V1: multiple terminal rows plus one later unknown must not mint new work."""
    from jarvise_intel.digest import list_unresolved_slot_records, write_slot_record
    from jarvise_intel.gateway import RunResult

    digest_dir, state_dir = _cfg_env(tmp_path, monkeypatch)
    for label, status, started in (
        ("2026-11-25T07", "completed", "2026-11-25T01:00:00+00:00"),
        ("2026-11-25T19", "failed_terminal", "2026-11-25T12:00:00+00:00"),
        ("2026-11-26T07", "invalid_result", "2026-11-26T01:00:00+00:00"),
    ):
        slot = parse_slot(label)
        write_slot_record(
            state_dir,
            slot,
            {
                "status": status,
                "attempt": 1,
                "run_id": f"run-{label}",
                "idempotency_key": f"{label}-a1",
                "started_at": started,
                "slot": label,
            },
        )
    unknown = parse_slot("2026-11-27T07")
    write_slot_record(
        state_dir,
        unknown,
        {
            "status": "unknown",
            "attempt": 1,
            "run_id": "run-later",
            "idempotency_key": "2026-11-27T07-a1",
            "started_at": "2026-11-27T01:00:00+00:00",
            "slot": unknown.label,
        },
    )
    assert len(list_unresolved_slot_records(state_dir)) == 1
    creates = {"n": 0}

    def fake_get(_cfg: Any, run_id: str, **_k: Any) -> Any:
        return RunResult(
            run_id=run_id,
            status="running",
            output="",
            usage={},
            runtime={},
            raw={"status": "running"},
        )

    def fake_run(*_a: Any, **_k: Any) -> Any:
        creates["n"] += 1
        raise AssertionError("must not create")

    monkeypatch.setattr("jarvise_intel.app.get_run", fake_get)
    monkeypatch.setattr("jarvise_intel.app.run_research", fake_run)
    cfg = AdapterConfig()
    cfg.now = _slot_now("2026-11-27T19")
    code, body = handle_ingest(cfg, {"slot": "2026-11-27T19"})
    assert creates["n"] == 0
    assert code == 409
    assert body.get("error") in {"admission_busy", "unknown_run"}
    _ = digest_dir


def test_v2_running_marker_unknown_slot_enforces_deadline(tmp_path: Path, monkeypatch: Any) -> None:
    """V2: running marker + unknown slot must resume/stop the known run, not mismatch."""
    from jarvise_intel.digest import write_active_admission, write_slot_record
    from jarvise_intel.gateway import RunResult

    digest_dir, state_dir = _cfg_env(tmp_path, monkeypatch)
    slot = parse_slot("2026-11-26T07")
    # Mid-window now is 05:00Z; started_at well before deadline_s=600.
    started = "2026-11-26T00:00:00+00:00"
    write_slot_record(
        state_dir,
        slot,
        {
            "status": "unknown",
            "attempt": 1,
            "run_id": "accepted-run",
            "idempotency_key": "2026-11-26T07-a1",
            "started_at": started,
            "slot": slot.label,
        },
    )
    write_active_admission(
        state_dir,
        {
            "status": "running",
            "attempt": 1,
            "run_id": "accepted-run",
            "idempotency_key": "2026-11-26T07-a1",
            "started_at": started,
            "slot": slot.label,
        },
    )
    stops: list[str] = []
    gets: list[str] = []
    creates = {"n": 0}

    def fake_stop(_cfg: Any, run_id: str, **_k: Any) -> None:
        stops.append(run_id)

    def fake_get(_cfg: Any, run_id: str, **_k: Any) -> Any:
        gets.append(run_id)
        return RunResult(
            run_id=run_id,
            status="running",
            output="",
            usage={},
            runtime={},
            raw={"status": "running"},
        )

    def fake_run(*_a: Any, **_k: Any) -> Any:
        creates["n"] += 1
        raise AssertionError("must not create")

    monkeypatch.setattr("jarvise_intel.app.stop_run", fake_stop)
    monkeypatch.setattr("jarvise_intel.app.get_run", fake_get)
    monkeypatch.setattr("jarvise_intel.app.run_research", fake_run)
    cfg = AdapterConfig()
    cfg.deadline_s = 600.0
    # Wall clock far past started_at + 600s.
    cfg.now = _slot_now("2026-11-26T07")
    code, body = handle_ingest(cfg, {"slot": "2026-11-26T07"})
    assert body.get("error") != "admission_state_invalid"
    assert "marker_slot_mismatch" not in str(body.get("detail") or "")
    assert creates["n"] == 0
    assert stops == ["accepted-run"]
    assert gets == ["accepted-run"]
    assert code == 409
    assert body.get("error") == "unknown_run"
    _ = digest_dir


def test_v2_crash_during_marker_write_then_deadline_stop(tmp_path: Path, monkeypatch: Any) -> None:
    """V2: crash between unknown slot write and marker update; restart past deadline."""
    from jarvise_intel.digest import load_active_admission, write_active_admission, write_slot_record
    from jarvise_intel.gateway import RunResult

    digest_dir, state_dir = _cfg_env(tmp_path, monkeypatch)
    slot = parse_slot("2026-11-26T07")
    started = "2026-11-26T04:50:00+00:00"
    write_slot_record(
        state_dir,
        slot,
        {
            "status": "running",
            "attempt": 1,
            "run_id": "accepted-run",
            "idempotency_key": "2026-11-26T07-a1",
            "started_at": started,
            "slot": slot.label,
        },
    )
    write_active_admission(
        state_dir,
        {
            "status": "running",
            "attempt": 1,
            "run_id": "accepted-run",
            "idempotency_key": "2026-11-26T07-a1",
            "started_at": started,
            "slot": slot.label,
        },
    )
    crash = {"armed": True}
    real_write = write_active_admission

    def crashing_write(state: Any, record: dict[str, Any]) -> None:
        # First resume writes unknown slot then marker — abort the marker write.
        if crash["armed"] and str(record.get("status") or "") == "unknown":
            crash["armed"] = False
            raise OSError("simulated crash before marker")
        real_write(state, record)

    monkeypatch.setattr("jarvise_intel.app.write_active_admission", crashing_write)

    def fake_get_running(_cfg: Any, run_id: str, **_k: Any) -> Any:
        return RunResult(
            run_id=run_id,
            status="running",
            output="",
            usage={},
            runtime={},
            raw={"status": "running"},
        )

    monkeypatch.setattr("jarvise_intel.app.get_run", fake_get_running)
    monkeypatch.setattr(
        "jarvise_intel.app.run_research",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no create")),
    )
    cfg = AdapterConfig()
    cfg.deadline_s = 600.0
    # Still inside the research window so resume takes the non-deadline GET path.
    cfg.now = datetime.fromisoformat("2026-11-26T04:55:00+00:00")
    try:
        handle_ingest(cfg, {"slot": "2026-11-26T07"})
    except OSError:
        pass
    # Slot advanced to unknown; marker still running.
    from jarvise_intel.digest import load_slot_record

    slot_rec = load_slot_record(state_dir, slot)
    assert slot_rec is not None
    assert slot_rec.get("status") == "unknown"
    active = load_active_admission(state_dir)
    assert active is not None
    assert active.get("status") == "running"

    stops: list[str] = []
    creates = {"n": 0}

    def fake_stop(_cfg: Any, run_id: str, **_k: Any) -> None:
        stops.append(run_id)

    def fake_get(_cfg: Any, run_id: str, **_k: Any) -> Any:
        return RunResult(
            run_id=run_id,
            status="running",
            output="",
            usage={},
            runtime={},
            raw={"status": "running"},
        )

    def fake_run(*_a: Any, **_k: Any) -> Any:
        creates["n"] += 1
        raise AssertionError("must not create")

    monkeypatch.setattr("jarvise_intel.app.write_active_admission", real_write)
    monkeypatch.setattr("jarvise_intel.app.stop_run", fake_stop)
    monkeypatch.setattr("jarvise_intel.app.get_run", fake_get)
    monkeypatch.setattr("jarvise_intel.app.run_research", fake_run)
    cfg2 = AdapterConfig()
    cfg2.deadline_s = 600.0
    # 04:50Z + 600s = 05:00Z; mid-window now is 05:00Z Bangkok noon — use later.
    cfg2.now = datetime.fromisoformat("2026-11-26T05:10:00+00:00")
    code, body = handle_ingest(cfg2, {"slot": "2026-11-26T07"})
    assert creates["n"] == 0
    assert stops == ["accepted-run"]
    assert code == 409
    assert body.get("error") == "unknown_run"
    assert "marker_slot_mismatch" not in str(body.get("detail") or "")
    _ = digest_dir


def test_v2_create_unconfirmed_marker_unknown_slot_resumes(tmp_path: Path, monkeypatch: Any) -> None:
    """V2: create_unconfirmed marker + unknown slot GETs the stored run, no create."""
    from jarvise_intel.digest import load_active_admission, write_active_admission, write_slot_record
    from jarvise_intel.gateway import RunResult

    digest_dir, state_dir = _cfg_env(tmp_path, monkeypatch)
    slot = parse_slot("2026-11-26T07")
    write_slot_record(
        state_dir,
        slot,
        {
            "status": "unknown",
            "attempt": 1,
            "run_id": "accepted-run",
            "idempotency_key": "2026-11-26T07-a1",
            "started_at": "2026-11-26T05:00:00+00:00",
            "slot": slot.label,
        },
    )
    write_active_admission(
        state_dir,
        {
            "status": "create_unconfirmed",
            "attempt": 1,
            "idempotency_key": "2026-11-26T07-a1",
            "started_at": "2026-11-26T05:00:00+00:00",
            "slot": slot.label,
        },
    )
    gets: list[str] = []
    creates = {"n": 0}

    def fake_get(_cfg: Any, run_id: str, **_k: Any) -> Any:
        gets.append(run_id)
        return RunResult(
            run_id=run_id,
            status="running",
            output="",
            usage={},
            runtime={},
            raw={"status": "running"},
        )

    def fake_run(*_a: Any, **_k: Any) -> Any:
        creates["n"] += 1
        raise AssertionError("must not create")

    monkeypatch.setattr("jarvise_intel.app.get_run", fake_get)
    monkeypatch.setattr("jarvise_intel.app.run_research", fake_run)
    cfg = AdapterConfig()
    cfg.now = _slot_now("2026-11-26T07")
    code, body = handle_ingest(cfg, {"slot": "2026-11-26T07"})
    assert code == 409
    assert body.get("error") == "unknown_run"
    assert gets == ["accepted-run"]
    assert creates["n"] == 0
    active = load_active_admission(state_dir)
    assert active is not None
    assert active.get("status") == "unknown"
    assert active.get("run_id") == "accepted-run"
    _ = digest_dir


def test_w1_marker_ahead_reloads_and_resumes(tmp_path: Path, monkeypatch: Any) -> None:
    """W1: after repairing marker-ahead unknown onto create_unconfirmed, GET the run — no create."""
    from jarvise_intel.app import request_sha256, scope_sha256
    from jarvise_intel.digest import (
        load_active_admission,
        load_slot_record,
        write_active_admission,
        write_slot_record,
    )
    from jarvise_intel.gateway import RunResult

    digest_dir, state_dir = _cfg_env(tmp_path, monkeypatch)
    slot = parse_slot("2026-11-26T07")
    instructions = (tmp_path / "prompt.md").read_text(encoding="utf-8")
    input_text = (
        f"Produce the news digest JSON for slot {slot.label} "
        f"(Bangkok {slot.bangkok.isoformat()}, UTC file {slot.filename})."
    )
    req_hash = request_sha256(
        instructions=instructions,
        input_text=input_text,
        provider="openrouter",
        model="test-model",
    )
    probe = AdapterConfig()
    sc_hash = scope_sha256(gateway_url=probe.gateway_url, api_key=probe.api_key)
    # Mid-window now is 05:00Z; started 60s earlier so create_unconfirmed is still live.
    started = "2026-11-26T04:59:00+00:00"
    write_slot_record(
        state_dir,
        slot,
        {
            "status": "create_unconfirmed",
            "attempt": 1,
            "idempotency_key": "2026-11-26T07-a1",
            "started_at": started,
            "slot": slot.label,
            "request_sha256": req_hash,
            "scope_sha256": sc_hash,
            "provider": "openrouter",
            "model": "test-model",
        },
    )
    write_active_admission(
        state_dir,
        {
            "status": "unknown",
            "attempt": 1,
            "run_id": "accepted-run",
            "idempotency_key": "2026-11-26T07-a1",
            "started_at": started,
            "slot": slot.label,
            "request_sha256": req_hash,
            "scope_sha256": sc_hash,
            "provider": "openrouter",
            "model": "test-model",
        },
    )
    gets: list[str] = []
    creates = {"n": 0}

    def fake_get(_cfg: Any, run_id: str, **_k: Any) -> Any:
        gets.append(run_id)
        return RunResult(
            run_id=run_id,
            status="running",
            output="",
            usage={},
            runtime={},
            raw={"status": "running"},
        )

    def fake_run(*_a: Any, **_k: Any) -> Any:
        creates["n"] += 1
        raise AssertionError("must not create after marker-ahead repair")

    monkeypatch.setattr("jarvise_intel.app.get_run", fake_get)
    monkeypatch.setattr("jarvise_intel.app.run_research", fake_run)
    monkeypatch.setattr("jarvise_intel.app.create_run", fake_run)
    cfg = AdapterConfig()
    cfg.deadline_s = 600.0
    cfg.now = _slot_now("2026-11-26T07")
    code, body = handle_ingest(cfg, {"slot": "2026-11-26T07"})
    assert creates["n"] == 0
    assert gets == ["accepted-run"]
    assert code == 409
    assert body.get("error") == "unknown_run"
    slot_rec = load_slot_record(state_dir, slot)
    assert slot_rec is not None
    assert slot_rec.get("status") == "unknown"
    assert slot_rec.get("run_id") == "accepted-run"
    active = load_active_admission(state_dir)
    assert active is not None
    assert active.get("run_id") == "accepted-run"
    _ = digest_dir


def test_w1_marker_ahead_get_failure_keeps_run_id(tmp_path: Path, monkeypatch: Any) -> None:
    """W1: transport failure after repair must not mint create_unconfirmed without the run id."""
    from jarvise_intel.app import request_sha256, scope_sha256
    from jarvise_intel.digest import (
        load_active_admission,
        load_slot_record,
        write_active_admission,
        write_slot_record,
    )

    digest_dir, state_dir = _cfg_env(tmp_path, monkeypatch)
    slot = parse_slot("2026-11-26T07")
    instructions = (tmp_path / "prompt.md").read_text(encoding="utf-8")
    input_text = (
        f"Produce the news digest JSON for slot {slot.label} "
        f"(Bangkok {slot.bangkok.isoformat()}, UTC file {slot.filename})."
    )
    req_hash = request_sha256(
        instructions=instructions,
        input_text=input_text,
        provider="openrouter",
        model="test-model",
    )
    probe = AdapterConfig()
    sc_hash = scope_sha256(gateway_url=probe.gateway_url, api_key=probe.api_key)
    started = "2026-11-26T04:59:00+00:00"
    write_slot_record(
        state_dir,
        slot,
        {
            "status": "create_unconfirmed",
            "attempt": 1,
            "idempotency_key": "2026-11-26T07-a1",
            "started_at": started,
            "slot": slot.label,
            "request_sha256": req_hash,
            "scope_sha256": sc_hash,
            "provider": "openrouter",
            "model": "test-model",
        },
    )
    write_active_admission(
        state_dir,
        {
            "status": "unknown",
            "attempt": 1,
            "run_id": "accepted-run",
            "idempotency_key": "2026-11-26T07-a1",
            "started_at": started,
            "slot": slot.label,
            "request_sha256": req_hash,
            "scope_sha256": sc_hash,
        },
    )
    gets: list[str] = []
    creates = {"n": 0}

    def fake_get(_cfg: Any, run_id: str, **_k: Any) -> Any:
        gets.append(run_id)
        raise GatewayError("provider_error", "URLError")

    def fake_run(*_a: Any, **_k: Any) -> Any:
        creates["n"] += 1
        raise AssertionError("must not create")

    monkeypatch.setattr("jarvise_intel.app.get_run", fake_get)
    monkeypatch.setattr("jarvise_intel.app.run_research", fake_run)
    monkeypatch.setattr("jarvise_intel.app.create_run", fake_run)
    cfg = AdapterConfig()
    cfg.deadline_s = 600.0
    cfg.now = _slot_now("2026-11-26T07")
    code, body = handle_ingest(cfg, {"slot": "2026-11-26T07"})
    assert creates["n"] == 0
    assert gets == ["accepted-run"]
    assert code == 409
    assert body.get("error") == "unknown_run"
    slot_rec = load_slot_record(state_dir, slot)
    assert slot_rec is not None
    assert slot_rec.get("status") == "unknown"
    assert slot_rec.get("run_id") == "accepted-run"
    active = load_active_admission(state_dir)
    assert active is not None
    assert active.get("status") == "unknown"
    assert active.get("run_id") == "accepted-run"
    _ = digest_dir


def test_load_provenance_rejects_undecodable_bytes(tmp_path: Path) -> None:
    """Invalid UTF-8 provenance fails closed instead of raising."""
    from jarvise_intel.digest import _provenance_path, load_provenance

    slot = parse_slot("2026-11-26T19")
    path = _provenance_path(tmp_path, slot)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfe{")
    assert load_provenance(tmp_path, slot) is None


def test_suffix_list_rejects_bare_tld() -> None:
    """A one-label suffix closes the allowlist; deny_hosts may stay one label."""
    assert load_connect_allowlist(policy={"allow_suffixes": [".com"]}) is None
    assert load_connect_allowlist(policy={"allow_suffixes": ["com"]}) is None
    assert load_connect_allowlist(policy={"allow_suffixes": ["com."]}) is None
    closed_deny = load_connect_allowlist(policy={"allow_suffixes": ["nvidia.com"], "deny_suffixes": [".com"]})
    assert closed_deny is None
    rules = load_connect_allowlist(
        policy={
            "allow_suffixes": ["nvidia.com", ".nvidia.com"],
            "deny_hosts": ["localhost"],
        }
    )
    assert rules is not None
    assert rules["allow_suffixes"] == ["nvidia.com", "nvidia.com"]
    assert rules["deny_hosts"] == ["localhost"]


def test_shipped_hermes_policy_files_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVISE_HERMES_CONNECT_ALLOWLIST", raising=False)
    monkeypatch.chdir(tmp_path)
    connect = load_connect_allowlist()
    assert connect is not None
    assert "openrouter.ai" in connect["allow_suffixes"]
    assert "nvidia.com" in connect["allow_suffixes"]
    source = load_source_policy(SOURCE_POLICY)
    assert "nvidia.com" in source["allow_suffixes"]
    assert "openrouter.ai" not in source["allow_suffixes"]


def test_connect_allowlist_accepts_mapping_not_sequence() -> None:
    from types import MappingProxyType

    proxy = MappingProxyType({"allow_suffixes": ["nvidia.com"], "deny_hosts": ["localhost"]})
    rules = load_connect_allowlist(policy=proxy)
    assert rules is not None
    assert rules["allow_suffixes"] == ["nvidia.com"]
    assert rules["deny_hosts"] == ["localhost"]
    assert load_connect_allowlist(policy=["nvidia.com"]) is None  # type: ignore[arg-type]

    seen: list[str] = []

    def resolve(host: str) -> list[str]:
        seen.append(host)
        return ["8.8.8.8"]

    ok, reason, addrs = allow_connect_target("nvidia.com", 443, resolve=resolve, policy=proxy)
    assert ok is True
    assert reason == "ok"
    assert addrs == ["8.8.8.8"]
    seen.clear()
    closed, why, got = allow_connect_target(
        "nvidia.com",
        443,
        resolve=resolve,
        policy=["nvidia.com"],  # type: ignore[arg-type]
    )
    assert closed is False
    assert why == "connect_allowlist_closed"
    assert got == []
    assert seen == []


def test_allow_connect_target_resolves_normalized_host() -> None:
    policy = {"allow_suffixes": ["nvidia.com"]}
    forms = ("nvidia.com", "NVIDIA.COM", "nvidia.com.", "[nvidia.com]", "NVIDIA.COM.")
    for host in forms:
        seen: list[str] = []

        def resolve(name: str, _seen: list[str] = seen) -> list[str]:
            _seen.append(name)
            return ["8.8.8.8"]

        ok, reason, addrs = allow_connect_target(host, 443, resolve=resolve, policy=policy)
        assert ok is True, (host, reason)
        assert reason == "ok"
        assert addrs == ["8.8.8.8"]
        assert seen == ["nvidia.com"], (host, seen)


def test_source_suffixes_are_covered_by_connect_allowlist() -> None:
    from jarvise_intel.policy import source_suffix_covered_by_connect

    source = json.loads(SOURCE_POLICY.read_text(encoding="utf-8"))
    connect_path = ROOT / "config" / "hermes" / "connect-allowlist.json"
    connect = json.loads(connect_path.read_text(encoding="utf-8"))
    missing = [
        suffix
        for suffix in source["allow_suffixes"]
        if not source_suffix_covered_by_connect(suffix, connect["allow_suffixes"])
    ]
    assert missing == []


def test_source_suffix_coverage_helper_rejects_a_dropped_suffix() -> None:
    from jarvise_intel.policy import source_suffix_covered_by_connect

    allow = ["nvidia.com", "bbc.co.uk"]
    assert source_suffix_covered_by_connect("nvidia.com", allow) is True
    assert source_suffix_covered_by_connect("www.nvidia.com", allow) is True
    assert source_suffix_covered_by_connect("news.bbc.co.uk", allow) is True
    dropped = [suffix for suffix in allow if suffix != "nvidia.com"]
    assert source_suffix_covered_by_connect("nvidia.com", dropped) is False
    assert source_suffix_covered_by_connect("notnvidia.com", allow) is False
