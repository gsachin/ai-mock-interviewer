"""Hybrid engine composition tests: dense+BM25 over in-memory stores (no
infra), dense-only when the keyword leg is absent, and fusion semantics.
Also: the OpenAI-compatible embeddings client (the MLX backend) request
shape — hermetic, no live server."""
import asyncio

import httpx
import pytest

from enterprise_rag.model import Chunk, UpsertRecord
from enterprise_rag.security import SecurityContext
from enterprise_rag.hybrid import (
    AsyncParallelHybridEngine,
    OpenAICompatibleEmbeddingClient,
    fuse_wrrf,
    _wrrf,
)
from enterprise_rag.adapters.memory_vector import InMemoryVectorStore
from enterprise_rag.adapters.bm25_memory import BM25KeywordStore

SEC = SecurityContext("u1", "acme", ["interviewer"], [], 3, [])


class FakeEmbedder:
    def __init__(self, mapping: dict[str, list[float]]):
        self._mapping = mapping
        self.calls: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return self._mapping[text]

    def embed_sync(self, text: str) -> list[float]:
        return self._mapping[text]


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def engine():
    vector = InMemoryVectorStore()
    bm25 = BM25KeywordStore()
    embedder = FakeEmbedder({"rubric query": [1.0, 0.0]})
    recs = [
        UpsertRecord("a", "p", "acme", "leadership rubric", "", 0, "eng", [1.0, 0.0]),
        UpsertRecord("b", "p", "acme", "collaboration rubric", "", 0, "eng", [0.9, 0.1]),
        UpsertRecord("c", "p", "acme", "rubric coding standards", "", 0, "eng", [0.5, 0.5]),
    ]
    run(vector.upsert(recs))
    run(bm25.upsert(recs))
    return AsyncParallelHybridEngine(vector, bm25, embedder, alpha=0.3)


def test_engine_retrieves_with_two_legs(engine):
    chunks = run(engine.retrieve_parallel("rubric query", SEC, top_k=3))
    assert {c.chunk_id for c in chunks} == {"a", "b", "c"}
    # 'a' is rank-0 on both legs -> highest fused score
    assert chunks[0].chunk_id == "a"


def test_engine_precomputed_vector_skips_embedding(engine):
    engine._embeddings.calls.clear()
    run(engine.retrieve_parallel("rubric query", SEC, top_k=3, query_vector=[1.0, 0.0]))
    assert engine._embeddings.calls == []


def test_engine_dense_only_when_no_keyword_leg(engine):
    dense_only = AsyncParallelHybridEngine(
        engine._dense, None, engine._embeddings, alpha=0.3,
    )
    chunks = run(dense_only.retrieve_parallel("rubric query", SEC, top_k=3))
    assert [c.chunk_id for c in chunks] == ["a", "b", "c"]  # dense order preserved


def test_fuse_wrrf_semantics():
    c1 = Chunk("c1", "p1", "acme", "a")
    c2 = Chunk("c2", "p2", "acme", "b")
    c3 = Chunk("c3", "p3", "acme", "c")
    merged = fuse_wrrf([c1, c2], [c2, c3], alpha=0.3)
    assert merged[0].chunk_id == "c2"
    assert abs(merged[0].score - (0.7 / 60 + 0.3 / 61)) < 1e-9
    assert abs(_wrrf(0) - 1 / 60) < 1e-12
    single = fuse_wrrf([c1], [c3], alpha=0.5)
    assert len(single) == 2 and all(c.score > 0 for c in single)


# ── OpenAI-compatible embeddings client (MLX backend) ─────────────────────

class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_openai_client_embed_request_shape(monkeypatch):
    captured = {}

    async def fake_post(self, url, json=None, **kwargs):
        captured["url"] = url
        captured["json"] = json
        return _FakeResponse({"data": [{"embedding": [0.1, 0.2, 0.3]}]})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    client = OpenAICompatibleEmbeddingClient("http://127.0.0.1:8000/v1/", "bge-small")
    vec = asyncio.run(client.embed("hello"))
    assert vec == [0.1, 0.2, 0.3]
    assert captured["url"] == "http://127.0.0.1:8000/v1/embeddings"   # trailing slash normalized
    assert captured["json"] == {"model": "bge-small", "input": "hello"}


def test_openai_client_embed_sync_request_shape(monkeypatch):
    captured = {}

    def fake_post(self, url, json=None, **kwargs):
        captured["url"] = url
        captured["json"] = json
        return _FakeResponse({"data": [{"embedding": [1.0]}]})

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    client = OpenAICompatibleEmbeddingClient("http://127.0.0.1:8000/v1", "bge-small")
    vec = client.embed_sync("probe")
    assert vec == [1.0]
    assert captured["json"] == {"model": "bge-small", "input": "probe"}
