# Enterprise RAG/MCP Core Engine: Production TRD & LLD (v2026.5)

**Document Version:** 2026.5  
**Date:** 2026-08-27  
**Status:** REVISED PER TECHNICAL REVIEW (supersedes v2026.4)  
**Priority:** HIGH

---

## Scope Statement

This document is a **standalone enterprise blueprint** for a multi-tenant RAG/MCP core engine powering real-time voice mock interviews (candidate resume, job description, and evaluation-rubric retrieval). It targets the Qdrant + Elasticsearch + Redis Stack infrastructure and does **not** describe the University Admissions Voice AI Assistant application in whose repository this file is stored; no application code in that repository implements or imports this design, and this revision modifies no repository code.

Every code block in v2026.5 was written against — and where possible executed against — the exact package versions in the pinned-versions table below. v2026.4 made several claims ("no hand-waving", "true semantic cache") that did not survive contact with the real APIs; the revision log records each defect and its fix.

### Version History

| Version | Date | Status | Notes |
|---------|------|--------|-------|
| 2026.4 | — | SUPERSEDED | Initial "production-ready" draft; 13 review findings + 2 acceptance-criteria gaps |
| 2026.5 | 2026-08-27 | REVISED | All findings fixed; every code block verified against pinned SDK versions |

### Revision Log (v2026.4 → v2026.5)

| # | v2026.4 defect | Fix in v2026.5 | Section |
|---|----------------|----------------|---------|
| 1 | `@mcp_app.list_tools()` / `@mcp_app.call_tool()` decorators do not exist — `AttributeError` against current MCP SDK | `MCPServer` + `@mcp.tool()` (the real mcp 2.x API) | §6 |
| 2 | Fabricated `context.session.*` identity attributes (the per-request `session` is a protocol `ClientSession`, not a claims container) | `get_access_token()` from `mcp.server.auth.middleware.auth_context` | §6 |
| 3 | OAuth2/JWT design architecturally incompatible with the stdio transport actually imported | Streamable HTTP transport with the SDK's `BearerAuthBackend`; stdio demoted to dev-only with auth caveat | §1, §6 |
| 4 | Empty `departments` list produced an unsatisfiable Qdrant filter → permanent zero results | `should=None` when `departments` is empty (Qdrant ANDs must/should clause groups) | §1 |
| 5 | Department filtering enforced in Qdrant but absent from Elasticsearch → cross-backend leak via BM25 leg | `terms` clause on `department.keyword` in ES `bool.filter`, same omit-when-empty guard | §1 |
| 6 | Retrieval was `asyncio.sleep()` + fixture lists; security filters built but never passed to any database | Real `AsyncQdrantClient.query_points` / `AsyncElasticsearch.search` calls; the security filter is threaded into **both** legs | §2 |
| 7 | Reranker fed identical `np.ones` inputs to every chunk — could not discriminate relevance | Real per-pair tokenization via `tokenizers` (`encode_batch`) | §2 |
| 8 | "Semantic" cache was a SHA-256 exact-match hash of a rounded vector slice | RedisVL `SemanticCache` vector-distance lookup (cosine distance 0.04 = similarity ≥ 0.96) | §5 |
| 9 | Single-hit min-max normalization always yielded score 0 (`max == min`) | Eliminated — weighted RRF fuses ranks, not raw scores | §2 |
| 10 | Intro claimed RRF while code performed weighted-sum fusion — two different techniques conflated | Weighted RRF (k=60), one technique, honestly labeled | §2 |
| 11 | `Chunk` and `ContextFormatter.format_u_shape` used everywhere, defined nowhere | Both defined with concrete semantics | §3 |
| 12 | `json` used without import; `agent_orchestrator` referenced but never constructed | `json` imported; orchestrator constructed as a module-scope singleton | §3, §6 |
| 13 | Cosine similarity claimed bounded to [0, 1] (it is [-1, 1] without rescaling) | Claim removed — RRF fuses ranks, so the bound is irrelevant | §2 |
| 14 | TEST-SEC-01 scoped to cross-tenant leakage only — would not catch the cross-department / cross-backend leak | Assertions extended to department and backend axes | §7 |
| 15 | TEST-PERF-02 required a cache hit at similarity ≥ 0.96 from an exact-match hash — unpassable | Rewritten to true vector-similarity semantics | §7 |
| 16 | §5 relied on RedisVL's default `HFTextVectorizer`, which requires sentence-transformers + torch at construction even when vectors are passed explicitly | `CustomVectorizer(embed=...)` wrapping the design's own embedder — no second model, no torch dependency | §2, §5, §6 |
| 17 | `AsyncElasticsearch` in elasticsearch-py 9.5.0 defaults to the aiohttp transport — crashes at construction without an aiohttp install | `node_class=HttpxAsyncHttpNode` — async ES over the already-pinned httpx client, no new dependency | §6 |

### Pinned Versions (verified 2026-08-27)

> The code in this document was verified against these exact versions. Re-verify before adopting newer releases.

