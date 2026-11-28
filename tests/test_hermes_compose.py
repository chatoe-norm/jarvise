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
CI = ROOT / ".github" / "workflows" / "ci.yml"
WORKFLOWS = ROOT / "infra" / "n8n" / "workflows"
PLUGIN_PIN = ROOT / "config" / "hermes" / "plugins" / "mcp-serve" / "PIN"
CONFIG_TMPL = ROOT / "config" / "hermes" / "config.yaml.template"
INIT = ROOT / "infra" / "docker" / "init-hermes-plugins.sh"
CONFIG_HELPER = ROOT / "infra" / "docker" / "hermes_config_toolsets.py"


def _service_block(text: str, service: str) -> str:
    match = re.search(rf"^  {service}:\n(.*?)(?=^  [a-z][\w-]*:\n|^networks:\n|\Z)", text, re.M | re.S)
    assert match, f"service {service!r} not found"
    return match.group(1)


def _networks(block: str) -> set[str]:
    match = re.search(r"^    networks: \[(.*)\]", block, re.M)
    assert match, "networks list missing"
    return {part.strip() for part in match.group(1).split(",") if part.strip()}


def _network_block(text: str, name: str) -> str:
    match = re.search(rf"^  {name}:\n(.*?)(?=^  [a-z][\w-]*:\n|^volumes:\n|\Z)", text, re.M | re.S)
    assert match, f"network {name!r} not found"
    return match.group(1)


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


def test_hermes_image_pin_gateway_run_and_isolation() -> None:
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
    assert 'command: ["gateway", "run"]' in hermes
    assert "hermes-entrypoint.sh" in hermes
    assert _networks(hermes) == {"hermes_net", "hermes_api"}
    assert "hermes_egress" not in _networks(hermes)
    assert "jarvise_net" not in _networks(hermes)
    assert "openclaw_net" not in _networks(hermes)
    assert "mem_limit: 3072m" in hermes
    assert "cpus: 1.5" in hermes

    egress = _service_block(text, "hermes-egress")
    assert _networks(egress) == {"hermes_net", "hermes_egress"}

    ingest = _service_block(text, "hermes-ingest")
    assert _networks(ingest) == {"hermes_api", "hermes_ingress"}
    assert "pilot-digests" in ingest
    assert "hermes_egress" not in _networks(ingest)
    assert "hermes_net" not in _networks(ingest)

    n8n = _networks(_service_block(text, "n8n"))
    assert "hermes_ingress" in n8n
    assert "openclaw_net" in n8n
    assert "8642" not in PROD.read_text(encoding="utf-8")

    for name in ("hermes_net", "hermes_api", "hermes_ingress"):
        block = _network_block(text, name)
        assert "internal: true" in block
        assert "enable_ipv6: false" in block
    egress_net = _network_block(text, "hermes_egress")
    assert "internal: true" not in egress_net
    assert "enable_ipv6: false" in egress_net


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
    assert "--profile hermes" in text
    assert "ssh -T" in text
    assert "18789" not in text
    assert "8642" not in text
    assert "Authorization" not in text
    assert "API_KEY" not in text


