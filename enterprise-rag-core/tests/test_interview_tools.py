"""Phase 1 interview question-bank tools: pure grouping helpers, none-auth
tool behavior over a real zero-infra stack (prepopulated bank), the MCP tool
catalog, and the OIDC direct-call refusal."""
import asyncio
import json

import httpx
import pytest

import enterprise_rag.server as server
from enterprise_rag.config import EngineConfig
from enterprise_rag.hybrid import OllamaEmbeddingClient
from enterprise_rag.interview import (
    group_questions,
    parse_chunk_position,
    question_refs,
)
from enterprise_rag.model import Chunk
from enterprise_rag.prepopulate import prepopulate

KB = """# Bank

## Rate limiter design

Design a rate limiter for a public API. Expected points: token bucket vs
sliding window, per-user keys, Redis counters, backpressure and 429s.

## Consistent hashing

Explain consistent hashing and why it minimizes reshuffling when nodes
join or leave a ring.
"""


def run(coro):
    return asyncio.run(coro)


# ── pure helpers ────────────────────────────────────────────────────────────

def test_parse_chunk_position():
    assert parse_chunk_position("bank-sd:s2:c1", "bank-sd") == (2, 1)
    assert parse_chunk_position("bank-sd:s2:c1", "other-bank") is None
    assert parse_chunk_position("not-a-chunk-id", "bank-sd") is None


def test_group_questions_orders_by_section_and_chunk():
    chunks = [
        Chunk("bank-sd:s2:c2", "bank-sd", "acme", "two", section_title="Consistent hashing"),
        Chunk("bank-sd:s1:c1", "bank-sd", "acme", "one", section_title="Rate limiter design"),
        Chunk("bank-sd:s2:c1", "bank-sd", "acme", "one-b", section_title="Consistent hashing"),
        Chunk("bank-sd:s1:c2", "bank-sd", "acme", "one-b", section_title="Rate limiter design"),
        Chunk("other:s1:c1", "other", "acme", "foreign", section_title="Foreign"),
    ]
    questions = group_questions(chunks, "bank-sd")
    assert [q.question_id for q in questions] == ["s1", "s2"]
    assert [c.chunk_id for c in questions[0].chunks] == ["bank-sd:s1:c1", "bank-sd:s1:c2"]
    refs = question_refs(questions)
    assert refs[0].question_id == "s1" and refs[0].chunk_count == 2
    assert refs[0].section_title == "Rate limiter design"


# ── hermetic tool behavior (none-auth, real zero-infra stack) ──────────────

@pytest.fixture
def wired(monkeypatch, tmp_path):
    async def fake_embed(self, text):
        v = [0.0] * 8
        for i, ch in enumerate(text[:8]):
            v[i % 8] += (ord(ch) % 5 + 1) / 10.0
        return v

    monkeypatch.setattr(OllamaEmbeddingClient, "embed", fake_embed)
    config = EngineConfig(
        vector_backend="memory", keyword_backend="bm25", cache_backend="memory",
        default_tenant="acme", rerank_model_path="definitely/not/here.onnx",
        embed_backend="ollama",     # machine-independent: auto resolves mlx on macOS
    )
    stack = config.build_stack()
    kb = tmp_path / "kb.md"
    kb.write_text(KB, encoding="utf-8")
    result = run(prepopulate(
        stack, kb, doc_id="bank-sd", tenant_id="acme",
        department="system-design",
    ))
    assert not result.skipped and result.chunks >= 2
    # wire the seams, restoring the previous values afterwards: the module-level
    # seams are shared with other tests (e.g. test_mcp_boot's stub orchestrator)
    prev = (server.agent_engine, server.agent_orchestrator, server.agent_vector_store)
    server._set_engine(stack.engine)
    server._set_vector_store(stack.vector_store)
    server._set_orchestrator(stack.orchestrator)
    yield config
    (server.agent_engine, server.agent_orchestrator,
     server.agent_vector_store) = prev


def _none_tools(config):
    return server._make_none_auth_interview_tools(config)


def test_interview_bank_lists_questions(wired):
    bank, _q, _f = _none_tools(wired)
    data = json.loads(run(bank("bank-sd", ctx=None)))
    assert data["count"] == 2
    assert data["tenant_id"] == "acme"
    assert data["questions"][0] == {
        "question_id": "s1", "section_title": "Rate limiter design", "chunk_count": 1,
    }
    assert data["questions"][1]["section_title"] == "Consistent hashing"


def test_interview_question_fetches_full_question(wired):
    _b, question, _f = _none_tools(wired)
    data = json.loads(run(question("bank-sd", "s1", ctx=None)))
    assert data["question_id"] == "s1"
    content = " ".join(c["content"] for c in data["chunks"])
    assert "rate limiter" in content.lower()
    assert "token bucket" in content.lower()
    assert data["chunks"][0]["chunk_id"] == "bank-sd:s1:c1"

    with pytest.raises(ValueError, match="unknown question"):
        run(question("bank-sd", "s99", ctx=None))


def test_interview_followup_filters_by_domain(wired):
    _b, _q, followup = _none_tools(wired)
    hits = json.loads(run(followup(
        "rate limiter tokens redis", domain="system-design", top_k=3, ctx=None)))
    assert hits["count"] >= 1
    assert all(c["department"] == "system-design" for c in hits["chunks"])

    # other domain: nothing matches — per-domain isolation
    none_hits = json.loads(run(followup(
        "rate limiter tokens redis", domain="ios", top_k=3, ctx=None)))
    assert none_hits["count"] == 0


def test_interview_followup_rejects_empty_query(wired):
    _b, _q, followup = _none_tools(wired)
    with pytest.raises(ValueError, match="empty"):
        run(followup("   ", domain="system-design", top_k=3, ctx=None))


def test_interview_bank_direct_call_refuses_without_token():
    async def direct_refusal():
        try:
            await server.interview_bank(doc_id="x", ctx=None)
            return False
        except ValueError as exc:
            return "unauthenticated" in str(exc)

    assert run(direct_refusal())


# ── MCP catalog + wire roundtrip (none-auth) ────────────────────────────────

def _rpc(base_url, method, params=None, session=None) -> httpx.Response:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session:
        headers["Mcp-Session-Id"] = session
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    return httpx.post(f"{base_url}/mcp", json=body, headers=headers, timeout=15)


def _rpc_result(r: httpx.Response) -> dict:
    if r.headers.get("content-type", "").startswith("text/event-stream"):
        payload = None
        for line in r.text.splitlines():
            if line.startswith("data: "):
                payload = json.loads(line[len("data: "):])
        assert payload is not None, f"no data line in SSE: {r.text}"
        return payload
    return r.json()


def test_catalog_and_bank_roundtrip_over_http(wired, running_app):
    mcp = server.build_mcp(wired)
    app = mcp.streamable_http_app(streamable_http_path="/mcp")
    with running_app(app) as base_url:
        init = _rpc(base_url, "initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"},
        })
        session = init.headers.get("mcp-session-id")
        assert session, f"no session id: {dict(init.headers)}"

        tools = _rpc_result(_rpc(base_url, "tools/list", session=session))["result"]["tools"]
        names = {t["name"] for t in tools}
        assert {"interview_bank", "interview_question", "interview_followup"} <= names

        r = _rpc(base_url, "tools/call", {
            "name": "interview_bank",
            "arguments": {"doc_id": "bank-sd"},
        }, session=session)
        data = json.loads(_rpc_result(r)["result"]["content"][0]["text"])
        assert data["count"] == 2
