# TRD — Phase 0: Realtime-Readiness Hardening

**Repo:** enterprise-rag-core
**Date:** 2026-08-29
**Status:** Implemented — every user story validated by an automated test (see §7)
**Validation run:** full suite **114 passed, 0 skipped** (Redis Stack live;
13 new Phase 0 tests, 2 pre-existing SDK deprecation warnings from redisvl)
**Parent:** Live Voice Interviewer feasibility study (§5 roadmap, Phase 0)

## 1. Context and objectives

The engine will be consumed by a real-time voice interviewer: dozens of
concurrent interview sessions, each turning on sub-second deadlines
(< 1.5 s utterance → first audio, of which retrieval is budgeted < 150 ms).
Phase 0 hardens the engine for that consumer **without coupling to it** —
the package must remain a standalone, zero-infrastructure library.

Phase 0 goals:

1. No synchronous work on the event loop in request paths.
2. Per-stage millisecond latency observability in the orchestration result.
3. Multi-tenant boot warm-up for the in-memory keyword leg.
4. One embedding contract across MLX, vLLM, and Ollama servers.
5. Evidence that concurrent sessions overlap instead of serialize.

Explicit non-goals: dialogue state, audio, LLM generation (Phases 1–3),
and replacing the O(n) BM25 rebuild with an incremental index (deferred,
see §8).

## 2. Design principles

- **Standalone library stays standalone.** All changes live inside the
  `enterprise_rag` package and its tests; no new runtime dependencies
  (stdlib `asyncio`/`time` only); no voice-framework imports anywhere;
  `import enterprise_rag` must still pull in only the core deps.
- **Additive, not breaking.** Public signatures grow keyword-only optional
  parameters; existing env vars keep their meaning; default behavior is
  unchanged (the default warm scope is still the default tenant).
- **Env-driven, like everything else.** New behavior is selectable via
  `RAG_CORE_*` variables, never hard-coded for one consumer.
- **Validated, not asserted by prose.** Every user story below maps to a
  runnable test; the run results are recorded in §7.

## 3. Decision: event-loop offload strategy

The voice gateway will run many sessions in one process. Three places in
the request path do synchronous work inside `async` methods:

| Call site | Blocking work | Fix |
|---|---|---|
| `chroma_vector.py` — `query`, `get`, `upsert`, `delete` | Chroma SDK is fully synchronous | wrap every SDK call in `asyncio.to_thread` |
| `bm25_memory.py` — `_score`, `_rebuild` | CPU-bound BM25 scoring / index rebuild | offload both |
| `memory_vector.py` — cosine scoring loop | CPU-bound numpy | offload the scoring closure |
| `cache.py` — `InMemorySemanticCache.get` | CPU-bound numpy cosine over entries | offload the lookup closure |
| `orchestrator.py` — `rerank` | CPU-bound ONNX inference (≤ 11 ms for ≤ 8 candidates) | offload via `asyncio.to_thread` |

Deliberately **not** offloaded: trivial in-memory dict operations
(`get_by_ids`, `upsert` bookkeeping, `NoOp*` classes) and one-time
construction probes (`embed_sync` dimension probe, the RedisVL vectorizer
probe) — offloading sub-microsecond work costs more than it saves.

`asyncio.to_thread` uses the default executor (`min(32, cpu + 4)` workers);
that is sufficient for the Phase 0 concurrency target. A per-room worker
process model (LiveKit) further isolates sessions and is orthogonal to
this change.

## 4. Decision: per-stage timing instrumentation

`execute_agent_context` now measures each stage with `time.perf_counter`
and appends a `timings_ms` object to the result (additive — existing keys
unchanged):

```
"timings_ms": {
    "direct":   float,  # get_by_ids + direct_context synthesis
    "embed":    float,  # rubric query embedding
    "cache":    float,  # semantic-cache lookup
    "retrieval":float,  # hybrid legs (≈0 on cache hit)
    "rerank":   float,  # includes the thread handoff
    "format":   float,  # U-shape envelope
    "total":    float,
}
```

Values are milliseconds, rounded to 3 decimals, always ≥ 0. A live server
can stream this object per turn to a latency dashboard; tests assert its
schema.

## 5. Decision: warm-all-tenants + `list_tenants` protocol growth

The in-memory BM25 leg starts empty in every fresh process; warm-up
repopulated only the default tenant. `VectorStore` grows one method:

