# STATUS — Phase 4: Dynamic Skill Registration

**Date:** 2026-09-04 · **Status:** Code **implemented + unit-verified + live-gate PASSED** (uncommitted, 33 working-tree changes); **pending:** user app-testing gates + commit decision
**Read also:** `docs/TRD_PHASE4_DYNAMIC_SKILL_REGISTRATION.md` (TRD & LLD + §5 task-by-task stories/test gates) · `docs/DONE_AND_PENDING.md` · `docs/STATUS.md` (pre-Phase-4 launch + numbers)

---

## 1. Original intent (user, verbatim) and what it maps to

> "update the system which check the question bank folder and check if the md files is present in the folder and skill is not added than add it and create all the necessary entries for it. And create one more html page called skill update where the user can upload the skill and it will be added for the interview. Make a detailed TRD… check what the updates are needed in this code base… make sure there should not be a break in the current functionality… work in plan mode and create the TRD and plan."

| # | Original intent (clause) | Implemented as | Status |
|---|---|---|---|
| 1 | System checks the `question_banks` folder | `start_services.ps1` Step 5 + `scripts/prepopulate_banks.sh` now **glob** `question_banks/*.md` (no fixed list) | ✅ DONE |
| 2 | If a `.md` is present and the skill is not added → add it | prepopulate is idempotent (skip = "already added"); new `register_bank` MCP tool ingests in-process into **both** legs (vector + BM25) | ✅ DONE |
| 3 | …and create all necessary entries | deterministic chunks `bank-<skill>:sN:cN`, department = skill, doc registered so `interview_bank`/`interview_question`/follow-up retrieval all serve it — **no RAG restart needed** | ✅ DONE |
| 4 | One more HTML page called **Skill Update** where the user uploads the skill | `web/skills.html` at `/skills.html` — upload card, status table, Replace flow, Register missing; nav link from the voice page | ✅ DONE |
| 5 | …and it will be added for the interview | upload → `POST /skills` → `register_bank` (immediate); pickers become dynamic: voice page dropdown, Streamlit, voice room parser (`folder ∪ legacy 4`) | ✅ DONE |
| 6 | Make a detailed TRD | `docs/TRD_PHASE4_DYNAMIC_SKILL_REGISTRATION.md` — TRD & LLD: 7 flow diagrams, module blueprints, interface contracts, edge cases, §5.1–5.10 = 44 user stories each with test gates | ✅ DONE |
| 7 | Check what updates are needed in this codebase | §10 change inventory: 23 files modified + 9 new across both repos | ✅ DONE (see §3) |
| 8 | No break in current functionality | both suites green; defaults/fallbacks/order preserved (see §4) | ✅ DONE (verified) |
| 9 | Work in plan mode, create the plan first | plan-mode exploration (3 Explore agents + Plan agent) → approved plan → TRD-driven implementation | ✅ DONE (historical) |

---

## 2. What the system was BEFORE vs AFTER

| Aspect | Before (Phase 1–3) | After (Phase 4) |
|---|---|---|
| Known skills | hardcoded 4 domains in 4 places (launcher, bash script, Streamlit, voice room parser) | `question_banks/*.md` folder = source of truth; static 4 kept as fallback only |
| Adding a skill | edit 4 files + prepopulate + **restart RAG** | drop `.md` in folder (registers at next start) **or** upload on `/skills.html` (registers live) |
| RAG registration surface | CLI subprocess only, BM25 visible only after restart | MCP `register_bank` tool — in-process ingest, both legs live immediately |
| Missing-skill UX | silent zero-question interview | typed `EmptyBankError` → Streamlit error / voice `notice` + `ended` with instructions |
| Question-bank docs | "4 banks / 16 questions" (stale) | TRD + status docs current (7 banks: 4 legacy ×4 + html/javascript/css ×15) |

**RAG store state (verified 2026-09-04, collection `meridian-kb`, tenant `default`):**
`bank-system-design` 4 · `bank-ios` 4 · `bank-dsa` 4 · `bank-devops` 4 · **`bank-html` 15 · `bank-javascript` 15 · `bank-css` 15** ← registered via the live gate, no restart.

---

## 3. Change inventory (what "all necessary updates" meant — 23 + 9 files)

