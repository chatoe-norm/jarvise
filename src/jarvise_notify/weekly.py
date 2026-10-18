"""Read-only weekly owner report. No orders and no analyzer changes."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from jarvise.freshness import RISK_MONITOR_STALE_AFTER_MIN, dashboard_freshness
from jarvise_ingest.db import (
    get_paper_account,
    list_paper_orders_asc,
    list_paper_positions,
    open_db,
)
from jarvise_notify.telegram import notify_configured, send_telegram_message
from jarvise_paper.feedback import WINDOW_30D_MS, metrics_by_decision_source, read_ev_gate_latch
from jarvise_paper.metrics import STARTING_PAPER_EQUITY, reconstruct_closed_trades
from jarvise_paper.shadow_veto import WINDOW_DAYS, digest_dir, score_shadow
from jarvise_risk import get_paper_auto_decide_enabled, kill_switch_state
from jarvise_trade.flags import live_trading_enabled

MARK_OK = "OK"
MARK_LOOK = "ดู"
MARK_DO = "ต้องทำ"
DIGEST_STALE_HOURS = 36.0
SHADOW_SCORED_CAP = 40
WEEK_MS = 7 * 24 * 60 * 60 * 1000

JOB_STATUS_KEYS: tuple[tuple[str, str], ...] = (
    ("ingest", "jarvise:ingest:last"),
    ("rag", "jarvise:rag:last"),
    ("paper", "jarvise:paper:last"),
    ("paper_expire", "jarvise:paper:expire:last"),
    ("paper_digest", "jarvise:paper:digest:last"),
    ("paper_auto", "jarvise:paper_auto:last"),
    ("ingest_health", "jarvise:ingest:health"),
    ("risk_monitor", "jarvise:risk_monitor:last"),
    ("shadow_veto", "jarvise:shadow_veto:last"),
)

SHADOW_WINDOW_ACTION = "ส่งผล shadow veto ให้ Claude ตัดสินตามกติกา"


def _num(value: Any, digits: int = 2) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def _age_min(at_ms: Any, now_ms: int) -> float | None:
    try:
        stamp = int(at_ms)
    except (TypeError, ValueError):
        return None
    return (now_ms - stamp) / 60_000


def format_weekly_report(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Turn a snapshot into short Thai lines. Markers are OK, ดู, and ต้องทำ."""
    lines: list[str] = ["รายงานรายสัปดาห์"]
    actions: list[str] = []

    def flag(level: str, note: str) -> None:
        actions.append(f"{level}: {note}")

    if snapshot.get("live"):
        lines.append(f"ระบบ: live เปิด [{MARK_DO}]")
        flag(MARK_DO, "live เปิดอยู่")
    else:
        lines.append(f"ระบบ: live ปิด [{MARK_OK}]")

    if snapshot.get("auto_decide"):
        lines.append(f"ระบบ: auto-decide เปิด [{MARK_LOOK}]")
        flag(MARK_LOOK, "auto-decide เปิดอยู่")
    else:
        lines.append(f"ระบบ: auto-decide ปิด [{MARK_OK}]")

    gate = snapshot.get("ev_gate") or {}
    n = gate.get("n")
    ev_txt = _num(gate.get("ev"), 4)
    gate_line = f"n={n if n is not None else '—'} EV={ev_txt}"
    if gate.get("latched"):
        lines.append(f"ระบบ: EV gate latched {gate_line} [{MARK_LOOK}]")
        flag(MARK_LOOK, f"EV gate latched {gate_line}")
    else:
        lines.append(f"ระบบ: EV gate ไม่ latch {gate_line} [{MARK_OK}]")

    if snapshot.get("kill_switch_known") is False:
        lines.append(f"ระบบ: kill switch อ่านไม่ได้ [{MARK_DO}]")
        flag(MARK_DO, "kill switch อ่านไม่ได้")
    elif snapshot.get("kill_switch"):
        lines.append(f"ระบบ: kill switch เปิด [{MARK_DO}]")
        flag(MARK_DO, "kill switch เปิด")
    else:
        lines.append(f"ระบบ: kill switch ปิด [{MARK_OK}]")

    if snapshot.get("ledger_error"):
        lines.append(f"paper: อ่านสมุดไม่ได้ [{MARK_DO}]")
        flag(MARK_DO, "อ่านสมุด paper ไม่ได้")
        lines.append(f"shadow: อ่านสมุดไม่ได้ [{MARK_DO}]")
    else:
        _append_shadow_and_paper(lines, flag, snapshot)

    for item in snapshot.get("health") or []:
        level = str(item.get("level") or MARK_OK)
        lines.append(f"ข้อมูล: {item.get('line')} [{level}]")
        if level in {MARK_LOOK, MARK_DO} and item.get("note"):
            flag(level, str(item["note"]))

    for job in snapshot.get("jobs") or []:
        level = str(job.get("level") or MARK_OK)
        lines.append(f"งาน: {job.get('name')} {job.get('result')} [{level}]")
        if level in {MARK_LOOK, MARK_DO} and job.get("note"):
            flag(level, str(job["note"]))

    do = [item for item in actions if item.startswith(MARK_DO)]
    look = [item for item in actions if item.startswith(MARK_LOOK)]
    if not do and not look:
        lines.append("ไม่ต้องทำอะไร")
    else:
        lines.extend(do)
        lines.extend(look)

    return {
        "ok": True,
        "paper_only": True,
        "text": "\n".join(lines),
        "actions": do + look,
    }


