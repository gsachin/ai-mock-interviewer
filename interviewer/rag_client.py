"""MCP client for the standalone enterprise-rag-core service.

One ``RagClient`` per agent worker; each call opens one MCP session
(``streamable_http_client`` + ``ClientSession`` — the same pattern the core
repo's own boot test uses). OIDC mode: set ``RAG_MCP_TOKEN`` (bearer,
scope ``rag:retrieve``). none-auth mode: leave it unset and every request
runs as the RAG service's default tenant.
"""
import json
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel, Field


class Chunk(BaseModel):
    chunk_id: str
    parent_id: str | None = None
    tenant_id: str
    section_title: str = ""
    content: str
    score: float = 0.0
    required_clearance: int = 0
    department: str | None = None


class RetrieveContextResult(BaseModel):
    chunks: list[Chunk]
    count: int
    hit_source: str = "retrieval"


class RagClient:
    """Async client for enterprise-rag-core's MCP endpoint."""

    def __init__(self, url: str = "http://127.0.0.1:8000/mcp",
                 token: str | None = None):
        self._url = url
        self._token = token

    @asynccontextmanager
    async def session(self) -> AsyncIterator[ClientSession]:
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        async with httpx.AsyncClient(headers=headers, timeout=30.0) as hc:
            async with streamable_http_client(self._url, http_client=hc) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session

    async def _call(self, tool: str, args: dict[str, Any]) -> str:
        async with self.session() as session:
            result = await session.call_tool(tool, args)
            if result.is_error:
                raise RuntimeError(f"MCP tool {tool} failed: {result.content}")
            if not result.content:
                return ""
            return result.content[0].text

    async def retrieve_context(self, query: str, top_k: int | None = None) -> RetrieveContextResult:
        """Tenant-scoped hybrid retrieval from the RAG service. Budget: the
        RAG service answers in ~30–150 ms; this hop adds ~1–5 ms."""
        top_k = top_k or 5
        text = await self._call("retrieve_context", {"query": query, "top_k": top_k})
        return RetrieveContextResult.model_validate(json.loads(text))

    async def agent_context(self, resume_text: str, job_description: str,
                            rubric_query: str, channel: str = "voice") -> dict[str, Any]:
        """Full atomic agent-context envelope (direct injections + cached
        rubric retrieval + rerank + U-shape formatting) from the RAG service."""
        text = await self._call("execute_agent_context", {
            "resume_text": resume_text,
            "job_description": job_description,
            "rubric_query": rubric_query,
            "channel": channel,
        })
        return json.loads(text)