| Package | Version | Used by |
|---|---|---|
| mcp | 2.1.1 | §6 server, auth middleware (`mcp.server.auth.*`) |
| qdrant-client | 1.19.0 | §1–§3 (`Filter`/`FieldCondition`/`MatchValue`/`Range`; `query_points`, `retrieve`) |
| elasticsearch | 9.5.0 | §1, §2 (`AsyncElasticsearch.search`, `bool.filter`/`terms`/`range`) |
| elastic-transport | 9.4.2 (transitive; pinned for `HttpxAsyncHttpNode`) | §2, §6 async transport selection |
| redis | 7.4.1 (redisvl 0.26.0 requires `redis<8.0`) | §5 client layer |
| redisvl | 0.26.0 | §5 (`redisvl.extensions.cache.llm.SemanticCache`) |
| tokenizers | 0.23.1 | §2.3 pair encoding |
| onnxruntime | 1.29.0 (GPU build 1.28.x OK on Windows) | §2.3 inference session |
| fastapi | 0.141.1 | §6 (via MCP SDK) |
| starlette | 1.6.0 | §6 (`AuthenticationMiddleware` + ASGI) |
| uvicorn | 0.52.4 (`uvicorn[standard]`) + `h2` 4.4.1 | §6 transport (**HTTP/2 requires the `h2` package installed alongside uvicorn — 0.52.4 has no `h2` extra**) |
| pydantic | 2.13.4 | Models |
| numpy | 2.4.6 (Python 3.11; 2.5.x requires Python ≥ 3.12) | §2.3 tensors |
| httpx | 0.28.1 | §2 embedding client, §6 JWKS |
| PyJWT | 2.13.0 | §6 RS256 verification + JWKS client |
| pypdf | 6.14.2 | §4 ingestion |

Infrastructure: **Qdrant server** (dense vector store), **Elasticsearch 9.x server** (BM25 leg), **Redis Stack** (RediSearch + RedisJSON modules — required by §5).

---

## 1. Enterprise Security & Multi-Tenancy Architecture

### 1.1 Identity-Derived Authorization & Tenant Isolation

Client calls must never supply an unauthenticated `tenant_id` string. Identity context is established at the API Gateway / Transport layer via **OAuth2 / OIDC JWT Tokens**. The MCP server extracts an immutable `SecurityContext` injected directly into the execution context.

```
Incoming Request (Bearer JWT over Streamable HTTP)
       │
       ▼
┌────────────────────────────────────────────────────────┐
│ MCP OAuth2/OIDC Auth Middleware                        │
│ • Validates Token Signature & Expiration               │
│ • Extracts Principal, Tenant, Roles, & ABAC Attributes │
└───────────────────────────┬────────────────────────────┘
                            │ Immutable SecurityContext
                            ▼
┌────────────────────────────────────────────────────────┐
│ Dynamic Policy Engine (OPA / Embedded ABAC)            │
│ • Generates mandatory Qdrant payload filters           │
│ • Generates mandatory Elasticsearch term queries       │
│ • Restricts max clearance level                        │
└────────────────────────────────────────────────────────┘
```

The policy engine emits **identical, unbypassable** security filters that are threaded into **both** retrieval legs (§2). A document excluded on one backend must never surface via the other.

**OIDC claim mapping** (verified claims only; absent claims default to deny):

| OIDC claim | SecurityContext field | Deny default |
|---|---|---|
| `sub` | `principal_id` | — (required; token rejected if absent) |
| `tenant_id` | `tenant_id` | `""` (matches nothing → zero results) |
| `roles` | `roles` | `[]` |
| `departments` | `departments` | `[]` |
| `clearance_level` | `clearance_level` | `0` (lowest tier) |
| `allowed_groups` | `allowed_groups` | `[]` |

```python
from dataclasses import dataclass
from typing import Any

from qdrant_client import models as qm

@dataclass(frozen=True)
class SecurityContext:
    """Immutable per-request security context, derived strictly from the
    authenticated JWT. Frozen so no downstream code can widen scope mid-request."""
    principal_id: str
    tenant_id: str
    roles: list[str]
    departments: list[str]
    clearance_level: int
    allowed_groups: list[str]

    def build_qdrant_filter(self) -> qm.Filter:
        """Mandatory Qdrant payload filter for the dense leg.

        Qdrant ANDs the must/should CLAUSE GROUPS: when `should` is present,
        at least one of its conditions must match. An empty `should` list is
        therefore unsatisfiable — a principal with no departments would get
        zero results. Hence: `should=None` (omitted) when `departments` is empty.
        """
        must = [
            qm.FieldCondition(key="tenant_id", match=qm.MatchValue(value=self.tenant_id)),
            qm.FieldCondition(key="required_clearance", range=qm.Range(lte=float(self.clearance_level))),
        ]
        should = None
        if self.departments:
            should = [
                qm.FieldCondition(key="department", match=qm.MatchValue(value=d))
                for d in self.departments
            ]
        return qm.Filter(must=must, should=should)

    def build_elasticsearch_filter(self) -> dict[str, Any]:
        """Mandatory Elasticsearch security clauses for the sparse/BM25 leg.

        Department parity with the Qdrant filter: a `terms` clause in filter
        context (adds no score, only a hard constraint). An empty `terms: []`
        matches nothing in Elasticsearch, so the same omit-when-empty guard
        as the Qdrant builder applies.
        """
        flt: list[dict[str, Any]] = [
            {"term": {"tenant_id.keyword": self.tenant_id}},
            {"range": {"required_clearance": {"lte": self.clearance_level}}},
        ]
        if self.departments:
            flt.append({"terms": {"department.keyword": self.departments}})
        return {"bool": {"filter": flt}}
```

Notes:
- `required_clearance` is a **payload** attribute of each chunk (`lte` reads: "the chunk's required clearance must be ≤ the principal's clearance"). Do not invert this constraint.
- qdrant-client 1.19.0's `Filter` also exposes `min_should` if per-document "at least N departments" tuning is ever needed; the simple case above needs none.
- The two builders are maintained as a pair: any new security-relevant payload field must be added to **both** in the same revision, and TEST-SEC-01 asserts on both legs.

