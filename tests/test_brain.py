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


# ── RCA fix regression tests (2026-09-03) ─────────────────────────────────────
# T1/T4: three-question sessions + the typed page protocol (state phases,
# per-question score events, compact summary). T2: the bounded answer wait
# with one re-prompt and an unanswered-score fallback.

EVAL_NONE = ("Correctness: 4 - covers the core mechanism.\n"
             "Depth: 4 - solid trade-offs.\n"
             "Communication: 4 - clear.\n"
             "FOLLOW_UP: none")


def test_event_protocol_phases_scores_and_compact_summary():
    """T4: every phase emits a labelled state event; scores arrive per
    question; the summary data packet carries scores+stats only (no full
    transcript — it cannot overflow the room channel)."""
    rag = StubRag(["Rate limiter", "Consistent hashing", "Caching"])
    llm = StubLLM(
        ["Welcome to your system design interview.",      # greeting
         "Design a rate limiter for a public API.",       # q1
         "Explain consistent hashing.",                   # q2
         "Design a cache for hot keys.",                  # q3
         "Closing remarks. Your average score was 4.0."], # wrap
        [EVAL_NONE, EVAL_NONE, EVAL_NONE])
    events: list[dict] = []

    async def collect(ev: dict) -> None:
        events.append(ev)

    interviewer = LLMInterviewer(
        rag, llm, Session(session_id="proto", tenant_id="default",
                          domain="system-design"),
        max_questions=3, on_event=collect)
    summary = run(interviewer.run("bank-sd", answers={
        "s1": "token bucket with redis counters",
        "s2": "virtual nodes minimize reshuffling",
        "s3": "a write-through cache with an LRU eviction list",
    }))

    assert summary["stats"]["questions_asked"] == 3
    states = [e for e in events if e["type"] == "state"]
    # every state event carries a visible processing label (3.0.2)
    assert all(e.get("label") for e in states)
    phases = [e["phase"] for e in states]
    assert phases.count("listening") == 3
    assert phases.count("evaluating") == 3
    assert phases.count("scoring") == 3
    # per-question scores arrive progressively, in question order
    scores = [e for e in events if e["type"] == "score"]
    assert [e["score"]["question_id"] for e in scores] == ["s1", "s2", "s3"]
    assert all(set(e["score"]) >= {"question_id", "section_title", "scores",
                                   "followup_asked"} for e in scores)
    # non-empty spoken answers still produce candidate turn events
    cand_turns = [e for e in events
                  if e["type"] == "turn" and e["role"] == "candidate"]
    assert len(cand_turns) == 3
    # exactly one summary packet, compact: {state, scores, stats}, no turns
    summaries = [e for e in events if e["type"] == "summary"]
    assert len(summaries) == 1
    assert set(summaries[0]["summary"]) == {"state", "scores", "stats"}
    assert summaries[0]["summary"]["state"] == "wrap"
    assert summaries[0]["summary"]["stats"]["questions_asked"] == 3


def test_answer_timeout_reprompts_once_then_moves_on():
    """T2: a candidate that never speaks must not hang the interview — one
    spoken re-prompt per question, then the question is scored as unanswered
    (empty answer) and the FSM reaches wrap."""
    from interviewer.voice.livekit import LiveKitCandidate

    rag = StubRag(["Rate limiter", "Consistent hashing"])
    llm = StubLLM(
        ["Welcome to your system design interview.",      # greeting
         "Design a rate limiter for a public API.",       # q1
         "Sorry, I did not catch that. Could you repeat?",  # re-prompt q1
         "Explain consistent hashing.",                   # q2
         "Sorry, I did not catch that. Could you repeat?",  # re-prompt q2
         "Closing remarks. Your average score was 2.0."],  # wrap
        [EVAL_NONE, EVAL_NONE])
    events: list[dict] = []

    async def collect(ev: dict) -> None:
        events.append(ev)

    interviewer = LLMInterviewer(
        rag, llm, Session(session_id="silent", tenant_id="default",
                          domain="system-design"),
        answer_timeout_s=0.05, on_event=collect)
    summary = run(interviewer.run("bank-sd", candidate=LiveKitCandidate()))

    # no deadlock: wrap reached, every question scored (as unanswered)
    assert summary["state"] == InterviewerState.WRAP.value
    assert len(summary["scores"]) == 2
    assert summary["stats"]["wall_ms"] < 30_000
    # one re-prompt per question, spoken and shown in the transcript
    reprompts = [e for e in events
                 if e["type"] == "turn" and e.get("stage") == "reprompt"]
    assert len(reprompts) == 2
    # silence produced no candidate turns
    assert not any(e.get("role") == "candidate" for e in events)
    # unanswered questions went through the judge (evaluating phases exist)
    phases = [e["phase"] for e in events if e["type"] == "state"]
    assert phases.count("evaluating") == 2


# ── Manual answer review gate (Retake / Next) ────────────────────────────────
# After every scored question the brain pauses at the review gate when a
# decider is present: "retake" re-asks the same question and replaces its
# score; "next" (or a gate timeout) advances. With no decider the flow is
# byte-for-byte the original automated one.

EVAL_NONE2 = EVAL_NONE  # readability alias


class _FakeDecider:
    """Canned review decisions, recorded per gate."""

    def __init__(self, decisions: list[str]):
        self._decisions = list(decisions)
        self.calls: list[str] = []

    async def decide(self, question_id: str,
                     timeout_s: float | None = None) -> str:
        self.calls.append(question_id)
        return self._decisions.pop(0) if self._decisions else "next"


