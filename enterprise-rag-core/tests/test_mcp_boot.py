"""TEST-MCP-01: boot the §6 server against pinned mcp==2.1.1.
  * tools/list returns the documented catalog
  * tools/call without/invalid bearer -> 401 (SDK BearerAuthBackend)
  * valid token -> tenant-scoped envelope
  * unauthenticated direct invocation raises the documented refusal
Converted from the seed verification harness."""
import asyncio
import json
import os

import httpx
import pytest
from pydantic import AnyHttpUrl

from mcp.server import MCPServer
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings

# server.py's module-scope singletons need the ONNX model + Redis Stack:
os.environ["REDIS_STACK_URL"] = os.environ.get("REDIS_STACK_URL", "redis://localhost:6379")
os.environ["RAG_TENANT_ID"] = "acme"

# server.py's module-scope ``mcp = build_mcp()`` constructs no engine stack
# and no cache (the stack is only built by build_app), so importing it is
# hermetic without any patching. Phase 0 note: earlier revisions of this
# file patched OllamaEmbeddingClient.embed_sync and EnterpriseSemanticCache
# at module scope for a pre-refactor server — that leaked session-wide and
# was removed.
import enterprise_rag.server as server  # §6 transcript


# ── stub orchestrator: echoes the security context derived from the token ──
class StubOrchestrator:
    async def execute_agent_context(self, req):
        return {
            "status": "SUCCESS",
            "hit_source": "retrieval",
            "context_envelope": (
                f"[context_envelope tenant={req.sec_ctx.tenant_id} "
                f"clearance>={req.sec_ctx.clearance_level}]"
            ),
            "provenance": [],
        }


server.agent_orchestrator = StubOrchestrator()


class StubVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        if token == "valid-token":
            return AccessToken(
                token=token,
                client_id="client-1",
                scopes=["rag:retrieve"],
                claims={
                    "sub": "u1",
                    "tenant_id": "acme",
                    "roles": ["interviewer"],
                    "departments": ["engineering"],
                    "clearance_level": 3,
                },
            )
        return None


@pytest.fixture(scope="module")
def test_app():
    test_mcp = MCPServer(
        name="enterprise-rag-core-test",
        token_verifier=StubVerifier(),
        auth=AuthSettings(
            issuer_url=AnyHttpUrl("https://iam.example.com/oauth2"),
            resource_server_url=AnyHttpUrl("http://127.0.0.1:8000/mcp"),
            required_scopes=["rag:retrieve"],
        ),
    )
    test_mcp.add_tool(server.execute_agent_context)
    return test_mcp.streamable_http_app(streamable_http_path="/mcp")


@pytest.fixture(scope="module")
def base_url(test_app, running_app):
    """One uvicorn boot shared by all tests in this module (booting a second
    uvicorn thread in the same process races on Windows)."""
    with running_app(test_app) as base:
        yield base


async def sdk_client_calls(url: str):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with httpx.AsyncClient(headers={"Authorization": "Bearer valid-token"}) as hc:
        async with streamable_http_client(url, http_client=hc) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                result = await session.call_tool(
                    "execute_agent_context",
                    {
                        "resume_text": "r",
                        "job_description": "jd",
                        "rubric_query": "situational leadership",
                    },
                )
                return tools, result


def test_unauthenticated_direct_call_refuses():
    async def direct_refusal():
        try:
            await server.execute_agent_context(
                resume_text="r", job_description="jd", rubric_query="q",
                channel="voice", ctx=None,
            )
            return False
        except ValueError as e:
            return "unauthenticated" in str(e)

    assert asyncio.run(direct_refusal())


def test_401_without_or_invalid_bearer(base_url):
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"},
        },
    }
    with httpx.Client(timeout=10) as c:
        r = c.post(
            f"{base_url}/mcp",
            json=init_body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        assert r.status_code == 401, f"status={r.status_code}"
        r = c.post(
            f"{base_url}/mcp",
            json=init_body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": "Bearer invalid-token",
            },
        )
        assert r.status_code == 401, f"status={r.status_code}"


def test_happy_path_sdk_client(base_url):
    tools, result = asyncio.run(sdk_client_calls(f"{base_url}/mcp"))

    names = [t.name for t in tools.tools]
    assert "execute_agent_context" in names, f"{names}"
    tool = next(t for t in tools.tools if t.name == "execute_agent_context")
    schema = tool.input_schema
    props = schema.get("properties", {})
    assert schema.get("required") == [
        "resume_text", "job_description", "rubric_query",
    ], f"{schema.get('required')}"
    assert props.get("channel", {}).get("default") == "voice", f"{props.get('channel')}"

    text = result.content[0].text if result.content else ""
    envelope = json.loads(text)
    assert envelope.get("status") == "SUCCESS", text
    assert "tenant=acme" in envelope.get("context_envelope", ""), text
    assert "clearance>=3" in envelope.get("context_envelope", ""), text
