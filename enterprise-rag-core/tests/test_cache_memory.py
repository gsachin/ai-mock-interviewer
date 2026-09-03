"""InMemorySemanticCache and NoOpSemanticCache: the zero-infra cache variants
with true vector-similarity semantics (cosine distance <= 0.04)."""
import asyncio

import numpy as np

from enterprise_rag.cache import InMemorySemanticCache, NoOpSemanticCache


def norm(v):
    v = np.asarray(v, dtype=np.float32)
    return (v / np.linalg.norm(v)).tolist()


def run(coro):
    return asyncio.run(coro)


def test_memory_cache_hit_miss_and_isolation():
    cache = InMemorySemanticCache()
    base = norm(np.random.default_rng(7).normal(size=768))
    near = norm(np.asarray(base) + 0.0007 * np.random.default_rng(8).normal(size=768))
    far = norm(np.random.default_rng(9).normal(size=768))
    payload = {"chunks": [{"chunk_id": "c1"}]}

    run(cache.put(base, payload, "acme", "v1"))
    assert run(cache.get(base, "acme", "v1")) == payload
    assert run(cache.get(near, "acme", "v1")) == payload     # paraphrase hits
    assert run(cache.get(far, "acme", "v1")) is None         # dissimilar misses
    assert run(cache.get(base, "evilcorp", "v1")) is None    # tenant isolation
    assert run(cache.get(base, "acme", "v9")) is None        # schema isolation


def test_memory_cache_ttl_expiry():
    import time

    cache = InMemorySemanticCache(distance_threshold=0.04, ttl=1)
    base = norm(np.random.default_rng(11).normal(size=16))
    run(cache.put(base, {"k": "v"}, "acme", "v1"))
    assert run(cache.get(base, "acme", "v1")) == {"k": "v"}
    time.sleep(1.2)
    assert run(cache.get(base, "acme", "v1")) is None


def test_noop_cache():
    cache = NoOpSemanticCache()
    assert run(cache.get([0.1, 0.2], "acme", "v1")) is None
    run(cache.put([0.1, 0.2], {"x": 1}, "acme", "v1"))  # no-op, must not raise
