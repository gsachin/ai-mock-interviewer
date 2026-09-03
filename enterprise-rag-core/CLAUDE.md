# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Standalone, pluggable Enterprise RAG/MCP Core Engine: hybrid retrieval (vector + keyword legs), weighted RRF fusion, ONNX INT8 reranking, tenant-scoped semantic cache, PDF/markdown ingestion, and an MCP server (OIDC or no-auth).

Implemented from the verified design blueprint `docs/TRD_ENTERPRISE_RAG_MCP_CORE.md` (v2026.5) — code comments reference its sections (§1–§6), and it is the authority for design intent. `docs/ent-solution-status.md` tracks done/pending work; `docs/ent-chat-context.md` has handover context.

## Commands

Windows-first repo: venv at repo root `.venv`; scripts under `.venv/Scripts/` (use `.venv/bin/` on macOS/Linux).

```bash
.venv/Scripts/pip install -e ".[dev]"             # install; dev extras = full test deps (pinned in requirements-dev.txt)
.venv/Scripts/enterprise-rag-core download-model  # INT8 reranker ONNX (22 MiB, gitignored); reranker tests skip without it

# Tests (no linter/formatter is configured)
.venv/Scripts/python -m pytest tests/             # full suite; redis-/ollama-/model-dependent tests auto-skip without their infra
.venv/Scripts/python -m pytest tests/test_hybrid.py::test_fuse_wrrf   # single test
.venv/Scripts/python -m pytest tests/ -m redis    # redis-marked only: needs Redis Stack at $REDIS_STACK_URL (default redis://localhost:6379)
docker run -d --name rag-redis-stack -p 6379:6379 redis/redis-stack-server:latest   # Redis Stack for redis tests

# Run
.venv/Scripts/enterprise-rag-core serve           # MCP streamable HTTP on :8000/mcp
.venv/Scripts/enterprise-rag-core serve-stdio     # stdio transport (stdio has no auth layer — use default RAG_CORE_AUTH_MODE=none)
.venv/Scripts/enterprise-rag-core ingest --pdf doc.pdf --doc-id hr-policy --tenant acme --department hr --clearance 3
.venv/Scripts/enterprise-rag-core prepopulate --kb kb.md --doc-id meridian-kb --tenant default --required-marker "…" --blocked-marker "…"
.\start_services.ps1                              # Windows one-shot launcher (venv self-heal + Redis + serve :8010)
```

Test markers: `redis` and `ollama` auto-skip when the service is unreachable (conftest probes ports). Model-dependent reranker tests skip until `download-model` has run.

## Architecture

### Backend pluggability (the core design)

Every backend is optional: `import enterprise_rag` must never import a backend SDK (Qdrant, ChromaDB, Elasticsearch, Redis) — SDKs are imported lazily inside the branch/class that needs them. Contracts live in `enterprise_rag/adapters/protocols.py` (`VectorStore`, `KeywordStore`, `SemanticCache`, `Reranker`, `EmbeddingClient`). Each adapter translates the same security rules into its own dialect (Qdrant `Filter`, ES `bool.filter`, Chroma `where`, or post-filter via `SecurityContext.matches`) — parity across all backends is a hard invariant.

`EngineConfig.from_env()` (all `RAG_CORE_*` env vars; exceptions: `OLLAMA_URL` / `EMBED_MODEL` keep app-compatible names) + `config.build_stack()` wires embeddings, vector leg, keyword leg, cache, reranker, engine, and orchestrator into a `Stack` dataclass. `build_stack` raises `ValueError` for unknown backends or missing required URLs. SDK clients that own connections (Qdrant, ES) are registered in `stack.clients`; long-running servers `await stack.aclose()` on shutdown.

Zero-infra mode (defaults): chroma vector (persistent, `./chroma_data`), in-memory BM25 keyword, no cache, no reranker model. Embeddings `auto` = OS auto-configuration: MLX on macOS Apple Silicon, vLLM where an NVIDIA GPU is detected (stdlib probe), Ollama elsewhere; explicit `RAG_CORE_EMBED_BACKEND` wins (`ollama | mlx | vllm | openai`, the last three share the OpenAI-compatible `/v1/embeddings` client).

### Security (design doc §1)

