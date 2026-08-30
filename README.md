# mock-interviewer

Real-time, web-based **AI technical mock interviewer** (voice chat) — the
consumer of the standalone [`enterprise-rag-core`](https://github.com/gsachin/enterprise-rag-core)
service. This repository contains the interviewer only: voice gateway, dialogue
state machine, LLM turn engine, and the web app. **It never imports
`enterprise-rag-core`** — retrieval is consumed over MCP from a separately
deployed RAG service.

## Topology

```
┌─────────────────────────────┐        MCP (streamable HTTP, OIDC or none)       ┌────────────────────────────┐
│  mock-interviewer (this)    │  ───────────────────────────────────────────▶   │  enterprise-rag-core      │
│  LiveKit voice worker       │   tools: retrieve_context, execute_agent_context│  `enterprise-rag-core     │
│  state machine, LLM,        │                                                  │   serve`  :8000/mcp       │
│  STT/TTS, web app           │  ◀───────────────────────────────────────────   │  chroma/qdrant + ES +     │
└─────────────────────────────┘              chunk payloads                     │  Redis + reranker         │
                                                                                 └────────────────────────────┘
```

- **Voice hot path** (Phase 3): browser mic → LiveKit SFU → agent worker
  (VAD → STT → state machine → `RagClient.retrieve_context` → LLM stream →
  TTS stream) → SFU → browser speaker.
- **Management plane** (this repo's FastAPI app): session CRUD, scores,
  question-bank admin triggers — request/response only, never audio.

## Quick start

RAG service (separate repo, zero infra):

```bash
# in enterprise-rag-core
pip install -e ".[dev]"
enterprise-rag-core prepopulate --kb kb.md --doc-id meridian-kb --tenant default
RAG_CORE_WARM_KEYWORD=all enterprise-rag-core serve     # :8000/mcp, none-auth mode
```

This repo:

```bash
pip install -e ".[api,dev]"
python -m uvicorn interviewer.server:app --port 8010    # management plane + health
RAG_MCP_URL=http://127.0.0.1:8000/mcp python -m pytest tests/ -m live   # live integration (optional)
```

OIDC mode: set `RAG_MCP_TOKEN` to a bearer with scope `rag:retrieve`.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `RAG_MCP_URL` | `http://127.0.0.1:8000/mcp` | enterprise-rag-core MCP endpoint |
| `RAG_MCP_TOKEN` | — | OIDC bearer token; unset = none-auth mode |
| `INTERVIEW_DOMAIN` | `system-design` | question-bank department filter |
| `INTERVIEW_TOP_K` | `5` | chunks per retrieval call |
| `INTERVIEW_TTS_PROVIDER` | `cartesia` | `cartesia` \| `elevenlabs` \| `kokoro` \| `piper` |
| `INTERVIEW_TTS_VOICE_ID` | — | voice preset; unset = provider default |
| `INTERVIEW_LLM_BASE_URL` | `http://127.0.0.1:8000/v1` | OpenAI-compatible chat endpoint (vLLM / Ollama `/v1` / MLX `mlx_lm.server`) |
| `INTERVIEW_LLM_MODEL` | — | required for LLM turns, e.g. `mlx-community/Qwen2.5-14B-Instruct-4bit` |
| `INTERVIEW_LLM_TOKEN` | — | bearer for hosted endpoints |
| `INTERVIEW_SESSION_STORE` | `memory` | `memory` \| `redis` (Redis-backed session summaries) |
| `INTERVIEW_REDIS_URL` | `redis://localhost:6379` | Redis URL for the session store |

## Voice quality (hard requirement)

The interviewer **must sound like a natural human voice** — robotic
(espeak-class) voices are excluded from production. Preference order:
**ElevenLabs** (best naturalness), **Cartesia Sonic** (natural voice presets
with the lowest first-audio latency), **Kokoro-82M** (best self-hosted,
pick a high-quality preset), **Piper** (self-hosted CPU fallback only).
The voice identity is per-deployment config, not per-user audio; see
`interviewer/voice/protocols.py`.

## Layout

| Thing | Location |
|---|---|
| MCP client for the RAG service | `interviewer/rag_client.py` |
| Interviewer state machine (Phase 2) | `interviewer/state_machine.py` |
| Scripted text-mode interview loop (Phase 1 gate) | `interviewer/interview.py` |
| Domain question banks (16 questions, real content) | `question_banks/` + `scripts/prepopulate_banks.sh` |
| Voice-optimized prompt templates | `interviewer/prompts.py` |
| STT/TTS/LLM engine protocols + text-mode stubs | `interviewer/voice/` |
| LiveKit agent worker skeleton (Phase 3) | `interviewer/voice/agent.py` |
| Management-plane FastAPI app | `interviewer/server.py` |
| Tests (unit + `live` integration) | `tests/` |

## Phase 1: scripted interview over MCP

```bash
scripts/prepopulate_banks.sh               # ingest the 4 domain banks (idempotent)
RAG_MCP_URL=http://127.0.0.1:8031/mcp python -m pytest tests/ -m live
```

`ScriptedInterview` walks the full FSM over the RAG service: question bank
catalog → exact question fetch → candidate answer → cache-gated rubric
retrieval + domain follow-up → score ledger → wrap, reporting the rubric
cache hit rate and per-turn timings (live gate: hit rate 1.0, 28–43 ms per
question).

## Phase 2: LLM interviewer (text mode)

```bash
INTERVIEW_LLM_BASE_URL=http://127.0.0.1:1234/v1 \
INTERVIEW_LLM_MODEL=mlx-community/Qwen2.5-14B-Instruct-4bit \
RAG_MCP_URL=http://127.0.0.1:8031/mcp \
python -m interviewer.demo --questions 2            # full scripted interview
python scripts/run_gate.py --sessions 10            # recorded gate sessions + aggregates
```

`LLMInterviewer` (interviewer/brain.py) drives the FSM with an
OpenAI-compatible LLM (interviewer/llm.py — vLLM / Ollama / MLX, streamed,
first-token + total metrics per hop): voice-optimized turns, an LLM-judge
evaluation against the cache-gated rubric plus domain follow-up chunks
(interviewer/scoring.py), one follow-up round when the judge asks, a score
ledger, and session persistence (interviewer/session_store.py). See
`docs/TRD_PHASE2_LLM_INTERVIEWER.md`.

The deployed MLX server (`mlx_lm.server` on `:1234`,
`mlx-community/Qwen2.5-14B-Instruct-4bit`) is reused as the live LLM — chat
only, so embeddings stay on Ollama `nomic-embed-text`.

## Roadmap

Phase 3: LiveKit voice pipeline (`pip install -e ".[voice]"`) — plug the
brain's turns into STT/TTS engines and meet the < 1.5 s budget.
See `enterprise-rag-core`'s feasibility study + TRDs for the full plan.
