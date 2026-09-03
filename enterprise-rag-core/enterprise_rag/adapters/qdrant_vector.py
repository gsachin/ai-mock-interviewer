"""Qdrant vector-store adapter (design doc §1/§2, filter builder moved here
from the seed SecurityContext — verified against qdrant-client 1.19.0)."""
from typing import Any

from enterprise_rag.model import Chunk, UpsertRecord
from enterprise_rag.security import SecurityContext

from qdrant_client import AsyncQdrantClient, models as qm


def build_qdrant_filter(sec_ctx: SecurityContext) -> qm.Filter:
    """Mandatory Qdrant payload filter for the dense leg.

    Qdrant ANDs the must/should CLAUSE GROUPS: when `should` is present, at
    least one of its conditions must match. An empty `should` list is therefore
    unsatisfiable — a principal with no departments would get zero results.
    Hence: `should=None` (omitted) when `departments` is empty. (Regression
    tested in tests/test_qdrant_filters.py.)
    """
    must = [
        qm.FieldCondition(key="tenant_id", match=qm.MatchValue(value=sec_ctx.tenant_id)),
        qm.FieldCondition(
            key="required_clearance", range=qm.Range(lte=float(sec_ctx.clearance_level))
        ),
    ]
    should = None
    if sec_ctx.departments:
        should = [
            qm.FieldCondition(key="department", match=qm.MatchValue(value=d))
            for d in sec_ctx.departments
        ]
    return qm.Filter(must=must, should=should)


def _distinct_tenant_ids(points: list[Any]) -> list[str]:
    """Pure helper (unit-tested without a live server): distinct tenant ids
    from scrolled point payloads."""
    return sorted({
        str(payload.get("tenant_id", ""))
        for point in points
        if (payload := (point.payload or {})) and payload.get("tenant_id")
    })


def _payload_to_chunk(point: Any, score: float = 0.0) -> Chunk:
    payload = point.payload or {}
    return Chunk(
        chunk_id=str(point.id),
        parent_id=payload.get("parent_id"),
        tenant_id=payload.get("tenant_id", ""),
        content=payload.get("content", ""),
        score=score,
        section_title=payload.get("section_title", ""),
        required_clearance=int(payload.get("required_clearance", 0)),
        department=payload.get("department"),
    )


class QdrantVectorStore:
    """Dense leg over Qdrant. The client is injected (constructed by the
    caller — see config.build_stack); the adapter owns filter/search mapping."""

    def __init__(self, client: AsyncQdrantClient, *, collection: str):
        self._client = client
        self._collection = collection

    async def search(self, query_vector: list[float],
                     sec_ctx: SecurityContext, limit: int) -> list[Chunk]:
        resp = await self._client.query_points(
            collection_name=self._collection,
            query=query_vector,
            query_filter=build_qdrant_filter(sec_ctx),
            limit=limit,
            with_payload=True,
        )
        return [_payload_to_chunk(p, float(p.score)) for p in resp.points]

    async def get_by_ids(self, ids: list[str], tenant_id: str) -> list[Chunk]:
        resp = await self._client.retrieve(
            collection_name=self._collection,
            ids=ids,
            with_payload=True,
        )
        # Id lookup bypasses query filters — tenant re-verified post-fetch.
        return [
            _payload_to_chunk(p, 1.0)
            for p in resp
            if (p.payload or {}).get("tenant_id") == tenant_id
        ]

    async def upsert(self, records: list[UpsertRecord]) -> None:
        points = [
            qm.PointStruct(
                id=r.chunk_id,
                vector=r.vector,
                payload={
                    "parent_id": r.parent_id,
                    "tenant_id": r.tenant_id,
                    "content": r.content,
                    "section_title": r.section_title,
                    "required_clearance": r.required_clearance,
                    "department": r.department,
                },
            )
            for r in records
        ]
        if points:
            await self._client.upsert(collection_name=self._collection, points=points)

    async def delete_by_parent(self, parent_id: str, tenant_id: str) -> int:
        scroll = await self._client.scroll(
            collection_name=self._collection,
            scroll_filter=qm.Filter(
                must=[
                    qm.FieldCondition(key="parent_id", match=qm.MatchValue(value=parent_id)),
                    qm.FieldCondition(key="tenant_id", match=qm.MatchValue(value=tenant_id)),
                ]
            ),
            with_payload=False,
            limit=10_000,
        )
        ids = [p.id for p in scroll[0]]
        if ids:
            await self._client.delete(collection_name=self._collection, points_selector=ids)
        return len(ids)

    async def get_all(self, tenant_id: str) -> list[Chunk]:
        chunks: list[Chunk] = []
        offset = None
        while True:
            page = await self._client.scroll(
                collection_name=self._collection,
                scroll_filter=qm.Filter(
                    must=[qm.FieldCondition(
                        key="tenant_id", match=qm.MatchValue(value=tenant_id))],
                ),
                with_payload=True,
                with_vectors=False,
                limit=1000,
                offset=offset,
            )
            points, offset = page
            chunks.extend(_payload_to_chunk(p, 0.0) for p in points)
            if offset is None:
                break
        return chunks

    async def list_tenants(self) -> list[str]:
        """Distinct tenant ids across the collection (bulk admin — warm-all,
        migrations). Unfiltered scroll, payload limited to ``tenant_id``."""
        tenants: set[str] = set()
        offset = None
        while True:
            page, offset = await self._client.scroll(
                collection_name=self._collection,
                with_payload=qm.PayloadSelectorInclude(include=["tenant_id"]),
                with_vectors=False,
                limit=1000,
                offset=offset,
            )
            tenants.update(_distinct_tenant_ids(page))
            if offset is None:
                break
        return sorted(tenants)
