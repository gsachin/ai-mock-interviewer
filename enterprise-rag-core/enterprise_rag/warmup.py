"""Keyword-leg warm-up.

The BM25 keyword leg is in-memory (zero infra), so a fresh service process
starts with an empty sparse index even though the vector store is persisted.
``warm_keyword_from_vector_store`` repopulates it at boot from the persistent
vector store — the launcher/CLI runs it before serving (env-gated by
``RAG_CORE_WARM_KEYWORD``, default on).

Phase 0 (realtime-readiness): the warm scope is selectable. ``RAG_CORE_WARM_KEYWORD``
accepts:

- ``0`` — warm-up disabled
- ``1`` (default) — warm the default tenant only (original behavior)
- ``all`` — warm every tenant present in the vector store (via
  ``VectorStore.list_tenants``); multi-tenant deployments get a complete
  sparse index at boot
"""
from enterprise_rag.model import UpsertRecord


async def warm_keyword_from_vector_store(stack, tenants: str | list[str] | None = None) -> int:
    """Upserts persisted chunks into the keyword leg and returns the number of
    chunks warmed (0 when nothing is in scope).

    ``tenants=None`` warms the default tenant only; ``tenants="all"`` warms
    every tenant present in the vector store; a list warms exactly those.
    """
    if tenants is None:
        scope = [stack.config.default_tenant]
    elif tenants == "all":
        scope = await stack.vector_store.list_tenants()
    else:
        scope = list(tenants)

    records = []
    for tenant_id in scope:
        for c in await stack.vector_store.get_all(tenant_id):
            records.append(UpsertRecord(
                chunk_id=c.chunk_id,
                parent_id=c.parent_id or "",
                tenant_id=c.tenant_id,
                content=c.content,
                section_title=c.section_title,
                required_clearance=c.required_clearance,
                department=c.department,
                vector=[],      # keyword legs never read the vector
            ))
    if records:
        await stack.keyword_store.upsert(records)
    return len(records)
