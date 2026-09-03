# Verbatim transcript of TRD_ENTERPRISE_RAG_MCP_CORE.md §3.1 — used for verification only.
from enterprise_rag.model import Chunk
from enterprise_rag.security import SecurityContext


class ContextFormatter:
    MAX_MIDDLE_CHUNKS = 6
    MAX_CHUNK_CHARS = 1200

    @staticmethod
    def format_u_shape(chunks: list[Chunk], security: SecurityContext) -> str:
        ordered = sorted(chunks, key=lambda c: -c.score)
        if not ordered:
            return ""
        head = ordered[0]
        tail = ordered[1] if len(ordered) > 1 else None
        middle = ordered[2:] if tail is not None else []
        middle = sorted(middle, key=lambda c: c.score)[: ContextFormatter.MAX_MIDDLE_CHUNKS]

        parts = [f"[context_envelope tenant={security.tenant_id} "
                 f"clearance>={security.clearance_level}]"]
        parts.append(ContextFormatter._trim(head))
        parts.extend(ContextFormatter._trim(c) for c in middle)
        if tail is not None:
            parts.append(ContextFormatter._trim(tail))
        return "\n\n".join(parts)

    @staticmethod
    def _trim(chunk: Chunk) -> str:
        # Truncation happens HERE, at formatting time — retrieval keeps full chunks.
        content = chunk.content
        if len(content) > ContextFormatter.MAX_CHUNK_CHARS:
            content = content[: ContextFormatter.MAX_CHUNK_CHARS] + "…"
        return f"[{chunk.chunk_id} | {chunk.parent_id or 'direct'} | score={chunk.score:.4f}]\n{content}"
