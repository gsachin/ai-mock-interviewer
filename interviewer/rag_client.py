"""MCP client for the standalone enterprise-rag-core service.

One ``RagClient`` per agent worker; each call opens one MCP session
(``streamable_http_client`` + ``ClientSession`` — the same pattern the core
repo's own boot test uses). OIDC mode: set ``RAG_MCP_TOKEN`` (bearer,
scope ``rag:retrieve``). none-auth mode: leave it unset and every request
runs as the RAG service's default tenant.
"""
import asyncio
import json
from contextlib import AsyncExitStack, asynccontextmanager
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


class QuestionRef(BaseModel):
    question_id: str
    section_title: str
    chunk_count: int


class InterviewBankResult(BaseModel):
    doc_id: str
    tenant_id: str
    questions: list[QuestionRef]
    count: int


class InterviewQuestionChunk(BaseModel):
    chunk_id: str
    content: str


class InterviewQuestionResult(BaseModel):
    doc_id: str
    tenant_id: str
    question_id: str
    section_title: str
    chunks: list[InterviewQuestionChunk]

    @property
    def formatted(self) -> str:
        """The question as spoken/displayed text (title + chunk bodies)."""
        return self.section_title + "\n\n" + "\n\n".join(
            c.content for c in self.chunks
        )


class RegisterBankResult(BaseModel):
    doc_id: str
    tenant_id: str
    sections: int
    chunks: int
    status: str  # "registered" when new, "already_present" on idempotent skip


class RagClient:
    """Async client for enterprise-rag-core's MCP endpoint."""

    def __init__(self, url: str = "http://127.0.0.1:8000/mcp",
                 token: str | None = None):
        self._url = url
        self._token = token
        # Persistent-session state. All four are cleared together by aclose()
        # so a half-torn-down client is never observable.
        self._stack: AsyncExitStack | None = None
        self._http: httpx.AsyncClient | None = None
        self._session: ClientSession | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def session(self, timeout_s: float = 30.0) -> AsyncIterator[ClientSession]:
        """A one-shot session. Kept for callers that genuinely want isolation;
        normal traffic goes through the persistent session below."""
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        async with httpx.AsyncClient(headers=headers, timeout=timeout_s) as hc:
            async with streamable_http_client(self._url, http_client=hc) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session

    async def _ensure_session(self, timeout_s: float) -> ClientSession:
        """One long-lived MCP session per client, built on first use.

        The previous behaviour opened a fresh session per call, which meant
        every single retrieval paid a full JSON-RPC ``initialize()``
        handshake on top of a new TCP connection. RAG measures 200-360 ms
        against a 150 ms stage budget, so that handshake was a large slice of
        an already-over-budget hop -- and it is the one latency fix in this
        migration that needs no GPU at all.

        Loop-aware on purpose: the session is bound to the event loop that
        created it, so a cached session reused from a different loop (which is
        exactly what a test doing one ``asyncio.run`` per case does) is
        detected and replaced rather than silently failing later.
        """
        loop = asyncio.get_running_loop()
        if self._session is not None and self._loop is loop:
            return self._session
        if self._session is not None:
            # Bound to a loop that no longer exists -- drop it and rebuild.
            await self.aclose()

        async with self._lock:
            if self._session is not None and self._loop is loop:
                return self._session
            headers = ({"Authorization": f"Bearer {self._token}"}
                       if self._token else {})
            stack = AsyncExitStack()
            http = await stack.enter_async_context(
                httpx.AsyncClient(headers=headers, timeout=timeout_s,
                                  limits=httpx.Limits(max_connections=8,
                                                      max_keepalive_connections=4)))
            read, write = await stack.enter_async_context(
                streamable_http_client(self._url, http_client=http))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self._stack, self._http, self._session, self._loop = (
                stack, http, session, loop)
            return session

    async def aclose(self) -> None:
        """Tear the persistent session down. Safe to call repeatedly."""
        stack, self._stack = self._stack, None
        self._http = None
        self._session = None
        self._loop = None
        if stack is not None:
            try:
                await stack.aclose()
            except Exception:
                # Closing a session whose transport already died must not
                # raise: this runs from shutdown paths, where a secondary
                # error would mask the real reason for terminating.
                pass

    async def _call(self, tool: str, args: dict[str, Any], *,
                    timeout_s: float = 30.0, _retry: bool = True) -> str:
        try:
            session = await self._ensure_session(timeout_s)
            # Per-call read timeout, so the persistent session does not force
            # one timeout on every tool: register_bank legitimately needs 120 s
            # for server-side embedding while the rest want 30 s.
            # The SDK takes SECONDS AS A FLOAT, not a timedelta -- caught by
            # checking the real signature after a test double rejected the
            # kwarg, which would otherwise have looked like a test problem.
            result = await session.call_tool(
                tool, args, read_timeout_seconds=float(timeout_s))
        except Exception:
            # A dropped session is recoverable and every tool here is
            # idempotent, so reconnect exactly once before surfacing the error.
            await self.aclose()
            if not _retry:
                raise
            return await self._call(tool, args, timeout_s=timeout_s,
                                    _retry=False)

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
        rubric retrieval + rerank + U-shape formatting) from the RAG service.
        ``hit_source`` reports cache vs retrieval — the interviewer measures
        its rubric cache hit rate from it."""
        text = await self._call("execute_agent_context", {
            "resume_text": resume_text,
            "job_description": job_description,
            "rubric_query": rubric_query,
            "channel": channel,
        })
        return json.loads(text)

    async def interview_bank(self, doc_id: str) -> InterviewBankResult:
        """Question catalog of a prepopulated bank (deterministic ids,
        no search — an exact listing)."""
        text = await self._call("interview_bank", {"doc_id": doc_id})
        return InterviewBankResult.model_validate(json.loads(text))

    async def interview_question(self, doc_id: str,
                                 question_id: str) -> InterviewQuestionResult:
        """One full question by deterministic id (exact fetch, no search)."""
        text = await self._call("interview_question", {
            "doc_id": doc_id, "question_id": question_id,
        })
        return InterviewQuestionResult.model_validate(json.loads(text))

    async def interview_followup(self, query: str, domain: str = "",
                                 top_k: int = 3) -> RetrieveContextResult:
        """Domain-scoped hybrid retrieval for follow-ups and rubric checks
        (department filter on the RAG service side)."""
        text = await self._call("interview_followup", {
            "query": query, "domain": domain, "top_k": top_k,
        })
        return RetrieveContextResult.model_validate(json.loads(text))

    async def register_bank(self, doc_id: str, markdown: str,
                            department: str, *,
                            force: bool = False) -> RegisterBankResult:
        """Registers one question bank from markdown content on the RAG
        service (in-process ingest into both legs — queryable immediately, no
        service restart). Idempotent: registering an existing doc without
        ``force`` returns ``status=already_present``. Server-side embedding of
        a large bank can exceed the 30 s default budget, so this call uses a
        120 s timeout."""
        text = await self._call("register_bank", {
            "markdown": markdown,
            "doc_id": doc_id,
            "department": department,
            "force": force,
        }, timeout_s=120.0)
        return RegisterBankResult.model_validate(json.loads(text))
