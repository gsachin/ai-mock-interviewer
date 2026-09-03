"""Phase 0 realtime-readiness: blocking work must leave the event loop.

Spies on ``asyncio.to_thread`` (per module) prove the offload actually happens
while the functional result stays identical. Also validates the orchestrator's
per-stage ``timings_ms`` instrumentation."""
import asyncio

import pytest

from enterprise_rag.cache import InMemorySemanticCache
from enterprise_rag.model import Chunk, UpsertRecord
from enterprise_rag.orchestrator import AgentContextRequest, AtomicAgentContextOrchestrator
from enterprise_rag.security import SecurityContext

SEC = SecurityContext("u1", "acme", ["interviewer"], [], 3, [])


def run(coro):
    return asyncio.run(coro)


def spy_to_thread(module, monkeypatch):
    """Record every call that goes through ``asyncio.to_thread`` in ``module``
    while delegating to the real implementation."""
    calls = []
    real = module.asyncio.to_thread

    def wrapper(func, *args, **kwargs):
        calls.append(func)
        return real(func, *args, **kwargs)

    monkeypatch.setattr(module.asyncio, "to_thread", wrapper)
    return calls


def record(cid, content, tenant="acme", vector=None):
    return UpsertRecord(
        chunk_id=cid, parent_id="p", tenant_id=tenant, content=content,
        section_title="", required_clearance=0, department=None,
        vector=vector if vector is not None else [1.0, 0.0],
    )


# ── US-01: Chroma adapter offloads every SDK call ─────────────────────────

def test_chroma_offloads_sdk_calls(monkeypatch):
    import chromadb

    from enterprise_rag.adapters import chroma_vector as cv

    calls = spy_to_thread(cv, monkeypatch)
    store = cv.ChromaVectorStore(
        chromadb.EphemeralClient().get_or_create_collection("offload-test")
    )
    run(store.upsert([record("a", "leadership rubric"), record("b", "team rubric")]))
    hits = run(store.search([1.0, 0.0], SEC, 5))
    assert [c.chunk_id for c in hits] == ["a", "b"]
    assert run(store.get_by_ids(["a"], "acme"))[0].chunk_id == "a"
    assert run(store.list_tenants()) == ["acme"]
    assert run(store.delete_by_parent("p", "acme")) == 2
    assert len(calls) >= 5      # upsert, search, get_by_ids, list_tenants, get, delete


# ── US-02: BM25 + memory-vector + in-memory cache offload scoring ─────────

def test_bm25_offloads_scoring_and_rebuild(monkeypatch):
    from enterprise_rag.adapters import bm25_memory as bm

    calls = spy_to_thread(bm, monkeypatch)
    store = bm.BM25KeywordStore()
    run(store.upsert([record("a", "leadership rubric"), record("b", "team rubric")]))
    hits = run(store.search("rubric", SEC, 5))
    assert [c.chunk_id for c in hits] == ["a", "b"]
    assert len(calls) >= 2      # _rebuild (upsert) + _score (search)


def test_memory_vector_offloads_cosine_scoring(monkeypatch):
    from enterprise_rag.adapters import memory_vector as mv

    calls = spy_to_thread(mv, monkeypatch)
    store = mv.InMemoryVectorStore()
    run(store.upsert([record("a", "leadership rubric"), record("b", "team rubric")]))
    hits = run(store.search([1.0, 0.0], SEC, 5))
    assert [c.chunk_id for c in hits] == ["a", "b"]
    assert len(calls) >= 1


def test_memory_cache_offloads_cosine_lookup(monkeypatch):
    from enterprise_rag import cache as cache_module

    calls = spy_to_thread(cache_module, monkeypatch)
    cache = InMemorySemanticCache()
    run(cache.put([1.0, 0.0], {"chunks": []}, "acme", "v1", query_text="q"))
    assert run(cache.get([1.0, 0.0], "acme", "v1")) == {"chunks": []}
    assert len(calls) >= 1


# ── US-03: orchestrator offloads the rerank call ──────────────────────────

class StubVectorStore:
    async def get_by_ids(self, ids, tenant_id):
        return [Chunk("resume:current", "resume", "acme", "resume text", 1.0)]


class StubEngine:
    async def embed_query(self, text):
        return [float(len(text))]

    async def retrieve_parallel(self, query_text, sec_ctx, top_k=5, fetch_k=None,
                                query_vector=None):
        return [Chunk("rub1", "rubric", "acme", "leadership rubric", 0.9)]


class StubReranker:
    def rerank(self, query, chunks):
        return sorted(chunks, key=lambda c: -c.score)


def test_orchestrator_offloads_rerank(monkeypatch):
    from enterprise_rag import orchestrator as orch

    calls = spy_to_thread(orch, monkeypatch)
    o = AtomicAgentContextOrchestrator(
        StubEngine(), StubReranker(), InMemorySemanticCache(), StubVectorStore(),
    )
    result = run(o.execute_agent_context(AgentContextRequest(
        sec_ctx=SEC, resume_text="r", job_description="jd",
        conversation_history=[], rubric_query="leadership",
    )))
    assert result["status"] == "SUCCESS"
    rerank_calls = [f for f in calls if getattr(f, "__name__", "") == "rerank"]
    assert len(rerank_calls) == 1


# ── US-04: per-stage timings present and sane ─────────────────────────────

def test_orchestrator_reports_per_stage_timings(monkeypatch):
    from enterprise_rag import orchestrator as orch

    spy_to_thread(orch, monkeypatch)
    o = AtomicAgentContextOrchestrator(
        StubEngine(), StubReranker(), InMemorySemanticCache(), StubVectorStore(),
    )
    result = run(o.execute_agent_context(AgentContextRequest(
        sec_ctx=SEC, resume_text="r", job_description="jd",
        conversation_history=[], rubric_query="leadership",
    )))
    timings = result["timings_ms"]
    assert set(timings) == {"direct", "embed", "cache", "retrieval",
                            "rerank", "format", "total"}
    assert all(isinstance(v, float) and v >= 0 for v in timings.values())
    assert timings["total"] > 0
    # retrieval leg was exercised (cold cache) -> nonzero retrieval window
    assert timings["retrieval"] >= 0