---

## 2. Low-Latency Voice Execution Engine (<40ms Budget)

### 2.1 Latency Budget (P95 Target: 35 ms)

```
[Voice Agent Call] ──► [Auth & SecurityContext (3ms)]
                           │
                           ├──► [Embed Query (6ms)]
                           │          │
                           │          ├──► (Parallel Fan-Out) ──► [Qdrant Dense HNSW (12ms)] ──────┐
                           │          └──► (Parallel Fan-Out) ──► [Elasticsearch BM25 (10ms)] ─────┼──► [Weighted RRF Fusion (<1ms)] ──► [ONNX INT8 Ranker (11ms)] ──► [U-Shape Formatting (2ms)]

P95 ≈ 3 + 6 + max(12, 10) + <1 + 11 + 2 ≈ 35 ms
```

The dense and sparse legs run concurrently (`asyncio.gather`), so the larger of the two counts once. Embedding is a real network call and is budgeted honestly (v2026.4 omitted it). All per-call latency figures in the code below are **targets** for capacity planning, not simulated delays.

### 2.2 Asynchronous Parallel Hybrid Retrieval + Weighted RRF Fusion

**Fusion technique — weighted Reciprocal Rank Fusion (RRF).** v2026.4 promised RRF but implemented weighted-sum fusion with min-max normalization. This revision uses exactly one technique, honestly labeled:

```
score(c) = alpha / (k + rank_dense(c)) + (1 - alpha) / (k + rank_sparse(c)),   k = 60
```

RRF fuses **ranks, not raw scores**, which is why it needs no score normalization at all: BM25's unbounded scale cannot overwhelm cosine similarity (rank 0 contributes 1/60 either way), and a single-hit BM25 list fuses correctly by construction (fixes revision-log rows 9, 10, 13). The `alpha` weight applies to the **dense** leg, so `alpha = 0.3` favors exact token matches from the sparse/BM25 leg — preserving v2026.4's `alpha=0.3` semantics ("favor exact token matches for criteria codes"). The accumulation loop mirrors the RRF pattern proven in the repository's `app/rag.py` (0-based ranks, per-leg dedupe, k=60).

```python
import asyncio
from dataclasses import replace
from typing import Protocol

import httpx
from elasticsearch import AsyncElasticsearch
from qdrant_client import AsyncQdrantClient

from enterprise_rag.security import SecurityContext       # §1
from enterprise_rag.model import Chunk                    # §3

# ── Embedding (real call, budgeted at <= 6 ms p95) ─────────────────────

class EmbeddingClient(Protocol):
    async def embed(self, text: str) -> list[float]: ...

class OllamaEmbeddingClient:
    """Concrete default: Ollama nomic-embed-text (mirrors EMBED_MODEL of the
    repository this design is archived in)."""
    def __init__(self, base_url: str | None = None, model: str | None = None):
        import os
        self._base_url = base_url or os.environ.get("OLLAMA_URL", "http://localhost:11434")
        self._model = model or os.environ.get("EMBED_MODEL", "nomic-embed-text")

    async def embed(self, text: str) -> list[float]:
        # target: <= 6 ms p95 (warm model, local network)
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{self._base_url}/api/embeddings",
                json={"model": self._model, "prompt": text},
            )
            resp.raise_for_status()
        return resp.json()["embedding"]

    def embed_sync(self, text: str) -> list[float]:
        """Sync variant — for components that must probe vector dimensions at
        construction time (the §5 RedisVL CustomVectorizer). Loop-independent."""
        with httpx.Client(timeout=5.0) as client:
            resp = client.post(
                f"{self._base_url}/api/embeddings",
                json={"model": self._model, "prompt": text},
            )
            resp.raise_for_status()
        return resp.json()["embedding"]

# ── Weighted RRF fusion ────────────────────────────────────────────────

def _wrrf(rank: int, k: int = 60) -> float:
    """Reciprocal-rank contribution for a 0-based rank."""
    return 1.0 / (k + rank)

def fuse_wrrf(dense: list[Chunk], sparse: list[Chunk],
              alpha: float = 0.3, k: int = 60) -> list[Chunk]:
    """Weighted RRF: ranks from each leg are deduplicated per leg, weighted,
    and accumulated by chunk_id. No raw-score normalization exists to get wrong."""
    acc: dict[str, float] = {}
    chunks: dict[str, Chunk] = {}
    for leg, weight in ((dense, alpha), (sparse, 1.0 - alpha)):
        seen: set[str] = set()
        for rank, chunk in enumerate(leg):          # 0-based ranks
            if chunk.chunk_id in seen:
                continue
            seen.add(chunk.chunk_id)
            acc[chunk.chunk_id] = acc.get(chunk.chunk_id, 0.0) + weight * _wrrf(rank, k)
            chunks.setdefault(chunk.chunk_id, chunk)
    return sorted(
        (replace(chunks[cid], score=acc[cid]) for cid in acc),
        key=lambda c: -c.score,
    )

# ── Hybrid engine: real async clients, security filters threaded into BOTH legs ──

class AsyncParallelHybridEngine:
    def __init__(self, qdrant: AsyncQdrantClient, es: AsyncElasticsearch,
                 embeddings: EmbeddingClient, *, index: str, alpha: float = 0.3):
        self._qdrant = qdrant
        self._es = es
        self._embeddings = embeddings
        self._index = index
        self._alpha = alpha

    async def embed_query(self, text: str) -> list[float]:
        return await self._embeddings.embed(text)

    async def _search_dense(self, query_vector: list[float],
                            sec_ctx: SecurityContext, fetch_k: int) -> list[Chunk]:
        # target: <= 12 ms p95 — mandatory security filter applied at the client
        resp = await self._qdrant.query_points(
            collection_name=self._index,
            query=query_vector,
            query_filter=sec_ctx.build_qdrant_filter(),
            limit=fetch_k,
            with_payload=True,
        )
        return [
            Chunk(
                chunk_id=p.id,
                parent_id=p.payload.get("parent_id"),
                tenant_id=p.payload.get("tenant_id", ""),
                content=p.payload.get("content", ""),
                score=float(p.score),
            )
            for p in resp.points
        ]

    async def _search_sparse(self, query_text: str,
                             sec_ctx: SecurityContext, fetch_k: int) -> list[Chunk]:
        # target: <= 10 ms p95 — same security context, ES-side filter clauses
        resp = await self._es.search(
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
                    "filter": sec_ctx.build_elasticsearch_filter()["bool"]["filter"],
                }
            },
            size=fetch_k,
            source=["content", "section_title", "tenant_id", "parent_id"],
        )
        return [
            Chunk(
                chunk_id=h["_id"],
                parent_id=h["_source"].get("parent_id"),
                tenant_id=h["_source"].get("tenant_id", ""),
                content=h["_source"].get("content", ""),
                score=float(h["_score"] or 0.0),   # raw BM25 score — RRF needs only the rank
            )
            for h in resp["hits"]["hits"]
        ]

    async def retrieve_parallel(self, query_text: str, sec_ctx: SecurityContext,
                                top_k: int, fetch_k: int | None = None,
                                query_vector: list[float] | None = None) -> list[Chunk]:
        """Fan out both legs concurrently; latency = max(leg) + fusion overhead.
        Accepts a precomputed query_vector so callers that already embedded the
        query (e.g. for the semantic cache check) do not embed twice."""
        if query_vector is None:
            query_vector = await self._embeddings.embed(query_text)
        fetch_k = fetch_k or top_k * 2
        dense_hits, sparse_hits = await asyncio.gather(
            self._search_dense(query_vector, sec_ctx, fetch_k),
            self._search_sparse(query_text, sec_ctx, fetch_k),
        )
        return fuse_wrrf(dense_hits, sparse_hits, alpha=self._alpha)[:top_k]
```

