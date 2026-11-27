#!/usr/bin/env python3
"""Validate and strip managed platform_toolsets for Hermes Stage A (F4).

Prefers ``yaml.safe_load`` when PyYAML is installed (CI / local tests).
The pinned Hermes image may lack PyYAML; then a structural subset parser
handles only ``platform_toolsets.api_server`` / ``cli`` (block and flow
lists). It does **not** refuse merely because an unrelated nested
``skills:`` key exists, and it does not use the old text-regex security check.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

ALLOWED_TOOLSETS = frozenset({"web"})
MANAGED_KEYS = ("api_server", "cli")

_HAS_YAML = False
try:
    import yaml

    _HAS_YAML = True
except ImportError:
    yaml = None  # type: ignore[assignment]


def _as_list(value: Any) -> list[Any] | None:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    return None


def _strip_validate_mapping(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    platform = data.get("platform_toolsets")
    if platform is None:
        return data, False
    if not isinstance(platform, dict):
        raise ValueError("platform_toolsets_not_mapping")

    changed = False
    for key in MANAGED_KEYS:
        raw = platform.get(key)
        items = _as_list(raw)
        if items is None:
            raise ValueError(f"toolset_not_list:{key}")
        cleaned = [x for x in items if x != "skills"]
        if cleaned != items:
            changed = True
            platform[key] = cleaned
            items = cleaned
        names = {str(x) for x in items}
        if names != ALLOWED_TOOLSETS:
            raise ValueError(f"unsafe_toolset:{key}:{sorted(names)}")
    return data, changed


def _parse_flow_list(raw: str) -> list[str] | None:
    inner = raw.strip()
    if not (inner.startswith("[") and inner.endswith("]")):
        return None
    body = inner[1:-1].strip()
    if not body:
        return []
    return [part.strip().strip("'\"") for part in body.split(",") if part.strip()]


def _structural_strip_and_validate(text: str) -> tuple[str, bool]:
    """Minimal structural pass when PyYAML is unavailable (Hermes image)."""
    lines = text.splitlines(keepends=True)
    # Locate platform_toolsets block (indent 0).
    start = -1
    for i, line in enumerate(lines):
        if re.match(r"^platform_toolsets\s*:", line):
            start = i
            break
    if start < 0:
        return text, False

    end = len(lines)
    for j in range(start + 1, len(lines)):
        if (
            lines[j].strip()
            and not lines[j].startswith((" ", "\t"))
            and not lines[j].lstrip().startswith("#")
        ):
            end = j
            break

    # Collect managed keys inside the block.
    key_info: dict[str, dict[str, Any]] = {}
    i = start + 1
    while i < end:
        line = lines[i]
        m = re.match(r"^(\s+)(api_server|cli)\s*:\s*(.*)$", line)
        if not m:
            i += 1
            continue
        indent, key, rest = m.group(1), m.group(2), m.group(3).strip()
        flow = _parse_flow_list(rest) if rest else None
        if flow is not None:
            key_info[key] = {"mode": "flow", "index": i, "indent": indent, "items": flow}
            i += 1
            continue
        # Block list: following lines at greater indent starting with "- "
        items: list[str] = []
        item_indexes: list[int] = []
        k = i + 1
        while k < end:
            item_line = lines[k]
            if not item_line.strip() or item_line.lstrip().startswith("#"):
                k += 1
                continue
            im = re.match(r"^(\s+)-\s+(\S+)\s*(#.*)?$", item_line)
            if not im or len(im.group(1)) <= len(indent):
                break
            items.append(im.group(2))
            item_indexes.append(k)
            k += 1
        key_info[key] = {
            "mode": "block",
            "index": i,
            "indent": indent,
            "items": items,
            "item_indexes": item_indexes,
        }
        i = k

    for key in MANAGED_KEYS:
        if key not in key_info:
            raise ValueError(f"missing_toolset:{key}")
        items = list(key_info[key]["items"])
        cleaned = [x for x in items if x != "skills"]
        names = set(cleaned)
        if names != ALLOWED_TOOLSETS:
            raise ValueError(f"unsafe_toolset:{key}:{sorted(names)}")
        key_info[key]["cleaned"] = cleaned
        key_info[key]["changed"] = cleaned != items

    if not any(key_info[k]["changed"] for k in MANAGED_KEYS):
        return text, False

    # Rewrite changed keys only.
    out_lines = list(lines)
    # Process from bottom so indexes stay valid.
    for key in reversed(MANAGED_KEYS):
        info = key_info[key]
        if not info["changed"]:
            continue
        cleaned: list[str] = info["cleaned"]
        if info["mode"] == "flow":
            rendered = ", ".join(cleaned)
            out_lines[info["index"]] = f"{info['indent']}{key}: [{rendered}]\n"
        else:
            # Drop skills item lines; keep web.
            drop = {
                idx for idx, val in zip(info["item_indexes"], info["items"], strict=True) if val == "skills"
            }
            for idx in sorted(drop, reverse=True):
                del out_lines[idx]
    return "".join(out_lines), True


def strip_and_validate(text: str) -> tuple[str, bool]:
    """Return (output_yaml, changed). Raises ValueError on unsafe/unparseable config."""
    if _HAS_YAML:
        assert yaml is not None
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ValueError(f"yaml_parse:{exc}") from exc
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ValueError("yaml_not_mapping")
        data, changed = _strip_validate_mapping(data)
        if not changed:
            return text, False
        out = yaml.safe_dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)
        return out, True
    return _structural_strip_and_validate(text)


def process_path(path: Path, *, write: bool = True) -> int:
    if not path.is_file():
        return 0
    text = path.read_text(encoding="utf-8")
    try:
        out, changed = strip_and_validate(text)
    except ValueError as exc:
        print(f"[hermes-config] refuse: {exc}", file=sys.stderr)
        return 1
    if changed and write:
        path.write_text(out, encoding="utf-8")
        print("[hermes-config] stripped skills from platform_toolsets")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "config",
        nargs="?",
        default="",
        help="Path to config.yaml (default: $HERMES_HOME/config.yaml)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate only; do not write",
    )
    args = parser.parse_args(argv)
    if args.config:
        path = Path(args.config)
    else:
        import os

        path = Path(os.environ.get("HERMES_HOME", "/opt/data")) / "config.yaml"
    return process_path(path, write=not args.check)


if __name__ == "__main__":
    raise SystemExit(main())
