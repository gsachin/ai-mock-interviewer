"""LLMInterviewer — the Phase 2 interviewer brain.

Drives the FSM over the standalone RAG MCP service and an OpenAI-compatible
LLM: greeting -> question turns (voice-optimized rephrase, streamed and
truncated to the spoken budget) -> evaluation (LLM-judge against the
cache-gated rubric plus domain follow-up chunks) -> one follow-up turn when
the judge asks -> score ledger -> wrap. Per-hop latency (the core's
``timings_ms`` plus LLM first-token/total metrics) is logged into the
summary — the Phase 2 gate metric.
"""
import time
from typing import Any

from interviewer.llm import OpenAICompatibleLLM
from interviewer.prompts import (
    MAX_SPOKEN_CHARS,
    build_evaluation_prompt,
    build_followup_prompt,
    build_greeting_prompt,
    build_question_prompt,
    build_system_prompt,
    build_wrap_prompt,
)
from interviewer.scoring import Evaluation, parse_evaluation
from interviewer.state_machine import InterviewerEvent, Session, Turn


class _EmptyFollowups:
    """Stand-in when there is no answer to retrieve against (empty scripted
    answer) — the judge still evaluates the (empty) answer."""
    chunks: list = []


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


class LLMInterviewer:
    def __init__(self, rag: Any, llm: OpenAICompatibleLLM, session: Session, *,
                 max_questions: int = 2, followup_budget: int = 1,
                 spoken_max_chars: int = MAX_SPOKEN_CHARS):
        self._rag = rag
        self._llm = llm
        self._session = session
        self._max_questions = max_questions
        self._followup_budget = followup_budget
        self._spoken_max_chars = spoken_max_chars
        self._system_prompt = build_system_prompt(session.domain)

    # ── turn helpers ────────────────────────────────────────────────────────

    def _hop(self, stage: str, *, rag_ms: float = 0.0, **extra: Any) -> dict[str, Any]:
        """Snapshot one hop's latency: LLM metrics (set by the last call) +
        the RAG service's timings_ms.total."""
        m = self._llm.metrics
        return {
            "stage": stage,
            "llm_first_token_ms": round(m.first_token_ms or 0.0, 1),
            "llm_total_ms": round(m.total_ms, 1),
            "rag_total_ms": round(rag_ms, 1),
            **extra,
        }

    async def _speak(self, prompt: str) -> str:
        """One short spoken turn: stream the LLM, keep the spoken budget."""
        messages = [{"role": "system", "content": self._system_prompt},
                    {"role": "user", "content": prompt}]
        parts = []
        async for delta in self._llm.respond_stream(messages):
            parts.append(delta)
        return "".join(parts).strip()[:self._spoken_max_chars]

    async def _judge(self, prompt: str) -> Evaluation:
        messages = [{"role": "system", "content": self._system_prompt},
                    {"role": "user", "content": prompt}]
        return parse_evaluation(await self._llm.respond(messages))

    # ── the interview ───────────────────────────────────────────────────────

    async def run(self, doc_id: str, answers: dict[str, str] | None = None) -> dict[str, Any]:
        answers = answers or {}
        s = self._session
        hops: list[dict[str, Any]] = []
        rubric_retrievals = 0
        rubric_cache_hits = 0
        t_start = time.perf_counter()

        greeting = await self._speak(build_greeting_prompt(s.domain))
        s.turns.append(Turn("interviewer", greeting))
        hops.append(self._hop("greeting"))
        s.transition(InterviewerEvent.GREETED)

        bank = await self._rag.interview_bank(doc_id)
        for i, ref in enumerate(bank.questions[:self._max_questions]):
            if i > 0:
                s.transition(InterviewerEvent.MORE_QUESTIONS)
            s.current_question_id = ref.question_id
            s.transition(InterviewerEvent.QUESTION_ASKED)        # -> LISTEN

            question = await self._rag.interview_question(doc_id, ref.question_id)
            spoken_question = await self._speak(
                build_question_prompt(question.formatted, ref.question_id))
            s.turns.append(Turn("interviewer", spoken_question))
            hops.append(self._hop("question", question_id=ref.question_id))

            answer = answers.get(ref.question_id, "")
            s.turns.append(Turn("candidate", answer))
            s.transition(InterviewerEvent.ANSWER_RECEIVED)       # -> EVALUATE

            followup_asked = False
            while True:
                # Cache-gated rubric retrieval — repeated rubrics hit the
                # RAG service's semantic cache (the Phase 1 gate metric).
                rubric = await self._rag.agent_context(
                    "", "", rubric_query=ref.section_title)
                rubric_retrievals += 1
                if rubric.get("hit_source") == "cache":
                    rubric_cache_hits += 1
                rag_ms = float((rubric.get("timings_ms") or {}).get("total", 0.0))

                if answer.strip():
                    followups = await self._rag.interview_followup(
                        answer, domain=s.domain, top_k=3)
                else:
                    followups = _EmptyFollowups()   # nothing to retrieve against

                evaluation = await self._judge(build_evaluation_prompt(
                    question.formatted, answer, rubric_context(rubric, followups)))
                hops.append(self._hop("evaluate", question_id=ref.question_id,
                                      rag_ms=rag_ms, rubric_hit_source=rubric.get("hit_source")))

                if (evaluation.followup and not followup_asked
                        and self._followup_budget > 0):
                    followup_asked = True
                    s.transition(InterviewerEvent.FOLLOWUP_NEEDED)   # -> FOLLOW_UP
                    spoken_followup = await self._speak(
                        build_followup_prompt(evaluation.followup))
                    s.turns.append(Turn("interviewer", spoken_followup))
                    hops.append(self._hop("followup", question_id=ref.question_id))
                    s.transition(InterviewerEvent.FOLLOWUP_ASKED)    # -> LISTEN
                    followup_answer = answers.get(f"{ref.question_id}:followup", "")
                    s.turns.append(Turn("candidate", followup_answer))
                    answer = followup_answer
                    s.transition(InterviewerEvent.ANSWER_RECEIVED)   # -> EVALUATE
                    continue    # judge the follow-up answer this round
                break

            s.scores.append({
                "question_id": ref.question_id,
                "section_title": ref.section_title,
                "scores": evaluation.scores,
                "justifications": evaluation.justifications,
                "followup_asked": followup_asked,
                "raw_evaluation": evaluation.justifications[:500],
            })
            s.transition(InterviewerEvent.NO_FOLLOWUP)               # -> SCORE
            s.transition(InterviewerEvent.SCORING_DONE)              # -> NEXT

        s.transition(InterviewerEvent.NO_MORE_QUESTIONS)             # -> WRAP
        avg = 0.0
        all_scores = [v for entry in s.scores for v in entry["scores"].values()]
        if all_scores:
            avg = sum(all_scores) / len(all_scores)
        closing = await self._speak(build_wrap_prompt(avg))
        s.turns.append(Turn("interviewer", closing))
        hops.append(self._hop("wrap"))
        s.transition(InterviewerEvent.SESSION_ENDED)

        llm_ft = [h["llm_first_token_ms"] for h in hops if h["llm_first_token_ms"]]
        llm_tot = [h["llm_total_ms"] for h in hops if h["llm_total_ms"]]
        rag_tot = [h["rag_total_ms"] for h in hops if h["rag_total_ms"]]
        return {
            "session_id": s.session_id,
            "tenant_id": s.tenant_id,
            "domain": s.domain,
            "doc_id": doc_id,
            "state": s.state.value,
            "turns": [t.__dict__ for t in s.turns],
            "scores": s.scores,
            "stats": {
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
                "wall_ms": round((time.perf_counter() - t_start) * 1000, 1),
            },
        }
