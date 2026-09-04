# STATUS — ai-mock-interviewer

**Last updated:** 2026-09-02 (Phase 1–3 launch + numbers) · **Phase 4:** see `docs/STATUS_PHASE4.md` (dynamic skill registration, 2026-09-04) · **Voice plan:** `PLAN_VOICE_PHASE3_BROWSER.md` (superseded — implemented)

---

## 1. What is running (all verified end-to-end)

| Phase | Deliverable | Status |
|---|---|---|
| 1 | Scripted interview over MCP (`interviewer/interview.py`) | ✅ |
| 2 | LLM interviewer, text mode (`interviewer/brain.py`, Streamlit UI) | ✅ |
| 3 | **Voice interview in the browser** — LiveKit room → VAD (silero) → STT (faster-whisper) → brain (RAG + llama3.2:3b) → Kokoro TTS → playback, barge-in, live transcript + scores in the page | ✅ **verified 2026-09-02** |

Phase 3 verified by `scripts/e2e_voice_client.py` (no-mic scripted candidate):
3 consecutive full interviews on the launcher-started stack, each ending
`state=wrap`, 2 questions scored (real LLM judging + RAG retrieval),
interviewer audio received, summary delivered to the client.

## 2. One-shot launch

```powershell
cd D:\project\ai-mock-interviewer      # MUST be project root, not enterprise-rag-core
.\start_services.ps1 -WithVoice
# Voice UI (spoken interview): http://127.0.0.1:8010/
# Management health:            http://127.0.0.1:8010/health
# RAG MCP:                      http://127.0.0.1:8031/mcp
# LiveKit (dev):                http://127.0.0.1:7880
```

Text mode (no mic): `.\start_services.ps1 -WithStreamlit` → :8501.

## 3. Voice architecture (what was built)

| Piece | Location |
|---|---|
| `/voice/token` — LiveKit JWT + room + agent dispatch | `interviewer/server.py` |
| Agent worker `run_agent` (VAD→STT→brain→TTS, barge-in, data events) | `interviewer/voice/agent.py` |
| Worker entry point (fail-fast engines, always-accept) | `interviewer/voice/worker.py` |
| Event-driven candidate + LiveKit audio sink (48 kHz s16le frames) | `interviewer/voice/livekit.py` |
| Audio format normalization (f32le/wav/mp3 → 48 k s16le) | `interviewer/voice/audio_format.py` |
| Kokoro TTS (self-hosted, ONNX, no espeak) — default TTS | `interviewer/voice/tts.py` |
| faster-whisper STT, CUDA→CPU fallback — default STT | `interviewer/voice/stt.py` |
| AudioSink protocol + staged UI events in the brain | `interviewer/brain.py`, `interviewer/voice/protocols.py` |
| Browser voice UI (served by FastAPI, no CORS) | `web/index.html` |
| E2E scripted candidate (no mic) | `scripts/e2e_voice_client.py` |
| Launcher `-WithVoice` (livekit :7880 + worker, kill-stale, logs, summary) | `start_services.ps1` |

Env (exported by `-WithVoice`): `LIVEKIT_URL/API_KEY/API_SECRET` (devkey/secret),
`INTERVIEW_STT_PROVIDER=faster-whisper`, `INTERVIEW_TTS_PROVIDER=kokoro`,
`INTERVIEW_VOICE_LLM_BASE_URL=http://127.0.0.1:11434/v1`,
`INTERVIEW_VOICE_LLM_MODEL=llama3.2:3b`.

## 4. Verified numbers (real engines, all CPU, 2026-09-02)

| Metric | Measured | Stage budget |
|---|---|---|
| LLM first token (llama3.2:3b) | ~280–800 ms | 500 ms |
| RAG | ~200–360 ms | 150 ms |
| STT final (faster-whisper `base`, CPU int8) | ~1.0–1.1 s | 300 ms |
| TTS first audio (Kokoro, CPU) | ~3.6 s | 200 ms |
| **Voice round-trip (utterance-end → first audio)** | **~6.5–8.4 s** | **1500 ms** |

The five-stage bar is measured on real engines (`voice_budget_bar` in every
summary). Meeting < 1.5 s needs GPU or cloud STT/TTS — the architecture and
budget plumbing are in place; engines are the lever.

## 5. Known dev-mode caveats

1. **livekit-server `--dev` drops a worker that idles ~20 s** and the worker
   does not reliably re-register. Start the interview promptly after
   `-WithVoice` boots; for back-to-back sessions re-run the launcher.
2. **Smart App Control** (this machine, `VerifiedAndReputablePolicyState=1`)
   blocks PyAV ≥ 13's unsigned DLLs → `[voice]` pins `av==12.3.0`.
3. **No Redis semantic cache this session** (Docker down) → rubric
   `cache_hit_rate` measured 0.0. Start Docker + redis-stack for cache hits.
4. faster-whisper CUDA load failed on this machine (missing cuDNN/cuBLAS) →
   automatic CPU int8 fallback is implemented and verified.

## 6. Suggested next work

- Real-engine latency: GPU whisper (RTX 5060 Ti needs cuDNN) or `tiny.en`,
  Kokoro int8/`small` first-sentence chunking, or cloud STT/TTS.
- Persist worker session summaries to the management-plane registry
  (`/sessions/{id}` is currently empty for voice rooms).
- Evaluation harness for voice prompt A/B (the pre-Phase-3 quality item).
- Production LiveKit (Cloud or `--keys` mode) + OIDC on the RAG side.
