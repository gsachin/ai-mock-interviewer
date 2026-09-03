# RCA — Browser voice interview stalls after Q1; no transcripts/ratings; End call inert

**Author:** Claude Code (AI architect) · **Date:** 2026-09-02
**Scope:** root-cause analysis of the reported browser behaviour + a concrete
design to make the interview interactive (3 questions per session, visible transcripts,
live ratings, working End call). All claims cite code (`file:line`) or
verified runs.

---

## ⏱ Implementation status (2026-09-03) — fix plan T1–T8: DONE

The §4 fix plan below has been fully implemented and verified. The analysis and
design remain the reference for *why*; `docs/DONE_AND_PENDING.md` tracks overall
project state.

| Task | Status | Where |
|---|---|---|
| T1 — 3 questions (`INTERVIEW_MAX_QUESTIONS`, default 3) | ✅ | `interviewer/config.py`, `voice/interviewer.py`, `brain.py`, `interviewer/server.py` (token `max_questions`) |
| T2 — bounded `answer()` + re-prompt + unanswered fallback | ✅ | `interviewer/voice/livekit.py`, `brain.py` (`_listen`), `interviewer/prompts.py` |
| T3 — STT-first echo (`candidate_heard` + `state: transcribing`) | ✅ | `interviewer/voice/agent.py` |
| T4 — `state {phase,label}` + per-question `score` + compact `summary` | ✅ | `interviewer/brain.py` |
| T5 — page UX (loader, tolerant `onData`, AEC mic, End always) | ✅ | `web/index.html` (also 7.4 chips) |
| T6 — echo gate in the VAD consumer | ✅ | `EchoGate` in `interviewer/voice/livekit.py`, wired in `agent.py` |
| T7 — `ended` event + UI end-state + judge-model config | ✅ | `interviewer/voice/agent.py`, `web/index.html`, `config.py` |
| T8 — regression tests + verification | ✅ | 64 unit tests (was 50); no-mic E2E PASS — state=wrap, **3 questions scored**, progressive `score` events, `candidate_heard` echoes, `ended` received (~2:45 wall on the all-CPU stack) |

**Verification evidence (2026-09-03):** `.venv\Scripts\python.exe -m pytest tests/ -m "not live"` → 64 passed;
`scripts/e2e_voice_client.py --answers 6` against the launcher stack → E2E PASSED;
page served HTTP 200, inline JS passes `node --check`.

**Still outstanding:**
1. **Manual browser + mic check** (§5 checklist, owner: the user — see P-A in
   DONE_AND_PENDING.md): transcript echo ~1 s after speaking, loader visible
   during every backend phase, progressive scoreboard, End from every state.
2. §7 improvement suggestions 7.1–7.3 / 7.5–7.7 / 7.9 and P1–P7 of
   DONE_AND_PENDING.md (latency, watchdog, Redis gate, persistence, hardening,
   quality loop, repo housekeeping) — not started.

**Decision-log deltas (from §6):** none — defaults implemented as decided
(3 questions, qwen2.5:14b judge with `INTERVIEW_JUDGE_MODEL` override added,
compact summary transport, page-side AEC + agent-side gate).

---

## 1. Symptom map (what the user reported → what it means)

| # | Reported symptom | Meaning in the system |
|---|---|---|
| S1 | "Not able to see the interviewee voice-converted text" | Candidate STT transcripts never render in the transcript panel |
| S2 | "Neither any rating" | Scores exist only inside the final `summary` event, rendered at the very end — never reached (or the packet was dropped) |
| S3 | "Only one question is asked, then nothing happens" | The interview advances exactly once, then no further interviewer speech, ever |
| S4 | "End call button is also not working" | Room disconnect from the page is not reachable/effective in some states |
| S5 | "Only 2 questions are asked" | Current voice path is hard-capped at **2** questions (`brain.py:78`), never overridden by the voice worker. **Product decision (2026-09-02): 3 questions per session** |

**Key verification fact:** the same pipeline passed 3× end-to-end with the
*scripted Python client* (`scripts/e2e_voice_client.py`): `state=wrap`,
2 questions scored, transcripts + summary received. So the core loop works.
Every browser-specific failure must therefore come from (a) the human audio
path, (b) the page's JS event handling, or (c) configuration the E2E never
exercises.