def _append_shadow_and_paper(
    lines: list[str],
    flag: Any,
    snapshot: dict[str, Any],
) -> None:
    shadow = snapshot.get("shadow") or {}
    scored = int(shadow.get("n_scored") or 0)
    no_obj = int(shadow.get("n_no_objection") or 0)
    veto = int(shadow.get("n_veto") or 0)
    elapsed = shadow.get("elapsed_days")
    elapsed_txt = "—" if elapsed is None else _num(elapsed, 1)
    window = "ปิด" if shadow.get("window_closed") else "เปิด"
    shadow_line = (
        f"shadow: {scored}/{SHADOW_SCORED_CAP} ไม่คัดค้าน {no_obj} veto {veto} "
        f"วัน {elapsed_txt}/{WINDOW_DAYS} หน้าต่าง{window}"
    )
    if shadow.get("window_closed"):
        lines.append(f"{shadow_line} [{MARK_DO}]")
        flag(MARK_DO, SHADOW_WINDOW_ACTION)
    else:
        lines.append(f"{shadow_line} [{MARK_OK}]")

    paper = snapshot.get("paper") or {}
    lines.append(
        f"paper: equity {_num(paper.get('equity'))} เปิด {int(paper.get('open_positions') or 0)} ไม้ [{MARK_OK}]"
    )
    lines.append(
        f"paper: ปิด {int(paper.get('closed_7d') or 0)} ไม้ 7 วัน PnL {_num(paper.get('pnl_7d'))} [{MARK_OK}]"
    )


def _redis_json(key: str) -> Any:
    url = (os.environ.get("REDIS_URL") or "").strip()
    if not url:
        return None
    try:
        import redis

        client = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=2.0,
            socket_timeout=2.0,
        )
        raw = client.get(key)
    except Exception:  # noqa: BLE001 — a missing Redis blob is a look item, not a crash
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}


def _digest_age_hours(directory: Path, *, now_ms: int) -> float | None:
    if not directory.is_dir():
        return None
    files = [path for path in directory.iterdir() if path.is_file() and path.suffix == ".md"]
    if not files:
        return None
    latest = max(files, key=lambda path: (path.stat().st_mtime, path.name))
    return (now_ms / 1000.0 - latest.stat().st_mtime) / 3600.0


