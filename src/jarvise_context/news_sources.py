"""Watchlist headlines and existing digests. Research files only.

Failures stay in the report. This module does not write market candles,
the paper ledger, or doctrine.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from jarvise_context.news_reaction import THEME_LABELS, context_lines, summarize_reactions
from jarvise_ingest.db import load_candle_series

GDELT_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
MAX_RECORDS = 75
WINDOW_YEARS = 3

THEME_QUERIES: tuple[tuple[str, str], ...] = (
    ("chips_ai", "NVIDIA OR AMD OR TSMC OR ASML OR OpenAI OR Anthropic"),
    ("geopolitics", "Iran OR China OR India OR Japan OR Europe"),
    ("fed", '"Federal Reserve" OR FOMC'),
    (
        "people",
        '"Elon Musk" OR "Sam Altman" OR "Michael Saylor" OR "Vitalik Buterin"',
    ),
)

_THEME_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("chips_ai", ("nvidia", "amd", "tsmc", "asml", "samsung", "openai", "anthropic", "microsoft", "google", "ai model")),
    ("geopolitics", ("iran", "china", "india", "japan", "europe", "usa", "russia", "hormuz", "washington")),
    ("fed", ("federal reserve", "fomc", "policy rate", "fed", "cpi", "inflation", "bowman", "interest rate")),
    ("people", ("elon musk", "sam altman", "michael saylor", "saylor", "vitalik", "donald trump", "trump")),
)

_DIGEST_NAME = re.compile(r"(\d{4}-\d{2}-\d{2})")
_SEEN_DAY = re.compile(r"(\d{4})(\d{2})(\d{2})")


def default_digest_dir() -> Path:
    raw = (os.environ.get("JARVISE_NEWS_DIGEST_DIR") or "").strip()
    if raw:
        return Path(raw)
    return Path("data/openclaw/workspace-ingest/digests")


def default_report_path() -> Path:
    """Report the web process can read.

    On the VPS the web container mounts only the analytics directory, so when
    ``JARVISE_DB`` is set the file sits beside that database. A local run
    without that variable keeps the research path.
    """
    raw = (os.environ.get("JARVISE_NEWS_REACTION") or "").strip()
    if raw:
        return Path(raw)
    db = (os.environ.get("JARVISE_DB") or "").strip()
    if db:
        return Path(db).expanduser().parent / "news-reaction" / "latest.json"
    return Path("data/research/news-reaction/latest.json")


def news_home_view(report: dict[str, Any] | None) -> dict[str, Any]:
    """Slim context for Home. Never a signal and never an approval."""
    if not report:
        return {
            "ok": True,
            "not_a_trade_signal": True,
            "available": False,
            "lines": ["ยังไม่มีรายงานข่าว"],
            "digest_note": None,
            "errors": [],
            "generated_at_ms": None,
            "window_years": None,
        }
    return {
        "ok": True,
        "not_a_trade_signal": True,
        "available": True,
        "lines": [str(line) for line in (report.get("lines") or []) if str(line).strip()],
        "digest_note": report.get("digest_note"),
        "digest_items": report.get("digest_items") or [],
        "errors": [str(err) for err in (report.get("errors") or [])],
        "generated_at_ms": report.get("generated_at_ms"),
        "window_years": report.get("window_years"),
    }


def read_digests(directory: Path) -> tuple[list[dict[str, Any]], str | None]:
    """Return event days from digest files. An empty folder adds no events."""
    if not directory.is_dir():
        return [], "ยังไม่มีไดเจสต์"
    events: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if not text:
            continue
        match = _DIGEST_NAME.search(path.name)
        if match is None:
            continue
        themes = _themes_in_text(text)
        if not themes:
            themes = ["digest"]
        for theme in themes:
            events.append(
                {
                    "day": match.group(1),
                    "theme": theme,
                    "source": "digest",
                    "title": path.name,
                }
            )
    if not events:
        return [], "ยังไม่มีไดเจสต์"
    return events, None


def fetch_gdelt(
    *,
    now: datetime | None = None,
    client: httpx.Client | None = None,
    years: int = WINDOW_YEARS,
) -> tuple[list[dict[str, Any]], list[str]]:
    """GET a capped headline sample per watchlist theme. One failure does not stop the rest."""
    moment = now or datetime.now(tz=UTC)
    start = (moment - timedelta(days=365 * years)).strftime("%Y%m%d%H%M%S")
    end = moment.strftime("%Y%m%d%H%M%S")
    owns_client = client is None
    http = client or httpx.Client(timeout=20.0)
    events: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        for theme, query in THEME_QUERIES:
            try:
                response = http.get(
                    GDELT_DOC_URL,
                    params={
                        "query": query,
                        "mode": "artlist",
                        "maxrecords": str(MAX_RECORDS),
                        "format": "json",
                        "startdatetime": start,
                        "enddatetime": end,
                        "sort": "datedesc",
                    },
                )
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:  # noqa: BLE001 — one theme must not blank the study
                errors.append(f"{theme}: {type(exc).__name__}: {exc}")
                continue
            for article in payload.get("articles") or []:
                day = _article_day(str(article.get("seendate") or ""))
                if day is None:
                    continue
                events.append(
                    {
                        "day": day,
                        "theme": theme,
                        "source": "gdelt",
                        "title": str(article.get("title") or "")[:180],
                    }
                )
    finally:
        if owns_client:
            http.close()
    return events, errors


def load_daily_candles(conn: Any, symbol: str) -> list[dict[str, Any]]:
    rows = []
    for row in load_candle_series(conn, symbol, "1d"):
        rows.append({"timestamp": int(row["timestamp"]), "close": float(row["close"])})
    return rows


def parse_digest_text(content: str) -> list[dict[str, Any]]:
    """Parse structured news items from an OpenClaw digest markdown file."""
    if not content or not content.strip():
        return []
    blocks = re.split(r"\n(?=\d+\.\s+\*\*)", content)
    items: list[dict[str, Any]] = []
    for b in blocks:
        m = re.match(r"^\d+\.\s+\*\*(.*?)\*\*(?:[^\n]*)?\n(.*)", b.strip(), re.DOTALL)
        if not m:
            continue
        title = m.group(1).strip()
        title = re.sub(r"\s*—\s*\d{4}-\d{2}-\d{2}.*", "", title).strip()
        body = m.group(2).strip()
        url = ""
        comment_lines = []
        for line in body.split("\n"):
            line = line.strip()
            if not line or line.startswith("Not a trade signal"):
                continue
            if line.startswith("http://") or line.startswith("https://"):
                url = line
            else:
                comment_lines.append(line)
        comment = " ".join(comment_lines)
        scores: dict[str, int] = {}
        for theme, words in _THEME_WORDS:
            score = 0
            for w in words:
                if w in title.lower():
                    score += 3
                if w in comment.lower():
                    score += 1
            scores[theme] = score
        best_theme = max(scores, key=scores.get)
        if scores[best_theme] == 0:
            best_theme = "digest"
        items.append({
            "theme": best_theme,
            "title": title,
            "url": url,
            "comment": comment,
        })
    return items


def parse_digest_items_from_dir(directory: Path) -> list[dict[str, Any]]:
    """Parse items from the latest digest markdown file in directory."""
    if not directory.is_dir():
        return []
    md_files = sorted(directory.glob("*.md"))
    if not md_files:
        return []
    latest_file = md_files[-1]
    text = latest_file.read_text(encoding="utf-8", errors="replace")
    return parse_digest_text(text)


def build_report(
    *,
    events: list[dict[str, Any]],
    candles_by_symbol: dict[str, list[dict[str, Any]]],
    errors: list[str],
    digest_note: str | None,
    digest_dir: str,
    now_ms: int,
    digest_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    summary = summarize_reactions(events, candles_by_symbol)
    return {
        "ok": True,
        "paper_only": True,
        "not_a_trade_signal": True,
        "window_years": WINDOW_YEARS,
        "sample_cap_per_theme": MAX_RECORDS,
        "generated_at_ms": now_ms,
        "digest_dir": digest_dir,
        "digest_note": digest_note,
        "digest_items": digest_items or [],
        "errors": errors,
        "theme_labels": THEME_LABELS,
        **summary,
        "lines": context_lines(summary, digest_items=digest_items),
    }


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def load_report(path: Path | None = None) -> dict[str, Any] | None:
    target = path or default_report_path()
    if not target.is_file():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("not_a_trade_signal") is not True:
        return None
    return payload


def run_news_reaction(
    conn: Any | None,
    *,
    digest_dir: Path | None = None,
    report_path: Path | None = None,
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    moment = now or datetime.now(tz=UTC)
    folder = digest_dir if digest_dir is not None else default_digest_dir()
    digest_events, digest_note = read_digests(folder)
    digest_items = parse_digest_items_from_dir(folder)
    gdelt_events, errors = fetch_gdelt(now=moment, client=client)
    candles: dict[str, list[dict[str, Any]]] = {}
    if conn is None:
        errors.append("missing_db")
    else:
        for symbol in ("BTCUSDT", "ETHUSDT"):
            try:
                candles[symbol] = load_daily_candles(conn, symbol)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{symbol}: {type(exc).__name__}: {exc}")
                candles[symbol] = []
    report = build_report(
        events=[*digest_events, *gdelt_events],
        candles_by_symbol=candles,
        errors=errors,
        digest_note=digest_note,
        digest_dir=str(folder),
        now_ms=int(moment.timestamp() * 1000),
        digest_items=digest_items,
    )
    write_report(report_path or default_report_path(), report)
    return report


def _themes_in_text(text: str) -> list[str]:
    lowered = text.lower()
    found = [theme for theme, words in _THEME_WORDS if any(word in lowered for word in words)]
    return found


def _article_day(seendate: str) -> str | None:
    match = _SEEN_DAY.search(seendate)
    if match is None:
        return None
    year, month, day = match.group(1), match.group(2), match.group(3)
    try:
        datetime(int(year), int(month), int(day), tzinfo=UTC)
    except ValueError:
        return None
    return f"{year}-{month}-{day}"
