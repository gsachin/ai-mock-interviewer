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
| Domain question banks (one `## ` per question; 7 banks as of 2026-09-04) | `question_banks/` + auto-registration (`start_services.ps1` Step 5, `scripts/prepopulate_banks.sh`, Skill Update page) |
| Voice-optimized prompt templates | `interviewer/prompts.py` |
| STT/TTS/LLM engine protocols + text-mode stubs | `interviewer/voice/` |
| LiveKit agent worker skeleton (Phase 3) | `interviewer/voice/agent.py` |
| Management-plane FastAPI app | `interviewer/server.py` |
| Tests (unit + `live` integration) | `tests/` |

## Phase 1: scripted interview over MCP

```bash
scripts/prepopulate_banks.sh               # ingest every question_banks/*.md (idempotent — new banks auto-register)
RAG_MCP_URL=http://127.0.0.1:8031/mcp python -m pytest tests/ -m live
```

New skills need no script edits: drop a `.md` bank into `question_banks/` and
it registers on the next start, or upload it on the **Skill Update** page
(`http://127.0.0.1:8010/skills.html`) while services run — see
`docs/TRD_PHASE4_DYNAMIC_SKILL_REGISTRATION.md`.

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

## Phase 3: voice interview in the browser (LiveKit)

One command boots the full voice stack (needs the `.tools/livekit/livekit-server.exe`
binary — download the `windows_amd64` release zip from
github.com/livekit/livekit/releases into `.tools/livekit/`):

```powershell
.\start_services.ps1 -WithVoice
# Voice UI:  http://127.0.0.1:8010/   (pick a domain, Start, speak)
# LiveKit:   http://127.0.0.1:7880    (dev mode, key devkey/secret)
# Worker:    python -m interviewer.voice.worker (auto-started, PID in the summary)
```

What happens: the browser gets a LiveKit JWT from `POST /voice/token`
(`interviewer/server.py`), joins room `interview-<domain>-<sid>`, and the
worker's `run_agent` (`interviewer/voice/agent.py`) runs one interview per
room: candidate audio → silero VAD → faster-whisper STT → `LLMInterviewer`
(RAG over MCP, fast voice LLM llama3.2:3b) → Kokoro TTS sentence streaming →
room playback, with barge-in. Transcripts + the final summary reach the page
as LiveKit data packets.

Manual run (equivalent env):

```powershell
$env:INTERVIEW_STT_PROVIDER = "faster-whisper"; $env:INTERVIEW_TTS_PROVIDER = "kokoro"
$env:INTERVIEW_VOICE_LLM_BASE_URL = "http://127.0.0.1:11434/v1"; $env:INTERVIEW_VOICE_LLM_MODEL = "llama3.2:3b"
$env:LIVEKIT_URL = "http://127.0.0.1:7880"; $env:LIVEKIT_API_KEY = "devkey"; $env:LIVEKIT_API_SECRET = "secret"
python -m uvicorn interviewer.server:app --port 8010          # token endpoint + UI
python -m interviewer.voice.worker                            # registers with :7880
```

Scripted end-to-end verification (no mic needed — pre-recorded Kokoro answers
played into the room, asserts the interview completes and scores):

```powershell
python scripts/e2e_voice_client.py --domain system-design --answers 5
```

**Known dev-mode caveats (livekit-server `--dev`):**
- The dev server drops a worker that idles ~20 s — a session started right
  after boot works; for back-to-back sessions re-run `start_services.ps1
  -WithVoice` (kill-stale handles both processes).
- The < 1.5 s voice budget needs GPU/cloud engines: all-CPU faster-whisper +
  Kokoro measure ~3.6 s TTS first audio + ~1 s STT (see `voice_budget_bar`
  in the summary). The stage budgets (`interviewer/voice/budget.py`) keep the
  bar measurable on real engines; llama3.2:3b first token is ~280 ms, RAG
  ~200 ms.
- First-run downloads: faster-whisper `base` model + Kokoro ONNX (~110 MB)
  + silero VAD; Ollama needs `llama3.2:3b` pulled (`ollama pull llama3.2:3b`).
- faster-whisper falls back CUDA→CPU automatically when the cuDNN/cuBLAS
  runtime is missing; Windows Smart App Control can block PyAV ≥ 13 DLLs —
  the `[voice]` extra pins `av==12.3.0`, which loads clean.

## Roadmap

Phase 3 is implemented and verified end-to-end (see the runbook above).
Remaining quality work: get the real-engine round-trip under 1.5 s (GPU
whisper / kokoro-int8 or cloud STT/TTS), persistent session summaries to the
management plane, and the evaluation harness for prompt A/B tests.
See `enterprise-rag-core`'s feasibility study + TRDs for the full plan.
