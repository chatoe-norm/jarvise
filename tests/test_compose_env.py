"""Web and jobs must agree on the paper universe the Ops page edits."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _service_env(text: str, service: str) -> dict[str, str]:
    """`KEY=value` lines under one top-level compose service (no YAML dependency)."""
    match = re.search(rf"^  {service}:\n(.*?)(?=^  [a-z][\w-]*:\n|\Z)", text, re.M | re.S)
    assert match, f"service {service!r} not found in docker-compose.yml"
    env: dict[str, str] = {}
    for key, value in re.findall(r"^\s+- ([A-Z][A-Z0-9_]*)=(.*)$", match.group(1), re.M):
        env[key] = value.strip()
    return env


def test_web_and_jobs_share_paper_universe_env() -> None:
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    web = _service_env(text, "web")
    jobs = _service_env(text, "jobs")
    for key in ("JARVISE_PAPER_UNIVERSE", "JARVISE_PAPER_TIMEFRAME"):
        assert key in web, f"web service is missing {key}"
        assert web[key] == jobs[key], f"{key} differs between web and jobs"
