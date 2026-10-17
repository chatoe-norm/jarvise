"""News-to-chart context: closed daily returns only, never a trade signal."""

from datetime import UTC, datetime

import httpx

from jarvise_context.news_reaction import summarize_reactions
from jarvise_context.news_sources import (
    GDELT_RATE_LIMIT_BACKOFF_S,
    GDELT_RATE_LIMIT_RETRIES,
    GDELT_REQUEST_GAP_S,
    fetch_gdelt,
    fetch_headlines,
    read_digests,
    run_news_reaction,
)


def _ms(day: str) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp() * 1000)


def _candles(pairs: list[tuple[str, float]]) -> list[dict]:
    return [{"timestamp": _ms(day), "close": close} for day, close in pairs]


def test_same_day_two_themes_count_once() -> None:
    candles = _candles(
        [
            ("2024-01-01", 100.0),
            ("2024-01-02", 110.0),
            ("2024-01-03", 100.0),
            ("2024-01-04", 100.0),
            ("2024-01-05", 100.0),
            ("2024-01-06", 80.0),
        ]
    )
    events = [
        {"day": "2024-01-01", "theme": "fed"},
        {"day": "2024-01-01", "theme": "chips_ai"},
    ]
    summary = summarize_reactions(events, {"BTCUSDT": candles, "ETHUSDT": candles})
    assert summary["not_a_trade_signal"] is True
    assert summary["event_days"] == 1
    btc = summary["combined"]["symbols"]["BTCUSDT"]["horizon_1d"]
    assert btc["n"] == 1
    assert btc["median_pct"] == 10.0
    assert summary["combined"]["symbols"]["BTCUSDT"]["horizon_5d"]["median_pct"] == -20.0
    assert {row["theme"] for row in summary["themes"]} == {"fed", "chips_ai"}


def test_next_bar_missing_is_not_measurable() -> None:
    candles = _candles([("2024-01-01", 100.0)])
    summary = summarize_reactions(
        [{"day": "2024-01-01", "theme": "fed"}],
        {"BTCUSDT": candles, "ETHUSDT": []},
    )
    horizon = summary["combined"]["symbols"]["BTCUSDT"]["horizon_1d"]
    assert horizon["n"] == 0
    assert horizon["median_pct"] is None
    assert horizon["pending"] == 1
    eth = summary["combined"]["symbols"]["ETHUSDT"]["horizon_1d"]
    assert eth["n"] == 0
    assert eth["missing_bar"] == 1


def test_empty_digest_creates_no_events(tmp_path) -> None:
    empty_dir = tmp_path / "digests"
    empty_dir.mkdir()
    events, note = read_digests(empty_dir)
    assert events == []
    assert note == "ยังไม่มีไดเจสต์"
    blank = empty_dir / "2024-01-01-07.md"
    blank.write_text("   \n", encoding="utf-8")
    events, note = read_digests(empty_dir)
    assert events == []
    assert note == "ยังไม่มีไดเจสต์"


def test_gdelt_failure_keeps_other_themes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params["query"]
        if "FOMC" in query:
            return httpx.Response(500, text="down")
        return httpx.Response(
            200,
            json={"articles": [{"seendate": "20240102T120000Z", "title": "NVIDIA ships"}]},
        )

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        events, errors = fetch_gdelt(
            now=datetime(2024, 6, 1, tzinfo=UTC),
            client=client,
            sleep=lambda _s: None,
        )
    assert any(err.startswith("fed:") for err in errors)
    assert {event["theme"] for event in events} == {"chips_ai", "geopolitics", "people"}
    assert {event["day"] for event in events} == {"2024-01-02"}


def test_parse_digest_text_and_format_executive_lines() -> None:
    from jarvise_context.news_reaction import context_lines
    from jarvise_context.news_sources import parse_digest_text

    sample_md = """# Digest
1. **NVIDIA — $250M UK compute donation** — 2026-10-14
   https://nvidia.com/news
   Primary release: UK AI research safety spend. Frontier-lab capex tracks crypto risk appetite.

2. **US dollar soft as focus turns to CPI and Fed speakers** — 2026-10-14
   https://cnbc.com/news
   CNBC: markets await CPI. Rate-path data is the macro backdrop.
"""
    items = parse_digest_text(sample_md)
    assert len(items) == 2
    assert items[0]["theme"] == "chips_ai"
    assert "NVIDIA" in items[0]["title"]
    assert items[1]["theme"] == "fed"

    summary = {
        "combined": {"label": "รวมทุกธีม", "event_days": 1},
        "themes": [
            {
                "theme": "chips_ai",
                "label": "ชิปและ AI",
                "event_days": 1,
                "symbols": {
                    "BTCUSDT": {"horizon_1d": {"n": 1, "median_pct": 0.69}},
                },
            }
        ],
    }
    lines = context_lines(summary, digest_items=items)
    assert lines[0] == "บริบทข่าว ไม่ใช่สัญญาณ"
    assert any("ชิปและ AI" in line and "→" in line for line in lines)
    assert any("ธนาคารกลางสหรัฐ" in line and "→" in line for line in lines)


def _article_response() -> httpx.Response:
    return httpx.Response(
        200, json={"articles": [{"seendate": "20261016T120000Z", "title": "Fed holds rates"}]}
    )


