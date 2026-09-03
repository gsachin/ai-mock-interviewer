"""§2 Low-Latency Hybrid Retrieval — weighted RRF over pluggable legs.

The engine fans the dense (VectorStore) and sparse (KeywordStore) legs out
concurrently; latency is max(leg) + fusion overhead. Security filters are the
adapters' responsibility on BOTH legs (design doc §1: identical, unbypassable).
"""
import asyncio
import json
from dataclasses import replace

import httpx

from enterprise_rag.model import Chunk
from enterprise_rag.security import SecurityContext
from enterprise_rag.adapters.protocols import EmbeddingClient, VectorStore, KeywordStore


# ── Embedding (real call, budgeted at <= 6 ms p95) ─────────────────────

class OllamaEmbeddingClient:
    """Concrete default: Ollama nomic-embed-text (mirrors EMBED_MODEL of the
    repository this design is archived in)."""

    def __init__(self, base_url: str | None = None, model: str | None = None, *,
                 transport: httpx.AsyncBaseTransport | None = None,
                 sync_transport: httpx.BaseTransport | None = None):
        import os

        self._base_url = base_url or os.environ.get("OLLAMA_URL", "http://localhost:11434")
        self._model = model or os.environ.get("EMBED_MODEL", "nomic-embed-text")
        # Transport seams are test-only hooks (httpx.MockTransport); production
        # passes nothing and gets default transports.
        self._transport = transport
        self._sync_transport = sync_transport

    async def embed(self, text: str) -> list[float]:
        # target: <= 6 ms p95 (warm model, local network)
        vector = await self._embed_request(text)
        return vector

    async def _embed_request(self, text: str, *, attempts: int = 3) -> list[float]:
        # Ollama unloads idle models (keep_alive) and returns
        # {"embedding": []} with HTTP 200 while the runner reloads — retry with
        # backoff across the reload window. An empty vector would otherwise die
        # deep inside a vector-store SDK (observed live: IndexError in chromadb).
        # ReadTimeout also retries: a shared Ollama instance serving a chat
        # model alongside the embedding model can queue embeds past the 5s
        # budget under memory pressure (observed live with llama3.2:3b).
        if not text:
            raise ValueError("embed prompt must not be empty (Ollama returns [] for empty prompts)")
        for attempt in range(attempts):
            try:
                async with httpx.AsyncClient(timeout=15.0, transport=self._transport) as client:
                    resp = await client.post(
                        f"{self._base_url}/api/embeddings",
                        json={"model": self._model, "prompt": text},
                    )
                    resp.raise_for_status()
                body = resp.json()
            except httpx.ReadTimeout:
                if attempt < attempts - 1:
                    await asyncio.sleep(0.3 * (attempt + 1))
                    continue
                raise
            vector = body.get("embedding") or []
            if vector:
                return vector
            if attempt < attempts - 1:
                await asyncio.sleep(0.3 * (attempt + 1))
        snippet = json.dumps(body)[:200] if isinstance(body, dict) else repr(body)[:200]
        raise ValueError(
            f"empty embedding from {self._base_url} for model {self._model!r}"
            f" after {attempts} attempts — response: {snippet}"
        )

    def embed_sync(self, text: str) -> list[float]:
        """Sync variant — for components that must probe vector dimensions at
        construction time (the §5 RedisVL CustomVectorizer). Loop-independent.
        Same empty-embedding retry as the async path (the Ollama reload window
        hits dimension probes too)."""
        import time

        for attempt in range(3):
            with httpx.Client(timeout=5.0, transport=self._sync_transport) as client:
                resp = client.post(
                    f"{self._base_url}/api/embeddings",
                    json={"model": self._model, "prompt": text},
                )
                resp.raise_for_status()
            vector = resp.json().get("embedding") or []
            if vector:
                return vector
            if attempt < 2:
                time.sleep(0.3 * (attempt + 1))
        raise ValueError(f"empty embedding from {self._base_url} for model {self._model!r}")


class OpenAICompatibleEmbeddingClient:
    """Embeddings from an OpenAI-compatible ``/v1/embeddings`` endpoint.

    This is the MLX backend on Apple Silicon: mlx-lm serves chat only, so
    embeddings come from a separate MLX embedding server (vllm-mlx,
    mlx-serve, mlx-omni-server, ...) — all of which speak the OpenAI
    ``{"model": ..., "input": ...}`` -> ``data[0].embedding`` shape. Works
    with any other OpenAI-compatible embeddings endpoint too.
    """

    def __init__(self, base_url: str, model: str, *,
                 transport: httpx.AsyncBaseTransport | None = None,
                 sync_transport: httpx.BaseTransport | None = None):
        self._base_url = base_url.rstrip("/")     # e.g. http://127.0.0.1:8000/v1
        self._model = model
        # Transport seams are test-only hooks (httpx.MockTransport); production
        # passes nothing and gets default transports.
        self._transport = transport
        self._sync_transport = sync_transport

    async def embed(self, text: str) -> list[float]:
        if not text:
            raise ValueError("embed prompt must not be empty")
        async with httpx.AsyncClient(timeout=10.0, transport=self._transport) as client:
            resp = await client.post(
                f"{self._base_url}/embeddings",
                json={"model": self._model, "input": text},
            )
            resp.raise_for_status()
        data = (resp.json().get("data") or [])
        if not data or not data[0].get("embedding"):
            raise ValueError(f"empty embedding from {self._base_url} for model {self._model!r}")
        return list(data[0]["embedding"])

    def embed_sync(self, text: str) -> list[float]:
        """Sync variant — dimension probes at construction time. Loop-independent."""
        with httpx.Client(timeout=10.0, transport=self._sync_transport) as client:
            resp = client.post(
                f"{self._base_url}/embeddings",
                json={"model": self._model, "input": text},
            )
            resp.raise_for_status()
        data = (resp.json().get("data") or [])
        if not data or not data[0].get("embedding"):
            raise ValueError(f"empty embedding from {self._base_url} for model {self._model!r}")
        return list(data[0]["embedding"])


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


# ── Hybrid engine over adapter protocols ───────────────────────────────

class AsyncParallelHybridEngine:
    def __init__(self, dense: VectorStore, keyword: KeywordStore | None,
                 embeddings: EmbeddingClient, *, alpha: float = 0.3,
                 rrf_k: int = 60):
        self._dense = dense
        self._keyword = keyword
        self._embeddings = embeddings
        self._alpha = alpha
        self._rrf_k = rrf_k

    async def embed_query(self, text: str) -> list[float]:
        return await self._embeddings.embed(text)

    async def retrieve_parallel(self, query_text: str, sec_ctx: SecurityContext,
                                top_k: int, fetch_k: int | None = None,
                                query_vector: list[float] | None = None) -> list[Chunk]:
        """Fan the legs out concurrently; latency = max(leg) + fusion overhead.
        Accepts a precomputed query_vector so callers that already embedded the
        query (e.g. for the semantic cache check) do not embed twice. With no
        keyword leg configured, runs dense-only."""
        if query_vector is None:
            query_vector = await self._embeddings.embed(query_text)
        fetch_k = fetch_k or top_k * 2
        if self._keyword is None:
            dense_hits = await self._dense.search(query_vector, sec_ctx, fetch_k)
            sparse_hits: list[Chunk] = []
        else:
            dense_hits, sparse_hits = await asyncio.gather(
                self._dense.search(query_vector, sec_ctx, fetch_k),
                self._keyword.search(query_text, sec_ctx, fetch_k),
            )
        return fuse_wrrf(dense_hits, sparse_hits,
                         alpha=self._alpha, k=self._rrf_k)[:top_k]
