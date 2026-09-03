"""Evaluation parsing — turns the LLM's evaluation text into a machine-
readable score ledger entry (Phase 2 scoring, LLM-judge against the
retrieved rubric)."""
import re
from dataclasses import dataclass, field

_SCORE_RE = re.compile(r"\b(correctness|depth|communication)\s*[:：]\s*([1-5])\b", re.I)
_FOLLOWUP_RE = re.compile(r"FOLLOW_UP:\s*(.*)", re.I)
_VERDICT_RE = re.compile(
    r"\bVerdict\s*[:：]\s*(correct|incorrect|wrong|not\s+correct|"
    r"partial(?:ly)?(?:\s+correct)?)", re.I)
_KEY_LINE_RE = re.compile(
    r"\n\s*(?:Correctness|Depth|Communication|Justification|Verdict|"
    r"Model\s+answer|FOLLOW_UP)\s*[:：]")

SCORE_DIMENSIONS = ("correctness", "depth", "communication")

# Page-facing verdict strings (also speech-friendly).
VERDICT_CORRECT = "correct"
VERDICT_PARTIAL = "partial"
VERDICT_INCORRECT = "incorrect"


def verdict_from_scores(scores: dict[str, int]) -> str:
    """Deterministic fallback when the judge omits the Verdict line."""
    if not scores:
        return VERDICT_PARTIAL
    avg = sum(scores.values()) / len(scores)
    if avg >= 4.0:
        return VERDICT_CORRECT
    if avg >= 2.5:
        return VERDICT_PARTIAL
    return VERDICT_INCORRECT


def _normalise_verdict(token: str) -> str:
    t = token.strip().lower()
    if "partial" in t:
        return VERDICT_PARTIAL
    if "incorrect" in t or "wrong" in t or t.startswith("not correct"):
        return VERDICT_INCORRECT
    return VERDICT_CORRECT


@dataclass(frozen=True)
class Evaluation:
    scores: dict[str, int] = field(default_factory=dict)   # dimension -> 1..5
    justifications: str = ""
    followup: str | None = None                             # None = satisfied
    verdict: str | None = None            # correct | partial | incorrect
    model_answer: str = ""                # the judge's 2-4 sentence ideal answer


def parse_evaluation(text: str) -> Evaluation:
    """Parses the EVALUATION_PROMPT response: 1-5 scores per dimension, an
    optional ``FOLLOW_UP:`` line (``none``/empty = satisfied), an optional
    ``Verdict:`` line and an optional ``Model answer:`` paragraph. Everything
    the page needs (verdict + correct answer) comes from the same judge call
    — no extra LLM round-trip."""
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

    vm = _VERDICT_RE.search(text)
    verdict = _normalise_verdict(vm.group(1)) if vm else None

    # Model answer: the paragraph after "Model answer:" up to the next key.
    model_answer = ""
    mm = re.search(r"(?i)Model answer\s*[:：]\s*(.*)", text, re.S)
    if mm:
        body = mm.group(1)
        cut = _KEY_LINE_RE.search(body)
        if cut:
            body = body[:cut.start()]
        model_answer = body.strip().strip('"').strip()

    # Justifications: the text without the structural keys' content blocks.
    justifications = text
    if mm:
        justifications = (justifications[:mm.start()] + " " +
                          justifications[mm.end():])
    justifications = _VERDICT_RE.sub("", justifications)
    justifications = _FOLLOWUP_RE.sub("", justifications)
    justifications = re.sub(r"\s+", " ", justifications).strip()
    return Evaluation(scores=scores, justifications=justifications,
                      followup=followup, verdict=verdict,
                      model_answer=model_answer)
