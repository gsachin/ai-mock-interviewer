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
