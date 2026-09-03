# PLAN — Enable the Voice Interview in the Browser (Phase 3)

**Date:** 2026-09-01 · **Author:** Claude Code (gap analysis — implementation not started)
**Scope:** Why the voice interview cannot start today, and the full task list to make it run end-to-end in the browser.

> **STATUS: IMPLEMENTED + VERIFIED 2026-09-02** — all phases A–F done; the
> voice interview runs end-to-end in the browser (`start_services.ps1
> -WithVoice`, 3× E2E passes). See `STATUS.md` for results and caveats.
> Decisions resolved: D1 self-host dev server, D3 Kokoro TTS (C1 done),
> D2 faster-whisper STT (C5 done), D5 llama3.2:3b, D6 FastAPI-served UI.

---

## 1. Executive summary

The voice pipeline is **~60% built but 0% wired**. The engine layer (protocols,
STT/TTS providers, sentence splitter, latency budget, brain-side TTS streaming,
barge-in flag, unit tests) is implemented and green. But the delivery chain that
would actually let a browser user talk to the interviewer **does not exist**:

- The LiveKit agent worker raises `NotImplementedError` (`interviewer/voice/agent.py:43`).
- There is no LiveKit server, no API keys, no token endpoint, no worker process.
- The brain **collects TTS audio bytes and throws them away** — nothing plays them.
- The browser page is a bare demo that `prompt()`s for a URL + JWT.

So today "starting the voice interview" is impossible by design — every entry
point is a stub. The task list in §6 turns it into a runnable product.

**Verified environment snapshot (2026-09-01):**

| Check | Result |
|---|---|
| Ollama `:11434` | ✅ up — `qwen2.5:14b` (9 GB), `nomic-embed-text`. **`llama3.2:3b` (the hot-path voice LLM) missing** |
| RAG MCP `:8031` / backend `:8010` / Streamlit `:8501` | ❌ all down (STATUS.md said "left running" — no longer true) |
| Redis Stack `:6379` (semantic cache) | ❌ down (no docker containers) |
| `livekit-server` binary | ❌ not installed |
| `livekit-agents` in `.venv` | ❌ not installed (only `numpy` among audio deps) |
| `LIVEKIT_*` / Deepgram / Cartesia / ElevenLabs keys | ❌ none present |

---

## 2. What already works (do not rebuild)

| Component | Where | Status |
|---|---|---|
| Engine protocols (`STTEngine` / `TTSEngine` / `LLMEngine`) | `interviewer/voice/protocols.py` | ✅ done |
| Deepgram STT, faster-whisper STT | `interviewer/voice/stt.py` | ✅ done (Deepgram is per-frame HTTP, not websocket streaming) |
| Cartesia / ElevenLabs / Piper TTS engines | `interviewer/voice/tts.py` | ✅ done (kokoro allowlisted but **raises** — not wired) |
| Sentence-level TTS streaming in the brain (first audio after sentence 1) | `interviewer/brain.py:138-170` | ✅ done — but audio is **collected, never played** (§3 F3) |
| Barge-in flag (`interrupt()`) | `interviewer/brain.py:103-107` | ✅ done, unit-tested |
| Latency budget tracker (< 1500 ms gate, stage budgets) | `interviewer/voice/budget.py` | ✅ done, unit-tested |
| Voice factory (`build_voice_interviewer`) | `interviewer/voice/interviewer.py:32` | ✅ done |
| `AudioCandidate` (bytes → STT → measured transcript) | `interviewer/voice/interviewer.py:14` | ✅ done — test-only shape (preloaded dict), not event-driven |
| Voice unit tests (TTS through brain, barge-in, 10 concurrent sessions, provider policy) | `tests/test_voice.py` | ✅ 8 tests green (stub engines) |
| Phase-3 live gate (real RAG + voice LLM + simulated STT/TTS latencies, < 1500 ms) | `tests/test_live_phase3.py` | ✅ written; needs RAG :8031 + voice LLM up to run |

---

## 3. Failure map — where the voice interview is broken

### F1. The agent worker is a skeleton — the #1 blocker
`interviewer/voice/agent.py:34-44`: `on_enter()` raises `NotImplementedError`.
No room join, no STT/VAD wiring, no greeting, no candidate feed, no playback,
no dispatch. Everything downstream of the LiveKit room is missing.

