"""Interview question-bank helpers (Phase 1 — retrieval-backed interviewer).

Question banks are markdown KBs prepopulated with ``prepopulate``: one
``## `` section = one interview question, chunk ids are deterministic
(``{doc_id}:s{section}:c{chunk}``), ``section_title`` carries the question
heading, and ``parent_id`` is the bank's ``doc_id``. These pure helpers
turn a tenant's chunks into question structures without touching any
backend — the MCP tools in ``server.py`` feed them from
``VectorStore.get_all``.
"""
import re
from dataclasses import dataclass

from enterprise_rag.model import Chunk

_CHUNK_ID_RE = re.compile(r"^(?P<doc_id>.*):s(?P<section>\d+):c(?P<chunk>\d+)$")


@dataclass(frozen=True)
class QuestionRef:
    """One question's identity within a bank (no content — the catalog row)."""
    question_id: str            # "s{section}" — deterministic per-doc id
    section_title: str
    chunk_count: int


@dataclass(frozen=True)
class Question:
    """One full question: its chunks in deterministic order."""
    question_id: str
    section_title: str
    chunks: list[Chunk]


def parse_chunk_position(chunk_id: str, doc_id: str) -> tuple[int, int] | None:
    """(section, chunk) position of a deterministic question-bank chunk id,
    or None when the id does not belong to ``doc_id``."""
    m = _CHUNK_ID_RE.match(chunk_id)
    if not m or m.group("doc_id") != doc_id:
        return None
    return int(m.group("section")), int(m.group("chunk"))


def group_questions(chunks: list[Chunk], doc_id: str) -> list[Question]:
    """Groups a bank's chunks into questions ordered by section; chunks of a
    section are ordered by chunk index (ids are deterministic, so a rebuild
    from the same corpus is stable)."""
    by_section: dict[int, list[tuple[int, Chunk]]] = {}
    titles: dict[int, str] = {}
    for chunk in chunks:
        pos = parse_chunk_position(chunk.chunk_id, doc_id)
        if pos is None:
            continue
        section, chunk_idx = pos
        by_section.setdefault(section, []).append((chunk_idx, chunk))
        titles.setdefault(section, chunk.section_title)
    return [
        Question(
            question_id=f"s{section}",
            section_title=titles[section],
            chunks=[c for _, c in sorted(by_section[section], key=lambda p: p[0])],
        )
        for section in sorted(by_section)
    ]


def question_refs(questions: list[Question]) -> list[QuestionRef]:
    """Catalog rows for a bank (content-free — safe to send as a menu)."""
    return [
        QuestionRef(q.question_id, q.section_title, len(q.chunks))
        for q in questions
    ]
