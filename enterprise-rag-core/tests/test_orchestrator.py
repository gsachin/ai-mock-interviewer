"""Orchestrator behavior over stubs: direct-context injection (default ids,
per-request ids, explicit direct_context), cache hit path, rerank + envelope."""
import asyncio

from enterprise_rag.model import Chunk
from enterprise_rag.security import SecurityContext
from enterprise_rag.orchestrator import AgentContextRequest, AtomicAgentContextOrchestrator
from enterprise_rag.cache import InMemorySemanticCache

SEC = SecurityContext("u1", "acme", ["interviewer"], [], 3, [])


class StubVectorStore:
    def __init__(self, chunks: dict[str, Chunk]):
        self._chunks = chunks

    async def get_by_ids(self, ids, tenant_id):
        return [c for cid, c in self._chunks.items() if cid in ids and c.tenant_id == tenant_id]

    async def search(self, *a, **k):
        return []


class StubEngine:
    def __init__(self, retrieved: list[Chunk]):
        self._retrieved = retrieved
        self.queries: list[str] = []

    async def embed_query(self, text: str) -> list[float]:
        return [float(len(text))]

    async def retrieve_parallel(self, query_text, sec_ctx, top_k=5, fetch_k=None,
                                query_vector=None):
        self.queries.append(query_text)
        return self._retrieved


class StubReranker:
    def rerank(self, query, chunks):
        return sorted(chunks, key=lambda c: -c.score)


def run(coro):
    return asyncio.run(coro)


def build(req, *, cache=None, direct_ids=("resume:current", "jd:target")):
    store = StubVectorStore({
        "resume:current": Chunk("resume:current", "resume", "acme", "resume text", 1.0),
        "jd:target": Chunk("jd:target", "jd", "acme", "jd text", 1.0),
        "other:tenant": Chunk("other:tenant", "resume", "evilcorp", "foreign", 1.0),
    })
    engine = StubEngine([
        Chunk("rub1", "rubric", "acme", "leadership rubric", 0.9),
        Chunk("rub2", "rubric", "acme", "teamwork rubric", 0.8),
    ])
    orchestrator = AtomicAgentContextOrchestrator(
        engine, StubReranker(), cache or InMemorySemanticCache(), store,
        direct_chunk_ids=direct_ids,
    )
    return orchestrator, engine


def make_req(**over):
    kwargs = dict(sec_ctx=SEC, resume_text="r", job_description="jd",
                  conversation_history=[], rubric_query="leadership")
    kwargs.update(over)
    return AgentContextRequest(**kwargs)


def test_orchestrator_full_pipeline():
    orchestrator, engine = build(make_req())
    result = run(orchestrator.execute_agent_context(make_req()))
    assert result["status"] == "SUCCESS"
    assert result["hit_source"] == "retrieval"
    ids = [p["chunk_id"] for p in result["provenance"]]
    assert "resume:current" in ids and "jd:target" in ids and "rub1" in ids
    assert "other:tenant" not in ids  # cross-tenant direct chunk excluded
    assert result["context_envelope"].startswith("[context_envelope tenant=acme clearance>=3]")
    assert engine.queries == ["leadership"]


def test_orchestrator_cache_hit():
    cache = InMemorySemanticCache()
    orchestrator, engine = build(make_req(), cache=cache)
    first = run(orchestrator.execute_agent_context(make_req()))
    assert first["hit_source"] == "retrieval"
    second = run(orchestrator.execute_agent_context(make_req()))
    assert second["hit_source"] == "cache"
    assert engine.queries == ["leadership"]  # only one retrieval happened


def test_orchestrator_per_request_direct_ids():
    orchestrator, _engine = build(make_req(), direct_ids=("resume:current",))
    req = make_req(direct_chunk_ids=["resume:current"])
    result = run(orchestrator.execute_agent_context(req))
    ids = [p["chunk_id"] for p in result["provenance"]]
    assert "resume:current" in ids and "jd:target" not in ids


def test_orchestrator_explicit_direct_context():
    orchestrator, _engine = build(make_req(), direct_ids=())
    req = make_req(direct_chunk_ids=[], direct_context={"custom": "explicitly injected"})
    result = run(orchestrator.execute_agent_context(req))
    assert any(p["source"] == "direct" for p in result["provenance"])
    assert "explicitly injected" in result["context_envelope"]
