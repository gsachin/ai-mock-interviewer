"""No-op keyword leg — dense-only retrieval."""
from enterprise_rag.model import Chunk, UpsertRecord
from enterprise_rag.security import SecurityContext


class NoOpKeywordStore:
    async def search(self, query_text: str,
                     sec_ctx: SecurityContext, limit: int) -> list[Chunk]:
        return []

    async def upsert(self, records: list[UpsertRecord]) -> None:
        return None
