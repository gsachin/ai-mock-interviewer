"""Interviewer dialogue FSM (Phase 2 skeleton — pure logic, no I/O).

The flow: greeting -> ask_question -> listen -> evaluate -> (follow_up |
score) -> next -> (ask_question | wrap). Transitions are a plain dict; an
invalid (state, event) pair raises ``InvalidTransition`` so bugs surface at
the call site, never as silent state drift. Session state (transcript,
scores, question pointer) lives here and is serialized to Redis by the
caller — this module stays dependency-free and fully unit-testable.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class InterviewerState(str, Enum):
    GREETING = "greeting"
    ASK_QUESTION = "ask_question"
    LISTEN = "listen"
    EVALUATE = "evaluate"
    FOLLOW_UP = "follow_up"
    SCORE = "score"
    NEXT = "next"
    WRAP = "wrap"


class InterviewerEvent(str, Enum):
    GREETED = "greeted"                    # candidate mic ready / session started
    QUESTION_ASKED = "question_asked"      # interviewer finished speaking
    ANSWER_RECEIVED = "answer_received"    # STT final transcript in
    FOLLOWUP_NEEDED = "followup_needed"    # evaluation wants a follow-up
    NO_FOLLOWUP = "no_followup"            # evaluation is satisfied
    FOLLOWUP_ASKED = "followup_asked"
    SCORING_DONE = "scoring_done"
    MORE_QUESTIONS = "more_questions"
    NO_MORE_QUESTIONS = "no_more_questions"
    SESSION_ENDED = "session_ended"


TRANSITIONS: dict[tuple[InterviewerState, InterviewerEvent], InterviewerState] = {
    (InterviewerState.GREETING, InterviewerEvent.GREETED): InterviewerState.ASK_QUESTION,
    (InterviewerState.ASK_QUESTION, InterviewerEvent.QUESTION_ASKED): InterviewerState.LISTEN,
    (InterviewerState.LISTEN, InterviewerEvent.ANSWER_RECEIVED): InterviewerState.EVALUATE,
    (InterviewerState.EVALUATE, InterviewerEvent.FOLLOWUP_NEEDED): InterviewerState.FOLLOW_UP,
    (InterviewerState.EVALUATE, InterviewerEvent.NO_FOLLOWUP): InterviewerState.SCORE,
    (InterviewerState.FOLLOW_UP, InterviewerEvent.FOLLOWUP_ASKED): InterviewerState.LISTEN,
    (InterviewerState.SCORE, InterviewerEvent.SCORING_DONE): InterviewerState.NEXT,
    (InterviewerState.NEXT, InterviewerEvent.MORE_QUESTIONS): InterviewerState.ASK_QUESTION,
    (InterviewerState.NEXT, InterviewerEvent.NO_MORE_QUESTIONS): InterviewerState.WRAP,
    (InterviewerState.WRAP, InterviewerEvent.SESSION_ENDED): InterviewerState.WRAP,  # terminal
}


class InvalidTransition(ValueError):
    """Raised when an event is not legal in the current state."""


@dataclass
class Turn:
    role: str                       # "interviewer" | "candidate"
    text: str


@dataclass
class Session:
    """One interview session. ``domain`` maps to the RAG service's
    ``department`` filter on retrieval; ``tenant_id`` is the RAG tenant."""
    session_id: str
    tenant_id: str
    domain: str
    state: InterviewerState = InterviewerState.GREETING
    current_question_id: str | None = None
    turns: list[Turn] = field(default_factory=list)
    scores: list[dict[str, Any]] = field(default_factory=list)

    def transition(self, event: InterviewerEvent) -> InterviewerState:
        key = (self.state, event)
        if key not in TRANSITIONS:
            raise InvalidTransition(f"{event.value!r} is not legal in {self.state.value!r}")
        self.state = TRANSITIONS[key]
        return self.state

    # ── serialization ───────────────────────────────────────────────────────
    # The store contract is ``save(session_id, dict)``, so a Session has to
    # cross a JSON boundary. This is what the module docstring has always
    # promised ("serialized to Redis by the caller") and it did not exist --
    # which is why wiring the store alone would have raised TypeError on the
    # first real save rather than returning a session.

    def to_dict(self) -> dict[str, Any]:
        """Plain-JSON shape. ``state`` is written as its string value and
        ``turns`` as plain dicts, because ``InterviewerState`` is a str-Enum
        (dumps fine) but ``Turn`` is a dataclass that ``json.dumps`` refuses.
        """
        return {
            "session_id": self.session_id,
            "tenant_id": self.tenant_id,
            "domain": self.domain,
            "state": self.state.value,
            "current_question_id": self.current_question_id,
            "turns": [{"role": t.role, "text": t.text} for t in self.turns],
            "scores": list(self.scores),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Session":
        """Rebuild from ``to_dict`` output.

        Unknown keys are **ignored rather than rejected**: during a rolling
        deploy an old replica can read a record written by a newer one, and a
        hard failure there would turn a schema addition into an outage.
        Genuinely unusable input still raises -- unknown ``state`` is a
        ValueError, not a silently-defaulted GREETING, because silently
        rewinding an interview is worse than a loud error.
        """
        raw_turns = data.get("turns") or []
        turns: list[Turn] = []
        for turn in raw_turns:
            if isinstance(turn, Turn):
                turns.append(turn)
            elif isinstance(turn, dict):
                turns.append(Turn(role=str(turn.get("role", "")),
                                  text=str(turn.get("text", ""))))
            # anything else is a shape we do not know: skip it rather than
            # fail the whole session load

        try:
            state = InterviewerState(data.get("state", InterviewerState.GREETING.value))
        except ValueError as exc:
            raise ValueError(
                f"session {data.get('session_id')!r} has unknown state "
                f"{data.get('state')!r}; refusing to guess (a wrong state "
                f"would silently rewind a live interview)"
            ) from exc

        return cls(
            session_id=data["session_id"],
            tenant_id=data.get("tenant_id", "default"),
            domain=data.get("domain", ""),
            state=state,
            current_question_id=data.get("current_question_id"),
            turns=turns,
            scores=list(data.get("scores") or []),
        )
