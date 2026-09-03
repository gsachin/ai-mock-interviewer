"""Adapter behavior tests: in-memory vector store, in-memory BM25 keyword
store, ChromaDB adapter (EphemeralClient), and the no-op keyword store — all
with real security post-filtering."""
import asyncio

import pytest

from enterprise_rag.model import UpsertRecord
from enterprise_rag.security import SecurityContext
from enterprise_rag.adapters.memory_vector import InMemoryVectorStore
from enterprise_rag.adapters.bm25_memory import BM25KeywordStore
from enterprise_rag.adapters.none_keyword import NoOpKeywordStore

SEC = SecurityContext("u1", "acme", ["interviewer"], [], 3, [])
SEC_SALES = SecurityContext("u1", "acme", ["interviewer"], ["sales"], 3, [])
SEC_OTHER = SecurityContext("u1", "other", ["interviewer"], [], 3, [])
SEC_LOW = SecurityContext("u1", "acme", ["interviewer"], [], 2, [])


def record(cid, content, tenant="acme", clearance=0, department=None, vector=None, parent="p"):
    if vector is None:
        vector = [hash(cid) % 100 / 100.0, 0.5]
    return UpsertRecord(
        chunk_id=cid, parent_id=parent, tenant_id=tenant, content=content,
        section_title="", required_clearance=clearance, department=department,
        vector=vector,
    )


RECORDS = [
    record("a", "leadership rubric for situational pressure", clearance=3,
           department="engineering", vector=[0.99, 0.01]),
    record("b", "team collaboration rubric", clearance=0, department="hr",
           vector=[0.7, 0.3]),
    record("c", "coding standards for java", clearance=0, department="engineering",
           vector=[0.3, 0.7]),
    record("d", "foreign tenant rubric", tenant="other", clearance=0,
           vector=[0.01, 0.99]),
]

QUERY_VEC = [1.0, 0.0]  # cosine ranking: a > b > c > d (clearly separated)


def run(coro):
    return asyncio.run(coro)


# ── InMemoryVectorStore ─────────────────────────────────────────────────

def test_memory_vector_store_search_and_filters():
    store = InMemoryVectorStore()
    run(store.upsert(RECORDS))
    hits = run(store.search(QUERY_VEC, SEC, limit=10))
    assert [c.chunk_id for c in hits] == ["a", "b", "c"]
    # cross-tenant + clearance + department post-filtering:
    # 'a' (clearance 3) is blocked for SEC_LOW; 'b'/'c' (clearance 0) remain.
    assert [c.chunk_id for c in run(store.search(QUERY_VEC, SEC_LOW, 10))] == ["b", "c"]
    assert run(store.search(QUERY_VEC, SEC_SALES, 10)) == []
    other = run(store.search(QUERY_VEC, SEC_OTHER, 10))
    assert [c.chunk_id for c in other] == ["d"]


def test_memory_vector_store_get_by_ids_and_delete():
    store = InMemoryVectorStore()
    run(store.upsert(RECORDS))
    got = run(store.get_by_ids(["a", "d"], "acme"))
    assert [c.chunk_id for c in got] == ["a"]  # 'd' is another tenant
    n = run(store.delete_by_parent("p", "acme"))
    assert n == 3
    assert run(store.get_by_ids(["a"], "acme")) == []


# ── BM25KeywordStore ────────────────────────────────────────────────────

def test_bm25_ranking_and_postfilter():
    store = BM25KeywordStore()
    run(store.upsert(RECORDS))
    hits = run(store.search("rubric leadership", SEC, limit=10))
    assert hits, "BM25 should score rubric terms"
    assert hits[0].chunk_id == "a"  # 'leadership rubric' matches query terms
    assert all(c.tenant_id == "acme" for c in hits)
    # clearance 2 blocks only 'a' (required 3); 'c' has no rubric term
    assert {c.chunk_id for c in run(store.search("rubric", SEC_LOW, 10))} == {"b"}
    # tenant 'other' sees only its own chunk
    assert {c.chunk_id for c in run(store.search("rubric", SEC_OTHER, 10))} == {"d"}
    assert run(store.search("rubric", SEC_SALES, 10)) == []    # no sales-department docs


def test_bm25_zero_score_excluded():
    store = BM25KeywordStore()
    run(store.upsert(RECORDS))
    assert run(store.search("zzzz-not-in-corpus", SEC, 10)) == []


# ── NoOpKeywordStore ────────────────────────────────────────────────────

def test_noop_keyword_store():
    store = NoOpKeywordStore()
    assert run(store.search("anything", SEC, 10)) == []
    run(store.upsert(RECORDS))  # no-op, must not raise


# ── ChromaVectorStore (EphemeralClient) ─────────────────────────────────

@pytest.fixture(scope="module")
def chroma_collection():
    import chromadb

    client = chromadb.EphemeralClient()
    return client.get_or_create_collection("adapter-test")


def test_chroma_vector_store_roundtrip(chroma_collection):
    from enterprise_rag.adapters.chroma_vector import ChromaVectorStore

    store = ChromaVectorStore(chroma_collection)
    run(store.upsert(RECORDS))
    hits = run(store.search(QUERY_VEC, SEC, limit=10))
    assert [c.chunk_id for c in hits] == ["a", "b", "c"]  # cosine: a > b > c

    # clearance filter is enforced via Chroma `where` ('a' requires 3)
    assert [c.chunk_id for c in run(store.search(QUERY_VEC, SEC_LOW, 10))] == ["b", "c"]
    # department filter
    assert run(store.search(QUERY_VEC, SEC_SALES, 10)) == []
    # cross-tenant
    other = run(store.search(QUERY_VEC, SEC_OTHER, 10))
    assert [c.chunk_id for c in other] == ["d"]

    got = run(store.get_by_ids(["a", "d"], "acme"))
    assert [c.chunk_id for c in got] == ["a"]
    n = run(store.delete_by_parent("p", "acme"))
    assert n == 3
