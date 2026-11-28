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

from jarvise_intel.policy import SOURCE_URL_RE, allow_source_url

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
PILOT_DIGEST_MARKER = "pilot-digests"
MAX_ATTEMPTS = 2
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")


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
    def idempotency_base(self) -> str:
        return f"jarvise-hermes-ingest-{self.label}"

    def idempotency_key(self, attempt: int) -> str:
        return f"{self.idempotency_base}-a{attempt}"

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


def assert_smoke_confined(slot: Slot, digest_dir: Path) -> None:
    """Refuse smoke unless the final directory name is the pilot tree (F8)."""
    if not slot.smoke:
        return
    resolved = digest_dir.resolve()
    if resolved.name != PILOT_DIGEST_MARKER:
        raise ValueError("smoke_not_pilot_dir")


def _reject_controls(value: str, field: str) -> None:
    if CONTROL_RE.search(value):
        raise ValueError(f"control_char:{field}")


def validate_items(
    payload: Any,
    *,
    resolve: Any = None,
    slot: Slot | None = None,
    policy_path: Path | None = None,
) -> tuple[list[DigestItem], bool]:
    """Parse gateway JSON into items. Returns (items, no_news). No DNS."""
    del resolve  # adapter path is hermetic; keep kw for call-site compatibility
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
        # Validate the exact strings that will be rendered (no strip of interior controls).
        title = title_raw.strip()
        day = day_raw.strip()
        url = url_raw.strip()
        comment = comment_raw.strip()
        if not title or not day or not url or not comment:
            raise ValueError("item_fields_missing")
        if len(title) > MAX_TITLE or len(comment) > MAX_COMMENT or len(url) > MAX_URL:
            raise ValueError("item_fields_too_long")
        for field, value in (("title", title), ("day", day), ("url", url), ("comment", comment)):
            _reject_controls(value, field)
        if "**" in title:
            raise ValueError("title_delimiter")
        lower_comment = comment.lower()
        if lower_comment.startswith("http://") or lower_comment.startswith("https://"):
            raise ValueError("comment_looks_like_url")
        if lower_comment.startswith("not a trade signal"):
            raise ValueError("comment_footer_forbidden")
        if not SOURCE_URL_RE.fullmatch(url):
            raise ValueError("url_not_printable")
        try:
            item_day = datetime.strptime(day, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError("bad_timestamp") from exc
        if item_day > ref_day:
            raise ValueError("day_in_future")
        if ref_day - item_day > ITEM_DAY_MAX_AGE:
            raise ValueError("day_too_old")
        ok, reason = allow_source_url(url, policy_path=policy_path)
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


def assert_round_trip(items: list[DigestItem], markdown: str) -> None:
    """Require the stdlib digest parser to recover the validated title/day/URL set."""
    from jarvise_intel.digest_parse import parse_digest_items

    parsed = parse_digest_items(markdown)
    if len(parsed) != len(items):
        raise ValueError("round_trip_count")
    for expected, got in zip(items, parsed, strict=True):
        if got.get("title") != expected.title:
            raise ValueError("round_trip_title")
        if got.get("url") != expected.url:
            raise ValueError("round_trip_url")
        day_got = str(got.get("day") or got.get("date") or "")
        if day_got != expected.day:
            raise ValueError("round_trip_day")


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


def _provenance_path(state_dir: Path, slot: Slot) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", slot.label)
    return state_dir / "provenance" / f"{safe}.json"


def _inflight_lock_path(state_dir: Path) -> Path:
    return state_dir / "locks" / "inflight.lock"


ACTIVE_ADMISSION_STATUSES = frozenset({"running", "unknown", "create_unconfirmed"})
# Blocks new paid work; only cleared after verified terminal or owner action.
BLOCKING_ADMISSION_STATUSES = ACTIVE_ADMISSION_STATUSES | frozenset({"owner_recovery_required"})
TERMINAL_SLOT_STATUSES = frozenset({"completed", "invalid_result", "failed_terminal"})
# Explicit supported set for durable slot/marker records (U2). Unknown ≠ terminal.
SUPPORTED_SLOT_STATUSES = BLOCKING_ADMISSION_STATUSES | TERMINAL_SLOT_STATUSES


class ActiveAdmissionError(Exception):
    """Raised when the active-admission index is present but unreadable/invalid (S5)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _active_admission_path(state_dir: Path) -> Path:
    return state_dir / "active_admission.json"


def validate_active_admission_record(data: dict[str, Any]) -> dict[str, Any]:
    """Require a minimal schema before trusting or clearing a marker (S5/U2)."""
    raw_status = data.get("status")
    if not isinstance(raw_status, str):
        raise ActiveAdmissionError("admission_bad_status_type")
    status = raw_status.strip()
    if not status:
        raise ActiveAdmissionError("admission_missing_status")
    if status not in SUPPORTED_SLOT_STATUSES:
        raise ActiveAdmissionError(f"admission_unknown_status:{status}")
    slot = str(data.get("slot") or "").strip()
    if not slot:
        raise ActiveAdmissionError("admission_missing_slot")
    try:
        attempt = int(data.get("attempt") or 0)
    except (TypeError, ValueError) as exc:
        raise ActiveAdmissionError("admission_bad_attempt") from exc
    if attempt < 1:
        raise ActiveAdmissionError("admission_bad_attempt")
    out = dict(data)
    out["status"] = status
    if status in ACTIVE_ADMISSION_STATUSES:
        started = str(data.get("started_at") or "").strip()
        if not started:
            raise ActiveAdmissionError("admission_missing_started_at")
        key = str(data.get("idempotency_key") or "").strip()
        if not key:
            raise ActiveAdmissionError("admission_missing_idempotency_key")
    return out


def load_active_admission(state_dir: Path) -> dict[str, Any] | None:
    """Return the marker dict, None if absent, or raise ActiveAdmissionError (S5)."""
    path = _active_admission_path(state_dir)
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ActiveAdmissionError("admission_unreadable") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ActiveAdmissionError("admission_malformed_json") from exc
    if not isinstance(data, dict):
        raise ActiveAdmissionError("admission_not_object")
    return validate_active_admission_record(data)


def _validate_slot_record_payload(data: dict[str, Any], *, path: Path) -> dict[str, Any]:
    """Validate a durable slot record; raise ActiveAdmissionError on ambiguity (T2/U2).

    Returns a copy with canonical stripped status. Unknown/noncanonical statuses
    are ambiguous evidence — never treated as terminal or as "no admission".
    """
    raw_status = data.get("status")
    if not isinstance(raw_status, str):
        raise ActiveAdmissionError(f"slot_bad_status_type:{path.name}")
    status = raw_status.strip()
    if not status:
        raise ActiveAdmissionError(f"slot_missing_status:{path.name}")
    if status not in SUPPORTED_SLOT_STATUSES:
        raise ActiveAdmissionError(f"slot_unknown_status:{status}:{path.name}")
    out = dict(data)
    out["status"] = status
    if status in BLOCKING_ADMISSION_STATUSES:
        slot = str(data.get("slot") or "").strip()
        if not slot:
            raise ActiveAdmissionError(f"slot_missing_identity:{path.name}")
        try:
            attempt = int(data.get("attempt") or 0)
        except (TypeError, ValueError) as exc:
            raise ActiveAdmissionError(f"slot_bad_attempt:{path.name}") from exc
        if attempt < 1:
            raise ActiveAdmissionError(f"slot_bad_attempt:{path.name}")
        key = str(data.get("idempotency_key") or "").strip()
        if not key:
            raise ActiveAdmissionError(f"slot_missing_idempotency_key:{path.name}")
        started = str(data.get("started_at") or "").strip()
        if status in ACTIVE_ADMISSION_STATUSES and not started:
            raise ActiveAdmissionError(f"slot_missing_started_at:{path.name}")
    return out


def list_unresolved_slot_records(state_dir: Path) -> list[dict[str, Any]]:
    """Scan durable slot files for unresolved paid admissions (S5/T2/U2 rebuild).

    Unreadable, malformed, unknown-status, or incomplete blocking rows raise —
    never silently skip into an empty "no admission" result.
    """
    slots_dir = state_dir / "slots"
    if not slots_dir.is_dir():
        return []
    found: list[dict[str, Any]] = []
    for path in sorted(slots_dir.glob("*.json")):
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ActiveAdmissionError(f"slot_unreadable:{path.name}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ActiveAdmissionError(f"slot_malformed_json:{path.name}") from exc
        if not isinstance(data, dict):
            raise ActiveAdmissionError(f"slot_not_object:{path.name}")
        validated = _validate_slot_record_payload(data, path=path)
        status = str(validated.get("status") or "")
        if status not in BLOCKING_ADMISSION_STATUSES:
            continue
        found.append(validated)
    return found


def write_active_admission(state_dir: Path, record: dict[str, Any]) -> None:
    path = _active_admission_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".active_admission.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def clear_active_admission(state_dir: Path, *, slot_label: str | None = None) -> None:
    """Clear the adapter-wide admission when it matches *slot_label* (or always if None).

    Corrupt markers are not cleared by slot match alone — raise so the caller
    fails closed (S5). Absent file is a no-op.
    """
    path = _active_admission_path(state_dir)
    if not path.is_file():
        return
    if slot_label is not None:
        current = load_active_admission(state_dir)
        if current is None:
            return
        if str(current.get("slot") or "") != slot_label:
            return
    try:
        path.unlink()
    except OSError:
        pass


def load_slot_record(state_dir: Path, slot: Slot) -> dict[str, Any] | None:
    """Return the slot record, None if absent, or raise ActiveAdmissionError (T2).

    Missing file is true absence. Unreadable/malformed/non-object/incomplete
    blocking rows fail closed — callers must not treat them as "no admission".
    """
    path = _record_path(state_dir, slot)
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ActiveAdmissionError(f"slot_unreadable:{path.name}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ActiveAdmissionError(f"slot_malformed_json:{path.name}") from exc
    if not isinstance(data, dict):
        raise ActiveAdmissionError(f"slot_not_object:{path.name}")
    return _validate_slot_record_payload(data, path=path)


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
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        _fsync_dir(path.parent)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def _write_json_atomic(path: Path, data: dict[str, Any], *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        try:
            os.chmod(path, mode)
        except OSError:
            pass
        _fsync_dir(path.parent)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def write_provenance(state_dir: Path, slot: Slot, *, sha256: str, byte_length: int) -> None:
    path = _provenance_path(state_dir, slot)
    _write_json_atomic(
        path,
        {
            "slot": slot.label,
            "filename": slot.filename,
            "sha256": sha256,
            "byte_length": byte_length,
        },
        mode=0o600,
    )


def load_provenance(state_dir: Path, slot: Slot) -> dict[str, Any] | None:
    path = _provenance_path(state_dir, slot)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


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
    """Create target from tmp without overwriting. Fail closed when hard links are unavailable."""
    try:
        os.link(tmp, target)
        return
    except FileExistsError:
        raise
    except OSError:
        if sys.platform != "win32":
            # Never expose a partial final *.md on Linux (O_EXCL-then-write is unsafe).
            raise
    # Windows-only fallback: write to a unique sibling then os.replace is still replace-capable,
    # so use O_EXCL on a staging name under the digest dir, then link/rename only if target absent.
    staging = target.with_name(f".{target.name}.{uuid.uuid4().hex}.stg")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    fd = os.open(str(staging), flags, 0o644)
    try:
        with open(tmp, "rb") as src, os.fdopen(fd, "wb") as dst:
            dst.write(src.read())
            dst.flush()
            os.fsync(dst.fileno())
        if target.exists():
            raise FileExistsError("filename_conflict")
        os.replace(staging, target)
    except Exception:
        try:
            if staging.exists():
                staging.unlink()
        except OSError:
            pass
        raise


def adopt_existing_digest(
    *,
    digest_dir: Path,
    state_dir: Path,
    slot: Slot,
) -> dict[str, Any] | None:
    """If a complete trusted file exists without a completed record, adopt it (no Hermes call)."""
    target = digest_dir / slot.filename
    if not target.is_file():
        return None
    data = target.read_bytes()
    if not data:
        raise ValueError("recovery_empty_file")
    prior_hash = hashlib.sha256(data).hexdigest()
    prov = load_provenance(state_dir, slot)
    if prov is None:
        raise ValueError("recovery_missing_provenance")
    if str(prov.get("slot") or "") != slot.label:
        raise ValueError("recovery_slot_mismatch")
    if str(prov.get("filename") or "") != slot.filename:
        raise ValueError("recovery_filename_mismatch")
    if str(prov.get("sha256") or "") != prior_hash:
        raise ValueError("recovery_hash_mismatch")
    raw_len = prov.get("byte_length")
    if raw_len is None:
        raise ValueError("recovery_length_mismatch")
    try:
        byte_length = int(raw_len)
    except (TypeError, ValueError) as exc:
        raise ValueError("recovery_length_mismatch") from exc
    if byte_length != len(data):
        raise ValueError("recovery_length_mismatch")
    text = data.decode("utf-8")
    if FOOTER not in text:
        raise ValueError("recovery_footer_missing")
    from jarvise_intel.digest_parse import parse_digest_items

    if "No confirmed watchlist coverage" not in text:
        parsed = parse_digest_items(text)
        if not parsed:
            raise ValueError("recovery_unparseable")
    record = {
        "status": "completed",
        "slot": slot.label,
        "filename": slot.filename,
        "output_hash": prior_hash,
        "published_at": datetime.fromtimestamp(target.stat().st_mtime, tz=UTC).isoformat(),
        "replay": True,
        "recovered": True,
    }
    write_slot_record(state_dir, slot, record)
    return record


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

        write_provenance(state_dir, slot, sha256=new_hash, byte_length=len(data))

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
                os.chmod(tmp, 0o644)
            except OSError:
                pass
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


_INFLIGHT_HANDLE: Any = None
_INFLIGHT_THREAD = threading.Lock()
_INFLIGHT_MODE: str | None = None


def try_acquire_inflight(state_dir: Path | None = None) -> bool:
    """Cross-process single-in-flight lock when state_dir is set; else process-local."""
    global _INFLIGHT_HANDLE, _INFLIGHT_MODE
    if state_dir is None:
        if _INFLIGHT_THREAD.acquire(blocking=False):
            _INFLIGHT_MODE = "thread"
            return True
        return False
    lock_path = _inflight_lock_path(state_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            handle.write(b"0")
            handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _INFLIGHT_HANDLE = handle
        _INFLIGHT_MODE = "file"
        return True
    except OSError:
        handle.close()
        return False


def release_inflight() -> None:
    global _INFLIGHT_HANDLE, _INFLIGHT_MODE
    mode = _INFLIGHT_MODE
    _INFLIGHT_MODE = None
    if mode == "thread":
        try:
            _INFLIGHT_THREAD.release()
        except RuntimeError:
            pass
        return
    handle = _INFLIGHT_HANDLE
    _INFLIGHT_HANDLE = None
    if handle is None:
        return
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
    try:
        handle.close()
    except OSError:
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
