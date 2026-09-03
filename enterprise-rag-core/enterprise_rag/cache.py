"""§5 Production Vector Semantic Cache — RedisVL implementation (verified
against redisvl 0.26.0) plus multi-tenant, in-memory, and no-op variants so
any consuming system can pick its cache backend.

The redisvl imports are lazy (inside ``EnterpriseSemanticCache.__init__``) so
the package core imports without the ``redisvl`` extra installed — behavior is
otherwise unchanged from the verified seed code.

Realtime-readiness (Phase 0): the in-memory variant's cosine lookup is
CPU-bound, so it runs in a worker thread via ``asyncio.to_thread``.
"""
import asyncio
import json
from typing import Any, Callable


class EnterpriseSemanticCache:
    """RedisVL SemanticCache (verified against redisvl 0.26.0).

    Per-(tenant, schema) index name as defense in depth, on top of the tag
    filters enforced on every lookup. Requires Redis Stack (RediSearch +
    RedisJSON modules); plain Redis OSS cannot run this design.
    """

    def __init__(self, redis_url: str, *, tenant_id: str,
                 schema_version: str = "v1",
                 embedding_fn: Callable[[str], list[float]] | None = None,  # must be supplied — see note
                 distance_threshold: float = 0.04,   # COSINE distance => similarity >= 0.96
                 ttl: int = 86400):
        from redisvl.extensions.cache.llm import SemanticCache   # current import path (redisvl 0.26.0)
        from redisvl.query.filter import Tag
        from redisvl.utils.vectorize import CustomVectorizer

        self._Tag = Tag
        self._cache = SemanticCache(
            name=f"rag:sc:{tenant_id}:{schema_version}",
            redis_url=redis_url,
            distance_threshold=distance_threshold,
            ttl=ttl,
            # Index dimensions come from OUR embedder. The default vectorizer
            # (HFTextVectorizer) would require sentence-transformers + torch at
            # construction — rejected for this latency-budget design.
            vectorizer=CustomVectorizer(embed=embedding_fn),
            filterable_fields=[
                {"name": "tenant_id", "type": "tag"},
                {"name": "schema_version", "type": "tag"},
            ],
        )

    async def get(self, query_vector: list[float], tenant_id: str,
                  schema_version: str) -> dict[str, Any] | None:
        # target: <= 4.2 ms p95 (embedding done upstream, per TEST-PERF-02)
        hits = await self._cache.acheck(
            vector=query_vector, num_results=1,
            filter_expression=(self._Tag("tenant_id") == tenant_id)
                            & (self._Tag("schema_version") == schema_version),
        )
        if hits:
            return json.loads(hits[0]["response"])
        return None

    async def put(self, query_vector: list[float], payload: dict[str, Any],
                  tenant_id: str, schema_version: str, *, query_text: str = "") -> None:
        await self._cache.astore(
            prompt=query_text,
            response=json.dumps(payload),
            vector=query_vector,
            filters={"tenant_id": tenant_id, "schema_version": schema_version},
        )


class MultiTenantSemanticCache:
    """SemanticCache facade for RedisVL: lazily constructs one
    EnterpriseSemanticCache per (tenant, schema) on first access.

    Resolves the single-tenant-at-construction wrinkle of the seed design
    while keeping its defense-in-depth property (per-tenant index + tag
    filters on every lookup). The one-time construction cost per key (the
    vectorizer dimension probe) is acceptable — request paths always pass
    ``vector=`` explicitly, so no per-call embedding happens inside the cache.
    """

    def __init__(self, redis_url: str, *, embedding_fn: Callable[[str], list[float]] | None = None,
                 distance_threshold: float = 0.04, ttl: int = 86400):
        self._redis_url = redis_url
        self._embedding_fn = embedding_fn
        self._distance_threshold = distance_threshold
        self._ttl = ttl
        self._caches: dict[tuple[str, str], EnterpriseSemanticCache] = {}

    def _for(self, tenant_id: str, schema_version: str) -> EnterpriseSemanticCache:
        key = (tenant_id, schema_version)
        if key not in self._caches:
            self._caches[key] = EnterpriseSemanticCache(
                self._redis_url, tenant_id=tenant_id, schema_version=schema_version,
                embedding_fn=self._embedding_fn,
                distance_threshold=self._distance_threshold, ttl=self._ttl,
            )
        return self._caches[key]

    async def get(self, query_vector: list[float], tenant_id: str,
                  schema_version: str) -> dict[str, Any] | None:
        return await self._for(tenant_id, schema_version).get(
            query_vector, tenant_id, schema_version)

    async def put(self, query_vector: list[float], payload: dict[str, Any],
                  tenant_id: str, schema_version: str, *, query_text: str = "") -> None:
        await self._for(tenant_id, schema_version).put(
            query_vector, payload, tenant_id, schema_version, query_text=query_text)


class InMemorySemanticCache:
    """True vector-similarity cache without any infra: normalized vectors with
    a cosine-distance threshold (0.04 => similarity >= 0.96, same as RedisVL)."""

    def __init__(self, distance_threshold: float = 0.04, ttl: int = 86400):
        self._threshold = distance_threshold
        self._ttl = ttl
        self._entries: dict[tuple[str, str], list[dict[str, Any]]] = {}

    def _cosine_distance(self, a: list[float], b: list[float]) -> float:
        import numpy as np

        va = np.asarray(a, dtype=np.float32)
        vb = np.asarray(b, dtype=np.float32)
        na, nb = float(np.linalg.norm(va)), float(np.linalg.norm(vb))
        if na == 0 or nb == 0:
            return 1.0
        return 1.0 - float(np.dot(va, vb) / (na * nb))

    async def get(self, query_vector: list[float], tenant_id: str,
                  schema_version: str) -> dict[str, Any] | None:
        import time

        key = (tenant_id, schema_version)
        now = time.monotonic()

        def _lookup() -> tuple[dict[str, Any] | None, float | None]:
            best, best_dist = None, None
            for entry in self._entries.get(key, []):
                if now - entry["ts"] > self._ttl:
                    continue
                dist = self._cosine_distance(query_vector, entry["vector"])
                if best_dist is None or dist < best_dist:
                    best, best_dist = entry, dist
            return best, best_dist

        best, best_dist = await asyncio.to_thread(_lookup)
        if best is not None and best_dist <= self._threshold:
            return best["payload"]
        return None

    async def put(self, query_vector: list[float], payload: dict[str, Any],
                  tenant_id: str, schema_version: str, *, query_text: str = "") -> None:
        import time

        key = (tenant_id, schema_version)
        self._entries.setdefault(key, []).append(
            {"vector": query_vector, "payload": payload, "ts": time.monotonic()}
        )


class NoOpSemanticCache:
    """Cache disabled: get always misses, put is a no-op."""

    async def get(self, query_vector: list[float], tenant_id: str,
                  schema_version: str) -> dict[str, Any] | None:
        return None

    async def put(self, query_vector: list[float], payload: dict[str, Any],
                  tenant_id: str, schema_version: str, *, query_text: str = "") -> None:
        return None
