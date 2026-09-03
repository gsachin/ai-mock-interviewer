# DONE & PENDING — ai-mock-interviewer (voice phase)

**Date:** 2026-09-03 (evening) · **Status:** RCA fixes (T1–T8) **done**; **manual
answer toggle + Retake/Next review gate implemented + verified**; manual
browser-with-mic check still **pending** (owner: user, after app testing)
**Read also:** `docs/STATUS.md` (launch + numbers) · `docs/RCA_VOICE_BROWSER_INTERVIEW.md`
(fix plan — all T tasks now DONE per §8 status block)

---

## 1. ✅ DONE — implemented and verified

### Foundations (Phases 1–3, verified previously)
- Scripted + LLM text interviews over the enterprise-rag-core MCP service (Phases 1–2).
- Phase 3 voice pipeline verified end-to-end with the *scripted Python client*
  (worker, VAD/STT/TTS, Kokoro + faster-whisper CPU, LiveKit `--dev`, launcher `-WithVoice`).

### RCA browser fixes — NEW 2026-09-03 (the reported stall/no-transcript/no-rating/End-call bugs)
Implemented per `docs/RCA_VOICE_BROWSER_INTERVIEW.md` §4; **64 unit tests green**
(was 50) + **live E2E PASS** (3 questions scored, per-question `score` events,
`candidate_heard` echoes, `ended` received, `state=wrap`, ~2:45 wall):

| # | Task | Implementation |
|---|---|---|
| T1 | 3 questions/session | `config.py`: `INTERVIEW_MAX_QUESTIONS` (default **3**) → `voice/interviewer.py` → `brain.py`; token response carries `max_questions` |
| T2 | No-hang answers | bounded `answer(timeout_s)` (`voice/livekit.py`), `INTERVIEW_ANSWER_TIMEOUT_S` (60 s), one spoken re-prompt then scored-as-unanswered (`brain._listen`); candidates accept `timeout_s` |
| T3 | STT-first echo | worker emits `state: transcribing` + `candidate_heard {text}` the instant STT returns; brain candidate-role turns filtered (`is_page_event`) so no transcript duplicates |
| T4 | Phase/label protocol | `state {phase,label}` on every transition, per-question `score` events, compact `summary` packet (`{state, scores, stats}`, no full transcript) |
| T5 | Page UX | `web/index.html`: loader never absent (mic-pulse listening / spinner labels), tolerant `onData`, progressive scoreboard, explicit mic AEC constraints, End call from connect onward, end-states, "Question n of Y" + elapsed chips |
| T6 | Echo gate | pure `EchoGate` (`voice/livekit.py`) — no self-barge-in during playback, echo-tail utterances dropped; sink exposes `playing`/`last_stop_ts` |
| T7 | `ended` event + end-state | worker publishes `ended {reason}` on job end; page renders end-state + auto-disconnect for a fresh Start; `INTERVIEW_JUDGE_MODEL` override config |
| T8 | Regression tests | +14 tests (protocol ordering, timeout path w/ real `LiveKitCandidate`, EchoGate decisions, sink state, factory wiring, config envs) — 64 passed |

### Manual answer toggle + Retake/Next review gate — NEW 2026-09-03 (evening)
Answer capture is now **explicit, not voice-activity dependent**: the page's
single toggle drives the flow, and each scored question ends at a **review
gate** where the candidate chooses Retake or Next (product decisions from the
user, 2026-09-03):

