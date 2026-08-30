"""Session persistence (Phase 2): interview summaries saved/loaded per
session id. In-memory for dev/tests; Redis for multi-worker deployments
(lazy import — the redis package is an optional extra)."""
import json
from typing import Any, Protocol


class SessionStore(Protocol):
    async def save(self, session_id: str, summary: dict[str, Any]) -> None: ...
    async def load(self, session_id: str) -> dict[str, Any] | None: ...
    async def delete(self, session_id: str) -> None: ...


class InMemorySessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}

    async def save(self, session_id: str, summary: dict[str, Any]) -> None:
        self._sessions[session_id] = json.loads(json.dumps(summary))

    async def load(self, session_id: str) -> dict[str, Any] | None:
        return self._sessions.get(session_id)

    async def delete(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)


class RedisSessionStore:
    """Redis-backed store. Constructing it does not import redis; the client
    is created lazily on first use so the package runs without the extra."""

    def __init__(self, redis_url: str, *, ttl_seconds: int = 86400):
        self._redis_url = redis_url
        self._ttl = ttl_seconds
        self._client = None

    def _redis(self):
        if self._client is None:
            import redis.asyncio as aioredis
            self._client = aioredis.from_url(self._redis_url)
        return self._client

    def _key(self, session_id: str) -> str:
        return f"interviewer:session:{session_id}"

    async def save(self, session_id: str, summary: dict[str, Any]) -> None:
        await self._redis().set(self._key(session_id), json.dumps(summary), ex=self._ttl)

    async def load(self, session_id: str) -> dict[str, Any] | None:
        raw = await self._redis().get(self._key(session_id))
        return json.loads(raw) if raw else None

    async def delete(self, session_id: str) -> None:
        await self._redis().delete(self._key(session_id))
