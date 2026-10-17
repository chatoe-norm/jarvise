"""n8n workflows that wake an OpenClaw agent must wait for that run."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / "infra" / "n8n" / "workflows"


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
