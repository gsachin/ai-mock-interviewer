"""§4 document ingestion (pypdf 6.14.2): PDF text + table extraction into the
dual representation — ``structured`` (structured JSON: headers + rows, for
precise key lookup) and ``markdown_payload`` (markdown visual matrix, for
semantic vector indexing) — then chunking, embedding, and idempotent upsert
into the configured vector + keyword legs.

Table detection heuristic: within a page's extracted text, a run of >= 2
consecutive lines whose cells align on >= 2-space column boundaries with a
consistent column count (>= 2 columns) is treated as a table — first line is
the header row. This is the honest capability boundary of pypdf (no layout
API): tables drawn with unaligned single-space cells come out as plain text.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from enterprise_rag.model import UpsertRecord

_COL_SPLIT_RE = re.compile(r"\s{2,}")


@dataclass
class PageBlock:
    """One extracted unit: a text paragraph or a table."""
    page_number: int
    content_type: str                      # "text" | "table"
    text: str = ""                         # paragraph text (content_type=text)
    headers: list[str] | None = None       # table header cells
    rows: list[list[str]] | None = None    # table body rows
    markdown_payload: str = ""
    structured: dict[str, Any] | None = None


def _to_markdown(headers: list[str], rows: list[list[str]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines)


def _table_at(lines: list[str], i: int) -> tuple[list[str], list[list[str]], int] | None:
    """If lines[i:] starts a table run, return (headers, rows, end_index)."""
    first_cols = [c for c in _COL_SPLIT_RE.split(lines[i].strip()) if c]
    if len(first_cols) < 2:
        return None
    headers, end = first_cols, i + 1
    rows: list[list[str]] = []
    while end < len(lines):
        line = lines[end].strip()
        if not line:
            break
        cols = [c for c in _COL_SPLIT_RE.split(line) if c]
        if len(cols) != len(headers):
            break
        rows.append(cols)
        end += 1
    if not rows:            # a lone aligned line is not a table
        return None
    return headers, rows, end


def extract_blocks(pdf_path: str | Path) -> list[PageBlock]:
    """Extracts per-page paragraphs and tables from a PDF."""
    from pypdf import PdfReader

    reader = PdfReader(str(pdf_path))
    blocks: list[PageBlock] = []
    for page_number, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        lines = [ln.rstrip() for ln in text.splitlines()]
        i = 0
        while i < len(lines):
            if not lines[i].strip():
                i += 1
                continue
            table = _table_at(lines, i)
            if table is not None:
                headers, rows, end = table
                md = _to_markdown(headers, rows)
                blocks.append(PageBlock(
                    page_number=page_number, content_type="table",
                    headers=headers, rows=rows,
                    markdown_payload=md,
                    structured={"headers": headers, "rows": rows},
                ))
                i = end
                continue
            para = []
            while i < len(lines) and lines[i].strip() and _table_at(lines, i) is None:
                para.append(lines[i].strip())
                i += 1
            text_para = " ".join(para)
            blocks.append(PageBlock(
                page_number=page_number, content_type="text",
                text=text_para, markdown_payload=text_para,
            ))
    return blocks


def _chunk_text(text: str, max_chars: int) -> list[str]:
    """Splits long text on whitespace boundaries near max_chars (no overlap)."""
    if len(text) <= max_chars:
        return [text]
    pieces: list[str] = []
    start = 0
    while start < len(text):
        stop = min(start + max_chars, len(text))
        if stop < len(text):
            cut = text.rfind(" ", start, stop)
            if cut > start:
                stop = cut
        pieces.append(text[start:stop].strip())
        start = stop
    return [p for p in pieces if p]


@dataclass
class IngestResult:
    doc_id: str
    tenant_id: str
    pages: int
    tables: int
    chunks: int


async def ingest(stack, pdf_path: str | Path, *, doc_id: str, tenant_id: str,
                 department: str | None = None, clearance: int = 0,
                 max_chunk_chars: int = 1200) -> IngestResult:
    """Extracts, chunks, embeds, and upserts one document. Idempotent:
    ``delete_by_parent(doc_id, tenant)`` runs first, so re-ingesting the same
    doc replaces its chunks (deterministic chunk ids, no duplicates)."""
    blocks = extract_blocks(pdf_path)

    records: list[UpsertRecord] = []
    for bi, block in enumerate(blocks):
        content = block.markdown_payload or block.text
        pieces = _chunk_text(content, max_chunk_chars)
        for pi, piece in enumerate(pieces):
            vector = await stack.embeddings.embed(piece)
            records.append(UpsertRecord(
                chunk_id=f"{doc_id}:p{block.page_number}:c{bi}x{pi}",
                parent_id=doc_id,
                tenant_id=tenant_id,
                content=piece,
                section_title=f"page {block.page_number}",
                required_clearance=clearance,
                department=department,
                vector=vector,
                structured=block.structured,
                markdown_payload=block.markdown_payload or None,
            ))

    await stack.vector_store.delete_by_parent(doc_id, tenant_id)
    await stack.vector_store.upsert(records)
    await stack.keyword_store.upsert(records)

    pages = max((b.page_number for b in blocks), default=0)
    tables = sum(1 for b in blocks if b.content_type == "table")
    return IngestResult(doc_id=doc_id, tenant_id=tenant_id, pages=pages,
                        tables=tables, chunks=len(records))
