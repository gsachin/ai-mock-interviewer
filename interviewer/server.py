"""Management-plane API: health, session registry, LiveKit token issuance,
and the dynamic skill (question-bank) surface — GET /skills, POST /skills
(upload + register), POST /skills/reconcile.

Request/response only — audio never flows through this app (the voice hot
path runs in the LiveKit agent worker). Retrieval happens in the standalone
enterprise-rag-core MCP service; this app calls it through
``interviewer.rag_client.RagClient``.
"""
import threading
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from interviewer import skills
from interviewer.config import InterviewerConfig
from interviewer.rag_client import RagClient
from interviewer.state_machine import InterviewerState, Session
from interviewer.voice import AGENT_NAME

app = FastAPI(title="mock-interviewer", version="0.1.0")

config = InterviewerConfig.from_env()

# Skills API reaches the RAG service over MCP (register_bank / interview_bank).
# Module-level for seam-testing: tests swap server._rag for a stub.
_rag = RagClient(config.rag_mcp_url, token=config.rag_mcp_token)

_MAX_UPLOAD_BYTES = 1_000_000

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
        # session shape — the page shows "Question n of Y" (RCA 7.4)
        "max_questions": config.max_questions,
    }


# ── Skills (question banks) ─────────────────────────────────────────────────
# Routes MUST stay above the static mount below — the catch-all would shadow
# them. The question_banks/*.md folder is the source of truth for available
# skills; the RAG service (interview_bank probe / register_bank tool) is the
# source of truth for registration state. Everything here tolerates a RAG
# service that is down or an ERC build that predates register_bank.

def _local_shape(bank: skills.LocalBank) -> tuple[int | None, str | None]:
    try:
        headings, errors = skills.parse_markdown_shape(
            bank.path.read_text(encoding="utf-8"))
    except OSError as exc:
        return None, f"cannot read file: {exc}"
    return (len(headings), None) if not errors else (None, "; ".join(errors))


@app.get("/skills")
async def list_skills() -> dict[str, Any]:
    """Every skill available on this machine (question_banks/*.md) with its
    registration state in the RAG service, probed live. RAG down ⇒
    ``rag_ok=false`` and ``registered`` stays null — never a 500."""
    rag_ok = True
    entries: list[dict[str, Any]] = []
    for bank in skills.discover_local_banks(skills.BANK_DIR):
        sections, shape_error = _local_shape(bank)
        entry: dict[str, Any] = {
            "name": bank.name,
            "doc_id": bank.doc_id,
            "present": True,
            "sections": sections,
            "error": shape_error,
            "registered": None,
            "questions": None,
        }
        if not shape_error:
            try:
                probe = await _rag.interview_bank(bank.doc_id)
                entry["registered"] = probe.count > 0
                entry["questions"] = probe.count
            except Exception:  # RAG down / old ERC — keep the row, mark offline
                rag_ok = False
        entries.append(entry)
    return {
        "rag_ok": rag_ok,
        "skills": entries,
        "unusable_files": [p.name
                           for p in skills.unusable_bank_files(skills.BANK_DIR)],
    }


@app.post("/skills", status_code=201)
async def upload_skill(file: UploadFile = File(...)) -> dict[str, Any]:
    """Upload a question-bank .md: validates the corpus, saves it under
    question_banks/<name>.md, and registers it on the RAG service via the
    register_bank MCP tool (in-process, no restart). Uploading a skill that
    already exists — locally or in RAG — replaces it (force rebuild)."""
    try:
        name = skills.normalize_skill_name(file.filename or "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    raw = await file.read()
    if len(raw) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"file is {len(raw)} bytes — the 1 MB upload limit")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400,
                            detail="file must be UTF-8 encoded") from exc
    try:
        skills.validate_upload(text, name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    bank_dir = skills.BANK_DIR
    target = (bank_dir / f"{name}.md").resolve()
    if target.parent != bank_dir.resolve():
        raise HTTPException(status_code=400,
                            detail=f"refusing path outside question_banks: "
                                   f"{target.name}")
    bank_dir.mkdir(parents=True, exist_ok=True)
    replaced_locally = target.exists()

    # RAG registration replaces when the bank is already registered, so an
    # upload of a re-added file still lands (idempotent skip is for no-ops).
    replace = replaced_locally
    try:
        probe = await _rag.interview_bank(skills.bank_doc_id(name))
        if probe.count > 0:
            replace = True
    except Exception:
        pass  # RAG down — registration below will surface it as a 502
    target.write_text(text, encoding="utf-8")

    try:
        result = await _rag.register_bank(
            skills.bank_doc_id(name), text, name, force=replace)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"RAG registration failed: {exc} — is the RAG service up "
                   "with the register_bank tool (enterprise-rag-core Phase 4)?"
        ) from exc
    return {
        "name": name,
        "doc_id": result.doc_id,
        "sections": result.sections,
        "chunks": result.chunks,
        "status": result.status,
        "replaced": replace,
    }


@app.post("/skills/reconcile")
async def reconcile_skills() -> dict[str, Any]:
    """Register-missing: probe every local bank against the RAG service and
    register the ones with no registered questions (idempotent). Probes all
    banks BEFORE writing anything, so a RAG outage fails clean (502) with no
    partial registrations."""
    banks = skills.discover_local_banks(skills.BANK_DIR)
    if not banks:
        return {"rag_ok": True, "registered": [], "already_present": [],
                "errors": []}

    shaped: list[tuple[skills.LocalBank, str]] = []     # (bank, markdown)
    errors: list[dict[str, str]] = []
    for bank in banks:
        try:
            text = bank.path.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append({"name": bank.name, "error": f"cannot read: {exc}"})
            continue
        _headings, shape_error = _local_shape(bank)
        if shape_error:
            errors.append({"name": bank.name, "error": shape_error})
            continue
        shaped.append((bank, text))

    try:
        probes = {
            bank.name: (await _rag.interview_bank(bank.doc_id)).count
            for bank, _text in shaped
        }
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"RAG service unavailable: {exc}") from exc

    registered: list[dict[str, Any]] = []
    already_present: list[dict[str, Any]] = []
    for bank, text in shaped:
        if probes[bank.name] > 0:
            already_present.append({"name": bank.name,
                                    "questions": probes[bank.name]})
            continue
        try:
            result = await _rag.register_bank(
                skills.bank_doc_id(bank.name), text, bank.name, force=False)
            registered.append({
                "name": bank.name,
                "sections": result.sections,
                "chunks": result.chunks,
                "status": result.status,
            })
        except Exception as exc:
            errors.append({"name": bank.name, "error": str(exc)})
    return {"rag_ok": True, "registered": registered,
            "already_present": already_present, "errors": errors}


# The voice UI (web/index.html) is served from this app so the page and the
# token endpoint share an origin (no CORS). API routes above take precedence.
_web_dir = Path(__file__).resolve().parent.parent / "web"
if _web_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(_web_dir), html=True), name="web")