def _health_items(
    snapshot_bits: dict[str, Any], *, now_ms: int, freshness: dict[str, Any]
) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    ingest = snapshot_bits.get("ingest") if isinstance(snapshot_bits.get("ingest"), dict) else None
    health_blob = (
        snapshot_bits.get("ingest_health") if isinstance(snapshot_bits.get("ingest_health"), dict) else None
    )
    ingest_at = None if ingest is None else ingest.get("at_ms")
    if ingest_at is None and health_blob is not None:
        ingest_at = health_blob.get("at_ms")
    ingest_age = _age_min(ingest_at, now_ms)
    max_age = float(freshness.get("market_safety_max_age_min") or 90)
    if ingest_age is None:
        items.append({"line": "ingest ไม่มีสถานะ", "level": MARK_LOOK, "note": "ingest ไม่มีสถานะ"})
    elif ingest_age > max_age:
        items.append(
            {
                "line": f"ingest อายุ {_num(ingest_age, 0)} นาที",
                "level": MARK_LOOK,
                "note": f"ingest เก่า {_num(ingest_age, 0)} นาที",
            }
        )
    else:
        items.append({"line": f"ingest อายุ {_num(ingest_age, 0)} นาที", "level": MARK_OK, "note": ""})

    digest_age = snapshot_bits.get("digest_age_hours")
    if digest_age is None:
        items.append({"line": "ข่าว ไม่มีไฟล์", "level": MARK_LOOK, "note": "ข่าวไม่มีไฟล์"})
    elif float(digest_age) > DIGEST_STALE_HOURS:
        items.append(
            {
                "line": f"ข่าว อายุ {_num(digest_age, 1)} ชม.",
                "level": MARK_LOOK,
                "note": f"ข่าวเก่า {_num(digest_age, 1)} ชม.",
            }
        )
    else:
        items.append({"line": f"ข่าว อายุ {_num(digest_age, 1)} ชม.", "level": MARK_OK, "note": ""})

    risk = snapshot_bits.get("risk_monitor")
    risk_age = _age_min((risk or {}).get("at_ms") if isinstance(risk, dict) else None, now_ms)
    stale_after = float(freshness.get("risk_monitor_stale_after_min") or RISK_MONITOR_STALE_AFTER_MIN)
    if risk_age is None or risk_age > stale_after:
        shown = "ไม่มีสถานะ" if risk_age is None else f"อายุ {_num(risk_age, 0)} นาที"
        items.append(
            {
                "line": f"risk monitor {shown}",
                "level": MARK_DO,
                "note": "risk monitor ค้าง",
            }
        )
    else:
        items.append({"line": f"risk monitor อายุ {_num(risk_age, 0)} นาที", "level": MARK_OK, "note": ""})
    return items


