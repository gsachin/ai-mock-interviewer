"""Evaluation parsing — turns the LLM's evaluation text into a machine-
readable score ledger entry (Phase 2 scoring, LLM-judge against the
retrieved rubric)."""
import re
from dataclasses import dataclass, field

_SCORE_RE = re.compile(r"\b(correctness|depth|communication)\s*[:：]\s*([1-5])\b", re.I)
_FOLLOWUP_RE = re.compile(r"FOLLOW_UP:\s*(.*)", re.I)

SCORE_DIMENSIONS = ("correctness", "depth", "communication")


@dataclass(frozen=True)
class Evaluation:
    scores: dict[str, int] = field(default_factory=dict)   # dimension -> 1..5
    justifications: str = ""
    followup: str | None = None                             # None = satisfied


def parse_evaluation(text: str) -> Evaluation:
    """Parses the EVALUATION_PROMPT response: 1-5 scores per dimension plus
    an optional ``FOLLOW_UP:`` line (``none``/empty = satisfied)."""
    scores = {
        dim.lower(): int(value)
        for dim, value in _SCORE_RE.findall(text)
        if dim.lower() in SCORE_DIMENSIONS
    }
    m = _FOLLOWUP_RE.search(text)
    followup = None
    if m:
        candidate = m.group(1).strip()
        if candidate and candidate.lower() not in ("none", "n/a", "none."):
            followup = candidate
    justifications = _FOLLOWUP_RE.sub("", text).strip()
    return Evaluation(scores=scores, justifications=justifications, followup=followup)
