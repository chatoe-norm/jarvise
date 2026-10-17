"""Shadow veto log. The model can only add caution; it never touches fills or auto-decide."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from jarvise_ingest.db import latest_derivatives_as_of, latest_order_book
from jarvise_paper.llm_openrouter import OpenRouterError, chat_json
from jarvise_paper.shadow_track import (
    REF_USD_PER_R,
    bar_ms,
    build_track,
    paper_timeframe,
    paper_universe_id,
    sync_track,
)
from jarvise_risk.market_safety import evaluate_market_safety, load_market_safety_config

MODEL = "anthropic/claude-sonnet-5.5"
PROMPT_VERSION = "v1"
MAX_CALLS = 6
DIGEST_CHARS = 1500
JOB_TIMEOUT_S = 180
VERDICTS = frozenset({"veto", "no_objection", "abstain"})
CATEGORIES = frozenset(
    {
        "event_risk",
        "news_contradiction",
        "derivatives_crowding",
        "book_health",
        "data_stale",
        "doctrine_conflict",
        "none",
    }
)
SCORED = frozenset({"veto", "no_objection"})
MIN_NO_OBJECTION = 20
MIN_VETO = 10
MIN_R_GAP = 0.25
MAX_VETO_RATE = 0.60
WINDOW_DAYS = 60
EXTENSION_DAYS = 30
_FORBIDDEN = (
    "secret",
    "token",
    "password",
    "api_key",
    "qty",
    "notional",
    "size_pct",
    "order_id",
    "client_order",
)


def digest_dir() -> Path:
    raw = (os.environ.get("JARVISE_SHADOW_DIGEST_DIR") or "").strip()
    if raw:
        return Path(raw)
    return Path("data/openclaw/workspace-ingest/digests")


def on_time(entry_ts: int, created_at: int, *, timeframe: str) -> bool:
    return int(created_at) <= int(entry_ts) + 2 * bar_ms(timeframe)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def newest_digest(directory: Path) -> tuple[str | None, str | None, str]:
    """Return filename, sha256 of the file, and at most 1,500 characters. Never persists the text."""
    if not directory.is_dir():
        return None, None, ""
    files = [path for path in directory.iterdir() if path.is_file() and path.suffix == ".md"]
    if not files:
        return None, None, ""
    latest = max(files, key=lambda path: path.name)
    raw = latest.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    clipped = text[-DIGEST_CHARS:]
    return latest.name, _sha256(raw), clipped


def _percentile(values: list[float], current: float) -> float | None:
    if not values:
        return None
    return 100.0 * sum(1 for value in values if value <= current) / len(values)


def _coin(symbol: str) -> str:
    coin = symbol.upper()
    for quote in ("USDT", "USD", "BUSD", "USDC"):
        if coin.endswith(quote) and len(coin) > len(quote):
            return coin[: -len(quote)]
    return coin


def _derivative_context(conn: Any, symbol: str, as_of_ms: int) -> dict[str, Any]:
    coin = _coin(symbol)
    rows = conn.execute(
        """
        SELECT timestamp, ingested_at, funding_rate, open_interest_usd
        FROM derivatives_analytics
        WHERE symbol=? AND timestamp<=?
        ORDER BY timestamp, ingested_at
        """,
        (coin, int(as_of_ms)),
    ).fetchall()
    latest_by_ts: dict[int, Any] = {}
    for row in rows:
        latest_by_ts[int(row["timestamp"])] = row
    ordered = [latest_by_ts[key] for key in sorted(latest_by_ts)]
    current = latest_derivatives_as_of(conn, symbol, as_of_ms=as_of_ms) or {}
    funding = [float(row["funding_rate"]) for row in ordered if row["funding_rate"] is not None]
    oi = [float(row["open_interest_usd"]) for row in ordered if row["open_interest_usd"] is not None]
    funding_now = current.get("funding_rate")
    oi_now = current.get("open_interest_usd")
    return {
        "funding_rate": None if funding_now is None else float(funding_now),
        "funding_percentile": None if funding_now is None else _percentile(funding, float(funding_now)),
        "open_interest_usd": None if oi_now is None else float(oi_now),
        "oi_percentile": None if oi_now is None else _percentile(oi, float(oi_now)),
    }


def _book_health(conn: Any, symbol: str, as_of_ms: int) -> dict[str, Any]:
    book = latest_order_book(conn, symbol, as_of_ms=as_of_ms)
    if book is None:
        return {"available": False}
    spread = book.get("bid_ask_spread")
    return {
        "available": True,
        "timestamp": int(book["timestamp"]),
        "spread_bps": None if spread is None else float(spread) * 10_000.0,
        "bid_depth_1pct_usd": book.get("bid_depth_1pct_usd"),
        "ask_depth_1pct_usd": book.get("ask_depth_1pct_usd"),
        "spoof_wall_detected": int(book.get("spoof_wall_detected") or 0),
    }


def _fear_greed(conn: Any, as_of_ms: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT timestamp, fear_greed_index FROM macro_onchain_sentiment
        WHERE timestamp<=? AND fear_greed_index IS NOT NULL
        ORDER BY timestamp DESC LIMIT 1
        """,
        (int(as_of_ms),),
    ).fetchone()
    if row is None:
        return {"role": "context", "value": None}
    return {"role": "context", "value": int(row["fear_greed_index"]), "as_of_ms": int(row["timestamp"])}


