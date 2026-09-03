"""Console entry point (pyproject: ``enterprise-rag-core = enterprise_rag.cli:main``).

Commands:
  download-model   Fetch the INT8 MiniLM reranker ONNX (22 MiB, Hugging Face)
  serve            Streamable-HTTP MCP server (:8000/mcp)
  serve-stdio      stdio MCP transport (no auth layer — none-auth mode)
  ingest           Extract, chunk, embed, upsert a PDF document
  prepopulate      Build/prepopulate DBs from a markdown knowledge base
"""
import argparse
import asyncio
import os
import shutil
import sys
from pathlib import Path

from enterprise_rag import prepopulate
from enterprise_rag.warmup import warm_keyword_from_vector_store

_REPO_ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = _REPO_ROOT / "models" / "reranker"
MODEL_FILE = MODEL_DIR / "minilm-int8.onnx"

# Repo hosting the quantized cross-encoder (also the reranker's tokenizer
# source): cross-encoder/ms-marco-MiniLM-L-6-v2, file onnx/model_quint8_avx2.onnx.
MODEL_REPO_ID = "cross-encoder/ms-marco-MiniLM-L-6-v2"
MODEL_REPO_FILE = "onnx/model_quint8_avx2.onnx"


def cmd_download_model(args: argparse.Namespace) -> int:
    """Downloads the INT8 ONNX reranker into models/reranker/minilm-int8.onnx."""
    if MODEL_FILE.is_file() and not args.force:
        print(f"already present: {MODEL_FILE} (use --force to re-download)")
        return 0

    # Repo-known xet.dll issue on Windows when huggingface_hub downloads models.
    os.environ.setdefault("HF_HUB_ENABLE_HF_XET", "0")
    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        print(
            "huggingface_hub is not installed — pip install huggingface_hub "
            "(it arrives as a tokenizers dependency in the dev install)",
            file=sys.stderr,
        )
        return 1

    print(f"downloading {MODEL_REPO_ID}::{MODEL_REPO_FILE} ...")
    path = hf_hub_download(
        repo_id=MODEL_REPO_ID,
        filename=MODEL_REPO_FILE,
        local_dir=str(_REPO_ROOT / "models"),
    )
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(MODEL_FILE))
    # drop the now-empty onnx/ staging dir inside models/ (if unused)
    staging = Path(path).parent
    try:
        staging.rmdir()
    except OSError:
        pass
    print(f"saved: {MODEL_FILE}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Streamable HTTP MCP server; stack closed after uvicorn exits."""
    from enterprise_rag.config import EngineConfig
    from enterprise_rag.server import build_app

    import uvicorn

    app = build_app(EngineConfig.from_env())
    warm_mode = os.environ.get("RAG_CORE_WARM_KEYWORD", "1")
    if warm_mode != "0":
        tenants = "all" if warm_mode == "all" else None
        warmed = asyncio.run(warm_keyword_from_vector_store(app.state.stack, tenants=tenants))
        if warmed:
            print(f"keyword leg warmed with {warmed} chunks")
        else:
            print("keyword leg warm-up: vector store is empty (prepopulate first)")
    print(
        f"enterprise-rag-core MCP on http://{args.host}:{args.port}/mcp "
        f"(auth={app.state.stack.config.auth_mode}, "
        f"vector={app.state.stack.config.vector_backend}, "
        f"keyword={app.state.stack.config.keyword_backend}, "
        f"cache={app.state.stack.config.cache_backend})"
    )
    try:
        uvicorn.run(app, host=args.host, port=args.port)
    finally:
        asyncio.run(app.state.stack.aclose())
    return 0


def cmd_serve_stdio(args: argparse.Namespace) -> int:
    """stdio transport. No auth layer exists on stdio — the OIDC tool refuses
    to run without a bearer token, so use RAG_CORE_AUTH_MODE=none (default)."""
    from enterprise_rag.config import EngineConfig
    from enterprise_rag.server import _set_engine, _set_orchestrator, _set_vector_store, build_mcp

    config = EngineConfig.from_env()
    stack = config.build_stack()
    _set_orchestrator(stack.orchestrator)
    _set_engine(stack.engine)
    _set_vector_store(stack.vector_store)
    mcp = build_mcp(config)

    async def run() -> None:
        try:
            warm_mode = os.environ.get("RAG_CORE_WARM_KEYWORD", "1")
            if warm_mode != "0":
                tenants = "all" if warm_mode == "all" else None
                warmed = await warm_keyword_from_vector_store(stack, tenants=tenants)
                if warmed:
                    print(f"keyword leg warmed with {warmed} chunks", file=sys.stderr)
            await mcp.run_stdio_async()
        finally:
            await stack.aclose()

    asyncio.run(run())
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    """PDF ingestion: extract text + tables (dual representation), chunk,
    embed, and upsert into the configured vector + keyword legs."""
    from enterprise_rag.config import EngineConfig
    from enterprise_rag.ingestion import ingest

    config = EngineConfig.from_env()
    stack = config.build_stack()

    async def run() -> None:
        try:
            result = await ingest(
                stack, args.pdf, doc_id=args.doc_id, tenant_id=args.tenant,
                department=args.department, clearance=args.clearance,
            )
            print(
                f"ingested {result.doc_id}: {result.pages} page(s), "
                f"{result.tables} table(s), {result.chunks} chunk(s) "
                f"-> tenant={result.tenant_id}"
            )
        finally:
            await stack.aclose()

    asyncio.run(run())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="enterprise-rag-core",
        description="Standalone, pluggable Enterprise RAG/MCP Core Engine",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_dl = sub.add_parser("download-model", help="Download the INT8 reranker model (22 MiB, Hugging Face)")
    p_dl.add_argument("--force", action="store_true", help="Re-download even if the model exists")
    p_dl.set_defaults(func=cmd_download_model)

    p_serve = sub.add_parser("serve", help="Streamable-HTTP MCP server on :8000/mcp")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.set_defaults(func=cmd_serve)

    p_stdio = sub.add_parser("serve-stdio", help="stdio MCP transport")
    p_stdio.set_defaults(func=cmd_serve_stdio)

    p_ingest = sub.add_parser("ingest", help="Extract, chunk, embed, upsert a PDF document")
    p_ingest.add_argument("--pdf", required=True, help="Path to the PDF document")
    p_ingest.add_argument("--doc-id", required=True, help="Deterministic document id (chunk ids derive from it)")
    p_ingest.add_argument("--tenant", required=True, help="Tenant the document belongs to")
    p_ingest.add_argument("--department", default=None, help="Optional department scope")
    p_ingest.add_argument("--clearance", type=int, default=0, help="Required clearance level (default 0)")
    p_ingest.set_defaults(func=cmd_ingest)

    p_prep = sub.add_parser("prepopulate", help="Build/prepopulate DBs from a markdown knowledge base")
    p_prep.add_argument("--kb", required=True, help="Path to the markdown knowledge base")
    p_prep.add_argument("--doc-id", default="meridian-kb", help="Deterministic doc id (chunk ids derive from it)")
    p_prep.add_argument("--tenant", default="default", help="Tenant the doc belongs to")
    p_prep.add_argument("--department", default=None, help="Optional department scope")
    p_prep.add_argument("--clearance", type=int, default=0, help="Required clearance level (default 0)")
    p_prep.add_argument("--required-marker", action="append", default=[],
                        help="Substring that must appear in the corpus (repeatable)")
    p_prep.add_argument("--blocked-marker", action="append", default=[],
                        help="Substring that must NOT appear (fatal if present; repeatable)")
    p_prep.add_argument("--force", action="store_true", help="Replace existing chunks instead of skipping")
    p_prep.add_argument("--chunk-size", type=int, default=600)
    p_prep.add_argument("--chunk-overlap", type=int, default=90)
    p_prep.set_defaults(func=prepopulate.cmd_prepopulate)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