| Piece | Behavior | Where |
|---|---|---|
| Start/Finish toggle | Idle = Start interview. Interviewer speaking/processing → **disabled**. Question done (`listening`) → **▶ Start answer** → click → **■ Finish answer** (elapsed timer) → audio since Start is transcribed. After the last question's wrap → **■ Finish session** → back to Start. A small "End call ✕" link remains as an escape | `web/index.html` |
| Per-question feedback | The judge's single evaluation call also returns **Verdict: correct/partial/incorrect** + a short **Model answer**; both ride on the `score` event and the review gate shows a ✅/◐/❌ badge, the gap, and the correct answer (fallback verdict derived from scores when the judge omits it) | `interviewer/scoring.py`, `prompts.py`, `brain.py`, `web/index.html` |
| Transcription no-hang (RCA 2026-09-03) | Multi-minute "Transcribing…" after a short answer: the manual arm captured thinking-time silence and whisper decoded it all (measured 35 s CPU for 150 s quiet; worse under TTS/judge contention). Fixes: energy-trim the armed buffer to the speech region before STT (`voice/trim.py`), clamp engine input to 60 s + harden the CUDA→CPU fallback (`stt.py`), pin `INTERVIEW_WHISPER_DEVICE=cpu` in the launcher (this box lacks cuBLAS/cuDNN; per-room CUDA probing wasted seconds and could stall), and a 20 s page watchdog so a slow decode never looks hung | `interviewer/voice/trim.py`, `stt.py`, `agent.py`, `start_services.ps1`, `web/index.html` |
| 60 s no-answer | Timeout → interviewer re-prompts once, toggle returns to Start (2nd 60 s chance); second silence → scored & moves on | `brain._listen` (unchanged semantics) |
| Retake / Next | After every scored question the brain pauses at a **review gate**: Retake re-asks the same question and **replaces** its score row; Next advances (90 s without a choice auto-advances). Text/headless runs pass no decider → old automated flow, unchanged | `brain.py` (decider protocol), `voice/livekit.py` (`LiveKitReviewer`) |
| Manual capture | Worker buffers the candidate's mic only between `answer_start` / `answer_finish` control messages (data topic `control`); STT-first echo + notice events preserved. VAD auto-endpoint path removed; re-prompt answers are no longer drained as junk (F1 `drop_before_ts`) | `interviewer/voice/agent.py`, `voice/livekit.py` |

### Verified 2026-09-03
- `.venv\Scripts\python.exe -m pytest tests/ -m "not live"` → **71 passed, 5 deselected**.
- No-mic E2E (`scripts/e2e_voice_client.py --answers 5`, now driving the manual
  protocol) on the launcher stack → **PASSED**: state=wrap, questions=3, 3
  progressive scores, 6 `heard me` echoes, review-gate `next` advanced each
  question, `ended` received, ~2:07 wall.
- Page served (HTTP 200) and inline JS passes `node --check`.

---

## 2. ⏳ PENDING — for future work (prioritized)

### P-A. Manual browser verification (NEXT — owner: user, after app testing)
Browser checklist (RCA §5 + the new toggle flow): the toggle is **disabled
while the interviewer speaks** and becomes **▶ Start answer** when it is your
turn; recording shows **■ Finish answer** with a count-up; your words appear
as text ~1 s after Finish; the scoreboard row appears per question with
**↻ Retake answer / Next question ▶**; Retake re-asks and replaces the score;
Next moves on; after the summary the toggle reads **■ Finish session**; 60 s
of silence triggers one re-prompt and the toggle re-enables; End call works
from any state. Stack state at last boot: running (see §3 to restart).

### P-B. RCA §7 improvement suggestions (architect review — all still pending)
| # | Suggestion | Status |
|---|---|---|
| 7.1 | Preload STT/TTS models before the greeting (first utterance pays the model load) | ❌ pending |
| 7.2 | Breadth-first question sampling across bank sections (today: sequential top-of-bank → same 3 questions cluster / repeat) | ❌ pending |
| 7.3 | Adaptive judge (switch to the warm hot-path LLM after 2 slow judge rounds) | ❌ pending |
| 7.4 | "Question n of Y" + elapsed chip in the page header | ✅ done (in T5) |
| 7.5 | Let the student correct a mis-transcription once per answer | ❌ pending |
| 7.6 | Persist each scored question to the management plane as it happens | ❌ pending (= P4) |
| 7.7 | Guard the follow-up cadence (cap drilling on strong answers) | ❌ pending |
| 7.8 | "Retry interview" / "different domain" one-click affordance | ◑ partial — Start-again works after `ended`; fresh per-session question selection awaits 7.2 |
| 7.9 | Instrument the abandonment funnel (worker log milestones) | ❌ pending |

