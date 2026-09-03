"""Management-plane API: health, session registry, LiveKit token issuance.

Request/response only — audio never flows through this app (the voice hot
path runs in the LiveKit agent worker). Retrieval happens in the standalone
enterprise-rag-core MCP service; this app calls it through
``interviewer.rag_client.RagClient``.
"""
import threading
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from interviewer.config import InterviewerConfig
from interviewer.state_machine import InterviewerState, Session
from interviewer.voice import AGENT_NAME

app = FastAPI(title="mock-interviewer", version="0.1.0")

config = InterviewerConfig.from_env()

# The legacy static UI option (python -m http.server :8080) and the Streamlit
# UI call the token endpoint from another origin — allow the local ones.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8080", "http://127.0.0.1:8080",
        "http://localhost:8501", "http://127.0.0.1:8501",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

_registry: dict[str, Session] = {}
_lock = threading.Lock()


class CreateSessionRequest(BaseModel):
    tenant_id: str = "default"
    domain: str | None = None


class VoiceTokenRequest(BaseModel):
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


@app.post("/voice/token")
async def voice_token(body: VoiceTokenRequest) -> dict[str, Any]:
    """Mint a LiveKit JWT for one voice interview room.

    The browser joins the room with this token; LiveKit dispatches the
    ``interviewer-agent`` worker into the room (RoomAgentDispatch). The
    token endpoint itself never handles audio.
    """
    try:
        from livekit import api  # the [voice] extra
    except ImportError as exc:
        raise HTTPException(status_code=503,
            detail="livekit-api is not installed — pip install -e '.[voice]'"
        ) from exc
    if not config.livekit_api_key or not config.livekit_api_secret:
        raise HTTPException(status_code=503,
            detail="LIVEKIT_API_KEY and LIVEKIT_API_SECRET are required "
                   "(livekit-server --dev uses 'devkey' / 'secret')")

    domain = body.domain or config.default_domain
    session = Session(session_id=uuid.uuid4().hex[:12],
                      tenant_id="default", domain=domain)
    room = f"interview-{domain}-{session.session_id}"
    with _lock:
        _registry[session.session_id] = session

    token = (api.AccessToken(config.livekit_api_key, config.livekit_api_secret)
             .with_identity(f"candidate-{session.session_id}")
             .with_name("candidate")
             .with_grants(api.VideoGrants(room_join=True, room=room,
                                          can_publish=True, can_subscribe=True))
             .with_room_config(api.RoomConfiguration(
                 agents=[api.RoomAgentDispatch(agent_name=AGENT_NAME)]))
             .to_jwt())
    return {
        "livekit_url": config.livekit_url,
        "token": token,
        "room": room,
        "session_id": session.session_id,
        "domain": domain,
    }


# The voice UI (web/index.html) is served from this app so the page and the
# token endpoint share an origin (no CORS). API routes above take precedence.
_web_dir = Path(__file__).resolve().parent.parent / "web"
if _web_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(_web_dir), html=True), name="web")
