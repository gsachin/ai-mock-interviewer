"""LLMInterviewer — the interviewer brain (text and voice modes).

Drives the FSM over the standalone RAG MCP service and OpenAI-compatible
LLMs: greeting -> question turns -> judge evaluation (cache-gated rubric +
domain follow-up context) -> one follow-up round when the judge asks ->
score ledger -> wrap.

Phase 3 voice mode: pass a ``tts`` engine (interviewer turns stream through
sentence-level TTS — first audio after sentence one, never after the full
answer), a fast ``voice_llm`` for the hot path, and a ``Candidate`` whose
answers arrive from STT with measured transcription time. Per-hop latency
(the core's ``timings_ms`` plus LLM/TTS/STT metrics) is logged into the
summary; a ``LatencyBudgetTracker`` receives one record per interviewer
turn for the < 1.5 s gate.
"""
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

from interviewer.llm import OpenAICompatibleLLM
from interviewer.prompts import (
    MAX_SPOKEN_CHARS,
    build_evaluation_prompt,
    build_followup_prompt,
    build_greeting_prompt,
    build_question_prompt,
    build_reprompt_prompt,
    build_system_prompt,
    build_wrap_prompt,
)
from interviewer.scoring import (
    Evaluation,
    parse_evaluation,
    verdict_from_scores,
)
from interviewer.state_machine import InterviewerEvent, Session, Turn
from interviewer.voice.budget import LatencyBudgetTracker
from interviewer.voice.protocols import AudioSink
from interviewer.voice.splitter import SentenceAccumulator


@dataclass(frozen=True)
class CandidateAnswer:
    text: str
    stt_ms: float = 0.0        # transcription latency (voice mode)


class Candidate(Protocol):
    async def answer(self, question_id: str,
                     timeout_s: float | None = None,
                     drop_before_ts: float | None = None) -> CandidateAnswer:
        """The candidate's spoken/text answer for one question. ``timeout_s``
        caps the wait (voice no-hang gate): implementations may raise
        ``TimeoutError`` when the limit is hit, or ignore it (scripted
        candidates return instantly). ``drop_before_ts`` (monotonic) makes
        the candidate discard transcripts that arrived before that instant —
        the brain uses it to keep an answer that arrived while the spoken
        re-prompt played, instead of treating it as barge-in junk."""


class ScriptedCandidate:
    """Text-mode candidate: answers come from a caller-fed dict."""

    def __init__(self, answers: dict[str, str] | None = None):
        self._answers = answers or {}

    async def answer(self, question_id: str,
                     timeout_s: float | None = None,
                     drop_before_ts: float | None = None) -> CandidateAnswer:
        return CandidateAnswer(text=self._answers.get(question_id, ""))


class Reviewer(Protocol):
    async def decide(self, question_id: str,
                     timeout_s: float | None = None) -> str:
        """The review gate's decision after a question is scored (manual
        answer mode): the candidate chooses ``"retake"`` (answer the same
        question again — its score is replaced) or ``"next"`` (advance).
        May raise ``TimeoutError`` when no choice arrives in time — the
        interview then advances as if "next"."""


class _EmptyFollowups:
    """Stand-in when there is no answer to retrieve against (empty scripted
    answer) — the judge still evaluates the (empty) answer."""
    chunks: list = []


class EmptyBankError(RuntimeError):
    """Raised when a doc id has no registered question bank — the interview
    cannot start. Typed so the UIs can show a friendly, actionable message
    instead of a silent zero-question interview or a generic error."""


def rubric_context(rubric: dict[str, Any], followups: Any) -> str:
    """Evaluation context: the core's U-shape envelope plus domain follow-up
    chunks, capped so the judge prompt stays lean."""
    parts = []
    envelope = (rubric.get("context_envelope") or "").strip()
    if envelope:
        parts.append(envelope)
    for chunk in followups.chunks:
        parts.append(f"[{chunk.section_title}] {chunk.content}")
    text = "\n\n".join(parts).strip()
    return text[:3000] or "(no rubric context retrieved)"


