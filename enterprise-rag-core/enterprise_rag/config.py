"""Environment-driven engine configuration and stack construction.

This is the pluggability surface for consuming systems: one
``EngineConfig.from_env()`` + ``config.build_stack()`` yields a fully wired
retrieval stack (embeddings, vector leg, keyword leg, semantic cache, reranker,
engine, orchestrator) without importing any backend SDK at module import time.

Every backend SDK is imported lazily inside the branch that needs it, so a
system adopting the engine with zero or partial infrastructure never pays for
the others. All configuration flows through ``RAG_CORE_*`` environment
variables (see README "Backend matrix").
"""
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from enterprise_rag.security import SecurityContext

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_MODEL_PATH = _REPO_ROOT / "models" / "reranker" / "minilm-int8.onnx"

# Default OpenAI-compatible base URLs per embed backend. All three speak the
# same {"model": ..., "input": ...} -> data[0].embedding contract, so they
# share OpenAICompatibleEmbeddingClient; only the endpoint differs.
_EMBED_BASE_URL_DEFAULTS = {
    "mlx": "http://127.0.0.1:8000/v1",      # vllm-mlx / mlx-serve / mlx-omni-server
    "vllm": "http://127.0.0.1:8000/v1",     # vLLM embedding endpoint (--task embed)
    "openai": "https://api.openai.com/v1",  # any OpenAI-compatible API
}


