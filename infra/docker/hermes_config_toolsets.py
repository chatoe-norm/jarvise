#!/usr/bin/env python3
"""Validate and strip managed platform_toolsets for Hermes Stage A (R2/R3).

Prefers ``yaml.safe_load`` when PyYAML is installed (CI / local tests).
Without PyYAML (pinned Hermes image), a fail-closed structural parser
handles only column-0 ``platform_toolsets`` with direct ``api_server`` /
``cli`` children. Quoted keys, merges, anchors/aliases, and missing
managed keys refuse start. Nested ``api_server`` under another key cannot
mask the direct unsafe list.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
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


def _require_toolsets(platform: dict[str, Any]) -> tuple[dict[str, list[Any]], bool]:
    """Return cleaned managed lists and whether skills was stripped."""
    changed = False
    cleaned_map: dict[str, list[Any]] = {}
    for key in MANAGED_KEYS:
        if key not in platform:
            raise ValueError(f"missing_toolset:{key}")
        items = _as_list(platform.get(key))
        if items is None:
            raise ValueError(f"toolset_not_list:{key}")
        cleaned = [x for x in items if x != "skills"]
        if cleaned != items:
            changed = True
        names = {str(x) for x in cleaned}
        if names != ALLOWED_TOOLSETS:
            raise ValueError(f"unsafe_toolset:{key}:{sorted(names)}")
        cleaned_map[key] = cleaned
        platform[key] = cleaned
    return cleaned_map, changed


def _strip_validate_mapping(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    platform = data.get("platform_toolsets")
    if platform is None:
        raise ValueError("missing_platform_toolsets")
    if not isinstance(platform, dict):
        raise ValueError("platform_toolsets_not_mapping")
    _cleaned, changed = _require_toolsets(platform)
    return data, changed


def _parse_flow_list(raw: str) -> list[str] | None:
    inner = raw.strip()
    if not (inner.startswith("[") and inner.endswith("]")):
        return None
    body = inner[1:-1].strip()
    if not body:
        return []
    return [part.strip().strip("'\"") for part in body.split(",") if part.strip()]


def _reject_unsupported_syntax(text: str) -> None:
    """Fail closed on YAML features the structural parser does not understand."""
    # Quoted keys at any indentation (S1: nested "api_server" must not bypass).
    if re.search(r"(?m)^[ \t]*[\"'][^\"'\n]+[\"'][ \t]*:", text):
        raise ValueError("quoted_key_unsupported")
    if "<<" in text:
        raise ValueError("merge_unsupported")
    # Anchors / aliases (not inside comments).
    for line in text.splitlines():
        stripped = line.split("#", 1)[0]
        if re.search(r"(^|[\s\[\{,])&[A-Za-z0-9_-]+", stripped):
            raise ValueError("anchor_unsupported")
        if re.search(r"(^|[\s\[\{,])\*[A-Za-z0-9_-]+", stripped):
            raise ValueError("alias_unsupported")
        # Explicit keys (? key / following :) anywhere — including after the
        # first platform_toolsets block (T4). Tags likewise.
        code = stripped.rstrip()
        if not code:
            continue
        if re.match(r"^[ \t]*\?", code):
            raise ValueError("explicit_key_unsupported")
        if re.match(r"^[ \t]*:[ \t]", code) or re.match(r"^[ \t]*:$", code):
            raise ValueError("explicit_key_unsupported")
        if re.search(r"(^|[\s,\[\{])![A-Za-z]", code):
            raise ValueError("tag_unsupported")


_DIRECT_KEY = re.compile(r"^(\s+)([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$")
_DIRECT_BAD_KEY = re.compile(r"^(\s+)([\"'].+)$")


def _structural_strip_and_validate(text: str) -> tuple[str, bool]:
    """Fail-closed structural pass when PyYAML is unavailable (Hermes image)."""
    _reject_unsupported_syntax(text)
    lines = text.splitlines(keepends=True)

    starts = [i for i, line in enumerate(lines) if re.match(r"^platform_toolsets\s*:", line)]
    if not starts:
        raise ValueError("missing_platform_toolsets")
    if len(starts) > 1:
        raise ValueError("duplicate_platform_toolsets")
    start = starts[0]

    end = len(lines)
    for j in range(start + 1, len(lines)):
        if (
            lines[j].strip()
            and not lines[j].startswith((" ", "\t"))
            and not lines[j].lstrip().startswith("#")
        ):
            end = j
            break

    # First non-comment child indent under platform_toolsets defines the direct level.
    direct_indent: int | None = None
    for j in range(start + 1, end):
        if not lines[j].strip() or lines[j].lstrip().startswith("#"):
            continue
        direct_indent = len(lines[j]) - len(lines[j].lstrip(" "))
        break
    if direct_indent is None:
        raise ValueError("empty_platform_toolsets")

    key_info: dict[str, dict[str, Any]] = {}
    i = start + 1
    while i < end:
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        indent = len(line) - len(line.lstrip(" "))
        # Nested mappings deeper than direct children are ignored for managed keys
        # (they cannot replace column-direct api_server/cli).
        if indent > direct_indent:
            i += 1
            continue
        if indent < direct_indent:
            break
        # Refuse quoted / explicit unsupported key forms at the managed indent (S1).
        if _DIRECT_BAD_KEY.match(line):
            raise ValueError("quoted_key_unsupported")
        m = _DIRECT_KEY.match(line)
        if not m:
            raise ValueError("unsupported_platform_toolsets_entry")
        key, rest = m.group(2), m.group(3).strip()
        if key not in MANAGED_KEYS:
            # Non-managed direct children (e.g. owner notes) are not silently
            # treated as toolset overrides — refuse ambiguous siblings.
            raise ValueError(f"unsupported_toolset_key:{key}")
        if key in key_info:
            raise ValueError(f"duplicate_toolset:{key}")
        flow = _parse_flow_list(rest) if rest else None
        if flow is not None:
            key_info[key] = {
                "mode": "flow",
                "index": i,
                "indent": m.group(1),
                "items": flow,
                "item_indexes": [],
            }
            i += 1
            continue
        items: list[str] = []
        item_indexes: list[int] = []
        k = i + 1
        while k < end:
            item_line = lines[k]
            if not item_line.strip() or item_line.lstrip().startswith("#"):
                k += 1
                continue
            im = re.match(r"^(\s+)-\s+(\S+)\s*(#.*)?$", item_line)
            if not im or len(im.group(1)) <= indent:
                break
            items.append(im.group(2))
            item_indexes.append(k)
            k += 1
        key_info[key] = {
            "mode": "block",
            "index": i,
            "indent": m.group(1),
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

    # Collect deletion/rewrite ops by descending source offsets (R3).
    ops: list[tuple[int, str, dict[str, Any]]] = []
    for key in MANAGED_KEYS:
        info = key_info[key]
        if not info["changed"]:
            continue
        ops.append((info["index"], key, info))
    ops.sort(key=lambda t: t[0], reverse=True)

    out_lines = list(lines)
    for _idx, _key, info in ops:
        cleaned: list[str] = info["cleaned"]
        if info["mode"] == "flow":
            rendered = ", ".join(cleaned)
            out_lines[info["index"]] = f"{info['indent']}{_key}: [{rendered}]\n"
        else:
            drop = {
                line_i
                for line_i, val in zip(info["item_indexes"], info["items"], strict=True)
                if val == "skills"
            }
            for line_i in sorted(drop, reverse=True):
                del out_lines[line_i]

    out = "".join(out_lines)
    # Re-validate before returning (refuse to write a broken rewrite).
    again, changed_again = _structural_strip_and_validate_nocheck(out)
    if changed_again:
        raise ValueError("rewrite_still_dirty")
    # Ensure both keys are still exactly {web}.
    _ = again
    return out, True


def _structural_strip_and_validate_nocheck(text: str) -> tuple[str, bool]:
    """Second-pass validate that does not rewrite (detect residual skills)."""
    _reject_unsupported_syntax(text)
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if re.match(r"^platform_toolsets\s*:", line)]
    if not starts:
        raise ValueError("missing_platform_toolsets")
    start = starts[0]
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if (
            lines[j].strip()
            and not lines[j].startswith((" ", "\t"))
            and not lines[j].lstrip().startswith("#")
        ):
            end = j
            break
    direct_indent: int | None = None
    for j in range(start + 1, end):
        if not lines[j].strip() or lines[j].lstrip().startswith("#"):
            continue
        direct_indent = len(lines[j]) - len(lines[j].lstrip(" "))
        break
    if direct_indent is None:
        raise ValueError("empty_platform_toolsets")
    found: dict[str, list[str]] = {}
    i = start + 1
    while i < end:
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent > direct_indent:
            i += 1
            continue
        if indent < direct_indent:
            break
        if _DIRECT_BAD_KEY.match(line):
            raise ValueError("quoted_key_unsupported")
        m = _DIRECT_KEY.match(line)
        if not m:
            raise ValueError("unsupported_platform_toolsets_entry")
        key, rest = m.group(2), m.group(3).strip()
        if key not in MANAGED_KEYS:
            raise ValueError(f"unsupported_toolset_key:{key}")
        if key in found:
            raise ValueError(f"duplicate_toolset:{key}")
        flow = _parse_flow_list(rest) if rest else None
        if flow is not None:
            found[key] = flow
            i += 1
            continue
        items = []
        k = i + 1
        while k < end:
            item_line = lines[k]
            if not item_line.strip() or item_line.lstrip().startswith("#"):
                k += 1
                continue
            im = re.match(r"^(\s+)-\s+(\S+)\s*(#.*)?$", item_line)
            if not im or len(im.group(1)) <= indent:
                break
            items.append(im.group(2))
            k += 1
        found[key] = items
        i = k
    for key in MANAGED_KEYS:
        if key not in found:
            raise ValueError(f"missing_toolset:{key}")
        names = {str(x) for x in found[key] if x != "skills"}
        if "skills" in found[key]:
            return text, True
        if names != ALLOWED_TOOLSETS:
            raise ValueError(f"unsafe_toolset:{key}:{sorted(names)}")
    return text, False


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
        # Confirm dump still validates.
        reloaded = yaml.safe_load(out)
        if not isinstance(reloaded, dict):
            raise ValueError("rewrite_invalid")
        _strip_validate_mapping(reloaded)
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
        # Atomic replace after re-validation already done in strip_and_validate.
        fd, tmp_name = tempfile.mkstemp(
            prefix=".hermes-config-",
            suffix=".yaml",
            dir=str(path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(out)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
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
        path = Path(os.environ.get("HERMES_HOME", "/opt/data")) / "config.yaml"
    return process_path(path, write=not args.check)


if __name__ == "__main__":
    raise SystemExit(main())
