"""Interviewer FSM: the full happy path plus invalid-transition rejection."""
import pytest

from interviewer.state_machine import (
    InterviewerEvent,
    InterviewerState,
    InvalidTransition,
    Session,
)


def _session() -> Session:
    return Session(session_id="s1", tenant_id="default", domain="system-design")


def test_full_happy_path_with_follow_up():
    s = _session()
    path = [
        (InterviewerEvent.GREETED, InterviewerState.ASK_QUESTION),
        (InterviewerEvent.QUESTION_ASKED, InterviewerState.LISTEN),
        (InterviewerEvent.ANSWER_RECEIVED, InterviewerState.EVALUATE),
        (InterviewerEvent.FOLLOWUP_NEEDED, InterviewerState.FOLLOW_UP),
        (InterviewerEvent.FOLLOWUP_ASKED, InterviewerState.LISTEN),
        (InterviewerEvent.ANSWER_RECEIVED, InterviewerState.EVALUATE),
        (InterviewerEvent.NO_FOLLOWUP, InterviewerState.SCORE),
        (InterviewerEvent.SCORING_DONE, InterviewerState.NEXT),
        (InterviewerEvent.MORE_QUESTIONS, InterviewerState.ASK_QUESTION),
        (InterviewerEvent.QUESTION_ASKED, InterviewerState.LISTEN),
        (InterviewerEvent.ANSWER_RECEIVED, InterviewerState.EVALUATE),
        (InterviewerEvent.NO_FOLLOWUP, InterviewerState.SCORE),
        (InterviewerEvent.SCORING_DONE, InterviewerState.NEXT),
        (InterviewerEvent.NO_MORE_QUESTIONS, InterviewerState.WRAP),
        (InterviewerEvent.SESSION_ENDED, InterviewerState.WRAP),  # terminal
    ]
    for event, expected in path:
        assert s.transition(event) == expected
    assert s.state == InterviewerState.WRAP


def test_invalid_transition_raises():
    s = _session()
    with pytest.raises(InvalidTransition):
        s.transition(InterviewerEvent.ANSWER_RECEIVED)   # not legal in GREETING
    assert s.state == InterviewerState.GREETING          # state unchanged on rejection


def test_evaluate_has_both_exits():
    s = _session()
    for ev in (InterviewerEvent.GREETED, InterviewerEvent.QUESTION_ASKED,
               InterviewerEvent.ANSWER_RECEIVED):
        s.transition(ev)
    assert s.state == InterviewerState.EVALUATE
    assert s.transition(InterviewerEvent.NO_FOLLOWUP) == InterviewerState.SCORE

    s2 = _session()
    for ev in (InterviewerEvent.GREETED, InterviewerEvent.QUESTION_ASKED,
               InterviewerEvent.ANSWER_RECEIVED):
        s2.transition(ev)
    assert s2.transition(InterviewerEvent.FOLLOWUP_NEEDED) == InterviewerState.FOLLOW_UP
