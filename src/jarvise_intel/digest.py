"""Digest schema, Markdown render, cross-process lock, and create-if-absent publication."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jarvise_intel.policy import Resolver, allow_source_url

BANGKOK = timezone(timedelta(hours=7))
ALLOWED_HOURS = frozenset({7, 19})
MAX_ITEMS = 8
MAX_TITLE = 240
MAX_COMMENT = 500
MAX_URL = 2048
ITEM_DAY_MAX_AGE = timedelta(days=14)
SLOT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2})$")
SMOKE_SLOT_RE = re.compile(r"^smoke-(\d{4}-\d{2}-\d{2})T(\d{2})$")
FOOTER = "Not a trade signal. Research context only."

_INFLIGHT = threading.Lock()


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
        base = self.utc.strftime("%Y-%m-%d-%H.md")
        if self.smoke:
            return f"smoke-{base}"
        return base

    @property
    def idempotency_key(self) -> str:
        return f"jarvise-hermes-ingest-{self.label}"

    @property
    def slot_day(self) -> date:
        return self.bangkok.date()


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
    return Slot(label=text, bangkok=bangkok, utc=utc, smoke=smoke)


def current_bangkok_slot(now: datetime | None = None) -> Slot:
    """Active Bangkok digest slot: 07 until 19, then 19 until next 07."""
    moment = now or datetime.now(tz=BANGKOK)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=BANGKOK)
    else:
        moment = moment.astimezone(BANGKOK)
    if moment.hour < 7:
        day = (moment - timedelta(days=1)).date()
        hour = 19
    elif moment.hour < 19:
        day = moment.date()
        hour = 7
    else:
        day = moment.date()
        hour = 19
    return parse_slot(f"{day.isoformat()}T{hour:02d}")


def assert_current_slot(slot: Slot, *, now: datetime | None = None, smoke_allowed: bool = False) -> None:
    """Reject historical/future normal slots. Smoke slots skip the current-window check."""
    if slot.smoke:
        if not smoke_allowed:
            raise ValueError("smoke_forbidden")
        return
    current = current_bangkok_slot(now)
    if slot.label != current.label:
        raise ValueError("slot_not_current")


def validate_items(
    payload: Any,
    *,
    resolve: Resolver | None = None,
    slot: Slot | None = None,
    policy_path: Path | None = None,
) -> tuple[list[DigestItem], bool]:
    """Parse gateway JSON into items. Returns (items, no_news)."""
    if not isinstance(payload, dict):
        raise ValueError("payload_not_object")
    if "no_news" not in payload:
        no_news = False
    else:
        raw_no_news = payload.get("no_news")
        if not isinstance(raw_no_news, bool):
            raise ValueError("no_news_type")
        no_news = raw_no_news
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
    ref_day = slot.slot_day if slot is not None else datetime.now(tz=BANGKOK).date()
    for entry in raw_items:
        if not isinstance(entry, dict):
            raise ValueError("item_not_object")
        title_raw = entry.get("title")
        day_raw = entry.get("day")
        url_raw = entry.get("url")
        comment_raw = entry.get("comment")
        if not isinstance(title_raw, str) or not isinstance(day_raw, str):
            raise ValueError("item_fields_type")
        if not isinstance(url_raw, str) or not isinstance(comment_raw, str):
            raise ValueError("item_fields_type")
        title = title_raw.strip()
        day = day_raw.strip()
        url = url_raw.strip()
        comment = comment_raw.strip()
        if not title or not day or not url or not comment:
            raise ValueError("item_fields_missing")
        if len(title) > MAX_TITLE or len(comment) > MAX_COMMENT or len(url) > MAX_URL:
            raise ValueError("item_fields_too_long")
        if "\n" in title or "\n" in comment:
            raise ValueError("multiline_forbidden")
        try:
            item_day = datetime.strptime(day, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError("bad_timestamp") from exc
        if item_day > ref_day:
            raise ValueError("day_in_future")
        if ref_day - item_day > ITEM_DAY_MAX_AGE:
            raise ValueError("day_too_old")
        ok, reason = allow_source_url(url, resolve=resolve, policy_path=policy_path)
        if not ok:
            raise ValueError(f"url_denied:{reason}")
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


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            handle.write(b"0")
            handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if sys.platform == "win32":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def _record_path(state_dir: Path, slot: Slot) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", slot.label)
    return state_dir / "slots" / f"{safe}.json"


def _filename_lock_path(state_dir: Path, filename: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", filename)
    return state_dir / "locks" / f"{safe}.lock"


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
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        _fsync_dir(path.parent)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def _fsync_dir(directory: Path) -> None:
    if sys.platform == "win32":
        return
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _link_create_if_absent(tmp: Path, target: Path) -> None:
    """Create target from tmp without overwriting an existing file."""
    try:
        os.link(tmp, target)
    except FileExistsError:
        raise
    except OSError:
        # Windows may lack link privileges; fall back to exclusive create.
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        try:
            fd = os.open(str(target), flags, 0o644)
        except FileExistsError:
            raise
        try:
            with open(tmp, "rb") as src, os.fdopen(fd, "wb") as dst:
                dst.write(src.read())
                dst.flush()
                os.fsync(dst.fileno())
        except Exception:
            try:
                os.unlink(target)
            except OSError:
                pass
            raise


def publish_digest(
    *,
    digest_dir: Path,
    state_dir: Path,
    slot: Slot,
    markdown: str,
    meta: dict[str, Any],
) -> dict[str, Any]:
    """Create-if-absent publication keyed by output filename. Never overwrite."""
    if ".." in slot.filename or "/" in slot.filename or "\\" in slot.filename:
        raise ValueError("path_traversal")

    digest_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = _filename_lock_path(state_dir, slot.filename)
    data = markdown.encode("utf-8")
    new_hash = hashlib.sha256(data).hexdigest()
    target = digest_dir / slot.filename

    with _file_lock(lock_path):
        existing = load_slot_record(state_dir, slot)
        if existing and existing.get("status") == "completed":
            replayed = dict(existing)
            replayed["replay"] = True
            return replayed

        if target.exists():
            prior = target.read_bytes()
            prior_hash = hashlib.sha256(prior).hexdigest()
            if prior_hash != new_hash:
                raise FileExistsError("filename_conflict")
            record = {
                "status": "completed",
                "slot": slot.label,
                "filename": slot.filename,
                "output_hash": prior_hash,
                "published_at": datetime.fromtimestamp(target.stat().st_mtime, tz=UTC).isoformat(),
                "replay": True,
                **meta,
            }
            write_slot_record(state_dir, slot, record)
            return record

        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{uuid.uuid4().hex}.",
            suffix=".tmp",
            dir=str(digest_dir),
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                _link_create_if_absent(tmp, target)
            except FileExistsError as exc:
                prior = target.read_bytes()
                prior_hash = hashlib.sha256(prior).hexdigest()
                if prior_hash != new_hash:
                    raise FileExistsError("filename_conflict") from exc
                record = {
                    "status": "completed",
                    "slot": slot.label,
                    "filename": slot.filename,
                    "output_hash": prior_hash,
                    "published_at": datetime.fromtimestamp(target.stat().st_mtime, tz=UTC).isoformat(),
                    "replay": True,
                    **meta,
                }
                write_slot_record(state_dir, slot, record)
                return record
            _fsync_dir(digest_dir)
            record = {
                "status": "completed",
                "slot": slot.label,
                "filename": slot.filename,
                "output_hash": new_hash,
                "published_at": datetime.now(tz=UTC).isoformat(),
                "replay": False,
                **meta,
            }
            write_slot_record(state_dir, slot, record)
            return record
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass


def try_acquire_inflight() -> bool:
    return _INFLIGHT.acquire(blocking=False)


def release_inflight() -> None:
    try:
        _INFLIGHT.release()
    except RuntimeError:
        pass


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
