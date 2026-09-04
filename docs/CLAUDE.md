# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Real-time, web-based AI technical mock interviewer (voice chat). This repo is the **consumer side only**: voice gateway, interviewer state machine, LLM turn engine, management-plane API, web app. Retrieval comes from the standalone **enterprise-rag-core** service (separate repo at `../enterprise-rag-core`) consumed **over MCP** — this repo never imports that package, and never touches vector stores directly.

## Commands

```bash
pip install -e ".[api,dev]"          # install; [voice] extra adds livekit-agents (Phase 3)
python -m pytest tests/              # unit tests
python -m pytest tests/ -m live      # live integration against a running RAG MCP (set RAG_MCP_URL)
python -m uvicorn interviewer.server:app --port 8010   # management plane
```

## Architecture

- `interviewer/rag_client.py` — MCP client for the RAG service (`streamable_http_client` + `ClientSession`, same pattern as enterprise-rag-core's `test_mcp_boot.py`). Tools: `retrieve_context`, `execute_agent_context`, and the Phase 1 interview tools `interview_bank` / `interview_question` / `interview_followup` (domain-scoped), plus the Phase 4 `register_bank` (runtime bank registration, 120 s budget). OIDC bearer via `RAG_MCP_TOKEN`; unset = none-auth mode.
- `interviewer/interview.py` — `ScriptedInterview` drives the FSM over MCP: bank → question → answer → cache-gated rubric (`execute_agent_context` hit_source) + domain follow-up → score ledger → wrap. Gate metric: rubric cache hit rate (`InterviewStats`). Question banks live in `question_banks/*.md` (one `## ` heading = one question) — registered automatically at launcher start (folder scan) and on demand from the Skill Update page over the RAG `register_bank` MCP tool (department = domain).
- `interviewer/skills.py` — consumer-side question-bank discovery + upload validation (stdlib only — never imports enterprise-rag-core): `discover_local_banks()`, `normalize_skill_name()`, `parse_markdown_shape()`, `validate_upload()`. The folder is the single source of truth for available skills; registration state is probed on the RAG service.
- `interviewer/llm.py` — OpenAI-compatible streaming chat client (vLLM/Ollama/MLX), hand-parsed SSE, first-token/total ms metrics per call. **The deployed MLX server on `:1234` (Qwen2.5-14B-Instruct-4bit) is the live LLM — chat only, no embeddings.** `INTERVIEW_LLM_BASE_URL`/`INTERVIEW_LLM_MODEL` configure it.
- `interviewer/brain.py` — `LLMInterviewer`: voice-optimized turns (streamed, truncated to `MAX_SPOKEN_CHARS`), LLM-judge evaluation via `interviewer/scoring.py` (parse `Correctness:/Depth:/Communication:` 1–5 + `FOLLOW_UP:`), one follow-up round per question (repeated rubrics hit the semantic cache), per-hop latency ledger, wrap summary.
- `interviewer/session_store.py` — memory + lazy-import Redis stores. In tests, one `asyncio.run` per Redis client instance (redis.asyncio binds its pool to the first loop).
- `interviewer/state_machine.py` — the interviewer dialogue FSM: `greeting → ask_question → listen → evaluate → (follow_up | score) → next → wrap`. Transitions are a pure dict; invalid (state, event) raises `InvalidTransition`. The FSM is the product — keep it fully unit-tested and free of I/O.
- `interviewer/voice/` — STT/TTS/LLM engine protocols (`protocols.py`), text-mode stubs (`stubs.py`) for development without audio, and the LiveKit worker skeleton (`agent.py`, guarded import — livekit-agents is an optional extra).
- `interviewer/server.py` — FastAPI management plane (health, session CRUD, scores, LiveKit tokens) + the Phase 4 skills surface `GET/POST /skills` + `POST /skills/reconcile` (declared BEFORE the static `web/` mount — a catch-all route shadows anything after it). Request/response only; audio never flows through it.
- Env config: `interviewer/config.py` — `RAG_MCP_URL`, `RAG_MCP_TOKEN`, `INTERVIEW_DOMAIN`, `INTERVIEW_TOP_K`. The `RAG_CORE_*` namespace belongs to the core service; this repo uses its own.

## Conventions

- The latency budget is < 1.5 s utterance-end → first audio; retrieval is budgeted < 150 ms. Every turn must be measurable — mirror the core's `timings_ms` discipline in this repo's turn logs.
- Voice-optimized prompts are short: one question per turn, no markdown, ≤ ~600 spoken chars (`prompts.py`).
- **Natural human voice is a hard requirement** for TTS: ElevenLabs / Cartesia Sonic (cloud), Kokoro-82M with a high-quality preset (self-hosted); Piper is a CPU fallback only. Robotic espeak-class voices never ship. Configured via `INTERVIEW_TTS_PROVIDER` / `INTERVIEW_TTS_VOICE_ID`.
- The interviewer quality loop needs an evaluation harness before Phase 3 — ground-truth scored sessions, prompt A/B tests.
