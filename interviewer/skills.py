"""Question-bank (skill) discovery and upload validation, consumer side.

The ``question_banks/*.md`` folder is the single source of truth for which
skills can be interviewed: every ``*.md`` whose stem is a valid slug names a
skill — RAG doc id ``bank-<name>``, department ``<name>`` (one ``## `` heading
per question, the format the RAG ``register_bank`` tool ingests).

This module only reads the folder and validates uploads — registration state
lives in the RAG service and is probed separately (server endpoints call
``RagClient.interview_bank`` / ``RagClient.register_bank``). Stdlib only: the
root venv never imports enterprise-rag-core, so the shape parser mirrors the
RAG splitter's rules with a small regex instead of importing them.
"""
import re
from dataclasses import dataclass
from pathlib import Path

# Skill name == bank file stem: lowercase slug, alnum start, no trailing dash.
SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SECTION_RE = re.compile(r"(?m)^## ")

BANK_DIR: Path = Path(__file__).resolve().parent.parent / "question_banks"


@dataclass(frozen=True)
class LocalBank:
    """One question-bank file found in the question_banks folder."""

    name: str
    doc_id: str
    path: Path


def discover_local_banks(bank_dir: Path = BANK_DIR) -> list[LocalBank]:
    """Every valid *.md question bank in the folder, sorted by name. Files
    whose stems are not valid skill slugs are ignored (see
    ``unusable_bank_files`` for surfacing them)."""
    banks = []
    for path in sorted(bank_dir.glob("*.md")):
        name = path.stem.lower()
        if SKILL_NAME_RE.match(name):
            banks.append(LocalBank(name=name, doc_id=bank_doc_id(name),
                                   path=path))
    return banks


def unusable_bank_files(bank_dir: Path = BANK_DIR) -> list[Path]:
    """*.md files in the folder that cannot name a skill (bad slug) — the
    Skill Update page lists them as warnings instead of silently hiding."""
    return sorted(
        p for p in bank_dir.glob("*.md")
        if not SKILL_NAME_RE.match(p.stem.lower())
    )


def normalize_skill_name(filename: str) -> str:
    """Filename (or user-entered name) → the skill slug the bank registers
    under. Accepts ``<slug>`` or ``<slug>.md`` (extension stripped,
    case-insensitive). Raises ValueError for anything that is not a single
    path-free slug."""
    raw = (filename or "").strip()
    if not raw:
        raise ValueError("missing skill file name")
    if "/" in raw or "\\" in raw or raw.startswith(".") or ".." in raw:
        raise ValueError(f"invalid file name {raw!r} — no paths allowed")
    name = raw[:-3].lower() if raw.lower().endswith(".md") else raw.lower()
    if not SKILL_NAME_RE.match(name) or name.endswith("-"):
        raise ValueError(
            f"invalid skill name {name!r} — use lowercase letters, digits and "
            "single dashes (e.g. system-design, dsa)")
    return name


def bank_doc_id(name: str) -> str:
    """The RAG doc id a skill registers under."""
    return f"bank-{name}"


def parse_markdown_shape(text: str) -> tuple[list[str], list[str]]:
    """Mirror of the RAG corpus splitter — front matter (everything before the
    first ``## `` heading) dropped, one ``## `` heading per question, empty
    bodies skipped — plus a validation pass.

    Returns ``(headings, errors)`` where ``headings`` lists the section
    headings that WOULD be registered (the count the RAG tool reports) and
    ``errors`` lists problems that make the corpus un-registerable."""
    parts = _SECTION_RE.split(text or "")
    if len(parts) < 2:
        return [], ["no '## ' question sections found — the file needs one "
                    "'## ' heading per question"]
    headings: list[str] = []
    errors: list[str] = []
    for part in parts[1:]:
        lines = part.splitlines()
        heading = lines[0].strip() if lines else ""
        body = "\n".join(lines[1:]).strip()
        if not heading:
            errors.append("a question section has an empty heading")
        elif not body:
            errors.append(f"section {heading!r} has no body — add the question "
                          "text and expected points")
        else:
            headings.append(heading)
    return headings, errors


def validate_upload(text: str, name: str) -> None:
    """Raises ValueError when a corpus cannot be registered as skill
    ``name``. Mirrors the gates the RAG register_bank tool enforces (naming,
    minimum non-empty sections) plus the repo's 'Expected points' convention
    (every question bank states what a good answer covers)."""
    if not name or not SKILL_NAME_RE.match(name):
        raise ValueError(
            f"invalid skill name {name!r} — use lowercase letters, digits and "
            "single dashes (e.g. system-design, dsa)")
    headings, errors = parse_markdown_shape(text)
    if errors:
        raise ValueError("; ".join(errors))
    if not headings:
        raise ValueError("the file contains no registerable question sections")
    if "expected points" not in (text or "").lower():
        raise ValueError(
            "the file must state 'Expected points' (what a good answer "
            "covers) under each '## ' question — this drives scoring")