def test_gdelt_queries_are_parenthesized_and_paced() -> None:
    """GDELT rejects bare OR queries and allows one request every 5 s."""
    seen: list[str] = []
    waits: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params["query"])
        return _article_response()

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        events, errors = fetch_gdelt(
            now=datetime(2026, 10, 17, tzinfo=UTC), client=client, sleep=waits.append
        )
    assert errors == []
    assert len(seen) == 4
    assert all(q.startswith("(") and q.endswith(")") for q in seen)
    assert waits == [GDELT_REQUEST_GAP_S] * 3
    assert {e["theme"] for e in events} == {"chips_ai", "geopolitics", "fed", "people"}


def test_gdelt_rate_limit_is_retried_then_succeeds() -> None:
    calls = {"n": 0}
    waits: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text="Please limit requests to one every 5 seconds")
        return _article_response()

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        events, errors = fetch_gdelt(
            now=datetime(2026, 10, 17, tzinfo=UTC), client=client, sleep=waits.append
        )
    assert errors == []
    assert GDELT_RATE_LIMIT_BACKOFF_S in waits
    assert len(events) == 4


def test_gdelt_plain_text_and_persistent_rate_limit_are_clear_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params["query"]
        if "FOMC" in query:
            # What GDELT returns for a malformed query: HTTP 200, plain text.
            return httpx.Response(200, text="Queries containing OR'd terms must be surrounded by ().")
        if "Musk" in query:
            return httpx.Response(200, text="Please limit requests to one every 5 seconds or contact ...")
        return _article_response()

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        events, errors = fetch_gdelt(
            now=datetime(2026, 10, 17, tzinfo=UTC), client=client, sleep=lambda _s: None
        )
    by_theme = {e.split(":", 1)[0]: e for e in errors}
    assert "non-JSON reply: Queries containing OR'd terms" in by_theme["fed"]
    assert "rate limited" in by_theme["people"]
    assert "JSONDecodeError" not in " ".join(errors)
    assert {e["theme"] for e in events} == {"chips_ai", "geopolitics"}


_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>q</title>
<item><title>Fed signals patience - Reuters</title><pubDate>Fri, 16 Oct 2026 14:05:00 GMT</pubDate></item>
<item><title>No date item</title></item>
<item><title>FOMC minutes - CNBC</title><pubDate>Thu, 15 Oct 2026 23:30:00 GMT</pubDate></item>
</channel></rss>"""


def _host(request: httpx.Request) -> str:
    return request.url.host


def test_headlines_fall_back_to_google_news_when_gdelt_is_rate_limited() -> None:
    gdelt_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if _host(request) == "api.gdeltproject.org":
            gdelt_calls["n"] += 1
            return httpx.Response(429, text="Please limit requests to one every 5 seconds")
        return httpx.Response(200, text=_RSS, headers={"content-type": "application/xml"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        events, errors, sources = fetch_headlines(
            now=datetime(2026, 10, 17, tzinfo=UTC), client=client, sleep=lambda _s: None
        )
    assert errors == []
    assert sources == {t: "google_news" for t in ("chips_ai", "geopolitics", "fed", "people")}
    # GDELT tried once for the first theme (plus its retries), then skipped for the rest.
    assert gdelt_calls["n"] == 1 + GDELT_RATE_LIMIT_RETRIES
    days = {e["day"] for e in events}
    assert days == {"2026-10-16", "2026-10-15"}
    assert {e["source"] for e in events} == {"google_news"}


def test_headlines_prefer_gdelt_when_it_answers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert _host(request) == "api.gdeltproject.org", "fallback must not run when GDELT works"
        return _article_response()

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        _events, errors, sources = fetch_headlines(
            now=datetime(2026, 10, 17, tzinfo=UTC), client=client, sleep=lambda _s: None
        )
    assert errors == []
    assert set(sources.values()) == {"gdelt"}


def test_headlines_error_only_when_every_source_fails() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if _host(request) == "api.gdeltproject.org":
            return httpx.Response(200, text="Queries containing OR'd terms must be surrounded by ().")
        return httpx.Response(503, text="unavailable")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        events, errors, sources = fetch_headlines(
            now=datetime(2026, 10, 17, tzinfo=UTC), client=client, sleep=lambda _s: None
        )
    assert events == [] and sources == {}
    assert len(errors) == 4
    assert all("gdelt GdeltError: non-JSON reply" in e and "google_news HTTPStatusError" in e for e in errors)


def test_run_news_reaction_records_headline_sources(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if _host(request) == "api.gdeltproject.org":
            return httpx.Response(429, text="Please limit requests to one every 5 seconds")
        return httpx.Response(200, text=_RSS)

    report_path = tmp_path / "latest.json"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = run_news_reaction(
            None,
            digest_dir=tmp_path / "digests",
            report_path=report_path,
            client=client,
            now=datetime(2026, 10, 17, tzinfo=UTC),
            sleep=lambda _s: None,
        )
    assert report["headline_sources"]["fed"] == "google_news"
    # Only the missing local DB is an error; no headline theme failed.
    assert report["errors"] == ["missing_db"]
    assert report_path.exists()