# RCA fix (2026-09-03): the page-facing phase protocol. Every backend phase
# emits a ``state`` event whose label the page shows as an activity line —
# the student always sees *something* between spoken turns. Labels are plain
# typography (no emoji glyphs — the UI renders icons as graphics, not text).
PHASE_LABELS: dict[str, str] = {
    "speaking": "Interviewer speaking",
    "listening": "Listening — please answer aloud",
    "transcribing": "Transcribing your answer",
    "evaluating": "Evaluating your answer",
    "scoring": "Recording your score",
    "review": "Answer scored — retake it or move to the next question",
    "wrap": "Preparing your feedback",
}


class LLMInterviewer:
    def __init__(self, rag: Any, llm: OpenAICompatibleLLM, session: Session, *,
                 max_questions: int = 2, followup_budget: int = 1,
                 spoken_max_chars: int = MAX_SPOKEN_CHARS,
                 answer_timeout_s: float = 60.0,
                 review_timeout_s: float = 90.0,
                 # Phase 3 voice extensions (all optional — text mode needs none):
                 tts: Any = None,
                 voice_llm: OpenAICompatibleLLM | None = None,
                 budget: LatencyBudgetTracker | None = None,
                 sink: AudioSink | None = None,
                 on_event: Callable[[dict], Awaitable[None]] | None = None,
                 decider: Reviewer | None = None):
        self._rag = rag
        self._llm = llm
        self._session = session
        self._max_questions = max_questions
        self._answer_timeout_s = answer_timeout_s
        self._review_timeout_s = review_timeout_s
        self._decider = decider
        self._followup_budget = followup_budget
        self._spoken_max_chars = spoken_max_chars
        self._system_prompt = build_system_prompt(session.domain)
        self.tts = tts
        self._voice_llm = voice_llm
        self._budget = budget
        self._sink = sink
        self._on_event = on_event
        self._interrupted = False
        self._last_stt_ms = 0.0
        self._last_rag_ms = 0.0
        self._last_tts_ms = 0.0
        self._last_judge_ms = 0.0
        # metrics of the engine used by the most recent call (_speak or _judge)
        self._current_metrics = llm.metrics

    # ── barge-in ────────────────────────────────────────────────────────────

    async def interrupt(self) -> None:
        """Barge-in: stop speaking at the next sentence boundary. The FSM
        caller then transitions to LISTEN. Never raises — the LLM stream
        drains silently until the next boundary check. In voice mode the
        playback sink is interrupted too (mid-sentence audio stops at once)."""
        self._interrupted = True
        if self._sink is not None:
            await self._sink.interrupt()

    # ── turn helpers ────────────────────────────────────────────────────────

    async def _emit(self, event: dict[str, Any]) -> None:
        """Push a UI event (turn transcript / state / score / summary) to the
        optional subscriber — the LiveKit worker publishes these as data
        packets. Text mode has no subscriber: the events are no-ops."""
        if self._on_event is not None:
            try:
                await self._on_event(event)
            except Exception:  # telemetry never breaks the interview
                pass

    async def _phase(self, phase: str) -> None:
        """One ``state`` event: the page shows the phase's label as a spinner
        until the next event (requirement: no silent background work)."""
        await self._emit({"type": "state", "phase": phase,
                          "label": PHASE_LABELS.get(phase, phase)})

    def _hop(self, stage: str, *, rag_ms: float = 0.0, **extra: Any) -> dict[str, Any]:
        """Snapshot one hop's latency: the last call's LLM metrics + the RAG
        service's timings_ms.total + voice-stage timings."""
        m = self._current_metrics
        hop = {
            "stage": stage,
            "llm_first_token_ms": round(m.first_token_ms or 0.0, 1),
            "llm_total_ms": round(m.total_ms, 1),
            "rag_total_ms": round(rag_ms, 1),
            "stt_final_ms": round(self._last_stt_ms, 1),
            "tts_first_audio_ms": round(self._last_tts_ms, 1),
            "judge_wait_ms": round(self._last_judge_ms, 1),
            **extra,
        }
        if self._budget is not None and stage in ("greeting", "question",
                                                  "followup", "wrap"):
            # utterance-end -> first interviewer audio (the study's bar).
            self._budget.record({
                "stt_final_ms": self._last_stt_ms,
                "rag_ms": self._last_rag_ms,
                "llm_first_token_ms": m.first_token_ms or 0.0,
                "tts_first_audio_ms": self._last_tts_ms,
                "judge_wait_ms": self._last_judge_ms,
            })
        self._last_tts_ms = 0.0
        return hop

    async def _speak(self, prompt: str) -> str:
        """One short spoken turn. Voice mode: stream through sentence-level
        TTS — first audio after sentence one, truncated to the spoken budget.
        Text mode: stream to a string only."""
        self._interrupted = False
        await self._phase("speaking")
        engine = self._voice_llm or self._llm
        messages = [{"role": "system", "content": self._system_prompt},
                    {"role": "user", "content": prompt}]
        t0 = time.perf_counter()
        accumulator = SentenceAccumulator()
        parts: list[str] = []
        audio: list[bytes] = []

        async def _synth(sentence: str) -> None:
            data = await self.tts.synthesize(sentence)
            if self._sink is not None:
                await self._sink.play(data)      # voice mode: play it
            else:
                audio.append(data)               # dev/text mode: nothing to play
            if self._last_tts_ms == 0.0:
                self._last_tts_ms = (time.perf_counter() - t0) * 1000

        async for delta in engine.respond_stream(messages):
            parts.append(delta)
            for sentence in accumulator.feed(delta):
                if self._interrupted:
                    break
                if self.tts is not None:
                    await _synth(sentence)
            if self._interrupted:
                break
        if not self._interrupted:
            for sentence in accumulator.flush():
                if self.tts is not None:
                    await _synth(sentence)
        self._current_metrics = engine.metrics
        return "".join(parts).strip()[:self._spoken_max_chars]

    async def _judge(self, prompt: str) -> Evaluation:
        t0 = time.perf_counter()
        messages = [{"role": "system", "content": self._system_prompt},
                    {"role": "user", "content": prompt}]
        result = parse_evaluation(await self._llm.respond(messages))
        self._last_judge_ms = (time.perf_counter() - t0) * 1000
        self._current_metrics = self._llm.metrics
        return result

    async def _listen(self, question_id: str) -> CandidateAnswer:
        """Wait for one spoken answer with a bounded wait — the no-hang gate
        (RCA R1). First timeout: the interviewer asks the candidate to repeat
        (one re-prompt per question). Second timeout: the question counts as
        unanswered — the brain returns an empty answer, the judge evaluates
        it, and the interview moves on. The FSM therefore can never deadlock
        and always reaches ``wrap``."""
        drop_before_ts: float | None = None
        for attempt in (1, 2):
            await self._phase("listening")
            try:
                return await self._candidate.answer(
                    question_id, timeout_s=self._answer_timeout_s,
                    drop_before_ts=drop_before_ts)
            except TimeoutError:
                if attempt == 2:
                    return CandidateAnswer(text="")   # scored as unanswered
                # The re-prompt invites an immediate answer: speech that
                # arrives while it plays is a real answer, not barge-in junk —
                # the second attempt must not drain it (RCA F1).
                drop_before_ts = time.monotonic()
                reprompt = await self._speak(build_reprompt_prompt())
                self._session.turns.append(Turn("interviewer", reprompt))
                await self._emit({"type": "turn", "role": "interviewer",
                                  "text": reprompt, "stage": "reprompt"})
        raise AssertionError("unreachable")  # pragma: no cover

    # ── the interview ───────────────────────────────────────────────────────

    async def run(self, doc_id: str,
                  candidate: Candidate | dict[str, str] | None = None, *,
                  answers: dict[str, str] | None = None) -> dict[str, Any]:
        if answers is not None:                 # backwards-compatible alias
            candidate = ScriptedCandidate(answers)
        elif candidate is None or isinstance(candidate, dict):
            candidate = ScriptedCandidate(candidate)
        self._candidate = candidate
        s = self._session
        hops: list[dict[str, Any]] = []
        rubric_retrievals = 0
        rubric_cache_hits = 0
        t_start = time.perf_counter()

        greeting = await self._speak(build_greeting_prompt(s.domain))
        s.turns.append(Turn("interviewer", greeting))
        await self._emit({"type": "turn", "role": "interviewer",
                          "text": greeting, "stage": "greeting"})
        hops.append(self._hop("greeting"))
        s.transition(InterviewerEvent.GREETED)

        bank = await self._rag.interview_bank(doc_id)
        if not bank.questions:
            raise EmptyBankError(
                f"no questions registered for '{doc_id}' — register the skill "
                "on the Skill Update page, or restart services to "
                "auto-register the question_banks folder")
        for i, ref in enumerate(bank.questions[:self._max_questions]):
            if i > 0:
                s.transition(InterviewerEvent.MORE_QUESTIONS)
            # Manual-answer retake loop: after the question is scored the
            # review gate (if a decider exists) lets the candidate answer it
            # again — the previous score for the question is replaced. With
            # no decider the interview advances exactly as before.
            while True:
                s.current_question_id = ref.question_id
                s.transition(InterviewerEvent.QUESTION_ASKED)        # -> LISTEN

                question = await self._rag.interview_question(
                    doc_id, ref.question_id)
                spoken_question = await self._speak(
                    build_question_prompt(question.formatted, ref.question_id))
                s.turns.append(Turn("interviewer", spoken_question))
                await self._emit({"type": "turn", "role": "interviewer",
                                  "text": spoken_question,
                                  "stage": "question",
                                  "question_id": ref.question_id})
                hops.append(self._hop("question",
                                      question_id=ref.question_id))

                answer = await self._listen(ref.question_id)
                self._last_stt_ms = answer.stt_ms
                if answer.text.strip():
                    # The candidate's words are appended to the transcript;
                    # in voice mode the page already showed them as
                    # ``candidate_heard`` (STT-first echo), so empty
                    # (unanswered) turns add nothing.
                    s.turns.append(Turn("candidate", answer.text))
                    await self._emit({"type": "turn", "role": "candidate",
                                      "text": answer.text})
                s.transition(InterviewerEvent.ANSWER_RECEIVED)       # -> EVALUATE

                followup_asked = False
                while True:
                    # Cache-gated rubric retrieval — repeated rubrics hit the
                    # RAG service's semantic cache (the Phase 1 gate metric).
                    await self._phase("evaluating")
                    rubric = await self._rag.agent_context(
                        "", "", rubric_query=ref.section_title)
                    rubric_retrievals += 1
                    if rubric.get("hit_source") == "cache":
                        rubric_cache_hits += 1
                    rag_ms = float((rubric.get("timings_ms") or {}).get("total", 0.0))
                    self._last_rag_ms = rag_ms

                    if answer.text.strip():
                        followups = await self._rag.interview_followup(
                            answer.text, domain=s.domain, top_k=3)
                    else:
                        followups = _EmptyFollowups()  # nothing to retrieve against

                    evaluation = await self._judge(build_evaluation_prompt(
                        question.formatted, answer.text,
                        rubric_context(rubric, followups)))
                    hops.append(self._hop(
                        "evaluate", question_id=ref.question_id, rag_ms=rag_ms,
                        rubric_hit_source=rubric.get("hit_source")))

                    if (evaluation.followup and not followup_asked
                            and self._followup_budget > 0):
                        followup_asked = True
                        s.transition(InterviewerEvent.FOLLOWUP_NEEDED)  # -> FOLLOW_UP
                        spoken_followup = await self._speak(
                            build_followup_prompt(evaluation.followup))
                        s.turns.append(Turn("interviewer", spoken_followup))
                        await self._emit({"type": "turn", "role": "interviewer",
                                          "text": spoken_followup,
                                          "stage": "followup"})
                        hops.append(self._hop("followup",
                                              question_id=ref.question_id))
                        s.transition(InterviewerEvent.FOLLOWUP_ASKED)   # -> LISTEN
                        followup_answer = await self._listen(
                            f"{ref.question_id}:followup")
                        self._last_stt_ms = followup_answer.stt_ms
                        if followup_answer.text.strip():
                            s.turns.append(Turn("candidate",
                                                followup_answer.text))
                            await self._emit({"type": "turn",
                                              "role": "candidate",
                                              "text": followup_answer.text})
                        answer = followup_answer
                        s.transition(InterviewerEvent.ANSWER_RECEIVED)  # -> EVALUATE
                        continue    # judge the follow-up answer this round
                    break

                await self._phase("scoring")
                verdict = evaluation.verdict or verdict_from_scores(
                    evaluation.scores)
                model_answer = evaluation.model_answer
                gap = evaluation.justifications[:300]
                s.scores.append({
                    "question_id": ref.question_id,
                    "section_title": ref.section_title,
                    "scores": evaluation.scores,
                    "justifications": evaluation.justifications,
                    "followup_asked": followup_asked,
                    "raw_evaluation": evaluation.justifications[:500],
                    "verdict": verdict,
                    "model_answer": model_answer,
                })
                # per-question score event: the page's scoreboard grows after
                # every question, never only at the final summary (RCA R3),
                # and carries the review-gate feedback (verdict + model
                # answer) the student sees before Retake / Next.
                await self._emit({"type": "score", "score": {
                    "question_id": ref.question_id,
                    "section_title": ref.section_title,
                    "scores": evaluation.scores,
                    "followup_asked": followup_asked,
                    "verdict": verdict,
                    "model_answer": model_answer,
                    "gap": gap,
                }})
                s.transition(InterviewerEvent.NO_FOLLOWUP)           # -> SCORE
                s.transition(InterviewerEvent.SCORING_DONE)          # -> NEXT

                # ── review gate (manual answer mode) ────────────────────────
                if self._decider is None:
                    break
                await self._phase("review")
                try:
                    decision = await self._decider.decide(
                        ref.question_id, timeout_s=self._review_timeout_s)
                except TimeoutError:
                    break                       # no choice -> advance
                if decision != "retake":
                    break
                # Retake: drop this question's previous score entry, then
                # re-ask from scratch (this loop re-runs the question).
                s.scores = [e for e in s.scores
                            if e["question_id"] != ref.question_id]
                s.transition(InterviewerEvent.MORE_QUESTIONS)   # NEXT -> ASK

        s.transition(InterviewerEvent.NO_MORE_QUESTIONS)             # -> WRAP
        avg = 0.0
        all_scores = [v for entry in s.scores for v in entry["scores"].values()]
        if all_scores:
            avg = sum(all_scores) / len(all_scores)
        closing = await self._speak(build_wrap_prompt(avg))
        s.turns.append(Turn("interviewer", closing))
        await self._emit({"type": "turn", "role": "interviewer",
                          "text": closing, "stage": "wrap"})
        hops.append(self._hop("wrap"))
        s.transition(InterviewerEvent.SESSION_ENDED)

        llm_ft = [h["llm_first_token_ms"] for h in hops if h["llm_first_token_ms"]]
        llm_tot = [h["llm_total_ms"] for h in hops if h["llm_total_ms"]]
        rag_tot = [h["rag_total_ms"] for h in hops if h["rag_total_ms"]]
        stats = {
            "questions_asked": len(s.scores),
            "rubric_retrievals": rubric_retrievals,
            "rubric_cache_hits": rubric_cache_hits,
            "cache_hit_rate": round(rubric_cache_hits / rubric_retrievals, 3)
                              if rubric_retrievals else None,
            "average_score": round(avg, 2),
            "hops": hops,
            "llm_first_token_mean_ms": round(sum(llm_ft) / len(llm_ft), 1)
                                       if llm_ft else None,
            "llm_total_mean_ms": round(sum(llm_tot) / len(llm_tot), 1)
                                 if llm_tot else None,
            "rag_total_mean_ms": round(sum(rag_tot) / len(rag_tot), 1)
                                 if rag_tot else None,
            "voice_budget": self._budget.aggregate() if self._budget else None,
            "voice_budget_bar": self._budget.bar() if self._budget else None,
            "wall_ms": round((time.perf_counter() - t_start) * 1000, 1),
        }
        summary = {
            "session_id": s.session_id,
            "tenant_id": s.tenant_id,
            "domain": s.domain,
            "doc_id": doc_id,
            "state": s.state.value,
            "turns": [t.__dict__ for t in s.turns],
            "scores": s.scores,
            "stats": stats,
        }
        await self._phase("wrap")
        # Compact wire event: scores + stats only. The full-history summary
        # stays the return value (persistence); the data packet stays small
        # so it cannot overflow a live room channel (RCA R3).
        await self._emit({"type": "summary", "summary": {
            "state": s.state.value, "scores": s.scores, "stats": stats}})
        return summary
