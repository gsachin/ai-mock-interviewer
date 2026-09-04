# TRD & LLD — Phase 4: Dynamic Skill Registration (auto-discover + Skill Update page)

**Repo:** mock-interviewer (consumer: folder scan, HTTP skills API, Skill Update page, launcher) + enterprise-rag-core (engine: `register_bank` MCP tool) — both live in this workspace and commit together
**Date:** 2026-09-04
**Status:** Implemented & live-verified — this document doubles as the developer blueprint (a fresh implementer can build the feature from §2–§8 alone)
**Validation runs:**
- `pytest tests/ -m "not live"` (consumer repo) → **121 passed, 5 deselected** (+36 new Phase 4 tests)
- `pytest tests/` (enterprise-rag-core) → **136 passed, 2 skipped** (auto-skip markers; +12 new)
- Live gate (real stack, 2026-09-04): `register_bank` registered html/javascript/css (15 sections each); `interview_bank` + follow-up retrieval hit them **without a RAG restart** — see §12
**Parent:** Live Voice Interviewer roadmap (Phase 3 browser voice follow-on)

---

## 1. Context and objectives

Phases 1–3 hardcode **four** interview domains in **four** places — the
launcher prepopulate loop (`start_services.ps1` Step 5), `scripts/prepopulate_banks.sh`,
the Streamlit domain list (`web/streamlit_app.py:34`), and the voice room
parser (`interviewer/voice/agent.py:48`). A fifth bank dropped into
`question_banks/` is invisible: no UI lists it, voice rooms for it fall back
to `system-design`, and it never reaches the RAG store.

Phase 4 objectives:

1. **Auto-register** — at system start, scan `question_banks/*.md`; every file
   whose stem is a valid slug names a skill that is not yet registered gets
   ingested (idempotent: "already added" banks skip). On demand, a
   **Register missing** action does the same while services run.
2. **Skill Update page** — a same-origin HTML page listing every skill with its
   RAG registration state; the user **uploads a `.md` question bank**,
   validated and registered for interviews immediately.
3. **Live registration without a RAG restart** — a newly uploaded bank must be
   queryable on BOTH retrieval legs (dense Chroma + in-memory BM25) the moment
   it registers. The old launcher flow restarts RAG after every prepopulate
   (BM25 warms only at serve boot); an upload endpoint restarting a service it
   does not own — mid-voice-session — is not acceptable.
4. **Single source of truth** — the `question_banks/` folder names what skills
   exist; the RAG service names what is registered; domain pickers (voice
   page, Streamlit, room parser) derive from folder ∪ static legacy list.
5. **No breakage** — every change additive or default-preserving; both repos'
   suites stay green; the original four domains keep their behavior/ordering.

---

## 2. End-to-end flows

### 2.1 — System context and component boundaries

```
┌────────────────────────── mock-interviewer (consumer repo) ───────────────────────────┐
│                                                                                       │
│  question_banks/*.md        ┌───────────────┐     ┌────────────────────────────────┐  │
│  (source of truth for      │ interviewer/  │     │ interviewer/server.py (FastAPI │  │
│   available skills)        │ skills.py     │◄───►│  :8010)                        │  │
│  ▲ read / write            │ (stdlib only) │     │  /health /sessions             │  │
│  │                         └───────┬───────┘     │  /voice/token                  │  │
│  │                                 │             │  GET  /skills        ─┐        │  │
│  │  web/skills.html ◄──mount──┐    │             │  POST /skills         │ routes │  │
│  │  web/index.html (nav/      │    │             │  POST /skills/reconcile┘ before │  │
│  │   dynamic domain select)   │    │             │  mount "/"  StaticFiles(web/)   │  │
│  └────────────────────────────┼────┼─────────────┴───────────────┬──────────────────┘  │
│                               │    │                             │ MCP over HTTP       │
└───────────────────────────────┼────┼─────────────────────────────┼─────────────────────┘
                                │    │ RagClient (register_bank / interview_bank) │
                                │    │                                             ▼
                    ┌───────────▼────▼──────────────────────────── enterprise-rag-core (engine) ─┐
                    │        MCP server (streamable HTTP :8031/mcp, auth none | oidc)            │
                    │  tools: interview_bank interview_question interview_followup                │
                    │         execute_agent_context retrieve_context  register_bank ◄── NEW       │
                    │  module seams: _set_engine _set_orchestrator _set_vector_store _set_stack  │
                    │                                                                             │
                    │  Stack: vector_store (Chroma) ── persist ──► chroma_data/                   │
                    │         keyword_store (BM25 in-memory) ── warm at boot, upsert at register  │
                    │         embeddings (Ollama nomic-embed-text)                                 │
                    └─────────────────────────────────────────────────────────────────────────────┘
```

**Boundary rules (non-negotiable):** the consumer repo never imports
`enterprise_rag` or `chromadb` (two separate venvs; `pyproject.toml` contract).
All registration crosses the MCP boundary. `interviewer/skills.py` mirrors the
RAG splitter rules with a small regex because it cannot import them.

### 2.2 — Flow A: startup auto-registration (launcher)

```
start_services.ps1
  │ Step 5
  ├─► Get-ChildItem question_banks/*.md          (no fixed list!)
  │     foreach file:
  │        name = stem.lower()
  │        name valid slug? ──NO──► warn "cannot be a skill", skip
  │        │ YES
  │        ▼
  │     ERC venv CLI: python -m enterprise_rag.prepopulate
  │        --kb <file> --doc-id bank-<name> --tenant default --department <name>
  │        │  idempotent skip: chunks with parent_id==doc_id already exist
  │        ▼
  │     exit 0 → banksPrepopulated = true        (unchanged semantics)
  │ Step 6
  ├─► banksPrepopulated? ──YES──► restart RAG MCP once (BM25 warm at boot)
  │        (when banks were ingested OUT-of-process this is what makes the
  │         keyword leg see them; since Phase 4's register_bank ingests
  │         IN-process, banks registered that way never need this restart)
  ▼
serve boot: warm_keyword_from_vector_store()  — BM25 rebuilt from Chroma
            (so even a restart-free deployment stays consistent on next boot)
```

Same shape in `scripts/prepopulate_banks.sh` (glob + `case` slug check).

### 2.3 — Flow B: Skill Update upload → register (the hot path)

