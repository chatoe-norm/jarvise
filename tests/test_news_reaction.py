"""News-to-chart context: closed daily returns only, never a trade signal."""

from datetime import UTC, datetime

import httpx

from jarvise_context.news_reaction import summarize_reactions
from jarvise_context.news_sources import fetch_gdelt, read_digests


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

