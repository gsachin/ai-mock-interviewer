"""Every HTTP route, on one APIRouter.

Lives here rather than in ``server.py`` for a structural reason: the app mounts
``StaticFiles`` at ``/`` as a catch-all, and FastAPI matches in declaration
order, so any route declared AFTER the mount is silently shadowed. That was
enforced only by a comment at the bottom of ``server.py``, and this codebase
has already shipped one silent-shadowing bug (an over-strict data filter that
ate every message — see docs/RCA_VOICE_BROWSER_INTERVIEW.md). Registering the
router before the mount makes the ordering structural instead of conventional.

``tests/test_server_probes.py`` asserts the probes are reachable, which is the
regression test for exactly that class of mistake.
"""
import uuid
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from interviewer import skills

router = APIRouter()


def _srv():
    """Late-bound access to server state.

    Imported here, not at module scope: see ``interviewer/api/__init__.py``.
    Resolving on every call is what keeps the ``monkeypatch.setattr(server,
    "_store", ...)`` seam working.
    """
    from interviewer import server
    return server


# A reserved key that can never collide with a real session id (those are 12
# hex characters). Readiness reads it and expects None: a *successful miss* is
# the proof the store answers, and it needs no extra method on the protocol --
# `ping()` would have to be added to SessionStore and to both implementations
# just to say what a harmless GET already says.
_READYZ_PROBE_KEY = "__readyz__"


class CreateSessionRequest(BaseModel):
    tenant_id: str = "default"
    domain: str | None = None


class VoiceTokenRequest(BaseModel):
    domain: str | None = None


# ── probes ──────────────────────────────────────────────────────────────────

@router.get("/healthz")
async def healthz() -> dict[str, Any]:
    """Liveness. Checks nothing, deliberately.

    A liveness probe that consults dependencies turns someone else's outage
    into a restart loop of healthy pods: a Redis blip would kill every API
    replica at once, and the restart would not fix Redis.
    """
    return {"status": "ok"}


@router.get("/readyz")
async def readyz() -> JSONResponse:
    """Readiness: can this pod actually serve?

    Reports per-dependency state so a 503 says *which* thing is wrong rather
    than just that something is. Always 200- or 503-bodied — never raises — so
    the breakdown survives into whatever is scraping it.

    RAG is gated behind ``INTERVIEW_READY_REQUIRES_RAG``, **default off**. The
    app is explicitly built to survive a RAG outage: ``GET /skills`` reports
    ``rag_ok=false`` rather than failing, and an interview degrades to
    last-known rubric. Making RAG mandatory for readiness would invert that —
    a RAG blip would pull the pods out of service and take down ``/voice/token``
    and the static UI with it, which are the two things that must keep working.
    """
    srv = _srv()
    checks: dict[str, str] = {}
    ok = True

    # Session store. A read of the reserved key proves reachability; a broken
    # connection raises rather than returning None, which is the distinction.
    try:
        await srv._store.load(_READYZ_PROBE_KEY)
        checks["store"] = "ok"
    except Exception as exc:
        checks["store"] = f"fail: {type(exc).__name__}: {exc}"
        ok = False

    # Question banks. Present-but-unreadable is a real failure: skills are how
    # an interview picks its questions, and an empty bank yields a
    # zero-question interview (see the EmptyBankError path in brain.py).
    try:
        bank_dir = skills.bank_dir()
        if bank_dir.is_dir():
            banks = skills.discover_local_banks(bank_dir)
            checks["banks"] = f"ok ({len(banks)} banks)"
        else:
            checks["banks"] = f"fail: {bank_dir} is not a directory"
            ok = False
    except Exception as exc:
        checks["banks"] = f"fail: {type(exc).__name__}: {exc}"
        ok = False

    if srv.config.ready_requires_rag:
        try:
            await srv._rag.interview_bank("bank-__readyz__")
            checks["rag"] = "ok"
        except Exception as exc:
            checks["rag"] = f"fail: {type(exc).__name__}: {exc}"
            ok = False
    else:
        checks["rag"] = "not required (INTERVIEW_READY_REQUIRES_RAG=false)"

    return JSONResponse(
        status_code=200 if ok else 503,
        content={"status": "ok" if ok else "degraded", "checks": checks},
    )


@router.get("/health")
async def health() -> dict[str, Any]:
    """Legacy alias, kept because it is not ours to retire.

    ``start_services.ps1`` polls this URL and greps the BODY for the RAG URL
    when it prints its summary, and the docs reference it. Returning the same
    shape through the new probes would break that script for no gain, so this
    keeps its original contract and points at /healthz and /readyz for
    anything new.
    """
    srv = _srv()
    return {
        "status": "ok",
        "rag_mcp_url": srv.config.rag_mcp_url,
        "rag_auth": "oidc" if srv.config.rag_mcp_token else "none",
    }


# ── sessions ────────────────────────────────────────────────────────────────

@router.post("/sessions")
async def create_session(body: CreateSessionRequest) -> dict[str, Any]:
    srv = _srv()
    from interviewer.state_machine import Session

    session = Session(
        session_id=uuid.uuid4().hex[:12],
        tenant_id=body.tenant_id,
        domain=body.domain or srv.config.default_domain,
    )
    await srv._store.save(session.session_id, session.to_dict())
    return {"session_id": session.session_id, "state": session.state.value}


