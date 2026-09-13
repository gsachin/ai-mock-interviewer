## US-009 — BankStore protocol and local backend

> **Epic:** Phase 2 — Pluggable bank storage · **Primary module:** MOD-08 · **Depends on:** US-003, US-006 · **Blocks:** US-010

**Story statement:** As a skill author, I want question-bank storage to sit behind one protocol with a local implementation as the default so that moving banks to object storage later is a configuration change rather than a rewrite.

**Business value.** `POST /skills` writes to the pod's local disk (`server.py:237`) while every request re-globs that folder, so with two replicas an uploaded bank is invisible to the other — and the cluster has no shared disk to put the folder on without an RWX PVC, which is the least portable thing in Kubernetes and is exactly why the object-store decision was taken (`DG-01`). This story introduces the seam that makes the decision implementable and does it as a **behavior-preserving refactor**: with `LocalBankStore` as the default the folder is still the store, every existing test and the Windows flow behave identically, and the authority simply has a name now. US-010 adds an S3 backend and a cache behind the same seam without touching a single call site.

## Acceptance Criteria

### Scenario 1 — The protocol exists and mirrors the session store's style
**Given** `interviewer/bank_store.py`
**When** it is imported
**Then** it defines a `BankStore` protocol with `list()`, `get(name)`, `put(name, markdown)`, `delete(name)` — all `async`, all with the same shape as `SessionStore`'s `save`/`load`/`delete`
**And** `LocalBankStore(root)` implements it against a directory
**And** `bank_store.py` imports nothing beyond the standard library and `interviewer.skills`

### Scenario 2 — The refactor is behavior-preserving
**Given** the nine banks in `question_banks/`
**When** `discover_local_banks()` runs after this story
**Then** it returns the same nine `LocalBank` entries, sorted, with the same `name`/`doc_id`/`path` values as before
**And** `GET /skills` returns byte-identical JSON for the same folder
**And** the full suite passes with no change to `tests/test_skills.py` or `tests/test_voice.py`

### Scenario 3 — `bank_dir()` is the single authority
**Given** `INTERVIEW_BANK_DIR` is unset
**When** `skills.bank_dir()` is called
**Then** it returns the repo-relative `question_banks/`, exactly as `BANK_DIR` does today
**Given** `INTERVIEW_BANK_DIR=/var/cache/banks`
**Then** it returns that path, resolved to absolute
**And** a module-level `skills.BANK_DIR` remains as an explicit override that wins over both, so the existing `monkeypatch.setattr(skills, "BANK_DIR", tmp_path)` pattern in `tests/test_skills_api.py` keeps working unmodified

### Scenario 4 — Signatures are preserved, call sites change one token
**Given** `discover_local_banks(bank_dir=None)`, `unusable_bank_files(bank_dir=None)`, `parse_markdown_shape(text)`, `validate_upload(text, name)`
**When** a caller passes an explicit directory
**Then** it is still honored (`tests/test_skills.py` passes `tmp_path` positionally)
**And** when it is omitted the default is resolved *at call time* through `bank_dir()`, not frozen at import
**And** `server.py`'s three call sites change from `skills.BANK_DIR` to `skills.bank_dir()` — the filesystem API is kept and the authority is moved, nothing else

### Scenario 5 — Validation is enforced at the store boundary
**Given** `LocalBankStore.put`
**When** it is called with `../evil`, `Bad Name`, `x.txt`, or an empty name
**Then** `ValueError` is raised and **nothing is written anywhere**
**And** a resolved target outside the root is refused even if the name passed the slug check
**And** `put` is the last line of defence, so a future non-local backend inherits the same guard

### Scenario 6 — Writes are atomic
**Given** a reader listing and reading the directory concurrently with a `put`
**When** the write happens
**Then** the reader sees either the old file or the complete new one, never a partial write
**And** no `*.tmp` file is left behind (write to a sibling temp file, then `os.replace`)
**And** `put` of the same name twice is an idempotent replace — one file, the second content

### Scenario 7 — Empty and unusable states are explicit
**Given** an empty store directory
**When** `list()` runs
**Then** it returns `[]` and `discover_local_banks()` returns `[]` — an empty bank set is not an error
**Given** a file named `Bad Name.md` in the store
**Then** `list()` excludes it and `unusable_bank_files()` still reports it, so the Skill Update page can warn rather than silently hide it

### Scenario 8 — Upload writes through the store, ordering unchanged
**Given** `POST /skills` with a valid `.md`
**When** it is handled
**Then** validation runs first, then `await bank_store.put(name, text)`, then `register_bank` over MCP — the existing write-then-register order
**And** a RAG failure returns 502 with the file **present and unregistered** (repairable by the existing `POST /skills/reconcile`), which is the deliberate design in WF-02
**And** the traversal guard that `server.py:221` performs inline today is removed as redundant rather than kept in two places

