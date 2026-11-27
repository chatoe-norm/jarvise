"""Unit tests for infra/docker/hermes_config_toolsets.py (F4 / R2 / R3)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "infra" / "docker" / "hermes_config_toolsets.py"


def _load():
    spec = importlib.util.spec_from_file_location("hermes_config_toolsets", HELPER)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hermes_config_toolsets"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_block_list_strips_skills_and_validates(tmp_path: Path) -> None:
    mod = _load()
    text = (
        "model:\n  default: keep-me\n  provider: openrouter\n"
        "platform_toolsets:\n"
        "  api_server:\n    - web\n    - skills\n"
        "  cli:\n    - web\n"
    )
    out, changed = mod.strip_and_validate(text)
    assert changed is True
    assert "keep-me" in out
    assert "- skills" not in out
    out2, changed2 = mod.strip_and_validate(out)
    assert changed2 is False
    assert out2 == out


def test_flow_list_strips_skills() -> None:
    mod = _load()
    text = "platform_toolsets:\n  api_server: [web, skills]\n  cli: [web]\n"
    out, changed = mod.strip_and_validate(text)
    assert changed is True
    again, changed2 = mod.strip_and_validate(out)
    assert changed2 is False
    assert again == out


def test_nested_skills_mapping_ignored() -> None:
    mod = _load()
    text = (
        "plugins:\n  entries:\n    mcp-serve:\n      skills:\n        foo: 1\n"
        "platform_toolsets:\n  api_server: [web]\n  cli: [web]\n"
    )
    out, changed = mod.strip_and_validate(text)
    assert changed is False
    assert out == text
    assert "skills:" in out


def test_unsafe_terminal_refused() -> None:
    mod = _load()
    text = "platform_toolsets:\n  api_server: [web, terminal]\n  cli: [web]\n"
    with pytest.raises(ValueError, match="unsafe_toolset"):
        mod.strip_and_validate(text)


def test_unsafe_skill_manage_refused() -> None:
    mod = _load()
    text = "platform_toolsets:\n  api_server: [web, skill_manage]\n  cli: [web]\n"
    with pytest.raises(ValueError, match="unsafe_toolset"):
        mod.strip_and_validate(text)


def test_process_path_writes_and_check(tmp_path: Path) -> None:
    mod = _load()
    path = tmp_path / "config.yaml"
    path.write_text(
        "platform_toolsets:\n  api_server:\n    - skills\n    - web\n  cli:\n    - web\n",
        encoding="utf-8",
    )
    assert mod.process_path(path, write=True) == 0
    text = path.read_text(encoding="utf-8")
    assert "- skills" not in text
    assert mod.process_path(path, write=False) == 0
    bad = tmp_path / "bad.yaml"
    bad.write_text("platform_toolsets:\n  api_server: [terminal]\n  cli: [web]\n", encoding="utf-8")
    assert mod.process_path(bad, write=True) == 1
    assert bad.read_text(encoding="utf-8").startswith("platform_toolsets:")


def test_structural_fallback_r2_r3(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production path without PyYAML: fail closed + descending edits (R2/R3)."""
    mod = _load()
    monkeypatch.setattr(mod, "_HAS_YAML", False)
    monkeypatch.setattr(mod, "yaml", None)

    # Quoted root key refused.
    with pytest.raises(ValueError, match="quoted_key"):
        mod.strip_and_validate('"platform_toolsets":\n  api_server: [web, terminal]\n  cli: [web]\n')

    # Nested safe api_server must not mask direct unsafe list.
    with pytest.raises(ValueError, match="unsafe_toolset"):
        mod.strip_and_validate(
            "platform_toolsets:\n"
            "  api_server: [web, terminal]\n"
            "  cli: [web]\n"
            "  extra:\n"
            "    api_server: [web]\n"
        )

    # Missing platform_toolsets refused.
    with pytest.raises(ValueError, match="missing_platform_toolsets"):
        mod.strip_and_validate("model:\n  default: x\n")

    # Merge refused.
    with pytest.raises(ValueError, match="merge_unsupported"):
        mod.strip_and_validate("platform_toolsets:\n  <<: {x: 1}\n  api_server: [web]\n  cli: [web]\n")
    # Alias refused (no anchor defined in this snippet).
    with pytest.raises(ValueError, match="alias_unsupported"):
        mod.strip_and_validate("platform_toolsets:\n  api_server: *d\n  cli: [web]\n")

    # cli before api_server: strip both skills, keep model (R3).
    text = (
        "platform_toolsets:\n"
        "  cli:\n    - web\n    - skills\n"
        "  api_server:\n    - web\n    - skills\n"
        "model: {default: keep-me}\n"
    )
    out, changed = mod.strip_and_validate(text)
    assert changed is True
    assert "keep-me" in out
    assert "skills" not in [ln.strip() for ln in out.splitlines() if ln.strip().startswith("- ")]
    assert "- web" in out or "[web]" in out

    # Mixed flow + block.
    mixed = (
        "platform_toolsets:\n"
        "  api_server: [web, skills]\n"
        "  cli:\n    - web\n    - skills\n"
        "owner_key: preserved\n"
    )
    mout, mchanged = mod.strip_and_validate(mixed)
    assert mchanged is True
    assert "owner_key: preserved" in mout
    assert "skills" not in [ln.strip() for ln in mout.splitlines() if "skills" in ln and "entries" not in ln]