```
async def list_tenants(self) -> list[str]   # distinct tenant ids
```

implemented by all adapters — memory (dict scan), Chroma (metadata-only
`get`), Qdrant (unfiltered scroll with a `tenant_id`-only payload
selector; its distinct logic is a pure helper, unit-tested without a
server). `warm_keyword_from_vector_store(stack, tenants=...)` selects the
scope: `None` = default tenant (unchanged), `"all"` = every tenant, or an
explicit list. The CLI reads `RAG_CORE_WARM_KEYWORD`: `0` off, `1` default
tenant (unchanged), `all` every tenant.

## 6. Decision: one embedding contract for MLX, vLLM, and Ollama

All three servers must be usable with zero code changes:

| Backend | Endpoint | Wire contract |
|---|---|---|
| `ollama` | `POST {base}/api/embeddings` | `{"model", "prompt"}` → `{"embedding": [...]}` |
| `mlx` | `POST {base}/embeddings` | OpenAI-compatible: `{"model", "input"}` → `{"data":[{"embedding":[...]}]}` |
| `vllm` | `POST {base}/embeddings` | same OpenAI-compatible contract (vLLM `--task embed`) |
| `openai` | `POST {base}/embeddings` | same contract (alias for any OpenAI-compatible API) |

`RAG_CORE_EMBED_BACKEND` now accepts `ollama | mlx | vllm | openai | auto`.
`auto` performs OS auto-configuration: **mlx** on macOS Apple Silicon,
**vllm** where an NVIDIA GPU is detected (stdlib probe — the Linux driver
procfs marker or `nvidia-smi` on PATH; never raises), **ollama** elsewhere.
An explicit backend always wins over auto. Base-URL
resolution precedence for the OpenAI-compatible family:
`RAG_CORE_EMBED_BASE_URL` (new, generic) → `RAG_CORE_MLX_BASE_URL`
(legacy, mlx only) → per-backend default (`vllm`/`mlx`:
`http://127.0.0.1:8000/v1`, `openai`: `https://api.openai.com/v1`).
`EMBED_MODEL` is required for the OpenAI-compatible family. Both embedding
clients gained keyword-only `transport`/`sync_transport` seams — test-only
hooks for `httpx.MockTransport`; production passes nothing.

## 7. User stories and validation record

### US-01 — Chroma adapter never blocks the loop

**Story:** As a voice-gateway developer, I want every Chroma SDK call
offloaded from the event loop so one slow query cannot stall the other
concurrent sessions.

**Acceptance:**
- GIVEN a Chroma collection with records, WHEN `search`, `get_by_ids`,
  `upsert`, `delete_by_parent`, `get_all`, and `list_tenants` are awaited,
  THEN each executes through `asyncio.to_thread` AND returns the same
  results as before (tenant/clearance filters intact).

**Validation:** `tests/test_realtime_offload.py::test_chroma_offloads_sdk_calls`
— spies on `asyncio.to_thread` while asserting ranking and filtering results.

**Result:** ✅ PASSED

### US-02 — CPU-bound scoring leaves the loop

**Story:** As a voice-gateway developer, I want BM25 scoring/index
rebuilds, the in-memory vector cosine scoring, and the in-memory cache
cosine lookup offloaded, so heavy scoring never blocks turn-taking.

**Acceptance:**
- GIVEN a populated BM25 store, WHEN `upsert` and `search` are awaited,
  THEN `_rebuild` and `_score` run in worker threads AND rankings are
  unchanged.
- GIVEN a populated memory vector store, WHEN `search` is awaited, THEN
  the cosine scoring runs in a worker thread AND results are unchanged.
- GIVEN a populated in-memory cache, WHEN `get` is awaited, THEN the
  cosine lookup runs in a worker thread AND payloads are unchanged.

**Validation:** `test_bm25_offloads_scoring_and_rebuild`,
`test_memory_vector_offloads_cosine_scoring`,
`test_memory_cache_offloads_cosine_lookup` (same file).

**Result:** ✅ PASSED

### US-03 — Orchestrator offloads rerank

**Story:** As a voice-gateway developer, I want the ONNX rerank inference
to run off the event loop so concurrent sessions share CPU without
stalling each other.

**Acceptance:**
- GIVEN an orchestrator with any sync reranker, WHEN
  `execute_agent_context` is awaited, THEN the rerank call executes via
  `asyncio.to_thread` AND the result order/content is unchanged.

