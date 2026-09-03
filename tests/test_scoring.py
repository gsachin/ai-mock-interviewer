"""parse_evaluation: score extraction, follow-up detection, edge cases."""
from interviewer.scoring import parse_evaluation

GOOD = """Correctness: 4 - covers the core mechanism.
Depth: 3 - misses trade-offs.
Communication: 4 - clear and structured.
FOLLOW_UP: none"""


def test_parse_full_evaluation():
    ev = parse_evaluation(GOOD)
    assert ev.scores == {"correctness": 4, "depth": 3, "communication": 4}
    assert ev.followup is None
    assert "covers the core mechanism" in ev.justifications


def test_parse_followup_request():
    ev = parse_evaluation(
        "Correctness: 2 - wrong algorithm.\n"
        "Depth: 2\nCommunication: 3\n"
        "FOLLOW_UP: What is the difference between token bucket and sliding window?"
    )
    assert ev.followup == "What is the difference between token bucket and sliding window?"
    assert "FOLLOW_UP" not in ev.justifications


def test_parse_accepts_missing_dimensions():
    ev = parse_evaluation("Correctness: 5\nFOLLOW_UP: none")
    assert ev.scores == {"correctness": 5}
    assert ev.followup is None


def test_parse_out_of_range_scores_ignored():
    ev = parse_evaluation("Correctness: 9\nDepth: 2\nFOLLOW_UP: none")
    assert ev.scores == {"depth": 2}


# ── Verdict + Model answer (per-question feedback, 2026-09-03) ───────────────

def test_parse_verdict_and_model_answer():
    ev = parse_evaluation(
        "Correctness: 5 - covers everything.\nDepth: 4\nCommunication: 4\n"
        "Justification: misspelled cache.\n"
        "Verdict: correct\n"
        "Model answer: Use a token bucket with Redis counters; refill atoms.\n"
        "FOLLOW_UP: none")
    assert ev.verdict == "correct"
    assert "token bucket" in ev.model_answer
    assert "Redis counters" in ev.model_answer
    # structural keys are not part of the justification text
    assert "Verdict" not in ev.justifications
    assert "Model answer" not in ev.justifications


def test_parse_multiline_model_answer_stops_at_followup():
    ev = parse_evaluation(
        "Correctness: 2\nDepth: 2\nCommunication: 3\n"
        "Verdict: partial\n"
        "Model answer: Shard the counters by key. Replicate for availability. "
        "Trade exact limits for latency.\n"
        "FOLLOW_UP: what about sharding?")
    assert ev.verdict == "partial"
    assert ev.model_answer.startswith("Shard the counters")
    assert "FOLLOW_UP" not in ev.model_answer
    assert ev.followup == "what about sharding?"


def test_verdict_derived_when_judge_omits_it():
    ev = parse_evaluation(
        "Correctness: 5\nDepth: 5\nCommunication: 5\nFOLLOW_UP: none")
    assert ev.verdict is None                     # not in the judge text
    from interviewer.scoring import verdict_from_scores
    assert verdict_from_scores(ev.scores) == "correct"
    assert verdict_from_scores({"correctness": 3, "depth": 2,
                                "communication": 3}) == "partial"
    assert verdict_from_scores({"correctness": 1, "depth": 1,
                                "communication": 2}) == "incorrect"


def test_parse_partially_correct_variant():
    ev = parse_evaluation(
        "Verdict: partially correct\n"
        "Model answer: Some answer here.\nFOLLOW_UP: none")
    assert ev.verdict == "partial"
