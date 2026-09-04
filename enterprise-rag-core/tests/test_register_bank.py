"""Phase 4 runtime bank registration: text-first splitting parity, the
register_bank core (validation, idempotent skip, force replace on BOTH legs
— including in-memory BM25 delete), keyword-store delete_by_parent, the
none-auth register_bank MCP tool with keyword-leg visibility WITHOUT a
service restart, the OIDC scope guard, and an HTTP tools/list+tools/call
roundtrip. Hermetic (fake embedder, memory + bm25 backends)."""
import asyncio
import json

import httpx
import pytest

import enterprise_rag.server as server
from enterprise_rag.adapters.bm25_memory import BM25KeywordStore
from enterprise_rag.adapters.none_keyword import NoOpKeywordStore
from enterprise_rag.config import EngineConfig
from enterprise_rag.hybrid import OllamaEmbeddingClient
from enterprise_rag.prepopulate import (
    register_bank,
    split_markdown_sections,
    split_markdown_text,
)

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


# ── text-first splitting parity ─────────────────────────────────────────────

def test_split_markdown_text_matches_path_variant(tmp_path):
    kb = tmp_path / "kb.md"
    kb.write_text(KB, encoding="utf-8")
    assert split_markdown_text(KB) == split_markdown_sections(kb)
    # front matter (before the first ## heading) is dropped in both
    headings = [h for h, _b in split_markdown_text(KB)]
    assert headings == ["Rate limiter design", "Consistent hashing"]


# ── register_bank core ──────────────────────────────────────────────────────

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


def test_register_bank_ingests_into_both_legs(monkeypatch):
    stack = _stack(monkeypatch)
    result = run(register_bank(
        stack, markdown=KB, doc_id="bank-web", department="html",
        tenant_id="acme"))
    assert not result.skipped
    assert result.sections == 2 and result.chunks >= 2
    assert result.doc_id == "bank-web" and result.tenant_id == "acme"

    chunks = run(stack.vector_store.get_all("acme"))
    ids = {c.chunk_id for c in chunks}
    assert "bank-web:s1:c1" in ids
    assert all(c.parent_id == "bank-web" for c in chunks)

    from enterprise_rag.security import SecurityContext
    sec = SecurityContext("u1", "acme", [], ["html"], 0, [])
    hits = run(stack.keyword_store.search("rate limiter redis", sec, 5))
    assert any("token bucket" in h.content for h in hits)


def test_register_bank_validation_rejections(monkeypatch):
    stack = _stack(monkeypatch)
    with pytest.raises(ValueError, match="empty"):
        run(register_bank(stack, markdown="   ", doc_id="bank-x",
                          department="html"))
    with pytest.raises(ValueError, match="doc_id"):
        run(register_bank(stack, markdown=KB, doc_id="meridian-kb",
                          department="html"))
    with pytest.raises(ValueError, match="doc_id"):
        run(register_bank(stack, markdown=KB, doc_id="Bank-X",
                          department="html"))
    with pytest.raises(ValueError, match="department"):
        run(register_bank(stack, markdown=KB, doc_id="bank-x",
                          department="Web Dev"))
    with pytest.raises(ValueError, match="at least 1"):
        run(register_bank(stack, markdown="# Only a title\n\nno sections",
                          doc_id="bank-x", department="html"))
    # nothing was written by the failing calls
    assert run(stack.vector_store.get_all("acme")) == []


