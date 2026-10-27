"""n8n workflows that wake an OpenClaw agent must wait for that run."""

import json
import re
from pathlib import Path

from jarvise.jobs import JOB_TIMEOUTS_S

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
    assert "jarvise-shadow-veto.json" in callers
    assert "jarvise-weekly-report.json" in callers
    assert "jarvise-forward-h3.json" in callers
    shadow = json.loads((WORKFLOWS / "jarvise-shadow-veto.json").read_text(encoding="utf-8"))
    assert shadow["id"] == "jrvsShadowVeto4h1"
    shadow_timeout = next(
        node["parameters"]["options"]["timeout"]
        for node in shadow["nodes"]
        if node.get("type") == "n8n-nodes-base.httpRequest"
    )
    assert shadow_timeout >= 360000
    weekly = json.loads((WORKFLOWS / "jarvise-weekly-report.json").read_text(encoding="utf-8"))
    assert weekly["id"] == "jrvsWeeklyReport1"
    assert weekly["settings"]["timezone"] == "Asia/Bangkok"
    cron = next(
        node["parameters"]["rule"]["interval"][0]["expression"]
        for node in weekly["nodes"]
        if node.get("type") == "n8n-nodes-base.scheduleTrigger"
    )
    assert cron == "0 8 * * 1"
    weekly_timeout = next(
        node["parameters"]["options"]["timeout"]
        for node in weekly["nodes"]
        if node.get("type") == "n8n-nodes-base.httpRequest"
    )
    assert weekly_timeout >= JOB_TIMEOUTS_S["weekly_report"] * 1000
    forward = json.loads((WORKFLOWS / "jarvise-forward-h3.json").read_text(encoding="utf-8"))
    assert forward["id"] == "jrvsForwardH3Daily"
    assert forward["settings"]["timezone"] == "UTC"
    forward_cron = next(
        node["parameters"]["rule"]["interval"][0]["expression"]
        for node in forward["nodes"]
        if node.get("type") == "n8n-nodes-base.scheduleTrigger"
    )
    assert forward_cron == "40 0 * * *"
    forward_timeout = next(
        node["parameters"]["options"]["timeout"]
        for node in forward["nodes"]
        if node.get("type") == "n8n-nodes-base.httpRequest"
    )
    assert forward_timeout >= 360000
    assert forward_timeout >= JOB_TIMEOUTS_S["forward_h3"] * 1000
    assist = json.loads((WORKFLOWS / "jarvise-assist-signal.json").read_text(encoding="utf-8"))
    assert assist["id"] == "jrvsAssistSignal4h"
    assert assist["settings"]["timezone"] == "Asia/Bangkok"
    assist_cron = next(
        node["parameters"]["rule"]["interval"][0]["expression"]
        for node in assist["nodes"]
        if node.get("type") == "n8n-nodes-base.scheduleTrigger"
    )
    assert assist_cron == "25 0,4,8,12,16,20 * * *"
    assist_timeout = next(
        node["parameters"]["options"]["timeout"]
        for node in assist["nodes"]
        if node.get("type") == "n8n-nodes-base.httpRequest"
    )
    assert assist_timeout >= 360000
    assert assist_timeout >= JOB_TIMEOUTS_S["assist_signal"] * 1000
    morning = json.loads((WORKFLOWS / "jarvise-assist-morning.json").read_text(encoding="utf-8"))
    assert morning["id"] == "jrvsAssistMorning1"
    assert morning["settings"]["timezone"] == "Asia/Bangkok"
    morning_cron = next(
        node["parameters"]["rule"]["interval"][0]["expression"]
        for node in morning["nodes"]
        if node.get("type") == "n8n-nodes-base.scheduleTrigger"
    )
    assert morning_cron == "20 8 * * *"
    morning_timeout = next(
        node["parameters"]["options"]["timeout"]
        for node in morning["nodes"]
        if node.get("type") == "n8n-nodes-base.httpRequest"
    )
    assert morning_timeout >= 360000
    assert "jarvise-assist-signal.json" in callers
    assert "jarvise-assist-morning.json" in callers
    assert "jarvise-assist-watch.json" in callers
    watch = json.loads((WORKFLOWS / "jarvise-assist-watch.json").read_text(encoding="utf-8"))
    assert watch["id"] == "jrvsAssistWatch5m"
    assert watch["settings"]["timezone"] == "Asia/Bangkok"
    watch_cron = next(
        node["parameters"]["rule"]["interval"][0]["expression"]
        for node in watch["nodes"]
        if node.get("type") == "n8n-nodes-base.scheduleTrigger"
    )
    assert watch_cron == "*/5 * * * *"
    watch_timeout = next(
        node["parameters"]["options"]["timeout"]
        for node in watch["nodes"]
        if node.get("type") == "n8n-nodes-base.httpRequest"
    )
    assert watch_timeout >= 360000
    assert watch_timeout >= JOB_TIMEOUTS_S["assist_watch"] * 1000
    assist_weekly = json.loads((WORKFLOWS / "jarvise-assist-weekly.json").read_text(encoding="utf-8"))
    assert assist_weekly["id"] == "jrvsAssistWeekly1"
    assert assist_weekly["settings"]["timezone"] == "Asia/Bangkok"
    assist_weekly_cron = next(
        node["parameters"]["rule"]["interval"][0]["expression"]
        for node in assist_weekly["nodes"]
        if node.get("type") == "n8n-nodes-base.scheduleTrigger"
    )
    assert assist_weekly_cron == "30 8 * * 1"
    assist_weekly_timeout = next(
        node["parameters"]["options"]["timeout"]
        for node in assist_weekly["nodes"]
        if node.get("type") == "n8n-nodes-base.httpRequest"
    )
    assert assist_weekly_timeout >= JOB_TIMEOUTS_S["assist_weekly"] * 1000
    assert "jarvise-assist-weekly.json" in callers
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


def test_shadow_veto_cron_follows_paper_run_slots() -> None:
    """Paper-run fires at 00,04,08,12,16,20 Asia/Bangkok :00:54. Shadow is 10-30 min later."""
    shadow = json.loads((WORKFLOWS / "jarvise-shadow-veto.json").read_text(encoding="utf-8"))
    assert shadow["settings"]["timezone"] == "Asia/Bangkok"
    expression = next(
        item["expression"]
        for node in shadow["nodes"]
        if node.get("type") == "n8n-nodes-base.scheduleTrigger"
        for item in node["parameters"]["rule"]["interval"]
        if item.get("field") == "cronExpression"
    )
    minute_s, hour_s, _dom, _month, _dow = expression.split()
    assert minute_s == "15"
    hours = [int(part) for part in hour_s.split(",")]
    assert hours == [0, 4, 8, 12, 16, 20]
    gap_s = (15 * 60) - 54
    assert 10 * 60 <= gap_s <= 30 * 60
