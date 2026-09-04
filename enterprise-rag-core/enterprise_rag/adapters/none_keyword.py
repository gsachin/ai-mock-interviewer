"""No-op keyword leg — dense-only retrieval."""
from enterprise_rag.model import Chunk, UpsertRecord
from enterprise_rag.security import SecurityContext


class NoOpKeywordStore:
    async def search(self, query_text: str,
                     sec_ctx: SecurityContext, limit: int) -> list[Chunk]:
        return []

    async def upsert(self, records: list[UpsertRecord]) -> None:
        return None

    async def delete_by_parent(self, parent_id: str, tenant_id: str) -> int:
        return 0
