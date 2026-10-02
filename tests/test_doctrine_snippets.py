from types import SimpleNamespace

from jarvise.rag import doctrine_snippets


class _FakeClient:
    def __init__(self, points):
        self.points = points
        self.calls = []

    def query_points(self, *, collection_name, query, limit):
        self.calls.append((collection_name, limit))
        return SimpleNamespace(points=self.points)


def test_empty_query_returns_empty() -> None:
    assert doctrine_snippets("   ") == []


def test_missing_encoder_deps_fail_soft(monkeypatch) -> None:
    def boom(_text):
        raise ImportError("no sentence_transformers")

    monkeypatch.setattr("jarvise.rag._encode_query", boom)
    assert doctrine_snippets("trend_up long") == []


def test_hits_mapped_and_truncated() -> None:
    points = [
        SimpleNamespace(score=0.9, payload={"text": "x" * 500, "source": "a.md"}),
        SimpleNamespace(score=0.5, payload={"text": "stops at least 1.5x ATR", "source": None}),
    ]
    client = _FakeClient(points)
    hits = doctrine_snippets(
        "trend_up long", limit=2, client=client, encoder=lambda _t: [0.1, 0.2]
    )
    assert client.calls == [("jarvise_doctrine", 2)]
    assert len(hits) == 2
    assert len(hits[0]["text"]) == 240
    assert hits[1] == {"text": "stops at least 1.5x ATR", "source": None, "score": 0.5}


def test_client_error_fail_soft() -> None:
    class Broken:
        def query_points(self, **kwargs):
            raise RuntimeError("qdrant down")

    assert doctrine_snippets("q", client=Broken(), encoder=lambda _t: [0.0]) == []
