"""Keyword warm-up: a fresh process's in-memory BM25 leg is repopulated from
the persistent vector store before serving. Hermetic (tmp Chroma, fake
embedder)."""
import asyncio

from enterprise_rag.config import EngineConfig
from enterprise_rag.hybrid import OllamaEmbeddingClient
from enterprise_rag.prepopulate import prepopulate
from enterprise_rag.security import SecurityContext
from enterprise_rag.warmup import warm_keyword_from_vector_store

KB = "## Overview\n\nMeridian is a university with many programs.\n\n## Fees\nThe tuition is $18,500 per year.\n"


def _stack(tmp_path, monkeypatch):
    async def fake_embed(self, text):
        v = [0.0] * 8
        for i, ch in enumerate(text[:8]):
            v[i % 8] += (ord(ch) % 5 + 1) / 10.0
        return v

    monkeypatch.setattr(OllamaEmbeddingClient, "embed", fake_embed)
    return EngineConfig(
        vector_backend="chroma", chroma_path=str(tmp_path),
        chroma_collection="kbstore", keyword_backend="bm25", cache_backend="none",
        rerank_model_path="definitely/not/here.onnx", default_tenant="acme",
        embed_backend="ollama",     # machine-independent: auto resolves mlx on macOS
    ).build_stack()


def _run(coro):
    return asyncio.run(coro)


def test_warm_repopulates_fresh_keyword_leg(tmp_path, monkeypatch):
    kb = tmp_path / "kb.md"
    kb.write_text(KB, encoding="utf-8")
    stack1 = _stack(tmp_path, monkeypatch)
    result = _run(prepopulate(stack1, kb, doc_id="kb", tenant_id="acme"))
    assert not result.skipped and result.chunks >= 2

    # simulate a service restart: same persistent chroma, brand-new BM25 leg
    stack2 = _stack(tmp_path, monkeypatch)
    sec = SecurityContext("u1", "acme", [], [], 0, [])
    before = _run(stack2.keyword_store.search("tuition", sec, 5))
    assert before == []                  # fresh process, empty sparse index

    warmed = _run(warm_keyword_from_vector_store(stack2))
    assert warmed == result.chunks

    after = _run(stack2.keyword_store.search("tuition", sec, 5))
    assert any("$18,500" in h.content for h in after)


def test_warm_empty_store_returns_zero(tmp_path, monkeypatch):
    stack = _stack(tmp_path, monkeypatch)
    assert _run(warm_keyword_from_vector_store(stack)) == 0


# ── Phase 0: warm scope selection ─────────────────────────────────────────

KB_BETA = "## Scholarships\nThe merit scholarship is $4,000 per term.\n"


def _populate(tmp_path, monkeypatch):
    """Prepopulate two tenants (acme + beta) into a persistent chroma store."""
    kb = tmp_path / "kb.md"
    kb.write_text(KB, encoding="utf-8")
    kb_beta = tmp_path / "kb-beta.md"
    kb_beta.write_text(KB_BETA, encoding="utf-8")
    stack = _stack(tmp_path, monkeypatch)
    acme = _run(prepopulate(stack, kb, doc_id="kb", tenant_id="acme"))
    beta = _run(prepopulate(stack, kb_beta, doc_id="kb-beta", tenant_id="beta"))
    assert not acme.skipped and not beta.skipped
    return acme, beta


def _search_all(stack, sec, query):
    return _run(stack.keyword_store.search(query, sec, 10))


def test_warm_all_tenants_repopulates_every_tenant(tmp_path, monkeypatch):
    acme, beta = _populate(tmp_path, monkeypatch)

    # fresh process: same persistent chroma, brand-new BM25 leg
    stack2 = _stack(tmp_path, monkeypatch)
    warmed = _run(warm_keyword_from_vector_store(stack2, tenants="all"))
    assert warmed == acme.chunks + beta.chunks

    acme_sec = SecurityContext("u1", "acme", [], [], 0, [])
    beta_sec = SecurityContext("u2", "beta", [], [], 0, [])
    assert any("$18,500" in h.content for h in _search_all(stack2, acme_sec, "tuition"))
    assert any("$4,000" in h.content for h in _search_all(stack2, beta_sec, "scholarship"))


def test_warm_default_scope_ignores_other_tenants(tmp_path, monkeypatch):
    acme, _beta = _populate(tmp_path, monkeypatch)

    stack2 = _stack(tmp_path, monkeypatch)
    warmed = _run(warm_keyword_from_vector_store(stack2))   # default scope
    assert warmed == acme.chunks

    beta_sec = SecurityContext("u2", "beta", [], [], 0, [])
    assert _search_all(stack2, beta_sec, "scholarship") == []
