"""EngineConfig: env parsing, default_security, and hermetic build_stack
coverage for every zero-infrastructure backend combination. SDK-backed
branches (chroma) are exercised against the real client with a temp dir;
Qdrant/Elasticsearch/Redis branches are checked for their config guards only
(live servers are out of scope here — see the adapter tests)."""
import asyncio

import pytest

from enterprise_rag.config import EngineConfig
from enterprise_rag.hybrid import OllamaEmbeddingClient
from enterprise_rag.model import UpsertRecord
from enterprise_rag.orchestrator import AgentContextRequest
from enterprise_rag.security import SecurityContext


# ── env parsing ───────────────────────────────────────────────────────────

def test_from_env_reads_rag_core_matrix(monkeypatch):
    env = {
        "RAG_CORE_VECTOR_BACKEND": "memory",
        "RAG_CORE_KEYWORD_BACKEND": "bm25",
        "RAG_CORE_CACHE_BACKEND": "memory",
        "RAG_CORE_INDEX": "idx",
        "RAG_CORE_DEFAULT_TENANT": "acme",
        "RAG_CORE_DEFAULT_CLEARANCE": "4",
        "RAG_CORE_AUTH_MODE": "oidc",
        "RAG_CORE_OIDC_ISSUER": "https://iam.example.com/oauth2",
        "RAG_CORE_OIDC_AUDIENCE": "enterprise-rag-core",
        "RAG_CORE_EMBED_BACKEND": "mlx",
        "RAG_CORE_MLX_BASE_URL": "http://127.0.0.1:8080/v1",
        "OLLAMA_URL": "http://ollama:11434",
        "EMBED_MODEL": "nomic-embed-text",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    cfg = EngineConfig.from_env()
    assert cfg.vector_backend == "memory"
    assert cfg.keyword_backend == "bm25"
    assert cfg.cache_backend == "memory"
    assert cfg.index == "idx"
    assert cfg.default_tenant == "acme"
    assert cfg.default_clearance == 4
    assert cfg.auth_mode == "oidc"
    assert cfg.oidc_issuer == env["RAG_CORE_OIDC_ISSUER"]
    assert cfg.oidc_audience == env["RAG_CORE_OIDC_AUDIENCE"]
    assert cfg.ollama_url == "http://ollama:11434"
    assert cfg.embed_model == "nomic-embed-text"
    assert cfg.embed_backend == "mlx"
    assert cfg.mlx_base_url == "http://127.0.0.1:8080/v1"


def test_from_env_defaults():
    cfg = EngineConfig.from_env(environ={})
    assert cfg.vector_backend == "chroma"
    assert cfg.keyword_backend == "bm25"
    assert cfg.cache_backend == "none"
    assert cfg.auth_mode == "none"
    assert cfg.embed_backend == "auto"      # machine-aware resolution at build time
    assert cfg.default_tenant == "default"
    assert cfg.default_clearance == 0


def test_default_security():
    cfg = EngineConfig(default_tenant="acme", default_clearance=3)
    sec = cfg.default_security()
    assert sec.tenant_id == "acme"
    assert sec.clearance_level == 3
    assert sec.departments == []     # no department locks in none mode


# ── build_stack: backends and guards ──────────────────────────────────────

def test_build_stack_rejects_unknown_backends():
    with pytest.raises(ValueError, match="vector_backend"):
        EngineConfig(vector_backend="sphinx", embed_backend="ollama").build_stack()
    with pytest.raises(ValueError, match="keyword_backend"):
        EngineConfig(keyword_backend="sphinx", embed_backend="ollama").build_stack()
    with pytest.raises(ValueError, match="cache_backend"):
        EngineConfig(cache_backend="sphinx", embed_backend="ollama").build_stack()
    with pytest.raises(ValueError, match="embed_backend"):
        EngineConfig(embed_backend="sphinx").build_stack()


def test_build_stack_mlx_requires_embed_model():
    with pytest.raises(ValueError, match="EMBED_MODEL"):
        EngineConfig(embed_backend="mlx").build_stack()


def test_build_stack_auto_detects_mlx_on_apple_silicon(monkeypatch):
    import platform
    import sys

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    stack = EngineConfig(
        embed_backend="auto", embed_model="bge-small",
        vector_backend="memory", keyword_backend="none", cache_backend="none",
        rerank_model_path="definitely/not/here.onnx",
    ).build_stack()
    assert stack.embeddings.__class__.__name__ == "OpenAICompatibleEmbeddingClient"


def test_build_stack_auto_detects_ollama_on_cpu_machines(monkeypatch):
    import platform
    import sys

    monkeypatch.setattr(sys, "platform", "linux")     # CPU machine
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    monkeypatch.setattr("enterprise_rag.config._has_nvidia_gpu", lambda: False)
    stack = EngineConfig(
        embed_backend="auto",
        vector_backend="memory", keyword_backend="none", cache_backend="none",
        rerank_model_path="definitely/not/here.onnx",
    ).build_stack()
    assert stack.embeddings.__class__.__name__ == "OllamaEmbeddingClient"


def test_build_stack_auto_detects_vllm_on_gpu_machines(monkeypatch):
    import platform
    import sys

    monkeypatch.setattr(sys, "platform", "linux")     # CUDA machine
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    monkeypatch.setattr("enterprise_rag.config._has_nvidia_gpu", lambda: True)
    from enterprise_rag.hybrid import OpenAICompatibleEmbeddingClient

    stack = EngineConfig(
        embed_backend="auto", embed_model="BAAI/bge-small-en-v1.5",
        vector_backend="memory", keyword_backend="none", cache_backend="none",
        rerank_model_path="definitely/not/here.onnx",
    ).build_stack()
    assert isinstance(stack.embeddings, OpenAICompatibleEmbeddingClient)
    assert stack.embeddings._base_url == "http://127.0.0.1:8000/v1"   # vLLM default


def test_build_stack_mlx_selects_openai_compatible_client():
    from enterprise_rag.hybrid import OpenAICompatibleEmbeddingClient

    stack = EngineConfig(
        embed_backend="mlx", embed_model="BAAI/bge-small-en-v1.5",
        vector_backend="memory", keyword_backend="none", cache_backend="none",
        rerank_model_path="definitely/not/here.onnx",
    ).build_stack()
    assert isinstance(stack.embeddings, OpenAICompatibleEmbeddingClient)
    assert stack.embeddings._base_url == "http://127.0.0.1:8000/v1"
    assert stack.embeddings._model == "BAAI/bge-small-en-v1.5"

    stack2 = EngineConfig(
        embed_backend="mlx", embed_model="m", mlx_base_url="http://127.0.0.1:9999/v1",
        vector_backend="memory", keyword_backend="none", cache_backend="none",
        rerank_model_path="definitely/not/here.onnx",
    ).build_stack()
    assert stack2.embeddings._base_url == "http://127.0.0.1:9999/v1"


def test_build_stack_requires_urls_for_sdk_backends():
    with pytest.raises(ValueError, match="RAG_CORE_QDRANT_URL"):
        EngineConfig(vector_backend="qdrant", embed_backend="ollama").build_stack()
    with pytest.raises(ValueError, match="RAG_CORE_ES_URL"):
        EngineConfig(keyword_backend="elasticsearch", embed_backend="ollama").build_stack()
    with pytest.raises(ValueError, match="RAG_CORE_REDIS_URL"):
        EngineConfig(cache_backend="redisvl", embed_backend="ollama").build_stack()


def test_build_stack_chroma_constructs_collection(tmp_path):
    stack = EngineConfig(
        vector_backend="chroma", chroma_path=str(tmp_path), embed_backend="ollama",
    ).build_stack()
    assert stack.vector_store.__class__.__name__ == "ChromaVectorStore"
    asyncio.run(stack.aclose())


def test_build_stack_no_model_falls_back_to_noop_reranker(monkeypatch):
    cfg = EngineConfig(
        vector_backend="memory", keyword_backend="none", cache_backend="none",
        rerank_model_path="definitely/not/here.onnx",
        embed_backend="ollama",     # machine-independent: auto resolves mlx on macOS
    )
    stack = cfg.build_stack()
    assert stack.reranker.__class__.__name__ == "NoOpReranker"
    asyncio.run(stack.aclose())


# ── end-to-end over the built stack (hermetic: stubbed embedder) ──────────

def test_zero_infra_stack_end_to_end(monkeypatch):
    async def fake_embed(self, text):
        # deterministic pseudo-embedding: first chars seed a unit vector
        v = [0.0] * 8
        for i, ch in enumerate(text[:8]):
            v[i % 8] += (ord(ch) % 5 + 1) / 10.0
        return v

    monkeypatch.setattr(OllamaEmbeddingClient, "embed", fake_embed)
    cfg = EngineConfig(
        vector_backend="memory", keyword_backend="bm25", cache_backend="memory",
        default_tenant="acme", rerank_model_path="definitely/not/here.onnx",
        embed_backend="ollama",     # machine-independent: auto resolves mlx on macOS
    )
    stack = cfg.build_stack()

    async def run():
        def rec(chunk_id, parent, text, clearance=0):
            vec = [0.0] * 8
            vec[hash(chunk_id) % 8] = 1.0
            return UpsertRecord(
                chunk_id=chunk_id, parent_id=parent, tenant_id="acme",
                content=text, section_title="s", required_clearance=clearance,
                department=None, vector=vec,
            )

        r1 = rec("resume:current", "resume", "Jane engineers python platforms daily")
        r2 = rec("jd:target", "jd", "platform engineering python required")
        await stack.vector_store.upsert([r1, r2])
        await stack.keyword_store.upsert([r1, r2])

        sec = cfg.default_security()
        out = await stack.orchestrator.execute_agent_context(AgentContextRequest(
            sec_ctx=sec, resume_text="Jane", job_description="platform",
            conversation_history=[], rubric_query="python platform engineering",
        ))
        assert out["status"] == "SUCCESS"
        assert "tenant=acme" in out["context_envelope"]
        # direct context injection picked up both deterministic ids
        direct_ids = {p["chunk_id"] for p in out["provenance"] if p["source"] in ("resume", "jd")}
        assert direct_ids == {"resume:current", "jd:target"}, direct_ids

        # cross-tenant: same corpus, different tenant -> no provenance
        other = SecurityContext("u2", "other", [], [], 9, [])
        out2 = await stack.orchestrator.execute_agent_context(AgentContextRequest(
            sec_ctx=other, resume_text="Jane", job_description="platform",
            conversation_history=[], rubric_query="python",
        ))
        assert out2["provenance"] == [], out2["provenance"]

    asyncio.run(run())