def _job_items(blobs: dict[str, Any]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for name, _key in JOB_STATUS_KEYS:
        payload = blobs.get(name)
        if not isinstance(payload, dict):
            items.append({"name": name, "result": "ไม่มีผล", "level": MARK_OK, "note": ""})
            continue
        if payload.get("skipped"):
            items.append({"name": name, "result": "ข้าม", "level": MARK_OK, "note": ""})
            continue
        if payload.get("ok") is False:
            items.append({"name": name, "result": "ล้ม", "level": MARK_DO, "note": f"{name} ล้ม"})
            continue
        items.append({"name": name, "result": "ok", "level": MARK_OK, "note": ""})
    return items


def collect_weekly_snapshot(*, now_ms: int | None = None) -> dict[str, Any]:
    """Read ledger, Redis, and digest files. Writes nothing."""
    ts = int(now_ms if now_ms is not None else time.time() * 1000)
    ks = kill_switch_state(strict=False)
    freshness = dashboard_freshness()
    blobs = {name: _redis_json(key) for name, key in JOB_STATUS_KEYS}
    shadow = {
        "n_scored": 0,
        "n_no_objection": 0,
        "n_veto": 0,
        "elapsed_days": None,
        "window_closed": False,
    }
    equity = STARTING_PAPER_EQUITY
    open_positions = 0
    closed_7d = 0
    pnl_7d = 0.0
    ev_gate: dict[str, Any] = {"latched": False, "n": 0, "ev": None}
    ledger_error: str | None = None
    raw = os.environ.get("JARVISE_DB") or "data/analytics/jarvise.db"
    path = Path(raw)
    if not path.exists():
        ledger_error = "missing"
    else:
        conn = None
        try:
            conn = open_db(path)
            scored = score_shadow(conn, now_ms=ts)
            shadow = {
                "n_scored": scored.get("n_scored"),
                "n_no_objection": scored.get("n_no_objection"),
                "n_veto": scored.get("n_veto"),
                "elapsed_days": scored.get("elapsed_days"),
                "window_closed": scored.get("window_closed"),
            }
            account = get_paper_account(conn)
            equity = float(account.get("equity") or STARTING_PAPER_EQUITY)
            open_positions = len(list_paper_positions(conn))
            trades, _unmatched, _open = reconstruct_closed_trades(list_paper_orders_asc(conn))
            week_start = ts - WEEK_MS
            recent = [trade for trade in trades if int(trade["close_ts"]) >= week_start]
            closed_7d = len(recent)
            pnl_7d = round(sum(float(trade["pnl_usd"]) for trade in recent), 8)
            latch = read_ev_gate_latch(conn)
            by_src = metrics_by_decision_source(conn, since_ms=ts - WINDOW_30D_MS)
            auto = by_src.get("auto") or {}
            ev_gate = {
                "latched": latch is not None,
                "n": auto.get("closed_trades"),
                "ev": auto.get("expected_value_ev"),
            }
        except Exception:  # noqa: BLE001 — a failed read must not look like a quiet book
            ledger_error = "read_failed"
        finally:
            if conn is not None:
                conn.close()
    return {
        "live": live_trading_enabled(),
        "auto_decide": get_paper_auto_decide_enabled(),
        "ev_gate": ev_gate,
        "kill_switch": bool(ks.get("engaged")),
        "kill_switch_known": ks.get("known", True),
        "ledger_error": ledger_error,
        "shadow": shadow,
        "paper": {
            "equity": equity,
            "open_positions": open_positions,
            "closed_7d": closed_7d,
            "pnl_7d": pnl_7d,
        },
        "health": _health_items(
            {
                "ingest": blobs.get("ingest"),
                "ingest_health": blobs.get("ingest_health"),
                "risk_monitor": blobs.get("risk_monitor"),
                "digest_age_hours": _digest_age_hours(digest_dir(), now_ms=ts),
            },
            now_ms=ts,
            freshness=freshness,
        ),
        "jobs": _job_items(blobs),
    }


def build_weekly_report(*, send: bool = False, now_ms: int | None = None) -> dict[str, Any]:
    """Build the report. Telegram send failures stay in the JSON."""
    report = format_weekly_report(collect_weekly_snapshot(now_ms=now_ms))
    report["telegram_sent"] = False
    report["telegram_error"] = None
    if not send:
        return report
    if not notify_configured():
        report["telegram_error"] = "not_configured"
        return report
    try:
        sent = send_telegram_message(str(report["text"]))
    except Exception as exc:  # noqa: BLE001 — the report itself still returns
        report["telegram_error"] = type(exc).__name__
        return report
    report["telegram_sent"] = bool(sent)
    if not sent:
        report["telegram_error"] = "send_failed"
    return report


WEEKLY_BUILD_FAILURE = "รายงานสัปดาห์สร้างไม่สำเร็จ"


def notify_weekly_build_failure() -> dict[str, Any]:
    """One short line when the report cannot be built. Send errors stay in the JSON."""
    out: dict[str, Any] = {"telegram_sent": False, "telegram_error": None}
    if not notify_configured():
        out["telegram_error"] = "not_configured"
        return out
    try:
        sent = send_telegram_message(WEEKLY_BUILD_FAILURE)
    except Exception as exc:  # noqa: BLE001
        out["telegram_error"] = type(exc).__name__
        return out
    out["telegram_sent"] = bool(sent)
    if not sent:
        out["telegram_error"] = "send_failed"
    return out
