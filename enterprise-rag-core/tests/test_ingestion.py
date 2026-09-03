"""TEST-INGEST-01 (convertible subset): PDF text + table extraction into the
dual representation, chunk/embed/upsert into a hermetic zero-infra stack, and
idempotent re-ingest. The test PDF is generated with fpdf2 (pinned in
requirements-dev.txt) so no binary fixtures live in the repo."""
import asyncio

from fpdf import FPDF

from enterprise_rag.config import EngineConfig
from enterprise_rag.hybrid import OllamaEmbeddingClient
from enterprise_rag.ingestion import extract_blocks, ingest


def make_policy_pdf(path) -> None:
    pdf = FPDF()
    pdf.set_font("Helvetica", size=11)
    pdf.add_page()

    def line(text):
        pdf.cell(0, 6, text)
        pdf.ln()

    line("Employee Handbook - Benefits. All staff accrue PTO.")
    pdf.ln(4)
    # table drawn as 2+space-aligned columns (the documented detection heuristic)
    line("Employment Tier    Annual Accrual    Max Carryover")
    line("Level 1 - Staff    15 Days           5 Days")
    line("Level 2 - Executive    25 Days       10 Days")
    pdf.ln(4)
    line("Requests are reviewed monthly by the people team.")
    pdf.add_page()
    line("Appendix: policy versions are archived annually.")
    pdf.output(str(path))


# ── extraction: dual representation ───────────────────────────────────────

def test_extract_blocks_dual_representation(tmp_path):
    pdf_path = tmp_path / "policy.pdf"
    make_policy_pdf(pdf_path)
    blocks = extract_blocks(pdf_path)

    tables = [b for b in blocks if b.content_type == "table"]
    assert len(tables) == 1, blocks
    table = tables[0]
    # headers retained without column loss; row/column counts preserved
    assert table.headers == ["Employment Tier", "Annual Accrual", "Max Carryover"]
    assert table.rows == [
        ["Level 1 - Staff", "15 Days", "5 Days"],
        ["Level 2 - Executive", "25 Days", "10 Days"],
    ]
    assert table.structured == {"headers": table.headers, "rows": table.rows}
    lines = table.markdown_payload.splitlines()
    assert lines[0].startswith("| Employment Tier")
    assert lines[1] == "| --- | --- | --- |"
    assert "Level 2 - Executive" in table.markdown_payload

    texts = [b for b in blocks if b.content_type == "text"]
    assert any("Employee Handbook" in b.text for b in texts)
    assert any("Appendix" in b.text for b in texts)


# ── end-to-end ingest: upsert + idempotent re-ingest ──────────────────────

def test_ingest_end_to_end_idempotent(tmp_path, monkeypatch):
    async def fake_embed(self, text):
        v = [0.0] * 8
        for i, ch in enumerate(text[:8]):
            v[i % 8] += (ord(ch) % 5 + 1) / 10.0
        return v

    monkeypatch.setattr(OllamaEmbeddingClient, "embed", fake_embed)

    pdf_path = tmp_path / "policy.pdf"
    make_policy_pdf(pdf_path)
    stack = EngineConfig(
        vector_backend="memory", keyword_backend="bm25", cache_backend="none",
        rerank_model_path="definitely/not/here.onnx",
        embed_backend="ollama",     # machine-independent: auto resolves mlx on macOS
    ).build_stack()

    async def run():
        r1 = await ingest(
            stack, pdf_path, doc_id="hr-policy", tenant_id="acme",
            department="hr", clearance=2,
        )
        assert r1.tables == 1
        assert r1.chunks >= 3          # 2 paragraphs + 1 table, >= 1 page each
        chunks = await stack.vector_store.get_by_ids(
            [f"hr-policy:p1:c{i}x0" for i in range(6)], "acme")
        table_chunk = next(
            (c for c in chunks if c.content.startswith("| Employment Tier")), None)
        assert table_chunk is not None, "table chunk missing from store"
        assert table_chunk.required_clearance == 2
        assert table_chunk.department == "hr"

        # idempotent re-ingest: same chunk set, no duplicates
        r2 = await ingest(
            stack, pdf_path, doc_id="hr-policy", tenant_id="acme",
            department="hr", clearance=2,
        )
        assert r2.chunks == r1.chunks
        assert r2.tables == r1.tables

        # cross-tenant: other tenant sees nothing
        empty = await stack.vector_store.get_by_ids(
            [f"hr-policy:p1:c{i}x0" for i in range(6)], "other")
        assert empty == []

    asyncio.run(run())