**Validation:** `test_orchestrator_offloads_rerank` — spies on the
orchestrator's `to_thread` calls and filters for the rerank callable.

**Result:** ✅ PASSED

### US-04 — Per-stage timings are reported

**Story:** As an SRE, I want per-stage millisecond timings in every
orchestration result so a live voice server can surface latency per turn.

**Acceptance:**
- GIVEN any `execute_agent_context` call, THEN the result contains
  `timings_ms` with exactly the keys `direct, embed, cache, retrieval,
  rerank, format, total`, each a non-negative float, `total > 0`.

**Validation:** `test_orchestrator_reports_per_stage_timings`.

**Result:** ✅ PASSED

### US-05 — Boot warm-up covers every tenant

**Story:** As an operator of a multi-tenant deployment, I want the boot
warm-up to repopulate the BM25 leg for every tenant so no tenant's first
keyword query silently misses after a restart.

**Acceptance:**
- GIVEN two tenants (acme, beta) persisted in the vector store and a
  fresh process, WHEN `warm_keyword_from_vector_store(stack,
  tenants="all")` is awaited, THEN the returned count equals the sum of
  both tenants' chunks AND keyword queries for both tenants return their
  content.
- WHEN warm-up runs with default scope, THEN only the default tenant is
  warmed (unchanged behavior).

**Validation:** `tests/test_warm_keyword.py::test_warm_all_tenants_repopulates_every_tenant`,
`test_warm_default_scope_ignores_other_tenants`.

**Result:** ✅ PASSED

### US-06 — Tenant discovery on every vector backend

**Story:** As a developer writing migrations and warm-all tooling, I want
`list_tenants()` on every vector backend so scope discovery is backend-
agnostic.

**Acceptance:**
- GIVEN records for tenants acme and beta, WHEN `list_tenants()` is
  awaited on the memory and Chroma adapters, THEN both return
  `["acme", "beta"]`; empty stores return `[]`.
- GIVEN scrolled Qdrant points with payloads, THEN the distinct helper
  returns sorted unique tenant ids and skips empty payloads.

**Validation:** `tests/test_list_tenants.py` (all three tests).

**Result:** ✅ PASSED

### US-07 — MLX, vLLM, and Ollama run the same engine

**Story:** As a deployer, I want to point the same engine at an MLX, a
vLLM, or an Ollama embedding server (or any OpenAI-compatible endpoint)
by changing env vars only.

**Acceptance:**
- GIVEN `RAG_CORE_EMBED_BACKEND=ollama`, THEN the stack embeds via
  `OllamaEmbeddingClient` with the documented `{"model", "prompt"}`
  contract (validated against a mock transport, async and sync variants).
- GIVEN `vllm`/`mlx`/`openai` with `EMBED_MODEL`, THEN the stack embeds
  via `OpenAICompatibleEmbeddingClient` with the `{"model", "input"}`
  contract and the per-backend default base URL (vLLM: `http://127.0.0.1:8000/v1`).
- GIVEN `RAG_CORE_EMBED_BASE_URL`, THEN it overrides per-backend and
  legacy `RAG_CORE_MLX_BASE_URL` values.
- GIVEN `mlx`/`vllm`/`openai` without `EMBED_MODEL`, THEN `build_stack`
  raises `ValueError` naming `EMBED_MODEL`.
- GIVEN `embed_backend=auto`, THEN OS auto-configuration selects mlx on
  macOS Apple Silicon, vllm on machines with a detected NVIDIA GPU, and
  ollama elsewhere.

**Validation:** `tests/test_embed_backends.py` (9 tests) plus
`test_config.py` auto-detection coverage (mlx on Apple Silicon, vllm on
GPU machines, ollama on CPU machines).

**Result:** ✅ PASSED

### US-08 — 50 concurrent sessions stay correct and overlap

**Story:** As a performance engineer, I want evidence that 50 concurrent
orchestrated requests on the zero-infra stack all succeed with correct
tenant isolation and overlap in time (no serialization catastrophe).

**Acceptance:**
- GIVEN a seeded zero-infra stack (~2,000 chunks across two tenants —
  ms-scale scoring, the regime where the offload matters), WHEN 50
  cold, unique queries are run concurrently (alternating tenants), THEN
  every request returns SUCCESS with the full `timings_ms` schema, acme
  requests include the direct chunks and no foreign rubric, other-tenant
  requests return only their own rubric chunks and never acme content
  (isolation in both directions), AND concurrent wall time
  < 0.9 × serial wall time.