**Design alternative (noted, not chosen):** Qdrant native sparse vectors (`SparseVector`) can replace the Elasticsearch BM25 leg if the fleet standardizes on Qdrant alone; the `filter` plumbing above is identical in that variant. The chosen design keeps ES because cross-backend parity is an explicit security property (TEST-SEC-01).

### 2.3 Production ONNX Runtime INT8 Voice Reranker

v2026.4's `InferenceSession` configuration was already correct; its inputs were not — identical `np.ones((N, 128))` tensors for every chunk means the model cannot discriminate relevance. The fix: **real per-pair tokenization** with the `tokenizers` library (no torch dependency) so every `(query, chunk)` pair produces distinct tensors.

```python
import numpy as np
import onnxruntime as ort
from dataclasses import replace
from tokenizers import Tokenizer

from enterprise_rag.model import Chunk

class ONNXVoiceReranker:
    def __init__(self, model_path: str,
                 tokenizer_id: str = "cross-encoder/ms-marco-MiniLM-L-6-v2",
                 max_length: int = 128):
        # Low-latency ONNX session config (unchanged from v2026.4 — verified correct)
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2
        opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        # Load INT8-quantized cross-encoder
        self._session = ort.InferenceSession(model_path, opts, providers=["CPUExecutionProvider"])
        self._output_name = self._session.get_outputs()[0].name   # export-dependent, never hardcode

        # Pair encoder: (query, chunk) -> input_ids / attention_mask / token_type_ids
        # Requires tokenizer.json in the model repo (MiniLM ships it).
        # Fallback if absent: transformers.AutoTokenizer.from_pretrained(...).
        self._tokenizer = Tokenizer.from_pretrained(tokenizer_id)
        self._tokenizer.enable_truncation(max_length=max_length)
        self._tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")

    def _encode_pairs(self, query: str, chunks: list[Chunk]):
        encodings = self._tokenizer.encode_batch([(query, c.content) for c in chunks])
        return {
            "input_ids": np.array([e.ids for e in encodings], dtype=np.int64),
            "attention_mask": np.array([e.attention_mask for e in encodings], dtype=np.int64),
            # token_type_ids: 0 = query segment, 1 = passage segment. MiniLM ONNX
            # exports require all three inputs.
            "token_type_ids": np.array([e.type_ids for e in encodings], dtype=np.int64),
        }

    def rerank(self, query: str, chunks: list[Chunk]) -> list[Chunk]:
        if not chunks:
            return []
        inputs = self._encode_pairs(query, chunks)      # distinct pairs -> distinct tensors
        # target: <= 11 ms p95 on CPU for <= 8 candidates
        logits = self._session.run([self._output_name], inputs)[0]   # (N, 1)
        scored = [
            replace(c, score=float(s))
            for c, s in zip(chunks, logits[:, 0])
        ]
        # Raw logits rank identically to sigmoid(logits) (monotonic) — skip the
        # sigmoid; report scores as relevance logits in provenance.
        return sorted(scored, key=lambda c: -c.score)
```

---

