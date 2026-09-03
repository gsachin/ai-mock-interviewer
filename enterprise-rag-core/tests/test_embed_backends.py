"""Embedding backend matrix (Phase 0): the same engine stack must run on
Ollama, MLX, and vLLM embedding servers. Wire protocols are verified with
httpx.MockTransport (no live server); backend selection and base-URL
resolution are verified through EngineConfig."""
import asyncio
import json

import httpx
import pytest

from enterprise_rag.config import EngineConfig
from enterprise_rag.hybrid import (
    OllamaEmbeddingClient,
    OpenAICompatibleEmbeddingClient,
)


def run(coro):
    return asyncio.run(coro)


# ── wire protocol: Ollama ─────────────────────────────────────────────────

def test_ollama_embed_wire_protocol():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json={"embedding": [0.1, 0.2, 0.3]},
                               request=request)

    client = OllamaEmbeddingClient(
        "http://ollama.test", "nomic-embed-text",
        transport=httpx.MockTransport(handler),
        sync_transport=httpx.MockTransport(handler),
    )
    assert run(client.embed("hello")) == [0.1, 0.2, 0.3]
    assert captured["url"] == "http://ollama.test/api/embeddings"
    assert captured["json"] == {"model": "nomic-embed-text", "prompt": "hello"}
    # sync variant (dimension probes) hits the same contract
    assert client.embed_sync("probe") == [0.1, 0.2, 0.3]


# ── empty-embedding robustness (live-gate finding: Ollama warm-up race) ────

def test_ollama_embed_retries_across_the_reload_window():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        vector = [] if calls["n"] < 3 else [0.1, 0.2]  # empty while "reloading"
        return httpx.Response(200, json={"embedding": vector}, request=request)

    client = OllamaEmbeddingClient(
        "http://ollama.test", "nomic-embed-text",
        transport=httpx.MockTransport(handler),
    )
    assert run(client.embed("hello")) == [0.1, 0.2]
    assert calls["n"] == 3      # two retries with backoff, then success


def test_ollama_embed_raises_on_persistent_empty():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embedding": []}, request=request)

    client = OllamaEmbeddingClient(
        "http://ollama.test", "nomic-embed-text",
        transport=httpx.MockTransport(handler),
        sync_transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ValueError, match="empty embedding"):
        run(client.embed("hello"))
    with pytest.raises(ValueError, match="empty embedding"):
        client.embed_sync("probe")


def test_empty_prompt_rejected_before_any_request():
    """Live-gate finding: embedding an empty string made Ollama answer
    {"embedding": []}, which died deep inside the vector-store SDK."""
    client = OllamaEmbeddingClient("http://ollama.test", "nomic-embed-text")
    with pytest.raises(ValueError, match="empty"):
        run(client.embed(""))

    oai = OpenAICompatibleEmbeddingClient("http://x/v1", "bge")
    with pytest.raises(ValueError, match="empty"):
        run(oai.embed(""))


def test_openai_compatible_raises_on_empty_data():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []}, request=request)

    client = OpenAICompatibleEmbeddingClient(
        "http://vllm.test:8000/v1", "bge",
        transport=httpx.MockTransport(handler),
        sync_transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ValueError, match="empty embedding"):
        run(client.embed("hello"))
    with pytest.raises(ValueError, match="empty embedding"):
        client.embed_sync("probe")


# ── wire protocol: OpenAI-compatible (MLX / vLLM) ─────────────────────────

def test_openai_compatible_embed_wire_protocol():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        return httpx.Response(
            200, json={"data": [{"embedding": [0.4, 0.5]}]}, request=request,
        )

    client = OpenAICompatibleEmbeddingClient(
        "http://vllm.test:8000/v1", "BAAI/bge-small-en-v1.5",
        transport=httpx.MockTransport(handler),
        sync_transport=httpx.MockTransport(handler),
    )
    assert run(client.embed("hello")) == [0.4, 0.5]
    assert captured["url"] == "http://vllm.test:8000/v1/embeddings"
    assert captured["json"] == {"model": "BAAI/bge-small-en-v1.5", "input": "hello"}
    assert client.embed_sync("probe") == [0.4, 0.5]


# ── backend selection + base-URL resolution ───────────────────────────────

def _cfg(**over):
    base = dict(
        vector_backend="memory", keyword_backend="none", cache_backend="none",
        rerank_model_path="definitely/not/here.onnx",
    )
    base.update(over)
    return EngineConfig(**base)


def test_ollama_backend_selects_ollama_client():
    stack = _cfg(embed_backend="ollama").build_stack()
    assert isinstance(stack.embeddings, OllamaEmbeddingClient)


def test_vllm_backend_selects_openai_compatible_client():
    stack = _cfg(embed_backend="vllm", embed_model="BAAI/bge-small-en-v1.5").build_stack()
    assert isinstance(stack.embeddings, OpenAICompatibleEmbeddingClient)
    assert stack.embeddings._base_url == "http://127.0.0.1:8000/v1"   # vLLM default port
    assert stack.embeddings._model == "BAAI/bge-small-en-v1.5"


def test_openai_backend_uses_public_default():
    stack = _cfg(embed_backend="openai", embed_model="text-embedding-3-small").build_stack()
    assert stack.embeddings._base_url == "https://api.openai.com/v1"


def test_embed_base_url_override_beats_per_backend_defaults():
    stack = _cfg(embed_backend="vllm", embed_model="m",
                 embed_base_url="http://vllm:8001/v1").build_stack()
    assert stack.embeddings._base_url == "http://vllm:8001/v1"
    # and beats the legacy mlx-specific URL for the mlx backend too
    stack2 = _cfg(embed_backend="mlx", embed_model="m",
                  mlx_base_url="http://legacy/v1", embed_base_url="http://new/v1").build_stack()
    assert stack2.embeddings._base_url == "http://new/v1"


def test_mlx_legacy_base_url_still_honored():
    stack = _cfg(embed_backend="mlx", embed_model="m",
                 mlx_base_url="http://127.0.0.1:9999/v1").build_stack()
    assert stack.embeddings._base_url == "http://127.0.0.1:9999/v1"


@pytest.mark.parametrize("backend", ["mlx", "vllm", "openai"])
def test_openai_compatible_backends_require_embed_model(backend):
    with pytest.raises(ValueError, match="EMBED_MODEL"):
        _cfg(embed_backend=backend).build_stack()


def test_from_env_reads_embed_base_url(monkeypatch):
    monkeypatch.setenv("RAG_CORE_EMBED_BASE_URL", "http://vllm:8001/v1")
    monkeypatch.setenv("RAG_CORE_EMBED_BACKEND", "vllm")
    cfg = EngineConfig.from_env()
    assert cfg.embed_backend == "vllm"
    assert cfg.embed_base_url == "http://vllm:8001/v1"
