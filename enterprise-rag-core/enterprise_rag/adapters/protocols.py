"""Backend protocols — the pluggability contract.

Every backend SDK is optional: adapters import their SDK inside their own
module (lazily at class-definition import of that module), so ``import
enterprise_rag`` never requires Qdrant, Elasticsearch, ChromaDB, or Redis.
"""
from typing import Protocol

from enterprise_rag.model import Chunk, UpsertRecord
from enterprise_rag.security import SecurityContext


class EmbeddingClient(Protocol):
    """Async embedder for queries; sync variant for dimension probes."""
    async def embed(self, text: str) -> list[float]: ...

    def embed_sync(self, text: str) -> list[float]: ...


class VectorStore(Protocol):
    """Dense/vector leg."""

    async def search(self, query_vector: list[float],
                     sec_ctx: SecurityContext, limit: int) -> list[Chunk]: ...

    async def get_by_ids(self, ids: list[str], tenant_id: str) -> list[Chunk]:
        """Id lookup WITHOUT query filters — callers re-verify tenant post-fetch
        (design doc §3: never trust ids alone). Backends MUST enforce tenant here."""
        ...

    async def upsert(self, records: list[UpsertRecord]) -> None: ...

    async def delete_by_parent(self, parent_id: str, tenant_id: str) -> int:
        """Delete all chunks belonging to a parent; returns the count removed."""
        ...

    async def get_all(self, tenant_id: str) -> list[Chunk]:
        """All chunks of one tenant (bulk export — BM25 warm-up, prepopulate
        idempotency checks)."""
        ...

    async def list_tenants(self) -> list[str]:
        """Distinct tenant ids present in the store (bulk admin — warm-all,
        migrations)."""
        ...


class KeywordStore(Protocol):
    """Sparse/keyword leg (BM25 or Elasticsearch)."""

    async def search(self, query_text: str,
                     sec_ctx: SecurityContext, limit: int) -> list[Chunk]: ...

    async def upsert(self, records: list[UpsertRecord]) -> None: ...


class SemanticCache(Protocol):
    """Tenant-scoped vector-distance semantic cache."""

    async def get(self, query_vector: list[float], tenant_id: str,
                  schema_version: str) -> dict | None: ...

    async def put(self, query_vector: list[float], payload: dict,
                  tenant_id: str, schema_version: str, *,
                  query_text: str = "") -> None: ...


class Reranker(Protocol):
    def rerank(self, query: str, chunks: list[Chunk]) -> list[Chunk]: ...
