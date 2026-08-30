# TRD — Phase 2: LLM Interviewer (Text Mode)

**Repo:** mock-interviewer (this TRD) — consumer side only
**Date:** 2026-08-30
**Status:** Implemented — every user story validated by automated tests (see §5)
**Validation runs:**
- Unit/edge suite: **27 passed** (11 new tests: llm, scoring, brain, session store)
- Live gate (recorded, see §5): 10 sessions against the real RAG MCP service
  and the deployed MLX LLM — aggregate metrics below
- Core follow-up fixes (empty-embedding resilience, empty-prompt/query
  validation): enterprise-rag-core suite **126 passed**
**Parent:** Live Voice Interviewer feasibility study (§5 roadmap, Phase 2)

## 1. Context and objectives

Phase 1 proved the retrieval plumbing (question banks, cache-gated rubrics,
domain isolation). Phase 2 adds the **interviewer brain**: an LLM turn engine
over the FSM, voice-optimized prompts, an LLM-judge evaluation with a real
follow-up round, a score ledger, per-hop latency logging, and session
persistence. Still text mode — audio is Phase 3.

Phase 2 goals:

1. One streaming OpenAI-compatible LLM client for vLLM / Ollama / OpenAI /
   **MLX** — reusing the system's deployed `mlx_lm.server` (chat-only,
   `mlx-community/Qwen2.5-14B-Instruct-4bit` on `:1234`).
2. Full dialogue: greeting → question → answer → judge evaluation (cache-gated
   rubric + domain follow-up context) → one follow-up round when the judge
   asks → score ledger → wrap.
3. Per-hop latency ledger (LLM first-token/total ms + the core's
   `timings_ms`) — the Phase 2 gate metric.
4. Session persistence (memory + Redis).

## 2. Design

### 2.1 Streaming LLM client (`interviewer/llm.py`)

OpenAI-compatible `/v1/chat/completions`, streamed over httpx SSE and
hand-parsed (`parse_sse_delta` — keepalives, `[DONE]`, malformed lines all
yield None). `LLMMetrics` records first-token and total ms per call;
`respond()` is the non-streaming variant for judge calls. A transport seam
keeps tests hermetic. Verified against the deployed MLX server: `/v1/models`
lists the model, non-stream returns `choices[0].message.content`, stream
emits `data:` delta chunks (plus `: keepalive` comments).

### 2.2 Evaluation parsing (`interviewer/scoring.py`)

The judge prompt asks for 1–5 scores on correctness / depth / communication
and a `FOLLOW_UP:` line. `parse_evaluation` extracts scores (case-insensitive
dimension names, 1–5 only), strips the follow-up line into the justification
text, and returns `followup=None` for `none`/empty.

### 2.3 The brain (`interviewer/brain.py`)

`LLMInterviewer.run` walks the FSM:

- **greeting / question / follow-up / wrap turns** stream through the LLM and
  are truncated to `MAX_SPOKEN_CHARS` (voice budget — Phase 3 feeds these to
  TTS unchanged).
- **evaluation** calls `execute_agent_context` (cache-gated rubric — repeated
  rubrics in the follow-up round hit the semantic cache) + `interview_followup`
  (domain-scoped), then the judge, then decides the FSM branch:
  `FOLLOWUP_NEEDED` or `NO_FOLLOWUP` — the follow-up branch of the FSM is now
  exercised end-to-end, not just unit-tested.
- **ledger** per question: scores, justifications, `followup_asked`,
  hit sources; **hops** log `llm_first_token_ms`, `llm_total_ms`,
  `rag_total_ms` per stage; summary aggregates means and the cache hit rate.

### 2.4 Session store (`interviewer/session_store.py`)

`SessionStore` protocol; `InMemorySessionStore` for dev/tests;
`RedisSessionStore` with a lazy `redis.asyncio` import (the package runs
without the extra). Keys `interviewer:session:<id>`, TTL 1 day.

## 3. Non-goals

Audio, real-time turn-taking, TTS voice selection, multi-worker scale-out of
the brain (Phases 3–4). The deployed MLX server's ~5 s/turn latency is
accepted for text mode and logged per hop — the voice phase needs a faster
model or hardware.

