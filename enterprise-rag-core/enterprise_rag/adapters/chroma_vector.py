"""ChromaDB vector-store adapter — lets the engine run against an existing
ChromaDB collection (e.g. the universityDemo admissions knowledge base) with
the same SecurityContext semantics as the Qdrant adapter.

Realtime-readiness (Phase 0): the Chroma SDK is synchronous, so every SDK call
is offloaded from the event loop via ``asyncio.to_thread`` — a blocking query
would stall every concurrent session in a voice server."""
import asyncio
from typing import Any

from enterprise_rag.model import Chunk, UpsertRecord
from enterprise_rag.security import SecurityContext


def build_chroma_where(sec_ctx: SecurityContext) -> dict[str, Any]:
    """SecurityContext as a ChromaDB ``where`` metadata filter.

    Same semantics as build_qdrant_filter: tenant equality + clearance ``lte``
    always; department membership only when non-empty (ChromaDB's
    ``$in: []`` matches nothing, so the clause is omitted — deny-by-default
    parity with the omit-when-empty guard of the other builders).
    """
    clauses: list[dict[str, Any]] = [
        {"tenant_id": {"$eq": sec_ctx.tenant_id}},
        {"required_clearance": {"$lte": sec_ctx.clearance_level}},
    ]
    if sec_ctx.departments:
        clauses.append({"department": {"$in": list(sec_ctx.departments)}})
    return {"$and": clauses}


def _to_chunk(chunk_id: str, document: str, metadata: dict, score: float = 0.0) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        parent_id=metadata.get("parent_id"),
        tenant_id=str(metadata.get("tenant_id", "")),
        content=document,
        score=score,
        section_title=str(metadata.get("section_title", "")),
        required_clearance=int(metadata.get("required_clearance", 0)),
        department=metadata.get("department"),
    )


class ChromaVectorStore:
    """Dense leg over a ChromaDB collection.

    The collection is injected (see config.build_stack) — the adapter makes no
    client/collection lifecycle decisions. Query vectors are passed explicitly,
    so the collection's own embedding function (if any) is only used elsewhere.
    """

    def __init__(self, collection, *, tenant_field: str = "tenant_id"):
        self._collection = collection
        self._tenant_field = tenant_field

    async def search(self, query_vector: list[float],
                     sec_ctx: SecurityContext, limit: int) -> list[Chunk]:
        res = await asyncio.to_thread(
            self._collection.query,
            query_embeddings=[query_vector],
            n_results=limit,
            where=build_chroma_where(sec_ctx),
            include=["documents", "metadatas", "distances"],
        )
        ids = (res.get("ids") or [[]])[0]
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        out = []
        for i, chunk_id in enumerate(ids):
            meta = (metas[i] or {}) if i < len(metas) else {}
            distance = dists[i] if i < len(dists) and dists[i] is not None else 0.0
            out.append(_to_chunk(chunk_id, docs[i] if i < len(docs) else "", meta,
                                 1.0 - float(distance)))  # cosine distance -> similarity
        return sorted(out, key=lambda c: -c.score)

    async def get_by_ids(self, ids: list[str], tenant_id: str) -> list[Chunk]:
        res = await asyncio.to_thread(
            self._collection.get, ids=ids, include=["documents", "metadatas"],
        )
        out = []
        for i, chunk_id in enumerate(res.get("ids") or []):
            meta = (res.get("metadatas") or [])[i] or {}
            if str(meta.get(self._tenant_field, "")) != tenant_id:
                continue  # id lookup bypasses filters — tenant re-verified here
            doc = (res.get("documents") or [])[i] or ""
            out.append(_to_chunk(chunk_id, doc, meta, 1.0))
        return out

    async def upsert(self, records: list[UpsertRecord]) -> None:
        if not records:
            return
        await asyncio.to_thread(
            self._collection.upsert,
            ids=[r.chunk_id for r in records],
            documents=[r.content for r in records],
            metadatas=[
                {
                    "parent_id": r.parent_id,
                    "tenant_id": r.tenant_id,
                    "section_title": r.section_title,
                    "required_clearance": r.required_clearance,
                    "department": r.department,
                }
                for r in records
            ],
            embeddings=[r.vector for r in records],
        )

    async def delete_by_parent(self, parent_id: str, tenant_id: str) -> int:
        res = await asyncio.to_thread(
            self._collection.get,
            where={"$and": [
                {"parent_id": {"$eq": parent_id}},
                {"tenant_id": {"$eq": tenant_id}},
            ]},
            include=[],
        )
        ids = res.get("ids") or []
        if ids:
            await asyncio.to_thread(self._collection.delete, ids=ids)
        return len(ids)

    async def get_all(self, tenant_id: str) -> list[Chunk]:
        res = await asyncio.to_thread(
            self._collection.get,
            where={self._tenant_field: {"$eq": tenant_id}},
            include=["documents", "metadatas"],
        )
        out = []
        for i, chunk_id in enumerate(res.get("ids") or []):
            meta = (res.get("metadatas") or [])[i] or {}
            doc = (res.get("documents") or [])[i] or ""
            out.append(_to_chunk(chunk_id, doc, meta, 0.0))
        return out

    async def list_tenants(self) -> list[str]:
        """Distinct tenant ids present in the collection (bulk admin — warm-all,
        migrations). Reads metadata only, offloaded like every other SDK call."""
        res = await asyncio.to_thread(self._collection.get, include=["metadatas"])
        return sorted({
            str(meta.get(self._tenant_field, ""))
            for meta in (res.get("metadatas") or [])
            if meta and meta.get(self._tenant_field)
        })