def _candle(conn: Any, symbol: str, timeframe: str, ts: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT close, atr_14, rsi_14, ema_20, ema_200 FROM market_technicals
        WHERE symbol=? AND timeframe=? AND timestamp=?
        """,
        (symbol, timeframe, int(ts)),
    ).fetchone()
    return {} if row is None else dict(row)


def _analysis(conn: Any, analysis_id: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT analysis_id, timestamp, symbol, timeframe, regime_state, confidence_score, action
        FROM analysis_output WHERE analysis_id=?
        """,
        (analysis_id,),
    ).fetchone()
    return {} if row is None else dict(row)


def build_veto_brief(
    conn: Any,
    entry: dict[str, Any],
    *,
    digest_text: str = "",
    timeframe: str | None = None,
) -> dict[str, Any]:
    """Inputs for one track open. No secrets and no order or size fields."""
    tf = timeframe or paper_timeframe()
    symbol = str(entry["symbol"]).upper()
    as_of = int(entry["entry_ts"]) + bar_ms(tf)
    analysis = _analysis(conn, str(entry["analysis_id"]))
    candle = _candle(conn, symbol, tf, int(entry["entry_ts"]))
    atr = candle.get("atr_14")
    distance = abs(float(entry["entry_mid"]) - float(entry["invalidation"]))
    safety = evaluate_market_safety(
        book=latest_order_book(conn, symbol, as_of_ms=as_of),
        derivatives=latest_derivatives_as_of(conn, symbol, as_of_ms=as_of),
        macro=None,
        config=load_market_safety_config(skip_macro=True),
        now_ms=as_of,
        intended_side=str(entry["side"]),
    )
    brief = {
        "symbol": symbol,
        "side": str(entry["side"]),
        "timeframe": tf,
        "regime": analysis.get("regime_state"),
        "confidence": analysis.get("confidence_score"),
        "indicators": {
            "atr_14": candle.get("atr_14"),
            "rsi_14": candle.get("rsi_14"),
            "ema_20": candle.get("ema_20"),
            "ema_200": candle.get("ema_200"),
        },
        "invalidation_atr": None if atr in (None, 0) else distance / float(atr),
        "market_safety": safety.as_dict(),
        "derivatives": _derivative_context(conn, symbol, as_of),
        "book_health": _book_health(conn, symbol, as_of),
        "fear_greed": _fear_greed(conn, as_of),
        "digest_excerpt": digest_text[:DIGEST_CHARS],
    }
    return brief


def brief_is_safe(brief: dict[str, Any]) -> bool:
    blob = json.dumps(brief, default=str).lower()
    return not any(word in blob for word in _FORBIDDEN)


def _prompt(brief: dict[str, Any]) -> list[dict[str, str]]:
    keys = ", ".join(sorted(brief))
    return [
        {
            "role": "system",
            "content": (
                "คุณเพิ่มความระวังได้อย่างเดียว ห้ามอนุมัติ ห้ามกำหนดขนาด ห้ามส่งคำสั่ง "
                "ตอบเป็น JSON เท่านั้น คีย์คือ verdict, reason_category, inputs_cited, reason "
                "verdict เป็น veto หรือ no_objection หรือ abstain "
                "reason_category เป็น event_risk, news_contradiction, derivatives_crowding, "
                "book_health, data_stale, doctrine_conflict, หรือ none "
                "inputs_cited เป็นรายการคีย์จาก brief เท่านั้น "
                "reason เป็นภาษาไทยและใช้คำว่า กฎการเทรด "
                "Fear & Greed เป็นบริบท ไม่ใช่สัญญาณ"
            ),
        },
        {
            "role": "user",
            "content": json.dumps({"brief_keys": keys, "brief": brief}, ensure_ascii=False, default=str),
        },
    ]


