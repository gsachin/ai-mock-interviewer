"""Elasticsearch keyword-leg adapter (design doc §1/§2, filter builder moved
here from the seed SecurityContext — verified against elasticsearch 9.5.0)."""
from typing import Any

from enterprise_rag.model import Chunk, UpsertRecord
from enterprise_rag.security import SecurityContext


def build_es_filter(sec_ctx: SecurityContext) -> dict[str, Any]:
    """Mandatory Elasticsearch security clauses for the sparse/BM25 leg.

    Department parity with the Qdrant filter: a `terms` clause in filter
    context (adds no score, only a hard constraint). An empty `terms: []`
    matches nothing in Elasticsearch, so the same omit-when-empty guard as the
    Qdrant builder applies.
    """
    flt: list[dict[str, Any]] = [
        {"term": {"tenant_id.keyword": sec_ctx.tenant_id}},
        {"range": {"required_clearance": {"lte": sec_ctx.clearance_level}}},
    ]
    if sec_ctx.departments:
        flt.append({"terms": {"department.keyword": sec_ctx.departments}})
    return {"bool": {"filter": flt}}


class ElasticsearchKeywordStore:
    """Sparse leg over Elasticsearch. The async client is injected — the caller
    picks the transport (see config.build_stack, which uses
    ``node_class=HttpxAsyncHttpNode`` to stay on the pinned httpx client)."""

    def __init__(self, client, *, index: str):
        self._client = client
        self._index = index

    async def search(self, query_text: str,
                     sec_ctx: SecurityContext, limit: int) -> list[Chunk]:
        resp = await self._client.search(
            index=self._index,
            query={
                "bool": {
                    "must": [
                        {"multi_match": {
                            "query": query_text,
                            "fields": ["content", "section_title"],
                            "type": "best_fields",
                        }}
                    ],
                    "filter": build_es_filter(sec_ctx)["bool"]["filter"],
                }
            },
            size=limit,
            source=["content", "section_title", "tenant_id", "parent_id",
                    "required_clearance", "department"],
        )
        return [
            Chunk(
                chunk_id=h["_id"],
                parent_id=h["_source"].get("parent_id"),
                tenant_id=h["_source"].get("tenant_id", ""),
                content=h["_source"].get("content", ""),
                score=float(h["_score"] or 0.0),  # raw BM25 score — RRF needs only the rank
                section_title=h["_source"].get("section_title", ""),
                required_clearance=int(h["_source"].get("required_clearance", 0)),
                department=h["_source"].get("department"),
            )
            for h in resp["hits"]["hits"]
        ]

    async def upsert(self, records: list[UpsertRecord]) -> None:
        for r in records:
            await self._client.index(
                index=self._index,
                id=r.chunk_id,
                document={
                    "parent_id": r.parent_id,
                    "tenant_id": r.tenant_id,
                    "content": r.content,
                    "section_title": r.section_title,
                    "required_clearance": r.required_clearance,
                    "department": r.department,
                },
            )