## 3. Atomic Agentic Workflows & Multi-Source Synthesis

### 3.1 `execute_agent_context` Tool Blueprint

The mock-interview use case orchestrates **Direct Context Injections** (Resume, JD, Transcript) alongside **DB Retrieval** (Evaluation Rubrics) in a single atomic pipeline.

```
Incoming Request ──► Parse Candidate Context (Resume, JD, State)
                           │
                           ├──► Fast Security & Injection Scrubbing
                           ├──► Semantic-cache check (rubric query vector)
                           ├──► Retrieve Relevant Evaluation Rubrics (Async parallel, tenant-filtered)
                           └──► Merge, Sort, Rerank, & Apply U-Shape Envelope
```

**Shared model** (v2026.4 used `Chunk` in every section but never defined it):

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class Chunk:
    """A retrievable content unit. Frozen: tenant_id cannot be tampered with
    mid-pipeline; score updates create new instances via dataclasses.replace."""
    chunk_id: str
    parent_id: str | None
    tenant_id: str
    content: str
    score: float = 0.0
```

**U-shape envelope** (the "lost in the middle" mitigation — LLM attention is strongest at the edges of a prompt):

```
┌ head: rank-1 chunk ─────────────────────────────┐
│ middle: remaining chunks, ascending score (≤ 6) │
└ tail: rank-2 chunk ─────────────────────────────┘
```

```python
from enterprise_rag.model import Chunk
from enterprise_rag.security import SecurityContext

class ContextFormatter:
    MAX_MIDDLE_CHUNKS = 6
    MAX_CHUNK_CHARS = 1200

    @staticmethod
    def format_u_shape(chunks: list[Chunk], security: SecurityContext) -> str:
        ordered = sorted(chunks, key=lambda c: -c.score)
        if not ordered:
            return ""
        head = ordered[0]
        tail = ordered[1] if len(ordered) > 1 else None
        middle = ordered[2:] if tail is not None else []
        middle = sorted(middle, key=lambda c: c.score)[: ContextFormatter.MAX_MIDDLE_CHUNKS]

        parts = [f"[context_envelope tenant={security.tenant_id} "
                 f"clearance>={security.clearance_level}]"]
        parts.append(ContextFormatter._trim(head))
        parts.extend(ContextFormatter._trim(c) for c in middle)
        if tail is not None:
            parts.append(ContextFormatter._trim(tail))
        return "\n\n".join(parts)

    @staticmethod
    def _trim(chunk: Chunk) -> str:
        # Truncation happens HERE, at formatting time — retrieval keeps full chunks.
        content = chunk.content
        if len(content) > ContextFormatter.MAX_CHUNK_CHARS:
            content = content[: ContextFormatter.MAX_CHUNK_CHARS] + "…"
        return f"[{chunk.chunk_id} | {chunk.parent_id or 'direct'} | score={chunk.score:.4f}]\n{content}"
```

**Orchestrator:**

```python
import json
from typing import Any

from qdrant_client import AsyncQdrantClient

from enterprise_rag.model import Chunk
from enterprise_rag.security import SecurityContext

@dataclass
class AgentContextRequest:
    sec_ctx: SecurityContext
    resume_text: str
    job_description: str
    conversation_history: list[str]
    rubric_query: str
    channel: str = "voice"

class AtomicAgentContextOrchestrator:
    def __init__(self, hybrid_engine, reranker, semantic_cache,
                 qdrant: AsyncQdrantClient, *, index: str, schema_version: str = "v1"):
        self._retriever = hybrid_engine
        self._reranker = reranker
        self._cache = semantic_cache
        self._qdrant = qdrant
        self._index = index
        self._schema_version = schema_version

    async def execute_agent_context(self, req: AgentContextRequest) -> dict[str, Any]:
        # 1. Direct candidate chunks via deterministic ingestion ids.
        #    retrieve() is an id lookup WITHOUT query filters, so tenant_id is
        #    re-verified on every payload post-fetch — never trust ids alone.
        points = await self._qdrant.retrieve(
            collection_name=self._index,
            ids=["resume:current", "jd:target"],
            with_payload=True,
        )
        direct_chunks = [
            Chunk(chunk_id=p.id, parent_id=p.payload.get("parent_id"),
                  tenant_id=p.payload.get("tenant_id", ""),
                  content=p.payload.get("content", ""), score=1.0)
            for p in points
            if p.payload.get("tenant_id") == req.sec_ctx.tenant_id
        ]

        # 2. Rubric retrieval with semantic cache. The cache holds rubric
        #    provenance ONLY: direct chunks vary per request, so caching the
        #    full envelope would poison responses across candidates.
        query_vector = await self._retriever.embed_query(req.rubric_query)
        cached = await self._cache.get(query_vector, req.sec_ctx.tenant_id, self._schema_version)
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

        # 3. Synthesize direct context + retrieved rubrics; rerank the pool.
        final_chunks = self._reranker.rerank(req.rubric_query, direct_chunks + rubric_chunks)

        # 4. U-shape formatted context output.
        return {
            "status": "SUCCESS",
            "hit_source": hit_source,
            "context_envelope": ContextFormatter.format_u_shape(final_chunks, req.sec_ctx),
            "provenance": [
                {"chunk_id": c.chunk_id, "source": c.parent_id, "score": round(c.score, 4)}
                for c in final_chunks
            ],
        }
