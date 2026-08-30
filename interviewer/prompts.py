"""Voice-optimized prompt templates (Phase 2 skeleton).

Rules for the spoken medium: one question per turn, short sentences, no
markdown, and the interviewer's knowledge comes from the RAG context — the
system prompt stays small so first-token latency stays low.
"""

MAX_SPOKEN_CHARS = 600   # spoken answers beyond this feel like a lecture

SYSTEM_PROMPT = """You are a technical interviewer running a {domain} mock interview over voice.
- Ask exactly ONE question per turn; keep every spoken line under {max_chars} characters.
- Speak plainly: no markdown, no lists, no code blocks — say code aloud.
- Ground technical claims in the provided context chunks; if the context is
  insufficient, say so and move on.
- After the candidate's answer: if a key point is missing or wrong, ask ONE
  short follow-up; otherwise evaluate silently and proceed.
- Never reveal scores mid-interview."""


def build_system_prompt(domain: str) -> str:
    return SYSTEM_PROMPT.format(domain=domain, max_chars=MAX_SPOKEN_CHARS)


QUESTION_PROMPT = """Context (chunks from the knowledge base):
{context}

Interview plan: {question_id}

Ask the next question as the interviewer. One question only."""


def build_question_prompt(context: str, question_id: str) -> str:
    return QUESTION_PROMPT.format(context=context, question_id=question_id)


EVALUATION_PROMPT = """Question: {question}
Candidate answer: {answer}
Rubric context:
{context}

Respond with exactly this structure, no markdown, nothing else:
Correctness: <1-5>
Depth: <1-5>
Communication: <1-5>
Justification: one sentence on the strongest gap.
FOLLOW_UP: one short question if any key concept is missing, otherwise: none"""


def build_evaluation_prompt(question: str, answer: str, context: str) -> str:
    return EVALUATION_PROMPT.format(question=question, answer=answer, context=context)


GREETING_PROMPT = (
    "Greet the candidate for a {domain} technical mock interview. One short "
    "spoken sentence, friendly and professional. No markdown, no lists."
)


def build_greeting_prompt(domain: str) -> str:
    return GREETING_PROMPT.format(domain=domain)


FOLLOWUP_PROMPT = (
    "Ask exactly ONE short spoken follow-up question about: {followup}. "
    "One sentence, no markdown."
)


def build_followup_prompt(followup: str) -> str:
    return FOLLOWUP_PROMPT.format(followup=followup)


WRAP_PROMPT = (
    "The interview is over. Give one short spoken closing sentence and state "
    "the average score out of 5 ({avg}). No markdown."
)


def build_wrap_prompt(avg: float) -> str:
    return WRAP_PROMPT.format(avg=round(avg, 1))