---

## 2. Root causes (ranked)

### R1 — PERMANENT HANG: the brain waits forever for a candidate answer it never receives
`LiveKitCandidate.answer()` blocks on an unbounded queue read:

```python
# interviewer/voice/livekit.py:46
text, stt_ms = await self._queue.get()      # no timeout, no escape
```

`LLMInterviewer.run()` (`brain.py:238-243`) calls `candidate.answer()` after
every spoken question and **only proceeds when a transcript arrives**. If the
candidate's mic audio never yields an STT final, the interview stops
**exactly after the first question, indefinitely** — no second question, no
follow-up, no wrap, no summary → S3 and S2 both follow directly.

Why can the human's speech fail while the Python E2E never does?
- The E2E client publishes clean pre-recorded PCM (no speakers, no echo).
- A human browser publishes a **microphone in a room with speakers**. The
  agent's interviewer TTS plays out of the speakers and is picked up by the
  mic. Depending on echo-cancellation state this produces either (i) the
  agent's own voice being VAD-detected as "candidate speech" and transcribed
  as garbage answers, or (ii) the mic being AEC-muted/gated so the real
  speech never crosses the VAD threshold. Both are *browser-only* paths the
  E2E cannot reproduce — confirmed by the fact that the worker's log shows
  no `candidate said …` line in the user's session.
- Contributing: no AEC / noise-suppression is requested on the browser track
  (`web/index.html:136` `setMicrophoneEnabled(true)` uses SDK defaults), and
  there is no acoustic-echo guard on the agent side (no VAD gate while the
  interviewer is speaking; `agent.py` barge-ins on any START_OF_SPEECH).

**Fix (must-have):** bounded wait + recovery instead of an infinite one —
`answer()` times out (configurable, ~45-60 s); the brain then speaks
"I didn't catch that — could you repeat it?" once; a second timeout moves on
to the next question. Overall session idle timeout ends the job cleanly.

### R2 — Candidate text can never render early enough (and can be filtered out entirely)
- The *only* candidate-turn emission happens after the brain pops the answer
  (`brain.py:242`, `brain.py:284`) — so if R1 holds, the user **never sees
  their own transcript**, even though the audio was heard. There is no
  "heard you: …" echo at the moment STT produces text.
- The page drops every event whose topic is not exactly `"interview"`
  (`web/index.html:97` `if (topic !== "interview") return;`). If the
  livekit-client version/delivery shape differs (topic arg missing or empty
  on some data paths), **all** events are silently discarded — interviewer
  text included (audio still plays, so the interview *seems* to run). This
  alone explains S1/S2 in a session that otherwise worked.
- Symptom S1 is therefore a union of two bugs: no STT-first echo event, and
  a fragile strict-topic filter.

### R3 — Ratings exist only in one end-of-interview packet
`web/index.html:78-93` renders scores **only** from the single `summary`
event at wrap (`brain.py:344`). Consequences:
- Any stall (R1) or dropped/large packet ⇒ zero ratings even when questions
  were already scored (S2).
- The summary payload embeds all turns + all hops (`brain.py:312-338`);
  at 3 questions it is already several KB and only grows (worker's
  `publish_data` failure is swallowed — `agent.py:74-77`), making the final
  packet the *most* likely to be lost exactly when it matters.

### R4 — Question count is capped at 2 by design
`LLMInterviewer.__init__` defaults `max_questions: int = 2`
(`brain.py:78`); `build_voice_interviewer` (`interviewer/voice/interviewer.py`)
never overrides it; the worker never sets it. Bank questions are sliced at
`brain.py:225` `bank.questions[:self._max_questions]`. ⇒ The voice interview
asks **at most 2 questions** (each may carry one follow-up when the judge
asks — the E2E runs show 2 questions + 2 follow-ups + wrap). S5 is a real
design gap versus the product requirement (3 questions per session).