@router.get("/sessions/{session_id}")
async def get_session(session_id: str) -> dict[str, Any]:
    from interviewer.state_machine import Session

    srv = _srv()
    data = await srv._store.load(session_id)
    if data is None:
        raise HTTPException(status_code=404, detail="unknown session")
    session = Session.from_dict(data)
    return {
        "session_id": session.session_id,
        "tenant_id": session.tenant_id,
        "domain": session.domain,
        "state": session.state.value,
        "turns": [t.__dict__ for t in session.turns],
        "scores": session.scores,
    }


@router.post("/voice/token")
async def voice_token(body: VoiceTokenRequest) -> dict[str, Any]:
    """Mint a LiveKit JWT for one voice interview room.

    The browser joins the room with this token; LiveKit dispatches the
    ``interviewer-agent`` worker into the room (RoomAgentDispatch). The
    token endpoint itself never handles audio.
    """
    from interviewer.state_machine import Session
    from interviewer.voice import AGENT_NAME

    srv = _srv()
    try:
        from livekit import api  # the [voice] extra
    except ImportError as exc:
        raise HTTPException(status_code=503,
            detail="livekit-api is not installed — pip install -e '.[voice]'"
        ) from exc
    if not srv.config.livekit_api_key or not srv.config.livekit_api_secret:
        raise HTTPException(status_code=503,
            detail="LIVEKIT_API_KEY and LIVEKIT_API_SECRET are required "
                   "(livekit-server --dev uses 'devkey' / 'secret')")

    domain = body.domain or srv.config.default_domain
    session = Session(session_id=uuid.uuid4().hex[:12],
                      tenant_id="default", domain=domain)
    room = f"interview-{domain}-{session.session_id}"
    await srv._store.save(session.session_id, session.to_dict())

    token = (api.AccessToken(srv.config.livekit_api_key,
                             srv.config.livekit_api_secret)
             .with_identity(f"candidate-{session.session_id}")
             .with_name("candidate")
             .with_grants(api.VideoGrants(room_join=True, room=room,
                                          can_publish=True, can_subscribe=True))
             .with_room_config(api.RoomConfiguration(
                 agents=[api.RoomAgentDispatch(agent_name=AGENT_NAME)]))
             .to_jwt())
    return {
        "livekit_url": srv.config.livekit_url,
        "token": token,
        "room": room,
        "session_id": session.session_id,
        "domain": domain,
        # session shape — the page shows "Question n of Y" (RCA 7.4)
        "max_questions": srv.config.max_questions,
    }


# ── skills (question banks) ─────────────────────────────────────────────────
# The question_banks/*.md folder is the source of truth for available skills;
# the RAG service (interview_bank probe / register_bank tool) is the source of
# truth for registration state. Everything here tolerates a RAG service that is
# down or an ERC build that predates register_bank.

_MAX_UPLOAD_BYTES = 1_000_000


def _local_shape(bank: skills.LocalBank) -> tuple[int | None, str | None]:
    try:
        headings, errors = skills.parse_markdown_shape(
            bank.path.read_text(encoding="utf-8"))
    except OSError as exc:
        return None, f"cannot read file: {exc}"
    return (len(headings), None) if not errors else (None, "; ".join(errors))


@router.get("/skills")
async def list_skills() -> dict[str, Any]:
    """Every skill available (question_banks/*.md) with its registration state
    in the RAG service, probed live. RAG down ⇒ ``rag_ok=false`` and
    ``registered`` stays null — never a 500."""
    srv = _srv()
    rag_ok = True
    entries: list[dict[str, Any]] = []
    for bank in skills.discover_local_banks(skills.bank_dir()):
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
                probe = await srv._rag.interview_bank(bank.doc_id)
                entry["registered"] = probe.count > 0
                entry["questions"] = probe.count
            except Exception:  # RAG down / old ERC — keep the row, mark offline
                rag_ok = False
        entries.append(entry)
    return {
        "rag_ok": rag_ok,
        "skills": entries,
        "unusable_files": [p.name
                           for p in skills.unusable_bank_files(skills.bank_dir())],
    }


@router.post("/skills", status_code=201)
async def upload_skill(file: UploadFile = File(...)) -> dict[str, Any]:
    """Upload a question-bank .md: validates the corpus, saves it under
    question_banks/<name>.md, and registers it on the RAG service via the
    register_bank MCP tool (in-process, no restart). Uploading a skill that
    already exists — locally or in RAG — replaces it (force rebuild)."""
    srv = _srv()
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

    bank_dir = skills.bank_dir()
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
        probe = await srv._rag.interview_bank(skills.bank_doc_id(name))
        if probe.count > 0:
            replace = True
    except Exception:
        pass  # RAG down — registration below will surface it as a 502
    target.write_text(text, encoding="utf-8")

    try:
        result = await srv._rag.register_bank(
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


@router.post("/skills/reconcile")
async def reconcile_skills() -> dict[str, Any]:
    """Register-missing: probe every local bank against the RAG service and
    register the ones with no registered questions (idempotent). Probes all
    banks BEFORE writing anything, so a RAG outage fails clean (502) with no
    partial registrations."""
    srv = _srv()
    banks = skills.discover_local_banks(skills.bank_dir())
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
            bank.name: (await srv._rag.interview_bank(bank.doc_id)).count
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
            result = await srv._rag.register_bank(
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
