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

Score 1-5 on correctness, depth, and communication, each with one line of
justification. End with "FOLLOW_UP:" plus one short question if any key
concept is missing, otherwise "FOLLOW_UP: none"."""


def build_evaluation_prompt(question: str, answer: str, context: str) -> str:
    return EVALUATION_PROMPT.format(question=question, answer=answer, context=context)
