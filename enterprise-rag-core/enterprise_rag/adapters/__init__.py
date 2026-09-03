"""Backend adapters. Import SDK-backed adapters explicitly — importing this
package does not import any backend SDK."""
from enterprise_rag.adapters.protocols import (
    EmbeddingClient,
    VectorStore,
    KeywordStore,
    SemanticCache,
    Reranker,
)
from enterprise_rag.adapters.memory_vector import InMemoryVectorStore
from enterprise_rag.adapters.bm25_memory import BM25KeywordStore
from enterprise_rag.adapters.none_keyword import NoOpKeywordStore

__all__ = [
    "EmbeddingClient",
    "VectorStore",
    "KeywordStore",
    "SemanticCache",
    "Reranker",
    "InMemoryVectorStore",
    "BM25KeywordStore",
    "NoOpKeywordStore",
]
