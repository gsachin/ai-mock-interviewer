# enterprise-rag-core — Chat Context (Session Handover)

**Repo:** https://github.com/gsachin/enterprise-rag-core
**Origin:** extracted from the `universityDemo` repo (`D:\project\universityDemo`,
branch `windows-ps1-updated-to-support-and-setup-dev-env-from-scrach`), where the
TRD/LLD blueprint was authored and verified against pinned SDKs.

## 1. What this project is

A standalone, pluggable **Enterprise RAG/MCP Core Engine**: hybrid retrieval
(vector + keyword legs), weighted RRF fusion, ONNX INT8 reranking,
tenant-scoped semantic cache, PDF/table ingestion (dual representation), and an
MCP server (OIDC or no-auth). Built for the voice mock-interview scenario
(resume + JD + rubric retrieval) but generalized: any system can adopt it as a
library (`EngineConfig.from_env()` / `build_stack()`) or run it as an MCP
server (`serve` / `serve-stdio`).

## 2. History in brief

1. **Blueprint** (`universityDemo`, Aug 2026): `TRD_ENTERPRISE_RAG_MCP_CORE.md`
   v2026.5 — every code block executed/verified against pinned SDKs
   (mcp 2.1.1, qdrant-client 1.19.0, elasticsearch 9.5.0, redisvl 0.26.0,
   tokenizers 0.23.1, pypdf 6.14.2, onnxruntime 1.29.0).
2. **Extraction** (this repo, Aug 28–29 2026): implemented in 6 gated phases
   (see `docs/ent-solution-status.md`). The phased commit history was squashed
   by the repo owner into the public `first commit` (byte-identical tree).
3. **Migration** (Aug 29 2026): docs moved here, LICENSE added, CI enabled;
   `universityDemo` retains only a pointer.

## 3. Key conventions

- **Every SDK claim is verified against the real pinned package, never from
  memory** — new dependencies get a runnable check, not an assumption.
- Phase commits carry a green test gate; never commit a red suite.
- Backend SDKs are optional extras and imported lazily; `import enterprise_rag`
  must never require Qdrant/ES/Chroma/Redis.
- SecurityContext is the single source of truth for authorization; every
  backend adapter enforces tenant + clearance + department parity.

## 4. Where things live

| Thing | Location |
|---|---|
| Blueprint TRD/LLD v2026.5 | `docs/TRD_ENTERPRISE_RAG_MCP_CORE.md` |
| Status / pending work | `docs/ent-solution-status.md` |
| Package | `enterprise_rag/` |
| Tests (69) | `tests/` |
| Pinned dev deps | `requirements-dev.txt` |
| Infra | `docker-compose.yml` (Qdrant, ES, Redis Stack, Ollama) |
| Original docs (pointer only) | `universityDemo/doc/enterprizesolutions/` |
