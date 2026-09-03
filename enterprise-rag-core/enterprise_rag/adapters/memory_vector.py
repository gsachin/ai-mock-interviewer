"""Zero-infrastructure brute-force cosine vector store — tests, demos, and
tiny corpora. Applies the SecurityContext as a post-filter predicate.

Realtime-readiness (Phase 0): the numpy cosine scoring is CPU-bound, so the
search scoring runs in a worker thread via ``asyncio.to_thread``."""
import asyncio

import numpy as np

from enterprise_rag.model import Chunk, UpsertRecord
from enterprise_rag.security import SecurityContext


def _cos_sim(a: list[float], b: list[float]) -> float:
    va = np.asarray(a, dtype=np.float32)
    vb = np.asarray(b, dtype=np.float32)
    na, nb = np.linalg.norm(va), np.linalg.norm(vb)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(va, vb) / (na * nb))


class InMemoryVectorStore:
    def __init__(self):
        self._records: dict[str, tuple[UpsertRecord, list[float]]] = {}

    async def search(self, query_vector: list[float],
                     sec_ctx: SecurityContext, limit: int) -> list[Chunk]:
        def _score_all() -> list[Chunk]:
            scored = [
                (_cos_sim(query_vector, vec), rec)
                for rec, vec in self._records.values()
                if sec_ctx.matches(rec.to_chunk())
            ]
            scored.sort(key=lambda pair: -pair[0])
            return [rec.to_chunk(score=s) for s, rec in scored[:limit]]

        return await asyncio.to_thread(_score_all)

    async def get_by_ids(self, ids: list[str], tenant_id: str) -> list[Chunk]:
        out = []
        for chunk_id in ids:
            rec, _vec = self._records.get(chunk_id, (None, None))
            if rec is not None and rec.tenant_id == tenant_id:
                out.append(rec.to_chunk(score=1.0))
        return out

    async def upsert(self, records: list[UpsertRecord]) -> None:
        for r in records:
            self._records[r.chunk_id] = (r, r.vector)

    async def delete_by_parent(self, parent_id: str, tenant_id: str) -> int:
        doomed = [
            cid for cid, (rec, _v) in self._records.items()
            if rec.parent_id == parent_id and rec.tenant_id == tenant_id
        ]
        for cid in doomed:
            del self._records[cid]
        return len(doomed)

    async def get_all(self, tenant_id: str) -> list[Chunk]:
        return [
            rec.to_chunk()
            for rec, _vec in self._records.values()
            if rec.tenant_id == tenant_id
        ]

    async def list_tenants(self) -> list[str]:
        """Distinct tenant ids present in the store (bulk admin — warm-all,
        migrations)."""
        return sorted({rec.tenant_id for rec, _vec in self._records.values()})
