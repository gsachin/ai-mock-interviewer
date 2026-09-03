"""Zero-infrastructure BM25 keyword leg — stdlib only.

A generalisation of the ``_MiniBM25`` scorer from the universityDemo
``app/rag.py`` (k1=1.5, b=0.75, same tokenizer) over full Chunk records with
SecurityContext post-filtering. This is what lets a consuming system get the
dual-leg hybrid with no Elasticsearch at all.

Realtime-readiness (Phase 0): scoring and the index rebuild are CPU-bound
synchronous work, so both are offloaded from the event loop via
``asyncio.to_thread``.
"""
import asyncio
import math
import re
from collections import Counter

from enterprise_rag.model import Chunk, UpsertRecord
from enterprise_rag.security import SecurityContext

_TOKEN_RE = re.compile(r"[a-z0-9$]+")


class BM25KeywordStore:
    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self._k1 = k1
        self._b = b
        self._chunks: list[Chunk] = []
        self._docs: list[list[str]] = []
        self._df: Counter = Counter()
        self._avgdl: float = 0.0
        self._n: int = 0

    # ── indexing ─────────────────────────────────────────────────────────

    def _rebuild(self) -> None:
        self._docs = [_TOKEN_RE.findall(c.content.lower()) for c in self._chunks]
        self._n = len(self._docs)
        self._df = Counter()
        for tokens in self._docs:
            for term in set(tokens):
                self._df[term] += 1
        self._avgdl = sum(len(t) for t in self._docs) / max(1, self._n)

    async def upsert(self, records: list[UpsertRecord]) -> None:
        by_id = {c.chunk_id: c for c in self._chunks}
        for r in records:
            by_id[r.chunk_id] = r.to_chunk()
        self._chunks = list(by_id.values())
        await asyncio.to_thread(self._rebuild)

    # ── scoring ──────────────────────────────────────────────────────────

    def _score(self, query: str, limit_hint: int) -> list[tuple[float, Chunk]]:
        q = _TOKEN_RE.findall(query.lower())
        scored: list[tuple[float, Chunk]] = []
        for idx, tokens in enumerate(self._docs):
            dl = max(1, len(tokens))
            tf = Counter(tokens)
            score = 0.0
            for term in q:
                if term not in self._df:
                    continue
                idf = math.log(1 + (self._n - self._df[term] + 0.5) / (self._df[term] + 0.5))
                t = tf.get(term, 0)
                if t:
                    score += idf * (t * (self._k1 + 1)) / (
                        t + self._k1 * (1 - self._b + self._b * dl / self._avgdl)
                    )
            if score > 0:
                scored.append((score, self._chunks[idx]))
        scored.sort(key=lambda pair: -pair[0])
        return scored[:limit_hint]

    async def search(self, query_text: str,
                     sec_ctx: SecurityContext, limit: int) -> list[Chunk]:
        # Score against the whole corpus, then post-filter: BM25 has no
        # server-side filter, so a post-filter over a generous candidate pool
        # keeps recall for restricted principals (fetch limit*4 first).
        scored = await asyncio.to_thread(self._score, query_text, limit * 4)
        out = [chunk for _s, chunk in scored if sec_ctx.matches(chunk)]
        return out[:limit]
