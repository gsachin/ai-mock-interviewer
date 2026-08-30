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
| Voice-optimized prompt templates | `interviewer/prompts.py` |
| STT/TTS/LLM engine protocols + text-mode stubs | `interviewer/voice/` |
| LiveKit agent worker skeleton (Phase 3) | `interviewer/voice/agent.py` |
| Management-plane FastAPI app | `interviewer/server.py` |
| Tests | `tests/` |

## Roadmap

Phase 1: question-bank MCP tools on the RAG side + retrieval-backed
interviewer here. Phase 2: dialogue state + LLM turn engine (text mode).
Phase 3: LiveKit voice pipeline (`pip install -e ".[voice]"`).
See `enterprise-rag-core`'s feasibility study + TRD for the full plan.