def test_review_gate_retake_replaces_score_then_next():
    """A Retake re-asks the same question (same question_id turn) and its new
    score replaces the old ledger entry; Next advances to the next question."""
    rag = StubRag(["Rate limiter", "Consistent hashing"])
    # speech script: greeting, q1, q1-retake, q2, wrap
    llm = StubLLM(
        ["Welcome to your system design interview.",
         "Design a rate limiter for a public API.",
         "Design a rate limiter for a public API.",     # retake re-ask
         "Explain consistent hashing.",
         "Closing remarks. Your average score was 4.0."],
        [EVAL_NONE2, EVAL_NONE2, EVAL_NONE2])            # 3 judged attempts
    events: list[dict] = []

    async def collect(ev: dict) -> None:
        events.append(ev)

    decider = _FakeDecider(["retake", "next", "next"])
    interviewer = LLMInterviewer(
        rag, llm, Session(session_id="gate", tenant_id="default",
                          domain="system-design"),
        decider=decider, on_event=collect)
    summary = run(interviewer.run("bank-sd", answers={
        "s1": "token bucket with redis counters",
        "s2": "virtual nodes minimize reshuffling",
    }))

    assert summary["state"] == InterviewerState.WRAP.value
    # the gate saw every question, and q1's retake came back before advancing
    assert decider.calls == ["s1", "s1", "s2"]
    # retake replaced the entry: one score per question, in order
    assert [e["question_id"] for e in summary["scores"]] == ["s1", "s2"]
    # the question was asked twice (same question_id turn), then q2 once
    q_turns = [e["question_id"] for e in events
               if e.get("stage") == "question"]
    assert q_turns == ["s1", "s1", "s2"]
    # review phases shown at each gate; score events for every attempt
    phases = [e["phase"] for e in events if e["type"] == "state"]
    assert phases.count("review") == 3
    assert [e["score"]["question_id"] for e in events
            if e["type"] == "score"] == ["s1", "s1", "s2"]


def test_no_decider_keeps_automated_flow():
    """Text mode / headless runs pass no decider: no review phase at all."""
    rag = StubRag(["Rate limiter"])
    llm = StubLLM(
        ["Welcome to your system design interview.",
         "Design a rate limiter for a public API.",
         "Closing remarks. Your average score was 4.0."],
        [EVAL_NONE2])
    events: list[dict] = []

    async def collect(ev: dict) -> None:
        events.append(ev)

    interviewer = LLMInterviewer(
        rag, llm, Session(session_id="autof", tenant_id="default",
                          domain="system-design"),
        on_event=collect)
    summary = run(interviewer.run("bank-sd", answers={
        "s1": "token bucket with redis counters"}))

    assert summary["state"] == InterviewerState.WRAP.value
    assert not any(e.get("phase") == "review" for e in events)


class _ProbeCandidate:
    """Records how the brain bounds each answer wait."""

    def __init__(self):
        self.calls: list[tuple[float | None, float | None]] = []

    async def answer(self, question_id: str, timeout_s: float | None = None,
                     drop_before_ts: float | None = None):
        from interviewer.brain import CandidateAnswer
        self.calls.append((timeout_s, drop_before_ts))
        raise TimeoutError()


def test_listen_second_attempt_keeps_reprompt_window():
    """F1: the re-prompt's answer must not be drained as barge-in junk — the
    second attempt passes a drop_before_ts so only pre-re-prompt transcripts
    are discarded."""
    rag = StubRag(["Rate limiter"])
    llm = StubLLM(
        ["Welcome to your system design interview.",
         "Design a rate limiter for a public API.",
         "Sorry, I did not catch that. Could you repeat?",
         "Closing remarks. Your average score was 2.0."],
        [EVAL_NONE2])
    probe = _ProbeCandidate()
    interviewer = LLMInterviewer(
        rag, llm, Session(session_id="f1", tenant_id="default",
                          domain="system-design"),
        answer_timeout_s=0.05)
    summary = run(interviewer.run("bank-sd", candidate=probe))

    assert summary["state"] == InterviewerState.WRAP.value
    assert len(probe.calls) == 2
    first_timeout, first_drop = probe.calls[0]
    second_timeout, second_drop = probe.calls[1]
    assert second_drop is not None and first_drop is None
    assert second_drop >= 0 and second_timeout == 0.05


def test_score_event_carries_feedback():
    """Per-question feedback (verdict + model answer) rides on the score
    event so the review gate can show Correct / Partial / Incorrect + the
    model answer without any extra LLM round-trip."""
    rag = StubRag(["Rate limiter"])
    llm = StubLLM(
        ["Welcome.", "Design a rate limiter.", "Closing. 4.0."],
        ["Correctness: 4\nDepth: 3\nCommunication: 4\n"
         "Verdict: partial\n"
         "Model answer: Use a token bucket with Redis counters.\n"
         "FOLLOW_UP: none"])
    events: list[dict] = []

    async def collect(ev: dict) -> None:
        events.append(ev)

    interviewer = LLMInterviewer(
        rag, llm, Session(session_id="fb", tenant_id="default",
                          domain="system-design"),
        on_event=collect)
    summary = run(interviewer.run("bank-sd", answers={
        "s1": "token bucket"}))

    score_ev = [e for e in events if e["type"] == "score"]
    assert len(score_ev) == 1
    payload = score_ev[0]["score"]
    assert payload["verdict"] == "partial"
    assert payload["model_answer"] == "Use a token bucket with Redis counters."
    assert payload["gap"]
    # ledger row carries the same feedback for review after the call
    assert summary["scores"][0]["verdict"] == "partial"
    assert summary["scores"][0]["model_answer"] == "Use a token bucket with Redis counters."
