"""Qdrant empty-departments regression (revision-log row 4):
Filter(must=[...], should=None) returns results for a departments=[] principal;
the v2026.4 shape Filter(must=[...], should=[]) returns ZERO.
Converted from the seed verification harness."""
import uuid

import pytest

from qdrant_client import QdrantClient, models as qm

from enterprise_rag.security import SecurityContext
from enterprise_rag.adapters.qdrant_vector import build_qdrant_filter


@pytest.fixture(scope="module")
def qdrant_env():
    """In-memory Qdrant with one acme and one 'other' chunk, clearance 3."""
    id_acme = str(uuid.uuid4())
    id_other = str(uuid.uuid4())

    client = QdrantClient(":memory:")
    client.create_collection(
        collection_name="regression",
        vectors_config=qm.VectorParams(size=4, distance=qm.Distance.COSINE),
    )
    client.upsert(
        collection_name="regression",
        points=[
            qm.PointStruct(
                id=id_acme,
                vector=[0.1, 0.1, 0.1, 0.1],
                payload={
                    "tenant_id": "acme",
                    "required_clearance": 3.0,
                    "department": "engineering",
                    "content": "leadership rubric",
                },
            ),
            qm.PointStruct(
                id=id_other,
                vector=[0.2, 0.2, 0.2, 0.2],
                payload={
                    "tenant_id": "other",
                    "required_clearance": 3.0,
                    "department": "engineering",
                    "content": "foreign rubric",
                },
            ),
        ],
    )
    return client


def search_with(client, flt):
    return client.query_points(
        collection_name="regression",
        query=[0.1, 0.1, 0.1, 0.1],
        query_filter=flt,
        limit=10,
    ).points


def test_empty_departments_gets_results_with_should_none(qdrant_env):
    sec = SecurityContext("u1", "acme", ["interviewer"], [], 3, [])
    hits = search_with(qdrant_env, build_qdrant_filter(sec))
    assert len(hits) == 1, f"hits={[h.id for h in hits]}"
    assert hits[0].payload["tenant_id"] == "acme"


def test_v2026_4_should_empty_list_reproduces_lockout(qdrant_env):
    buggy = qm.Filter(
        must=[
            qm.FieldCondition(key="tenant_id", match=qm.MatchValue(value="acme")),
            qm.FieldCondition(key="required_clearance", range=qm.Range(lte=3.0)),
        ],
        should=[],
    )
    hits = search_with(qdrant_env, buggy)
    assert len(hits) == 0, f"hits={[h.id for h in hits]}"


def test_department_inclusion(qdrant_env):
    sec = SecurityContext("u1", "acme", ["interviewer"], ["engineering"], 3, [])
    hits = search_with(qdrant_env, build_qdrant_filter(sec))
    assert len(hits) == 1, f"{[h.id for h in hits]}"


def test_cross_department_exclusion(qdrant_env):
    sec = SecurityContext("u1", "acme", ["interviewer"], ["sales"], 3, [])
    hits = search_with(qdrant_env, build_qdrant_filter(sec))
    assert len(hits) == 0


def test_clearance_lte_enforced(qdrant_env):
    sec = SecurityContext("u1", "acme", ["interviewer"], [], 2, [])
    hits = search_with(qdrant_env, build_qdrant_filter(sec))
    assert len(hits) == 0


def test_cross_tenant_exclusion(qdrant_env):
    sec = SecurityContext("u1", "other", ["interviewer"], [], 3, [])
    hits = search_with(qdrant_env, build_qdrant_filter(sec))
    assert len(hits) == 1 and hits[0].payload["tenant_id"] == "other"