### P1. Real-engine latency < 1.5 s round-trip  *(biggest quality gap)*
Measured all-CPU: STT ~1.0 s, RAG ~0.2–0.36 s, LLM ~0.28–0.8 s, **TTS ~3.6 s** →
~6.5–8.4 s vs the 1.5 s gate (E2E bar still FAILs; `voice_budget_bar` reports it
per interview). Levers, all machine/key-dependent:
- GPU STT on the RTX 5060 Ti (install cuDNN/cuBLAS 12) or `tiny.en` model.
- Kokoro int8 + shorter first sentences; cloud TTS (Cartesia key — engine already
  implemented in `tts.py`).
- Deepgram STT implemented, untested live (needs a key).
- **New lever from T7:** `INTERVIEW_JUDGE_MODEL` (a faster judge shrinks the long
  wait between answer and next question).

### P2. Worker watchdog for the dev-server idle-drop
`livekit-server --dev` drops a worker that idles ~20 s and it does not reliably
re-register → start the interview promptly after `-WithVoice`; re-run the
launcher between sessions (the page shows a hint when no interviewer joins
within ~10 s). Future: supervisor that restarts the worker (P5 / production
LiveKit server also removes the caveat).

### P3. Redis semantic cache → rubric cache-hit gate
Docker/redis-stack was down → `cache_hit_rate` measured 0.0. Start Docker
(+ `enterprise-rag-core`'s redis-stack container) to restore the Phase-1
cache-gate metric (target 1.0).

### P4. Voice session persistence to the management plane (= 7.6)
Worker summaries currently reach the browser only; `/sessions/{id}` registry is
not populated with voice turns/scores. Future: worker → backend score/summary
POST so interviews are reviewable after the call ends (incl. mid-session crash).

### P5. Production hardening
- LiveKit Cloud or `livekit-server` with real keys (not `--dev`); HTTPS/WSS.
- OIDC (`RAG_MCP_TOKEN`) is wired through the voice worker's `RagClient` —
  needs an end-to-end run against a real IdP.
- UI mic/speaker flows on a real headset + browser matrix (see P-A).

### P6. Quality loop
- Evaluation harness for voice prompt A/B (pre-Phase-3 plan item).
- Whisper mis-transcriptions ("Redis" → "Riddies" measured): log per-utterance
  confidence; tune VAD thresholds/endpointing. (Stops being cosmetic once 7.5 lands.)
- Kokoro voice preset tuning (`INTERVIEW_KOKORO_VOICE`) per interviewer tone.

### P7. Repo housekeeping
- **Commit decision still open** (owner: user): the large uncommitted set —
  `enterprise-rag-core/`, launcher, voice files, and the 2026-09-03 T1–T8 fixes.
  Suggested `.gitignore` additions already applied: `.tools/`, `*.wav`,
  `chroma_data/`, `.tunnel_*`.
- Stale backups to fold or remove: `docs/READMEbkp.md`, `docs/PLAN_start_services.md`,
  `docs/PLAN_VOICE_PHASE3_BROWSER.md` (superseded by the RCA + this file);
  root `CLAUDE.md` deleted (a doc-only change is staged in git).

---

## 3. How to reproduce / verify today

```powershell
cd D:\project\ai-mock-interviewer          # project root, not enterprise-rag-core
.\start_services.ps1 -WithVoice            # boots RAG :8031 + backend :8010 + LiveKit :7880 + worker
# 1) Browser:  http://127.0.0.1:8010/  → domain → Start → speak
#    (manual checklist: docs/RCA_VOICE_BROWSER_INTERVIEW.md §5 — P-A above)
# 2) No-mic:   .venv\Scripts\python.exe -u scripts/e2e_voice_client.py --domain system-design --answers 6
# 3) Tests:    .venv\Scripts\python.exe -m pytest tests/ -m "not live"
```
Stop the stack: `taskkill /F /IM python.exe /FI "PID NE <launcher-pid>"` — the
launcher prints the exact command at boot (or Ctrl+C in its console).
