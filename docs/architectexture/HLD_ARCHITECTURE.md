# High-Level Architecture — AI Mock-Interviewer

> High-level design of the mock-interviewer consumer repo: voice gateway,
> dialogue state machine, LLM turn engine, web app — and its integration with
> the standalone `enterprise-rag-core` RAG service over MCP.

## 1. What this project is

Real-time, web-based **AI technical mock interviewer** (text + voice) — the
consumer of the standalone `enterprise-rag-core` service. The interviewer
**never performs retrieval locally** — every context look-up goes over **MCP**
to a separately deployed RAG service via `interviewer/rag_client.py`. A copy
of `enterprise-rag-core/` is embedded in this repo for dev/testing, but in
production it is a separate service the interviewer calls.

The system grew in four phases that still coexist:

| Phase | What it added | Key file |
|---|---|---|
| 1 | Scripted text interview walking the FSM over MCP | `interviewer/interview.py` |
| 2 | LLM-driven text interviewer (LLM turns + LLM-judge scoring) | `interviewer/brain.py`, `interviewer/llm.py` |
| 3 | Real-time voice interview in the browser (LiveKit) | `interviewer/voice/` |
| 4 | Dynamic skill registration (drop a `.md` bank → auto-ingested) | `interviewer/skills.py`, `web/skills.html` |

## 2. System topology (two services)

