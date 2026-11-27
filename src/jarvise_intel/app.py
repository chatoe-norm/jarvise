"""Authenticated hermes-ingest HTTP adapter."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from jarvise_intel.digest import (
    BLOCKING_ADMISSION_STATUSES,
    MAX_ATTEMPTS,
    TERMINAL_SLOT_STATUSES,
    ActiveAdmissionError,
    adopt_existing_digest,
    assert_current_slot,
    assert_round_trip,
    assert_smoke_confined,
    clear_active_admission,
    extract_json_object,
    list_unresolved_slot_records,
    load_active_admission,
    load_slot_record,
    parse_slot,
    publish_digest,
    release_inflight,
    render_markdown,
    try_acquire_inflight,
    validate_items,
    write_active_admission,
    write_slot_record,
)
from jarvise_intel.gateway import (
    TERMINAL_FAIL,
    TERMINAL_OK,
    GatewayConfig,
    GatewayError,
    RunResult,
    create_run,
    finalize_completed,
    get_run,
    run_research,
    stop_run,
)

logger = logging.getLogger("jarvise_intel")

MAX_BODY = 4096
SECRET_RE = re.compile(r"(?i)(authorization|api[_-]?key|token|bearer|password)\s*[:=]\s*\S+")
# Pinned Hermes RunIdempotencyStore retains terminal keys 24h; stay strictly inside (T1).
IDEMPOTENCY_SAFE_HORIZON_S = 23 * 3600


def redact(text: str) -> str:
    return SECRET_RE.sub(r"\1=[REDACTED]", text)


class AdapterConfig:
    def __init__(self) -> None:
        self.ingest_token = (os.environ.get("JARVISE_HERMES_INGEST_TOKEN") or "").strip()
        self.api_key = (os.environ.get("JARVISE_HERMES_API_KEY") or "").strip()
        self.provider = (os.environ.get("HERMES_PROVIDER") or "").strip()
        self.model = (os.environ.get("HERMES_MODEL") or "").strip()
        self.gateway_url = (os.environ.get("JARVISE_HERMES_GATEWAY_URL") or "http://hermes:8642").strip()
        self.digest_dir = Path(os.environ.get("JARVISE_HERMES_DIGEST_DIR") or "/data/hermes/pilot-digests")
        self.state_dir = Path(os.environ.get("JARVISE_HERMES_STATE_DIR") or "/data/hermes/adapter")
        self.prompt_path = Path(
            os.environ.get("JARVISE_HERMES_PROMPT_PATH") or "/config/hermes/ingest-prompt.md"
        )
        self.source_policy_path = Path(
            os.environ.get("JARVISE_HERMES_SOURCE_POLICY") or "/config/hermes/source-policy.json"
        )
        self.smoke = (os.environ.get("JARVISE_HERMES_SMOKE") or "").strip() == "1"
        self.host = os.environ.get("HERMES_INGEST_HOST", "0.0.0.0")
        self.port = int(os.environ.get("HERMES_INGEST_PORT", "8092"))
        self.deadline_s = float(os.environ.get("JARVISE_HERMES_DEADLINE_S", "600"))
        # Injected clock for tests; production uses wall clock.
        self.now: datetime | None = None

    def provider_ready(self) -> bool:
        return bool(self.api_key and self.provider and self.model and self.ingest_token)


def _load_prompt(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _check_bearer(header: str | None, expected: str) -> bool:
    if not expected or not header:
        return False
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    return hmac.compare_digest(parts[1].encode("utf-8"), expected.encode("utf-8"))


def _gateway(cfg: AdapterConfig) -> GatewayConfig:
    return GatewayConfig(
        base_url=cfg.gateway_url,
        api_key=cfg.api_key,
        provider=cfg.provider,
        model=cfg.model,
        deadline_s=cfg.deadline_s,
    )


def _actual_meta(result: RunResult, *, attempt: int, idem: str | None = None) -> dict[str, Any]:
    """Store actual runtime identity only — never fill from configured env (F1)."""
    meta: dict[str, Any] = {
        "provider": str(result.runtime.get("provider") or ""),
        "model": str(result.runtime.get("model") or ""),
        "runtime": result.runtime,
        "usage": result.usage,
        "run_id": result.run_id,
        "attempt": attempt,
    }
    if idem is not None:
        meta["idempotency_key"] = idem
    return meta


def _next_attempt(existing: dict[str, Any] | None) -> tuple[int, str | None]:
    """Return (attempt_number, block_reason). attempt is 1..MAX_ATTEMPTS."""
    if existing is None:
        return 1, None
    status = str(existing.get("status") or "")
    if status == "completed":
        return 0, "completed"
    if status == "unknown":
        return 0, "unknown_run"
    if status == "running":
        return 0, "running"
    if status == "invalid_result":
        return 0, "invalid_result"
    if status == "owner_recovery_required":
        return 0, "owner_recovery_required"
    if status == "create_unconfirmed":
        # Same attempt + same Idempotency-Key; do not mint n+1 (F2).
        attempt = int(existing.get("attempt") or 1)
        if attempt < 1:
            attempt = 1
        if attempt > MAX_ATTEMPTS:
            return 0, "attempt_budget"
        return attempt, None
    attempt = int(existing.get("attempt") or 0)
    if status in TERMINAL_FAIL or status == "incomplete_run" or status == "failed_terminal":
        if attempt >= MAX_ATTEMPTS:
            return 0, "attempt_budget"
        return attempt + 1, None
    if attempt <= 0:
        return 1, None
    if attempt >= MAX_ATTEMPTS and status not in TERMINAL_OK:
        return 0, "attempt_budget"
    return attempt, None


def _publish_from_result(
    cfg: AdapterConfig,
    *,
    slot: Any,
    result: RunResult,
    attempt: int,
    idem: str | None = None,
) -> dict[str, Any]:
    payload = extract_json_object(result.output)
    items, no_news = validate_items(payload, slot=slot, policy_path=cfg.source_policy_path)
    markdown = render_markdown(items, no_news=no_news)
    if items:
        assert_round_trip(items, markdown)
    meta = _actual_meta(result, attempt=attempt, idem=idem)
    meta["source_urls"] = [item.url for item in items]
    meta["no_news"] = no_news
    meta["item_count"] = len(items)
    return publish_digest(
        digest_dir=cfg.digest_dir,
        state_dir=cfg.state_dir,
        slot=slot,
        markdown=markdown,
        meta=meta,
    )


def _wall_now(cfg: AdapterConfig) -> datetime:
    """Prefer the injected test clock; production uses UTC wall time."""
    if cfg.now is not None:
        moment = cfg.now
        if moment.tzinfo is None:
            return moment.replace(tzinfo=UTC)
        return moment.astimezone(UTC)
    return datetime.now(UTC)


def _parse_started_at(existing: dict[str, Any]) -> datetime | None:
    started = existing.get("started_at")
    if not isinstance(started, str) or not started.strip():
        return None
    try:
        started_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
    except ValueError:
        return None
    if started_dt.tzinfo is None:
        started_dt = started_dt.replace(tzinfo=UTC)
    return started_dt


def _remaining_deadline_s(existing: dict[str, Any], cfg: AdapterConfig) -> float:
    """Absolute remaining budget from original started_at (S4). Never resets to full."""
    started_dt = _parse_started_at(existing)
    if started_dt is None:
        return 0.0
    elapsed = (_wall_now(cfg) - started_dt).total_seconds()
    return max(0.0, float(cfg.deadline_s) - elapsed)


def _resume_past_deadline(existing: dict[str, Any], cfg: AdapterConfig) -> bool:
    return _remaining_deadline_s(existing, cfg) <= 0.0


def _admission_age_s(existing: dict[str, Any], cfg: AdapterConfig) -> float | None:
    started_dt = _parse_started_at(existing)
    if started_dt is None:
        return None
    return max(0.0, (_wall_now(cfg) - started_dt).total_seconds())


def request_sha256(
    *,
    instructions: str,
    input_text: str,
    provider: str,
    model: str,
) -> str:
    """Canonical frozen request identity for safe same-key rediscovery (T1)."""
    payload = json.dumps(
        {
            "instructions": instructions,
            "input": input_text,
            "provider": provider,
            "model": model,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def scope_sha256(*, gateway_url: str, api_key: str) -> str:
    """Non-secret fingerprint of the upstream idempotency namespace (U1).

    Pinned Hermes scopes reservations by expected API key (+ profile). We store
    only sha256(api_key) and the gateway base URL — never the raw secret.
    """
    payload = json.dumps(
        {
            "api_key_sha256": hashlib.sha256(api_key.encode("utf-8")).hexdigest(),
            "gateway_url": gateway_url.rstrip("/"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _current_scope_hash(cfg: AdapterConfig) -> str:
    return scope_sha256(gateway_url=cfg.gateway_url, api_key=cfg.api_key)


def _mark_active(
    cfg: AdapterConfig,
    *,
    slot: Any,
    status: str,
    attempt: int,
    idem: str | None,
    started_at: str | None,
    run_id: str = "",
    error: str | None = None,
    request_hash: str | None = None,
    scope_hash: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> None:
    record: dict[str, Any] = {
        "status": status,
        "slot": slot.label,
        "attempt": attempt,
        "idempotency_key": idem,
        "started_at": started_at,
    }
    if run_id:
        record["run_id"] = run_id
    if error:
        record["error"] = error
    if request_hash:
        record["request_sha256"] = request_hash
    if scope_hash:
        record["scope_sha256"] = scope_hash
    if provider:
        record["provider"] = provider
    if model:
        record["model"] = model
    write_slot_record(cfg.state_dir, slot, record)
    if status in BLOCKING_ADMISSION_STATUSES:
        write_active_admission(cfg.state_dir, record)
    else:
        clear_active_admission(cfg.state_dir, slot_label=slot.label)


def _clear_matching_terminal_admission(cfg: AdapterConfig, *, slot: Any) -> None:
    """S2: clear the global marker only when it names this verified terminal slot."""
    try:
        active = load_active_admission(cfg.state_dir)
    except ActiveAdmissionError:
        return
    if active is None:
        return
    if str(active.get("slot") or "") != slot.label:
        return
    clear_active_admission(cfg.state_dir, slot_label=slot.label)


def _slot_input_text(slot: Any) -> str:
    return (
        f"Produce the news digest JSON for slot {slot.label} "
        f"(Bangkok {slot.bangkok.isoformat()}, UTC file {slot.filename})."
    )


def _handle_resume(
    cfg: AdapterConfig,
    *,
    slot: Any,
    existing: dict[str, Any],
) -> tuple[int, dict[str, Any]] | None:
    """Resume running/unknown. Returns response or None to fall through after terminal fail."""
    run_id = str(existing.get("run_id") or "").strip()
    if not run_id:
        # create_unconfirmed / unknown without id: keep blocking; do not invent a prior id.
        return 409, {"ok": False, "error": "unknown_run"}
    gateway = _gateway(cfg)
    attempt = int(existing.get("attempt") or 1)
    idem = existing.get("idempotency_key")
    started_at = existing.get("started_at")
    remaining = _remaining_deadline_s(existing, cfg)

    if remaining <= 0:
        # Absolute deadline elapsed: stop-and-verify immediately (S4).
        stop_run(gateway, run_id)
        try:
            current = get_run(gateway, run_id)
        except GatewayError as exc:
            _mark_active(
                cfg,
                slot=slot,
                status="unknown",
                attempt=attempt,
                idem=str(idem) if idem else None,
                started_at=str(started_at) if started_at else None,
                run_id=run_id,
                error=exc.category,
            )
            return 409, {"ok": False, "error": "unknown_run"}
    else:
        try:
            current = get_run(gateway, run_id)
        except GatewayError as exc:
            _mark_active(
                cfg,
                slot=slot,
                status="unknown",
                attempt=attempt,
                idem=str(idem) if idem else None,
                started_at=str(started_at) if started_at else None,
                run_id=run_id,
                error=exc.category,
            )
            return 409, {"ok": False, "error": "unknown_run"}

    if current.status in TERMINAL_OK:
        try:
            finalized = finalize_completed(gateway, current)
        except GatewayError as exc:
            _mark_active(
                cfg,
                slot=slot,
                status="invalid_result",
                attempt=attempt,
                idem=str(idem) if idem else None,
                started_at=str(started_at) if started_at else None,
                run_id=run_id,
                error=exc.category,
            )
            return 502, {"ok": False, "error": exc.category}
        try:
            record = _publish_from_result(
                cfg,
                slot=slot,
                result=finalized,
                attempt=attempt,
                idem=str(idem) if idem else None,
            )
            clear_active_admission(cfg.state_dir, slot_label=slot.label)
            return 200, {"ok": True, "replay": bool(record.get("replay")), "result": record}
        except (ValueError, FileExistsError) as exc:
            _mark_active(
                cfg,
                slot=slot,
                status="invalid_result",
                attempt=attempt,
                idem=str(idem) if idem else None,
                started_at=str(started_at) if started_at else None,
                run_id=run_id,
                error=str(exc),
            )
            return 502, {"ok": False, "error": str(exc)}

    if current.status in TERMINAL_FAIL:
        _mark_active(
            cfg,
            slot=slot,
            status="failed_terminal",
            attempt=attempt,
            idem=str(idem) if idem else None,
            started_at=str(started_at) if started_at else None,
            run_id=run_id,
        )
        return None  # fall through to _next_attempt

    _mark_active(
        cfg,
        slot=slot,
        status="unknown",
        attempt=attempt,
        idem=str(idem) if idem else None,
        started_at=str(started_at) if started_at else None,
        run_id=run_id,
    )
    return 409, {"ok": False, "error": "unknown_run"}


def _recover_create_unconfirmed(
    cfg: AdapterConfig,
    *,
    slot: Any,
    existing: dict[str, Any],
) -> tuple[int, dict[str, Any]] | None:
    """Rediscover a lost create via the same frozen idempotency key only (S3/T1).

    Never POSTs outside the verified safe retention horizon. A known run_id
    stop-and-verifies without creating. Missing/mismatched request identity
    fails closed to owner_recovery_required.
    """
    attempt = int(existing.get("attempt") or 1)
    idem = str(existing.get("idempotency_key") or "").strip()
    started_at = str(existing.get("started_at") or "").strip()
    stored_hash = str(existing.get("request_sha256") or "").strip()
    stored_scope = str(existing.get("scope_sha256") or "").strip()
    if not idem or not started_at:
        _mark_active(
            cfg,
            slot=slot,
            status="owner_recovery_required",
            attempt=attempt,
            idem=idem or None,
            started_at=started_at or None,
            error="missing_admission_fields",
            request_hash=stored_hash or None,
            scope_hash=stored_scope or None,
        )
        return 409, {"ok": False, "error": "owner_recovery_required", "slot": slot.label}

    run_id = str(existing.get("run_id") or "").strip()
    age = _admission_age_s(existing, cfg)
    remaining = _remaining_deadline_s(existing, cfg)
    gateway = _gateway(cfg)
    current_scope = _current_scope_hash(cfg)

    if not run_id:
        # T1/U1: decide BEFORE POST — expired/no-hash/scope mismatch never create.
        if (
            age is None
            or age >= IDEMPOTENCY_SAFE_HORIZON_S
            or not stored_hash
            or not stored_scope
            or stored_scope != current_scope
        ):
            if age is not None and age >= IDEMPOTENCY_SAFE_HORIZON_S:
                err = "idempotency_retention_expired"
            elif not stored_hash:
                err = "missing_request_identity"
            elif not stored_scope or stored_scope != current_scope:
                err = "scope_identity_mismatch"
            else:
                err = "missing_request_identity"
            _mark_active(
                cfg,
                slot=slot,
                status="owner_recovery_required",
                attempt=attempt,
                idem=idem,
                started_at=started_at,
                error=err,
                request_hash=stored_hash or None,
                scope_hash=stored_scope or None,
            )
            return 409, {"ok": False, "error": "owner_recovery_required", "slot": slot.label}
        try:
            instructions = _load_prompt(cfg.prompt_path)
        except OSError:
            return 500, {"ok": False, "error": "prompt_unreadable"}
        input_text = _slot_input_text(slot)
        current_hash = request_sha256(
            instructions=instructions,
            input_text=input_text,
            provider=cfg.provider,
            model=cfg.model,
        )
        if current_hash != stored_hash:
            _mark_active(
                cfg,
                slot=slot,
                status="owner_recovery_required",
                attempt=attempt,
                idem=idem,
                started_at=started_at,
                error="request_identity_mismatch",
                request_hash=stored_hash,
                scope_hash=stored_scope,
            )
            return 409, {"ok": False, "error": "owner_recovery_required", "slot": slot.label}
        try:
            run_id = create_run(
                gateway,
                instructions=instructions,
                input_text=input_text,
                idempotency_key=idem,
            )
        except GatewayError as exc:
            # Transport/provider ambiguity: never invent terminal failure.
            if remaining <= 0:
                _mark_active(
                    cfg,
                    slot=slot,
                    status="owner_recovery_required",
                    attempt=attempt,
                    idem=idem,
                    started_at=started_at,
                    error=exc.category,
                    request_hash=stored_hash,
                    scope_hash=stored_scope,
                )
                return 409, {"ok": False, "error": "owner_recovery_required", "slot": slot.label}
            _mark_active(
                cfg,
                slot=slot,
                status="create_unconfirmed",
                attempt=attempt,
                idem=idem,
                started_at=started_at,
                error=exc.category,
                request_hash=stored_hash,
                scope_hash=stored_scope,
                provider=cfg.provider,
                model=cfg.model,
            )
            return 409, {"ok": False, "error": "admission_busy", "slot": slot.label}
        _mark_active(
            cfg,
            slot=slot,
            status="running",
            attempt=attempt,
            idem=idem,
            started_at=started_at,
            run_id=run_id,
            request_hash=stored_hash,
            scope_hash=stored_scope,
            provider=cfg.provider,
            model=cfg.model,
        )

    # Enforce original absolute deadline on the recovered run (no new create).
    resumed = _handle_resume(
        cfg,
        slot=slot,
        existing={
            **existing,
            "status": "running",
            "run_id": run_id,
            "attempt": attempt,
            "idempotency_key": idem,
            "started_at": started_at,
        },
    )
    return resumed


def _resolve_active_index(cfg: AdapterConfig) -> dict[str, Any] | None:
    """Load active marker or rebuild from durable unresolved slots (S5)."""
    try:
        active = load_active_admission(cfg.state_dir)
    except ActiveAdmissionError:
        raise
    if active is not None:
        return active
    unresolved = list_unresolved_slot_records(cfg.state_dir)
    if not unresolved:
        return None

    # Prefer the oldest started_at so a stale unpaid run keeps blocking.
    def _sort_key(row: dict[str, Any]) -> str:
        return str(row.get("started_at") or "")

    chosen = sorted(unresolved, key=_sort_key)[0]
    write_active_admission(cfg.state_dir, chosen)
    return chosen


# Same verified admission may advance create_unconfirmed → running → unknown (V2).
_ADMISSION_FORWARD_RANK: dict[str, int] = {
    "create_unconfirmed": 0,
    "running": 1,
    "unknown": 2,
}


def _try_forward_admission_repair(
    cfg: AdapterConfig,
    *,
    slot: Any,
    active: dict[str, Any],
    slot_rec: dict[str, Any],
) -> bool:
    """Align marker and slot on a forward crash boundary. True when repaired (V2/U3)."""
    same_attempt = str(slot_rec.get("attempt") or "") == str(active.get("attempt") or "")
    same_key = str(slot_rec.get("idempotency_key") or "") == str(active.get("idempotency_key") or "")
    if not (same_attempt and same_key):
        return False
    marker_status = str(active.get("status") or "")
    slot_status = str(slot_rec.get("status") or "")
    marker_rank = _ADMISSION_FORWARD_RANK.get(marker_status)
    slot_rank = _ADMISSION_FORWARD_RANK.get(slot_status)
    if marker_rank is None or slot_rank is None or marker_rank == slot_rank:
        return False
    active_run = str(active.get("run_id") or "").strip()
    slot_run = str(slot_rec.get("run_id") or "").strip()
    if active_run and slot_run and active_run != slot_run:
        return False
    # Further side must carry the run id once past create_unconfirmed.
    further = slot_rec if slot_rank > marker_rank else active
    further_status = str(further.get("status") or "")
    further_run = str(further.get("run_id") or "").strip()
    if further_status in {"running", "unknown"} and not further_run:
        return False
    if slot_rank > marker_rank:
        write_active_admission(cfg.state_dir, slot_rec)
    else:
        write_slot_record(cfg.state_dir, slot, active)
        write_active_admission(cfg.state_dir, active)
    return True


def _other_unresolved_admissions(
    cfg: AdapterConfig,
    *,
    slot_label: str,
) -> list[dict[str, Any]]:
    """Durable blocking admissions for slots other than the request (V1)."""
    return [
        row for row in list_unresolved_slot_records(cfg.state_dir) if str(row.get("slot") or "") != slot_label
    ]


def _block_until_no_other_unresolved(
    cfg: AdapterConfig,
    *,
    slot: Any,
) -> tuple[int, dict[str, Any]] | None:
    """Reconcile every other unresolved admission before allowing create (V1).

    Clearing one marker is not system-wide clearance. Re-index the oldest
    remaining other row until none remain, or fail closed if the set stalls.
    """
    prev_count: int | None = None
    while True:
        try:
            blocked = _block_other_active_admission(cfg, slot=slot)
        except ActiveAdmissionError as exc:
            return 409, {"ok": False, "error": "admission_state_invalid", "detail": exc.reason}
        if blocked is not None:
            return blocked
        others = _other_unresolved_admissions(cfg, slot_label=slot.label)
        if not others:
            return None
        count = len(others)
        if prev_count is not None and count >= prev_count:
            return 409, {
                "ok": False,
                "error": "admission_state_invalid",
                "detail": "unresolved_not_shrinking",
            }
        prev_count = count
        oldest = sorted(others, key=lambda row: str(row.get("started_at") or ""))[0]
        write_active_admission(cfg.state_dir, oldest)


def _block_other_active_admission(
    cfg: AdapterConfig,
    *,
    slot: Any,
) -> tuple[int, dict[str, Any]] | None:
    """R5/S2/S3/S5: another slot's unpaid/unknown admission blocks every other slot."""
    try:
        active = _resolve_active_index(cfg)
    except ActiveAdmissionError as exc:
        return 409, {"ok": False, "error": "admission_state_invalid", "detail": exc.reason}
    if active is None:
        return None
    status = str(active.get("status") or "")
    if status in TERMINAL_SLOT_STATUSES:
        # Stale terminal marker: clear only when slot file agrees.
        other_label = str(active.get("slot") or "")
        try:
            other_slot = parse_slot(other_label, smoke_allowed=True)
        except ValueError:
            return 409, {"ok": False, "error": "admission_state_invalid"}
        other_record = load_slot_record(cfg.state_dir, other_slot)
        if other_record and str(other_record.get("status") or "") in TERMINAL_SLOT_STATUSES:
            clear_active_admission(cfg.state_dir, slot_label=other_label)
            return None
        return 409, {"ok": False, "error": "admission_state_invalid"}
    if status not in BLOCKING_ADMISSION_STATUSES:
        # Do not clear unrecognized statuses without evidence (S5).
        return 409, {"ok": False, "error": "admission_state_invalid"}
    other = str(active.get("slot") or "")
    if other == slot.label:
        # T2: marker for THIS slot — reconcile against durable slot evidence.
        # Never fall through to mint attempt 1 over a valid unknown attempt 2.
        try:
            slot_rec = load_slot_record(cfg.state_dir, slot)
        except ActiveAdmissionError as exc:
            return 409, {"ok": False, "error": "admission_state_invalid", "detail": exc.reason}
        if slot_rec is None:
            # Marker exists but slot file absent: keep marker, block new work.
            return 409, {"ok": False, "error": "admission_state_invalid", "detail": "slot_missing_for_active"}
        if str(slot_rec.get("status") or "") in TERMINAL_SLOT_STATUSES:
            clear_active_admission(cfg.state_dir, slot_label=slot.label)
            return None
        slot_run = str(slot_rec.get("run_id") or "").strip()
        # V2/U3: same admission may be one step ahead after a crash between writes.
        if _try_forward_admission_repair(cfg, slot=slot, active=active, slot_rec=slot_rec):
            return None
        # Identity must agree before any replacement/resume path.
        for field in ("attempt", "idempotency_key", "status"):
            if str(slot_rec.get(field) or "") != str(active.get(field) or ""):
                return 409, {
                    "ok": False,
                    "error": "admission_state_invalid",
                    "detail": f"marker_slot_mismatch:{field}",
                }
        active_run = str(active.get("run_id") or "").strip()
        if active_run and slot_run and active_run != slot_run:
            return 409, {
                "ok": False,
                "error": "admission_state_invalid",
                "detail": "marker_slot_mismatch:run_id",
            }
        return None
    try:
        other_slot = parse_slot(other, smoke_allowed=True)
    except ValueError:
        return 409, {"ok": False, "error": "admission_busy", "slot": other}
    try:
        other_record = load_slot_record(cfg.state_dir, other_slot) or active
    except ActiveAdmissionError as exc:
        return 409, {"ok": False, "error": "admission_state_invalid", "detail": exc.reason}
    other_status = str(other_record.get("status") or "")

    # S2: durable terminal slot with stale marker — clear and continue.
    if other_status in TERMINAL_SLOT_STATUSES:
        clear_active_admission(cfg.state_dir, slot_label=other)
        return None

    if other_status == "owner_recovery_required":
        return 409, {"ok": False, "error": "owner_recovery_required", "slot": other}

    if other_status == "create_unconfirmed":
        recovered = _recover_create_unconfirmed(cfg, slot=other_slot, existing=other_record)
        if recovered is None:
            # Terminal fail on other → allow this slot if marker cleared.
            still = _peek_blocking(cfg)
            if still:
                return 409, {"ok": False, "error": "admission_busy", "slot": other}
            return None
        code, body = recovered
        if code == 200:
            # Other completed via reconcile; only proceed if marker gone.
            still = _peek_blocking(cfg)
            if still:
                return 409, {"ok": False, "error": "admission_busy", "slot": other}
            return None
        # Still unresolved / owner recovery / busy.
        return code, body

    if other_status in {"running", "unknown"} and str(other_record.get("run_id") or ""):
        resumed = _handle_resume(cfg, slot=other_slot, existing=other_record)
        if resumed is not None:
            still = _peek_blocking(cfg)
            if still:
                err = "admission_busy"
                if str(still.get("status") or "") == "owner_recovery_required":
                    err = "owner_recovery_required"
                return 409, {"ok": False, "error": err, "slot": other}
            if resumed[0] == 200:
                return None
            return 409, {"ok": False, "error": "admission_busy", "slot": other}
        still = _peek_blocking(cfg)
        if still:
            return 409, {"ok": False, "error": "admission_busy", "slot": other}
        return None
    return 409, {"ok": False, "error": "admission_busy", "slot": other}