def test_register_bank_idempotent_skip_and_force_replaces_both_legs(monkeypatch):
    stack = _stack(monkeypatch)
    first = run(register_bank(stack, markdown=KB, doc_id="bank-web",
                              department="html", tenant_id="acme"))
    second = run(register_bank(stack, markdown=KB, doc_id="bank-web",
                               department="html", tenant_id="acme"))
    assert second.skipped and second.chunks == first.chunks

    # force-replace with a single-section corpus: the superseded section's
    # chunks must vanish from BOTH legs (vector + in-memory BM25).
    replacement = ('# Bank\n\n## Only surviving topic\n\nAll about '
                   '"rate limiter" tokens. Expected points: token bucket.')
    forced = run(register_bank(stack, markdown=replacement,
                               doc_id="bank-web", department="html",
                               tenant_id="acme", force=True))
    assert not forced.skipped and forced.sections == 1

    from enterprise_rag.security import SecurityContext
    sec = SecurityContext("u1", "acme", [], ["html"], 0, [])
    gone = run(stack.keyword_store.search("consistent hashing", sec, 5))
    assert all("consistent hashing" not in h.content for h in gone)
    still = run(stack.keyword_store.search("rate limiter", sec, 5))
    assert still, "replacement content must still hit the keyword leg"
    chunks = run(stack.vector_store.get_all("acme"))
    assert len(chunks) == forced.chunks
    assert all("consistent hashing" not in c.content for c in chunks)


# ── keyword-store delete_by_parent (direct adapter unit) ────────────────────

def test_bm25_delete_by_parent_only_removes_that_parent():
    store = BM25KeywordStore()
    records = []
    for parent, section, term in (
            ("bank-a", "s1", "zebras migrate"), ("bank-a", "s2", "zebras graze"),
            ("bank-b", "s1", "meerkats dig")):
        from enterprise_rag.model import UpsertRecord
        records.append(UpsertRecord(
            chunk_id=f"{parent}:{section}:c1", parent_id=parent,
            tenant_id="acme", content=f"{term}. Expected points: {term}.",
            section_title=section, required_clearance=0, department="x",
            vector=[0.5] * 8,
        ))
    run(store.upsert(records))
    removed = run(store.delete_by_parent("bank-a", "acme"))
    assert removed == 2
    again = run(store.delete_by_parent("bank-a", "acme"))
    assert again == 0

    from enterprise_rag.security import SecurityContext
    sec = SecurityContext("u1", "acme", [], [], 0, [])
    assert run(store.search("zebras", sec, 5)) == []
    hits = run(store.search("meerkats", sec, 5))
    assert len(hits) == 1 and hits[0].chunk_id == "bank-b:s1:c1"
    # a different tenant's rows with the same parent id are untouched
    run(store.upsert([UpsertRecord(
        chunk_id="bank-a:s1:c1", parent_id="bank-a", tenant_id="other",
        content="zebras migrate. Expected points: zebras.",
        section_title="s1", required_clearance=0, department="x",
        vector=[0.5] * 8,
    )]))
    assert run(store.delete_by_parent("bank-a", "acme")) == 0


def test_noop_keyword_delete_returns_zero():
    assert run(NoOpKeywordStore().delete_by_parent("bank-a", "acme")) == 0


# ── register_bank MCP tool (none-auth, live stack, NO restart) ──────────────

@pytest.fixture
def wired(monkeypatch):
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
    prev = (server.agent_engine, server.agent_orchestrator,
            server.agent_vector_store, server.agent_stack)
    server._set_engine(stack.engine)
    server._set_vector_store(stack.vector_store)
    server._set_orchestrator(stack.orchestrator)
    server._set_stack(stack)
    yield config, stack
    (server.agent_engine, server.agent_orchestrator,
     server.agent_vector_store, server.agent_stack) = prev


def test_register_bank_tool_register_and_keyword_visible_without_restart(wired):
    config, stack = wired
    tool = server._make_none_auth_register_bank_tool(config)

    payload = json.loads(run(tool(
        markdown=KB, doc_id="bank-web", department="html", ctx=None)))
    assert payload["status"] == "registered"
    assert payload["sections"] == 2 and payload["doc_id"] == "bank-web"
    assert payload["tenant_id"] == "acme"

    # the bank is listed by the interview tool…
    bank_tool = server._make_none_auth_interview_tools(config)[0]
    bank = json.loads(run(bank_tool("bank-web", ctx=None)))
    assert bank["count"] == 2

    # …and follow-up retrieval on new-domain terms hits the BM25 keyword leg
    # IN-PROCESS (no serve-boot warmup, no restart — the Phase 4 contract).
    followup = server._make_none_auth_interview_tools(config)[2]
    hits = json.loads(run(followup(
        "rate limiter redis tokens", domain="html", top_k=3, ctx=None)))
    assert hits["count"] >= 1
    assert all(c["department"] == "html" for c in hits["chunks"])

    # idempotent second registration reports already_present
    again = json.loads(run(tool(
        markdown=KB, doc_id="bank-web", department="html", ctx=None)))
    assert again["status"] == "already_present"


