"""enterprise-rag-core — standalone, pluggable Enterprise RAG/MCP Core Engine.

Hybrid retrieval (vector + keyword legs), weighted RRF fusion, ONNX reranking,
tenant-scoped semantic cache, document ingestion, and an MCP server.

Imports are lazy with respect to backend SDKs: importing this package only
pulls in the core dependencies (httpx, pydantic, numpy, tokenizers,
onnxruntime). SDK-backed adapters (``enterprise_rag.adapters.qdrant_vector``,
``chroma_vector``, ``elasticsearch_keyword``) and the RedisVL cache are
imported explicitly when their extras are installed.
"""
from enterprise_rag.model import Chunk, UpsertRecord
from enterprise_rag.security import SecurityContext
from enterprise_rag.hybrid import OllamaEmbeddingClient, fuse_wrrf, AsyncParallelHybridEngine
from enterprise_rag.formatter import ContextFormatter
from enterprise_rag.orchestrator import AgentContextRequest, AtomicAgentContextOrchestrator
from enterprise_rag.reranker import ONNXVoiceReranker, NoOpReranker
from enterprise_rag.cache import (
    EnterpriseSemanticCache,
    MultiTenantSemanticCache,
    InMemorySemanticCache,
    NoOpSemanticCache,
)

__version__ = "0.1.0"

__all__ = [
    "Chunk",
    "UpsertRecord",
    "SecurityContext",
    "OllamaEmbeddingClient",
    "fuse_wrrf",
    "AsyncParallelHybridEngine",
    "ContextFormatter",
    "AgentContextRequest",
    "AtomicAgentContextOrchestrator",
    "ONNXVoiceReranker",
    "NoOpReranker",
    "EnterpriseSemanticCache",
    "MultiTenantSemanticCache",
    "InMemorySemanticCache",
    "NoOpSemanticCache",
]
