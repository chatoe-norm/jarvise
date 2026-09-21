from __future__ import annotations

import json
from io import BytesIO

from jarvise.jobs import JobHandler, run_ingest


class _Handler(JobHandler):
    def __init__(self) -> None:
        self.command = "POST"
        self.path = "/jobs/ingest"
        self.headers = {}
        self.wfile = BytesIO()
        self._status = None

    def send_response(self, code: int, message: str | None = None) -> None:
        self._status = code

    def send_header(self, keyword: str, value: str) -> None:
        return

    def end_headers(self) -> None:
        return


def test_ingest_respects_kill_switch(monkeypatch) -> None:
    monkeypatch.setattr("jarvise.jobs.kill_switch_engaged", lambda: True)
    code, body = run_ingest()
    assert code == 3
    assert body["skipped"] is True
    assert body["paper_only"] is True


def test_jobs_healthz() -> None:
    handler = _Handler()
    handler.command = "GET"
    handler.path = "/healthz"
    handler._dispatch()
    assert handler._status == 200
    payload = json.loads(handler.wfile.getvalue())
    assert payload["ok"] is True
    assert payload["paper_only"] is True


def test_jobs_token_required(monkeypatch) -> None:
    monkeypatch.setenv("JARVISE_JOBS_TOKEN", "secret")
    handler = _Handler()
    handler.headers = {"X-Jarvise-Token": "nope"}
    handler._dispatch()
    assert handler._status == 401
