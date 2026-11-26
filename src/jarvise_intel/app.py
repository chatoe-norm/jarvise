"""Authenticated hermes-ingest HTTP adapter."""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from jarvise_intel.digest import (
    extract_json_object,
    load_slot_record,
    parse_slot,
    publish_digest,
    render_markdown,
    validate_items,
)
from jarvise_intel.gateway import GatewayConfig, GatewayError, run_research

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
        self.smoke = (os.environ.get("JARVISE_HERMES_SMOKE") or "").strip() == "1"
        self.host = os.environ.get("HERMES_INGEST_HOST", "0.0.0.0")
        self.port = int(os.environ.get("HERMES_INGEST_PORT", "8092"))
        self.deadline_s = float(os.environ.get("JARVISE_HERMES_DEADLINE_S", "600"))

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
    except ValueError as exc:
        return 400, {"ok": False, "error": str(exc)}

    existing = load_slot_record(cfg.state_dir, slot)
    if existing and existing.get("status") == "completed":
        return 200, {"ok": True, "replay": True, "result": existing}

    try:
        instructions = _load_prompt(cfg.prompt_path)
    except OSError:
        return 500, {"ok": False, "error": "prompt_unreadable"}

    gateway = GatewayConfig(
        base_url=cfg.gateway_url,
        api_key=cfg.api_key,
        provider=cfg.provider,
        model=cfg.model,
        deadline_s=cfg.deadline_s,
    )
    input_text = (
        f"Produce the news digest JSON for slot {slot.label} "
        f"(Bangkok {slot.bangkok.isoformat()}, UTC file {slot.filename})."
    )
    try:
        result = run_research(
            gateway,
            instructions=instructions,
            input_text=input_text,
            idempotency_key=slot.idempotency_key,
        )
        payload = extract_json_object(result.output)
        items, no_news = validate_items(payload)
        markdown = render_markdown(items, no_news=no_news)
        meta = {
            "provider": cfg.provider,
            "model": cfg.model,
            "runtime": result.runtime,
            "usage": result.usage,
            "source_urls": [item.url for item in items],
            "run_id": result.run_id,
            "no_news": no_news,
            "item_count": len(items),
        }
        record = publish_digest(
            digest_dir=cfg.digest_dir,
            state_dir=cfg.state_dir,
            slot=slot,
            markdown=markdown,
            meta=meta,
        )
        return 200, {"ok": True, "replay": bool(record.get("replay")), "result": record}
    except GatewayError as exc:
        logger.warning("ingest gateway failure category=%s", exc.category)
        code = 401 if exc.category == "auth_error" and exc.status_code == 401 else 502
        if exc.category == "auth_error" and exc.status_code == 403:
            code = 403
        if exc.category == "timeout":
            code = 504
        return code, {"ok": False, "error": exc.category}
    except FileExistsError:
        return 409, {"ok": False, "error": "filename_conflict"}
    except ValueError as exc:
        logger.warning("ingest validation failure type=%s", type(exc).__name__)
        return 502, {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — fail closed; never leak secrets
        logger.warning("ingest failure type=%s", type(exc).__name__)
        return 500, {"ok": False, "error": "internal_error"}


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