### Scenario 9 — The no-restart promise still holds
**Given** a worker that booted when the folder held nine banks
**When** a tenth bank is written through `bank_store.put` (the equivalent of an upload)
**Then** `agent.domain_from_room("interview-newdomain-abc123")` recognizes it **without a worker restart**
**And** this test is named for the promise in `agent.py:56-66`, because it is the contract US-010 must not regress
**And** `scripts/prepopulate_banks.sh` still finds the banks by globbing the same directory

### Scenario 10 — The store is a module-level seam
**Given** the existing pattern that `tests/test_skills_api.py` patches `server._rag`
**When** a test patches `server._bank_store` with a recording fake
**Then** all three skills endpoints use it
**And** `_bank_store` is assigned at import next to `_rag` and `_store`, defaulting to `LocalBankStore(skills.bank_dir())`

## HLD

The move is one sentence long: **keep the filesystem API, move the authority.** Every consumer keeps calling `discover_local_banks()` and reading `.path`; what changes is who decides what is in the directory.

```mermaid
flowchart LR
  UP["POST /skills"] --> VAL["skills.validate_upload"] --> PUT["BankStore.put"]
  PUT --> LFS["LocalBankStore(root=bank_dir())<br/>atomic tmp + os.replace"]
  subgraph consumers["readers — signatures unchanged"]
    G1["skills.discover_local_banks()"] --> DIR[(bank_dir())]
    G2["agent.domain_from_room()"] --> DIR
    G3["scripts/prepopulate_banks.sh"] --> DIR
  end
  LFS --> DIR
  DIR --> PARSE["parse_markdown_shape(text)"]
  REC["POST /skills/reconcile"] --> G1
  DIR -.->|US-010 replaces this edge with CachedBankStore + TTL| S3[(object store)]
```

**Why the local backend is the default and the refactor is invisible.** `AS-05` and `AS-04` both require that nothing about the Windows dev flow or the 121-test floor changes. With `LocalBankStore` pointing at the same folder, `bank_dir()` returning the same path, and the same `LocalBank` objects coming out of `discover_local_banks`, there is no observable difference — only an indirection that US-010 can exploit. Deliberately splitting this from US-010 keeps a behavior-preserving refactor in its own commit, which is what makes the S3 story reviewable.

**Why the protocol is minimal.** `list`/`get`/`put`/`delete`, mirroring `SessionStore`. The consumers need names (to build the skill list and to derive a domain from a room) and markdown (to validate shape and to register). Anything richer — metadata, versions, timestamps — would be unused surface, and the plan is explicit that MOD-08 is a *library*, not a service (`05-modularization.md`).

**Why `put` owns validation.** Today `normalize_skill_name` + a resolved-parent comparison in `server.py` defend the write path. Moving the guard into the store means every backend inherits it, including the S3 backend where a traversal attempt is meaningless but a malformed name would poison the bucket's key space. This is the same defence-in-depth the `BankStore` docstring will state.

**The three-consumer constraint starts here.** `skills.discover_local_banks()`, `agent.domain_from_room`, and `scripts/prepopulate_banks.sh` each glob independently today. Under a local backend that is harmless — they all read the same directory. It stops being harmless the moment the authority is remote, which is why the converging test (Scenario 9) is written now, while it trivially passes, so that US-010 has a failing test to fix rather than a bug to discover.

## LLD

**Files changed**

| Path | Change |
|---|---|
| `interviewer/bank_store.py` (new) | `BankStore` protocol, `LocalBankStore`, `normalize_bank_name` delegate to `skills` |
| `interviewer/skills.py` | `BANK_DIR: Path | None = None` (explicit override); `bank_dir()` resolver; the three functions take `bank_dir: Path | None = None` and resolve at call time |
| `interviewer/server.py` | `_bank_store = LocalBankStore(skills.bank_dir())` beside `_rag`/`_store`; `GET /skills`, `POST /skills`, `POST /skills/reconcile` use `skills.bank_dir()`; `POST /skills` writes via `await _bank_store.put(name, text)` |
| `interviewer/config.py` | `bank_store: str = "local"` (`INTERVIEW_BANK_STORE`), `bank_dir: str | None = None` (`INTERVIEW_BANK_DIR`) |
| `tests/test_config_defaults.py` | Add both keys (US-003) |
| `tests/test_bank_store.py` (new) | Scenarios 1, 3, 5, 6, 7, 10 |
| `tests/test_skills.py`, `tests/test_skills_api.py` | Scenarios 2, 4, 8, 9 — extended, existing cases unmodified |

**Signatures**

