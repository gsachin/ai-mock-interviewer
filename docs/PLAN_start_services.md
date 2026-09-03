# PLAN — One-shot launcher `start_services.ps1` for ai-mock-interviewer

**Date:** 2026-08-31 · **Author:** Claude Code (AI expert)
**Status:** ✅ P1/P2/P4/P5 PASSED (2026-08-31) — full stack booted clean; RAG link verified; live interview works. Remaining: P3 (live gate), P6 (idempotency rerun), P7–P10 (§9).

---

## 1. Objective

Create **one PowerShell file** — `d:\project\ai-mock-interviewer\start_services.ps1` —
that launches the whole **ai-mock-interviewer** stack (backend, RAG, LLM wiring, optional web)
with quality **equal to or better than** the reference `d:\project\universityDemo\start_services.ps1`
(same structure: comment-based help, colored `Write-Step/OK/Warn/Err`, tool discovery,
dependency self-heal, kill-stale, port verify, readiness probes, summary, process-alive guard).

The RAG service is the `enterprise-rag-core` folder **inside** this project
(branch `enterprise-rag-core-realtime-ready`). The launcher must wire it correctly —
and fix the **missing links** found below.

---

## 2. Current state (verified 2026-08-31)

| Component | State |
|---|---|
| `ai-mock-interviewer` venv | ❌ none — fresh clone (no `.venv`) |
| `enterprise-rag-core` venv | ❌ none (launcher creates it, ~200 MB first install) |
| Ollama `:11434` | ✅ running — `qwen2.5:14b` (LLM-capable) + `nomic-embed-text` (embeddings) present |
| MLX LLM `:1234` (README's "deployed" server) | ❌ not running |
| Docker / Redis Stack `:6379` (semantic cache) | ✅ now running — the ERC launcher started Docker Desktop + `enterprise-rag-core-redis-stack-1`; cache backend = `redisvl` |
| Ports `8010`, `8031`, `8000` | ✅ free |
| LiveKit server `:7880` (Phase 3 voice) | ❌ not deployed — web voice room is inert without it |

---

## 3. Target topology & port scheme

```
┌─────────────────────── ai-mock-interviewer (this repo) ───────────────────────┐
│                                                                               │
│  start_services.ps1  (IMPLEMENTED)                                         │
│    ├─ venv self-heal  .venv  (pip install -e ".[api,dev,web]")             │
│    ├─ env wiring: RAG_MCP_URL, INTERVIEW_LLM_*, INTERVIEW_DOMAIN, ...      │
│    ├─ uvicorn interviewer.server:app ──────────────── 127.0.0.1:8010 (mgmt)│
│    ├─ (optional) Streamlit UI web/streamlit_app.py ─── 127.0.0.1:8501      │
│    └─ (optional) static web/ on :8080  (python -m http.server)             │
└───────────────┬───────────────────────────────────────────────────────────────┘
                │  MCP (streamable HTTP, none-auth)
                ▼  RAG_MCP_URL=http://127.0.0.1:8031/mcp
┌─────────────────────── enterprise-rag-core/ (inner folder) ──────────────────┐
│  start_services.ps1 (existing, invoked with -Port 8031 -SkipPrepopulate)     │
│    ├─ self-heals ITS .venv + installs (requirements-dev.txt, -e .)           │
│    ├─ .env load + RAG_CORE_* defaults (chroma, bm25, auto embeddings)        │
│    ├─ Redis Stack attempt (Docker) — warn-only if unavailable                 │
│    ├─ reranker model download (22 MiB, HF)                                    │
│    └─ MCP server  ────────────────────────────  127.0.0.1:8031/mcp           │
│                                                                               │
│  prepopulate × 4 banks (run by the NEW launcher, see §5 step 5)               │
└───────────────────────────────────────────────────────────────────────────────┘
      ▲ embeddings: Ollama nomic-embed-text :11434 (already running)
      ▲ LLM:        pinned Ollama /v1 (qwen2.5:14b) — user decision §7 #3
```

**Port scheme (conflict-free, matches the repo's own test/demo convention):**

| Service | Port | Why |
|---|---|---|
| Interviewer management plane (uvicorn) | **8010** | README's documented run command |
| RAG MCP service (ERC) | **8031** | matches `tests/test_live_phase2.py`, `demo.py`, `run_gate.py` defaults — NOT the ERC default 8010 (would collide with the interviewer) |
| Ollama | 11434 | already running (embeddings + optional LLM) |
| Web static (optional) | 8080 | `python -m http.server` |

---

## 4. 🔴 Missing links found for RAG (the gaps the new launcher must fix)

1. **Port conflict / default mismatch**
   - ERC's own launcher defaults to `-Port 8010`; the interviewer's management plane also runs on 8010 → **collision**.
   - `interviewer/config.py` defaults `RAG_MCP_URL` to `:8000/mcp` (stale — matches no deployment; even ERC's own `serve` default of 8000 is unused here).
   - **Fix:** launcher invokes ERC with `-Port 8031` and exports `RAG_MCP_URL=http://127.0.0.1:8031/mcp` (the convention the repo's live tests/demo already use).

2. **Question-bank ingestion is bash-only** (`scripts/prepopulate_banks.sh`)
   - ERC's `-KbPath` prepopulate is hard-wired to doc-id `meridian-kb` + `--required-marker "meridian university"` — wrong for the interviewer's banks.
   - **Fix:** new launcher calls `-SkipPrepopulate` on ERC, then runs per-domain prepopulate directly with the ERC venv python:
     `python -m enterprise_rag.prepopulate --kb question_banks/<domain>.md --doc-id bank-<domain> --tenant default --department <domain>` for `system-design ios dsa devops` (idempotent; `--force` opt-in).
   - ⚠ This is also a *Windows portability* fix: the repo's own script cannot run in plain PowerShell.

3. **LLM endpoint points at nothing**
   - Default `INTERVIEW_LLM_BASE_URL=http://127.0.0.1:8000/v1` (vLLM port) — no vLLM deployed.
   - **Fix (IMPLEMENTED):** launcher exports the user-pinned Ollama endpoint — `INTERVIEW_LLM_BASE_URL=http://127.0.0.1:11434/v1`, `INTERVIEW_LLM_MODEL=qwen2.5:14b` (§7 #3). Step 3 verifies both models present at boot and warns with `ollama pull` hints if not. (Original design had MLX auto-detect — superseded by the user's pin decision.)

4. **Embeddings dependency unverified at boot**
   - ERC prepopulate/serve embeds via Ollama `nomic-embed-text`; without it, retrieval is empty.
   - **Fix:** boot-time check `GET /api/tags` → warn + suggest `ollama pull nomic-embed-text` (present on this machine — will be `OK`).

5. **Fresh clones have no venvs**
   - Neither repo has `.venv`. Both launchers self-heal (same pattern as the reference), first run downloads deps.

6. **Redis semantic cache absent (Docker not running)** — warn-only, ERC falls back to `cache_backend=none`. Rubric cache-hit gate (Phase 1: 1.0) uses ERC's *semantic* cache — flag as degraded, not broken, when Docker is off.

7. **Phase 3 voice (LiveKit) not deployed** — `web/index.html` is a LiveKit room client; without a LiveKit server + token endpoint the page is inert. The launcher serves it optionally and warns; the runnable path is the **text-mode** interviewer (`interviewer.demo` / management-plane API).

---

## 5. The PS file — design (mirrors + improves on the reference)

**Location:** `d:\project\ai-mock-interviewer\start_services.ps1`
**Parity/improvements over `universityDemo\start_services.ps1`:**
- Same comment-based help + `.SYNOPSIS` + `.PARAMETER` + `.EXAMPLE` block
- Same colored `Write-Step / Write-OK / Write-Warn / Write-Err` helpers
- Same tool discovery (`Find-DockerCli`, `Find-DockerDesktopExe`, `Find-CloudflaredExe` — docker only needed for optional Redis)
- Same `Start-QuickTunnel` reuse (optional `-WithTunnel`; not default — nothing here needs a public URL out of the box)
- **Better:** every "missing link" from §4 has an explicit step; LLM auto-detect; per-bank prepopulate; MCP initialize handshake readiness (borrowed from ERC's own launcher); `cmd /c` file-redirection trick when invoking the ERC launcher (avoids the pipe-hang trap documented in the reference)

**Parameters (as implemented):**
```powershell
param(
    [int]$Port = 8010,                 # interviewer management plane
    [int]$RagPort = 8031,              # ERC MCP port
    [switch]$SkipRag = $false,         # skip RAG entirely (still warn)
    [switch]$SkipPrepopulate = $false, # skip question-bank ingestion
    [switch]$ForcePrepopulate = $false,# rebuild banks instead of idempotent skip
    [switch]$WithWeb = $false,         # serve static web/ on :8080
    [switch]$WithStreamlit = $false,   # Streamlit interview UI on :8501 (A3)
    [switch]$WithTunnel = $false       # quick tunnel :8010 (+ :8501 with -WithStreamlit)
)
```

**Steps (numbered, same skeleton as the reference):**

| Step | Action | Notes |
|---|---|---|
| 0 | **Dependency self-heal** (interviewer venv) | create `.venv` if missing → `pip install -e ".[api,dev,web]"`; probe `import fastapi, uvicorn, mcp` |
| 1 | Kill stale processes | ports 8010 + 8031 (+ 8501 with `-WithStreamlit`) |
| 2 | Verify ports free | retry loop like reference |
| 3 | **Ollama checks (pinned LLM)** | `/api/tags` → verify `nomic-embed-text` + `qwen2.5:14b`; warns with `ollama pull` hints; `INTERVIEW_LLM_BASE_URL=http://127.0.0.1:11434/v1`, `INTERVIEW_LLM_MODEL=qwen2.5:14b` exported (user decision §7 #3) |
| 4 | **RAG core** | invoke `enterprise-rag-core\start_services.ps1 -Port $RagPort -SkipPrepopulate` via `cmd /c` + file redirection (no pipe capture); warn-only on failure |
| 5 | **Prepopulate 4 question banks** | ERC venv python, `--department <domain>`, doc-id `bank-<domain>`, idempotent unless `-ForcePrepopulate`; requires Ollama up (checked in step 3) |
| 6 | **RAG MCP restart + readiness** | after ingestion, restart MCP (`RAG_CORE_WARM_KEYWORD=all`) so the in-memory BM25 leg warms; then POST `initialize` handshake to `:$RagPort/mcp` (temp-file body trick) |
| 7 | **Interviewer backend** | `uvicorn interviewer.server:app --port 8010` with exported env (`RAG_MCP_URL`, `INTERVIEW_LLM_*`, `INTERVIEW_DOMAIN=system-design`, `INTERVIEW_TOP_K=5`); curl `/health` readiness loop + RAG-link check (health body contains `RAG_MCP_URL`) |
| 8 | Optional UIs | **8 — Streamlit UI** on :8501 (`-WithStreamlit`, readiness probe) · **8b — static web** on :8080 (`-WithWeb`) · **8c — quick tunnel** (`-WithTunnel`, :8010 + :8501) |
| 9 | **Summary** | URLs, RAG info (backend/cache/embeddings), logs, PIDs, stop commands, degraded-mode warnings; final process-alive guard |

**Logs (all in `%TEMP%`):** `interviewer_server.log` / `_err.log`, `erc_launcher.log`, `erc_mcp.log` / `_err.log`, `interviewer_streamlit.log`, `interviewer_web.log`, `interviewer_tunnel.log` (+ `.tunnel_8501` cache).

---

## 6. Verification plan (after approval)

1. `powershell -NoProfile -ExecutionPolicy Bypass -File start_services.ps1` (first run: installs both venvs)
2. Assert: ERC `:8031/mcp` responds 200 to MCP `initialize`; `/health` on `:8010` shows `rag_mcp_url=http://127.0.0.1:8031/mcp`
3. `python -m pytest tests/ -m live` (live gate against the launched RAG) — optional but proves the RAG link end-to-end
4. `python -m interviewer.demo --questions 1` with the exported env (proves LLM + RAG + FSM)
5. Rerun the script → assert idempotency (no re-ingestion, all services reuse ports)

---

## 7. Decisions — FINALIZED by user (2026-08-31)

| # | Decision | Choice |
|---|---|---|
| 1 | File location | ✅ `d:\project\ai-mock-interviewer\start_services.ps1` (project root) |
| 2 | Port scheme | ✅ Interviewer `8010`, RAG MCP `8031` |
| 3 | LLM | ✅ Pin Ollama `http://127.0.0.1:11434/v1` + model `qwen2.5:14b` |
| 4 | Web static server | ✅ Off by default (`-WithWeb` opt-in) — **superseded in role by `-WithStreamlit`** (A1–A3): the interactive interview UI replaces the static page as the primary UI; `-WithWeb` kept for the raw voice-room page |
| 5 | Tunnel | ✅ Off by default (`-WithTunnel` opt-in; covers :8010 + :8501 with `-WithStreamlit`) |
| 6 | Prepopulate | ✅ Idempotent by default; `-ForcePrepopulate` rebuilds |

**Implementation note added during build:** BM25 is in-memory and warms from the
vector store at serve boot (`cli.py` `RAG_CORE_WARM_KEYWORD`) — the launcher
restarts the RAG MCP server after bank ingestion (`RAG_CORE_WARM_KEYWORD=all`)
so the keyword leg sees the new banks.

**Status:** `start_services.ps1` created + PowerShell parse-checked (SYNTAX OK);
A1–A3 Streamlit support implemented + validated. Not yet booted end-to-end.

---

## 8. ADDENDUM — Streamlit support (user request 2026-08-31)

**User finding:** row #4 above was NOT what they wanted — they need to **run the interviewer on Streamlit**.
**Verified:** the repo has zero Streamlit code today; `-WithWeb` serves only a static LiveKit page.

### Required adjustments

| # | Adjustment | Detail |
|---|---|---|
| A1 | **Create `web/streamlit_app.py`** — interactive text-mode interview UI | Streamlit chat app (`st.chat_message` / `st.chat_input` / `st.session_state`). Drives the existing `LLMInterviewer.run()` **untouched** via a background thread + queue-based `Candidate`: the thread blocks on `candidate.answer(question_id)`, the UI shows the interviewer's turns live and submits the user's typed answer when asked. Renders live turns, per-question scores (1–5 × Correctness/Depth/Communication), follow-up rounds, wrap summary + rubric cache hit rate. Sidebar: domain (system-design/ios/dsa/devops), number of questions, doc-id, session store choice |
| A2 | **Add `web` extra to `pyproject.toml`** | `web = ["streamlit>=1.40"]`; Step 0 installs `.[api,dev,web]` (one venv, no conditional complexity) |
| A3 | **Launcher: add `-WithStreamlit` switch** (replaces the role of row #4) | `streamlit run web/streamlit_app.py --server.port 8501 --server.headless true`, hidden, log `%TEMP%\interviewer_streamlit.log`; readiness probe on :8501; when `-WithTunnel` is also set, quick-tunnel :8501 too (same convention as the reference). `-WithWeb` (static page) is kept but now redundant — fold into `-WithStreamlit` note |

### Resulting run

```powershell
.\start_services.ps1 -WithStreamlit          # RAG :8031 + interviewer :8010 + UI http://localhost:8501
.\start_services.ps1 -WithStreamlit -WithTunnel   # ...plus public URLs
```

### Files touched
- ✅ NEW `web/streamlit_app.py` (the interview UI)
- ✅ EDIT `pyproject.toml` (add `web` extra)
- ✅ EDIT `start_services.ps1` (param + Step 8 Streamlit block + summary lines)

**Status: IMPLEMENTED (2026-08-31)** — all three files syntax-validated (PS parser OK, py_compile OK).
Not yet booted end-to-end.

---

## 9. PENDING — status as of 2026-08-31

| # | Item | Status | Notes |
|---|---|---|---|
| P1 | **End-to-end first boot** — `.\start_services.ps1 -WithStreamlit` | ✅ **PASSED** | 4 banks ingested; RAG :8031 + backend :8010 + UI :8501 up; "All launched processes are alive", exit 0 |
| P2 | **Assert RAG link** — MCP `initialize` HTTP 200 on `:8031/mcp`; `/health` reports `rag_mcp_url=:8031/mcp` | ✅ **PASSED** | `{"status":"ok","rag_mcp_url":"http://127.0.0.1:8031/mcp","rag_auth":"none"}` |
| P3 | **Live gate** — `python -m pytest tests/ -m live` | ⏳ PENDING | optional but strong; needs a few minutes |
| P4 | **Text interview smoke** — `python -m interviewer.demo --questions 1` | ✅ **PASSED** | Real qwen2.5:14b turns, RAG rubric + follow-up retrieval, score 4/3/4, avg 3.67, wall 36 s |
| P5 | **Streamlit UI boot check** — reachable at :8501 | ✅ **PASSED** (HTTP 200) | Interactive interview run: see P3/pytest or manual |
| P6 | **Idempotency rerun** — re-run the launcher; assert no re-ingestion, ports reused cleanly | ⏳ PENDING | prepopulate is idempotent (verified in code); a clean rerun remains |
| P7 | **Git housekeeping** — commit decision | ⏳ PENDING | staged ERC (59 files), modified `pyproject.toml`, untracked `start_services.ps1`, `web/streamlit_app.py`, `PLAN_start_services.md`; add `chroma_data/`, `.tunnel_*`, `models/` to parent `.gitignore` |
| P8 | **Redis Stack (semantic cache)** | ✅ **DONE (auto)** | ERC launcher started Docker Desktop + `enterprise-rag-core-redis-stack-1`; cache backend = `redisvl` |
| P9 | **LiveKit voice (Phase 3)** | ⏳ FUTURE | out of launcher scope; text mode is the runnable path |
| P10 | **MLX :1234 as LLM** | ⏳ OPTIONAL | Ollama `qwen2.5:14b` is the pinned live LLM |

### Bugs found & fixed during P1 (all in `start_services.ps1`)

1. **`$Port` clobbered by `foreach ($port ...)`** — PowerShell variables are case-insensitive; the Step 1/2 loop var overwrote the `$Port` parameter (launched uvicorn on 8501). Fixed: loop var renamed `$checkPort` (with comment).
2. **`EMBED_MODEL` missing in spawned subprocesses** — the ERC launcher sets RAG-core defaults only in its own process; our prepopulate/MCP-restart children crashed (`EMBED_MODEL is required for embed_backend='auto'`). Fixed: launcher exports the same `RAG_CORE_*`/`EMBED_MODEL`/`OLLAMA_URL` defaults.
3. **GPU → vLLM trap** — this machine has an NVIDIA RTX 5060 Ti, so ERC's `auto` embed backend resolves to vLLM (nothing deployed) → `httpx.ConnectError`. Fixed: `RAG_CORE_EMBED_BACKEND=ollama` pinned (matches the Ollama-first setup).
4. **Step 6 double-start** — when prepopulate failed, the launcher still started a second MCP (bind error) and the probe answered against the stale server. Fixed: kill → wait-until-port-free → start; reuse existing server when already up; `HasExited` guarded for `$null`.
5. **Runbook trap (not a code bug)** — launching `-File start_services.ps1` from inside `enterprise-rag-core/` runs the *ERC* launcher. Always launch from the project root.
