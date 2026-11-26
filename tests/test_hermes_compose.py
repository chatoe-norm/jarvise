"""Compose/profile/deploy guards for the dormant Hermes Stage A stack."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.yml"
PROD = ROOT / "docker-compose.prod.yml"
DEPLOY = ROOT / "infra" / "deploy" / "vps-deploy.sh"
MCP = ROOT / "scripts" / "hermes-mcp-stdio.ps1"
WORKFLOWS = ROOT / "infra" / "n8n" / "workflows"


def _service_block(text: str, service: str) -> str:
    match = re.search(rf"^  {service}:\n(.*?)(?=^  [a-z][\w-]*:\n|^networks:\n|\Z)", text, re.M | re.S)
    assert match, f"service {service!r} not found"
    return match.group(1)


def _networks(block: str) -> set[str]:
    match = re.search(r"^    networks: \[(.*)\]", block, re.M)
    assert match, "networks list missing"
    return {part.strip() for part in match.group(1).split(",") if part.strip()}


def test_hermes_services_are_profile_gated() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    for name in ("hermes", "hermes-ingest", "hermes-egress"):
        block = _service_block(text, name)
        assert "profiles: [hermes]" in block
        assert "ports:" not in block
        assert "privileged:" not in block
        assert "/var/run/docker.sock" not in block
        assert "network_mode: host" not in block
        assert "./secrets" not in block
        assert "workspace-ingest/digests" not in block
        assert "JARVISE_LIVE_TRADING" not in block
        assert "BINANCE_" not in block
        assert "TELEGRAM_" not in block
        assert "REDIS_URL" not in block


def test_hermes_image_pin_and_isolation() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    hermes = _service_block(text, "hermes")
    image_lines = [line for line in hermes.splitlines() if "image:" in line]
    assert image_lines, "hermes image pin missing"
    assert (
        "nousresearch/hermes-agent@sha256:3bc37fd8546e90fc41fe0d29511b4052840c281617cbcec6093bfe03d86a4ad9"
        in image_lines[0]
    )
    assert "latest" not in image_lines[0]
    assert "stable" not in image_lines[0]
    assert _networks(hermes) == {"hermes_net"}
    assert "jarvise_net" not in _networks(hermes)
    assert "openclaw_net" not in _networks(hermes)
    assert "mem_limit: 3072m" in hermes
    assert "cpus: 1.5" in hermes
    ingest = _service_block(text, "hermes-ingest")
    assert _networks(ingest) == {"hermes_net", "hermes_ingress"}
    assert "pilot-digests" in ingest
    n8n = _networks(_service_block(text, "n8n"))
    assert "hermes_ingress" in n8n
    assert "openclaw_net" in n8n
    assert "8642" not in PROD.read_text(encoding="utf-8")


def test_openclaw_still_default_and_isolated() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    openclaw = _service_block(text, "openclaw")
    assert "profiles:" not in openclaw
    assert _networks(openclaw) == {"openclaw_net"}
    assert "mem_limit: 4g" in openclaw
    assert "ghcr.io/openclaw/openclaw:2026.9.8" in openclaw


def test_deploy_scopes_hermes_gateway_uid() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    assert "data/hermes/gateway" in text
    assert 'chown -R "$hermes_uid:$hermes_gid" data/hermes/gateway' in text
    assert "/opt/learningyard" not in text.lower()
    assert "up -d --profile" not in text
    assert "compose --profile hermes" not in text
    assert "config/hermes/config.yaml.template" in text


def test_mcp_wrapper_is_tools_only_non_root() -> None:
    text = MCP.read_text(encoding="utf-8")
    assert "--user hermes" in text
    assert "mcp-serve --tools-only" in text
    assert "ssh -T" in text
    assert "18789" not in text
    assert "8642" not in text
    assert "Authorization" not in text
    assert "API_KEY" not in text


def test_hermes_workflow_inactive_fixed_body() -> None:
    path = WORKFLOWS / "jarvise-hermes-ingest.json"
    workflow = json.loads(path.read_text(encoding="utf-8"))
    assert workflow.get("active") is False
    assert workflow.get("id") == "jrvsHermesIngest0719"
    blob = json.dumps(workflow)
    assert "hermes-ingest:8092/ingest" in blob
    assert "__JARVISE_HERMES_INGEST_TOKEN__" in blob
    assert "660000" in blob
    assert "hooks/agent" not in blob
    assert "executeCommand" not in blob
    openclaw = json.loads((WORKFLOWS / "jarvise-news-ingest.json").read_text(encoding="utf-8"))
    assert openclaw["id"] == "jrvsNewsIngest0719"
    oc_blob = json.dumps(openclaw)
    assert "http://openclaw:18789/hooks/agent" in oc_blob
    assert "660000" in oc_blob
