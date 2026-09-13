"""US-006 — liveness/readiness split, and the route-shadowing regression.

The shadowing test is the reason the router extraction exists. FastAPI matches
routes in declaration order and the app mounts ``StaticFiles`` at ``/`` as a
catch-all, so anything declared after the mount is silently unreachable — a
valid app, a 200 response, and the wrong handler. This codebase has shipped
that class of bug once already (an over-strict data filter that ate every
message), so it gets a test rather than a comment.
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from interviewer import server as srv
from interviewer import skills
from interviewer.session_store import InMemorySessionStore


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(srv, "_store", InMemorySessionStore())
    return TestClient(srv.app)


# ── the shadowing regression ────────────────────────────────────────────────

def test_static_mount_is_the_last_route() -> None:
    """Structural assertion. A catch-all mount declared before the API routes
    shadows every one of them."""
    paths = [getattr(r, "path", None) for r in srv.app.routes]
    mount_idx = [i for i, r in enumerate(srv.app.routes)
                 if type(r).__name__ == "Mount"]
    assert mount_idx, "expected the web/ StaticFiles mount to exist"
    assert mount_idx[-1] == len(srv.app.routes) - 1, (
        f"the static mount at index {mount_idx[-1]} is not last "
        f"(total routes: {len(paths)}). Any route after it is shadowed."
    )


@pytest.mark.parametrize("path", ["/healthz", "/readyz", "/health"])
def test_probe_is_not_shadowed_by_the_static_mount(client, path: str) -> None:
    """Behavioural counterpart: the response must be our JSON, not the HTML
    the static handler would serve for an unknown path."""
    resp = client.get(path)
    assert resp.status_code in (200, 503)
    assert resp.headers["content-type"].startswith("application/json"), (
        f"{path} returned {resp.headers.get('content-type')!r} — the static "
        f"mount is shadowing it"
    )


# ── liveness ────────────────────────────────────────────────────────────────

def test_healthz_checks_nothing(client) -> None:
    """Liveness must not consult dependencies: a Redis blip would otherwise
    restart every healthy API pod at once, and a restart cannot fix Redis."""
    assert client.get("/healthz").json() == {"status": "ok"}


def test_healthz_stays_ok_when_everything_else_is_broken(client, monkeypatch) -> None:
    class Exploding:
        async def load(self, _sid):
            raise RuntimeError("store down")

    monkeypatch.setattr(srv, "_store", Exploding())
    monkeypatch.setattr(skills, "bank_dir", lambda: Path("/nonexistent"))

    assert client.get("/healthz").status_code == 200


# ── readiness ───────────────────────────────────────────────────────────────

def test_readyz_ok_and_reports_each_dependency(client) -> None:
    resp = client.get("/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["checks"]["store"] == "ok"
    assert body["checks"]["banks"].startswith("ok")


def test_readyz_503_when_the_store_is_unreachable(client, monkeypatch) -> None:
    class Exploding:
        async def load(self, _sid):
            raise TimeoutError("Timeout connecting to server")

    monkeypatch.setattr(srv, "_store", Exploding())

    resp = client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["checks"]["store"].startswith("fail")
    # The healthy dependency must still be reported as healthy: the value of
    # the breakdown is naming WHICH thing broke.
    assert resp.json()["checks"]["banks"].startswith("ok")


def test_readyz_503_when_banks_are_missing(client, monkeypatch) -> None:
    monkeypatch.setattr(skills, "bank_dir", lambda: Path("/nonexistent"))

    resp = client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["checks"]["banks"].startswith("fail")
    assert resp.json()["checks"]["store"] == "ok"


def test_readyz_probe_uses_a_key_that_cannot_collide(client) -> None:
    """The readiness read must not touch a real session. Session ids are 12
    hex characters, so the reserved key is safe by construction — asserted
    rather than assumed."""
    from interviewer.api.routes import _READYZ_PROBE_KEY

    # Session ids are uuid4().hex[:12]: twelve lowercase hex characters. The
    # reserved key must be unable to match one, so the invariant is "not a
    # 12-char hex string" rather than any particular length.
    is_session_shaped = (len(_READYZ_PROBE_KEY) == 12
                         and all(c in "0123456789abcdef"
                                 for c in _READYZ_PROBE_KEY))
    assert not is_session_shaped


# ── the RAG gate, and why it defaults off ───────────────────────────────────

def test_rag_is_not_required_by_default(client, monkeypatch) -> None:
    """A RAG outage must NOT take the API out of service: /voice/token and the
    static UI are exactly what needs to keep working while retrieval is down."""
    class RagDown:
        async def interview_bank(self, _doc_id):
            raise ConnectionError("RAG unreachable")

    monkeypatch.setattr(srv, "_rag", RagDown())

    resp = client.get("/readyz")
    assert resp.status_code == 200, (
        "RAG must not gate readiness by default — a RAG blip would pull the "
        "API pods out of service and take the token endpoint down with them"
    )
    assert "not required" in resp.json()["checks"]["rag"]


def test_rag_gates_readiness_when_explicitly_enabled(client, monkeypatch) -> None:
    """When an operator does opt in, a RAG outage must fail readiness."""
    class RagDown:
        async def interview_bank(self, _doc_id):
            raise ConnectionError("RAG unreachable")

    # InterviewerConfig is a frozen dataclass — replace the whole config
    # rather than mutating a field. (Learning it here rather than loosening
    # the dataclass: frozen config is a property worth keeping.)
    import dataclasses
    monkeypatch.setattr(
        srv, "config",
        dataclasses.replace(srv.config, ready_requires_rag=True))
    monkeypatch.setattr(srv, "_rag", RagDown())

    resp = client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["checks"]["rag"].startswith("fail")


# ── the legacy alias ────────────────────────────────────────────────────────

def test_health_keeps_its_original_body(client) -> None:
    """start_services.ps1 greps this body for the RAG URL when printing its
    summary, so the shape is a contract with a script, not just a URL."""
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert "rag_mcp_url" in body
    assert body["rag_auth"] in ("oidc", "none")


# ── the boolean parser ──────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("false", False), ("FALSE", False), ("0", False), ("no", False),
    ("off", False), ("", False), ("true", True), ("1", True), ("yes", True),
])
def test_env_booleans_are_parsed_not_truthy_tested(raw, expected) -> None:
    """`bool(os.environ.get(...))` would read "false" as True — the exact
    opposite of what the operator wrote, on the flag that decides whether a
    RAG outage removes the API from service."""
    from interviewer.config import _as_bool

    assert _as_bool(raw) is expected