### R5 — Long, silent judge gaps read as "nothing is happening"
After an answer, the judge (`self._llm` = qwen2.5:14b on Ollama) runs
synchronously (`brain.py:193-198`); measured judge waits dominate the wall
clock (97 s for a 2-question E2E; the *candidate's real wait* per question is
tens of seconds). Between "your answer" and the next spoken turn there is **no
UI feedback at all** — no state events exist between turn emissions. Users
interpret 30-60 s of silence as a hang (S3 even when the flow is alive).

### R6 — End call is fragile and the session end-state is undefined
- The only handler is `web/index.html:147`
  `hangupBtn.addEventListener("click", () => { if (room) room.disconnect(); })`.
- If `setMicrophoneEnabled(true)` throws (no mic / permission), the catch
  branch (`index.html:141-143`) leaves the room **connected with the End
  button hidden** — the user can only reload (S4).
- When the interviewer's job ends (summary spoken, agent leaves), the page
  stays "Connected" with no end-state UI; nothing signals the interview is
  over until the user presses End — and if the worker was idled out by the
  dev server between sessions (documented caveat), a new Start never gets an
  interviewer, which compounds the "nothing happens" impression.
- No `disabled` guard exists if `room` was already null; no worker-side
  "goodbye/end" event tells the UI to switch to the ended state.

---

## 3. Interaction design — making the interview visibly interactive (3 questions per session)

### 3.0 Product requirements (from the user — non-negotiable)

1. **Spoken answers must appear as text on the page.** The moment the
   candidate finishes speaking, their voice is converted to text (STT) and
   that text is **displayed immediately** in the transcript panel — the
   student must always be able to see what the system heard them say.
2. **No silent background work — ever.** Whenever any backend process runs
   (STT transcription, RAG retrieval, LLM judging, TTS synthesis, scoring),
   the page must show a **loader/spinner + a descriptive processing label**
   (e.g. "Transcribing your answer…", "Evaluating your answer…"). The page
   must never sit idle-looking between turns; a student who sees a spinner
   knows the backend is working and **will not leave the interview**.
3. Together: *spoken answer → text on screen → spinner while the backend
   thinks → next question spoken*. This cadence is the definition of the
   interactive interview; every turn and every transition must be visible.

### 3.1 Turn protocol (typed events, worker → page over the room data channel)
Replace the two event types with a small stateful protocol so the page always
knows what the interviewer is doing — **every event either appends text or
sets a visible loader state**:

| Event | Payload | When | UI effect |
|---|---|---|---|
| `state` | `{phase, label}` — phase: `speaking\|listening\|transcribing\|evaluating\|scoring\|wrap\|ended` | every phase change | spinner + `label` text ("Listening — speak now…", "Transcribing your answer…", "Evaluating your answer…", "Scoring…") |
| `interviewer_turn` | `{text, stage}` | after each spoken interviewer turn | transcript |
| `candidate_heard` | `{text}` | **the moment STT returns** (before the brain consumes it) | candidate transcript echo + switch loader to "evaluating" — S1 fixed even if the brain later stalls |
| `score` | `{question_id, scores, followup}` | after each question's evaluation | growing scoreboard — S2 fixed progressively |
| `summary` | compact: `{scores, stats}` (no full turns) | at wrap | final summary; small packet cannot overflow |
| `ended` | `{reason}` | worker job ends / disconnect | page switches to ended state, enables Start |

**Processing-label mapping (requirement 3.0.2) — the page shows a spinner
with exactly these labels and never nothing:**

| Phase | Spinner label | Backend work covered |
|---|---|---|
| `listening` | "🎙 Listening — please answer…" (pulsing mic, not a spinner) | VAD open — waiting for speech |
| `transcribing` | "⏳ Transcribing your answer…" | faster-whisper STT of the utterance |
| `evaluating` | "⏳ Evaluating your answer…" | RAG rubric/follow-up retrieval + LLM judge (this is the long one, 10–60 s — the label is what keeps the student on the page) |
| `scoring` | "⏳ Recording your score…" | score ledger + FSM advance |
| `speaking` | (no loader — audio is playing; transcript of the spoken turn appears) | TTS streaming |
| `wrap` | "⏳ Preparing your feedback…" | wrap summary build |

