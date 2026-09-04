"""Markdown knowledge-base prepopulation and runtime bank registration.

Builds the retrieval DBs from a markdown corpus (one ``## `` heading = one
section) into the configured vector + keyword legs. This is the reproducible
"build dbs and prepopulate data if it doesn't exist" path for standalone
deployments:

- marker validation gates (expected markers required, blocked markers fatal)
- idempotent by default: skips when chunks for the doc are already present
  (``--force`` replaces them via delete_by_parent + re-upsert)
- deterministic chunk ids ``{doc_id}:s{section}:c{chunk}`` (1-based) so a
  rebuild from the same corpus is stable

The text-first core (``split_markdown_text`` + ``_ingest_markdown``) is shared
by the path-based CLI (``prepopulate``) and the runtime MCP registration tool
(``register_bank``, which ingests markdown content in-process — no file, no
service restart).
"""
import argparse
import asyncio
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from enterprise_rag.model import UpsertRecord

_SECTION_RE = re.compile(r"(?m)^## ")
# Runtime-registration naming contract: a bank doc id is ``bank-<slug>`` and
# its department scope is the same slug (the interview domain).
_DOC_ID_RE = re.compile(r"^bank-[a-z0-9][a-z0-9-]*$")
_DEPARTMENT_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def split_markdown_text(text: str) -> list[tuple[str, str]]:
    """Splits markdown corpus text on ``## `` headings. Front matter (everything
    before the first heading) is dropped; sections with empty bodies are
    skipped. Returns ``[(heading, body), ...]``."""
    parts = _SECTION_RE.split(text)
    out: list[tuple[str, str]] = []
    for part in parts[1:]:      # parts[0] is the pre-first-heading front matter
        lines = part.splitlines()
        heading = lines[0].strip() if lines else ""
        body = "\n".join(lines[1:]).strip()
        if body:
            out.append((heading, body))
    return out


def split_markdown_sections(kb_path: str | Path) -> list[tuple[str, str]]:
    """Path-based twin of :func:`split_markdown_text` (the CLI corpus reader)."""
    return split_markdown_text(Path(kb_path).read_text(encoding="utf-8"))


