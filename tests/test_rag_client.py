"""RagClient: unit tests against a stubbed MCP transport, plus a live
integration test (marked ``live``) against a running enterprise-rag-core
MCP service."""
import asyncio
import json

import pytest

from interviewer.rag_client import RagClient


class _FakeContent:
    def __init__(self, text: str):
        self.text = text


class _FakeResult:
    def __init__(self, text: str):
        self.content = [_FakeContent(text)]
        self.is_error = False   # match the MCP SDK's CallToolResult (snake_case)


class _FakeSession:
    def __init__(self, script: dict[str, str]):
        self._script = script
        self.calls: list[tuple[str, dict]] = []
        self.initialized = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def initialize(self):
        self.initialized = True

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        return _FakeResult(self._script[name])


def _patch_mcp(monkeypatch, script):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_streamable(url, http_client=None):
        assert "Bearer tok" in http_client.headers.get("Authorization", "")
        yield None, None

    monkeypatch.setattr("interviewer.rag_client.streamable_http_client",
                        fake_streamable)

    def fake_session_cls(read, write):
        return _FakeSession(script)

    monkeypatch.setattr("interviewer.rag_client.ClientSession", fake_session_cls)


def test_retrieve_context_parses_chunk_payload(monkeypatch):
    payload = {
        "chunks": [
            {"chunk_id": "kb:s1:c1", "parent_id": "kb", "tenant_id": "default",
             "section_title": "Overview", "content": "leadership rubric",
             "score": 0.83, "required_clearance": 0, "department": None},
        ],
        "count": 1,
        "hit_source": "retrieval",
    }
    _patch_mcp(monkeypatch, {"retrieve_context": json.dumps(payload)})

    client = RagClient("http://127.0.0.1:8000/mcp", token="tok")
    result = asyncio.run(client.retrieve_context("leadership", top_k=3))

    assert result.count == 1
    assert result.hit_source == "retrieval"
    assert result.chunks[0].chunk_id == "kb:s1:c1"
    assert result.chunks[0].content == "leadership rubric"
    assert result.chunks[0].score == pytest.approx(0.83)


def _patch_mcp_recording(monkeypatch, script, recorded):
    """_patch_mcp + a session that appends every (tool, args) to ``recorded``."""

    class _RecordingSession(_FakeSession):
        async def call_tool(self, name, args):
            recorded.append((name, args))
            return await super().call_tool(name, args)

    _patch_mcp(monkeypatch, script)

    def fake_session_cls(read, write):
        return _RecordingSession(script)

    monkeypatch.setattr("interviewer.rag_client.ClientSession", fake_session_cls)


def test_register_bank_forwards_args_and_parses_payload(monkeypatch):
    payload = {
        "doc_id": "bank-html", "tenant_id": "default",
        "sections": 15, "chunks": 15, "status": "registered",
    }
    recorded = []
    _patch_mcp_recording(monkeypatch, {"register_bank": json.dumps(payload)},
                         recorded)

    client = RagClient(token="tok")
    result = asyncio.run(client.register_bank(
        "bank-html", "# HTML\n\n## Semantic HTML\n\nbody", "html"))

    assert recorded == [("register_bank", {
        "markdown": "# HTML\n\n## Semantic HTML\n\nbody",
        "doc_id": "bank-html",
        "department": "html",
        "force": False,
    })]
    assert result.status == "registered"
    assert result.sections == 15 and result.chunks == 15
    assert result.doc_id == "bank-html" and result.tenant_id == "default"