Guarantee: **between any two spoken interviewer turns, at least one `state`
event with a non-empty label is shown**, so there is never an unexplained gap
longer than ~1 s.

Implementation touch points: brain `_emit` sites gain `phase`/`label` (map
from the hop stages already recorded at `brain.py:219-309`); the worker's VAD
consumer pushes `state: transcribing` when `stt.transcribe()` starts and
`candidate_heard` when it returns, then `state: evaluating`; a per-question
`score` event is emitted right where `s.scores.append(...)` happens
(`brain.py:290-297`); the `summary` event is trimmed to scores+stats.
Page `onData` no longer filters on topic — it tries to parse any payload and
logs unknowns (removes the R2 fragile filter; keep `topic` in the log for
diagnosis). The transcript panel and the loader live in a fixed layout (no
jump on arrival) so the reading position never moves while a spinner runs.

### 3.2 Three questions per session (product decision) — with honest turn-taking
- New env `INTERVIEW_MAX_QUESTIONS` (default **3**) threaded from the worker's
  `InterviewerConfig` → `build_voice_interviewer` → `LLMInterviewer`
  (`brain.py:78/225` reads it). Banks hold 16 questions per domain.
- **Why 3 is the right number here** (analysis): each question costs ~1–2 min
  of wall time on the current all-CPU stack (answer ≈ 20–40 s + judge ≈
  20–60 s + TTS ≈ 10–20 s). A 3-question session lands at ≈ 6–9 min — short
  enough that students complete it (the reported abandonment risk is driven
  by *silence*, not by length, once §3.0's loader exists). With follow-ups
  the student still answers 3–6 spoken turns. Going beyond 3 would push the
  session past ~10 min on today's engine latency; revisit when GPU/cloud
  engines bring the round-trip under the 1.5 s budget.
- **Selection quality matters more at 3:** sequential top-of-bank sampling
  may cluster three similar questions. Sample breadth-first across the
  bank's sections so a session covers three different topics (see
  Improvement §7.2).
- **Answer timeout + recovery (fixes R1):** per-answer wait capped by
  `INTERVIEW_ANSWER_TIMEOUT_S` (default 60). On timeout the interviewer
  speaks a short re-prompt (one per question); on a second timeout the
  question is scored as unanswered and the interview moves on — the FSM
  never deadlocks and always reaches `wrap`.
- **Echo discipline (fixes the browser-only audio failure):**
  - Page: request the mic with explicit `echoCancellation: true,
    noiseSuppression: true, autoGainControl: true`.
  - Agent: ignore barge-in/VAD events while the interviewer is speaking
    except a genuine overlap (gate START_OF_SPEECH until ~300 ms of
    sustained speech), and do not treat speech that ends within ~200 ms of
    the interviewer's own stop as a fresh utterance (echo tail).
  - Runbook: headset strongly recommended for demos; mic test hint in the
    page ("say something — you'll see it appear below" on first `listening`).
- **Latency perception (fixes R5):** the `state` events above render
  "Evaluating your answer…" during the judge wait; optionally make the judge
  model configurable (`INTERVIEW_JUDGE_MODEL`) so a faster model can be
  chosen; the hot-path LLM stays llama3.2:3b.

### 3.3 End call and session end-state (fixes S4)
- End button: always visible once connected (move its enable point to right
  after `connect()`, before the mic step); `room.disconnect()` +
  `room.localParticipant.setMicrophoneEnabled(false)`; guarded + idempotent.
- On `summary`/`ended`: page shows "Interview complete — End call to
  finish", scores board, and a fresh Start.
- Worker: on job end publish an `ended` event before leaving; page renders
  "Interviewer ended the session".
- Back-to-back sessions: keep the documented dev-server caveat visible
  (`index.html` shows a "Start may need a fresh stack" hint when a join gets
  no interviewer within ~10 s), and add the P2 worker watchdog so a dropped
  worker is restarted automatically.

---

## 4. Fix plan (tasks, ordered)

