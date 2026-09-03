# TRD — Phase 1: Interview Question-Bank Tools & Scripted Interview Loop

**Repo:** enterprise-rag-core (this TRD) + mock-interviewer (consumer)
**Date:** 2026-08-30
**Status:** Implemented — every user story validated by automated tests (see §6)
**Validation runs:**
- Core: **121 passed, 2 warnings** (7 new interview-tool tests)
- Interviewer: **12 passed** (3 new interview tests, 2 live against the real MCP service)
- Live gate (measured, not asserted): two consecutive scripted interviews over
  MCP — state `wrap`, rubric cache hit rate **1.0** (2/2 hits each run),
  per-question orchestrator `timings_ms.total` **28–43 ms**
**Parent:** Live Voice Interviewer feasibility study (§5 roadmap, Phase 1)

## 1. Context and objectives

Phase 0 made the engine realtime-ready. Phase 1 builds the **retrieval-backed
interviewer brain minus voice and LLM**: question banks live in the core's
vector stores, the core exposes interview tools over MCP, and the consumer
(mock-interviewer repo) runs a scripted interview loop against them. The core
remains retrieval-only and standalone — no LLM, no audio, no imports of the
consumer.

Phase 1 goals:

1. Deterministic question-bank structure over the existing `prepopulate`
   format (one `## ` section = one question; ids `{doc_id}:s{section}:c{chunk}`).
2. Three interview MCP tools in both auth modes: `interview_bank`
   (catalog), `interview_question` (exact fetch, no search),
   `interview_followup` (domain-scoped hybrid retrieval).
3. A consumer-side scripted interview loop that drives the FSM end-to-end
   over MCP and measures the Phase 1 gate metrics (cache hit rate on rubric
   queries, per-domain isolation, per-turn timings).

## 2. Design

### 2.1 Question-bank structure (`enterprise_rag/interview.py`)

Pure, backend-free helpers: `parse_chunk_position` (id → (section, chunk)),
`group_questions` (chunks → ordered questions), `question_refs` (content-free
catalog rows). Deterministic ids make the bank stable across rebuilds and
fetchable without any search.

### 2.2 MCP tools (`enterprise_rag/server.py`)

- `interview_bank(doc_id)` — lists `{question_id, section_title, chunk_count}`
  rows. Tenant from the token (OIDC) or the configured default (none).
- `interview_question(doc_id, question_id)` — the full question (title +
  chunks in order); unknown id raises `ValueError`.
- `interview_followup(query, domain="", top_k=3)` — hybrid retrieval with the
  department filter set to `domain`. **Narrowing semantics in OIDC mode**: a
  requested domain outside the token's departments is refused; empty domain
  keeps token scope. In none mode the domain is taken as-is (no token scope
  to conflict with).
- New module-level seam `agent_vector_store` (wired by `build_app()` /
  `serve-stdio`, replaced in tests) — the bank tools read the store via
  `get_all` + grouping.

### 2.3 Consumer loop (`mock-interviewer/interviewer/interview.py`)

`ScriptedInterview` drives the Phase 0 FSM: greet → per question: fetch
(interview_question) → candidate answer (script) → evaluation support
(cache-gated `execute_agent_context` rubric + domain `interview_followup`) →
score ledger → next → wrap. Per-question ledger records `rubric_hit_source`,
chunk counts, and the core's `timings_ms`; `InterviewStats` accumulates the
rubric cache hit rate — the Phase 1 gate metric.

Question banks (real content, 4 questions each): `mock-interviewer/
question_banks/{system-design,ios,dsa,devops}.md`, ingested by
`scripts/prepopulate_banks.sh` (idempotent, department = domain).

## 3. Non-goals

LLM turns, follow-up dialogue depth, scoring quality, audio (Phases 2–3).
The core's `execute_agent_context` still carries `conversation_history=[]` —
dialogue state lives in the consumer's `Session`.

## 4. User stories

### US-01 — Deterministic bank structure

**Story:** As a tool author, I want prepopulated question banks grouped into
ordered questions by deterministic ids, so question identity is stable across
rebuilds and fetchable without search.

**Acceptance:** GIVEN chunks with ids `{doc}:s{i}:c{j}` (any input order),
WHEN `group_questions(chunks, doc)` runs, THEN questions are ordered by
section, chunks within a section by index, foreign doc ids are excluded, and
`question_refs` returns content-free rows.

**Validation:** `tests/test_interview_tools.py::test_parse_chunk_position`,
`test_group_questions_orders_by_section_and_chunk`. **Result:** ✅ PASSED

### US-02 — interview_bank catalogs questions

**Story:** As the interviewer, I want the question catalog of a bank so I can
choose the next question.

**Acceptance:** GIVEN a prepopulated bank, WHEN `interview_bank(doc_id)` is
called (none-auth, wired seams), THEN it returns the tenant, the count, and
one row per question with title and chunk count.

**Validation:** `test_interview_bank_lists_questions` + HTTP roundtrip in
`test_catalog_and_bank_roundtrip_over_http`. **Result:** ✅ PASSED

### US-03 — interview_question fetches exactly

**Story:** As the interviewer, I want one full question by id — an exact
fetch, never a search.

**Acceptance:** GIVEN a prepopulated bank, WHEN `interview_question(doc_id,
"s1")` is called, THEN the returned chunks belong to s1 and contain the
question body; an unknown id raises `ValueError`.