### F2. No LiveKit infrastructure anywhere
- No LiveKit server deployed (`docs/PLAN_start_services.md:32`: `:7880` ❌),
  no `livekit-server` binary (verified today).
- No `LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` in
  `InterviewerConfig` (`interviewer/config.py`) — the config layer cannot
  even represent a LiveKit deployment.
- No token endpoint: `interviewer/server.py` is management-plane only
  (health + session CRUD). The browser needs a `POST /voice/token` route that
  mints a LiveKit JWT. `web/index.html:31-32` works around this with
  `prompt()` dialogs asking the **user** to paste a JWT — not shippable.

### F3. The brain never plays audio
`interviewer/brain.py:149-169`: `_speak()` synthesizes sentences into an
`audio: list[bytes]` and **drops it**. There is no playback sink interface in
`LLMInterviewer` — even with a real TTS engine wired, `run()` is silent.
The unit tests pass because `RecordingTTS` asserts on captured sentences,
not on playback.

### F4. The candidate model is pull-based, not event-driven
`LLMInterviewer.run()` drives the whole interview in one coroutine and calls
`candidate.answer(question_id)` per turn (`brain.py:214`, `brain.py:251`).
`AudioCandidate` answers from a preloaded `dict[str, bytes]` — a test artifact.
Live audio is event-driven: VAD fires "utterance end" → STT final → transcript
must be handed to a **queue-based candidate** that unblocks `answer()`. That
`LiveKitCandidate` does not exist.

### F5. No audio-format normalization for LiveKit
The TTS engines emit incompatible formats; LiveKit room audio is 48 kHz s16le
frames:
- Cartesia: raw PCM `f32le` @ 24 kHz (`tts.py:42-43`) → must convert.
- ElevenLabs: `mp3_44100_128` (`tts.py:65`) → must decode (+ mp3 decoder dep).
- Piper: wav @ 22.05 kHz → must decode + resample.
No conversion module exists (`numpy` is installed, which is enough for the
Cartesia/Piper math; mp3 needs a decoder like `av` or `pydub`+ffmpeg).

### F6. STT/TTS gaps
- **Kokoro unwired**: `resolve_tts` raises for the allowlisted self-hosted
  option (`tts.py:114-116`).
- **Piper not installed**: no `piper` binary / voice model on this machine;
  on Windows that means downloading the release zip + a voice pack.
- **Deepgram is one HTTP POST per audio frame** (`stt.py:25-38`) — fine as a
  first cut (finals ~300 ms), but a websocket stream (or livekit-plugins)
  removes per-utterance HTTP setup. Decision in §5.
- **Defaults are broken**: `tts_provider="cartesia"` is the default
  (`config.py:18`) and fails at resolve-time without `INTERVIEW_CARTESIA_API_KEY`;
  `stt_provider="stub"` (`config.py:28`) silently returns nothing in prod.

### F7. The browser page is a 60-line bare client
`web/index.html`: hard-coded prompts for URL + JWT, no domain picker, no
transcript, no scores, no hang-up, no token fetch from the management plane.
Serving is an optional `python -m http.server` on :8080 (`start_services.ps1`),
which forces CORS on the token endpoint. It also has zero styling for the
actual interview flow (state, follow-ups, wrap scores).

### F8. The hot-path voice LLM is missing
- The < 1500 ms budget needs a fast small model (`tests/test_live_phase3.py:58-60`
  pins `llama3.2:3b` on Ollama :11434) — **not pulled** (verified).
- The only LLM present, `qwen2.5:14b`, has ~6 s first token
  (`docs/STATUS.md:46`) → ~4× over budget on the greeting/question hot path.
- `INTERVIEW_VOICE_LLM_BASE_URL` / `INTERVIEW_VOICE_LLM_MODEL` are not exported
  by `start_services.ps1`.

### F9. No worker process, no launcher step, no runbook
- No entry point like `python -m interviewer.voice.worker` (register the agent,
  dispatch per room, build a per-room brain via `build_voice_interviewer`).
- `start_services.ps1` has no LiveKit step (start server :7880, start worker,
  readiness probe, logs, summary).
- README references "the README runbook" for LiveKit wiring — **that runbook
  was never written**.

