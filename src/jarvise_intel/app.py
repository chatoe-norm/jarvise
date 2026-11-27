"""Authenticated hermes-ingest HTTP adapter."""

from __future__ import annotations

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
    ACTIVE_ADMISSION_STATUSES,
    MAX_ATTEMPTS,
    adopt_existing_digest,
    assert_current_slot,
    assert_round_trip,
    assert_smoke_confined,
    clear_active_admission,
    extract_json_object,
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
    finalize_completed,
    get_run,
    run_research,
    stop_run,
)

logger = logging.getLogger("jarvise_intel")

MAX_BODY = 4096
SECRET_RE = re.compile(r"(?i)(authorization|api[_-]?key|token|bearer|password)\s*[:=]\s*\S+")


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


def _resume_past_deadline(existing: dict[str, Any], cfg: AdapterConfig) -> bool:
    started = existing.get("started_at")
    if not isinstance(started, str) or not started.strip():
        return False
    try:
        started_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
    except ValueError:
        return False
    if started_dt.tzinfo is None:
        started_dt = started_dt.replace(tzinfo=UTC)
    elapsed = (datetime.now(UTC) - started_dt).total_seconds()
    return elapsed >= cfg.deadline_s


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
    write_slot_record(cfg.state_dir, slot, record)
    if status in ACTIVE_ADMISSION_STATUSES:
        write_active_admission(cfg.state_dir, record)
    else:
        clear_active_admission(cfg.state_dir, slot_label=slot.label)


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

    past_deadline = _resume_past_deadline(existing, cfg)
    if past_deadline:
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


def _block_other_active_admission(
    cfg: AdapterConfig,
    *,
    slot: Any,
) -> tuple[int, dict[str, Any]] | None:
    """R5: another slot's unpaid/unknown admission blocks every other slot."""
    active = load_active_admission(cfg.state_dir)
    if active is None:
        return None
    status = str(active.get("status") or "")
    if status not in ACTIVE_ADMISSION_STATUSES:
        clear_active_admission(cfg.state_dir)
        return None
    other = str(active.get("slot") or "")
    if other == slot.label:
        return None
    # Reconcile the other admission; do not create for this slot while unresolved.
    other_slot = None
    try:
        other_slot = parse_slot(other, smoke_allowed=True)
    except ValueError:
        return 409, {"ok": False, "error": "admission_busy", "slot": other}
    other_record = load_slot_record(cfg.state_dir, other_slot) or active
    if str(other_record.get("status") or "") in {"running", "unknown"} and str(
        other_record.get("run_id") or ""
    ):
        resumed = _handle_resume(cfg, slot=other_slot, existing=other_record)
        if resumed is not None:
            # Other still unresolved (or completed/invalid for that slot) — block this slot.
            still = load_active_admission(cfg.state_dir)
            if still and str(still.get("status") or "") in ACTIVE_ADMISSION_STATUSES:
                return 409, {"ok": False, "error": "admission_busy", "slot": other}
            # Cleared by terminal path — allow this slot to continue.
            return None
        # Terminal fail cleared via _mark_active failed_terminal — continue.
        still = load_active_admission(cfg.state_dir)
        if still and str(still.get("status") or "") in ACTIVE_ADMISSION_STATUSES:
            return 409, {"ok": False, "error": "admission_busy", "slot": other}
        return None
    return 409, {"ok": False, "error": "admission_busy", "slot": other}


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

    existing = load_slot_record(cfg.state_dir, slot)
    if existing and existing.get("status") == "completed":
        return 200, {"ok": True, "replay": True, "result": existing}
    if existing and existing.get("status") == "invalid_result":
        return 409, {"ok": False, "error": "invalid_result"}

    if not try_acquire_inflight(cfg.state_dir):
        return 429, {"ok": False, "error": "inflight_busy"}

    try:
        existing = load_slot_record(cfg.state_dir, slot)
        if existing and existing.get("status") == "completed":
            return 200, {"ok": True, "replay": True, "result": existing}
        if existing and existing.get("status") == "invalid_result":
            return 409, {"ok": False, "error": "invalid_result"}

        # P2-5: adopt a complete file published before the slot record was written.
        try:
            adopted = adopt_existing_digest(digest_dir=cfg.digest_dir, state_dir=cfg.state_dir, slot=slot)
        except (ValueError, TypeError) as exc:
            return 409, {"ok": False, "error": str(exc)}
        if adopted is not None:
            return 200, {"ok": True, "replay": True, "result": adopted}

        # R5: another slot's active paid admission blocks this one.
        blocked = _block_other_active_admission(cfg, slot=slot)
        if blocked is not None:
            return blocked

        # Resume an in-flight run after restart (no second create while unknown/running).
        if existing and existing.get("status") in {"running", "unknown"}:
            resumed = _handle_resume(cfg, slot=slot, existing=existing)
            if resumed is not None:
                return resumed
            existing = load_slot_record(cfg.state_dir, slot)

        # create_unconfirmed for this slot: resume same key without inventing a run_id.
        if existing and existing.get("status") == "create_unconfirmed":
            # Fall through to create with the same attempt/key/started_at.
            pass

        attempt, block = _next_attempt(existing)
        if block == "completed":
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
        if existing and str(existing.get("status") or "") == "create_unconfirmed":
            prior_key = str(existing.get("idempotency_key") or "").strip()
            prior_started = str(existing.get("started_at") or "").strip()
        idem = prior_key or slot.idempotency_key(attempt)
        input_text = (
            f"Produce the news digest JSON for slot {slot.label} "
            f"(Bangkok {slot.bangkok.isoformat()}, UTC file {slot.filename})."
        )
        started_at = prior_started or datetime.now(UTC).isoformat()

        # R4: persist this admission BEFORE POST, without a prior attempt's run_id.
        _mark_active(
            cfg,
            slot=slot,
            status="create_unconfirmed",
            attempt=attempt,
            idem=idem,
            started_at=started_at,
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
            )

        try:
            result = run_research(
                gateway,
                instructions=instructions,
                input_text=input_text,
                idempotency_key=idem,
                on_created=_persist_created,
            )
            record = _publish_from_result(cfg, slot=slot, result=result, attempt=attempt, idem=idem)
            clear_active_admission(cfg.state_dir, slot_label=slot.label)
            return 200, {"ok": True, "replay": bool(record.get("replay")), "result": record}
        except GatewayError as exc:
            logger.warning("ingest gateway failure category=%s", exc.category)
            prior = load_slot_record(cfg.state_dir, slot) or {}
            # Only trust a run_id bound to THIS admission (on_created), never a prior attempt.
            this_run_id = ""
            if str(prior.get("idempotency_key") or "") == idem and str(prior.get("attempt") or "") == str(
                attempt
            ):
                this_run_id = str(prior.get("run_id") or "").strip()
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