**Validation:** `tests/test_concurrency.py::test_50_concurrent_sessions_stay_correct_and_overlap`.

**Result:** ✅ PASSED

### US-09 — Standalone library contract is preserved

**Story:** As an integrator of this repo from another project (the voice
interviewer), I want enterprise-rag-core to remain a standalone library:
installable with its core extras, importable without backend SDKs, and
fully functional with zero infrastructure.

**Acceptance:**
- GIVEN the package installed, THEN `import enterprise_rag` succeeds with
  core deps only (httpx, pydantic, numpy, tokenizers, onnxruntime) — no
  qdrant/chromadb/elasticsearch/redis imports at module level.
- GIVEN `EngineConfig` with the zero-infra matrix, THEN `build_stack()`
  wires a working end-to-end stack (existing test coverage, re-run).
- GIVEN this Phase 0 diff, THEN it adds no new runtime dependencies
  (stdlib only) and no voice-framework imports.

**Validation:** `tests/test_config.py::test_zero_infra_stack_end_to_end`
plus the full pre-existing suite (88 tests) remaining green; dependency
audit by diff inspection (§9).

**Result:** ✅ PASSED

## 8. Deferred / known limits

- **BM25 rebuild is still O(n) per upsert** — acceptable for question-bank
  ingestion (few writes, big reads); an incremental index is a Phase 2
  optimization, not a Phase 0 blocker.
- **RedisVL cache construction probe** (`embed_sync` at
  `EnterpriseSemanticCache` construction) runs once per (tenant, schema)
  key at first access — one-time, not per request; the request path
  (`acheck`/`astore`) is already async.
- **Qdrant and Elasticsearch adapters** use async SDKs — nothing to
  offload; their concurrency behavior is inherited from the pinned
  clients.
- **list_tenants on Qdrant/Chroma** reads all payload metadata — fine at
  admin/boot time, not a hot-path operation.

## 9. Change inventory

| File | Change |
|---|---|
| `enterprise_rag/adapters/chroma_vector.py` | `to_thread` offload on all 5 SDK calls; `list_tenants` |
| `enterprise_rag/adapters/bm25_memory.py` | `to_thread` on `_score` and `_rebuild` |
| `enterprise_rag/adapters/memory_vector.py` | `to_thread` on cosine scoring; `list_tenants` |
| `enterprise_rag/adapters/qdrant_vector.py` | `list_tenants` + pure `_distinct_tenant_ids` helper |
| `enterprise_rag/adapters/protocols.py` | `VectorStore.list_tenants` protocol growth |
| `enterprise_rag/cache.py` | `to_thread` on `InMemorySemanticCache.get` lookup |
| `enterprise_rag/orchestrator.py` | rerank via `to_thread`; `timings_ms` instrumentation |
| `enterprise_rag/warmup.py` | `tenants` scope parameter (`None`/`"all"`/list) |
| `enterprise_rag/cli.py` | `RAG_CORE_WARM_KEYWORD=all` wiring in serve/serve-stdio |
| `enterprise_rag/config.py` | `vllm`/`openai` embed backends; `RAG_CORE_EMBED_BASE_URL`; per-backend base-URL defaults; OS auto-configuration (`mlx` on Apple Silicon, `vllm` with NVIDIA GPU, `ollama` elsewhere) via stdlib probe |
| `enterprise_rag/hybrid.py` | keyword-only `transport`/`sync_transport` seams on both embedding clients |
| `tests/test_realtime_offload.py`, `tests/test_list_tenants.py`, `tests/test_embed_backends.py`, `tests/test_concurrency.py`, `tests/test_warm_keyword.py` | new/extended validation (13 new tests) |
| `tests/test_config.py`, `tests/test_ingestion.py`, `tests/test_prepopulate.py`, `tests/test_concurrency.py` | test stacks pin `embed_backend="ollama"` — suite is machine-independent (auto resolves mlx on macOS hosts) |
| `tests/test_mcp_boot.py` | removed legacy module-scope monkeypatching of `embed_sync`/`EnterpriseSemanticCache` that leaked session-wide (the refactored `server.py` no longer builds a stack at import) |
