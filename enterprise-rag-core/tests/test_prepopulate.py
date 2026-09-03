"""Markdown prepopulation: section splitting, deterministic overlap chunking,
marker validation gates, both-leg upsert, idempotent skip / --force. Hermetic
(fake embedder, memory + bm25 backends)."""
import asyncio

import pytest

from enterprise_rag.config import EngineConfig
from enterprise_rag.hybrid import OllamaEmbeddingClient
from enterprise_rag.prepopulate import (
    chunk_text_with_overlap,
    prepopulate,
    split_markdown_sections,
)

KB = """# Meridian Knowledge Base (provenance note — dropped)

This front-matter line is dropped too.

## University Overview

Meridian University is a private university founded in 2010. It has three
campuses and serves twelve thousand students.

The mission is to educate principled leaders.

## Fees Structure

The MBA tuition is $18,500 per year. Hostel fee is $1,200 per year.

Merit scholarships cover 30 percent of tuition.

## Empty Section
"""


def _stack(monkeypatch):
    async def fake_embed(self, text):
        v = [0.0] * 8
        for i, ch in enumerate(text[:8]):
            v[i % 8] += (ord(ch) % 5 + 1) / 10.0
        return v

    monkeypatch.setattr(OllamaEmbeddingClient, "embed", fake_embed)
    return EngineConfig(
        vector_backend="memory", keyword_backend="bm25", cache_backend="none",
        rerank_model_path="definitely/not/here.onnx",
        embed_backend="ollama",     # machine-independent: auto resolves mlx on macOS
    ).build_stack()


def _run(coro):
    return asyncio.run(coro)


def test_split_markdown_sections(tmp_path):
    kb = tmp_path / "kb.md"
    kb.write_text(KB, encoding="utf-8")
    sections = split_markdown_sections(kb)
    assert [s[0] for s in sections] == ["University Overview", "Fees Structure"]
    assert "front-matter line" not in sections[0][1]
    assert "Merit scholarships" in sections[1][1]


def test_chunk_text_with_overlap_deterministic():
    text = "\n\n".join(f"Paragraph number {i} contains some content about the university." for i in range(40))
    a = chunk_text_with_overlap(text)
    b = chunk_text_with_overlap(text)
    assert a == b
    assert len(a) > 1
    assert all(len(p) < 700 for p in a)
    # overlap: later pieces start with the tail of their predecessor's words
    assert a[1].split()[0] in a[0].split()
    # no overlap mode
    plain = chunk_text_with_overlap(text, overlap=0)
    assert len(plain) == len(a)


def test_prepopulate_end_to_end(tmp_path, monkeypatch):
    kb = tmp_path / "kb.md"
    kb.write_text(KB, encoding="utf-8")
    stack = _stack(monkeypatch)

    result = _run(prepopulate(
        stack, kb, doc_id="meridian-kb", tenant_id="acme",
        expected_markers=["meridian university"], blocked_markers=["fafsa"],
    ))
    assert not result.skipped
    assert result.sections == 2
    assert result.chunks >= 2          # one chunk per (short) section minimum

    chunks = _run(stack.vector_store.get_all("acme"))
    ids = {c.chunk_id for c in chunks}
    assert "meridian-kb:s1:c1" in ids
    assert all(c.tenant_id == "acme" for c in chunks)
    fees = next(c for c in chunks if c.section_title == "Fees Structure")
    assert "$18,500" in fees.content

    # keyword leg got the same records (tenant must match the chunks)
    from enterprise_rag.security import SecurityContext

    sec = SecurityContext("u1", "acme", [], [], 0, [])
    hits = _run(stack.keyword_store.search("tuition", sec, 5))
    assert any("$18,500" in h.content for h in hits)


def test_prepopulate_idempotent_skip_and_force(tmp_path, monkeypatch):
    kb = tmp_path / "kb.md"
    kb.write_text(KB, encoding="utf-8")
    stack = _stack(monkeypatch)

    first = _run(prepopulate(stack, kb, doc_id="meridian-kb", tenant_id="acme"))
    second = _run(prepopulate(stack, kb, doc_id="meridian-kb", tenant_id="acme"))
    assert second.skipped and second.chunks == first.chunks
    # a different doc id in the same tenant is NOT skipped
    other = _run(prepopulate(stack, kb, doc_id="other-doc", tenant_id="acme"))
    assert not other.skipped

    forced = _run(prepopulate(
        stack, kb, doc_id="meridian-kb", tenant_id="acme", force=True))
    assert not forced.skipped and forced.chunks == first.chunks


def test_prepopulate_marker_validation(tmp_path, monkeypatch):
    kb = tmp_path / "kb.md"
    kb.write_text(KB, encoding="utf-8")
    stack = _stack(monkeypatch)

    with pytest.raises(ValueError, match="expected markers missing"):
        _run(prepopulate(stack, kb, expected_markers=["totally absent phrase"]))
    with pytest.raises(ValueError, match="blocked markers present"):
        _run(prepopulate(stack, kb, blocked_markers=["scholarship"]))


def test_main_help_exits_zero():
    from enterprise_rag.prepopulate import main

    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
