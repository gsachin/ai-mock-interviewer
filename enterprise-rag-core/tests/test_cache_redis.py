"""TEST-PERF-02 semantics: RedisVL SemanticCache round-trip against Redis Stack.
Converted from the seed verification harness (seed: test_cache.py).
Requires a Redis Stack server (see README); auto-skips when unreachable."""
import asyncio
import time

import numpy as np
import pytest

from enterprise_rag.cache import EnterpriseSemanticCache

pytestmark = pytest.mark.redis


def norm(v):
    v = np.asarray(v, dtype=np.float32)
    return (v / np.linalg.norm(v)).tolist()


def fake_embed(text: str) -> list[float]:
    # Stand-in for the embedder's sync variant (dimension probe only — request
    # paths always pass vector= explicitly).
    return [0.0] * 768


@pytest.fixture(scope="module")
def vectors():
    base = norm(np.random.default_rng(7).normal(size=768))
    # epsilon small enough that cos stays well above 0.96 (noise norm ~= 0.02):
    near = norm(np.asarray(base) + 0.0007 * np.random.default_rng(8).normal(size=768))
    far = norm(np.random.default_rng(9).normal(size=768))  # ~orthogonal
    return base, near, far


def run(coro):
    return asyncio.run(coro)


def test_roundtrip_and_vector_semantics(redis_stack_url, vectors):
    if redis_stack_url is None:
        pytest.skip("Redis Stack not reachable")
    base, near, far = vectors

    async def scenario():
        cache = EnterpriseSemanticCache(
            redis_stack_url, tenant_id="acme", schema_version="v1", embedding_fn=fake_embed
        )
        payload = {
            "chunks": [
                {
                    "chunk_id": "c1",
                    "parent_id": "rub",
                    "tenant_id": "acme",
                    "content": "Behavioral rubric: situational leadership.",
                    "score": 0.88,
                }
            ]
        }
        await cache.put(base, payload, "acme", "v1", query_text="leadership rubric query")

        hit = await cache.get(base, "acme", "v1")
        assert hit is not None and hit["chunks"][0]["chunk_id"] == "c1", f"{hit}"

        near_hit = await cache.get(near, "acme", "v1")
        assert near_hit is not None, (
            f"cos={float(np.dot(np.asarray(base), np.asarray(near))):.4f}"
        )

        far_hit = await cache.get(far, "acme", "v1")
        assert far_hit is None

        other_hit = await cache.get(base, "evilcorp", "v1")
        assert other_hit is None

        wrong_schema = await cache.get(base, "acme", "v9")
        assert wrong_schema is None

    run(scenario())


def test_ttl_expiry(redis_stack_url, vectors):
    if redis_stack_url is None:
        pytest.skip("Redis Stack not reachable")
    base, _near, _far = vectors
    payload = {"chunks": [{"chunk_id": "c1"}]}

    async def scenario():
        ttl_cache = EnterpriseSemanticCache(
            redis_stack_url, tenant_id="acme", schema_version="ttl1",
            embedding_fn=fake_embed, ttl=1,
        )
        await ttl_cache.put(base, payload, "acme", "ttl1")
        hit_immediate = await ttl_cache.get(base, "acme", "ttl1")
        assert hit_immediate is not None
        await asyncio.sleep(2.5)
        hit_late = await ttl_cache.get(base, "acme", "ttl1")
        assert hit_late is None

    run(scenario())
