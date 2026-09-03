"""VectorStore.list_tenants(tenant_id): distinct tenant discovery — the
building block for warm-all and backend migrations. Hermetic (memory + tmp
Chroma + pure helper for the Qdrant builder)."""
import asyncio

from enterprise_rag.adapters.chroma_vector import ChromaVectorStore
from enterprise_rag.adapters.memory_vector import InMemoryVectorStore
from enterprise_rag.model import UpsertRecord


def _rec(chunk_id: str, tenant: str) -> UpsertRecord:
    return UpsertRecord(
        chunk_id=chunk_id, parent_id="p", tenant_id=tenant,
        content="c", section_title="s", required_clearance=0,
        department=None, vector=[1.0, 0.0],
    )


def test_memory_list_tenants():
    store = InMemoryVectorStore()
    asyncio.run(store.upsert([
        _rec("a1", "acme"), _rec("a2", "acme"), _rec("b1", "beta"),
    ]))
    assert asyncio.run(store.list_tenants()) == ["acme", "beta"]
    assert asyncio.run(InMemoryVectorStore().list_tenants()) == []


def test_chroma_list_tenants(tmp_path):
    import chromadb

    client = chromadb.PersistentClient(path=str(tmp_path))
    col = client.get_or_create_collection(name="tenants")
    store = ChromaVectorStore(col)
    asyncio.run(store.upsert([
        _rec("a1", "acme"), _rec("b1", "beta"),
    ]))
    assert asyncio.run(store.list_tenants()) == ["acme", "beta"]
    assert asyncio.run(ChromaVectorStore(
        client.get_or_create_collection(name="tenants-empty")).list_tenants()) == []


def test_qdrant_distinct_tenant_helper():
    """The Qdrant implementation scrolls with the live client (out of scope
    without a server); its pure distinct helper is verified directly."""
    from enterprise_rag.adapters.qdrant_vector import _distinct_tenant_ids

    class FakePoint:
        def __init__(self, payload):
            self.payload = payload

    assert _distinct_tenant_ids([
        FakePoint({"tenant_id": "acme"}),
        FakePoint({"tenant_id": "beta"}),
        FakePoint({"tenant_id": "acme"}),
        FakePoint(None),
        FakePoint({}),
    ]) == ["acme", "beta"]
