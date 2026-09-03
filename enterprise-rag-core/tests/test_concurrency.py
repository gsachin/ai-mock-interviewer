"""Phase 0 concurrency smoke: 50 concurrent orchestrated requests over the
zero-infrastructure stack (memory vector + BM25 + in-memory cache) against a
realistic-sized corpus (~2000 chunks, ms-scale scoring work — the regime where
the offload wrappers matter). Every request must succeed with correct tenant
isolation and per-stage timings, and the concurrent wall time must clearly
beat the serial wall time — a blocked event loop would make them equal."""
import asyncio
import time

from enterprise_rag.config import EngineConfig
from enterprise_rag.hybrid import OllamaEmbeddingClient
from enterprise_rag.model import UpsertRecord
from enterprise_rag.orchestrator import AgentContextRequest
from enterprise_rag.security import SecurityContext

N_REQUESTS = 50
N_RUBRICS = 2000


def _vec(cid: str) -> list[float]:
    v = [0.0] * 8
    for j, ch in enumerate(cid[:8]):
        v[j % 8] += (ord(ch) % 5 + 1) / 10.0
    return v


def build_stack(monkeypatch):
    async def fake_embed(self, text):
        # deterministic pseudo-embedding: first chars seed a unit vector
        v = [0.0] * 8
        for i, ch in enumerate(text[:8]):
            v[i % 8] += (ord(ch) % 5 + 1) / 10.0
        return v

    monkeypatch.setattr(OllamaEmbeddingClient, "embed", fake_embed)
    return EngineConfig(
        vector_backend="memory", keyword_backend="bm25", cache_backend="memory",
        default_tenant="acme", rerank_model_path="definitely/not/here.onnx",
        embed_backend="ollama",     # machine-independent: auto resolves mlx on macOS
    ).build_stack()


def seed(stack) -> None:
    async def run():
        records = [
            UpsertRecord(
                chunk_id="resume:current", parent_id="resume", tenant_id="acme",
                content="Jane engineers python platforms daily", section_title="",
                required_clearance=0, department=None, vector=[1.0] + [0.0] * 7,
            ),
            UpsertRecord(
                chunk_id="jd:target", parent_id="jd", tenant_id="acme",
                content="platform engineering python required", section_title="",
                required_clearance=0, department=None, vector=[0.0, 1.0] + [0.0] * 6,
            ),
        ]
        for i in range(N_RUBRICS):
            records.append(UpsertRecord(
                chunk_id=f"rub{i}", parent_id="rubric", tenant_id="acme",
                content=f"rubric topic {i % 200} python engineering leadership",
                section_title="", required_clearance=0, department=None,
                vector=_vec(f"rub{i}"),
            ))
        for i in range(5):
            records.append(UpsertRecord(
                chunk_id=f"other-{i}", parent_id="rubric", tenant_id="other",
                content=f"foreign rubric {i}", section_title="",
                required_clearance=0, department=None, vector=_vec(f"other-{i}"),
            ))
        await stack.vector_store.upsert(records)
        await stack.keyword_store.upsert(records)

    asyncio.run(run())


def make_req(sec_ctx, i):
    # unique per request: no cache hits, every request does full retrieval
    return AgentContextRequest(
        sec_ctx=sec_ctx, resume_text="Jane", job_description="platform",
        conversation_history=[], rubric_query=f"rubric topic {i} python engineering",
    )


def test_50_concurrent_sessions_stay_correct_and_overlap(monkeypatch):
    stack = build_stack(monkeypatch)
    seed(stack)
    acme = SecurityContext("u1", "acme", ["interviewer"], [], 3, [])
    other = SecurityContext("u2", "other", ["interviewer"], [], 9, [])

    async def serial_run():
        out = []
        for i in range(N_REQUESTS):
            out.append(await stack.orchestrator.execute_agent_context(
                make_req(acme if i % 2 == 0 else other, i)))
        return out

    async def concurrent_run(offset):
        return await asyncio.gather(*[
            stack.orchestrator.execute_agent_context(
                make_req(acme if i % 2 == 0 else other, offset + i))
            for i in range(N_REQUESTS)
        ])

    t0 = time.perf_counter()
    serial = asyncio.run(serial_run())
    t_serial = time.perf_counter() - t0

    t0 = time.perf_counter()
    concurrent = asyncio.run(concurrent_run(offset=N_REQUESTS))
    t_conc = time.perf_counter() - t0

    for i, out in enumerate(serial + concurrent):
        assert out["status"] == "SUCCESS", f"request {i} failed"
        assert set(out["timings_ms"]) == {"direct", "embed", "cache", "retrieval",
                                          "rerank", "format", "total"}
        if i % 2 == 0:   # acme tenant: direct chunks present, no foreign rubrics
            ids = {p["chunk_id"] for p in out["provenance"]}
            assert "resume:current" in ids and "jd:target" in ids
            assert not any(cid.startswith("other-") for cid in ids)
        else:            # other tenant: only its own rubrics — never acme content
            ids = {p["chunk_id"] for p in out["provenance"]}
            assert ids and all(cid.startswith("other-") for cid in ids)
            assert not any(cid in ("resume:current", "jd:target") or
                           cid.startswith("rub") for cid in ids)

    # 50 sessions must overlap, not serialize: concurrent wall time clearly
    # below serial wall time. (A blocked event loop would make them equal.)
    assert t_conc < t_serial * 0.9, (
        f"no overlap: concurrent {t_conc * 1000:.1f} ms vs serial "
        f"{t_serial * 1000:.1f} ms"
    )