@dataclass
class EngineConfig:
    """Configuration for one engine stack, resolved from ``RAG_CORE_*`` env
    vars (``OLLAMA_URL`` / ``EMBED_MODEL`` kept app-compatible)."""

    vector_backend: str = "chroma"          # qdrant | chroma | memory
    qdrant_url: str | None = None
    chroma_path: str | None = None          # default: ./chroma_data
    chroma_collection: str = "langchain"
    keyword_backend: str = "bm25"           # elasticsearch | bm25 | none
    es_url: str | None = None
    index: str = "rag-chunks"
    cache_backend: str = "none"             # redisvl | memory | none
    redis_url: str | None = None
    rerank_model_path: str | None = None    # default: repo models/reranker/minilm-int8.onnx
    auth_mode: str = "none"                 # none | oidc
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    resource_server_url: str = "http://127.0.0.1:8000/mcp"
    default_tenant: str = "default"
    default_clearance: int = 0
    embed_backend: str = "auto"             # auto | ollama | mlx | vllm | openai
    ollama_url: str | None = None
    mlx_base_url: str | None = None         # legacy mlx-specific base URL
    embed_base_url: str | None = None       # generic override for mlx/vllm/openai
    embed_model: str | None = None
    alpha: float = 0.3
    rrf_k: int = 60
    schema_version: str = "v1"

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "EngineConfig":
        env = os.environ if environ is None else environ

        def _get(key: str, default: str | None = None) -> str | None:
            return env.get(key, default)

        return cls(
            vector_backend=_get("RAG_CORE_VECTOR_BACKEND", "chroma") or "chroma",
            qdrant_url=_get("RAG_CORE_QDRANT_URL"),
            chroma_path=_get("RAG_CORE_CHROMA_PATH"),
            chroma_collection=_get("RAG_CORE_CHROMA_COLLECTION", "langchain") or "langchain",
            keyword_backend=_get("RAG_CORE_KEYWORD_BACKEND", "bm25") or "bm25",
            es_url=_get("RAG_CORE_ES_URL"),
            index=_get("RAG_CORE_INDEX", "rag-chunks") or "rag-chunks",
            cache_backend=_get("RAG_CORE_CACHE_BACKEND", "none") or "none",
            redis_url=_get("RAG_CORE_REDIS_URL"),
            rerank_model_path=_get("RAG_CORE_RERANK_MODEL_PATH"),
            auth_mode=_get("RAG_CORE_AUTH_MODE", "none") or "none",
            oidc_issuer=_get("RAG_CORE_OIDC_ISSUER"),
            oidc_audience=_get("RAG_CORE_OIDC_AUDIENCE"),
            default_tenant=_get("RAG_CORE_DEFAULT_TENANT", "default") or "default",
            default_clearance=int(_get("RAG_CORE_DEFAULT_CLEARANCE", "0") or 0),
            embed_backend=_get("RAG_CORE_EMBED_BACKEND", "auto") or "auto",
            ollama_url=_get("OLLAMA_URL"),
            mlx_base_url=_get("RAG_CORE_MLX_BASE_URL"),
            embed_base_url=_get("RAG_CORE_EMBED_BASE_URL"),
            embed_model=_get("EMBED_MODEL"),
        )

    def default_security(self) -> SecurityContext:
        """SecurityContext for ``none`` auth mode: every request runs as the
        configured default tenant with the configured clearance. Deny-by-default
        semantics otherwise untouched (no roles, no department locks)."""
        return SecurityContext(
            principal_id="default",
            tenant_id=self.default_tenant,
            roles=[],
            departments=[],
            clearance_level=self.default_clearance,
            allowed_groups=[],
        )

    def build_stack(self) -> "Stack":
        """Constructs the full retrieval stack for this configuration.

        Raises ValueError for unknown backends or missing required URLs.
        """
        from enterprise_rag.adapters.bm25_memory import BM25KeywordStore
        from enterprise_rag.adapters.memory_vector import InMemoryVectorStore
        from enterprise_rag.adapters.none_keyword import NoOpKeywordStore
        from enterprise_rag.cache import (
            InMemorySemanticCache,
            MultiTenantSemanticCache,
            NoOpSemanticCache,
        )
        from enterprise_rag.hybrid import (
            AsyncParallelHybridEngine,
            OllamaEmbeddingClient,
            OpenAICompatibleEmbeddingClient,
        )
        from enterprise_rag.orchestrator import AtomicAgentContextOrchestrator
        from enterprise_rag.reranker import NoOpReranker, ONNXVoiceReranker

        embed_backend = self.embed_backend.lower()
        if embed_backend == "auto":
            embed_backend = _detect_embed_backend()
        if embed_backend == "ollama":
            embeddings = OllamaEmbeddingClient(
                base_url=self.ollama_url, model=self.embed_model,
            )
        elif embed_backend in ("mlx", "vllm", "openai"):
            # mlx-lm serves chat only; embeddings come from an OpenAI-compatible
            # embedding server (vllm-mlx, mlx-serve, mlx-omni-server, ...) — the
            # exact /v1/embeddings contract vLLM's embedding endpoint and the
            # OpenAI API itself speak, so all three share one client.
            if not self.embed_model:
                raise ValueError(
                    f"EMBED_MODEL is required for embed_backend={self.embed_backend!r}"
                )
            embeddings = OpenAICompatibleEmbeddingClient(
                base_url=self.embed_base_url
                or (self.mlx_base_url if embed_backend == "mlx" else None)
                or _EMBED_BASE_URL_DEFAULTS[embed_backend],
                model=self.embed_model,
            )
        else:
            raise ValueError(f"unknown embed_backend: {self.embed_backend!r}")

        clients: list[Any] = []

        # ── vector leg ────────────────────────────────────────────────────
        vector_backend = self.vector_backend.lower()
        if vector_backend == "memory":
            vector_store = InMemoryVectorStore()
        elif vector_backend == "qdrant":
            if not self.qdrant_url:
                raise ValueError("RAG_CORE_QDRANT_URL is required for vector_backend=qdrant")
            from qdrant_client import AsyncQdrantClient
            from enterprise_rag.adapters.qdrant_vector import QdrantVectorStore

            qdrant = AsyncQdrantClient(url=self.qdrant_url)
            clients.append(qdrant)
            vector_store = QdrantVectorStore(qdrant, collection=self.index)
        elif vector_backend == "chroma":
            import chromadb
            from enterprise_rag.adapters.chroma_vector import ChromaVectorStore

            client = chromadb.PersistentClient(
                path=self.chroma_path or str(_REPO_ROOT / "chroma_data")
            )
            collection = client.get_or_create_collection(name=self.chroma_collection)
            vector_store = ChromaVectorStore(collection)
        else:
            raise ValueError(f"unknown vector_backend: {self.vector_backend!r}")

        # ── keyword leg ───────────────────────────────────────────────────
        keyword_backend = self.keyword_backend.lower()
        if keyword_backend == "bm25":
            keyword_store = BM25KeywordStore()
        elif keyword_backend == "none":
            keyword_store = NoOpKeywordStore()
        elif keyword_backend == "elasticsearch":
            if not self.es_url:
                raise ValueError("RAG_CORE_ES_URL is required for keyword_backend=elasticsearch")
            from elastic_transport import HttpxAsyncHttpNode   # stay on the pinned httpx client
            from elasticsearch import AsyncElasticsearch
            from enterprise_rag.adapters.elasticsearch_keyword import ElasticsearchKeywordStore

            es = AsyncElasticsearch(self.es_url, node_class=HttpxAsyncHttpNode)
            clients.append(es)
            keyword_store = ElasticsearchKeywordStore(es, index=self.index)
        else:
            raise ValueError(f"unknown keyword_backend: {self.keyword_backend!r}")

        # ── semantic cache ────────────────────────────────────────────────
        cache_backend = self.cache_backend.lower()
        if cache_backend == "none":
            cache = NoOpSemanticCache()
        elif cache_backend == "memory":
            cache = InMemorySemanticCache()
        elif cache_backend == "redisvl":
            if not self.redis_url:
                raise ValueError("RAG_CORE_REDIS_URL is required for cache_backend=redisvl")
            cache = MultiTenantSemanticCache(self.redis_url, embedding_fn=embeddings.embed_sync)
        else:
            raise ValueError(f"unknown cache_backend: {self.cache_backend!r}")

        # ── reranker ──────────────────────────────────────────────────────
        model_path = Path(self.rerank_model_path) if self.rerank_model_path else _DEFAULT_MODEL_PATH
        if model_path.is_file():
            reranker = ONNXVoiceReranker(str(model_path))
        else:
            reranker = NoOpReranker()   # model not downloaded — keep fused order

        # ── engine + orchestrator ─────────────────────────────────────────
        engine = AsyncParallelHybridEngine(
            vector_store, keyword_store, embeddings,
            alpha=self.alpha, rrf_k=self.rrf_k,
        )
        orchestrator = AtomicAgentContextOrchestrator(
            engine, reranker, cache, vector_store, schema_version=self.schema_version,
        )
        return Stack(
            config=self,
            embeddings=embeddings,
            vector_store=vector_store,
            keyword_store=keyword_store,
            cache=cache,
            reranker=reranker,
            engine=engine,
            orchestrator=orchestrator,
            clients=clients,
        )


