"""Thread + process locked JSON RMW for partner sidecar state files.

Telegram listen runs as a daemon thread inside jarvise-jobs; ``fcntl.flock``
alone does not exclude other threads in the same process. Use both a module
``threading.Lock`` and a sibling ``.lock`` file flock around load-modify-save.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import secrets
import sqlite3
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

_THREAD_LOCK = threading.Lock()
_OWNER_PREVIEW_TTL_MS = 900_000
_TOKEN_HEX_LEN = 16  # 32 hex chars


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Write JSON via tmp + fsync + os.replace (same idea as partner_audit.save_state)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
    fd, tmp_name = tempfile.mkstemp(prefix="partner_json_", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except OSError:
            pass
        raise


def load_json_object(path: Path) -> dict[str, Any]:
    """Load a JSON object; missing or corrupt → empty dict (no crash)."""
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        logger.warning("partner json store corrupt path=%s; treating as empty", path.name)
        return {}
    if not isinstance(data, dict):
        logger.warning("partner json store not object path=%s; treating as empty", path.name)
        return {}
    return data


def locked_json_update(
    path: Path,
    mutator: Callable[[dict[str, Any]], T],
    *,
    load: Callable[[Path], dict[str, Any]] | None = None,
    save: Callable[[Path, dict[str, Any]], None] | None = None,
) -> T:
    """Serialize load-modify-save with threading.Lock + fcntl.flock.

    ``mutator`` receives the loaded dict (mutable) and returns a result.
    The file is always saved after ``mutator`` returns (mutator must put the
    desired final state into the dict, including no-op identity saves).
    """
    load_fn = load or load_json_object
    save_fn = save or atomic_write_json
    lock_path = path.with_name(path.name + ".lock")
    with _THREAD_LOCK:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a+", encoding="utf-8") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                state = load_fn(path)
                if not isinstance(state, dict):
                    state = {}
                result = mutator(state)
                save_fn(path, state)
                return result
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def _db_file(conn: sqlite3.Connection) -> str:
    row = conn.execute("PRAGMA database_list").fetchone()
    if row is None:
        return ""
    return str(row["file"] if hasattr(row, "keys") else row[2] or "")


def owner_preview_path_for(conn: sqlite3.Connection) -> Path | None:
    db_file = _db_file(conn)
    if not db_file:
        return None
    return Path(db_file).with_name("partner_owner_preview.json")


def _preview_map(state: dict[str, Any]) -> dict[str, Any]:
    raw = state.get("previews")
    if not isinstance(raw, dict):
        state["previews"] = {}
        return state["previews"]
    return raw


def issue_owner_preview(
    path: Path,
    *,
    side: str,
    tran_id: str,
    amount_micro: int,
    issued_at_ms: int,
) -> str:
    """Persist a new single-use preview token; return the hex token."""
    token = secrets.token_hex(_TOKEN_HEX_LEN)

    def mutate(state: dict[str, Any]) -> str:
        state["v"] = 1
        previews = _preview_map(state)
        previews[token] = {
            "side": str(side),
            "tran_id": str(tran_id),
            "amount_micro": int(amount_micro),
            "issued_at_ms": int(issued_at_ms),
        }
        return token

    return locked_json_update(path, mutate)


def revoke_owner_preview(path: Path, token: str) -> bool:
    """Remove a preview token (cancel). True when an entry was removed."""

    def mutate(state: dict[str, Any]) -> bool:
        previews = _preview_map(state)
        return previews.pop(str(token), None) is not None

    return locked_json_update(path, mutate)


def consume_owner_preview(
    path: Path,
    token: str,
    *,
    now_ms: int,
) -> dict[str, Any] | None:
    """Atomically take a still-valid preview.

    Returns a dict with side/tran/amount and ``replay`` True when the token was
    already consumed (so the caller can map a second tap to already_recorded).
    Missing or expired tokens return None.
    """

    def mutate(state: dict[str, Any]) -> dict[str, Any] | None:
        previews = _preview_map(state)
        entry = previews.get(str(token))
        if not isinstance(entry, dict):
            return None
        try:
            issued = int(entry.get("issued_at_ms") or 0)
            side = str(entry.get("side") or "")
            tran_id = str(entry.get("tran_id") or "")
            amount_micro = int(entry.get("amount_micro") or 0)
            consumed = bool(entry.get("consumed"))
        except (TypeError, ValueError):
            previews.pop(str(token), None)
            return None
        if side not in ("dep", "wd") or not tran_id or amount_micro <= 0:
            previews.pop(str(token), None)
            return None
        if int(now_ms) - issued > _OWNER_PREVIEW_TTL_MS or issued > int(now_ms) + 60_000:
            previews.pop(str(token), None)
            return None
        payload = {
            "token": str(token),
            "side": side,
            "tran_id": tran_id,
            "amount_micro": amount_micro,
            "issued_at_ms": issued,
            "replay": consumed,
        }
        if not consumed:
            entry["consumed"] = True
        return payload

    return locked_json_update(path, mutate)


def restore_owner_preview(path: Path, preview: dict[str, Any]) -> None:
    """Clear the consumed flag so a clean apply refusal can retry the same card."""
    token = str(preview.get("token") or "")
    if not token or preview.get("replay"):
        return

    def mutate(state: dict[str, Any]) -> None:
        state["v"] = 1
        previews = _preview_map(state)
        entry = previews.get(token)
        if isinstance(entry, dict):
            entry.pop("consumed", None)
        else:
            previews[token] = {
                "side": str(preview["side"]),
                "tran_id": str(preview["tran_id"]),
                "amount_micro": int(preview["amount_micro"]),
                "issued_at_ms": int(preview["issued_at_ms"]),
            }

    locked_json_update(path, mutate)