def test_register_bank_force_flag_and_error_mapping(monkeypatch):
    payload = {
        "doc_id": "bank-html", "tenant_id": "default",
        "sections": 15, "chunks": 15, "status": "registered",
    }
    recorded = []
    _patch_mcp_recording(monkeypatch, {"register_bank": json.dumps(payload)},
                         recorded)
    client = RagClient(token="tok")
    asyncio.run(client.register_bank("bank-html", "md", "html", force=True))
    assert recorded[0][1]["force"] is True

    # error results surface as RuntimeError (the SDK's is_error contract)
    class _ErrContent:
        text = "MCP tool register_bank failed: unknown question in bank"

    class _ErrResult:
        content = [_ErrContent]
        is_error = True

    class _ErrSession(_FakeSession):
        async def call_tool(self, name, args):
            self.calls.append((name, args))
            return _ErrResult()

    def fake_session_cls(read, write):
        return _ErrSession({})

    monkeypatch.setattr("interviewer.rag_client.ClientSession", fake_session_cls)
    with pytest.raises(RuntimeError, match="register_bank failed"):
        asyncio.run(client.register_bank("bank-html", "md", "html"))


def test_register_bank_uses_long_timeout_budget(monkeypatch):
    """Server-side embedding of a bank needs more than the 30 s default."""
    from contextlib import asynccontextmanager

    payload = {
        "doc_id": "bank-html", "tenant_id": "default",
        "sections": 15, "chunks": 15, "status": "registered",
    }
    seen_timeouts = []

    @asynccontextmanager
    async def fake_streamable(url, http_client=None):
        seen_timeouts.append(float(http_client.timeout.connect))
        yield None, None

    monkeypatch.setattr("interviewer.rag_client.streamable_http_client",
                        fake_streamable)

    def fake_session_cls(read, write):
        return _FakeSession({"register_bank": json.dumps(payload)})

    monkeypatch.setattr("interviewer.rag_client.ClientSession", fake_session_cls)
    asyncio.run(RagClient(token="tok").register_bank("bank-html", "md", "html"))
    assert seen_timeouts == [120.0]

    # a plain read call keeps the default 30 s budget
    seen_timeouts.clear()
    monkeypatch.setattr("interviewer.rag_client.ClientSession",
                        lambda read, write: _FakeSession(
                            {"retrieve_context": json.dumps({
                                "chunks": [], "count": 0,
                                "hit_source": "retrieval"})}))
    asyncio.run(RagClient(token="tok").retrieve_context("x", top_k=3))
    assert seen_timeouts == [30.0]


def test_agent_context_forwards_request_shape(monkeypatch):
    payload = {
        "status": "SUCCESS",
        "hit_source": "cache",
        "context_envelope": "[context_envelope tenant=default clearance>=0]",
        "provenance": [],
        "timings_ms": {"direct": 0.1, "embed": 1.2, "cache": 0.3,
                       "retrieval": 0.0, "rerank": 2.1, "format": 0.1, "total": 3.8},
    }
    _patch_mcp(monkeypatch, {"execute_agent_context": json.dumps(payload)})

    client = RagClient(token="tok")
    result = asyncio.run(client.agent_context(
        resume_text="r", job_description="jd", rubric_query="leadership"))

    assert result["status"] == "SUCCESS"
    assert result["timings_ms"]["total"] == pytest.approx(3.8)


@pytest.mark.live
def test_rag_client_live_against_core():
    """Integration against a running enterprise-rag-core serve (none-auth).
    Set RAG_MCP_URL to enable; skipped when nothing reachable (or when the
    port is taken by something that is not a RAG MCP server — e.g. an MLX
    embedding server on :8000)."""
    import os

    import httpx
    from mcp.shared.exceptions import MCPError

    from tests.conftest import port_open

    url = os.environ.get("RAG_MCP_URL", "http://127.0.0.1:8000/mcp")
    host, port = url.split("//")[1].split(":")[0], int(url.rsplit(":", 1)[1].split("/")[0])
    if not port_open(host, port):
        pytest.skip(f"no service at {host}:{port} — start enterprise-rag-core serve")

    client = RagClient(url)
    try:
        result = asyncio.run(client.retrieve_context("leadership rubric", top_k=3))
    except (MCPError, httpx.HTTPError) as exc:
        pytest.skip(f"port {host}:{port} is not a RAG MCP server: {exc}")
    assert isinstance(result.count, int) and result.count >= 0
