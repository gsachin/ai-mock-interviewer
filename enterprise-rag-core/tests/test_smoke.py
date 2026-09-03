"""Smoke test: every import path and signature in the design doc
(TRD_ENTERPRISE_RAG_MCP_CORE.md v2026.5) must resolve against the pinned SDK
versions. Converted from the seed verification harness."""
import inspect

import pytest
from dataclasses import replace

from enterprise_rag.security import SecurityContext
from enterprise_rag.adapters.qdrant_vector import build_qdrant_filter
from enterprise_rag.adapters.elasticsearch_keyword import build_es_filter
from enterprise_rag.hybrid import (
    OllamaEmbeddingClient,
    _wrrf,
    fuse_wrrf,
    AsyncParallelHybridEngine,
)
from enterprise_rag.model import Chunk
from enterprise_rag.reranker import ONNXVoiceReranker
from enterprise_rag.formatter import ContextFormatter
from enterprise_rag.orchestrator import AgentContextRequest, AtomicAgentContextOrchestrator
from enterprise_rag.cache import EnterpriseSemanticCache

from qdrant_client import models as qm


# ── §1: SecurityContext + filter builders (builders live in the adapters) ──

@pytest.fixture
def sec_empty():
    return SecurityContext("u1", "acme", ["interviewer"], [], 3, ["g1"])


@pytest.fixture
def sec_dept():
    return SecurityContext("u1", "acme", ["interviewer"], ["engineering", "hr"], 3, [])


def test_qdrant_filter_shape_empty_departments(sec_empty):
    f = build_qdrant_filter(sec_empty)
    assert isinstance(f, qm.Filter)
    assert f.should is None, f"should={f.should!r}"
    assert len(f.must) == 2
    assert f.must[1].range.lte == 3.0, f"range={f.must[1].range}"


def test_qdrant_filter_shape_with_departments(sec_dept):
    f = build_qdrant_filter(sec_dept)
    assert len(f.should) == 2, f"should={f.should!r}"


def test_es_filter_shape_with_departments(sec_dept):
    es_f = build_es_filter(sec_dept)
    assert len(es_f["bool"]["filter"]) == 3, f"{es_f}"
    assert "terms" in es_f["bool"]["filter"][2]
    assert "department.keyword" in es_f["bool"]["filter"][2]["terms"], f"{es_f}"


def test_es_filter_omits_terms_when_departments_empty(sec_empty):
    es_f = build_es_filter(sec_empty)
    assert len(es_f["bool"]["filter"]) == 2, f"{es_f}"


def test_security_context_is_frozen(sec_empty):
    with pytest.raises(Exception):
        sec_empty.tenant_id = "evil"  # type: ignore[misc]


def test_qdrant_model_constructors():
    c = qm.FieldCondition(
        key="tenant_id", match=qm.MatchValue(value="acme"), range=qm.Range(lte=3.0)
    )
    assert c.match.value == "acme"


# ── §2: weighted RRF fusion ────────────────────────────────────────────

@pytest.fixture
def chunks():
    return [
        Chunk("c1", "p1", "acme", "a", 0.9),
        Chunk("c2", "p2", "acme", "b", 0.8),
        Chunk("c3", "p3", "acme", "c", 0.7),
    ]


def test_wrrf_order_and_scores(chunks):
    c1, c2, c3 = chunks
    merged = fuse_wrrf([c1, c2], [c2, c3], alpha=0.3)
    assert merged[0].chunk_id == "c2", f"{[(c.chunk_id, round(c.score, 6)) for c in merged]}"
    assert abs(merged[0].score - (0.7 / 60 + 0.3 / 61)) < 1e-9, f"{merged[0].score}"


def test_wrrf_single_hit_lists_fuse(chunks):
    c1, _c2, c3 = chunks
    single = fuse_wrrf([c1], [c3], alpha=0.5)
    assert len(single) == 2 and all(c.score > 0 for c in single), (
        f"{[(c.chunk_id, c.score) for c in single]}"
    )