def _detect_embed_backend() -> str:
    """Machine-aware embedding backend (OS auto-configuration). The explicit
    ``RAG_CORE_EMBED_BACKEND`` always wins; ``auto`` resolves by machine class:

    - macOS Apple Silicon -> ``mlx`` (mlx-lm native; CUDA unavailable)
    - NVIDIA GPU present   -> ``vllm`` (CUDA-native OpenAI-compatible server)
    - otherwise            -> ``ollama`` (CPU-friendly, runs everywhere)
    """
    import platform
    import sys

    if sys.platform == "darwin" and platform.machine() == "arm64":
        return "mlx"
    if _has_nvidia_gpu():
        return "vllm"
    return "ollama"


def _has_nvidia_gpu() -> bool:
    """Stdlib NVIDIA presence probe: the Linux driver procfs marker or
    ``nvidia-smi`` on PATH. Never raises — an absent probe means "no GPU",
    and an explicit ``RAG_CORE_EMBED_BACKEND`` overrides the guess."""
    import os
    import shutil

    if os.path.exists("/proc/driver/nvidia/version"):
        return True
    return shutil.which("nvidia-smi") is not None


@dataclass
class Stack:
    """A fully wired retrieval stack. ``aclose()`` closes SDK clients that own
    network connections (Qdrant, Elasticsearch); the rest need nothing."""

    config: EngineConfig
    embeddings: Any
    vector_store: Any
    keyword_store: Any
    cache: Any
    reranker: Any
    engine: Any
    orchestrator: Any
    clients: list[Any] = field(default_factory=list)

    async def aclose(self) -> None:
        for client in self.clients:
            close = getattr(client, "close", None)
            if close is None:
                continue
            result = close()
            import inspect
            if inspect.isawaitable(result):
                await result
