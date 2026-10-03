from __future__ import annotations

import json
from io import BytesIO

from jarvise.jobs import (
    JobHandler,
    run_ingest,
    run_ingest_health,
    run_paper_auto_decide,
    run_paper_expire,
    run_paper_pending_digest,
    run_paper_run,
)
from jarvise_ingest.db import open_db


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


def test_ingest_also_fetches_paper_timeframe(monkeypatch) -> None:
    monkeypatch.setattr("jarvise.jobs.kill_switch_engaged", lambda: False)
    monkeypatch.delenv("JARVISE_PAPER_TIMEFRAME", raising=False)
    captured: list[list[str]] = []

    class FakeProc:
        returncode = 0
        stdout = '{"ok": true}'
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured.append(cmd)
        return FakeProc()

    monkeypatch.setattr("jarvise.jobs.subprocess.run", fake_run)
    monkeypatch.setattr("jarvise.jobs.publish_redis_status", lambda *a, **k: None)
    code, body = run_ingest()
    assert code == 0
    assert body["ok"] is True
    assert "1h" in body["timeframes"] and "4h" in body["timeframes"]
    assert "1d" in body["timeframes"]  # HTF for analyzer MTF (T2.3)
    tfs = []
    for cmd in captured:
        assert "--timeframe" in cmd
        assert "--skip-derivatives" not in cmd
        tfs.append(cmd[cmd.index("--timeframe") + 1])
    assert tfs == ["1h", "4h", "1d"]


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


def test_paper_run_respects_kill_switch(monkeypatch) -> None:
    monkeypatch.setattr("jarvise.jobs.kill_switch_engaged", lambda: True)
    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "jarvise.jobs.publish_redis_status",
        lambda key, payload: published.append((key, payload)),
    )
    code, body = run_paper_run()
    assert code == 3
    assert body["skipped"] is True
    assert body["paper_only"] is True
    assert published and published[0][0] == "jarvise:paper:last"


def test_paper_expire_respects_kill_switch(monkeypatch) -> None:
    monkeypatch.setattr("jarvise.jobs.kill_switch_engaged", lambda: True)
    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "jarvise.jobs.publish_redis_status",
        lambda key, payload: published.append((key, payload)),
    )
    code, body = run_paper_expire()
    assert code == 3
    assert body["skipped"] is True
    assert published[0][0] == "jarvise:paper:expire:last"


def test_paper_run_cmd_defaults_no_auto_fill(monkeypatch) -> None:
    monkeypatch.setattr("jarvise.jobs.kill_switch_engaged", lambda: False)
    monkeypatch.delenv("JARVISE_PAPER_UNIVERSE", raising=False)
    monkeypatch.delenv("JARVISE_PAPER_TIMEFRAME", raising=False)
    captured: list[list[str]] = []

    class FakeProc:
        returncode = 0
        stdout = '{"ok": true, "results": []}'
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured.append(cmd)
        return FakeProc()

    monkeypatch.setattr("jarvise.jobs.subprocess.run", fake_run)
    monkeypatch.setattr("jarvise.jobs.publish_redis_status", lambda *a, **k: None)
    code, body = run_paper_run()
    assert code == 0
    assert body["ok"] is True
    cmd = captured[0]
    assert "paper" in cmd and "run" in cmd
    assert "--universe" in cmd and "paper_core" in cmd
    assert "--timeframe" in cmd and "4h" in cmd
    assert "--json" in cmd
    assert "--auto-fill" not in cmd


def test_paper_run_route(monkeypatch) -> None:
    monkeypatch.setattr("jarvise.jobs.run_paper_run", lambda: (0, {"ok": True, "paper_only": True}))
    handler = _Handler()
    handler.path = "/jobs/paper-run"
    handler._dispatch()
    assert handler._status == 200


def test_paper_expire_route(monkeypatch) -> None:
    monkeypatch.setattr(
        "jarvise.jobs.run_paper_expire", lambda: (0, {"ok": True, "paper_only": True})
    )
    handler = _Handler()
    handler.path = "/jobs/paper-expire"
    handler._dispatch()
    assert handler._status == 200


def test_paper_pending_digest_empty_db(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("JARVISE_DB", str(tmp_path / "missing.db"))
    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "jarvise.jobs.publish_redis_status",
        lambda key, payload: published.append((key, payload)),
    )
    code, body = run_paper_pending_digest()
    assert code == 0
    assert body["sent"] is False
    assert body["reason"] == "no_database"
    assert published[0][0] == "jarvise:paper:digest:last"


def test_paper_pending_digest_route(monkeypatch) -> None:
    monkeypatch.setattr(
        "jarvise.jobs.run_paper_pending_digest",
        lambda: (0, {"ok": True, "sent": False, "paper_only": True}),
    )
    handler = _Handler()
    handler.path = "/jobs/paper-pending-digest"
    handler._dispatch()
    assert handler._status == 200


