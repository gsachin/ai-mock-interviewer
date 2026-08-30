"""Management-plane API (skeleton): health + session registry.

Request/response only — audio never flows through this app (the voice hot
path runs in the LiveKit agent worker). Retrieval happens in the standalone
enterprise-rag-core MCP service; this app calls it through
``interviewer.rag_client.RagClient``.
"""
import threading
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from interviewer.config import InterviewerConfig
from interviewer.state_machine import InterviewerState, Session

app = FastAPI(title="mock-interviewer", version="0.1.0")

config = InterviewerConfig.from_env()

_registry: dict[str, Session] = {}
_lock = threading.Lock()


class CreateSessionRequest(BaseModel):
    tenant_id: str = "default"
    domain: str | None = None


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "rag_mcp_url": config.rag_mcp_url,
        "rag_auth": "oidc" if config.rag_mcp_token else "none",
    }


@app.post("/sessions")
async def create_session(body: CreateSessionRequest) -> dict[str, Any]:
    session = Session(
        session_id=uuid.uuid4().hex[:12],
        tenant_id=body.tenant_id,
        domain=body.domain or config.default_domain,
    )
    with _lock:
        _registry[session.session_id] = session
    return {"session_id": session.session_id, "state": session.state.value}


@app.get("/sessions/{session_id}")
async def get_session(session_id: str) -> dict[str, Any]:
    with _lock:
        session = _registry.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="unknown session")
    return {
        "session_id": session.session_id,
        "tenant_id": session.tenant_id,
        "domain": session.domain,
        "state": session.state.value,
        "turns": [t.__dict__ for t in session.turns],
        "scores": session.scores,
    }