### F10. Session/room linkage missing
`server.py` creates `Session`s in a registry, but nothing maps a LiveKit room
to a session, and nothing persists the worker's interview summary into the
management plane (the worker and the API are separate processes).

---

## 4. Target architecture (end state)

```
browser (web/index.html, served by FastAPI :8010)
   │  ① POST /voice/token {domain} → {livekit_url, token, room}
   ▼
LiveKit SFU (livekit-server :7880, dev mode)  ──── ws audio
   │  room "interview-<sid>"    ② dispatch
   ▼
agent worker (python -m interviewer.voice.worker, LiveKit Worker)
   room audio → VAD (silero) → utterance-end events → Deepgram/faster-whisper STT
   → LiveKitCandidate (asyncio.Queue) → LLMInterviewer.run()
   → voice LLM (llama3.2:3b, Ollama) + RAG MCP :8031 → sentence TTS
   → AudioSink → format convert (48k s16le) → room playback
   barge-in: user speech mid-turn → interviewer.interrupt()
   scores/summary → session store → GET /sessions/{id} for the UI
```

Budget stays the existing five-stage bar: STT 300 + RAG 150 + LLM first token
500 + TTS first audio 200 + network 100 ≤ 1500 ms (`voice/budget.py`).

---

## 5. Decisions needed before implementation

| # | Decision | Options | Recommendation |
|---|---|---|---|
| D1 | LiveKit deployment | (a) self-host dev: `livekit-server --dev` via docker or binary; (b) LiveKit Cloud | **(a)** — free, local, matches "zero infra" dev style; cloud later |
| D2 | STT provider | (a) Deepgram cloud (key needed); (b) faster-whisper local (CPU load, model download); (c) browser Web Speech API (no server STT) | **(a) Deepgram** if a key is available, else **(b)** `base`/`small` model. Keep the existing `resolve_stt` protocol |
| D3 | TTS provider | (a) Cartesia (key); (b) ElevenLabs (key); (c) Piper local (binary + voice pack, ~quality floor); (d) wire Kokoro (self-hosted, better than Piper) | **(a) Cartesia** if a key exists; otherwise **(d) Kokoro** (wire it — allowlisted, best self-hosted naturalness). Piper as last resort |
| D4 | LiveKit STT/TTS plugins vs our protocols | livekit-agents ships `livekit-plugins-*` for Deepgram/ElevenLabs/Cartesia with native streaming; our protocols are engine-agnostic and already tested | Use our protocols behind thin LiveKit adapters in the worker (repo convention: one contract, swappable implementations). VAD/turn-detection comes from livekit-agents regardless |
| D5 | Hot-path voice LLM | (a) `llama3.2:3b` (test-pinned); (b) any small Ollama model | **(a)** — already the gate's default; `ollama pull llama3.2:3b` |
| D6 | UI serving | (a) FastAPI serves `web/` statically (no CORS); (b) keep :8080 http.server + CORS middleware | **(a)** — single origin, simpler security |

**✅ Resolved by user (2026-09-01):**
- **D1 → (a) self-host dev** — `livekit-server` on `:7880` (docker image or Windows binary).
- **D3 → Kokoro TTS** (self-hosted, no cloud keys) — task **C1 is mandatory**;
  Piper (C2) demoted to optional fallback; Cartesia/ElevenLabs remain implemented but unused for now.
- **D2 → local faster-whisper** STT (no Deepgram key) — task **C5** added below; C4 (Deepgram streaming) dropped.
- **D4** our protocols behind thin LiveKit adapters · **D5** `llama3.2:3b` · **D6** FastAPI serves `web/`.

---

## 6. Full task list

### Phase A — LiveKit infrastructure & config