```python
# interviewer/bank_store.py
class BankStore(Protocol):
    async def list(self) -> list[str]: ...                    # sorted valid skill names
    async def get(self, name: str) -> str | None: ...         # markdown or None
    async def put(self, name: str, markdown: str) -> None: ...
    async def delete(self, name: str) -> None: ...

class LocalBankStore:
    """Directory-backed store. The directory is the truth; every reader that
    globs it sees this store's writes immediately (no TTL)."""
    def __init__(self, root: Path) -> None: ...
    def _path(self, name: str) -> Path: ...   # normalize + traversal guard

# interviewer/skills.py
BANK_DIR: Path | None = None          # explicit override (tests / embedders)

def bank_dir() -> Path:
    """Resolution order: BANK_DIR override → INTERVIEW_BANK_DIR → the
    repo-relative question_banks/. Resolved per call, never frozen."""
```

`LocalBankStore.put` writes `root / f"{name}.md"` via a sibling `.{name}.md.tmp` then `os.replace`, so a concurrent `glob` cannot observe a half-written corpus. `delete` removes the file (missing is not an error). `list()` applies `SKILL_NAME_RE` and returns sorted stems, exactly as `discover_local_banks` does today — which is what keeps the two in agreement.

**Edge cases.** `INTERVIEW_BANK_DIR` may be relative; it is resolved against the process CWD at call time, and the manifests pass an absolute path so the resolution is unambiguous. `bank_dir()` must not create the directory — the API's readiness check (US-006) reports a missing directory rather than fixing it, and only the write path creates it (as `server.py:225` does today). `LocalBankStore` must tolerate a root that does not exist yet for `list`/`get`. A symlinked root must resolve before the traversal comparison, or a legitimate deployment with a symlinked volume is refused. `put` must fsync the temp file before `os.replace` only if the deployment cares about power loss — it does not, so the cheaper ordering is chosen and the choice is commented. The `BANK_DIR` override slot must remain a plain module attribute; converting it to a property or a settings object would break the monkeypatch pattern three existing tests rely on.

**Test scenarios**

| # | Scenario | Check | Where |
|---|---|---|---|
| 1 | Protocol round trip | put → get → list → delete on a tmp dir | `tests/test_bank_store.py` |
| 2 | Behavior preserved | nine banks, same entries as before the refactor | `tests/test_skills.py` |
| 3 | `bank_dir()` order | override → env → repo default | `tests/test_bank_store.py` |
| 4 | Monkeypatch pattern | `setattr(skills, "BANK_DIR", tmp_path)` still redirects everything | `tests/test_skills_api.py` (unmodified) |
| 5 | Traversal refused | `../evil`, `Bad Name`, `x.txt`, `""` → `ValueError`, nothing written | `tests/test_bank_store.py` |
| 6 | Atomic write | concurrent read sees old-or-new; no `.tmp` left | `tests/test_bank_store.py` |
| 7 | Idempotent replace | two `put`s → one file, newest content | `tests/test_bank_store.py` |
| 8 | Empty store | `list() == []`, `discover_local_banks() == []` | `tests/test_bank_store.py` |
| 9 | Unusable files | bad-slug file excluded from `list`, reported by `unusable_bank_files` | `tests/test_skills.py` |
| 10 | Upload path | `put` called with validated markdown; RAG failure → 502 with the file present | `tests/test_skills_api.py` |
| 11 | No-restart promise | new bank visible to `domain_from_room` without a restart | `tests/test_voice.py` |
| 12 | Store seam | `monkeypatch.setattr(server, "_bank_store", fake)` observed by all three endpoints | `tests/test_skills_api.py` |
| 13 | Defaults pinned | `bank_store`, `bank_dir` in `EXPECTED_DEFAULTS` | `tests/test_config_defaults.py` |

## Traceability

| Upstream | Reference | How this story serves it |
|---|---|---|
| Module | MOD-08 (Bank / Skill Store) | The protocol and its first implementation; the cache and S3 backend are US-010 |
| Module | MOD-01 (Management API) | Owns the skills surface that becomes the store's client |
| Requirement | BRD-04 — pluggable bank storage with identical semantics | The protocol is the "identical semantics" clause; `list`/`get`/`put`/`delete` are the whole contract |
| Use case | UC-03 — upload a new skill bank | Main flow step "persist to the bank store" gets a real store; the write-then-register ordering and the 502-with-file-retained behaviour are preserved |
| Use case | UC-04 — reconcile unregistered banks | Reads through `discover_local_banks()` unchanged, so the repair tool keeps working across backends |
| Decision | DG-01 — bank storage portability | Local FS for single-node is the option this story implements; S3 is US-010 |
| Reconciliation | REC-10 — the prepopulate script is already portable | Reused verbatim; Scenario 9 keeps it reading the same directory |
| Assumption | AS-05 / AS-04 | Behavior-preserving by construction; existing tests unmodified |

**Test files:** `tests/test_bank_store.py` (new), `tests/test_skills.py` (extended), `tests/test_skills_api.py` (extended, existing cases unmodified), `tests/test_voice.py` (extended), `tests/test_config_defaults.py` (extended).
