"""§1 Enterprise Security & Multi-Tenancy (design doc §1.1, made backend-pure).

The SecurityContext is the single source of truth for identity-derived
authorization. It is frozen (no downstream code can widen scope mid-request)
and carries no SDK imports: each backend adapter translates it into its own
filter dialect (Qdrant Filter, Elasticsearch bool.filter, ChromaDB ``where``,
or a post-filter predicate via :meth:`SecurityContext.matches`).
"""
from dataclasses import dataclass

from enterprise_rag.model import Chunk


@dataclass(frozen=True)
class SecurityContext:
    """Immutable per-request security context, derived strictly from the
    authenticated JWT (or from configuration in ``none`` auth mode).

    Deny-by-default claim mapping: a missing ``tenant_id`` maps to ``""``,
    which matches no chunk in any backend.
    """
    principal_id: str
    tenant_id: str
    roles: list[str]
    departments: list[str]
    clearance_level: int
    allowed_groups: list[str]

    def matches(self, chunk: Chunk) -> bool:
        """Post-filter predicate for backends without server-side filters
        (in-memory BM25 / vector stores). Semantics are the exact parity of the
        Qdrant filter: tenant equality, ``required_clearance <= clearance``
        (NOT inverted), and department membership only when ``departments`` is
        non-empty — a principal with no departments is NOT department-locked.
        """
        if chunk.tenant_id != self.tenant_id:
            return False
        if chunk.required_clearance > self.clearance_level:
            return False
        if self.departments and chunk.department not in self.departments:
            return False
        return True