def _peek_blocking(cfg: AdapterConfig) -> dict[str, Any] | None:
    try:
        still = load_active_admission(cfg.state_dir)
    except ActiveAdmissionError:
        return {"status": "admission_state_invalid"}
    if still and str(still.get("status") or "") in BLOCKING_ADMISSION_STATUSES:
        return still
    return None


def handle_ingest(cfg: AdapterConfig, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    if not cfg.provider_ready():
        return 503, {"ok": False, "error": "provider_unconfigured"}

    slot_raw = body.get("slot")
    if not isinstance(slot_raw, str) or not slot_raw.strip():
        return 400, {"ok": False, "error": "invalid_slot"}
    if len(body) != 1:
        return 400, {"ok": False, "error": "unexpected_fields"}

    try:
        slot = parse_slot(slot_raw, smoke_allowed=cfg.smoke)
        assert_current_slot(slot, now=cfg.now, smoke_allowed=cfg.smoke)
        assert_smoke_confined(slot, cfg.digest_dir)
    except ValueError as exc:
        return 400, {"ok": False, "error": str(exc)}

    if not try_acquire_inflight(cfg.state_dir):
        return 429, {"ok": False, "error": "inflight_busy"}

    try:
        try:
            existing = load_slot_record(cfg.state_dir, slot)
        except ActiveAdmissionError as exc:
            return 409, {"ok": False, "error": "admission_state_invalid", "detail": exc.reason}
        if existing and existing.get("status") == "completed":
            # S2: completed replay must clear a matching stale active marker under the lock.
            _clear_matching_terminal_admission(cfg, slot=slot)
            return 200, {"ok": True, "replay": True, "result": existing}
        if existing and existing.get("status") == "invalid_result":
            _clear_matching_terminal_admission(cfg, slot=slot)
            return 409, {"ok": False, "error": "invalid_result"}
        if existing and existing.get("status") == "owner_recovery_required":
            return 409, {"ok": False, "error": "owner_recovery_required"}

        # P2-5: adopt a complete file published before the slot record was written.
        try:
            adopted = adopt_existing_digest(digest_dir=cfg.digest_dir, state_dir=cfg.state_dir, slot=slot)
        except (ValueError, TypeError) as exc:
            return 409, {"ok": False, "error": str(exc)}
        if adopted is not None:
            _clear_matching_terminal_admission(cfg, slot=slot)
            return 200, {"ok": True, "replay": True, "result": adopted}

        # R5/S2/S3/S5/T2/V1: every other unresolved durable admission must clear first.
        try:
            blocked = _block_until_no_other_unresolved(cfg, slot=slot)
        except ActiveAdmissionError as exc:
            return 409, {"ok": False, "error": "admission_state_invalid", "detail": exc.reason}
        if blocked is not None:
            return blocked

        # Resume an in-flight run after restart (no second create while unknown/running).
        if existing and existing.get("status") in {"running", "unknown"}:
            resumed = _handle_resume(cfg, slot=slot, existing=existing)
            if resumed is not None:
                return resumed
            try:
                existing = load_slot_record(cfg.state_dir, slot)
            except ActiveAdmissionError as exc:
                return 409, {"ok": False, "error": "admission_state_invalid", "detail": exc.reason}

        # create_unconfirmed for this slot: reuse same key/started_at via fall-through (R4/S4).
        if existing and existing.get("status") == "create_unconfirmed":
            remaining_self = _remaining_deadline_s(existing, cfg)
            age_self = _admission_age_s(existing, cfg)
            # Past research deadline OR outside idempotency horizon: recover without minting.
            if remaining_self <= 0 or (age_self is not None and age_self >= IDEMPOTENCY_SAFE_HORIZON_S):
                recovered = _recover_create_unconfirmed(cfg, slot=slot, existing=existing)
                if recovered is not None:
                    return recovered
                try:
                    existing = load_slot_record(cfg.state_dir, slot)
                except ActiveAdmissionError as exc:
                    return 409, {"ok": False, "error": "admission_state_invalid", "detail": exc.reason}

        attempt, block = _next_attempt(existing)
        if block == "completed":
            _clear_matching_terminal_admission(cfg, slot=slot)
            return 200, {"ok": True, "replay": True, "result": existing}
        if block:
            return 409, {"ok": False, "error": block}

        try:
            instructions = _load_prompt(cfg.prompt_path)
        except OSError:
            return 500, {"ok": False, "error": "prompt_unreadable"}

        gateway = _gateway(cfg)
        # create_unconfirmed reuses the prior key and started_at (R4); otherwise mint {slot}-a{n}.
        prior_key = ""
        prior_started = ""
        prior_hash = ""
        prior_scope = ""
        current_scope = _current_scope_hash(cfg)
        is_replay_unconfirmed = bool(
            existing is not None and str(existing.get("status") or "") == "create_unconfirmed"
        )
        if is_replay_unconfirmed:
            assert existing is not None
            prior_key = str(existing.get("idempotency_key") or "").strip()
            prior_started = str(existing.get("started_at") or "").strip()
            prior_hash = str(existing.get("request_sha256") or "").strip()
            prior_scope = str(existing.get("scope_sha256") or "").strip()
            if not prior_hash or not prior_scope:
                _mark_active(
                    cfg,
                    slot=slot,
                    status="owner_recovery_required",
                    attempt=int(existing.get("attempt") or attempt),
                    idem=prior_key or None,
                    started_at=prior_started or None,
                    error="missing_request_identity" if not prior_hash else "scope_identity_mismatch",
                    request_hash=prior_hash or None,
                    scope_hash=prior_scope or None,
                )
                return 409, {"ok": False, "error": "owner_recovery_required"}
            if prior_scope != current_scope:
                # U1: gateway/auth namespace rotated — never create under new credentials.
                _mark_active(
                    cfg,
                    slot=slot,
                    status="owner_recovery_required",
                    attempt=int(existing.get("attempt") or attempt),
                    idem=prior_key or None,
                    started_at=prior_started or None,
                    error="scope_identity_mismatch",
                    request_hash=prior_hash,
                    scope_hash=prior_scope,
                )
                return 409, {"ok": False, "error": "owner_recovery_required"}
        idem = prior_key or slot.idempotency_key(attempt)
        input_text = _slot_input_text(slot)
        started_at = prior_started or _wall_now(cfg).isoformat()
        computed_hash = request_sha256(
            instructions=instructions,
            input_text=input_text,
            provider=cfg.provider,
            model=cfg.model,
        )
        if is_replay_unconfirmed and prior_hash != computed_hash:
            _mark_active(
                cfg,
                slot=slot,
                status="owner_recovery_required",
                attempt=attempt,
                idem=idem,
                started_at=started_at,
                error="request_identity_mismatch",
                request_hash=prior_hash,
                scope_hash=prior_scope,
            )
            return 409, {"ok": False, "error": "owner_recovery_required"}
        req_hash = prior_hash or computed_hash
        scope_hash = prior_scope or current_scope
        # Absolute remaining budget for this admission (S4/T3).
        remaining = (
            _remaining_deadline_s(
                {"started_at": started_at},
                cfg,
            )
            if is_replay_unconfirmed or prior_started
            else float(cfg.deadline_s)
        )

        # R4/T1/U1: persist this admission BEFORE POST, without a prior attempt's run_id.
        _mark_active(
            cfg,
            slot=slot,
            status="create_unconfirmed",
            attempt=attempt,
            idem=idem,
            started_at=started_at,
            request_hash=req_hash,
            scope_hash=scope_hash,
            provider=cfg.provider,
            model=cfg.model,
        )

        def _persist_created(run_id: str) -> None:
            _mark_active(
                cfg,
                slot=slot,
                status="running",
                attempt=attempt,
                idem=idem,
                started_at=started_at,
                run_id=run_id,
                request_hash=req_hash,
                scope_hash=scope_hash,
                provider=cfg.provider,
                model=cfg.model,
            )

        try:
            result = run_research(
                gateway,
                instructions=instructions,
                input_text=input_text,
                idempotency_key=idem,
                on_created=_persist_created,
                deadline_s=remaining,
            )
            record = _publish_from_result(cfg, slot=slot, result=result, attempt=attempt, idem=idem)
            clear_active_admission(cfg.state_dir, slot_label=slot.label)
            return 200, {"ok": True, "replay": bool(record.get("replay")), "result": record}
        except GatewayError as exc:
            logger.warning("ingest gateway failure category=%s", exc.category)
            try:
                prior = load_slot_record(cfg.state_dir, slot) or {}
            except ActiveAdmissionError:
                prior = {}
            # Only trust a run_id bound to THIS admission (on_created), never a prior attempt.
            this_run_id = ""
            if str(prior.get("idempotency_key") or "") == idem and str(prior.get("attempt") or "") == str(
                attempt
            ):
                this_run_id = str(prior.get("run_id") or "").strip()
            mark_kw: dict[str, Any] = {
                "request_hash": req_hash,
                "scope_hash": scope_hash,
                "provider": cfg.provider,
                "model": cfg.model,
            }
            if exc.category == "unknown_run":
                _mark_active(
                    cfg,
                    slot=slot,
                    status="unknown",
                    attempt=attempt,
                    idem=idem,
                    started_at=str(prior.get("started_at") or started_at),
                    run_id=this_run_id,
                    error=exc.category,
                    **mark_kw,
                )
                return 409, {"ok": False, "error": "unknown_run"}
            if exc.category == "provider_error":
                if this_run_id:
                    _mark_active(
                        cfg,
                        slot=slot,
                        status="unknown",
                        attempt=attempt,
                        idem=idem,
                        started_at=str(prior.get("started_at") or started_at),
                        run_id=this_run_id,
                        error=exc.category,
                        **mark_kw,
                    )
                    return 409, {"ok": False, "error": "unknown_run"}
                _mark_active(
                    cfg,
                    slot=slot,
                    status="create_unconfirmed",
                    attempt=attempt,
                    idem=idem,
                    started_at=started_at,
                    error=exc.category,
                    **mark_kw,
                )
                return 502, {"ok": False, "error": "provider_error"}
            if exc.category == "malformed_response" and not this_run_id:
                _mark_active(
                    cfg,
                    slot=slot,
                    status="create_unconfirmed",
                    attempt=attempt,
                    idem=idem,
                    started_at=started_at,
                    error=exc.category,
                    **mark_kw,
                )
                return 502, {"ok": False, "error": exc.category}
            if exc.category in {"incomplete_run", "timeout"}:
                _mark_active(
                    cfg,
                    slot=slot,
                    status="failed_terminal",
                    attempt=attempt,
                    idem=idem,
                    started_at=started_at,
                    run_id=this_run_id,
                    error=exc.category,
                    **mark_kw,
                )
            if exc.category in {"provider_mismatch", "model_mismatch"}:
                _mark_active(
                    cfg,
                    slot=slot,
                    status="invalid_result",
                    attempt=attempt,
                    idem=idem,
                    started_at=started_at,
                    run_id=this_run_id,
                    error=exc.category,
                    **mark_kw,
                )
            code = 401 if exc.category == "auth_error" and exc.status_code == 401 else 502
            if exc.category == "auth_error" and exc.status_code == 403:
                code = 403
            if exc.category in {"timeout", "unknown_run"}:
                code = 504 if exc.category == "timeout" else 409
            return code, {"ok": False, "error": exc.category}
        except FileExistsError:
            return 409, {"ok": False, "error": "filename_conflict"}
        except ValueError as exc:
            logger.warning("ingest validation failure type=%s", type(exc).__name__)
            _mark_active(
                cfg,
                slot=slot,
                status="failed_terminal",
                attempt=attempt,
                idem=idem,
                started_at=started_at,
                error=str(exc),
                request_hash=req_hash,
                scope_hash=scope_hash,
                provider=cfg.provider,
                model=cfg.model,
            )
            return 502, {"ok": False, "error": str(exc)}
        except Exception:  # noqa: BLE001 — fail closed; never leak secrets
            logger.warning("ingest failure type=internal_error")
            return 500, {"ok": False, "error": "internal_error"}
    finally:
        release_inflight()


def make_handler(cfg: AdapterConfig) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            logger.info(redact(fmt % args))

        def _send(self, code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/healthz":
                ready = cfg.provider_ready()
                self._send(200 if ready else 503, {"ok": ready, "service": "hermes-ingest"})
                return
            self._send(404, {"ok": False, "error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path != "/ingest":
                self._send(404, {"ok": False, "error": "not_found"})
                return
            if not cfg.ingest_token:
                self._send(503, {"ok": False, "error": "provider_unconfigured"})
                return
            if not _check_bearer(self.headers.get("Authorization"), cfg.ingest_token):
                self._send(401, {"ok": False, "error": "unauthorized"})
                return
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                self._send(415, {"ok": False, "error": "content_type"})
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                self._send(400, {"ok": False, "error": "bad_length"})
                return
            if length <= 0 or length > MAX_BODY:
                self._send(413, {"ok": False, "error": "body_too_large"})
                return
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send(400, {"ok": False, "error": "invalid_json"})
                return
            if not isinstance(body, dict):
                self._send(400, {"ok": False, "error": "invalid_json"})
                return
            code, payload = handle_ingest(cfg, body)
            self._send(code, payload)

    return Handler


def serve(cfg: AdapterConfig | None = None) -> ThreadingHTTPServer:
    config = cfg or AdapterConfig()
    handler = make_handler(config)
    server = ThreadingHTTPServer((config.host, config.port), handler)
    thread = threading.Thread(target=server.serve_forever, name="hermes-ingest", daemon=True)
    thread.start()
    return server


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    cfg = AdapterConfig()
    handler = make_handler(cfg)
    server = ThreadingHTTPServer((cfg.host, cfg.port), handler)
    logger.info("hermes-ingest listening on %s:%s", cfg.host, cfg.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
