"""LLMInterviewer: full dialogue with a follow-up round, scoring ledger,
per-hop latency logging, and the no-follow-up fast path. Stub RAG + stub LLM
(the real services are exercised by the live gate)."""
import asyncio

from interviewer.brain import LLMInterviewer
from interviewer.state_machine import InterviewerState, Session
from interviewer.voice.stubs import StubLLM
from tests.test_interview import StubRag

SPEAK_SCRIPT = [
    "Welcome to your system design interview.",              # greeting
    "Design a rate limiter for a public API.",               # q1
    "What is the difference between token bucket and sliding window?",  # follow-up
    "Explain consistent hashing and reshuffling.",            # q2
    "Closing remarks. Your average score was 4.0 out of 5.",  # wrap
]

EVAL_FOLLOWUP = (
    "Correctness: 2 - wrong algorithm.\nDepth: 2 - no trade-offs.\n"
    "Communication: 3 - clear.\n"
    "FOLLOW_UP: What is the difference between token bucket and sliding window?"
)
EVAL_Q1 = ("Correctness: 4 - core mechanism covered.\nDepth: 3 - misses trade-offs.\n"
           "Communication: 4 - structured.\nFOLLOW_UP: none")
EVAL_Q2 = ("Correctness: 4\nDepth: 4\nCommunication: 5\nFOLLOW_UP: none")

ANSWERS = {
    "s1": "token bucket with redis counters",
    "s1:followup": "sliding window smooths bursts better",
    "s2": "virtual nodes minimize reshuffling",
}


def run(coro):
    return asyncio.run(coro)


def test_full_interview_with_followup_round():
    rag = StubRag()
    llm = StubLLM(list(SPEAK_SCRIPT), [EVAL_FOLLOWUP, EVAL_Q1, EVAL_Q2])
    interviewer = LLMInterviewer(rag, llm, Session(
        session_id="s1", tenant_id="default", domain="system-design"))

    summary = run(interviewer.run("bank-sd", answers=ANSWERS))

    assert summary["state"] == InterviewerState.WRAP.value
    roles = [t["role"] for t in summary["turns"]]
    assert roles == ["interviewer", "interviewer", "candidate", "interviewer",
                     "candidate", "interviewer", "candidate", "interviewer"]
    # the follow-up turn is the judge's question, spoken verbatim
    assert any("token bucket and sliding window" in t["text"]
               for t in summary["turns"] if t["role"] == "interviewer")

    # ledger: q1 scored from the post-follow-up evaluation, q2 from its own
    assert [e["question_id"] for e in summary["scores"]] == ["s1", "s2"]
    assert summary["scores"][0]["scores"] == {"correctness": 4, "depth": 3,
                                              "communication": 4}
    assert summary["scores"][0]["followup_asked"] is True
    assert summary["scores"][1]["followup_asked"] is False

    # cache accounting: q1's rubric ran twice (initial + follow-up round)
    stats = summary["stats"]
    assert stats["rubric_retrievals"] == 3
    assert stats["rubric_cache_hits"] == 2
    assert stats["cache_hit_rate"] == round(2 / 3, 3)
    assert stats["average_score"] == 4.0

    # per-hop latency: every hop carries LLM + RAG timings
    stages = [h["stage"] for h in stats["hops"]]
    assert stages[:2] == ["greeting", "question"]
    assert "evaluate" in stages and "followup" in stages and "wrap" in stages
    assert all(h["llm_total_ms"] >= 0 and h["rag_total_ms"] >= 0
               for h in stats["hops"])
    assert stats["llm_first_token_mean_ms"] is not None
    assert stats["rag_total_mean_ms"] is not None
    assert stats["wall_ms"] > 0


def test_interview_without_followup_fast_path():
    rag = StubRag()
    llm = StubLLM(list(SPEAK_SCRIPT), [EVAL_Q1, EVAL_Q2])
    interviewer = LLMInterviewer(rag, llm, Session(
        session_id="s2", tenant_id="default", domain="system-design"))

    summary = run(interviewer.run("bank-sd", answers=ANSWERS))
    assert summary["state"] == InterviewerState.WRAP.value
    assert all(not e["followup_asked"] for e in summary["scores"])
    assert len(summary["turns"]) == 6           # greeting, q1, a1, q2, a2, wrap
    stats = summary["stats"]
    assert stats["rubric_retrievals"] == 2
    assert stats["rubric_cache_hits"] == 1
    assert not any(h["stage"] == "followup" for h in stats["hops"])


def test_empty_followup_answer_does_not_crash():
    """Live-gate finding: a missing scripted follow-up answer used to embed an
    empty string (Ollama answers {"embedding": []}) and fail the tool. The
    brain now skips retrieval for empty answers and still judges them."""
    rag = StubRag()
    llm = StubLLM(list(SPEAK_SCRIPT), [EVAL_FOLLOWUP, EVAL_Q1, EVAL_Q2])
    answers = dict(ANSWERS)
    del answers["s1:followup"]                  # candidate stayed silent
    interviewer = LLMInterviewer(rag, llm, Session(
        session_id="s3", tenant_id="default", domain="system-design"))

    summary = run(interviewer.run("bank-sd", answers=answers))
    assert summary["state"] == InterviewerState.WRAP.value
    assert summary["scores"][0]["followup_asked"] is True
    # the empty follow-up answer produced no followup retrieval call
    assert all("Silent" not in c[0] for c in rag.followup_calls if c[0])
    assert not any(c[0] == "" for c in rag.followup_calls)
