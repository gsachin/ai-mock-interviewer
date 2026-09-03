"""Shared data model (design doc §3, extended for pluggable backends)."""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Chunk:
    """A retrievable content unit. Frozen: tenant_id cannot be tampered with
    mid-pipeline; score updates create new instances via dataclasses.replace.

    Trailing fields (``section_title``, ``required_clearance``, ``department``)
    were added for the pluggable backends; they are positional-compatible with
    the design-doc shape, so all verified seed code keeps working unchanged.
    """
    chunk_id: str
    parent_id: str | None
    tenant_id: str
    content: str
    score: float = 0.0
    section_title: str = ""
    required_clearance: int = 0
    department: str | None = None


@dataclass(frozen=True)
class UpsertRecord:
    """An ingestion unit: content plus the vector already computed for it.

    ``content`` is the markdown matrix for tables, plain text otherwise;
    ``structured`` carries the dual structured-table JSON representation.
    """
    chunk_id: str
    parent_id: str
    tenant_id: str
    content: str
    section_title: str
    required_clearance: int
    department: str | None
    vector: list[float]
    structured: dict | None = None
    markdown_payload: str | None = None

    def to_chunk(self, score: float = 0.0) -> Chunk:
        return Chunk(
            chunk_id=self.chunk_id,
            parent_id=self.parent_id,
            tenant_id=self.tenant_id,
            content=self.content,
            score=score,
            section_title=self.section_title,
            required_clearance=self.required_clearance,
            department=self.department,
        )