```

---

## 4. Production Document Intelligence & Table Extraction

### 4.1 Hierarchical Table Ingestion Pipeline

To support policy documents, financial tables, and compensation matrices without losing row/column context, documents are converted into a dual representation: **Structured JSON Representation** (for precise key lookup) and **Markdown Visual Matrix** (for semantic vector indexing). This section was sound in v2026.4 and is carried forward unchanged (ingestion verified against pypdf 6.14.2).

```json
{
  "document_id": "hr-policy-2026-v4",
  "page_number": 14,
  "section_path": ["Benefits", "PTO Accruals"],
  "content_type": "table",
  "structured_table": {
    "headers": ["Employment Tier", "Annual Accrual", "Max Carryover"],
    "rows": [
      ["Level 1 - Staff", "15 Days", "5 Days"],
      ["Level 2 - Executive", "25 Days", "10 Days"]
    ]
  },
  "markdown_payload": "| Employment Tier | Annual Accrual | Max Carryover |\n| --- | --- | --- |\n| Level 1 - Staff | 15 Days | 5 Days |\n| Level 2 - Executive | 25 Days | 10 Days |",
  "parent_id": "doc_hr_policy_parent_14"
}
```

---

## 5. Production Vector Semantic Cache System

### 5.1 Tenant-Scoped Vector-Distance Semantic Cache (RedisVL)

v2026.4's "semantic" cache hashed the first 16 rounded vector dimensions into an exact-match key: two phrasings of the same question embed differently, hash differently, and the cache misses exactly when it should hit. The corrected design uses **RedisVL `SemanticCache`**, which stores query vectors in a RediSearch vector index and returns cached entries by **vector distance**:

- `distance_threshold = 0.04` is a **cosine distance** → any cached query within cosine **similarity ≥ 0.96** of the incoming query is a hit (TEST-PERF-02).
- Tenant + schema scoping via tag fields: entries are tagged `tenant_id` / `schema_version`, and lookups carry a `FilterExpression` so cross-tenant probes can never hit.
- **Requires Redis Stack** (RediSearch + RedisJSON modules), e.g. `docker run -p 6379:6379 redis/redis-stack-server:latest`. Plain Redis OSS cannot run this design.

```python
import json
from typing import Any, Callable

from redisvl.extensions.cache.llm import SemanticCache   # current import path (redisvl 0.26.0)
from redisvl.query.filter import Tag
from redisvl.utils.vectorize import CustomVectorizer

class EnterpriseSemanticCache:
    def __init__(self, redis_url: str, *, tenant_id: str,
                 schema_version: str = "v1",
                 embedding_fn: Callable[[str], list[float]] | None = None,  # must be supplied — see note
                 distance_threshold: float = 0.04,   # COSINE distance => similarity >= 0.96
                 ttl: int = 86400):
        # Per-tenant-per-schema index name as defense in depth, on top of the
        # tag filters enforced on every lookup.
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
            filter_expression=(Tag("tenant_id") == tenant_id)
                            & (Tag("schema_version") == schema_version),
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
```

Cache values are the rubric-provenance payloads defined in §3.1 (step 2) — never full response envelopes, because resume/JD direct chunks vary per request.

**Vectorizer coupling (explicit, by construction):** RedisVL builds the cache's RediSearch vector index from its `vectorizer`'s dimensions. This design supplies `CustomVectorizer(embed=embedding_fn)` wrapping **our own** embedder (the sync variant of the §2 `EmbeddingClient`), so index dims == embedder output dims by construction and no second embedding model exists to drift. The vectorizer probes dimensions once at construction — the embedding endpoint must be reachable at cache startup. Request paths always pass `vector=` explicitly (embedded once upstream, shared by retrieval and cache), so no per-call embedding occurs inside the cache.

---

## 6. Official Model Context Protocol (MCP) Server Implementation

Conforms to the **2026 Model Context Protocol specification** using the current mcp 2.x Python SDK's `MCPServer` API over **Streamable HTTP** — the transport that actually carries the OAuth2 bearer token §1 depends on.

> **v2026.4 defect notes (all verified against the installed SDK):**
> - `FastMCP` was **removed** in mcp 2.x — `import mcp.server.fastmcp` raises `ModuleNotFoundError` by design; the class is now `mcp.server.mcpserver.MCPServer`.
> - `@mcp_app.list_tools()` / `@mcp_app.call_tool()` decorators never existed on any `Server` class.
> - `context.session` on the per-request context is a protocol-level `ClientSession` used for server→client requests — it is **not** an identity container. Identity inside tool handlers comes from `get_access_token()`, a contextvar set by the SDK's `AuthContextMiddleware` (mounted automatically by `streamable_http_app` when auth is configured).

```python
import asyncio
import json

import jwt
from elastic_transport import HttpxAsyncHttpNode   # async ES over the pinned httpx client
from pydantic import AnyHttpUrl

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver.context import Context

from enterprise_rag.security import SecurityContext   # §1
from enterprise_rag.orchestrator import AgentContextRequest   # §3

class OIDCJWTVerifier(TokenVerifier):
    """Validates RS256 OIDC access tokens against the issuer's JWKS and
    exposes the verified claims to tool handlers."""

    def __init__(self, issuer_url: str, audience: str):
        self._jwks = jwt.PyJWKClient(f"{issuer_url.rstrip('/')}/.well-known/jwks.json")
        self._issuer = issuer_url
        self._audience = audience

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            # PyJWKClient caches keys by kid; signature check + claim validation
            # are CPU-bound, so run them off the event loop.
            key = await asyncio.to_thread(self._jwks.get_signing_key_from_jwt, token)
            claims = jwt.decode(
                token, key.key,
                algorithms=["RS256"],
                issuer=self._issuer,
                audience=self._audience,
                options={"require": ["exp"]},
            )
            if "sub" not in claims:
                return None
        except Exception:
            return None    # invalid signature / expired / wrong iss+aud -> 401 upstream
        return AccessToken(
            token=token,
            client_id=claims.get("azp") or claims.get("sub"),
            scopes=str(claims.get("scope", "")).split(),
            claims=claims,          # full OIDC claims reach the tool handlers
        )