def test_vendored_mcp_serve_pin_and_init() -> None:
    assert PLUGIN_PIN.is_file()
    pin = PLUGIN_PIN.read_text(encoding="utf-8")
    assert "80025332933bf84fb3c7b68f369d9fe59a771e98" in pin
    assert "1.0.1" in pin
    assert (ROOT / "config" / "hermes" / "plugins" / "mcp-serve" / "plugin.yaml").is_file()
    assert INIT.is_file()
    init = INIT.read_text(encoding="utf-8")
    assert "mcp-serve" in init
    assert "git clone" not in init.lower()
    assert "plugins enable mcp-serve" in init
    assert "tree_hash" in init
    assert "hermes_config_toolsets.py" in init
    assert CONFIG_HELPER.is_file()
    helper = CONFIG_HELPER.read_text(encoding="utf-8")
    assert "yaml.safe_load" in helper
    assert "skills" in helper
    hermes = _service_block(COMPOSE.read_text(encoding="utf-8"), "hermes")
    assert "hermes_config_toolsets.py" in hermes
    tmpl = CONFIG_TMPL.read_text(encoding="utf-8")
    assert "fallback_providers: []" in tmpl
    assert "mcp-serve" in tmpl
    assert 'default: ""' in tmpl
    assert "- skills" not in tmpl
    assert "- web" in tmpl
    jobs = _service_block(COMPOSE.read_text(encoding="utf-8"), "jobs")
    assert "JARVISE_DIGEST_PRODUCER" in jobs
    deploy = DEPLOY.read_text(encoding="utf-8")
    assert "! -name gateway" in deploy or "exclude" in deploy.lower()
    render = (ROOT / "infra" / "docker" / "render-n8n-hermes-ingest.sh").read_text(encoding="utf-8")
    assert "publish:workflow" not in render
    assert "json.dumps" in render
    env_ex = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "HERMES_IMAGE=" not in env_ex


def test_ci_builds_hermes_images() -> None:
    text = CI.read_text(encoding="utf-8")
    assert "Dockerfile.hermes-ingest" in text
    assert "Dockerfile.hermes-egress" in text


def test_hermes_openrouter_key_is_not_the_shared_host_key() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    hermes = _service_block(text, "hermes")
    jobs = _service_block(text, "jobs")
    openclaw = _service_block(text, "openclaw")
    assert "OPENROUTER_API_KEY=${JARVISE_HERMES_OPENROUTER_KEY:-}" in hermes
    assert "${OPENROUTER_API_KEY" not in hermes
    assert "OPENROUTER_API_KEY=${OPENROUTER_API_KEY:-}" not in hermes
    assert "API_SERVER_KEY=${JARVISE_HERMES_API_KEY:-}" in hermes
    assert "OPENROUTER_API_KEY=${OPENROUTER_API_KEY:-}" in jobs
    assert "OPENROUTER_API_KEY=${OPENROUTER_API_KEY:-}" in openclaw
    egress = _service_block(text, "hermes-egress")
    assert (
        "JARVISE_HERMES_CONNECT_ALLOWLIST=${JARVISE_HERMES_CONNECT_ALLOWLIST-/config/hermes/connect-allowlist.json}"
        in egress
    )
    assert "${JARVISE_HERMES_CONNECT_ALLOWLIST:-" not in egress
    assert "./config/hermes:/config/hermes:ro" in egress
    allow = ROOT / "config" / "hermes" / "connect-allowlist.json"
    assert allow.is_file()
    connect_doc = json.loads(allow.read_text(encoding="utf-8"))
    assert "openrouter.ai" in connect_doc["allow_suffixes"]
    source_doc = json.loads((ROOT / "config" / "hermes" / "source-policy.json").read_text(encoding="utf-8"))
    assert "openrouter.ai" not in source_doc["allow_suffixes"]
    env_ex = (ROOT / ".env.example").read_text(encoding="utf-8")
    match = re.search(r"^JARVISE_HERMES_OPENROUTER_KEY=(.*)$", env_ex, re.M)
    assert match is not None
    assert match.group(1).strip() == ""
    assert "JARVISE_HERMES_OPENCLAW_BACKUP_RETENTION_DAYS=30" in env_ex
    doc = (ROOT / "docs" / "ops" / "hermes-stage-a.md").read_text(encoding="utf-8")
    assert "JARVISE_HERMES_OPENROUTER_KEY" in doc
    assert "JARVISE_HERMES_OPENCLAW_BACKUP_RETENTION_DAYS" in doc
    assert "default **30**" in doc
    assert "stop OpenClaw with its 90s grace" not in doc
    assert "Chana" in doc


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
