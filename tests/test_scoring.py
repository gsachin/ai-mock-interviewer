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