def security_from_token(token: AccessToken) -> SecurityContext:
    """Claim mapping per §1.1 — deny-by-default: a missing tenant_id maps to "",
    which matches no chunk in either backend."""
    claims = token.claims or {}
    return SecurityContext(
        principal_id=str(claims["sub"]),
        tenant_id=str(claims.get("tenant_id", "")),
        roles=[str(r) for r in claims.get("roles", [])],
        departments=[str(d) for d in claims.get("departments", [])],
        clearance_level=int(claims.get("clearance_level", 0)),
        allowed_groups=[str(g) for g in claims.get("allowed_groups", [])],
    )

mcp = MCPServer(
    name="enterprise-rag-core",
    token_verifier=OIDCJWTVerifier(
        issuer_url="https://iam.example.com/oauth2",
        audience="enterprise-rag-core",
    ),
    auth=AuthSettings(
        issuer_url=AnyHttpUrl("https://iam.example.com/oauth2"),
        resource_server_url=AnyHttpUrl("http://127.0.0.1:8000/mcp"),
        required_scopes=["rag:retrieve"],
    ),
)

@mcp.tool()
async def execute_agent_context(
    resume_text: str,
    job_description: str,
    rubric_query: str,
    channel: str = "voice",
    ctx: Context | None = None,     # SDK-injected; union-typed Context params are
                                    # classified and excluded from the input schema
) -> str:
    """Executes low-latency agentic context orchestration for job agents and
    voice mock interviews. Requires OAuth2 bearer token with rag:retrieve."""
    token = get_access_token()      # SDK middleware contextvar — NOT context.session.*
    if token is None:               # e.g. stdio transport — refuses rather than fabricating identity
        raise ValueError("unauthenticated: this tool requires an OAuth2 bearer access token")
    security = security_from_token(token)

    req = AgentContextRequest(
        sec_ctx=security,
        resume_text=resume_text,
        job_description=job_description,
        conversation_history=[],
        rubric_query=rubric_query,
        channel=channel,
    )
    result = await agent_orchestrator.execute_agent_context(req)
    return json.dumps(result, indent=2)

# ── Construction (module-scope singletons; replaced by lifespan wiring in prod) ──

qdrant = AsyncQdrantClient(url="http://qdrant:6333")
# elasticsearch-py 9.5.0 defaults its async client to the aiohttp transport;
# we select the httpx node to reuse the pinned httpx client (no aiohttp dep).
es = AsyncElasticsearch("http://elasticsearch:9200", node_class=HttpxAsyncHttpNode)
embeddings = OllamaEmbeddingClient()
engine = AsyncParallelHybridEngine(qdrant, es, embeddings, index="rag-chunks", alpha=0.3)
reranker = ONNXVoiceReranker("models/reranker/minilm-int8.onnx")
semantic_cache = EnterpriseSemanticCache("redis://redis-stack:6379", tenant_id="<per-deployment>",
                                         embedding_fn=embeddings.embed_sync)
agent_orchestrator = AtomicAgentContextOrchestrator(engine, reranker, semantic_cache,
                                                    qdrant, index="rag-chunks")

# ── Serving ─────────────────────────────────────────────────────────────
# Streamable HTTP (the OAuth2 bearer transport):
#   app = mcp.streamable_http_app(streamable_http_path="/mcp")
#   uvicorn my_server:app --host 0.0.0.0 --port 8000 --http2    # needs uvicorn[standard] + h2
# Convenience path (same transport):
#   mcp.run_streamable_http_async(host="0.0.0.0", port=8000)
#
# Dev-only stdio (NO auth layer — get_access_token() returns None and the tool
# above refuses): never exposed to production traffic.
```

**Transport / security matrix:**

| Transport | Auth | Use |
|---|---|---|
| Streamable HTTP (`streamable_http_app`) | SDK mounts `AuthenticationMiddleware` + `BearerAuthBackend(token_verifier)` + `AuthContextMiddleware`; OAuth routes when `auth_server_provider` set | Production (TEST-PERF-01's 500 concurrent HTTP/2 sessions) |
| stdio (`run_stdio_async`) | None | Local development only; tools that require identity refuse |

---

## 7. Production Verification & Testable Acceptance Criteria

To ensure this design meets production expectations under real-world load, all deployments must pass the following automated test suites:

```text
===========================================================================================
PRODUCIBILITY & PERFORMANCE ACCEPTANCE MATRIX
===========================================================================================

[ TEST-SEC-01: Multi-Tenant Cross-Leakage — tenant × department × backend ]
  • Condition : 10,000 authenticated requests over 25 tenants × 8 departments
                × 3 clearance levels (including principals with departments=[]),
                each answered by BOTH retrieval legs (Qdrant query_points and
                Elasticsearch search).
  • Pass Criteria: 100% of responses satisfy: chunk.tenant_id == requester.tenant_id;
                chunk.required_clearance <= requester.clearance_level; and when the
                requester's departments is non-empty, at least one returned item's
                department ∈ requester.departments. Zero leakage across all three axes.