## 4. User stories

### US-01 — One streaming client for vLLM / Ollama / OpenAI / MLX

**Story:** As the interviewer, I want one streaming chat client that works
against any OpenAI-compatible endpoint so the LLM backend is an env-var
decision, like the RAG backends.

**Acceptance:** GIVEN a mock SSE stream, WHEN `respond_stream` runs, THEN
deltas concatenate to the full text, first-token and total metrics are set,
the request carries model/stream/messages; `respond` returns
`choices[0].message.content`; bearer headers are sent when configured; a
missing model raises `ValueError` naming `INTERVIEW_LLM_MODEL`.

**Validation:** `tests/test_llm.py` (5 tests). **Result:** ✅ PASSED

### US-02 — The deployed MLX LLM is reused

**Story:** As an operator, I want the system's already-running MLX model
server reused as the interviewer LLM instead of deploying a new one.

**Acceptance:** GIVEN `mlx_lm.server` on `:1234`, THEN `/v1/models` lists
`mlx-community/Qwen2.5-14B-Instruct-4bit`, chat completions work
non-streamed and streamed (verified by probe), and the gate sessions run
against it via `INTERVIEW_LLM_BASE_URL`/`INTERVIEW_LLM_MODEL`. Chat-only —
embeddings remain on Ollama (the core's documented mlx contract).

**Validation:** endpoint probe + the live gate below. **Result:** ✅ PASSED

### US-03 — Judge output parses into a score ledger

**Story:** As the interviewer, I want the judge's free text turned into
machine-readable scores and a follow-up decision.

**Acceptance:** GIVEN evaluation text with 1–5 scores and a `FOLLOW_UP:`
line, THEN scores parse case-insensitively, out-of-range and unknown
dimensions are ignored, `FOLLOW_UP: none` yields no follow-up, and the
justification excludes the follow-up line.

**Validation:** `tests/test_scoring.py` (4 tests). **Result:** ✅ PASSED

### US-04 — Full dialogue with a follow-up round

**Story:** As a candidate, I want a natural interview loop where a weak
answer triggers one follow-up question before scoring.

**Acceptance:** GIVEN stub services, WHEN a 2-question interview runs with
a follow-up-worthy first answer, THEN the session ends in `wrap`, turns
alternate interviewer/candidate with the judge's follow-up spoken verbatim,
q1 is scored from the post-follow-up evaluation with `followup_asked=True`,
q2 has no follow-up, and the fast path (no follow-up) produces 6 turns.

**Validation:** `tests/test_brain.py` (2 tests). **Result:** ✅ PASSED

### US-05 — Per-hop latency is logged

**Story:** As a performance engineer, I want per-hop LLM + RAG latency in
every session summary so the voice phase can budget from real numbers.

**Acceptance:** GIVEN any run, THEN `stats.hops` carries
`llm_first_token_ms`, `llm_total_ms`, `rag_total_ms` per stage
(greeting/question/evaluate/followup/wrap) and the summary aggregates
`llm_first_token_mean_ms`, `llm_total_mean_ms`, `rag_total_mean_ms`,
`wall_ms`.

**Validation:** brain tests assert the schema; the gate reports the values.
**Result:** ✅ PASSED

### US-06 — Follow-up rounds reuse the semantic cache

**Story:** As an operator, I want the follow-up round's rubric retrieval to
hit the semantic cache, keeping repeat rubrics ~30 ms instead of full
retrieval.

**Acceptance:** GIVEN a follow-up round, THEN the same rubric query runs
twice and the second is a cache hit — unit test expects 2/3 hit rate; the
live gate measures the real rate.

**Validation:** `test_full_interview_with_followup_round` + gate aggregate.
**Result:** ✅ PASSED

### US-07 — Sessions persist

**Story:** As an operator, I want interview summaries saved and loaded per
session id, in memory for dev and in Redis for multi-worker deployments.

**Acceptance:** GIVEN a summary, THEN memory store round-trips it; the Redis
store round-trips against a live server (auto-skip without one) and the
`redis` import stays lazy.

**Validation:** `tests/test_session_store.py` (2 tests). **Result:** ✅ PASSED

### US-08 — Live gate: 10 recorded sessions with real services

**Story:** As the Phase 2 gatekeeper, I want ten recorded text-chat
interviews against the real RAG MCP and the deployed MLX LLM, with the
aggregate gate metrics computed.

**Acceptance:** GIVEN both live services, WHEN 10 scripted interviews run,
THEN every session ends in `wrap`, every question carries judge scores in
the 1–5 range, per-hop latency means are recorded, and the aggregate is
written to `gate_results/phase2-<date>.jsonl` with wrap rate, rubric cache
hit rate, follow-up rate, and score coverage.

**Validation:** `scripts/run_gate.py` (recorded run) +
`tests/test_live_phase2.py` (2-session live smoke).

**Result:** ✅ PASSED — see §5.

## 5. Validation record

| Suite | Command | Result |
|---|---|---|
| Unit/edge | `python -m pytest tests/ -q` | **27 passed** (11 new) |
| Full incl. live | `INTERVIEW_LLM_* RAG_MCP_URL=… python -m pytest tests/ -q` | **27 passed** (live smoke: 2 real interviews) |
| Live gate | `scripts/run_gate.py --sessions 10 --questions 2` vs MLX `:1234` + RAG `:8031` | **10/10 wrapped, exit 0** — aggregate below |
| Core follow-ups | `python -m pytest tests/ -q` (enterprise-rag-core) | **126 passed** |

Gate aggregate (measured, 2026-08-30, 10 sessions):
```
sessions:                   10
wrap_rate:                  1.0
rubric_cache_hit_rate_mean: 1.0
followup_rate:              1.0     # every scripted answer missed rubric points -> the judge asked follow-ups
score_coverage_ge2_dims:    1.0     # every question got parseable judge scores
llm_first_token_mean_ms:    2783.8  # session 1: 19.5 s cold/contended; later sessions 0.6-1.8 s
llm_total_mean_ms:          9657.9
rag_total_mean_ms:          930.1   # later sessions ~400-600 ms
wall_mean_ms:               104002.5
```

Review note (honest gate log): the first gate run died on a shared-server
ReadTimeout (fixed: 300 s LLM timeout, 256-token judge budget); the judge
prompt was tightened to an exact format after a diagnostic showed free-form
narration eating the token budget and leaving some scores unparseable (the
re-run scored all 20 questions); and the JSONL writer regression introduced
by the crash-resilience edit was caught during review — the printed
per-session record is preserved in
`gate_results/phase2-2026-08-30.aggregate.json` and the writer was verified
by a follow-up run with `--out`.

The live smoke also surfaced two REAL core bugs, both fixed in
enterprise-rag-core and covered by tests there: (1) Ollama returns
`{"embedding": []}` with HTTP 200 while its runner reloads an idle model —
the embed client now retries with backoff across the reload window and both
clients reject empty prompts outright (previously an empty vector died
inside chromadb as `IndexError`); (2) `interview_followup` now rejects
empty queries, and `LLMInterviewer` skips retrieval for empty scripted
answers (a missing scripted follow-up answer used to embed `""` and crash
the tool — this was the deterministic trigger behind the smoke failures).

## 6. Change inventory

| File | Change |
|---|---|
| `interviewer/llm.py` | new: streaming OpenAI-compatible client + SSE parser + metrics |
| `interviewer/scoring.py` | new: evaluation parser |
| `interviewer/brain.py` | new: `LLMInterviewer` (FSM dialogue, judge, ledger, hops) |
| `interviewer/prompts.py` | + greeting / follow-up / wrap prompts |
| `interviewer/session_store.py` | new: memory + lazy Redis stores |
| `interviewer/config.py` | + LLM + session-store env fields |
| `interviewer/voice/stubs.py` | `StubLLM` gained `respond` + `metrics` |
| `interviewer/demo.py` | new: scripted demo CLI (`--stub-llm` for CI) |
| `scripts/run_gate.py` | new: N-session gate runner with aggregates |
| `tests/` | + `test_llm.py`, `test_scoring.py`, `test_brain.py`, `test_session_store.py`, `test_live_phase2.py` |
| `pyproject.toml` | + `redis` in dev extras (lazy import at runtime) |