```
┌───────────────────────────── mock-interviewer (this repo) ─────────────────────────────┐
│                                                                                          │
│   Browser UI (web/index.html, skills.html) ──► FastAPI management plane (interviewer/server.py)
│       │  mic/speaker                                   │  POST /voice/token (LiveKit JWT)
│       │                                               │  GET/POST /skills (bank admin)
│       ▼                                               ▼
│   LiveKit SFU (:7880)  ◄──►  Agent worker (interviewer/voice/worker.py)
│   │  (audio rooms)            └─ per room: run_agent (voice/agent.py)
│   │                               VAD → STT → LLMInterviewer brain (brain.py)
│   │                                    → LLM turn → sentence TTS
└───┼──────────────────────────────────────────────────────────────────────────┼──────────┘
    │  HTTP (browser ↔ FastAPI, request/response only — audio never passes here)
    │
    │  MCP streamable-HTTP  (:8000/mcp)  — tools: retrieve_context, execute_agent_context,
    │                                     register_bank, interview_bank (via rag_client.py)
    ▼
┌───────────────────────────── enterprise-rag-core (separate service) ───────────────────┐
│  MCP server (enterprise_rag/orchestrator.py) → hybrid search (enterprise_rag/hybrid.py) │
│     vector: Chroma/Qdrant (adapters/) + keyword: BM25/Elasticsearch                     │
│     → reranker → Redis cache (cache.py) → chunk payloads (clearance-filtered)          │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

**Two planes, deliberately split:**

- **Management plane** — the FastAPI app (`interviewer/server.py`): session
  registry, scores, LiveKit token issuance (`POST /voice/token`), and the
  question-bank admin surface (`GET/POST /skills`, `POST /skills/reconcile`).
  Plain request/response.
- **Voice hot path** — runs in the **LiveKit agent worker**, never in the
  FastAPI app, so audio latency is never queued behind HTTP requests.

## 3. Component inventory

### 3.1 mock-interviewer (this repo)

| Component | File | Responsibility |
|---|---|---|
| FastAPI management plane | `interviewer/server.py` | Session registry, LiveKit JWT issuance, skills API, static UI |
| MCP client | `interviewer/rag_client.py` | One MCP session per call (`streamable_http_client`); none-auth or OIDC bearer (`RAG_MCP_TOKEN`) |
| Dialogue FSM | `interviewer/state_machine.py` | Pure state machine — no I/O, unit-testable; transitions are a plain dict |
| Interviewer brain | `interviewer/brain.py` | `LLMInterviewer` drives the FSM over RAG-MCP + OpenAI-compatible LLMs (text and voice modes) |
| LLM client | `interviewer/llm.py` | Streamed OpenAI-compatible chat (vLLM / Ollama / MLX); first-token + total metrics per hop |
| LLM-judge scoring | `interviewer/scoring.py` | Parses judge evaluation vs rubric + follow-up chunks; verdict per turn |
| Voice prompts | `interviewer/prompts.py` | Voice-optimized prompt templates, spoken-char caps |
| Session store | `interviewer/session_store.py` | Persistence: `memory` \| `redis` |
| Skill registration | `interviewer/skills.py` | Parse `.md` banks, `register_bank` over MCP, reconcile |
| Config | `interviewer/config.py` | Env-driven (`INTERVIEW_*`, `RAG_MCP_*`, `LIVEKIT_*`) |
| Voice worker | `interviewer/voice/worker.py` | Boots agent, registers with LiveKit |
| Voice agent | `interviewer/voice/agent.py` | `run_agent`, one interview per room; VAD→STT→brain→TTS; barge-in; transcripts/summary as data packets |
| STT + VAD | `interviewer/voice/stt.py` | silero VAD endpointing + faster-whisper |
| TTS | `interviewer/voice/tts.py` | Kokoro (default self-hosted) / Cartesia / ElevenLabs / Piper |
| Sentence splitter | `interviewer/voice/splitter.py` | Streams LLM text sentence-by-sentence so audio starts after sentence 1 |
| Latency budget | `interviewer/voice/budget.py` | `< 1.5 s` round-trip gate; per-hop records |
| Question banks | `question_banks/*.md` | Content source of truth — one `## ` heading per question; new banks auto-register |
| Web UI | `web/index.html`, `web/skills.html`, `web/streamlit_app.py` | Voice UI, Skill Update page, legacy Streamlit text UI |

### 3.2 enterprise-rag-core (separate repo, embedded copy for dev)

| Component | File | Responsibility |
|---|---|---|
| MCP server + tools | `enterprise_rag/orchestrator.py` | Exposes `retrieve_context`, `execute_agent_context`, `register_bank`, `interview_bank` etc. at `:8000/mcp` |
| Hybrid search | `enterprise_rag/hybrid.py` | Vector + keyword fusion |
| Vector adapters | `enterprise_rag/adapters/chroma_vector.py`, `qdrant_vector.py`, `memory_vector.py` | Pluggable vector backends |
| Keyword adapters | `enterprise_rag/adapters/bm25_memory.py`, `elasticsearch_keyword.py` | Pluggable keyword backends |
| Reranker | `enterprise_rag/reranker.py` | Re-scores retrieved chunks |
| Cache | `enterprise_rag/cache.py` | Redis cache (rubric retrieval is cache-gated) |
| Security | `enterprise_rag/security.py` | OIDC tokens, per-chunk clearance filtering |
| Ingestion / warmup | `enterprise_rag/prepopulate.py`, `warmup.py`, `ingestion/` | Bank/doc ingestion and cache warming |

## 4. Interview flow — full interview lifecycle

```
 Candidate picks domain (question_banks/*.md)
        │
        ▼
 FastAPI issues LiveKit JWT  ──►  browser joins room "interview-<domain>-<sid>"
        │
        ▼
 Worker's run_agent (voice/agent.py) starts 1 interview per room
        │
        ▼
 ┌────────────────────────── Interviewer FSM (state_machine.py) ─────────────────────────┐
 │                                                                                        │
 │  GREETING ──greeted──► ASK_QUESTION ──question_asked──► LISTEN                          │
 │     ▲                                                     │                             │
 │     │                                                     │ answer_received (STT final) │
 │     │                                                     ▼                             │
 │  WRAP ◄──no_more── NEXT ◄──scoring_done── SCORE ◄──no_followup── EVALUATE               │
 │  (terminal)          ▲                                   │                              │
 │                      │                        followup_needed                           │
 │                      │                                   ▼                              │
 │                      └────── ask_question ◄─ next  FOLLOW_UP ──followup_asked──► LISTEN │
 │                              (more questions)                                            │
 └──────────────────────────────────────────────────────────────────────────────────────────┘
        │
        ▼
 Wrap: summary + scores → transcript/data packets back to the page
```

States/events are plain enums; transitions are a dict — an illegal
`(state, event)` pair raises `InvalidTransition` so bugs surface at the call
site instead of silent state drift (`interviewer/state_machine.py`).

## 5. One voice turn — the latency-budgeted hot path (Phase 3)

The `< 1.5 s` round-trip path tracked by `interviewer/voice/budget.py`:

```
 Candidate speaks                      Interviewer speaks
      │                                      ▲
      ▼                                      │
 Browser mic ──► LiveKit SFU ──► silero VAD (endpointing)
                                      │ speech detected
                                      ▼
                            faster-whisper STT (voice/stt.py)
                                      │ final transcript
                                      ▼
 ┌──────────────── LLMInterviewer turn (brain.py) ────────────────┐
 │  1. retrieve_context over MCP  (~200 ms, top-k cache-gated)     │
 │  2. voice LLM llama3.2:3b stream (llm.py, ~280 ms first token)  │
 │  3. SentenceAccumulator splits stream (voice/splitter.py)       │
 └─────────────────────────────────────────────────────────────────┘
                                      │ sentence 1 complete
                                      ▼
                            Kokoro TTS streams sentence (voice/tts.py)
                                      │  (not the whole answer!)
                                      ▼
                    room playback ──► browser speaker   ◄── barge-in:
                                                          new speech stops TTS
```

Key design choices:

- **Sentence-level TTS streaming** — first audio after sentence one, never
  after the full LLM answer.
- **Barge-in** — new candidate speech interrupts the TTS playback.
- **Voice quality is a hard requirement** — natural human voices only
  (ElevenLabs > Cartesia > Kokoro > Piper fallback; espeak-class excluded);
  voice identity is per-deployment config.
- **Audio never crosses the FastAPI app** — the hot path is contained in the
  LiveKit agent worker.
- All-CPU engines measure ~3.6 s TTS + ~1 s STT; the sub-1.5 s budget requires
  GPU/cloud engines (`voice_budget_bar` in the summary).

## 6. LLM turn internals — text-mode pipeline (Phase 2)

The same brain (`LLMInterviewer`) drives text mode (`interviewer/demo.py`,
`scripts/run_gate.py`) and voice mode:

```
 system prompt (voice-optimized, prompts.py)
        │
        ▼
 ┌─── question turn:   RAG rubric chunks ──► LLM stream ──► spoken question
 │                                              │
 │   candidate answer (text or STT)              ▼
 │   ┌────────────────────────────────────────────────────────────┐
 │   │ EVALUATE: LLM-judge vs rubric+domain chunks (scoring.py)   │
 │   │  → verdict → follow_up needed? ── yes ──► one follow-up round│
 │   │                 │ no                                        │
 │   │                 ▼                                          │
 │   │            SCORE ledger → NEXT question                    │
 │   └────────────────────────────────────────────────────────────┘
 │
 └──► WRAP: summary persisted (session_store.py — memory or Redis)
```

Every hop logs `timings_ms` (RAG, LLM first-token, STT, TTS) into the session
summary — that is how the latency gate is measured and reported.

## 7. Dynamic skill registration (Phase 4)

```
 question_banks/*.md (new bank dropped in, or uploaded via web/skills.html)
        │
        ▼
 FastAPI  POST /skills/reconcile  (interviewer/skills.py)
        │  parses "## " headers → questions
        ▼
 MCP register_bank ──► RAG ingests question bank as retrievable chunks
        │
        ▼
 INTERVIEW_DOMAIN filter picks the bank → interview_bank catalogs questions
```

New skills need no script edits: drop a `.md` bank into `question_banks/` and
it registers on the next service start, or upload it on the **Skill Update**
page while services run.

## 8. Deployment / process map

| Process | Command | Port | Purpose |
|---|---|---|---|
| FastAPI app | `python -m uvicorn interviewer.server:app` | 8010 | Management plane + UI + token/skills endpoints |
| LiveKit SFU | `.tools/livekit/livekit-server.exe` (dev mode) | 7880 | Audio room transport |
| Voice worker | `python -m interviewer.voice.worker` | — | Registers with LiveKit; runs one agent per room |
| RAG service | `enterprise-rag-core serve` (separate repo) | 8000/mcp | MCP retrieval (none-auth or OIDC) |
| LLM engines | vLLM / Ollama `/v1` / MLX `mlx_lm.server` | e.g. 11434, 1234 | OpenAI-compatible chat endpoints (text LLM + fast voice LLM) |

One command boots the full voice stack: `.\start_services.ps1 -WithVoice`
(see `start_services.ps1`).

## 9. One-line summary

Browser ↔ LiveKit SFU ↔ agent worker (FSM-driven `LLMInterviewer` brain doing
MCP-RAG + LLM + TTS per hop) — with FastAPI only issuing tokens and managing
skills/sessions, and the `enterprise-rag-core` MCP service owning all
retrieval.