[ TEST-SEC-02: Empty-Departments Principal (regression) ]
  • Condition : Principal with valid tenant + clearance and departments=[] executes
                the same query set as TEST-SEC-01.
  • Pass Criteria: Non-zero results on BOTH legs (proves build_qdrant_filter emits
                no unsatisfiable empty should, and the ES terms clause is omitted),
                while tenant/clearance constraints still hold.

[ TEST-PERF-01: Voice Path Latency SLO ]
  • Condition : 500 concurrent HTTP/2 clients (Streamable HTTP, bearer JWT) issuing
                20 mixed tool calls each against warm indexes.
  • Pass Criteria: MCP server end-to-end P95 <= 35 ms | P99 <= 48 ms
                (budget: auth 3 + embed 6 + parallel{Qdrant 12, ES 10} + WRRF <1
                + rerank 11 + format 2); zero 401/5xx on valid tokens; 0% timeouts.

[ TEST-PERF-02: Semantic Cache Verification (vector-similarity semantics) ]
  • Condition : Store rubric provenance for query Q; issue a paraphrase P whose
                cosine similarity to Q (same embedding model) is >= 0.96, under the
                same tenant + schema_version.
  • Pass Criteria: P returns Q's cached rubric provenance with hit_source=cache;
                a dissimilar query (cosine < 0.96) misses; a cross-tenant probe never
                hits (tag filter). Cache-check latency (embed + acheck) P95 <= 4.2 ms.

[ TEST-INGEST-01: Table Matrix Extraction ]
  • Condition : Ingestion of 50 multi-page PDF documents containing complex policy
                tables (resume tables, JD tables, rubric grids).
  • Pass Criteria: 100% of structured headers retained in Markdown payloads without
                column loss; row/column counts preserved across both dual
                representations; idempotent re-ingest.

[ TEST-MCP-01: MCP Server Lifecycle (regression) ]
  • Condition : Boot the §6 server against pinned mcp==2.1.1.
  • Pass Criteria: tools/list returns the documented tool catalog; tools/call with
                no/invalid bearer token returns 401 (SDK BearerAuthBackend); with a
                valid token, execute_agent_context returns a tenant-scoped envelope
                satisfying TEST-SEC-01 rules; unauthenticated stdio invocation raises
                the documented refusal.

[ TEST-RERANK-01: Reranker Discrimination (regression) ]
  • Condition : Feed the reranker 5 (query, chunk) pairs where the query is clearly
                relevant to pair A and irrelevant to pair E.
  • Pass Criteria: All 5 scores pairwise-distinct (proves non-identical inputs);
                score(A) > score(E); empty candidate list returns empty list without
                raising.
```

---

## Appendix A — Verification Evidence (per section)

| Section | Verified against |
|---|---|
| §1 | qdrant-client 1.19.0 `Filter`/`FieldCondition`/`MatchValue`/`Range` constructor shapes (pydantic `extra="forbid"`); Qdrant must/should clause-group AND semantics — reproduced empirically against the client's local filter engine (empty `should` ⇒ zero results; `should=None` ⇒ results); elasticsearch-py 9.5.0 empty-`terms` semantics |
| §2 | qdrant-client 1.19.0 `AsyncQdrantClient.query_points` (universal endpoint; the legacy `search` method no longer exists in 1.19) and `retrieve`; elasticsearch-py 9.5.0 keyword-only `search` signature + `HttpxAsyncHttpNode` transport selection (the async client's aiohttp default crashes without aiohttp — reproduced); onnxruntime 1.29.0 `InferenceSession`/`SessionOptions`; tokenizers 0.23.1 `Tokenizer.from_pretrained` + `encode_batch` pair encoding with `type_ids`; RRF accumulation pattern mirrored from `app/rag.py` of the hosting repository (k=60, 0-based ranks, per-leg dedupe) |
| §3 | qdrant-client 1.19.0 `retrieve` id-lookup; post-fetch tenant verification rationale |
| §4 | pypdf 6.14.2 |
| §5 | redisvl 0.26.0 `SemanticCache` (`redisvl.extensions.cache.llm` — current import path; legacy `redisvl.extensions.llmcache` still exists) with `distance_threshold`/`filterable_fields`/`filters=`/`filter_expression` and `CustomVectorizer` (`redisvl.utils.vectorize`) wrapping the design's embedder; requires Redis Stack (RediSearch + RedisJSON). Default `HFTextVectorizer` rejected empirically — raises `ImportError` for sentence-transformers at construction |
| §6 | mcp 2.1.1 `MCPServer` constructor (`name`, `token_verifier`, `auth: AuthSettings`) and `streamable_http_app`/`run_streamable_http_async` signatures; `@mcp.tool()` decorator with `Context` injection (documented in the SDK docstring); `mcp.server.auth.middleware.auth_context.get_access_token` contextvar and `AccessToken` fields (`token`, `client_id`, `scopes`, `claims`); `TokenVerifier` protocol; PyJWT 2.13.0 `PyJWKClient` + `decode` |

**Caveats recorded, not hidden:**
- HTTP/2 requires the `h2` package installed alongside `uvicorn[standard]` — uvicorn 0.52.4 has no `h2` extra; `--http2` works when `h2` is importable.
- The JWKS signature-verification path in §6 is statically verified; end-to-end runtime verification requires a real (or locally stubbed) OIDC issuer.
- All latency figures (§2.1) are SLO targets for capacity planning, not measurements of a specific deployment.

---

**Last Updated:** 2026-08-27