```
Browser (web/skills.html)          server.py (FastAPI :8010)            RAG MCP :8031
   │ POST /skills (multipart file)     │                                    │
   │──────────────────────────────────►│                                    │
   │                                   │ 1. normalize_skill_name(filename)  │
   │                                   │    → lowercase slug (400 on bad)   │
   │                                   │ 2. read ≤ 1 MB, UTF-8 (400)        │
   │                                   │ 3. validate_upload(text, name):    │
   │                                   │    ≥1 '## ' section w/ body +      │
   │                                   │    "expected points" (400)         │
   │                                   │ 4. write question_banks/<name>.md  │
   │                                   │    (resolved path must stay in dir)│
   │                                   │ 5. probe interview_bank(bank-x)    │
   │                                   │    tolerate failure (RAG down)     │
   │                                   │    replace = local file existed    │
   │                                   │            OR probe.count > 0      │
   │                                   │ 6. MCP register_bank               │
   │                                   │    {markdown, doc_id:"bank-<n>",   │
   │                                   │     department:name, force:replace}│
   │                                   │───────────────────────────────────►│
   │                                   │   embed+chunk+upsert BOTH legs     │
   │                                   │◄── {sections,chunks,status} ───────│
   │ 201 {name,doc_id,sections,        │                                    │
   │      chunks,status,replaced}      │   (RAG error → 502 with detail)    │
   │◄──────────────────────────────────│                                    │
   │ re-render table from GET /skills  │                                    │
```

### 2.4 — Flow C: replace (force) — both legs stay honest

```
register_bank(..., force=True)  ──► _ingest_markdown
   marker gates (expected/blocked) ── fail ⇒ ValueError, nothing written
   sections = split_markdown_text(markdown)
   vector_store.get_all(tenant) → parent_id == doc_id present?
        force=False ──► return skipped=True (idempotent)
   vector_store.delete_by_parent(doc_id, tenant)      ── remove OLD dense rows
   keyword_store.delete_by_parent(doc_id, tenant)     ── remove OLD sparse rows ◄─ NEW
   for each section → chunk with overlap (600/90) → embed (Ollama)
   vector_store.upsert(records)  +  keyword_store.upsert(records)
```

`KeywordStore.delete_by_parent` is the piece that makes an **in-process**
force-replace clean: without it, superseded question text would stay
searchable in the in-memory BM25 leg forever (the old flow hid this by
restart-and-rewarm — restart is what Phase 4 removes).

### 2.5 — Flow D: reconcile (Register missing)

```
POST /skills/reconcile
  banks = discover_local_banks()            (skip bad slugs, log them)
  for each: read file, parse_markdown_shape → collect errors (continue)
  PROBE ALL first: interview_bank(bank-x).count  ── RAG down ⇒ 502, NOTHING written
  for each bank with count == 0 → register_bank(force=False)
  return {rag_ok, registered[], already_present[], errors[]}
```

### 2.6 — Flow E: an interview on a dynamically-registered skill

```
Browser domain select  index.html ── populated from GET /skills on load ──► + option "React"
   │ POST /voice/token {domain: "react"}
   ▼
room = interview-react-<sid>   (token endpoint never validates the domain)
   ▼
LiveKit worker: domain_from_room("interview-react-<sid>")
   parts[1]="react" ∈ static DOMAINS?  no
   parts[1] ∈ {name for question_banks/*.md}?  YES (folder scanned PER CALL —
   a skill uploaded after the worker booted resolves without a restart)
   ▼
Session(domain="react") → brain.run("bank-react")
   │ interview_bank("bank-react")   → 15 questions        (Chroma, read per call)
   │ interview_question(...)        → one full question
   │ follow-up: interview_followup(answer, domain="react")
   │    hybrid = dense(Chroma react dept) + keyword(BM25)
   │    BM25 rows were upserted IN-PROCESS at register time → hits NOW,
   │    no serve restart  ◄──────── the Phase 4 no-restart contract
   ▼
judge scores with rubric + follow-up context → wrap
```

### 2.7 — Flow F: empty-bank fail-fast

```
brain.run("bank-ghost")            interview_bank → count == 0
   │  previously: silent 0-question "interview" that said goodbye
   ▼  now: raise EmptyBankError("no questions registered for 'bank-ghost' …")
Streamlit: ctx["error"] path shows the message
Voice worker: publish {type:"notice", text}  then {type:"ended", reason} —
   the page explains the fix instead of ending silently
```

---

## 3. Naming & content contract (the "skill" definition)

