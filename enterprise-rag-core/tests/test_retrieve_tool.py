"""Generic retrieval MCP tool: JSON-RPC session sequence (initialize ->
Mcp-Session-Id -> tools/call), input schema (top_k default), none-mode
default-tenant passthrough, and the unauthenticated direct-call refusal.
Hermetic: stub engine wired through the server engine seam."""
import asyncio
import json

import httpx
import pytest

import enterprise_rag.server as server
from enterprise_rag.config import EngineConfig
from enterprise_rag.model import Chunk

CHUNKS = [
    Chunk(
        chunk_id="meridian-kb:s1:c1", parent_id="meridian-kb", tenant_id="acme",
        content="Meridian University is a private university founded in 2010.",
        score=0.9, section_title="University Overview",
        required_clearance=0, department=None,
    ),
    Chunk(
        chunk_id="meridian-kb:s5:c1", parent_id="meridian-kb", tenant_id="acme",
        content="The MBA tuition is $18,500 per year.",
        score=0.7, section_title="Fees Structure",
        required_clearance=0, department=None,
    ),
]


class StubEngine:
    def __init__(self, chunks):
        self._chunks = chunks
        self.calls: list[tuple[str, str, int]] = []

    async def retrieve_parallel(self, query_text, sec_ctx, top_k,
                                fetch_k=None, query_vector=None):
        self.calls.append((query_text, sec_ctx.tenant_id, top_k))
        return self._chunks[:top_k]


@pytest.fixture(autouse=True)
def stub_engine():
    engine = StubEngine(CHUNKS)
    server._set_engine(engine)
    return engine


@pytest.fixture(scope="module")
def base_url(running_app):
    mcp = server.build_mcp(EngineConfig(default_tenant="acme"))
    app = mcp.streamable_http_app(streamable_http_path="/mcp")
    with running_app(app) as base:
        yield base


def _rpc(base_url, method, params=None, session=None) -> httpx.Response:
    # The streamable-HTTP endpoint rejects clients that accept application/json
    # alone — both types must be listed. Responses may arrive as SSE or plain
    # JSON; _rpc_result handles both.
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


@pytest.fixture(scope="module")
def session_id(base_url):
    r = _rpc(base_url, "initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "1"},
    })
    assert r.status_code == 200, r.text
    sid = r.headers.get("mcp-session-id")
    assert sid, f"no session id header: {dict(r.headers)}"
    return sid


def test_tool_catalog_has_both_tools(base_url, session_id):
    r = _rpc(base_url, "tools/list", session=session_id)
    tools = _rpc_result(r)["result"]["tools"]
    names = [t["name"] for t in tools]
    assert "execute_agent_context" in names
    assert "retrieve_context" in names
    retrieve = next(t for t in tools if t["name"] == "retrieve_context")
    props = retrieve["inputSchema"]["properties"]
    assert props["query"]["type"] == "string"
    assert props["top_k"].get("default") == 5
    assert retrieve["inputSchema"]["required"] == ["query"]


def test_retrieve_session_sequence_and_payload(base_url, session_id, stub_engine):
    r = _rpc(base_url, "tools/call", {
        "name": "retrieve_context",
        "arguments": {"query": "tell me about the university", "top_k": 1},
    }, session=session_id)
    assert r.status_code == 200, r.text
    text = _rpc_result(r)["result"]["content"][0]["text"]
    data = json.loads(text)
    assert data["count"] == 1
    assert data["hit_source"] == "retrieval"
    chunk = data["chunks"][0]
    assert chunk["chunk_id"] == "meridian-kb:s1:c1"
    assert chunk["section_title"] == "University Overview"
    assert chunk["tenant_id"] == "acme"
    # engine got the query, the none-mode default tenant, and the top_k
    q, tenant, top_k = stub_engine.calls[-1]
    assert q == "tell me about the university"
    assert tenant == "acme"
    assert top_k == 1


def test_retrieve_default_top_k(base_url, session_id, stub_engine):
    r = _rpc(base_url, "tools/call", {
        "name": "retrieve_context",
        "arguments": {"query": "tuition fees"},
    }, session=session_id)
    assert r.status_code == 200, r.text
    assert stub_engine.calls[-1][2] == 5     # top_k default


def test_direct_unauthenticated_call_refuses():
    async def direct_refusal():
        try:
            await server.retrieve_context(query="q", ctx=None)
            return False
        except ValueError as exc:
            return "unauthenticated" in str(exc)

    assert asyncio.run(direct_refusal())
