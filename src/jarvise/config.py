from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CONFIG_DIR_NAME = ".jarvise"
CONFIG_FILE_NAME = "config.json"


def resolve_root(path: Path) -> Path:
    return path.expanduser().resolve()


def config_file(root: Path) -> Path:
    return resolve_root(root) / CONFIG_DIR_NAME / CONFIG_FILE_NAME


def is_initialized(root: Path) -> bool:
    return config_file(root).is_file()


def read_config(root: Path) -> dict[str, Any]:
    path = config_file(root)
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("config file must contain a JSON object")
    return data


def write_config(root: Path, data: dict[str, Any]) -> None:
    path = config_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
