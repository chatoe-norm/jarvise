"""P4 partner-audit CLI and morning-tail wiring."""

from __future__ import annotations

import contextlib
import csv
import hashlib
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from jarvise.cli import app
from jarvise_assist import partner_store as ps
from jarvise_assist.partner_audit import GRACE_BINANCE_MS, Transfer
from jarvise_assist.store import write_settings
from jarvise_ingest.db import SCHEMA_VERSION, open_db

D = Decimal
NOW = 1_700_000_000_000
runner = CliRunner()


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@contextlib.contextmanager
def _always_lock() -> Iterator[bool]:
    yield True


class FakeClient:
    def __init__(self, transfers: list[Transfer] | None = None, *, fail: bool = False) -> None:
        self.transfers = list(transfers or [])
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    def income_rows(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(kwargs)
        if self.fail:
            raise TimeoutError("x")
        out: list[dict[str, Any]] = []
        for t in self.transfers:
            if t.income_type != kwargs["income_type"]:
                continue
            if not (kwargs["start_ms"] <= t.time_ms <= kwargs["end_ms"]):
                continue
            out.append(
                {
                    "time": t.time_ms,
                    "income": str(t.amount),
                    "asset": t.asset,
                    "incomeType": t.income_type,
                    "tranId": t.tran_id,
                }
            )
        return out

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"unexpected method {name}")


def test_cli_no_binance_verify_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "cli.db"
    conn = open_db(db)
    admin = ps.create_partner(conn, name="admin", is_admin=True, now_ms=NOW)
    ps.apply_deposit(conn, admin["partner_id"], D("1000"), D("1000"), NOW)
    conn.close()
    monkeypatch.setenv("JARVISE_DB", str(db))
    before = _hash(db)
    result = runner.invoke(app, ["assist", "partner-audit", "--no-binance"])
    assert result.exit_code == 0
    assert "verify_ledger" in result.stdout
    assert "OK" not in result.stdout  # Thai, not English OK for success path of binance
    assert _hash(db) == before


def test_cli_csv_and_refuse_overwrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "csv.db"
    conn = open_db(db)
    admin = ps.create_partner(conn, name="admin", is_admin=True, now_ms=NOW)
    ps.apply_deposit(conn, admin["partner_id"], D("1000"), D("1000"), NOW)
    # formula-like name via note on adjust? use partner with special note through deposit note
    bob = ps.create_partner(conn, name="=Bob", is_admin=False, now_ms=NOW + 1)
    # name UNIQUE — "=Bob" is ok
    ps.apply_deposit(
        conn,
        bob["partner_id"],
        D("10"),
        D("1010"),
        NOW + 2,
        note="+inject",
    )
    conn.close()
    monkeypatch.setenv("JARVISE_DB", str(db))
    out = tmp_path / "events.csv"
    before = _hash(db)
    result = runner.invoke(app, ["assist", "partner-audit", "--no-binance", "--csv", str(out)])
    assert result.exit_code == 0
    assert out.is_file()
    with out.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    assert rows[0][0] == "event_id"
    # decimal strings
    assert any("1000" in cell for row in rows[1:] for cell in row)
    # formula prefix
    flat = "\n".join(",".join(r) for r in rows)
    assert "'=Bob" in flat or any(c.startswith("'=") for row in rows for c in row)
    assert any(c.startswith("'+") or c == "'+inject" for row in rows for c in row)
    again = runner.invoke(app, ["assist", "partner-audit", "--no-binance", "--csv", str(out)])
    assert again.exit_code == 2
    assert "มีไฟล์อยู่แล้ว" in again.stdout + again.stderr
    assert _hash(db) == before


def test_cli_read_failure_exit_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "fail.db"
    conn = open_db(db)
    admin = ps.create_partner(conn, name="admin", is_admin=True, now_ms=NOW)
    ps.apply_deposit(conn, admin["partner_id"], D("1000"), D("1000"), NOW)
    conn.close()
    monkeypatch.setenv("JARVISE_DB", str(db))
    monkeypatch.setattr(
        "jarvise_assist.futures.build_futures_client",
        lambda: FakeClient(fail=True),
    )
    result = runner.invoke(app, ["assist", "partner-audit", "--days", "7"])
    assert result.exit_code == 2
    text = result.stdout + result.stderr
    assert "ตรวจ Binance ไม่ได้" in text
    assert "OK" not in text
    assert "ข้อสรุป" in text


def test_cli_no_read_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "nokey.db"
    conn = open_db(db)
    admin = ps.create_partner(conn, name="admin", is_admin=True, now_ms=NOW)
    ps.apply_deposit(conn, admin["partner_id"], D("1000"), D("1000"), NOW)
    conn.close()
    monkeypatch.setenv("JARVISE_DB", str(db))
    monkeypatch.setattr("jarvise_assist.futures.build_futures_client", lambda: None)
    result = runner.invoke(app, ["assist", "partner-audit"])
    assert result.exit_code == 2
    assert "ไม่มี read key" in result.stdout + result.stderr


