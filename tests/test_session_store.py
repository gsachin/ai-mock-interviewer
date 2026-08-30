"""Session stores: in-memory roundtrip; Redis store against a live server
(auto-skip when none is reachable)."""
import asyncio

import pytest

from interviewer.session_store import InMemorySessionStore, RedisSessionStore

SUMMARY = {"session_id": "s1", "state": "wrap", "stats": {"questions_asked": 2}}


def run(coro):
    return asyncio.run(coro)


def test_memory_store_roundtrip():
    store = InMemorySessionStore()
    assert run(store.load("s1")) is None
    run(store.save("s1", SUMMARY))
    assert run(store.load("s1")) == SUMMARY
    run(store.delete("s1"))
    assert run(store.load("s1")) is None


@pytest.mark.live
def test_redis_store_roundtrip():
    from tests.conftest import port_open

    if not port_open("127.0.0.1", 6379):
        pytest.skip("no Redis on localhost:6379 — start redis-stack")

    async def roundtrip():
        store = RedisSessionStore("redis://localhost:6379")
        await store.save("stub-session", SUMMARY)
        assert await store.load("stub-session") == SUMMARY
        await store.delete("stub-session")
        assert await store.load("stub-session") is None
        await store._redis().aclose()

    run(roundtrip())
