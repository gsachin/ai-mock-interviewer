"""Scripted text-mode interview runner — the Phase 1 gate.

Drives the interviewer FSM against the standalone RAG service over MCP:
question bank catalog -> exact question fetch -> candidate answer ->
cache-gated rubric retrieval (via execute_agent_context) + domain follow-up
retrieval -> score ledger -> next question -> wrap. No LLM yet (Phase 2) and
no audio (Phase 3); the candidate answers come from a caller-fed script.
"""
from dataclasses import dataclass, field
from typing import Any

from interviewer.rag_client import RagClient
from interviewer.state_machine import InterviewerEvent, Session, Turn


@dataclass
class InterviewStats:
    questions_asked: int = 0
    rubric_retrievals: int = 0
    rubric_cache_hits: int = 0
    followups: int = 0

    @property
    def cache_hit_rate(self) -> float | None:
        if self.rubric_retrievals == 0:
            return None
        return round(self.rubric_cache_hits / self.rubric_retrievals, 3)


class ScriptedInterview:
    """Runs one interview session over a question bank document."""

    def __init__(self, rag: RagClient, session: Session, *,
                 script: dict[str, str] | None = None):
        self._rag = rag
        self._session = session
        self._script = script or {}
        self.stats = InterviewStats()

    async def run(self, doc_id: str, max_questions: int = 2) -> dict[str, Any]:
        s = self._session
        s.transition(InterviewerEvent.GREETED)

        bank = await self._rag.interview_bank(doc_id)
        for i, ref in enumerate(bank.questions[:max_questions]):
            if i > 0:
                s.transition(InterviewerEvent.MORE_QUESTIONS)      # NEXT -> ASK_QUESTION
            s.current_question_id = ref.question_id
            s.transition(InterviewerEvent.QUESTION_ASKED)          # -> LISTEN

            question = await self._rag.interview_question(doc_id, ref.question_id)
            s.turns.append(Turn("interviewer", question.formatted))

            answer = self._script.get(ref.question_id, "")
            s.turns.append(Turn("candidate", answer))
            s.transition(InterviewerEvent.ANSWER_RECEIVED)          # -> EVALUATE

            # Evaluation support: cache-gated rubric retrieval. Repeated
            # rubric queries across sessions hit the semantic cache — the
            # hit rate is the Phase 1 gate metric.
            rubric = await self._rag.agent_context(
                "", "", rubric_query=ref.section_title)
            self.stats.rubric_retrievals += 1
            if rubric.get("hit_source") == "cache":
                self.stats.rubric_cache_hits += 1

            # Domain-scoped follow-up material for the candidate's answer.
            followups = await self._rag.interview_followup(
                answer, domain=s.domain, top_k=3)
            self.stats.followups += 1

            s.scores.append({
                "question_id": ref.question_id,
                "section_title": ref.section_title,
                "rubric_hit_source": rubric.get("hit_source"),
                "rubric_chunks": len(rubric.get("provenance", [])),
                "followup_chunks": followups.count,
                "timings_ms": rubric.get("timings_ms"),
            })

            s.transition(InterviewerEvent.NO_FOLLOWUP)              # -> SCORE
            s.transition(InterviewerEvent.SCORING_DONE)             # -> NEXT
            self.stats.questions_asked += 1

        s.transition(InterviewerEvent.NO_MORE_QUESTIONS)            # -> WRAP
        s.transition(InterviewerEvent.SESSION_ENDED)
        return self.summary()

    def summary(self) -> dict[str, Any]:
        s = self._session
        return {
            "session_id": s.session_id,
            "tenant_id": s.tenant_id,
            "domain": s.domain,
            "state": s.state.value,
            "turns": [t.__dict__ for t in s.turns],
            "scores": s.scores,
            "stats": {
                "questions_asked": self.stats.questions_asked,
                "rubric_retrievals": self.stats.rubric_retrievals,
                "rubric_cache_hits": self.stats.rubric_cache_hits,
                "cache_hit_rate": self.stats.cache_hit_rate,
                "followups": self.stats.followups,
            },
        }
