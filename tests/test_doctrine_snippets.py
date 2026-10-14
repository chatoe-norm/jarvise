from types import SimpleNamespace

import pytest

from jarvise.rag import (
    doctrine_snippets,
    is_preferred_doctrine_payload,
    prefer_doctrine_hits,
)


class _FakeClient:
    def __init__(self, points):
        self.points = points
        self.calls = []

    def query_points(self, *, collection_name, query, limit):
        self.calls.append((collection_name, limit))
        return SimpleNamespace(points=self.points[:limit])


def test_empty_query_returns_empty() -> None:
    assert doctrine_snippets("   ") == []


def test_missing_encoder_deps_fail_soft(monkeypatch) -> None:
    def boom(_text):
        raise ImportError("no sentence_transformers")

    monkeypatch.setattr("jarvise.rag._encode_query", boom)
    assert doctrine_snippets("trend_up long") == []


def test_hits_mapped_and_truncated() -> None:
    points = [
        SimpleNamespace(score=0.9, payload={"text": "x" * 500, "source": "a.md", "kind": "notebook"}),
        SimpleNamespace(
            score=0.5,
            payload={"text": "stops at least 1.5x ATR", "source": None, "kind": "notebook"},
        ),
    ]
    client = _FakeClient(points)
    hits = doctrine_snippets("trend_up long", limit=2, client=client, encoder=lambda _t: [0.1, 0.2])
    # Oversample: min(40, max(2*10, 20)) == 20
    assert client.calls == [("jarvise_doctrine", 20)]
    assert len(hits) == 2
    assert len(hits[0]["text"]) == 240
    assert hits[1] == {"text": "stops at least 1.5x ATR", "source": None, "score": 0.5}


def test_preferred_markers() -> None:
    assert is_preferred_doctrine_payload(
        {"kind": "doctrine", "source": "jarvise-doctrine.txt", "path": "sources/jarvise-doctrine.txt"}
    )
    assert is_preferred_doctrine_payload(
        {
            "kind": "doctrine",
            "source": "jarvise-doctrine-pullback-entry.txt",
            "path": "sources/jarvise-doctrine-pullback-entry.txt",
        }
    )
    assert is_preferred_doctrine_payload(
        {
            "kind": "notebook",
            "source": "jarvise-autonomous-trader-core-protocol-2f474ccb.md",
            "path": "notebook/jarvise-autonomous-trader-core-protocol-2f474ccb.md",
        }
    )
    assert is_preferred_doctrine_payload({"kind": "openclaw", "source": "btc-note.md"})
    assert not is_preferred_doctrine_payload(
        {"kind": "notebook", "source": "what-is-macd-deebae93.md", "path": "notebook/what-is-macd.md"}
    )
    assert not is_preferred_doctrine_payload(
        {"kind": "doctrine", "source": "stack.md", "path": "data/analytics/stack.md"}
    )


def test_prefer_doctrine_hits_ranks_owner_first() -> None:
    hits = [
        {
            "text": "macd divergence",
            "source": "what-is-macd.md",
            "score": 0.9,
            "preferred": False,
        },
        {
            "text": "kill-switch FLAT",
            "source": "jarvise-doctrine.txt",
            "score": 0.4,
            "preferred": True,
        },
        {
            "text": "bollinger squeeze",
            "source": "bollinger.md",
            "score": 0.8,
            "preferred": False,
        },
    ]
    out = prefer_doctrine_hits(hits, 2)
    assert [h["source"] for h in out] == ["jarvise-doctrine.txt", "what-is-macd.md"]
    assert "preferred" not in out[0]


def test_doctrine_snippets_prefers_owner_over_higher_scored_scrapes() -> None:
    points = [
        SimpleNamespace(
            score=0.9,
            payload={"text": "macd scrape", "source": "what-is-macd.md", "kind": "notebook"},
        ),
        SimpleNamespace(
            score=0.85,
            payload={"text": "bb scrape", "source": "bollinger.md", "kind": "notebook"},
        ),
        SimpleNamespace(
            score=0.5,
            payload={
                "text": "owner kill-switch",
                "source": "jarvise-doctrine.txt",
                "kind": "doctrine",
                "path": "sources/jarvise-doctrine.txt",
            },
        ),
    ]
    client = _FakeClient(points)
    hits = doctrine_snippets("q", limit=2, client=client, encoder=lambda _t: [0.0])
    assert [h["source"] for h in hits] == ["jarvise-doctrine.txt", "what-is-macd.md"]


def test_client_error_fail_soft() -> None:
    class Broken:
        def query_points(self, **kwargs):
            raise RuntimeError("qdrant down")

    assert doctrine_snippets("q", client=Broken(), encoder=lambda _t: [0.0]) == []


def test_raise_on_error_propagates() -> None:
    class Broken:
        def query_points(self, **kwargs):
            raise RuntimeError("qdrant down")

    with pytest.raises(RuntimeError):
        doctrine_snippets("q", client=Broken(), encoder=lambda _t: [0.0], raise_on_error=True)