def parse_verdict(payload: Any, *, brief_keys: set[str]) -> dict[str, Any]:
    """Strict parse. Anything else is abstain."""
    abstain = {
        "verdict": "abstain",
        "reason_category": "none",
        "inputs_cited": [],
        "reason": "อ่านคำตอบไม่ได้ จึงงดให้ความเห็น",
    }
    if not isinstance(payload, dict):
        return abstain
    verdict = payload.get("verdict")
    category = payload.get("reason_category")
    cited = payload.get("inputs_cited")
    reason = payload.get("reason")
    if verdict not in VERDICTS or category not in CATEGORIES:
        return abstain
    if not isinstance(cited, list) or any(
        not isinstance(item, str) or item not in brief_keys for item in cited
    ):
        return abstain
    if not isinstance(reason, str) or not reason.strip():
        return abstain
    return {
        "verdict": verdict,
        "reason_category": category,
        "inputs_cited": cited,
        "reason": reason.strip(),
    }


def _existing_verdicts(conn: Any) -> set[str]:
    rows = conn.execute("SELECT analysis_id FROM paper_shadow_verdicts").fetchall()
    return {str(row["analysis_id"]) for row in rows}


def _insert_verdict(conn: Any, row: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO paper_shadow_verdicts (
            analysis_id, verdict, reason_category, reason_th, inputs_cited_json,
            brief_sha256, digest_name, digest_sha256, model, prompt_version,
            latency_ms, created_at, timing
        ) VALUES (
            :analysis_id, :verdict, :reason_category, :reason_th, :inputs_cited_json,
            :brief_sha256, :digest_name, :digest_sha256, :model, :prompt_version,
            :latency_ms, :created_at, :timing
        )
        """,
        row,
    )


def _late_row(
    entry: dict[str, Any], *, created_at: int, name: str | None, digest_hash: str | None
) -> dict[str, Any]:
    return {
        "analysis_id": entry["analysis_id"],
        "verdict": "abstain",
        "reason_category": "data_stale",
        "reason_th": "คำขอมาช้ากว่าหนึ่งแท่ง จึงไม่นำไปให้คะแนน",
        "inputs_cited_json": "[]",
        "brief_sha256": None,
        "digest_name": name,
        "digest_sha256": digest_hash,
        "model": MODEL,
        "prompt_version": PROMPT_VERSION,
        "latency_ms": 0,
        "created_at": int(created_at),
        "timing": "late",
    }


def request_verdicts(
    conn: Any,
    entries: list[dict[str, Any]],
    *,
    now_ms: int,
    timeframe: str,
    directory: Path,
    chat: Any = chat_json,
    max_calls: int = MAX_CALLS,
) -> dict[str, Any]:
    """Mark late opens without a call. Call the model for at most ``max_calls`` on-time opens."""
    have = _existing_verdicts(conn)
    pending = [row for row in entries if str(row["analysis_id"]) not in have]
    pending.sort(key=lambda row: int(row["entry_ts"]))
    name, digest_hash, text = newest_digest(directory)
    late = 0
    called = 0
    stored = 0
    errors = 0
    on_time_rows: list[dict[str, Any]] = []
    for entry in pending:
        if not on_time(int(entry["entry_ts"]), now_ms, timeframe=timeframe):
            _insert_verdict(conn, _late_row(entry, created_at=now_ms, name=name, digest_hash=digest_hash))
            late += 1
            stored += 1
            continue
        on_time_rows.append(entry)
    for entry in on_time_rows[:max_calls]:
        brief = build_veto_brief(conn, entry, digest_text=text, timeframe=timeframe)
        started = time.monotonic()
        try:
            raw = chat(_prompt(brief), model=MODEL, timeout_s=30.0)
        except OpenRouterError:
            errors += 1
            called += 1
            continue
        called += 1
        parsed = parse_verdict(raw, brief_keys=set(brief))
        latency = int((time.monotonic() - started) * 1000)
        canonical = json.dumps(brief, sort_keys=True, default=str).encode("utf-8")
        _insert_verdict(
            conn,
            {
                "analysis_id": entry["analysis_id"],
                "verdict": parsed["verdict"],
                "reason_category": parsed["reason_category"],
                "reason_th": parsed["reason"],
                "inputs_cited_json": json.dumps(parsed["inputs_cited"], ensure_ascii=False),
                "brief_sha256": _sha256(canonical),
                "digest_name": name,
                "digest_sha256": digest_hash,
                "model": MODEL,
                "prompt_version": PROMPT_VERSION,
                "latency_ms": latency,
                "created_at": int(now_ms),
                "timing": "on_time",
            },
        )
        stored += 1
    conn.commit()
    return {
        "late": late,
        "called": called,
        "stored": stored,
        "errors": errors,
        "waiting": max(0, len(on_time_rows) - max_calls),
    }


def score_records(records: list[dict[str, Any]], *, now_ms: int) -> dict[str, Any]:
    """Pass rule from the pre-registration. Win rate is reported and does not decide."""
    abstain = 0
    late = 0
    scored: list[dict[str, Any]] = []
    for row in records:
        if row.get("timing") == "late":
            late += 1
            continue
        verdict = row.get("verdict")
        if verdict == "abstain":
            abstain += 1
            continue
        if verdict in SCORED and row.get("resolved_at") is not None and row.get("net_r") is not None:
            scored.append(row)
    no_obj = [float(row["net_r"]) for row in scored if row["verdict"] == "no_objection"]
    veto = [float(row["net_r"]) for row in scored if row["verdict"] == "veto"]
    wins = [row for row in scored if float(row["net_r"]) > 0]
    n_no = len(no_obj)
    n_veto = len(veto)
    n = n_no + n_veto
    mean_no = sum(no_obj) / n_no if n_no else None
    mean_veto = sum(veto) / n_veto if n_veto else None
    veto_rate = (n_veto / n) if n else None
    gap_ok = mean_no is not None and mean_veto is not None and mean_veto <= mean_no - MIN_R_GAP
    passed = (
        n_no >= MIN_NO_OBJECTION
        and n_veto >= MIN_VETO
        and mean_no is not None
        and mean_no >= 0
        and gap_ok
        and veto_rate is not None
        and veto_rate <= MAX_VETO_RATE
    )
    first = min((int(row["created_at"]) for row in scored if row.get("created_at") is not None), default=None)
    elapsed_days = None if first is None else (int(now_ms) - first) / 86_400_000
    window_closed = n >= 40 or (elapsed_days is not None and elapsed_days >= WINDOW_DAYS + EXTENSION_DAYS)
    return {
        "ok": True,
        "passed": passed,
        "n_scored": n,
        "n_no_objection": n_no,
        "n_veto": n_veto,
        "n_abstain": abstain,
        "n_late": late,
        "mean_net_r_no_objection": mean_no,
        "mean_net_r_veto": mean_veto,
        "veto_rate": veto_rate,
        "win_rate": (len(wins) / n) if n else None,
        "win_rate_decides": False,
        "pnl_ref_usd_per_r": REF_USD_PER_R,
        "model": MODEL,
        "prompt_version": PROMPT_VERSION,
        "window_start_ms": first,
        "elapsed_days": elapsed_days,
        "window_closed": window_closed,
        "extension_days": EXTENSION_DAYS,
        "confirmatory_window_only": True,
        "paper_only": True,
    }


def _load_score_rows(conn: Any) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT t.net_r, t.resolved_at, v.verdict, v.timing, v.created_at
        FROM paper_shadow_track t
        JOIN paper_shadow_verdicts v ON v.analysis_id = t.analysis_id
        """
    ).fetchall()
    return [dict(row) for row in rows]