| # | Task | Files | Acceptance |
|---|---|---|---|
| T1 | `INTERVIEW_MAX_QUESTIONS` (default **3**) through config → worker → brain | `interviewer/config.py`, `voice/worker.py`, `voice/interviewer.py` | voice interview asks 3 bank questions (each may carry one follow-up) |
| T2 | Bounded `answer()` + re-prompt + unanswered-score fallback + session idle timeout | `voice/livekit.py`, `brain.py`, `prompts.py` | no hang: silence produces a re-prompt then `wrap` in < 3 min |
| T3 | **STT-first echo (3.0.1):** `candidate_heard` event from the VAD consumer the instant `stt.transcribe()` returns, plus `state: transcribing` while it runs | `voice/agent.py` | page shows the spoken answer as text within ~1 s of STT completing — even if the brain later stalls |
| T4 | **Phase/label events (3.0.2):** `state {phase,label}` on every transition + per-question `score` + compact `summary` | `brain.py`, `voice/agent.py` | every backend phase emits a label; scores appear per question; final packet ≪ 15 KB |
| T5 | **Page UX:** spinner + processing-label bar (loader never absent between turns), tolerant `onData`, progressive scoreboard, explicit mic constraints (echoCancellation/noiseSuppression), End always available + end-states | `web/index.html` | 3.0.1/3.0.2 hold visually: answer text appears, a loader with descriptive text is visible during every background phase, End works from every state |
| T6 | Echo gate in the VAD consumer (ignore self-echo windows) | `voice/agent.py` | speaking over the interviewer still barges in; interviewer's own TTS never becomes a "candidate answer" |
| T7 | `ended` event + UI end-state; optional judge-model config | `voice/agent.py`, `web/index.html`, `interviewer/config.py` | session end is explicit on both sides |
| T8 | Regression + verification: unit tests (timeout path, max-questions=3, phase/label events, per-question score events) + E2E with **echo-injected audio** + one manual browser run | `tests/test_voice_pipeline.py`, `scripts/e2e_voice_client.py` | 50+ unit tests green; E2E reaches `wrap` with 3 questions scored (5 prepared answers cover follow-ups); manual browser run shows: every spoken answer as text, a spinner+label during every backend phase, per-question ratings, End works |

## 5. Verification steps (instrumented)

1. Worker log tells you exactly where a session dies — watch these lines:
   - `candidate audio stream attached` — agent subscribed to the mic.
   - `candidate said (N ms): '…'` — STT produced text (absent ⇒ R1/R2 path).
   - `vad event: …` — add per-event logging in the consumer (T6).
   - `interview done: … voice_budget_bar=…` — wrap reached.
2. Browser checklist (student perspective — requirements 3.0.1/3.0.2):
   (a) speak an answer and confirm **your words appear as text** within ~1 s
   of finishing; (b) while the backend works, a **spinner + label** is always
   visible ("Transcribing…" → "Evaluating…") and never a dead page — watch
   the ~30–60 s judge window specifically; (c) the scoreboard grows after
   each question; (d) End call from mid-question, during evaluation, and
   after the summary — each must disconnect and enable a fresh Start.
3. Headless echo test: extend `e2e_voice_client.py` to play the interviewer's
   own synthesized question back into the mic track (loopback sim) — asserts
   the agent does **not** transcribe its own voice as the answer (T6), and
   asserts the transcript + phase/label events arrive for every turn (T3/T4).

---

## 6. Decision log

- **Q count:** default **3** via env (`INTERVIEW_MAX_QUESTIONS`); banks have
  16 questions per domain — no content work needed. Rationale: session stays
  ≈ 6–9 min on today's all-CPU latency; revisit upward when engines meet the
  < 1.5 s round-trip budget.
- **Judge latency:** keep qwen2.5:14b as default judge; UI feedback (T4) is
  the primary mitigation; judge model becomes configurable (T7) for later
  tuning.
- **Summary transport:** keep LiveKit data for `summary` but compact it;
  full-history persistence to `GET /sessions/{id}` stays on the roadmap
  (docs/DONE_AND_PENDING.md P4).
- **Echo handling:** page-side AEC constraints + agent-side gate (T6) are
  sufficient for the demo path; a headset remains the strong recommendation.

---

## 7. Improvement suggestions (architect review, 2026-09-02)

