"""§3 Atomic Agentic Workflows — orchestrator over pluggable backends.

Direct Context Injections (Resume, JD, ...) are fetched by deterministic
ingestion ids through the VectorStore adapter, then merged with tenant-scoped
rubric retrieval (semantic-cache-gated), reranked, and emitted as a U-shape
context envelope.

Realtime-readiness (Phase 0): the sync ``rerank`` call is CPU-bound ONNX
inference, so it runs in a worker thread (``asyncio.to_thread``); every stage
is timed with ``time.perf_counter`` and reported in ``timings_ms`` so a
concurrent voice server can measure per-stage latency in production.
"""
import asyncio
import time
from dataclasses import dataclass
from typing import Any

from enterprise_rag.model import Chunk
from enterprise_rag.security import SecurityContext
from enterprise_rag.formatter import ContextFormatter


@dataclass
class AgentContextRequest:
    sec_ctx: SecurityContext
    resume_text: str
    job_description: str
    conversation_history: list[str]
    rubric_query: str
    channel: str = "voice"
    # Generalization beyond the interview use case:
    direct_chunk_ids: list[str] | None = None   # per-request override of the default ids
    direct_context: dict[str, str] | None = None  # explicit injection, bypasses the store


class AtomicAgentContextOrchestrator:
    def __init__(self, hybrid_engine, reranker, semantic_cache, vector_store, *,
                 schema_version: str = "v1",
                 direct_chunk_ids: tuple[str, ...] = ("resume:current", "jd:target")):
        self._retriever = hybrid_engine
        self._reranker = reranker
        self._cache = semantic_cache
        self._vector_store = vector_store
        self._schema_version = schema_version
        self._default_direct_ids = direct_chunk_ids

    async def execute_agent_context(self, req: AgentContextRequest) -> dict[str, Any]:
        t_start = time.perf_counter()

        # 1. Direct candidate chunks via deterministic ingestion ids.
        #    get_by_ids() is an id lookup WITHOUT query filters, so tenant_id is
        #    re-verified post-fetch inside the adapter — never trust ids alone.
        direct_ids = req.direct_chunk_ids
        if direct_ids is None:
            direct_ids = list(self._default_direct_ids)
        direct_chunks = await self._vector_store.get_by_ids(direct_ids, req.sec_ctx.tenant_id)
        if req.direct_context:
            direct_chunks += [
                Chunk(chunk_id=f"direct:{i}", parent_id="direct",
                      tenant_id=req.sec_ctx.tenant_id, content=text, score=1.0)
                for i, text in enumerate(req.direct_context.values())
            ]
        t_direct = time.perf_counter()

        # 2. Rubric retrieval with semantic cache. The cache holds rubric
        #    provenance ONLY: direct chunks vary per request, so caching the
        #    full envelope would poison responses across candidates.
        query_vector = await self._retriever.embed_query(req.rubric_query)
        t_embed = time.perf_counter()
        cached = await self._cache.get(query_vector, req.sec_ctx.tenant_id, self._schema_version)
        t_cache = time.perf_counter()
        if cached is not None:
            rubric_chunks = [Chunk(**c) for c in cached["chunks"]]
            hit_source = "cache"
        else:
            rubric_chunks = await self._retriever.retrieve_parallel(
                req.rubric_query, req.sec_ctx, top_k=5, query_vector=query_vector,
            )
            await self._cache.put(
                query_vector,
                {"chunks": [{"chunk_id": c.chunk_id, "parent_id": c.parent_id,
                            "tenant_id": c.tenant_id, "content": c.content,
                            "score": c.score} for c in rubric_chunks]},
                req.sec_ctx.tenant_id, self._schema_version,
                query_text=req.rubric_query,
            )
            hit_source = "retrieval"
        t_retrieval = time.perf_counter()

        # 3. Synthesize direct context + retrieved rubrics; rerank the pool.
        #    ONNX inference is CPU-bound sync — offload from the event loop.
        final_chunks = await asyncio.to_thread(
            self._reranker.rerank, req.rubric_query, direct_chunks + rubric_chunks,
        )
        t_rerank = time.perf_counter()

        # 4. U-shape formatted context output.
        envelope = ContextFormatter.format_u_shape(final_chunks, req.sec_ctx)
        t_end = time.perf_counter()

        def _ms(a: float, b: float) -> float:
            return round((b - a) * 1000, 3)

        return {
            "status": "SUCCESS",
            "hit_source": hit_source,
            "context_envelope": envelope,
            "provenance": [
                {"chunk_id": c.chunk_id, "source": c.parent_id, "score": round(c.score, 4)}
                for c in final_chunks
            ],
            "timings_ms": {
                "direct": _ms(t_start, t_direct),
                "embed": _ms(t_direct, t_embed),
                "cache": _ms(t_embed, t_cache),
                "retrieval": _ms(t_cache, t_retrieval),
                "rerank": _ms(t_retrieval, t_rerank),
                "format": _ms(t_rerank, t_end),
                "total": _ms(t_start, t_end),
            },
        }