def chunk_text_with_overlap(text: str, size: int = 600, overlap: int = 90) -> list[str]:
    """Deterministic paragraph-packing chunker with word-boundary overlap.
    Mirrors the legacy (600, 90) splitter's intent; chunk boundaries are NOT
    byte-identical to LangChain's RecursiveCharacterTextSplitter (stated as an
    explicit non-goal in the integration TRD)."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        return []
    pieces: list[str] = []
    current = ""
    for para in paragraphs:
        if not current:
            current = para
        elif len(current) + 2 + len(para) <= size:
            current = f"{current}\n\n{para}"
        else:
            pieces.append(current)
            current = para
    pieces.append(current)
    if overlap <= 0 or len(pieces) == 1:
        return pieces
    out = [pieces[0]]
    for prev, piece in zip(pieces, pieces[1:]):
        tail = prev[-overlap:]
        cut = tail.find(" ")
        tail = tail[cut + 1:] if cut != -1 else ""
        out.append(f"{tail} {piece}".strip() if tail else piece)
    return out


@dataclass
class PrepopulateResult:
    doc_id: str
    tenant_id: str
    sections: int
    chunks: int
    skipped: bool = False


async def _ingest_markdown(stack, text: str, *, doc_id: str, tenant_id: str,
                           department: str | None, clearance: int,
                           expected_markers: list[str] | None,
                           blocked_markers: list[str] | None, force: bool,
                           chunk_size: int, chunk_overlap: int) -> PrepopulateResult:
    """Validates, chunks, embeds, and upserts one markdown corpus into both legs.

    Raises ValueError on marker validation failure. Idempotent: when chunks
    for ``doc_id`` already exist (same tenant) and ``force`` is false, nothing
    is written and ``skipped=True`` is returned. Force replaces on both legs
    (vector ``delete_by_parent`` then re-upsert, plus keyword delete so the
    in-memory sparse leg cannot retain superseded rows)."""
    low = text.lower()
    if expected_markers and not any(m.lower() in low for m in expected_markers):
        raise ValueError(f"expected markers missing: {expected_markers}")
    if blocked_markers:
        hits = [m for m in blocked_markers if m.lower() in low]
        if hits:
            raise ValueError(f"blocked markers present: {hits}")

    sections = split_markdown_text(text)
    existing = await stack.vector_store.get_all(tenant_id)
    doc_chunks = [c for c in existing if c.parent_id == doc_id]
    if doc_chunks and not force:
        return PrepopulateResult(
            doc_id=doc_id, tenant_id=tenant_id, sections=len(sections),
            chunks=len(doc_chunks), skipped=True,
        )

    await stack.vector_store.delete_by_parent(doc_id, tenant_id)
    await stack.keyword_store.delete_by_parent(doc_id, tenant_id)
    records: list[UpsertRecord] = []
    for si, (heading, body) in enumerate(sections, start=1):
        for ci, piece in enumerate(
                chunk_text_with_overlap(body, chunk_size, chunk_overlap), start=1):
            vector = await stack.embeddings.embed(piece)
            records.append(UpsertRecord(
                chunk_id=f"{doc_id}:s{si}:c{ci}",
                parent_id=doc_id,
                tenant_id=tenant_id,
                content=piece,
                section_title=heading,
                required_clearance=clearance,
                department=department,
                vector=vector,
            ))
    await stack.vector_store.upsert(records)
    await stack.keyword_store.upsert(records)
    return PrepopulateResult(
        doc_id=doc_id, tenant_id=tenant_id, sections=len(sections),
        chunks=len(records),
    )


async def prepopulate(stack, kb_path: str | Path, *, doc_id: str = "meridian-kb",
                      tenant_id: str = "default", department: str | None = None,
                      clearance: int = 0, expected_markers: list[str] | None = None,
                      blocked_markers: list[str] | None = None, force: bool = False,
                      chunk_size: int = 600, chunk_overlap: int = 90) -> PrepopulateResult:
    """Prepopulate one markdown KB file (the reproducible CLI/build path)."""
    text = Path(kb_path).read_text(encoding="utf-8")
    return await _ingest_markdown(
        stack, text, doc_id=doc_id, tenant_id=tenant_id, department=department,
        clearance=clearance, expected_markers=expected_markers,
        blocked_markers=blocked_markers, force=force,
        chunk_size=chunk_size, chunk_overlap=chunk_overlap,
    )


async def register_bank(stack, *, markdown: str, doc_id: str, department: str,
                        tenant_id: str = "default", clearance: int = 0,
                        expected_markers: list[str] | None = None,
                        blocked_markers: list[str] | None = None,
                        force: bool = False, min_sections: int = 1,
                        chunk_size: int = 600,
                        chunk_overlap: int = 90) -> PrepopulateResult:
    """Register one interview question bank from markdown content (runtime tool).

    Validates the naming contract (``bank-<slug>`` doc id, slug department),
    that the corpus parses into at least ``min_sections`` non-empty sections,
    then ingests into both legs in-process — the bank is immediately queryable
    (vector + BM25) without a service restart. Idempotent skip and force
    semantics identical to :func:`prepopulate`.

    Raises ValueError on any validation failure."""
    if not markdown or not markdown.strip():
        raise ValueError("markdown corpus is empty")
    if not _DOC_ID_RE.match(doc_id or ""):
        raise ValueError(
            f"doc_id {doc_id!r} invalid — must match ^bank-[a-z0-9][a-z0-9-]*$")
    if not _DEPARTMENT_RE.match(department or ""):
        raise ValueError(
            f"department {department!r} invalid — must match ^[a-z0-9][a-z0-9-]*$")
    sections = split_markdown_text(markdown)
    if len(sections) < min_sections:
        raise ValueError(
            f"corpus has {len(sections)} section(s); at least {min_sections} "
            "non-empty '## ' section(s) required")
    return await _ingest_markdown(
        stack, markdown, doc_id=doc_id, tenant_id=tenant_id, department=department,
        clearance=clearance, expected_markers=expected_markers,
        blocked_markers=blocked_markers, force=force,
        chunk_size=chunk_size, chunk_overlap=chunk_overlap,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="enterprise-rag-core prepopulate",
        description="Build/prepopulate the retrieval DBs from a markdown KB "
                    "(idempotent — skips when the doc is already present).",
    )
    parser.add_argument("--kb", required=True, help="Path to the markdown knowledge base")
    parser.add_argument("--doc-id", default="meridian-kb",
                        help="Deterministic doc id; chunk ids derive from it")
    parser.add_argument("--tenant", default="default", help="Tenant the doc belongs to")
    parser.add_argument("--department", default=None, help="Optional department scope")
    parser.add_argument("--clearance", type=int, default=0, help="Required clearance (default 0)")
    parser.add_argument("--required-marker", action="append", default=[],
                        help="Substring that must appear in the corpus (repeatable)")
    parser.add_argument("--blocked-marker", action="append", default=[],
                        help="Substring that must NOT appear (fatal if present; repeatable)")
    parser.add_argument("--force", action="store_true",
                        help="Replace existing chunks instead of skipping")
    parser.add_argument("--chunk-size", type=int, default=600)
    parser.add_argument("--chunk-overlap", type=int, default=90)
    return parser


def cmd_prepopulate(args: argparse.Namespace) -> int:
    """Console-command handler (wired by cli.py). Builds the stack from env,
    runs prepopulate, closes SDK clients."""
    from enterprise_rag.config import EngineConfig

    stack = EngineConfig.from_env().build_stack()
    try:
        result = asyncio.run(prepopulate(
            stack, args.kb, doc_id=args.doc_id, tenant_id=args.tenant,
            department=args.department, clearance=args.clearance,
            expected_markers=args.required_marker or None,
            blocked_markers=args.blocked_marker or None,
            force=args.force, chunk_size=args.chunk_size,
            chunk_overlap=args.chunk_overlap,
        ))
        if result.skipped:
            print(f"prepopulate skipped: {result.doc_id} already present "
                  f"({result.chunks} chunks) — use --force to rebuild")
        else:
            print(f"prepopulated {result.doc_id}: {result.sections} sections, "
                  f"{result.chunks} chunks -> tenant={result.tenant_id}")
        return 0
    except ValueError as exc:
        print(f"prepopulate ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        asyncio.run(stack.aclose())


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m enterprise_rag.prepopulate``."""
    return cmd_prepopulate(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