**enterprise-rag-core (engine) — 7 files:**
`prepopulate.py` (text-first core + `register_bank`, force deletes both legs) · `server.py` (`_set_stack` seam + `register_bank` MCP tool, OIDC `rag:write` + none-auth) · `adapters/{protocols,bm25_memory,elasticsearch_keyword,none_keyword}.py` (`KeywordStore.delete_by_parent`) · `tests/test_register_bank.py` (new, 12 tests) · `CLAUDE.md`

**mock-interviewer (consumer) — 16 files + 8 new:**
`interviewer/skills.py` (new) · `server.py` (`GET/POST /skills`, `/skills/reconcile`, routes before static mount) · `rag_client.py` (`register_bank`, 120 s budget) · `brain.py` (`EmptyBankError`) · `voice/agent.py` (per-call folder scan) · `web/skills.html` (new) · `web/index.html` (nav + dynamic select) · `web/streamlit_app.py` (dynamic `DOMAINS`) · `start_services.ps1` + `scripts/prepopulate_banks.sh` (glob) · `pyproject.toml` (`python-multipart`) · `.gitattributes` (new — `*.sh eol=lf`) · tests: `test_skills.py` (27, new), `test_skills_api.py` (12, new), +`test_rag_client.py` / `test_brain.py` / `test_voice.py`
**Docs:** `TRD_PHASE4_DYNAMIC_SKILL_REGISTRATION.md` (new) · `DONE_AND_PENDING.md` · `docs/CLAUDE.md` · `README.md` · `STATUS_PHASE4.md` (this file)
**Pre-existing content:** `question_banks/{html,javascript,css}.md` (added 2026-09-04, 15 questions each — the trigger)

---

## 4. Verified (automated, all green)

| Gate | Result |
|---|---|
| Root suite (consumer) | `pytest tests/ -m "not live"` → **123 passed, 5 deselected** (+38 Phase 4 tests) |
| ERC suite (engine) | `pytest tests/` → **136 passed, 2 skipped** (+12 `register_bank` tests) |
| §5 task-by-task gate walk (44 stories, S1.1–S10.2) | ✅ every gate green — walk **caught + fixed** CRLF bug in `prepopulate_banks.sh` (now LF + `.gitattributes`) |
| Live gate (real stack) | ERC serve on :8031, real Chroma + Ollama embeddings: `register_bank` registered html/javascript/css (15/15 chunks each); `interview_bank(bank-html)` = 15 **instantly**; follow-up retrieval hit the new domain — **no restart** |
| Syntax | `node --check` (2 pages) · PowerShell parser (`start_services.ps1`) · `bash -n` (`prepopulate_banks.sh`) ✅ |

## 5. Pending — what remains from the original intent

| Item | Why it's pending | Owner |
|---|---|---|
| 🔧 **Next demo launch** — `.\start_demo.ps1` (= `start_services.ps1 -WithVoice -WithStreamlit -WithTunnel`): expect all 7 banks "skipped/already present", RAG boots and BM25 warms from the store (html/css/js already registered → no restart needed) | needs the full stack (Ollama up — it is) | user |
| 🔧 **Browser pass on `/skills.html`**: table shows 7 skills Registered · N; upload a new bank → status flips; re-upload same name → Replace; Register missing | needs the server running | user |
| 🔧 **Interview on a newly-added skill**: voice (dropdown shows it, room parses) + Streamlit (picker shows it) | needs the stack + mic | user |
| 🔧 `streamlit run web/streamlit_app.py` — picker shows all 7 | needs streamlit + RAG env | user |
| **Commit decision** — nothing of Phase 4 is committed (33 working-tree changes on top of `a224567`) | user choice: one commit vs ERC split | user |
| Optional: `pytest tests/ -m live` integration rows; removal of `docs/READMEbkp.md` (flagged in DONE_AND_PENDING) | cosmetic/optional | either |

## 6. Where to look (one-line pointers)

- Intent → implementation trace: this doc §1 (each clause ✅) — full spec: `docs/TRD_PHASE4_DYNAMIC_SKILL_REGISTRATION.md` §1–§8.
- Developer re-implementation path + per-step tests: TRD §5.1–§5.10 (44 stories with gates).
- Change inventory: TRD §10; test evidence: TRD §12.
