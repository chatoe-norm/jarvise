"""n8n workflows that wake an OpenClaw agent must wait for that run."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / "infra" / "n8n" / "workflows"
COMPOSE = ROOT / "docker-compose.yml"
PROD = ROOT / "docker-compose.prod.yml"


def _service_block(text: str, service: str) -> str:
    match = re.search(rf"^  {service}:\n(.*?)(?=^  [a-z][\w-]*:\n|^networks:\n|\Z)", text, re.M | re.S)
    assert match, f"service {service!r} not found"
    return match.group(1)


def _networks(block: str) -> set[str]:
    match = re.search(r"^    networks: \[(.*)\]", block, re.M)
    assert match, "networks list missing"
    return {part.strip() for part in match.group(1).split(",") if part.strip()}


def test_openclaw_network_excludes_redis_jobs_web() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    openclaw = _networks(_service_block(text, "openclaw"))
    assert openclaw == {"openclaw_net"}
    for isolated in ("redis", "jobs", "web"):
        assert "openclaw_net" not in _networks(_service_block(text, isolated))
        assert openclaw.isdisjoint(_networks(_service_block(text, isolated)))
    for shared in ("n8n", "qdrant"):
        nets = _networks(_service_block(text, shared))
        assert "openclaw_net" in nets
        assert "jarvise_net" in nets
    block = _service_block(text, "openclaw")
    assert "mem_limit: 4g" in block
    assert "cpus: 2.0" in block
    assert "NODE_OPTIONS=--max-old-space-size=3072" in block
    assert "stop_grace_period: 90s" in block
    assert "REDIS_URL" not in block
    prod = PROD.read_text(encoding="utf-8")
    assert "${TAILSCALE_IP:?Set TAILSCALE_IP to the VPS Tailscale IPv4}:${OPENCLAW_PORT:-18789}:18789" in prod


def test_jobs_workflows_send_token_placeholder() -> None:
    files = sorted(WORKFLOWS.glob("*.json"))
    callers: list[str] = []
    for path in files:
        workflow = json.loads(path.read_text(encoding="utf-8"))
        for node in workflow.get("nodes", []):
            if node.get("type") != "n8n-nodes-base.httpRequest":
                continue
            params = node.get("parameters") or {}
            url = params.get("url")
            if not isinstance(url, str) or "jobs:8090" not in url:
                continue
            headers = {
                item.get("name"): item.get("value")
                for item in (params.get("headerParameters") or {}).get("parameters") or []
            }
            assert headers.get("X-Jarvise-Token") == "__JARVISE_JOBS_TOKEN__", path.name
            callers.append(path.name)
    assert len(callers) >= 9
    assert "jarvise-live-reconcile.json" in callers
    script = (ROOT / "infra" / "docker" / "render-n8n-jobs-workflows.sh").read_text(encoding="utf-8")
    assert "JARVISE_JOBS_TOKEN" in script
    assert "jarvise-live-reconcile.json" in script
    assert 'echo "$JARVISE_JOBS_TOKEN"' not in script
    assert "echo $JARVISE_JOBS_TOKEN" not in script


def test_hook_agent_http_timeout_covers_timeout_seconds() -> None:
    files = sorted(WORKFLOWS.glob("*.json"))
    assert files, "expected workflow JSON under infra/n8n/workflows"
    checked = 0
    for path in files:
        workflow = json.loads(path.read_text(encoding="utf-8"))
        for node in workflow.get("nodes", []):
            if node.get("type") != "n8n-nodes-base.httpRequest":
                continue
            params = node.get("parameters") or {}
            url = params.get("url")
            if not isinstance(url, str) or "/hooks/agent" not in url:
                continue
            body_raw = params.get("jsonBody")
            if not isinstance(body_raw, str):
                continue
            body = json.loads(body_raw)
            if "timeoutSeconds" not in body:
                continue
            seconds = int(body["timeoutSeconds"])
            timeout_ms = int(params["options"]["timeout"])
            assert timeout_ms >= seconds * 1000, (
                f"{path.name} node {node.get('name')}: timeout {timeout_ms} ms < timeoutSeconds {seconds}"
            )
            checked += 1
    assert checked >= 1