**Validation:** `test_interview_question_fetches_full_question`. **Result:** ✅ PASSED

### US-04 — interview_followup is domain-scoped

**Story:** As the interviewer, I want follow-up retrieval filtered to the
interview's domain department, and I want the OIDC path to never widen token
scope.

**Acceptance:** GIVEN banks for system-design and ios, WHEN follow-up runs
with domain system-design, THEN all returned chunks have
`department == "system-design"`; with domain ios the system-design query
returns zero chunks (isolation). GIVEN an OIDC token without the requested
domain, THEN the request is refused (narrowing).

**Validation:** `test_interview_followup_filters_by_domain` (hermetic, both
directions) + live `test_scripted_interview_live_against_core` (real chunks
all `department == "system-design"`). **Result:** ✅ PASSED

### US-05 — Tools are on the MCP wire in both auth modes

**Story:** As an MCP client, I want the three interview tools registered in
the catalog and callable over the streamable-HTTP session protocol.

**Acceptance:** GIVEN a booted none-auth server, WHEN `tools/list` runs, THEN
the catalog contains `interview_bank`, `interview_question`,
`interview_followup`; a `tools/call` of `interview_bank` returns the bank
payload.

**Validation:** `test_catalog_and_bank_roundtrip_over_http`. **Result:** ✅ PASSED

### US-06 — Seams stay testable and non-leaking

**Story:** As a maintainer, I want the new vector-store seam wired by
`build_app` and `serve-stdio` without breaking the other seam-based tests
(the fixture restores previous seam values).

**Acceptance:** GIVEN the full core suite, THEN all 121 tests pass (the
interview-tool fixture saves/restores the module seams — an earlier version
leaked the real orchestrator into `test_mcp_boot`; caught and fixed).

**Validation:** full-suite run **121 passed**. **Result:** ✅ PASSED

### US-07 — Scripted interview loop runs the full FSM

**Story:** As a Phase 1 demo operator, I want a text-mode interview over MCP
that walks greeting → questions → evaluation → scoring → wrap with a
per-question ledger and cache-hit statistics.

**Acceptance:** GIVEN a stubbed RAG client, WHEN a 2-question interview runs,
THEN the session ends in `wrap`, turns alternate interviewer/candidate, the
ledger records both questions with rubric hit sources, follow-up calls carry
the session domain, and stats report the cache hit rate (0.5 with the stub's
alternating hit source).

**Validation:** `mock-interviewer/tests/test_interview.py::test_scripted_interview_runs_full_loop`
+ `test_scripted_interview_caps_question_count`. **Result:** ✅ PASSED

### US-08 — Live gate: scripted interviews over the real MCP

**Story:** As the Phase 1 gatekeeper, I want two consecutive scripted
interviews against the real deployed service to prove the whole chain and to
measure the rubric cache hit rate.

**Acceptance:** GIVEN the core service with 4 prepopulated banks and a memory
semantic cache, WHEN two consecutive 2-question interviews run over MCP, THEN
both end in `wrap`, the second run's rubric queries hit the semantic cache
(≥ 50% hit rate), follow-up chunks stay inside the interview domain, and
per-question timings are reported.

**Validation:** `test_scripted_interview_live_against_core` (**PASSED**);
measured run: state `wrap`, cache hit rate **1.0**, timings **28–43 ms**
per question. **Result:** ✅ PASSED

### US-09 — Standalone boundaries preserved

**Story:** As an architect, I want the two repos to stay decoupled: the core
exposes tools but imports nothing from the consumer; the consumer talks MCP
only; question banks live with the consumer.

**Acceptance:** GIVEN both repos, THEN `import enterprise_rag` loads no
consumer code, `mock-interviewer` has no `enterprise-rag-core` dependency in
`pyproject.toml`, and bank ingestion happens through the core CLI
(`scripts/prepopulate_banks.sh`).

**Validation:** dependency/diff inspection; both suites green.
**Result:** ✅ PASSED

## 5. Change inventory

| Repo | File | Change |
|---|---|---|
| core | `enterprise_rag/interview.py` | new: pure bank helpers |
| core | `enterprise_rag/server.py` | vector-store seam; 3 interview tools × 2 auth modes |
| core | `enterprise_rag/cli.py` | `serve-stdio` wires the vector-store seam |
| core | `tests/test_interview_tools.py` | new: 7 tests (helpers, tools, catalog) |
| interviewer | `interviewer/rag_client.py` | 3 new tool methods + result models |
| interviewer | `interviewer/interview.py` | new: `ScriptedInterview` + `InterviewStats` |
| interviewer | `question_banks/*.md` | new: 4 domain banks (16 questions) |
| interviewer | `scripts/prepopulate_banks.sh` | new: idempotent bank ingestion |
| interviewer | `tests/test_interview.py` | new: 3 tests (2 unit, 1 live) |

## 6. Validation record

| Suite | Command | Result |
|---|---|---|
| Core | `python -m pytest tests/ -q` (Redis Stack live) | **121 passed, 2 warnings** |
| Interviewer unit | `python -m pytest tests/ -q` | 10 passed, 2 skipped (no live URL) |
| Interviewer live | `RAG_MCP_URL=http://127.0.0.1:8031/mcp python -m pytest tests/ -q` | **12 passed** |
| Live gate metrics | two consecutive scripted interviews | hit rate 1.0, timings 28–43 ms |