`SecurityContext` is a frozen dataclass — the single source of truth for identity-derived authorization, deny-by-default (a missing `tenant_id` maps to `""`, which matches no chunk in any backend). Empty `departments` = NOT department-locked; only a non-empty list restricts. Qdrant subtlety (regression-tested in `tests/test_qdrant_filters.py`): an empty `should` clause is unsatisfiable, so the adapter must omit it rather than pass an empty list.

### Retrieval pipeline

`AsyncParallelHybridEngine.retrieve_parallel` fans the dense (vector) and sparse (keyword) legs out concurrently with `asyncio.gather`, then `fuse_wrrf` merges them by weighted RRF on ranks (alpha=0.3, k=60) — never raw-score normalization. Accepts a precomputed `query_vector` so cache-checking callers don't embed twice.

`AtomicAgentContextOrchestrator.execute_agent_context`: (1) direct context injections fetched by deterministic chunk ids (`resume:current`, `jd:target`) via `get_by_ids` — an id lookup WITHOUT filters, so every adapter re-verifies tenant post-fetch (never trust ids alone); (2) rubric retrieval gated by the semantic cache, which stores rubric provenance ONLY — direct chunks vary per request, so caching the full envelope would poison responses across candidates; (3) rerank the pool; (4) U-shape `ContextFormatter` envelope. Truncation happens only at formatting time; retrieval keeps full chunks.

`Chunk` / `UpsertRecord` are frozen dataclasses; score updates create new instances via `dataclasses.replace`.

Realtime-readiness (Phase 0, see `docs/TRD_PHASE0_REALTIME_READINESS.md`): blocking work never runs on the event loop — Chroma SDK calls, BM25 scoring/rebuild, memory-vector/cache cosine, and ONNX rerank all go through `asyncio.to_thread`; `execute_agent_context` returns per-stage `timings_ms`. `VectorStore.list_tenants()` exists on all vector backends. Test stacks pin `embed_backend="ollama"` so the suite is machine-independent (auto resolves mlx on macOS hosts).

### MCP server (design doc §6)

Two auth modes: `oidc` (RS256 JWKS verification, required scope `rag:retrieve`, `SecurityContext` derived from JWT claims) or `none` (runs as configured default tenant/clearance). Tools: `execute_agent_context`, generic `retrieve_context`, and the Phase 1 interview tools — `interview_bank` / `interview_question` (deterministic question-bank fetch, helpers in `enterprise_rag/interview.py`) and `interview_followup` (domain-scoped hybrid, narrowing semantics in OIDC). `build_app()` builds the stack and wires it through module-level seams (`server.agent_orchestrator` / `server.agent_engine` / `server.agent_vector_store`) that tests replace — test fixtures must save/restore the seams (a leaked real orchestrator breaks `test_mcp_boot`); the stack is attached as `app.state.stack`. The module-scope `mcp = build_mcp()` at import is intentionally cheap — no stack, no SDK clients.

### Ingestion & prepopulation

- PDF ingestion (`enterprise_rag/ingestion/`): pypdf text + table extraction (heuristic: ≥2 aligned-space columns with a consistent column count), dual representation — structured JSON for precise lookup + markdown matrix for embedding.
- `prepopulate`: markdown corpus split on `## ` headings; deterministic chunk ids `{doc_id}:s{section}:c{chunk}`; marker gates (`--required-marker` / `--blocked-marker`); idempotent via `get_all` (reruns skip unless `--force`, which does `delete_by_parent` + re-upsert).
- BM25 warm-up (`warmup.py`): the in-memory BM25 leg starts empty in every fresh process, so boot (CLI `serve`, launcher) repopulates it from the persisted vector store via `get_all` — env-gated by `RAG_CORE_WARM_KEYWORD` (`0` off, `1` default tenant, `all` every tenant via `list_tenants`).

## Conventions

- Every SDK/dependency claim is verified against the real pinned package in `requirements-dev.txt`, never from memory; re-verify before adopting newer releases (see TRD §Pinned Versions).
- Commits are gated on a green test suite.
- Design-intent questions are resolved against `docs/TRD_ENTERPRISE_RAG_MCP_CORE.md`, not guessed.
- CI (`.github/workflows/ci.yml`): pytest on ubuntu + windows, Python 3.11/3.12; Redis Stack started via `docker run` under `shell: bash` so the `||` skip semantics behave identically on Windows runners (which can't use service containers — the redis tests auto-skip there).