def run_shadow(
    conn: Any,
    *,
    now_ms: int | None = None,
    directory: Path | None = None,
    chat: Any = chat_json,
    kill_switch_check: Any | None = None,
) -> dict[str, Any]:
    """Update the track, request verdicts, and leave outcomes on the track rows."""
    if kill_switch_check is not None and kill_switch_check():
        return {"ok": False, "skipped": True, "reason": "kill_switch engaged", "paper_only": True}
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    tf = paper_timeframe()
    rows = build_track(
        conn,
        as_of_ms=now,
        universe_id=paper_universe_id(),
        timeframe=tf,
    )
    synced = sync_track(conn, rows)
    calls = request_verdicts(
        conn,
        rows,
        now_ms=now,
        timeframe=tf,
        directory=directory if directory is not None else digest_dir(),
        chat=chat,
    )
    resolved = sum(1 for row in rows if row.get("resolved_at") is not None)
    return {
        "ok": True,
        "paper_only": True,
        "track_rows": synced,
        "resolved": resolved,
        "model": MODEL,
        "prompt_version": PROMPT_VERSION,
        **calls,
    }


def score_shadow(conn: Any, *, now_ms: int | None = None) -> dict[str, Any]:
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    return score_records(_load_score_rows(conn), now_ms=now)
