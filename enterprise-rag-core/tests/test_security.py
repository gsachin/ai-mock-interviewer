"""SecurityContext parity across all four filter implementations: the Qdrant
Filter, the Elasticsearch clauses, the ChromaDB ``where``, and the pure
post-filter predicate must make identical allow/deny decisions on the same
(chunk, principal) pairs (design doc §1: identical, unbypassable filters on
both legs — extended to every backend)."""
import pytest

from enterprise_rag.model import Chunk
from enterprise_rag.security import SecurityContext
from enterprise_rag.adapters.qdrant_vector import build_qdrant_filter
from enterprise_rag.adapters.elasticsearch_keyword import build_es_filter
from enterprise_rag.adapters.chroma_vector import build_chroma_where


def chunk(tenant="acme", clearance=3, department="engineering", cid="c"):
    return Chunk(
        chunk_id=cid, parent_id="p", tenant_id=tenant, content="x",
        required_clearance=clearance, department=department,
    )


def sec(tenant="acme", departments=None, clearance=3):
    return SecurityContext("u1", tenant, ["interviewer"], departments or [], clearance, [])


# (chunk, principal) -> allowed?
CASES = [
    ("same tenant, clearance ok, dept listed", chunk(), sec(departments=["engineering"]), True),
    ("same tenant, clearance ok, no departments (not dept-locked)",
     chunk(), sec(departments=[]), True),
    ("same tenant, clearance ok, wrong department", chunk(),
     sec(departments=["sales"]), False),
    ("clearance below required", chunk(), sec(clearance=2), False),
    ("clearance exactly required", chunk(), sec(clearance=3), True),
    ("cross-tenant", chunk(tenant="evilcorp"), sec(), False),
]


def qdrant_allows(chunk_, sec_):
    """Mirror what Qdrant's filter engine does on a single point."""
    f = build_qdrant_filter(sec_)
    if any(m.key == "tenant_id" and m.match.value != chunk_.tenant_id for m in f.must):
        return False
    if any(m.key == "required_clearance" and chunk_.required_clearance > m.range.lte
           for m in f.must):
        return False
    if f.should is not None:
        if not any(m.key == "department" and m.match.value == chunk_.department
                   for m in f.should):
            return False
    return True


def es_allows(chunk_, sec_):
    clauses = build_es_filter(sec_)["bool"]["filter"]
    for clause in clauses:
        if "term" in clause and clause["term"].get("tenant_id.keyword") != chunk_.tenant_id:
            return False
        if "range" in clause and chunk_.required_clearance > clause["range"]["required_clearance"]["lte"]:
            return False
        if "terms" in clause and chunk_.department not in clause["terms"].get("department.keyword", []):
            return False
    return True


def chroma_allows(chunk_, sec_):
    where = build_chroma_where(sec_)
    for clause in where["$and"]:
        if "tenant_id" in clause and clause["tenant_id"]["$eq"] != chunk_.tenant_id:
            return False
        if "required_clearance" in clause and chunk_.required_clearance > clause["required_clearance"]["$lte"]:
            return False
        if "department" in clause and chunk_.department not in clause["department"]["$in"]:
            return False
    return True


@pytest.mark.parametrize("label,chunk_,sec_,expected", CASES)
def test_filter_parity(label, chunk_, sec_, expected):
    assert qdrant_allows(chunk_, sec_) == expected, label
    assert es_allows(chunk_, sec_) == expected, label
    assert chroma_allows(chunk_, sec_) == expected, label
    assert sec_.matches(chunk_) == expected, label


def test_chroma_where_shape_empty_departments():
    where = build_chroma_where(sec(departments=[]))
    assert len(where["$and"]) == 2, f"{where}"


def test_chroma_where_shape_with_departments():
    where = build_chroma_where(sec(departments=["hr", "eng"]))
    assert len(where["$and"]) == 3, f"{where}"
    assert where["$and"][2]["department"]["$in"] == ["hr", "eng"]


def test_security_context_is_frozen():
    s = sec()
    with pytest.raises(Exception):
        s.tenant_id = "x"  # type: ignore[misc]
