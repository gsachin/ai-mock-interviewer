# DONE & PENDING — ai-mock-interviewer (voice phase)

**Date:** 2026-09-02 · **Status:** Phase 3 voice interview **implemented + verified end-to-end**
**Read also:** `docs/STATUS.md` (launch + numbers) · `docs/PLAN_VOICE_PHASE3_BROWSER.md` (original gap analysis, now superseded)

---

## 1. ✅ DONE — implemented and verified

### Text interview (Phases 1–2, previously verified)
- Scripted interview over the enterprise-rag-core MCP service; LLM interviewer
  (qwen2.5:14b via Ollama) with judge scoring, follow-ups, session persistence.
- One-shot launcher `start_services.ps1` (RAG :8031, backend :8010, Streamlit :8501);
  Streamlit chat UI.

### Voice interview in the browser (Phase 3) — NEW 2026-09-02
Full spoken pipeline, **verified with 3 complete end-to-end interviews** on the
launcher-started stack (each `state=wrap`, 2 questions scored, interviewer
audio received, summary delivered):

| Component | Implementation | Verified |
|---|---|---|
| Browser voice UI (domain picker → start → speak; live transcript + scores; served by FastAPI, no CORS) | `web/index.html` | ✅ 3× E2E |
| Token endpoint `POST /voice/token` (LiveKit JWT, room `interview-<domain>-<sid>`, agent dispatch) | `interviewer/server.py` | ✅ |
| Agent worker: room audio → silero VAD → STT → `LLMInterviewer` → sentence TTS → room playback; barge-in on speech | `interviewer/voice/agent.py` | ✅ |
| Worker entry point `python -m interviewer.voice.worker` (fail-fast real engines, always-accept, refusal logging) | `interviewer/voice/worker.py` | ✅ |
| Event-driven candidate (queue, stale-interjection clear) + LiveKit audio sink (20 ms frames, interrupt) | `interviewer/voice/livekit.py` | ✅ |
| Audio format normalization to 48 kHz mono s16le (f32le / wav / mp3) | `interviewer/voice/audio_format.py` | ✅ unit |
| **Kokoro TTS** wired (self-hosted ONNX, ~110 MB auto-download, high-quality preset `af_heart`) | `interviewer/voice/tts.py` | ✅ synth smoke |
| **faster-whisper STT** default; CUDA→CPU int8 auto-fallback (this machine lacks cuDNN/cuBLAS) | `interviewer/voice/stt.py` | ✅ transcribe smoke |
| `AudioSink` protocol + staged UI events (`stage: greeting/question/followup/wrap`) in the brain | `interviewer/brain.py` | ✅ unit |
| LiveKit infra: `livekit-server --dev` :7880 (binary in `.tools/`, git-ignored); `llama3.2:3b` pulled for the hot path | launcher | ✅ |
| Launcher `-WithVoice` (livekit :7880 + worker, kill-stale incl. worker, readiness probes, logs, summary) | `start_services.ps1` | ✅ boot |
| No-mic E2E harness (scripted Kokoro answers into the room) | `scripts/e2e_voice_client.py` | ✅ 3× pass |
| Unit tests (19 voice-pipeline tests incl. token endpoint, sink, candidate, formats, resolutions) | `tests/test_voice_pipeline.py` | ✅ 50/50 suite |

### Machine-specific problems solved
- **Windows Smart App Control** blocks PyAV ≥ 13 unsigned DLLs → `[voice]` pins
  `av==12.3.0` (verified importable).
- **cuDNN/cuBLAS missing** for faster-whisper GPU → automatic CPU int8 fallback.
- **livekit-agents 1.7 API drift** handled: `push_frame` is fire-and-forget
  (VAD events come from iterating the stream); `VADEvent.frames` carries the
  speech buffer; `run_app` accepts `AgentServer`; legacy CLI needs a
  subcommand; CPU-load gate must be `inf` (a busy dev machine otherwise marks
  the worker unavailable).

---

## 2. ⏳ PENDING — for future work (prioritized)

### P1. Real-engine latency < 1.5 s round-trip  *(biggest quality gap)*
Measured on all-CPU engines: STT ~1.0 s, RAG ~0.2–0.36 s, LLM first token
~0.28–0.8 s, **TTS first audio ~3.6 s** → total ~6.5–8.4 s vs the 1.5 s gate
(`voice_budget_bar` reports it per interview). Levers:
- GPU STT on the RTX 5060 Ti (install cuDNN/cuBLAS 12) or `tiny.en` model.
- Kokoro int8 model + shorter first sentences; or cloud TTS (Cartesia key —
  the engine is already implemented in `tts.py`).
- Deepgram STT is implemented but untested live (needs a key).

### P2. Worker watchdog for the dev server idle-drop
`livekit-server --dev` drops a worker that idles ~20 s and it does not
reliably re-register → start the interview promptly after `-WithVoice`;
re-run the launcher between sessions. Future: a supervisor that restarts the
worker when a dispatch probe fails, or a production LiveKit server.

### P3. Redis semantic cache → rubric cache-hit gate
Docker/redis-stack was down → `cache_hit_rate` measured 0.0. Start Docker
(+ `enterprise-rag-core`'s redis-stack container) to restore the Phase-1
cache-gate metric (target 1.0).

### P4. Voice session persistence to the management plane
Worker summaries currently go to the browser only; `/sessions/{id}` registry
is not populated for voice rooms. Future: worker → backend persistence
(shared session store / summary POST) so interviews are reviewable after the
call ends.

### P5. Production hardening
- LiveKit Cloud or `livekit-server` with real keys (not `--dev`); HTTPS/WSS.
- OIDC (`RAG_MCP_TOKEN`) end-to-end with voice.
- The UI's mic/speaker flows on a real headset + browser matrix.

### P6. Quality loop
- Evaluation harness for voice prompt A/B (pre-Phase-3 plan item).
- Whisper mis-transcriptions measured (e.g. "Redis" → "Riddies"): log
  per-utterance confidence; tune VAD thresholds/endpointing.
- Kokoro voice preset tuning (`INTERVIEW_KOKORO_VOICE`) per interviewer tone.

### P7. Repo housekeeping
- Commit decision for the big uncommitted set (ERC folder, launcher, voice
  files). Suggested `.gitignore` additions already applied: `.tools/`,
  `*.wav`, `chroma_data/`, `.tunnel_*`.
- `docs/CLAUDE.md`, `docs/READMEbkp.md`, `PLAN_start_services.md` are stale
  backups — fold or remove.

---

## 3. How to reproduce / verify today

```powershell
cd D:\project\ai-mock-interviewer          # project root, not enterprise-rag-core
.\start_services.ps1 -WithVoice            # boots RAG :8031 + backend :8010 + LiveKit :7880 + worker
# 1) Browser:  http://127.0.0.1:8010/  → domain → Start → speak
# 2) No-mic:   .venv\Scripts\python.exe -u scripts/e2e_voice_client.py --domain system-design --answers 5
# 3) Tests:    .venv\Scripts\python.exe -m pytest tests/ -m "not live"
```
