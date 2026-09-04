"""Phase 4 skills API: GET /skills, POST /skills (upload + register),
POST /skills/reconcile — over TestClient with a stubbed RAG seam and a
tmp question_banks folder. The static mount must never shadow these routes."""
import io
import types

import pytest
from fastapi.testclient import TestClient

from interviewer import server as server_mod
from interviewer import skills

GOOD = """# HTML Question Bank

## Semantic HTML

Explain semantic HTML. Expected points: meaning of elements, accessibility
benefits, and when a div is still appropriate.

## Forms and input types

Explain HTML forms. Expected points: labels, input types, validation, and
submission methods.
"""

BANK = "# JavaScript Question Bank\n\n## Closures\n\nWhat is a closure? " \
       "Expected points: lexical scope, private state, memory considerations.\n"


class StubRag:
    """Records register_bank calls; interview_bank answers from a dict.
    ``probe_error`` / ``register_error`` simulate a RAG outage."""

    def __init__(self, registered: dict[str, int] | None = None):
        self.registered = registered or {}
        self.register_calls: list[tuple[str, str, bool]] = []
        self.probe_error: Exception | None = None
        self.register_error: Exception | None = None

    async def interview_bank(self, doc_id: str):
        if self.probe_error:
            raise self.probe_error
        return types.SimpleNamespace(count=self.registered.get(doc_id, 0))

    async def register_bank(self, doc_id: str, markdown: str,
                            department: str, *, force: bool = False):
        self.register_calls.append((doc_id, department, force))
        if self.register_error:
            raise self.register_error
        was_registered = doc_id in self.registered
        sections = len(markdown.split("\n## ")) if "## " in markdown else 1
        self.registered[doc_id] = sections
        return types.SimpleNamespace(
            doc_id=doc_id, tenant_id="default", sections=sections,
            chunks=sections,
            status="already_present" if was_registered else "registered")


@pytest.fixture
def api(tmp_path, monkeypatch):
    """Server with a stub RAG and a tmp question_banks folder."""
    stub = StubRag()
    monkeypatch.setattr(server_mod, "_rag", stub)
    monkeypatch.setattr(skills, "BANK_DIR", tmp_path)
    return stub, TestClient(server_mod.app)


def _bank_file(tmp_path, name="html.md", text=GOOD):
    (tmp_path / name).write_text(text, encoding="utf-8")


# ── GET /skills ─────────────────────────────────────────────────────────────

def test_list_skills_reports_local_banks_and_registration(api, tmp_path):
    stub, client = api
    _bank_file(tmp_path, "html.md")
    _bank_file(tmp_path, "css.md", GOOD.replace("# HTML", "# CSS"))
    stub.registered["bank-html"] = 15   # only html is registered in RAG

    resp = client.get("/skills")
    assert resp.status_code == 200
    body = resp.json()
    assert body["rag_ok"] is True
    by_name = {s["name"]: s for s in body["skills"]}
    assert set(by_name) == {"html", "css"}
    assert by_name["html"]["registered"] is True
    assert by_name["html"]["questions"] == 15
    assert by_name["html"]["sections"] == 2
    assert by_name["css"]["registered"] is False
    assert by_name["css"]["questions"] == 0
    assert body["unusable_files"] == []


def test_list_skills_tolerates_rag_down(api, tmp_path):
    stub, client = api
    _bank_file(tmp_path, "html.md")
    stub.probe_error = RuntimeError("RAG down")

    resp = client.get("/skills")
    assert resp.status_code == 200            # never a 500 on RAG outage
    body = resp.json()
    assert body["rag_ok"] is False
    entry = body["skills"][0]
    assert entry["registered"] is None and entry["questions"] is None


def test_list_skills_flags_unusable_files(api, tmp_path):
    _stub, client = api
    _bank_file(tmp_path, "Bad Name.md")
    resp = client.get("/skills")
    assert resp.json()["unusable_files"] == ["Bad Name.md"]


# ── POST /skills (upload + register) ────────────────────────────────────────

def test_upload_new_skill_writes_file_and_registers(api, tmp_path):
    stub, client = api
    resp = client.post(
        "/skills",
        files={"file": ("react.md", io.BytesIO(BANK.encode()),
                        "text/markdown")})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "react" and body["replaced"] is False
    assert body["status"] == "registered"
    assert body["doc_id"] == "bank-react"

    assert (tmp_path / "react.md").read_text(encoding="utf-8") == BANK
    assert stub.register_calls == [("bank-react", "react", False)]


