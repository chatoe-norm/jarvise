from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "jarvise-openclaw"


def test_openclaw_plugin_manifest_valid() -> None:
    manifest_path = PLUGIN / "openclaw.plugin.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert data["id"] == "jarvise"
    assert data["configSchema"]["type"] == "object"
    assert "./skills/jarvise-paper-research" in data["skills"]
    assert "./skills/jarvise-doctrine-rag" in data["skills"]
    for rel in data["skills"]:
        skill = PLUGIN / rel / "SKILL.md"
        assert skill.is_file(), skill


def test_openclaw_skill_frontmatter() -> None:
    for name in ("jarvise-paper-research", "jarvise-doctrine-rag"):
        text = (PLUGIN / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
        assert text.startswith("---\n")
        match = re.match(r"^---\n(.*?)\n---\n", text, flags=re.DOTALL)
        assert match, f"missing frontmatter in {name}"
        body = match.group(1)
        assert re.search(r"^name:\s*\S+", body, flags=re.M)
        assert re.search(r"^description:\s*\S+", body, flags=re.M)
        assert "paper" in text.lower()
        assert "order" in text.lower()


def test_openclaw_example_enables_plugin() -> None:
    example = json.loads(
        (ROOT / "config" / "openclaw" / "openclaw.json.example").read_text(encoding="utf-8")
    )
    assert "/plugins/jarvise-openclaw" in example["plugins"]["load"]["paths"]
    assert example["plugins"]["entries"]["jarvise"]["enabled"] is True
    assert example["_jarvise"]["paper_only"] is True
    assert example["_jarvise"]["export_dir"] == "/home/node/.openclaw/exports"
