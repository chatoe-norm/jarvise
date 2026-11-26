"""Digest schema, Markdown render, slot lock, and atomic publication."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jarvise_intel.policy import Resolver, allow_https_url

BANGKOK = timezone(timedelta(hours=7))
ALLOWED_HOURS = frozenset({7, 19})
MAX_ITEMS = 8
SLOT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2})$")
SMOKE_SLOT_RE = re.compile(r"^smoke-(\d{4}-\d{2}-\d{2})T(\d{2})$")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FOOTER = "Not a trade signal. Research context only."

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True)
class DigestItem:
    title: str
    day: str
    url: str
    comment: str


@dataclass(frozen=True)
class Slot:
    label: str
    bangkok: datetime
    utc: datetime
    smoke: bool

    @property
    def filename(self) -> str:
        return self.utc.strftime("%Y-%m-%d-%H.md")

    @property
    def idempotency_key(self) -> str:
        return f"jarvise-hermes-ingest-{self.label}"


def parse_slot(raw: str, *, smoke_allowed: bool = False) -> Slot:
    text = (raw or "").strip()
    smoke = False
    match = SLOT_RE.match(text)
    if match is None and smoke_allowed:
        match = SMOKE_SLOT_RE.match(text)
        smoke = match is not None
    if match is None:
        raise ValueError("invalid_slot")
    day_s, hour_s = match.group(1), match.group(2)
    hour = int(hour_s)
    if hour not in ALLOWED_HOURS:
        raise ValueError("slot_hour_forbidden")
    try:
        day = datetime.strptime(day_s, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("invalid_slot_day") from exc
    bangkok = datetime(day.year, day.month, day.day, hour, 0, 0, tzinfo=BANGKOK)
    utc = bangkok.astimezone(UTC)
    label = text
    return Slot(label=label, bangkok=bangkok, utc=utc, smoke=smoke)


def validate_items(
    payload: Any,
    *,
    resolve: Resolver | None = None,
) -> tuple[list[DigestItem], bool]:
    """Parse gateway JSON into items. Returns (items, no_news)."""
    if not isinstance(payload, dict):
        raise ValueError("payload_not_object")
    no_news = bool(payload.get("no_news"))
    raw_items = payload.get("items")
    if raw_items is None:
        raw_items = []
    if not isinstance(raw_items, list):
        raise ValueError("items_not_list")
    if no_news and raw_items:
        raise ValueError("no_news_with_items")
    if not no_news and not raw_items:
        raise ValueError("empty_items")
    if len(raw_items) > MAX_ITEMS:
        raise ValueError("too_many_items")
    items: list[DigestItem] = []
    for entry in raw_items:
        if not isinstance(entry, dict):
            raise ValueError("item_not_object")
        title = str(entry.get("title") or "").strip()
        day = str(entry.get("day") or "").strip()
        url = str(entry.get("url") or "").strip()
        comment = str(entry.get("comment") or "").strip()
        if not title or not day or not url or not comment:
            raise ValueError("item_fields_missing")
        if not DAY_RE.match(day):
            raise ValueError("bad_timestamp")
        ok, reason = allow_https_url(url, resolve=resolve)
        if not ok:
            raise ValueError(f"url_denied:{reason}")
        if "\n" in title or "\n" in comment:
            raise ValueError("multiline_forbidden")
        items.append(DigestItem(title=title, day=day, url=url, comment=comment))
    return items, no_news


def render_markdown(items: list[DigestItem], *, no_news: bool = False) -> str:
    lines: list[str] = ["# Digest", ""]
    if no_news or not items:
        lines.append("No confirmed watchlist coverage for this slot.")
        lines.append("")
        lines.append(FOOTER)
        lines.append("")
        return "\n".join(lines)
    for index, item in enumerate(items, start=1):
        lines.append(f"{index}. **{item.title}** — {item.day}")
        lines.append(f"   {item.url}")
        lines.append(f"   {item.comment}")
        lines.append("")
    lines.append(FOOTER)
    lines.append("")
    return "\n".join(lines)


def _slot_lock(slot_label: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(slot_label)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[slot_label] = lock
        return lock


def _record_path(state_dir: Path, slot: Slot) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", slot.label)
    return state_dir / "slots" / f"{safe}.json"


def load_slot_record(state_dir: Path, slot: Slot) -> dict[str, Any] | None:
    path = _record_path(state_dir, slot)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_slot_record(state_dir: Path, slot: Slot, record: dict[str, Any]) -> None:
    path = _record_path(state_dir, slot)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    payload = json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True)
    tmp.write_text(payload + "\n", encoding="utf-8")
    with tmp.open("rb+") as handle:
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def publish_digest(
    *,
    digest_dir: Path,
    state_dir: Path,
    slot: Slot,
    markdown: str,
    meta: dict[str, Any],
) -> dict[str, Any]:
    """Atomically publish one digest. Completed slots are immutable."""
    lock = _slot_lock(slot.label)
    with lock:
        existing = load_slot_record(state_dir, slot)
        if existing and existing.get("status") == "completed":
            replayed = dict(existing)
            replayed["replay"] = True
            return replayed

        digest_dir.mkdir(parents=True, exist_ok=True)
        state_dir.mkdir(parents=True, exist_ok=True)
        target = digest_dir / slot.filename
        if ".." in slot.filename or "/" in slot.filename or "\\" in slot.filename:
            raise ValueError("path_traversal")

        if target.exists():
            prior = target.read_bytes()
            prior_hash = hashlib.sha256(prior).hexdigest()
            new_hash = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
            if prior_hash != new_hash:
                raise FileExistsError("filename_conflict")
            record = {
                "status": "completed",
                "slot": slot.label,
                "filename": slot.filename,
                "output_hash": prior_hash,
                "replay": True,
                **meta,
            }
            write_slot_record(state_dir, slot, record)
            return record

        # Temp outside *.md scan (no .md suffix)
        tmp = digest_dir / f".{slot.filename}.tmp"
        data = markdown.encode("utf-8")
        with tmp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        output_hash = hashlib.sha256(data).hexdigest()
        record = {
            "status": "completed",
            "slot": slot.label,
            "filename": slot.filename,
            "output_hash": output_hash,
            "published_at": datetime.now(tz=UTC).isoformat(),
            "replay": False,
            **meta,
        }
        write_slot_record(state_dir, slot, record)
        return record


def extract_json_object(text: str) -> Any:
    """Pull the first JSON object from model output."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty_output")
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("malformed_output") from None
        try:
            return json.loads(raw[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError("malformed_output") from exc