def test_upload_existing_skill_replaces_it(api, tmp_path):
    stub, client = api
    _bank_file(tmp_path, "html.md", GOOD)
    stub.registered["bank-html"] = 15     # already in RAG too
    updated = GOOD.replace("and when a div", "and when a span")

    resp = client.post("/skills",
                       files={"file": ("html.md",
                                       io.BytesIO(updated.encode()),
                                       "text/markdown")})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["replaced"] is True
    assert stub.register_calls == [("bank-html", "html", True)]
    assert "span" in (tmp_path / "html.md").read_text(encoding="utf-8")


def test_upload_validation_rejections(api, tmp_path):
    _stub, client = api
    # non-.md name
    assert client.post("/skills", files={"file": ("html.txt", b"x", "text/plain")}
                       ).status_code == 400
    # section-less corpus
    r = client.post("/skills",
                    files={"file": ("x.md", b"just prose, no headings",
                                    "text/markdown")})
    assert r.status_code == 400
    assert "question sections" in r.json()["detail"]
    # corpus without the Expected-points convention
    r = client.post("/skills",
                    files={"file": ("x.md",
                                    b"## Q\n\nbody with no rubric",
                                    "text/markdown")})
    assert r.status_code == 400
    # oversized (limit 1 MB)
    big = b"# t\n\n## q\n\n" + (b"expected points: " + b"x" * 2000) * 600
    assert len(big) > 1_000_000
    r = client.post("/skills", files={"file": ("big.md", big, "text/markdown")})
    assert r.status_code == 400 and "1 MB" in r.json()["detail"]


def test_upload_surfaces_rag_failure_as_502(api, tmp_path):
    stub, client = api
    stub.register_error = RuntimeError("MCP tool register_bank failed")
    resp = client.post("/skills",
                       files={"file": ("react.md", io.BytesIO(BANK.encode()),
                                       "text/markdown")})
    assert resp.status_code == 502
    assert "register_bank" in resp.json()["detail"]


# ── POST /skills/reconcile ──────────────────────────────────────────────────

def test_reconcile_registers_only_missing_banks(api, tmp_path):
    stub, client = api
    _bank_file(tmp_path, "html.md")            # registered already
    _bank_file(tmp_path, "javascript.md", BANK)
    stub.registered["bank-html"] = 15

    resp = client.post("/skills/reconcile")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rag_ok"] is True
    assert [r["name"] for r in body["already_present"]] == ["html"]
    assert [r["name"] for r in body["registered"]] == ["javascript"]
    assert body["errors"] == []
    assert stub.register_calls == [("bank-javascript", "javascript", False)]

    # second reconcile is a no-op (all already present)
    resp = client.post("/skills/reconcile")
    assert resp.json()["registered"] == []
    assert len(stub.register_calls) == 1


def test_reconcile_rag_down_fails_without_partial_registrations(api, tmp_path):
    stub, client = api
    _bank_file(tmp_path, "html.md")
    _bank_file(tmp_path, "javascript.md", BANK)
    stub.probe_error = RuntimeError("RAG down")

    resp = client.post("/skills/reconcile")
    assert resp.status_code == 502
    assert stub.register_calls == []          # nothing was written


# ── route precedence (static mount must not shadow the API) ─────────────────

def test_skills_routes_not_shadowed_by_static_mount(api):
    _stub, client = api
    resp = client.get("/skills")
    assert resp.status_code == 200
    assert resp.json().get("rag_ok") is not None    # JSON, not index.html
    resp = client.post("/skills/reconcile")
    assert resp.status_code == 200
    assert set(resp.json()) == {"rag_ok", "registered", "already_present",
                                "errors"}


# ── Skill Update page is served and reachable from the voice page ───────────

def test_skill_update_page_served_with_upload_surface(api):
    _stub, client = api
    resp = client.get("/skills.html")
    assert resp.status_code == 200
    text = resp.text
    assert "Skill Update" in text
    assert "Upload a skill" in text          # upload card is present
    assert "/skills/reconcile" in text       # Register missing wiring exists
    assert text.count("<html") == 1          # a page, not a JSON 404


def test_voice_page_links_to_skill_update(api):
    _stub, client = api
    resp = client.get("/")
    assert resp.status_code == 200
    assert "AI Mock Interviewer" in resp.text
    assert "/skills.html" in resp.text        # header nav link present