def test_cli_alertable_exit_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "alert.db"
    conn = open_db(db)
    admin = ps.create_partner(conn, name="admin", is_admin=True, now_ms=NOW)
    ps.apply_deposit(conn, admin["partner_id"], D("1000"), D("1000"), NOW)
    conn.close()
    t_xfer = NOW + 1000
    monkeypatch.setenv("JARVISE_DB", str(db))
    monkeypatch.setattr(
        "jarvise_assist.futures.build_futures_client",
        lambda: FakeClient(
            [
                Transfer(
                    time_ms=t_xfer,
                    amount=D("55"),
                    asset="USDT",
                    income_type="TRANSFER",
                    tran_id="orphan",
                )
            ]
        ),
    )
    monkeypatch.setattr("time.time", lambda: (t_xfer + GRACE_BINANCE_MS + 5_000) / 1000.0)
    result = runner.invoke(app, ["assist", "partner-audit", "--days", "30"])
    assert result.exit_code == 1
    assert "transfer_in_unrecorded" in result.stdout


def test_cli_schema_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "schema.db"
    conn = open_db(db)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION - 1}")
    conn.commit()
    conn.close()
    monkeypatch.setenv("JARVISE_DB", str(db))
    result = runner.invoke(app, ["assist", "partner-audit", "--no-binance"])
    assert result.exit_code == 2
    assert "schema mismatch" in result.stdout + result.stderr


def test_cli_not_init(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "empty.db"
    open_db(db).close()
    monkeypatch.setenv("JARVISE_DB", str(db))
    result = runner.invoke(app, ["assist", "partner-audit", "--no-binance"])
    assert result.exit_code == 0
    assert "ยังไม่ได้ตั้งต้น" in result.stdout


def test_morning_tail_audit_error_keeps_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvise import jobs

    db = tmp_path / "morning.db"
    conn = open_db(db)
    write_settings(conn, enabled=False)
    conn.close()
    monkeypatch.setenv("JARVISE_DB", str(db))
    monkeypatch.setattr(jobs, "single_flight", lambda _name: _always_lock())
    monkeypatch.setattr(jobs, "_publish_best_effort", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "jarvise_assist.partner_audit.run_partner_audit_job",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    code, payload = jobs.run_assist_morning_job()
    assert code == 0  # assist disabled -> morning ok
    assert payload.get("ok") is True
    assert payload["partner_audit"]["ok"] is False
    assert payload["partner_audit"]["status"] == "error"
    assert payload["partner_audit"]["error"] == "RuntimeError"


def test_morning_tail_runs_when_assist_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvise import jobs

    db = tmp_path / "dis.db"
    conn = open_db(db)
    write_settings(conn, enabled=False)
    conn.close()
    monkeypatch.setenv("JARVISE_DB", str(db))
    monkeypatch.setattr(jobs, "single_flight", lambda _name: _always_lock())
    monkeypatch.setattr(jobs, "_publish_best_effort", lambda *_a, **_k: None)
    called: list[bool] = []

    def fake_job(*_a: Any, **_k: Any) -> dict[str, Any]:
        called.append(True)
        return {
            "ok": True,
            "status": "not_init",
            "new": 0,
            "open": 0,
            "pending": 0,
            "matched": 0,
            "alert_sent": False,
            "error": None,
        }

    monkeypatch.setattr("jarvise_assist.partner_audit.run_partner_audit_job", fake_job)
    monkeypatch.setattr("jarvise_assist.futures.build_futures_client", lambda: None)
    code, payload = jobs.run_assist_morning_job()
    assert code == 0
    assert called == [True]
    assert "partner_audit" in payload


def test_static_guards_portal_and_paths() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "jarvise_assist"
    text = (root / "partner_audit.py").read_text(encoding="utf-8")
    for banned in (
        "float(",
        "chat_id",
        "telegram_user_id",
        "INSERT",
        "UPDATE",
        "DELETE",
        "POST",
        "/sapi",
        "reply_markup",
    ):
        assert banned not in text
    fut = (root / "futures.py").read_text(encoding="utf-8")
    assert "def income_rows" in fut
    assert "INCOME_PATH" in fut
    body = fut.split("def income_rows", 1)[1].split("def user_trades", 1)[0]
    assert "INCOME_PATH" in body
    assert "_PATH" not in body.replace("INCOME_PATH", "")
    jobs_src = (Path(__file__).resolve().parents[1] / "src" / "jarvise" / "jobs.py").read_text(
        encoding="utf-8"
    )
    assert "send=lambda text: send_message(text)," in jobs_src