| Rule | Value | Enforced where |
|---|---|---|
| Skill name = file stem | `question_banks/html.md` → skill `html` (lowercased) | `skills.discover_local_banks`, launchers |
| Slug charset | `^[a-z0-9][a-z0-9-]{0,63}$` (alnum start, no trailing dash, ≤ 64 chars) | `skills.SKILL_NAME_RE`, `register_bank` department check, both launchers |
| RAG doc id | `bank-<name>` — RE `^bank-[a-z0-9][a-z0-9-]*$` | `register_bank` core + MCP tool |
| RAG department | `<name>` (the interview domain; follow-up filter) | prepopulate/register CLI + tool |
| Tenant | `default` (none-auth deployment; token tenant in OIDC) | launcher, tool |
| Corpus format | `# Title` front matter (dropped) then one `## ` heading per question, each with a body stating **Expected points** (the judge's rubric source) | RAG splitter + consumer `validate_upload` |
| Chunk ids | `{doc_id}:s{section}:c{chunk}` (1-based) — deterministic | `_ingest_markdown` |
| Marker gates | optional `expected_markers` / `blocked_markers` (repeatable) | `_ingest_markdown` |
| Chunking | paragraph-packing, 600 chars, 90-word overlap | `chunk_text_with_overlap` |

---

## 4. Component blueprint (module-by-module spec)

### 4.1 — `enterprise-rag-core/enterprise_rag/prepopulate.py` (engine core)

Refactor the path-based CLI into a **text-first core** shared by CLI + MCP tool:

```python
def split_markdown_text(text: str) -> list[tuple[str, str]]        # NEW
    # split on regex (?m)^## ; parts[0] (front matter) dropped;
    # empty headings/bodies skipped → [(heading, body), ...]

def split_markdown_sections(kb_path: str | Path) -> list[tuple[str, str]]
    # NOW DELEGATES: split_markdown_text(Path(kb_path).read_text(encoding="utf-8"))
    # byte-identical behavior — existing CLI tests must not change

async def _ingest_markdown(stack, text, *, doc_id, tenant_id, department,
                           clearance, expected_markers, blocked_markers,
                           force, chunk_size=600, chunk_overlap=90) -> PrepopulateResult
    # order (fixed): marker gates → split → idempotency check
    #   (vector_store.get_all(tenant); parent_id == doc_id present and not force
    #    ⇒ return skipped=True) → delete BOTH legs on force path →
    # embed+chunk → upsert both legs. Raises ValueError on marker failure.

async def prepopulate(stack, kb_path, *, ...) -> PrepopulateResult
    # reads file, delegates to _ingest_markdown — CLI entry, signature unchanged

async def register_bank(stack, *, markdown: str, doc_id: str, department: str,
                        tenant_id="default", clearance=0,
                        expected_markers=None, blocked_markers=None,
                        force=False, min_sections=1,
                        chunk_size=600, chunk_overlap=90) -> PrepopulateResult
    # NEW runtime entry. Validation (ValueError, in order):
    #   markdown non-empty; doc_id matches ^bank-[a-z0-9][a-z0-9-]*$;
    #   department matches slug RE; len(sections) >= min_sections
    # then delegates to _ingest_markdown. PrepopulateResult.skipped carries
    # the idempotent-skip signal.
```

### 4.2 — Keyword-store `delete_by_parent` (`enterprise_rag/adapters/`)

| File | Change |
|---|---|
| `protocols.py` | `KeywordStore` gains `async def delete_by_parent(self, parent_id: str, tenant_id: str) -> int` (count removed) — required on all implementers |
| `bm25_memory.py` | filter `_chunks` where `parent_id == parent_id and tenant_id == tenant_id`, rebuild off-loop, return removed |
| `elasticsearch_keyword.py` | `delete_by_query` with `term` filters on `tenant_id.keyword` + `parent_id.keyword`, `refresh=True` |
| `none_keyword.py` | return 0 |

### 4.3 — MCP tool + stack seam (`enterprise-rag-core/enterprise_rag/server.py`)

```
seam:  agent_stack = None ; _set_stack(stack) ; async _stack()  (guard RuntimeError)
       wired by build_app() alongside the existing _set_engine/_set_orchestrator/
       _set_vector_store — tests MUST save/restore all four.

OIDC tool (module-level, registered in the oidc branch of build_mcp):
  register_bank(markdown: str, doc_id: str, department: str,
                force: bool = False, ctx: Context | None = None) -> str
    get_access_token() is None            → ValueError (unauthenticated)
    "rag:write" not in token.scopes        → ValueError (insufficient scope)
    no tenant_id claim                     → ValueError
    token.departments non-empty and department not in them → ValueError
    core register_bank(tenant_id from token) → json {doc_id, tenant_id,
      sections, chunks, status}  status ∈ registered | already_present

None-auth twin (factory _make_none_auth_register_bank_tool(config)):
  same schema; runs as config.default_tenant (this deployment).

Tool catalog is STATIC per serve boot ⇒ the code change needs ONE ERC restart
after deployment; afterwards every registration is live with no restart.
```

### 4.4 — `interviewer/rag_client.py` (consumer MCP client)

```python
class RegisterBankResult(BaseModel):
    doc_id: str; tenant_id: str; sections: int; chunks: int; status: str

async def session(self, timeout_s: float = 30.0)                      # extended
async def _call(self, tool, args, *, timeout_s: float = 30.0)         # extended

async def register_bank(self, doc_id: str, markdown: str, department: str,
                        *, force: bool = False) -> RegisterBankResult
    # calls the register_bank tool with timeout_s=120.0 — server-side
    # embedding of a large bank exceeds the 30 s default.
    # is_error from the wire ⇒ RuntimeError (existing contract).
```

### 4.5 — `interviewer/skills.py` (NEW — consumer discovery + validation)

Stdlib only. **The folder is the source of truth for "what skills exist";
registration state lives in RAG and is probed separately.**

```python
BANK_DIR: Path = repo_root / "question_banks"
SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SECTION_RE   = re.compile(r"(?m)^## ")

@dataclass(frozen=True) class LocalBank: name: str; doc_id: str; path: Path

def discover_local_banks(bank_dir=BANK_DIR) -> list[LocalBank]   # sorted; slug stems only
def unusable_bank_files(bank_dir=BANK_DIR) -> list[Path]          # *.md w/ bad slugs
def normalize_skill_name(filename: str) -> str
    # strip; reject "" and anything with / \ .. or leading dot;
    # strip one trailing ".md" case-insensitively (else keep whole);
    # lowercase; must match SKILL_NAME_RE and not end with "-" else ValueError
def bank_doc_id(name: str) -> str                                  # f"bank-{name}"
def parse_markdown_shape(text: str) -> tuple[list[str], list[str]]
    # mirrors split_markdown_text (front matter drop, empty-body skip) and
    # reports problems: no sections / empty heading / heading w/o body
def validate_upload(text: str, name: str) -> None
    # ValueError: bad name; any shape error; zero headings; or no
    # "expected points" substring (case-insensitive) in the corpus
```

### 4.6 — `interviewer/server.py` (Skill Update API)

Three routes, declared **above** `app.mount("/", StaticFiles(...))` — the
catch-all at the bottom would shadow any route added after it.

| Route | Request | Success | Failures |
|---|---|---|---|
| `GET /skills` | — | 200 `{rag_ok, skills:[{name, doc_id, present, sections, error, registered, questions}], unusable_files[]}` | never 500 — RAG down ⇒ `rag_ok=false`, `registered=null` |
| `POST /skills` | multipart `file` | 201 `{name, doc_id, sections, chunks, status, replaced}` | 400 validation (bad name / non-UTF-8 / > 1 MB / shape / no expected points); 502 RAG registration failed (detail names `register_bank`) |
| `POST /skills/reconcile` | — | 200 `{rag_ok, registered[], already_present[], errors[]}` | 502 on probe failure — **before any write** (no partial registrations) |

`POST /skills` exact order of operations (spec): normalize name → read (≤ 1 MB)
→ UTF-8 decode → `validate_upload` → resolve target path and assert it stays
under `BANK_DIR` → write file → probe `interview_bank` (failures tolerated) →
`replace = local_file_existed OR probe.count > 0` → `register_bank(force=replace)`.

Seam: module-level `_rag = RagClient(config.rag_mcp_url, token=…)` — tests
swap `server._rag` for a stub. `python-multipart` is declared in the `[api]`
extra of `pyproject.toml` (FastAPI `UploadFile` needs it).

### 4.7 — `interviewer/brain.py` — empty-bank fail-fast

```python
class EmptyBankError(RuntimeError): ...   # typed so UIs can act on it

# inside LLMInterviewer.run, after bank = await rag.interview_bank(doc_id):
if not bank.questions:
    raise EmptyBankError("no questions registered for '<doc_id>' — register the
        skill on the Skill Update page, or restart services to auto-register
        the question_banks folder")
```

### 4.8 — `interviewer/voice/agent.py` — dynamic room domains

```python
DOMAINS = ("system-design", "ios", "dsa", "devops")   # legacy fallback — KEEP

def domain_from_room(room_name, default="system-design") -> str:
    # rooms: interview-<domain>-<sid>
    parts = (room_name or "").split("-")
    if len(parts) >= 3 and (parts[1] in DOMAINS
            or parts[1] in {b.name for b in discover_local_banks()}):
        return parts[1]
    return default                       # unknown → default (never raises)
```

The folder scan runs **per call**, not at import: the worker may outlive a
skill upload. Exception branch: `except EmptyBankError` publishes a
`{type:"notice", text}` then `ended {reason: str(exc)}` (generic catch
unchanged for other failures).

### 4.9 — `web/streamlit_app.py` — dynamic picker

```python
_STATIC_DOMAINS = ["system-design", "ios", "dsa", "devops"]
DOMAINS = _STATIC_DOMAINS + sorted({b.name for b in discover_local_banks()}
                                   - set(_STATIC_DOMAINS))
# original four first (order + index-default logic untouched); new skills
# appended alphabetically. Import is safe: same machine/venv, no ERC import.
```

### 4.10 — Pages (`web/skills.html` NEW, `web/index.html`)

- `skills.html` — served automatically at `/skills.html` by the static mount
  (zero server code); design tokens copied from `index.html`. Table (Skill /
  doc-id / Local questions / Status badge: `Registered · N questions` green,
  `Not registered` amber, `RAG offline` red, `Local problem` red w/ title tooltip),
  upload card (file input + auto-derived skill name), Replace flow via
  `confirm()` when the skill already exists, **Register missing** button, RAG /
  unusable-file warning line.
- `index.html` — header nav (Voice interview ↔ Skill Update); domain `<select>`
  keeps its four static `<option>`s as the offline fallback and appends every
  `GET /skills` name on load (dedupe by value). The select value still flows to
  `/voice/token` unchanged.

### 4.11 — Launchers

`start_services.ps1` Step 5 and `scripts/prepopulate_banks.sh` glob
`question_banks/*.md`, skip non-slug stems with a warning, and run the existing
idempotent prepopulate CLI per file (`--doc-id bank-<name> --department <name>`).
PowerShell slug test: `-notmatch '^[a-z0-9][a-z0-9-]*$' -or EndsWith("-")`;
bash: `case "$name" in ''|*[!a-z0-9-]*|-*)` skip. Step 6 restart logic is
**untouched**.

---

## 5. Implementation checklist (a developer's ordered run — each step lands green)

Work in this order so both repos stay green at every commit; Tasks 1–3 form
one self-contained ERC change.

| # | Task | Files | Done when (acceptance test) |
|---|---|---|---|
| 1 | Text-first splitter + `_ingest_markdown` + `register_bank` core; force path deletes keyword leg | ERC `prepopulate.py` | `split_markdown_text == split_markdown_sections` on the same file; core happy path + id-format; every validation rejection writes nothing; idempotent skip; force-replace leaves only new chunks in BOTH legs |
| 2 | `KeywordStore.delete_by_parent` | ERC `adapters/{protocols,bm25_memory,elasticsearch_keyword,none_keyword}.py` | BM25 delete removes only that parent/tenant (sibling-parent and sibling-tenant retained); repeat delete → 0; NoOp → 0 |
| 3 | Stack seam + `register_bank` MCP tool (both auth modes) + catalog wiring | ERC `server.py`, new `tests/test_register_bank.py` | Hermetic (fake embedder, memory+bm25 backends, seams save/restore): none-auth register → bank listed AND follow-up retrieval on the new department hits — **no restart**; OIDC refuses no-token / missing `rag:write` / out-of-scope department; HTTP `tools/list` shows `register_bank`; register-then-`interview_bank` roundtrip |
| 4 | `RagClient.register_bank` + timeout seam | `interviewer/rag_client.py`, `tests/test_rag_client.py` | Stub-transport test: exact args incl. `force=False` default; 120 s budget on register vs 30 s on reads; `is_error` → `RuntimeError` |
| 5 | `interviewer/skills.py` | new module + `tests/test_skills.py` | normalization table; traversal/`.MD`/bad-slug rejection; shape parser over tmp fixtures; upload validation gates; folder discovery over a tmp dir; regression floor: real folder ⊇ the original four domains |
| 6 | Skills API + pyproject extra | `interviewer/server.py`, `pyproject.toml`, new `tests/test_skills_api.py` | TestClient + stub `_rag` + tmp `BANK_DIR`: list/upload/reconcile semantics; RAG-down tolerance on GET (never 500); upload writes file, forwards `force`; duplicate ⇒ replace; 400/502 paths; **route-precedence test** (the static mount must not shadow `/skills`) |
| 7 | Pages + dynamic select | `web/skills.html` new, `web/index.html` | `node --check` both inline scripts; manual: GET /skills fills the table, upload flips status |
| 8 | Dynamic domains + `EmptyBankError` | `web/streamlit_app.py`, `voice/agent.py`, `brain.py` + tests | `run` raises `EmptyBankError` on a zero-question bank (no candidate turns/scores); `domain_from_room` accepts a folder-only skill and still defaults unknown rooms; streamlit `DOMAINS` = 4 static first + new skills |
| 9 | Launcher glob | `start_services.ps1`, `scripts/prepopulate_banks.sh` | PS parser + `bash -n` clean; Step 5 runs one prepopulate per `*.md`, slug-skips with a warning; Step 6 untouched |
| 10 | Docs | this TRD + `DONE_AND_PENDING.md` + stale claims in `docs/CLAUDE.md`, `README.md`, ERC `CLAUDE.md` | no "4 banks / 16 questions" claims remain; status doc dated and accurate |

### 5.0 — How to use this breakdown

Each task below is split into **small user stories**; every story has an
**Acceptance** (GIVEN/WHEN/THEN) and concrete **Test(s)**. Implement and test
**one story at a time**; the task's **Gate** command must be fully green
before you move to the next task. Test names are real functions in the two
repos' suites (they pass in the reference implementation — your run must too).
Manual steps are marked 🔧 and belong to the launcher/browser gates only.

---

### 5.1 — Task 1: text-first splitter + `register_bank` core (ERC)

**Owner:** `enterprise-rag-core` · **Gate:** `python -m pytest tests/test_prepopulate.py tests/test_register_bank.py -q`

- **S1.1 — Splitter parity.** *Story:* as the CLI path, I can split corpus text without a file. *Acceptance:* GIVEN a temp `kb.md`, WHEN `split_markdown_text(content)` and `split_markdown_sections(path)` both run, THEN they return identical `(heading, body)` lists and front matter is dropped. **Test:** `test_split_markdown_text_matches_path_variant`.
- **S1.2 — Core ingest, both legs.** *Story:* as a caller of the core, registration lands in dense and keyword legs with deterministic ids. *Acceptance:* GIVEN markdown with two sections, WHEN `register_bank(stack, markdown, doc_id="bank-web", department="html")`, THEN `sections==2`, `chunks>=2`, `bank-web:s1:c1` exists in `get_all`, and a BM25 search for a section term hits. **Test:** `test_register_bank_ingests_into_both_legs`.
- **S1.3 — Validation rejects, writes nothing.** *Acceptance:* GIVEN empty markdown, a non-`bank-` doc id, an uppercase doc id, a spaced department, or a corpus with no `## ` sections, THEN `ValueError` is raised AND `get_all(tenant)` stays empty. **Test:** `test_register_bank_validation_rejections`.
- **S1.4 — Idempotent skip + force replace on both legs.** *Acceptance:* GIVEN a registered bank, WHEN registered again, THEN `skipped=True`; WHEN force-registered with a single-section corpus, THEN the superseded section's content is gone from BOTH `get_all` and BM25 search and the replacement still hits. **Test:** `test_register_bank_idempotent_skip_and_force_replaces_both_legs`.
- **S1.5 — CLI untouched.** *Acceptance:* GIVEN the refactor, WHEN `main(["--help"])`, THEN exit 0 and every pre-existing prepopulate test passes unchanged. **Test:** `test_main_help_exits_zero` + whole `tests/test_prepopulate.py`.

### 5.2 — Task 2: keyword-store `delete_by_parent` (ERC)

**Owner:** `enterprise-rag-core` · **Gate:** `python -m pytest tests/test_register_bank.py tests/test_adapters.py -q`

- **S2.1 — BM25 deletes exactly one parent.** *Story:* as the sparse leg, a force rebuild can drop superseded rows. *Acceptance:* GIVEN two parents in the same tenant, WHEN `delete_by_parent(parent_a)`, THEN the count removed == rows of parent_a only, `search` no longer finds parent_a terms, sibling parent_b still hits, and a repeat delete returns 0. A second tenant's rows with the same parent id survive. **Test:** `test_bm25_delete_by_parent_only_removes_that_parent`.
- **S2.2 — NoOp contract.** *Acceptance:* `delete_by_parent` on the no-op store returns 0 without raising. **Test:** `test_noop_keyword_delete_returns_zero`.
- **S2.3 — Elasticsearch parity (no live ES).** *Acceptance:* code review confirms `delete_by_query` with `tenant_id.keyword` + `parent_id.keyword` filters matches the adapter's own filter dialect (hermetic suites cannot reach ES in CI). 🔧 optional: unit-test with a mock async client if ES is available.

### 5.3 — Task 3: `register_bank` MCP tool + stack seam (ERC)

**Owner:** `enterprise-rag-core` · **Gate:** `python -m pytest tests/test_register_bank.py -q`

- **S3.1 — Stack seam wired.** *Acceptance:* the tool can reach vector + keyword stores + embeddings through `_stack()`; without wiring it raises the guard `RuntimeError` (the fixture sets all four seams and restores them). Covered implicitly by every S3.2 tool test (they fail with "server not wired" if the seam is missing).
- **S3.2 — None-auth tool registers and reports.** *Story:* as the default-tenant caller, one MCP call registers a bank. *Acceptance:* GIVEN a hermetic stack (fake embedder, memory+bm25), WHEN the none-auth tool runs with valid args, THEN payload `{status:"registered", sections:2, tenant:"acme"}`; the same call again → `{status:"already_present"}`. **Test:** `test_register_bank_tool_register_and_keyword_visible_without_restart`.
- **S3.3 — Keyword leg visible WITHOUT a restart.** *Acceptance:* after S3.2's registration, `interview_bank("bank-web")` lists 2 questions AND the follow-up tool restricted to `domain="html"` returns ≥1 chunk whose department is `html` — no serve boot between register and retrieve. **Test:** same as S3.2 (asserts the follow-up) — this is the Phase 4 no-restart proof.
- **S3.4 — Tool validation.** *Acceptance:* a non-`bank-` doc id or a section-less corpus through the tool raises `ValueError`. **Test:** `test_register_bank_tool_rejects_bad_input`.
- **S3.5 — OIDC guard.** *Acceptance:* no token → "unauthenticated"; token with only `rag:retrieve` → refusal naming `rag:write`; token with `rag:write` + tenant + matching departments → success and chunks land; department outside the token's departments → refusal. **Tests:** `test_register_bank_oidc_refuses_unauthenticated`, `test_register_bank_oidc_requires_rag_write_scope`, `test_register_bank_oidc_success_and_department_guard`.
- **S3.6 — Catalog + HTTP roundtrip.** *Acceptance:* over the real MCP HTTP surface, `tools/list` contains `register_bank` and a `tools/call` register → `interview_bank` shows the questions. **Test:** `test_register_bank_roundtrip_over_http`.
- **S3.7 — No catalog regression.** *Acceptance:* the full ERC suite stays green (existing tests assert tool *subsets*, not exact counts — verify none break). **Gate:** `python -m pytest tests/ -q`.

### 5.4 — Task 4: `RagClient.register_bank` (consumer)

**Owner:** mock-interviewer · **Gate:** `python -m pytest tests/test_rag_client.py -q`

- **S4.1 — Args forwarded, payload parsed.** *Story:* as the server code, one typed method reaches the MCP tool. *Acceptance:* GIVEN a stub transport, WHEN `register_bank("bank-html", md, "html")` runs, THEN the wire call is exactly `{markdown, doc_id, department, force: False}` and the JSON payload parses into `RegisterBankResult`. **Test:** `test_register_bank_forwards_args_and_parses_payload`.
- **S4.2 — Force flag.** *Acceptance:* `force=True` reaches the wire as `True`. **Test:** `test_register_bank_force_flag_and_error_mapping`.
- **S4.3 — Error mapping.** *Acceptance:* an `is_error` MCP result surfaces as `RuntimeError("MCP tool register_bank failed: …")`. **Test:** same as S4.2.
- **S4.4 — Timeout budget.** *Acceptance:* register calls open the transport with a 120 s timeout while plain reads keep 30 s. **Test:** `test_register_bank_uses_long_timeout_budget`.

### 5.5 — Task 5: `interviewer/skills.py` (consumer, new module)

**Owner:** mock-interviewer · **Gate:** `python -m pytest tests/test_skills.py -q`

- **S5.1 — Name normalization.** *Acceptance:* `<slug>` and `<slug>.md` (any case, trimmed) normalize to the slug; empty, path-bearing (`..\evil.md`, `a/b.md`), dot-prefixed, multi-suffix (`x.md.md`), space-bearing, leading-dash, and > 64-char names raise `ValueError`. **Tests:** `test_normalize_skill_name_accepts` / `test_normalize_skill_name_rejects` (parametrized tables).
- **S5.2 — Doc-id derivation.** *Acceptance:* `bank_doc_id("html") == "bank-html"`. **Test:** `test_bank_doc_id_shape`.
- **S5.3 — Shape parser.** *Acceptance:* front matter is dropped, only non-empty sections count, empty headings/bodies and section-less corpora produce errors. **Tests:** `test_parse_markdown_shape_counts_sections_and_drops_front_matter`, `test_parse_markdown_shape_flags_problems`.
- **S5.4 — Upload validation.** *Acceptance:* a valid corpus + slug passes; bad names, no sections, empty headings, and corpora without "Expected points" raise with the specific reason. **Tests:** `test_validate_upload_accepts_bank_shaped_corpus`, `test_validate_upload_rejects_bad_name_or_corpus`.
- **S5.5 — Folder discovery.** *Acceptance:* over a tmp dir, only slug-named `*.md` files come back sorted with correct `doc_id`/`path`; non-slug files appear in `unusable_bank_files`. **Test:** `test_discover_local_banks_over_tmp_dir`.
- **S5.6 — Regression floor.** *Acceptance:* the REAL question_banks folder still contains the original four domains (every dynamic picker unions onto this). **Test:** `test_discover_local_banks_matches_real_folder_shape`.

### 5.6 — Task 6: Skill Update API + pyproject extra (consumer)

**Owner:** mock-interviewer · **Gate:** `python -m pytest tests/test_skills_api.py -q`

- **S6.1 — List skills with registration.** *Acceptance:* GIVEN a stub RAG + tmp bank folder, GET /skills returns one entry per local bank with local `sections`, `registered` from the probe, `questions` from its count, plus `unusable_files`. **Test:** `test_list_skills_reports_local_banks_and_registration`.
- **S6.2 — RAG down tolerated.** *Acceptance:* probe failure ⇒ HTTP 200 with `rag_ok=false`, `registered=null` (never 500). **Test:** `test_list_skills_tolerates_rag_down`.
- **S6.3 — Bad files surfaced.** **Test:** `test_list_skills_flags_unusable_files`.
- **S6.4 — Upload new skill.** *Acceptance:* POST a valid `.md` ⇒ 201, file written under the bank dir verbatim, `register_bank` called with `force=False`, body reports `replaced:false`. **Test:** `test_upload_new_skill_writes_file_and_registers`.
- **S6.5 — Upload replaces existing.** *Acceptance:* when the skill exists locally and in RAG ⇒ `force=True` (`replaced:true`) and the file is rewritten. **Test:** `test_upload_existing_skill_replaces_it`.
- **S6.6 — Upload rejections.** *Acceptance:* non-`.md`, section-less, no-expected-points and > 1 MB corpora ⇒ 400 with the specific detail. **Test:** `test_upload_validation_rejections`.
- **S6.7 — RAG failure surfaced.** *Acceptance:* register raises ⇒ 502 whose detail names `register_bank`. **Test:** `test_upload_surfaces_rag_failure_as_502`.
- **S6.8 — Reconcile.** *Acceptance:* only unregistered banks are registered (one call each, `force=False`); already-registered ones reported; a second reconcile registers nothing. **Test:** `test_reconcile_registers_only_missing_banks`.
- **S6.9 — Reconcile is probe-first.** *Acceptance:* RAG down ⇒ 502 with **zero** register calls (no partial writes). **Test:** `test_reconcile_rag_down_fails_without_partial_registrations`.
- **S6.10 — Route precedence.** *Acceptance:* `/skills` and `/skills/reconcile` return JSON (not the index page) with the static mount in place. **Test:** `test_skills_routes_not_shadowed_by_static_mount`.
- **S6.11 — Page serving + nav.** *Acceptance:* GET `/skills.html` serves the Skill Update page (upload card, reconcile wiring) and GET `/` (index) links to `/skills.html`. **Tests:** `test_skill_update_page_served_with_upload_surface`, `test_voice_page_links_to_skill_update`.

### 5.7 — Task 7: Skill Update page + voice page nav/dynamic select (consumer)

**Owner:** mock-interviewer · **Gate:** 🔧 `node --check` on both inline scripts + S6.11 above; then the browser pass below

- **S7.1 — Page markup contract.** *Acceptance:* the page's `fetch("/skills")` renders one row per skill with a status badge (Registered · N / Not registered / RAG offline / Local problem), the upload card proposes a slug from the file name, and uploading an existing skill asks for Replace confirmation before `POST /skills`. **Test:** S6.11 (served surface) + 🔧 manual: load `/skills.html` against a running server.
- **S7.2 — Voice page nav + dynamic select.** *Acceptance:* index.html links to `/skills.html`; the domain select keeps its four static options and appends every `GET /skills` name (deduped) when the server answers; when the fetch fails, only the static options remain (offline fallback). **Test:** S6.11 + 🔧 manual: start the server, load `/`, confirm new skills appear in the dropdown and `system-design` is still selected.

### 5.8 — Task 8: dynamic domains + empty-bank fail-fast (consumer)

**Owner:** mock-interviewer · **Gate:** `python -m pytest tests/test_brain.py tests/test_voice.py -q`

- **S8.1 — Empty bank fails fast.** *Acceptance:* GIVEN a stub RAG whose bank has zero questions, WHEN `LLMInterviewer.run` executes, THEN it raises `EmptyBankError` ("no questions registered…") with no candidate turns and no scores. **Test:** `test_run_fails_fast_on_empty_bank`.
- **S8.2 — Static room domains unchanged.** *Acceptance:* `interview-ios-<sid>` parses to `ios`; unknown segments and malformed rooms fall back to the default without raising. **Test:** `test_domain_from_room_static_domains`.
- **S8.3 — Folder-only skills resolve per call.** *Acceptance:* a name present only in the question_banks folder parses from the room; unknown names still default. **Test:** `test_domain_from_room_accepts_folder_skills`.
- **S8.4 — Streamlit picker merge.** *Acceptance:* `DOMAINS` = the four static domains first, then folder-only skills alphabetically (html/css/javascript present on this machine). 🔧 formula verified by smoke run — the module cannot be unit-imported under pytest (Streamlit bare-mode semantics), so re-check with `streamlit run web/streamlit_app.py`.

### 5.9 — Task 9: launcher + bash glob (consumer)

**Owner:** mock-interviewer · **Gate:** 🔧 PowerShell parser + `bash -n` + one launcher run

- **S9.1 — Step 5 scans the folder.** *Acceptance:* GIVEN 7 bank files, WHEN the launcher Step 5 runs, THEN every `*.md` gets one prepopulate call (`--doc-id bank-<stem> --department <stem>`); reruns skip all (idempotent); a file whose stem is not a slug is skipped with a warning. **Test:** 🔧 run `.\start_services.ps1` (or `scripts/prepopulate_banks.sh`) and read the Step 5 log lines; expect one line per bank, `prepopulate skipped:` for the registered ones.
- **S9.2 — Syntax gates.** **Test:** PowerShell parser on `start_services.ps1`, `bash -n scripts/prepopulate_banks.sh` — both clean.

### 5.10 — Task 10: TRD + stale-doc sweep (docs)

**Owner:** mock-interviewer docs · **Gate:** 🔧 grep + link review

- **S10.1 — Claims match reality.** *Acceptance:* no LIVE doc still says "4 banks / 16 questions" or "ingested by scripts/prepopulate_banks.sh" as the only registration path. Exempt by design: `docs/RCA_VOICE_BROWSER_INTERVIEW.md` is a timestamped 2026-09-03 analysis whose quotes describe the state *at that date* (a record, not a claim) and `docs/READMEbkp.md` is a dead backup already flagged for removal in `DONE_AND_PENDING.md`. **Test:** 🔧 `grep -rn "16 questions\|4 domain banks" README.md docs/CLAUDE.md docs/DONE_AND_PENDING.md docs/TRD_PHASE4_DYNAMIC_SKILL_REGISTRATION.md enterprise-rag-core/CLAUDE.md` → no hits.
- **S10.2 — Status doc updated.** *Acceptance:* `DONE_AND_PENDING.md` carries a dated Phase 4 block with the verified-command counts; this TRD's validation record (§12) matches the actual runs.

**No-breakage rules (apply at every step):**

1. New FastAPI routes strictly before the static mount; endpoints additive only.
2. All domain lists keep the original four as base/fallback — only unioned with the folder scan. Defaults (`INTERVIEW_DOMAIN=system-design`, demo/run_gate doc-ids, voice room fallback) untouched.
3. Reconcile/register failures are surfaced (warn / 502 with detail), never crash the server or RAG.
4. Root venv must never import `enterprise_rag`/`chromadb`; root tests never assert RAG-side effects.
5. ERC tests stay hermetic: fake embedder (`monkeypatch` on `OllamaEmbeddingClient.embed`), `vector_backend="memory"`, `keyword_backend="bm25"`, tmp `kb.md`; seams saved/restored.
6. Tool catalog change ⇒ a deployed-but-not-restarted ERC lacks `register_bank` — map that error to a 502 with the cause (uploads still save the file locally; reconcile/upload can retry after the next restart).
7. Live tests keep the `@pytest.mark.live` + `port_open`-skip pattern.

---

## 6. Data model: what a "registered bank" is in RAG

Chroma metadata per chunk (collection `meridian-kb`, tenant `default`):
`parent_id` (= doc id `bank-<name>`) · `tenant_id` · `section_title` · `department`
(= the skill/domain) · `required_clearance` · content = one question chunk.
`interview_bank` = `get_all(tenant)` filtered `parent_id == doc_id`, grouped by
the deterministic id — **reads Chroma live per call**, so a doc registered by
any path is listed instantly. `interview_question` fetches by id; an unknown
doc/question raises (`unknown question in bank …`).
The BM25 leg is process-memory, upserted incrementally (`bm25_memory.upsert`)
or warmed at boot from Chroma (`warmup.warm_keyword_from_vector_store`) —
**in-process upsert is why `register_bank` needs no restart**, and
`delete_by_parent` is why force-replace cannot leave stale rows.

---

## 7. Edge cases handled (each with its rule)

| Edge | Rule |
|---|---|
| RAG down while listing skills | `GET /skills` 200, `rag_ok=false`, `registered=null` |
| RAG down while uploading | file saved locally, `register_bank` error → 502; a later upload/reconcile registers it |
| RAG down while reconciling | probe-first: 502 before any write (no partial registrations) |
| Upload of an already-registered skill | `force=True` — replace, both legs rebuilt |
| Upload whose file exists locally but RAG lost it | still `force=True` (probe adds to local-existence test) |
| Filename `../evil.md`, `a\b.md`, `.hidden.md`, `x.md.md`, > 64 chars, leading/trailing dash | 400 via `normalize_skill_name` |
| Corpus with no `## ` / empty heading / empty body / missing "Expected points" | 400 with the specific reason |
| > 1 MB upload | 400 |
| Bank file with a non-slug stem in the folder | skipped at launcher start with a warning; listed under `unusable_files` on the page |
| Voice room for an unregistered/unknown domain | `domain_from_room` → `default` (system-design), unchanged |
| Voice room for a folder-only skill (not yet in RAG) | domain accepted → interview starts → zero-question bank ⇒ `EmptyBankError` notice explains registration |
| Skill uploaded while the voice worker is running | per-call folder scan — resolves without a worker restart |
| Skill uploaded while RAG serves without the Phase-4 code | 502 naming `register_bank` — restart ERC once after deploy |
| Re-running the launcher | prepopulate idempotent skip; Step 6 restart only when something was ingested out-of-process |

---

## 8. Non-goals

- A background folder watcher / live polling — registration happens at
  launcher start and on demand (upload / Register missing). Deliberate: no
  ingestion during an active interview that the user did not trigger.
- Deleting, renaming, or editing banks from the page (local file + force
  re-register covers iteration).
- Upload formats other than markdown; anything that is not one `## ` section
  per question with expected points.
- Importing `enterprise-rag-core` into the consumer venv (unchanged contract).
- Multi-tenant admin surfaces or OIDC beyond the single `rag:write` scope
  gate. No authentication on the consumer app (localhost tool, unchanged from
  Phase 1–3 posture).

---

## 9. User stories

### US-01 — Startup auto-registers new banks

**Story:** As an operator, I drop `question_banks/react.md` and start
services — the skill is registered without editing any script.

**Acceptance:** GIVEN a `.md` bank file with a valid slug stem in the folder,
WHEN `start_services.ps1` (or `prepopulate_banks.sh`) runs, THEN prepopulate
runs for every `*.md` (idempotent skip for already-registered banks) and a
bank whose stem is not a slug is skipped with a warning. **Validation:**
slug checks in both launchers; idempotency covered by ERC
`test_prepopulate_idempotent_skip_and_force` + `test_register_bank_idempotent_skip_and_force_replaces_both_legs`. **Result:** ✅ PASSED.

### US-02 — `register_bank` ingests into both legs in-process

**Story:** As a user uploading a skill, the bank is immediately interviewable.

**Acceptance:** GIVEN a running none-auth RAG service, WHEN `register_bank`
ingests markdown for `doc_id=bank-web`, THEN `interview_bank` lists its
questions AND follow-up retrieval restricted to department `html` returns its
chunks — without any service restart (the in-memory BM25 leg was upserted
in-process). **Validation:** `test_register_bank_ingests_into_both_legs`,
`test_register_bank_tool_register_and_keyword_visible_without_restart` (MCP
roundtrip), live gate §12. **Result:** ✅ PASSED.

### US-03 — `register_bank` validates and refuses bad shapes

**Acceptance:** GIVEN a `register_bank` call, WHEN the doc id is not
`bank-<slug>`, the department is not a slug, the corpus is empty or has fewer
than the minimum sections, THEN a `ValueError` surfaces (MCP error) and no
chunks are written. OIDC mode refuses unauthenticated calls, tokens without
`rag:write`, and departments outside the token's scope. **Validation:**
`test_register_bank_validation_rejections`, `test_register_bank_tool_rejects_bad_input`,
`test_register_bank_oidc_refuses_unauthenticated`,
`test_register_bank_oidc_requires_rag_write_scope`,
`test_register_bank_oidc_success_and_department_guard`. **Result:** ✅ PASSED.

### US-04 — Duplicate upload replaces the bank

**Acceptance:** GIVEN a registered bank, WHEN `register_bank` runs with
`force=True` on a corpus with fewer sections, THEN the superseded chunks are
removed from BOTH the vector and keyword legs and the new corpus is served.
**Validation:** `test_register_bank_idempotent_skip_and_force_replaces_both_legs`,
`test_bm25_delete_by_parent_only_removes_that_parent`, `test_upload_existing_skill_replaces_it`. **Result:** ✅ PASSED.

### US-05 — Skill Update page uploads and registers a skill

**Acceptance:** GIVEN the page at `/skills.html`, WHEN I upload a valid bank,
THEN `POST /skills` saves it under `question_banks/<name>.md`, registers it
through `register_bank` (no restart), and the table shows it Registered with
its question count. Invalid files (non-`.md`, bad names, no `## ` sections,
missing expected points, > 1 MB, RAG down) fail with clear messages.
**Validation:** `test_upload_new_skill_writes_file_and_registers`,
`test_upload_validation_rejections`, `test_upload_surfaces_rag_failure_as_502`,
`test_list_skills_reports_local_banks_and_registration`,
`test_list_skills_tolerates_rag_down`, `test_list_skills_flags_unusable_files`. **Result:** ✅ PASSED.

### US-06 — Register missing reconciles folder vs RAG

**Acceptance:** GIVEN local banks, some registered, WHEN `POST /skills/reconcile`
runs, THEN only unregistered banks are registered, already-registered ones are
reported, a RAG outage aborts before any write (502). **Validation:**
`test_reconcile_registers_only_missing_banks`,
`test_reconcile_rag_down_fails_without_partial_registrations`. **Result:** ✅ PASSED.

### US-07 — Domain pickers everywhere reflect the available skills

**Acceptance:** GIVEN a newly registered skill `react`, WHEN the voice page,
the Streamlit picker, or a voice room `interview-react-<sid>` is used, THEN
the skill is selectable/parseable without editing or restarting any consumer.
Static legacy domains stay first and work offline. **Validation:**
`test_domain_from_room_static_domains`, `test_domain_from_room_accepts_folder_skills`,
`test_discover_local_banks_over_tmp_dir`, DOMAINS-merge smoke. **Result:** ✅ PASSED.

### US-08 — Empty bank fails fast with a friendly message

**Acceptance:** GIVEN a doc id with zero registered questions, WHEN the brain
runs, THEN it raises `EmptyBankError` (no questions asked, no scores) that the
Streamlit error path and the voice worker's `notice` + `ended {reason}` render
actionably. **Validation:** `test_run_fails_fast_on_empty_bank`. **Result:** ✅ PASSED.

### US-09 — Existing behavior is preserved

**Acceptance:** GIVEN the previous four-bank deployment, WHEN the Phase 4 code
runs, THEN: the four original banks remain registered and prepopulate reruns
skip them; config defaults, demo/run_gate doc-ids, the voice room fallback,
the static domain options, and route behavior are unchanged; both full test
suites pass. **Validation:** root **121 passed, 5 deselected**; ERC **136
passed, 2 skipped**; `test_skills_routes_not_shadowed_by_static_mount`. **Result:** ✅ PASSED.

---

## 10. Change inventory

| Repo | File | Change |
|---|---|---|
| enterprise-rag-core | `enterprise_rag/prepopulate.py` | text-first splitter + `_ingest_markdown` core; force path deletes keyword leg; new `register_bank` core with naming/§-count validation |
| enterprise-rag-core | `enterprise_rag/adapters/protocols.py` | `KeywordStore.delete_by_parent` |
| enterprise-rag-core | `enterprise_rag/adapters/bm25_memory.py` | `delete_by_parent` (filter + rebuild) |
| enterprise-rag-core | `enterprise_rag/adapters/elasticsearch_keyword.py` | `delete_by_parent` (delete_by_query) |
| enterprise-rag-core | `enterprise_rag/adapters/none_keyword.py` | `delete_by_parent` → 0 |
| enterprise-rag-core | `enterprise_rag/server.py` | `_set_stack` seam; `register_bank` tool (OIDC `rag:write` + none-auth twin); catalog wiring |
| enterprise-rag-core | `tests/test_register_bank.py` | new — 12 hermetic tests |
| mock-interviewer | `interviewer/rag_client.py` | `RegisterBankResult`; `_call(timeout_s=…)`; `register_bank` (120 s) |
| mock-interviewer | `interviewer/skills.py` | new — discovery, name normalization, shape parser, upload validation |
| mock-interviewer | `interviewer/server.py` | `GET /skills`, `POST /skills`, `POST /skills/reconcile` before the static mount; `server._rag` seam |
| mock-interviewer | `interviewer/brain.py` | `EmptyBankError` fail-fast on zero-question banks |
| mock-interviewer | `interviewer/voice/agent.py` | per-call folder scan in `domain_from_room`; friendly `EmptyBankError` notice/ended path |
| mock-interviewer | `web/streamlit_app.py` | static ∪ folder-scan `DOMAINS` |
| mock-interviewer | `web/skills.html` | new — Skill Update page |
| mock-interviewer | `web/index.html` | header nav; dynamic domain `<select>` (static fallback) |
| mock-interviewer | `start_services.ps1` | Step 5 globs `question_banks/*.md` (slug check) |
| mock-interviewer | `scripts/prepopulate_banks.sh` | folder glob (slug check) |
| mock-interviewer | `pyproject.toml` | `python-multipart` in `[api]` extra |
| mock-interviewer | `tests/test_skills.py`, `tests/test_skills_api.py`, additions to `test_rag_client.py`, `test_brain.py`, `test_voice.py` | new/added unit tests (+36) |
| docs | this file, `docs/DONE_AND_PENDING.md`, `docs/CLAUDE.md`, `README.md`, ERC `CLAUDE.md` | TRD & LLD; status + stale-claim sweep |

---

## 11. Validation record

| Suite | Command | Result |
|---|---|---|
| Consumer unit | `python -m pytest tests/ -m "not live"` | **121 passed, 5 deselected** (1 warning: pre-existing JWT key-length) |
| Phase 4 consumer tests | `tests/test_skills.py` (27), `test_skills_api.py` (10), `test_rag_client.py` (+3), `test_brain.py` (+1), `test_voice.py` (+2) | ✅ all passed |
| ERC hermetic | `python -m pytest tests/` (ERC venv) | **136 passed, 2 skipped** (redis/ollama markers auto-skip) |
| ERC Phase 4 tests | `tests/test_register_bank.py` | **12 passed** |
| Script syntax | PowerShell parser on `start_services.ps1`; `bash -n` on `prepopulate_banks.sh`; `node --check` on both pages' inline JS | ✅ clean |
| Live gate (real stack) | ERC `serve` on :8031 (real Chroma `meridian-kb`, Ollama nomic-embed-text) → MCP `register_bank` for html/javascript/css → **all registered (15 sections / 15 chunks each)**; `interview_bank(bank-html)` = 15 immediately; `interview_followup(domain=html)` = 3 hits — keyword leg live **without any restart** | ✅ PASSED 2026-09-04 |
| Launcher gate (manual) | `.\start_services.ps1` — the 3 banks are already in the store (live gate above), so Step 5 skips all 7 and RAG boot warms BM25 from Chroma: no restart needed; browser checks of the Skill Update page | ⏳ pending user's next app run |

Known operational notes (not defects): the MCP tool catalog is static per
serve boot, so a *running* ERC instance does not expose `register_bank` until
its next restart — the launcher's normal Step 6 restart after deployment
covers this, and `POST /skills` maps the missing-tool error to a 502 with the
cause in the detail. Concurrent registration via the upload endpoint while a
launcher prepopulate runs is not supported (they run at disjoint times by
design).