def test_doctrine_route_passes_query(monkeypatch) -> None:
    seen: list[tuple[str, int]] = []

    def fake_search(query: str, limit: int):
        seen.append((query, limit))
        return {"ok": True, "query": query, "hits": [], "paper_only": True}

    monkeypatch.setattr("jarvise.jobs.run_doctrine_search", fake_search)
    handler = _Handler()
    handler.command = "GET"
    handler.path = "/doctrine?q=trend_up%20long&limit=2"
    handler._dispatch()
    assert handler._status == 200
    assert seen == [("trend_up long", 2)]


def test_ingest_health_missing_db_publishes(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("JARVISE_DB", str(tmp_path / "missing.db"))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "jarvise.jobs.publish_redis_status",
        lambda key, payload: published.append((key, payload)),
    )
    code, body = run_ingest_health()
    assert code == 0
    assert body["ok"] is False
    assert body["alerts"] and "database missing" in body["alerts"][0]
    assert body["telegram_sent"] is False
    assert published[0][0] == "jarvise:ingest:health"


def test_ingest_health_route(monkeypatch) -> None:
    monkeypatch.setattr(
        "jarvise.jobs.run_ingest_health",
        lambda: (0, {"ok": True, "alerts": [], "paper_only": True}),
    )
    handler = _Handler()
    handler.path = "/jobs/ingest-health"
    handler._dispatch()
    assert handler._status == 200


def test_paper_auto_decide_kill_switch(monkeypatch) -> None:
    monkeypatch.setattr("jarvise.jobs.kill_switch_engaged", lambda: True)
    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "jarvise.jobs.publish_redis_status",
        lambda key, payload: published.append((key, payload)),
    )
    code, body = run_paper_auto_decide()
    assert code == 3 and body["skipped"] is True
    assert published[0][0] == "jarvise:paper_auto:last"


def test_paper_auto_decide_runs_and_publishes(monkeypatch, tmp_path) -> None:
    db = tmp_path / "auto.db"
    open_db(db).close()
    monkeypatch.setenv("JARVISE_DB", str(db))
    monkeypatch.setattr("jarvise.jobs.kill_switch_engaged", lambda: False)
    monkeypatch.setattr(
        "jarvise.jobs.run_auto_decide",
        lambda conn, **kw: {"ok": True, "paper_only": True, "deferred": [], "apply_failed": []},
    )
    monkeypatch.setattr("jarvise.jobs.notify_auto_decide", lambda payload: False)
    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "jarvise.jobs.publish_redis_status",
        lambda key, payload: published.append((key, payload)),
    )
    code, body = run_paper_auto_decide()
    assert code == 0 and body["ok"] is True and body["telegram_sent"] is False
    assert published[0][0] == "jarvise:paper_auto:last"


def test_paper_auto_decide_live_refused_is_409(monkeypatch, tmp_path) -> None:
    db = tmp_path / "auto2.db"
    open_db(db).close()
    monkeypatch.setenv("JARVISE_DB", str(db))
    monkeypatch.setattr("jarvise.jobs.kill_switch_engaged", lambda: False)
    monkeypatch.setattr(
        "jarvise.jobs.run_auto_decide",
        lambda conn, **kw: {"ok": False, "skipped": True, "reason": "live_trading_enabled", "paper_only": True},
    )
    monkeypatch.setattr("jarvise.jobs.notify_auto_decide", lambda payload: True)
    monkeypatch.setattr("jarvise.jobs.publish_redis_status", lambda *a, **k: None)
    code, body = run_paper_auto_decide()
    assert code == 3 and body["telegram_sent"] is True


def test_paper_auto_decide_route(monkeypatch) -> None:
    monkeypatch.setattr(
        "jarvise.jobs.run_paper_auto_decide",
        lambda: (0, {"ok": True, "skipped": True, "reason": "auto_decide_disabled", "paper_only": True}),
    )
    handler = _Handler()
    handler.path = "/jobs/paper-auto-decide"
    handler._dispatch()
    assert handler._status == 200