Analysed the design above as a whole (3-question session, turn protocol,
loader UX, engine stack). Beyond the required fixes (T1–T8) these are the
improvements I recommend, in priority order:

| # | Suggestion | Why (evidence from this analysis) | Effort |
|---|---|---|---|
| 7.1 | **Preload the STT + TTS models before the greeting.** faster-whisper and Kokoro load lazily on first use; the *first* transcription of a session pays the model load on top of the utterance (~6 s measured in the first E2E run vs ~1 s later). Warm both engines in the worker at job start (LiveKit's `setup_fnc`/prewarm hook) so the first answer round-trip is not the slowest one. | Measured: first `candidate said (5938 ms)` vs later `(1160 ms)`. | Small |
| 7.2 | **Pick the 3 questions breadth-first across the bank, not sequentially.** Sequential top-of-bank sampling can cluster three related questions (all cache/consistency), under-representing the domain. Select one question per distinct bank section for 3 topics, and skip questions already used in the student's previous sessions. | Banks have 16 real questions across sections; a 3-question sample should cover ≥ 3 sections. | Small |
| 7.3 | **Adaptive judge:** if the qwen2.5:14b judge exceeds ~25 s on two consecutive answers, switch the remainder of the session to llama3.2:3b judging (the hot-path model is already warm). Judge quality drops slightly; a 90 s wait that makes a student leave is worse. | Measured judge waits dominate the 97 s E2E wall clock; the whole point of §3.0.2 is to keep students on the page — speed is part of that. | Medium |
| 7.4 | **Show "Question 2 of 3" and an elapsed-time chip** in the page header. A visible session budget ("~3 of ~7 minutes") sets the expectation that the interview is finite, which measurably reduces mid-session abandonment. | 3-question session ≈ 6–9 min on today's stack (analysis in §3.2). | Small |
| 7.5 | **Let the student correct a mis-transcription once per answer.** Whisper on CPU mis-hears terms ("Redis" → "Riddies" measured). With only 3 questions, one bad transcription can cost a whole score. After the `candidate_heard` echo, accept a short "no, I said …" interjection as a replacement for that turn (a second STT final within ~3 s supersedes the first) — no extra UI needed. | Measured mis-transcriptions in every E2E run ("Riddies counters", "Lewis grips"). | Medium |
| 7.6 | **Persist each scored question to the management plane as it happens** (worker → `POST /sessions/{id}/score`), not only at wrap. Then a session that dies mid-way still shows the student 1–2 earned ratings, and the page can recover the scoreboard after a reload. | R3: ratings exist only in the final packet today; a crash or drop loses everything. | Medium |
| 7.7 | **Guard the follow-up cadence:** cap *spoken* follow-up questions at one per question *and* make the follow-up prompt conditional on the judge's confidence, not just on `FOLLOW_UP:` presence — a weak answer to Q1 of 3 should not eat half the session with two follow-ups. | E2E logs: follow-up asked on both questions; with a 3-question budget the ratio of questions-to-follow-ups should stay ≥ 1. | Small |
| 7.8 | **Add a "Retry interview" affordance** (same domain, fresh question selection per 7.2) one click after the summary, plus a "Try a different domain" shortcut — the natural loop for a practice product; today the page requires a full End → Start cycle. | Session end-state exists only as an idea (T7); the practice loop is the product's core value. | Small |
| 7.9 | **Instrument the abandonment funnel:** log (a) page load → Start clicked, (b) Start → first `candidate_heard`, (c) first answer → first score, (d) session end, with the phase at any drop-off. Two weeks of these numbers tell us whether the remaining exits are echo, latency, or content — the three hypotheses this document could not fully separate without a real user session. | The RCA could only rank hypotheses R1/R2/R6 against code; the browser-only audio path (echo vs mic) still needs field data (§5). | Small |

**Not recommended (deliberately out of scope):** voice-skill grading, adaptive
difficulty per answer, multi-domain mixed sessions, and candidate-specified
question selection — all would complicate the turn protocol before the basics
(visible transcripts, loader UX, 3 reliable questions, End call) are proven in
the browser.
