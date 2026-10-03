"""Kill-switch must fail closed: unknown state blocks services; engage failures alert."""

from __future__ import annotations

import pytest

from jarvise import jobs
from jarvise_notify import format_kill_switch_message
from jarvise_paper import cli as paper_cli
from jarvise_risk import (
    KillSwitchUnavailable,
    caps,
    engage_kill_switch,
    engage_kill_switch_strict,
    kill_switch_state,
    read_kill_switch,
)


class _FakeRedis:
    def __init__(self, store: dict[str, str] | None = None, *, fail: bool = False):
        self.store = store if store is not None else {}
        self.fail = fail

    def get(self, key: str):
        if self.fail:
            raise ConnectionError("redis down")
        return self.store.get(key)

    def set(self, key: str, value: str):
        if self.fail:
            raise ConnectionError("redis down")
        self.store[key] = value


def test_state_unset_url_strict_is_engaged_unknown(monkeypatch) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)
    state = kill_switch_state(strict=True)
    assert state == {"engaged": True, "known": False, "reason": None, "error": "REDIS_URL unset"}
    assert read_kill_switch(strict=True) is True


def test_state_unset_url_non_strict_is_standalone(monkeypatch) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)
    state = kill_switch_state(strict=False)
    assert state["engaged"] is False and state["known"] is False
    assert read_kill_switch(strict=False) is False
    # Local paper CLI without Redis keeps working (standalone mode).
    assert paper_cli.kill_switch_engaged() is False


def test_state_connection_error_is_engaged_even_non_strict(monkeypatch) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setattr(caps, "_redis_client", lambda url: _FakeRedis(fail=True))
    for strict in (True, False):
        state = kill_switch_state(strict=strict)
        assert state["engaged"] is True and state["known"] is False
        assert "ConnectionError" in (state["error"] or "")
    assert paper_cli.kill_switch_engaged() is True


def test_state_reads_value_and_reason(monkeypatch) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    fake = _FakeRedis({"jarvise:kill_switch": "1", "jarvise:kill_switch:reason": "drawdown"})
    monkeypatch.setattr(caps, "_redis_client", lambda url: fake)
    state = kill_switch_state()
    assert state == {"engaged": True, "known": True, "reason": "drawdown", "error": None}
    fake.store["jarvise:kill_switch"] = "0"
    assert kill_switch_state() == {"engaged": False, "known": True, "reason": None, "error": None}


def test_engage_sets_keys_and_alerts_once_per_transition(monkeypatch) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    fake = _FakeRedis()
    monkeypatch.setattr(caps, "_redis_client", lambda url: fake)
    alerts: list[tuple] = []
    monkeypatch.setattr(
        caps, "notify_kill_switch", lambda reason, **kw: alerts.append((reason, kw)) or True
    )
    assert engage_kill_switch(reason="max_daily_loss") is True
    assert fake.store["jarvise:kill_switch"] == "1"
    assert fake.store["jarvise:kill_switch:reason"] == "max_daily_loss"
    # Second engage while already on: keys refreshed, no second alert.
    assert engage_kill_switch(reason="again") is True
    assert fake.store["jarvise:kill_switch:reason"] == "again"
    assert alerts == [("max_daily_loss", {"engaged": True})]


def test_engage_failure_returns_false_and_alerts(monkeypatch) -> None:
    alerts: list[tuple] = []
    monkeypatch.setattr(
        caps, "notify_kill_switch", lambda reason, **kw: alerts.append((reason, kw)) or True
    )
    monkeypatch.delenv("REDIS_URL", raising=False)
    assert engage_kill_switch(reason="breach") is False
    assert alerts[-1] == ("breach", {"engaged": False, "error": "REDIS_URL unset"})

    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setattr(caps, "_redis_client", lambda url: _FakeRedis(fail=True))
    assert engage_kill_switch(reason="breach2") is False
    assert alerts[-1][0] == "breach2"
    assert alerts[-1][1]["engaged"] is False
    assert "ConnectionError" in alerts[-1][1]["error"]
    with pytest.raises(KillSwitchUnavailable):
        engage_kill_switch_strict(reason="breach3")


def test_jobs_fail_closed_without_redis(monkeypatch) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)
    assert jobs.kill_switch_engaged() is True
    code, body = jobs.run_paper_run()
    assert code == 3 and body["skipped"] is True
    assert body["reason"].startswith("kill_switch unreadable")
    assert body["kill_switch_known"] is False
    code, body = jobs.run_ingest()
    assert code == 3 and body["skipped"] is True


def test_jobs_fail_closed_on_connection_error(monkeypatch) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setattr(caps, "_redis_client", lambda url: _FakeRedis(fail=True))

    def publish_boom(*a, **k):
        raise ConnectionError("redis down")

    # Redis is down for status publishing too: the job must still return a clean 409 payload.
    monkeypatch.setattr(jobs, "publish_redis_status", publish_boom)
    assert jobs.kill_switch_engaged() is True
    code, body = jobs.run_paper_expire()
    assert code == 3 and "unreadable" in body["reason"]
    code, body = jobs.run_paper_run()
    assert code == 3 and body["skipped"] is True


def test_kill_switch_message_formats() -> None:
    on = format_kill_switch_message("drawdown_lock", engaged=True)
    assert "ENGAGED" in on and "drawdown_lock" in on
    off = format_kill_switch_message("breach", engaged=False, error="ConnectionError")
    assert "FAILED" in off and "ConnectionError" in off