def test_register_bank_tool_rejects_bad_input(wired):
    config, _stack = wired
    tool = server._make_none_auth_register_bank_tool(config)
    with pytest.raises(ValueError, match="doc_id"):
        run(tool(markdown=KB, doc_id="web", department="html", ctx=None))
    with pytest.raises(ValueError, match="at least 1"):
        run(tool(markdown="# nope", doc_id="bank-web", department="html",
                 ctx=None))


def test_register_bank_oidc_refuses_unauthenticated():
    async def refusal():
        try:
            await server.register_bank(markdown=KB, doc_id="bank-web",
                                       department="html", ctx=None)
            return False
        except ValueError as exc:
            return "unauthenticated" in str(exc)

    assert run(refusal())


def test_register_bank_oidc_requires_rag_write_scope(wired, monkeypatch):
    config, _ = wired
    from mcp.server.auth.provider import AccessToken
    token = AccessToken(
        token="t", client_id="c",
        scopes=["rag:retrieve"],
        claims={"sub": "u1", "tenant_id": "acme"},
    )
    monkeypatch.setattr(server, "get_access_token", lambda: token)

    async def refusal():
        try:
            await server.register_bank(markdown=KB, doc_id="bank-web",
                                       department="html", ctx=None)
            return False
        except ValueError as exc:
            return "rag:write" in str(exc)

    assert run(refusal())


def test_register_bank_oidc_success_and_department_guard(wired, monkeypatch):
    config, stack = wired
    from mcp.server.auth.provider import AccessToken
    scoped = AccessToken(
        token="t", client_id="c",
        scopes=["rag:retrieve", "rag:write"],
        claims={"sub": "u1", "tenant_id": "acme",
                "departments": ["html"]},
    )
    monkeypatch.setattr(server, "get_access_token", lambda: scoped)

    payload = json.loads(run(server.register_bank(
        markdown=KB, doc_id="bank-web", department="html", ctx=None)))
    assert payload["status"] == "registered" and payload["tenant_id"] == "acme"
    chunks = run(stack.vector_store.get_all("acme"))
    assert any(c.parent_id == "bank-web" for c in chunks)

    # registration outside the token's departments is refused
    async def outside():
        try:
            await server.register_bank(markdown=KB, doc_id="bank-ios",
                                       department="ios", ctx=None)
            return False
        except ValueError as exc:
            return "outside the token" in str(exc)

    assert run(outside())


# ── HTTP roundtrip: catalog + register then list ────────────────────────────

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


def test_register_bank_roundtrip_over_http(wired, running_app):
    config, _ = wired
    mcp = server.build_mcp(config)
    app = mcp.streamable_http_app(streamable_http_path="/mcp")
    with running_app(app) as base_url:
        init = _rpc(base_url, "initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"},
        })
        session = init.headers.get("mcp-session-id")
        assert session, f"no session id: {dict(init.headers)}"

        tools = _rpc_result(_rpc(base_url, "tools/list", session=session))
        names = {t["name"] for t in tools["result"]["tools"]}
        assert "register_bank" in names

        reg = _rpc_result(_rpc(base_url, "tools/call", {
            "name": "register_bank",
            "arguments": {"markdown": KB, "doc_id": "bank-web",
                          "department": "html"},
        }, session=session))
        assert not reg.get("result", {}).get("isError")
        payload = json.loads(reg["result"]["content"][0]["text"])
        assert payload["status"] == "registered"

        bank = _rpc_result(_rpc(base_url, "tools/call", {
            "name": "interview_bank",
            "arguments": {"doc_id": "bank-web"},
        }, session=session))
        data = json.loads(bank["result"]["content"][0]["text"])
        assert data["count"] == 2