def test_subprocess_timeout_returns_failure_and_alerts(monkeypatch) -> None:
    import subprocess as sp

    from jarvise import jobs

    monkeypatch.setattr("jarvise.jobs.kill_switch_engaged", lambda: False)
    monkeypatch.setattr("jarvise.jobs.publish_redis_status", lambda *a, **k: None)
    monkeypatch.setenv("JARVISE_JOB_TIMEOUT_PAPER_RUN_S", "7")
    alerts: list[tuple[str, str]] = []
    monkeypatch.setattr("jarvise.jobs.notify_job_failure", lambda job, err: alerts.append((job, err)) or True)
    seen: dict = {}

    def fake_run(cmd, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        raise sp.TimeoutExpired(cmd, kwargs.get("timeout"), output=b"partial", stderr=b"")

    monkeypatch.setattr("jarvise.jobs.subprocess.run", fake_run)
    code, body = run_paper_run()
    assert seen["timeout"] == 7.0
    assert code == jobs.TIMEOUT_EXIT_CODE
    assert body["ok"] is False and body["error"].startswith("timeout after 7s")
    assert alerts == [("paper_run", "timeout after 7s")]


def test_single_flight_refuses_overlap_in_process(monkeypatch) -> None:
    from jarvise import jobs

    monkeypatch.delenv("REDIS_URL", raising=False)
    with jobs.single_flight("paper_expire") as first:
        assert first is True
        with jobs.single_flight("paper_expire") as second:
            assert second is False
        # Different job names do not contend.
        with jobs.single_flight("ingest") as other:
            assert other is True
    with jobs.single_flight("paper_expire") as again:
        assert again is True


def test_single_flight_uses_redis_set_nx(monkeypatch) -> None:
    from jarvise import jobs

    class FakeRedis:
        store: dict[str, str] = {}
        evals: list = []

        def set(self, key, value, nx=False, ex=None):
            if nx and key in self.store:
                return None
            self.store[key] = value
            return True

        def eval(self, script, numkeys, key, token):
            self.evals.append((key, token))
            if self.store.get(key) == token:
                del self.store[key]
                return 1
            return 0

    fake = FakeRedis()
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    # The redis driver is an optional extra; inject a stub module so the Redis path runs here.
    import sys
    import types

    stub = types.ModuleType("redis")
    stub.Redis = types.SimpleNamespace(from_url=lambda *a, **k: fake)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "redis", stub)
    with jobs.single_flight("ingest") as acquired:
        assert acquired is True
        assert "jarvise:lock:ingest" in fake.store
        with jobs.single_flight("ingest") as second:
            assert second is False
    assert "jarvise:lock:ingest" not in fake.store
    assert fake.evals and fake.evals[0][0] == "jarvise:lock:ingest"


def test_paper_run_returns_409_payload_when_running(monkeypatch) -> None:
    from jarvise import jobs

    monkeypatch.setattr("jarvise.jobs.kill_switch_engaged", lambda: False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    lock = jobs._local_lock("paper_run")
    assert lock.acquire(blocking=False)
    try:
        code, body = run_paper_run()
    finally:
        lock.release()
    assert code == 3 and body["skipped"] is True and body["reason"] == "paper_run_running"


def test_live_reconcile_route_and_noop(monkeypatch, tmp_path) -> None:
    from jarvise.jobs import run_live_reconcile

    db = tmp_path / "live.db"
    open_db(db).close()
    monkeypatch.setenv("JARVISE_DB", str(db))
    monkeypatch.delenv("BINANCE_TRADE_API_KEY", raising=False)
    published: list = []
    monkeypatch.setattr("jarvise.jobs.publish_redis_status", lambda k, p: published.append((k, p)))
    code, body = run_live_reconcile()
    assert code == 0 and body["ok"] is True and body["open"] == 0 and body["read_only"] is True
    assert published[0][0] == "jarvise:live:reconcile:last"

    handler = _Handler()
    handler.path = "/jobs/live-reconcile"
    handler.command = "POST"
    handler._dispatch()
    assert handler._status == 200


def test_paper_auto_decide_crash_still_publishes(monkeypatch, tmp_path) -> None:
    db = tmp_path / "crash.db"
    open_db(db).close()
    monkeypatch.setenv("JARVISE_DB", str(db))
    monkeypatch.setattr("jarvise.jobs.kill_switch_engaged", lambda: False)

    def boom(conn, **kw):
        raise RuntimeError("sqlite exploded")

    monkeypatch.setattr("jarvise.jobs.run_auto_decide", boom)
    sent: list[dict] = []
    monkeypatch.setattr("jarvise.jobs.notify_auto_decide", lambda payload: sent.append(payload) or True)
    published: list[tuple[str, dict]] = []
    monkeypatch.setattr("jarvise.jobs.publish_redis_status", lambda key, payload: published.append((key, payload)))
    code, body = run_paper_auto_decide()
    assert code == 1 and body["ok"] is False
    assert body["error"].startswith("RuntimeError: ")
    assert published[0][0] == "jarvise:paper_auto:last" and sent


def test_paper_auto_decide_redis_failure_fails_closed(monkeypatch) -> None:
    def boom() -> bool:
        raise RuntimeError("redis down")

    monkeypatch.setattr("jarvise.jobs.kill_switch_engaged", boom)
    monkeypatch.setattr(
        "jarvise.jobs.publish_redis_status",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("redis down")),
    )
    code, body = run_paper_auto_decide()
    assert code == 3 and body["skipped"] is True and body["reason"] == "kill_switch unreadable"