| ID | Task | Files | Acceptance |
|---|---|---|---|
| A1 | Add `livekit_url`, `livekit_api_key`, `livekit_api_secret` to `InterviewerConfig` + `from_env` (`LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`) | `interviewer/config.py` | config round-trips the three vars; unit test |
| A2 | Token endpoint `POST /voice/token` `{domain}` → `{livekit_url, token, room}`: create `Session` + room `interview-<sid>`, mint LiveKit JWT (room join + publish + subscribe, agent dispatch for the interviewer agent) via `livekit.api.AccessToken` | `interviewer/server.py`, deps: `livekit-api` | curl returns a token the LiveKit SDK accepts; unit test with fixed key/secret |
| A3 | Add `livekit-api` (or rely on transitive `livekit-agents`) + `numpy` to extras; install `[voice]` | `pyproject.toml` | `pip install -e ".[voice]"` succeeds |
| A4 | LiveKit server: document + launcher step to run `livekit-server --dev` (docker `livekit/livekit-server:latest` or binary), `:7880`, dev API key/secret, readiness probe | `start_services.ps1`, docs | server answers on :7880; probe green |
| A5 | Pull the hot-path model `llama3.2:3b`; launcher boot-check + `INTERVIEW_VOICE_LLM_BASE_URL=http://127.0.0.1:11434/v1`, `INTERVIEW_VOICE_LLM_MODEL=llama3.2:3b` export | `start_services.ps1` | `ollama list` shows it; warn with pull hint otherwise |

### Phase B — Agent worker (the core wiring, unblocks everything)

| ID | Task | Files | Acceptance |
|---|---|---|---|
| B1 | `LiveKitCandidate`: asyncio.Queue-backed candidate; utterance-end STT finals are pushed per question id; `answer()` awaits next transcript (mirrors `QueueCandidate` in `web/streamlit_app.py:37-52`) | `interviewer/voice/livekit.py` (new) | unit test: push transcript → `answer()` returns it with `stt_ms` |
| B2 | `AudioSink` protocol (`play(bytes)` async) + brain wiring: replace the dropped `audio` list in `_speak` with `await sink.play(chunk)` per synthesized sentence (keep `tts=None`/stub path working for text mode) | `interviewer/voice/protocols.py`, `interviewer/brain.py` | unit test: RecordingSink receives every sentence in order; existing tests stay green |
| B3 | Audio format conversion: `to_livekit_frames()` — Cartesia f32le@24k → s16le@48k; Piper wav@22.05k → s16le@48k; (ElevenLabs mp3 → decode, needs `av`/`pydub` — do only if D3 picks ElevenLabs) | `interviewer/voice/audio_format.py` (new) | unit tests on known-byte fixtures; 48 kHz s16le frames out |
| B4 | Implement `InterviewerAgent.on_enter`: `ctx.connect()`; wire VAD + STT plugin (silero VAD, turn detection); stream interviewer audio via `ctx.room`; push utterance finals into `LiveKitCandidate`; build per-room brain with `build_voice_interviewer`; barge-in on user speech during interviewer turn → `interviewer.interrupt()`; persist summary to session store | `interviewer/voice/agent.py` | worker joins a real room, greets, answers, scores (manual + live test) |
| B5 | Worker entry point `python -m interviewer.voice.worker`: LiveKit `Worker` + job executor per room, env-driven config, agent registration | `interviewer/voice/worker.py` (new) | process starts, registers with :7880, dispatches on room creation |

### Phase C — Voice engines completion

| ID | Task | Files | Acceptance |
|---|---|---|---|
| C1 | **MANDATORY (user choice)** — wire `KokoroTTS` (kokoro python package + phonemizer; needs `espeak-ng` binary on Windows; pick a high-quality preset; emits wav 24 kHz) into `resolve_tts` (replaces the raise at `tts.py:114-116`); make it the worker's default TTS | `interviewer/voice/tts.py`, `pyproject.toml` | resolve + synthesize smoke test on this machine; policy test updated |
| C2 | Piper on Windows (optional fallback only — skip unless Kokoro fails): document (or auto-download in launcher) release zip + `en_US-<voice>` model; `INTERVIEW_PIPER_MODEL` path | `start_services.ps1`, docs | `piper --model ... "hi"` produces wav |
| C3 | Prod-unsafe defaults: `stt_provider` default stays `stub` but the **worker** fails fast (clear error) unless a real STT/TTS pair resolves; worker defaults: `INTERVIEW_STT_PROVIDER=faster-whisper`, `INTERVIEW_TTS_PROVIDER=kokoro` | `interviewer/voice/worker.py`, `interviewer/config.py` | wrong/missing config → startup error naming the env var, not silent stubs |
| C5 | **faster-whisper provisioning (the STT path)** — `INTERVIEW_WHISPER_MODEL=base` (upgrade to `small` if CPU budget allows), verify lazy CTranslate2 load works on this machine (RTX 5060 Ti: pin `INTERVIEW_WHISPER_DEVICE=cpu` if CUDA/cuDNN libs are missing; model downloads on first use) | `interviewer/voice/stt.py`, docs | transcription smoke test on a recorded utterance; device choice documented |
| ~~C4~~ | ~~Deepgram websocket streaming~~ — **DROPPED** (no Deepgram key; faster-whisper is the STT path) | — | — |

