"""VectorStore.get_all(tenant_id): bulk per-tenant export — the building block
for BM25 warm-up and prepopulate idempotency. Hermetic (tmp Chroma + memory)."""
import asyncio

import pytest

from enterprise_rag.adapters.chroma_vector import ChromaVectorStore
from enterprise_rag.adapters.memory_vector import InMemoryVectorStore
from enterprise_rag.model import UpsertRecord


def _rec(chunk_id: str, tenant: str, content: str) -> UpsertRecord:
    return UpsertRecord(
        chunk_id=chunk_id, parent_id="p", tenant_id=tenant,
        content=content, section_title="s", required_clearance=0,
        department=None, vector=[1.0, 0.0],
    )


RECORDS = [
    _rec("a1", "acme", "hello"),
    _rec("a2", "acme", "world"),
    _rec("o1", "other", "secret"),
]


def test_memory_get_all_tenant_filter():
    store = InMemoryVectorStore()
    asyncio.run(store.upsert(RECORDS))
    chunks = asyncio.run(store.get_all("acme"))
    assert {c.chunk_id for c in chunks} == {"a1", "a2"}
    assert all(c.tenant_id == "acme" for c in chunks)
    assert asyncio.run(store.get_all("nobody")) == []


def test_chroma_get_all_tenant_filter(tmp_path):
    import chromadb

    client = chromadb.PersistentClient(path=str(tmp_path))
    col = client.get_or_create_collection(name="test")
    store = ChromaVectorStore(col)
    asyncio.run(store.upsert(RECORDS))

    chunks = asyncio.run(store.get_all("acme"))
    assert {c.chunk_id for c in chunks} == {"a1", "a2"}
    assert all(c.tenant_id == "acme" for c in chunks)
    a1 = next(c for c in chunks if c.chunk_id == "a1")
    assert a1.content == "hello"
    assert a1.section_title == "s"
    assert a1.parent_id == "p"
    assert asyncio.run(store.get_all("nobody")) == []


def test_chroma_get_all_empty_store(tmp_path):
    import chromadb

    client = chromadb.PersistentClient(path=str(tmp_path))
    col = client.get_or_create_collection(name="test")
    assert asyncio.run(ChromaVectorStore(col).get_all("acme")) == []