def test_wrrf_helper():
    assert _wrrf(0) == 1.0 / 60


# ── §3: formatter / orchestrator imports and semantics ──────────────────

@pytest.fixture
def fmt_chunks():
    return [
        Chunk("a", "rub", "acme", "most relevant", 0.95),
        Chunk("b", "rub", "acme", "second", 0.9),
        Chunk("c", "rub", "acme", "third", 0.85),
        Chunk("d", "rub", "acme", "fourth", 0.8),
    ]


def test_u_shape_envelope(sec_empty, fmt_chunks):
    out = ContextFormatter.format_u_shape(fmt_chunks, sec_empty)
    lines = out.split("\n\n")
    assert lines[0].startswith("[context_envelope tenant=acme"), lines[0]
    assert "most relevant" in lines[1], lines[1]
    assert "second" in lines[-1], lines[-1]
    assert out.index("fourth") < out.index("third")


def test_chunk_frozen_and_replace():
    c1 = Chunk("c1", "p1", "acme", "a", 0.9)
    with pytest.raises(Exception):
        c1.score = 99  # type: ignore[misc]
    assert replace(c1, score=0.5).score == 0.5


# ── §5: RedisVL import paths ────────────────────────────────────────────

def test_redisvl_import_paths():
    from redisvl.extensions.cache.llm import SemanticCache  # noqa: F401
    from redisvl.query.filter import Tag

    assert str(Tag("tenant_id") == "acme") is not None


# ── §6: MCP SDK surface ─────────────────────────────────────────────────

def test_mcp_server_surface():
    from mcp.server import MCPServer

    assert hasattr(MCPServer, "tool")
    assert hasattr(MCPServer, "streamable_http_app")
    assert hasattr(MCPServer, "run_streamable_http_async")

    init_params = inspect.signature(MCPServer.__init__).parameters
    assert {"name", "token_verifier", "auth"} <= set(init_params), f"{sorted(init_params)}"


def test_mcp_auth_settings_fields():
    from mcp.server.auth.settings import AuthSettings

    auth_fields = set(AuthSettings.model_fields.keys())
    assert {"issuer_url", "resource_server_url", "required_scopes"} <= auth_fields, (
        f"{sorted(auth_fields)}"
    )


def test_mcp_access_token_fields():
    from mcp.server.auth.provider import AccessToken

    access_fields = set(AccessToken.model_fields.keys())
    assert {"token", "client_id", "scopes", "claims"} <= access_fields, (
        f"{sorted(access_fields)}"
    )


def test_mcp_token_verifier_protocol():
    from mcp.server.auth.provider import TokenVerifier

    tok_sig = inspect.signature(TokenVerifier.verify_token)
    assert inspect.iscoroutinefunction(TokenVerifier.verify_token)
    assert tok_sig.parameters["token"].annotation is str


def test_fastmcp_removed_in_mcp_2x():
    with pytest.raises(ModuleNotFoundError):
        import mcp.server.fastmcp  # noqa: F401


# ── Client signatures (§2 claims) ───────────────────────────────────────

def test_qdrant_client_signatures():
    from qdrant_client import AsyncQdrantClient

    qp_params = inspect.signature(AsyncQdrantClient.query_points).parameters
    assert {"collection_name", "query", "query_filter", "limit", "with_payload"} <= set(
        qp_params
    ), f"{sorted(qp_params)}"
    ret_params = inspect.signature(AsyncQdrantClient.retrieve).parameters
    assert {"collection_name", "ids", "with_payload"} <= set(ret_params), (
        f"{sorted(ret_params)}"
    )


def test_es_search_signature():
    from elasticsearch import AsyncElasticsearch

    es_params = inspect.signature(AsyncElasticsearch.search).parameters
    assert {"index", "query", "size", "source"} <= set(es_params), f"{sorted(es_params)}"
    if "source" in es_params:
        assert es_params["source"].kind == inspect.Parameter.KEYWORD_ONLY