### Phase D — Browser UI

| ID | Task | Files | Acceptance |
|---|---|---|---|
| D1 | Rewrite `web/index.html`: domain picker + start button → `POST /voice/token` → join room; live transcript panel (interviewer/candidate turns); status (state machine); final scores + voice budget bar; hang-up; mic mute. No `prompt()` | `web/index.html` | full interview runs from the page with no console intervention |
| D2 | Serve `web/` from FastAPI (`StaticFiles` mount, `/` route) so the UI and token endpoint share an origin; keep `-WithWeb` :8080 as an alternative | `interviewer/server.py`, `start_services.ps1` | UI loads at `http://127.0.0.1:8010/` |

### Phase E — Latency gate & tests

| ID | Task | Files | Acceptance |
|---|---|---|---|
| E1 | Unit tests: token endpoint, `LiveKitCandidate`, `AudioSink` wiring, `audio_format` conversions, kokoro/piper resolution, worker fail-fast | `tests/test_voice.py` + new files | all green in `pytest tests/` |
| E2 | Live E2E gate: with RAG :8031 + Ollama voice LLM + real LiveKit :7880 + real engines (faster-whisper STT, Kokoro TTS), run the five-stage budget check and assert `voice_budget_bar` ✓ (< 1500 ms) | `tests/test_live_phase3.py` (extend) | gate passes with **real** engine latencies, not fakes |
| E3 | Barge-in live check: play TTS, speak over it, assert the stream stops at the sentence boundary and FSM reaches LISTEN | live test | manual + automated where feasible |

### Phase F — Launcher, docs, packaging

| ID | Task | Files | Acceptance |
|---|---|---|---|
| F1 | Launcher: `-WithVoice` switch — steps: LiveKit server (:7880), agent worker process (venv python, env incl. `INTERVIEW_VOICE_*`, TTS/STT creds), readiness probes, logs (`%TEMP%`), summary + process guard; kill-stale covers 7880 + worker | `start_services.ps1` | one command boots the full voice stack |
| F2 | README "voice runbook" (the section `agent.py` references): requirements, env table (incl. `LIVEKIT_*`, TTS/STT keys), start commands, latency expectations, troubleshooting | `README.md` | runbook lets a fresh machine run voice |
| F3 | Update `docs/STATUS.md` (P9 → done/tracking), remove stale "left running" notes | `docs/STATUS.md` | status reflects reality |

### Task dependency order

```
A1 → A2 → A3 → A4 → A5          (infra + config)
A1,B2 → B1 → B4 → B5             (worker: sink before agent)
B4 → C1,C3,C5                     (engines complete the worker; C1 Kokoro mandatory)
A2 → D1 → D2                      (UI after token endpoint)
B5,D1 → E1 → E2 → E3             (tests after worker + UI)
A4,B5,C1 → F1 → F2 → F3          (launcher + docs last)
```

---

## 7. Definition of done (fully runnable)

1. `.\start_services.ps1 -WithVoice` (from the project root) boots RAG :8031,
   backend :8010, LiveKit :7880, and the agent worker — all alive at exit.
2. Browser at `http://127.0.0.1:8010/` → pick domain → Start → the interviewer
   **greets in a natural human voice** (no prompts, no console).
3. User speaks an answer → STT → RAG + voice LLM → spoken follow-up;
   `voice_budget_bar` shows ✓ under 1500 ms on real engines.
4. Barge-in works; wrap plays; scores + cache hit rate render in the UI.
5. `pytest tests/` green; `pytest tests/ -m live` green including the
   real-engine Phase-3 gate.

**Blocker list:** all decisions resolved (see §5). One implementation-time
detail remains: livekit-server via **docker** (Docker Desktop is installed on
this machine) or a native **Windows binary** — pick whichever probes clean at
implementation time; docker is the default.
