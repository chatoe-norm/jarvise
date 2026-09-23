from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "jarvise-openclaw"
SKILLS = ("jarvise-paper-research", "jarvise-doctrine-rag", "jarvise-binance-intel")
INTEL_HOSTS = {"web3.binance.com", "www.binance.com", "developers.binance.com", "github.com"}


def test_openclaw_plugin_manifest_valid() -> None:
    manifest_path = PLUGIN / "openclaw.plugin.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert data["id"] == "jarvise"
    assert data["configSchema"]["type"] == "object"
    for name in SKILLS:
        assert f"./skills/{name}" in data["skills"]
    for rel in data["skills"]:
        skill = PLUGIN / rel / "SKILL.md"
        assert skill.is_file(), skill


def test_openclaw_skill_frontmatter() -> None:
    for name in SKILLS:
        text = (PLUGIN / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
        assert text.startswith("---\n")
        match = re.match(r"^---\n(.*?)\n---\n", text, flags=re.DOTALL)
        assert match, f"missing frontmatter in {name}"
        body = match.group(1)
        assert re.search(r"^name:\s*\S+", body, flags=re.M)
        assert re.search(r"^description:\s*\S+", body, flags=re.M)
        assert "paper" in text.lower()
        assert "order" in text.lower()


def test_binance_intel_urls_are_allowlisted_reads() -> None:
    text = (PLUGIN / "skills" / "jarvise-binance-intel" / "SKILL.md").read_text(encoding="utf-8")
    urls = re.findall(r"https://[^\s)'\"`]+", text)
    paths = re.findall(r"`(/bapi/[^`]+)`", text)
    assert urls
    assert paths
    for url in urls:
        assert urlparse(url).hostname in INTEL_HOSTS, url
    for path in [urlparse(u).path for u in urls] + paths:
        lowered = path.lower()
        for word in ("order", "withdraw", "transfer"):
            assert word not in lowered, path


def test_cursor_binance_intel_points_at_canonical_skill() -> None:
    text = (ROOT / ".cursor" / "skills" / "jarvise-binance-intel" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert re.match(r"^---\nname: jarvise-binance-intel\n", text)
    assert "plugins/jarvise-openclaw/skills/jarvise-binance-intel/SKILL.md" in text


def test_openclaw_example_loads_jarvise_skills() -> None:
    example = json.loads(
        (ROOT / "config" / "openclaw" / "openclaw.json.example").read_text(encoding="utf-8")
    )
    assert "/plugins/jarvise-openclaw/skills" in example["skills"]["load"]["extraDirs"]
    assert "_jarvise" not in example
    assert "plugins" not in example
    assert example["env"]["vars"]["OPENCLAW_PAPER_ONLY"] == "true"
