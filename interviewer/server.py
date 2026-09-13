"""Management-plane API: app assembly only.

The routes live in ``interviewer/api/routes.py`` (US-006). This module owns
what genuinely has to be process-global — the app object, the middleware, the
config, and the two swappable seams — and does nothing else.

Request/response only — audio never flows through this app (the voice hot
path runs in the LiveKit agent worker). Retrieval happens in the standalone
enterprise-rag-core MCP service; this app calls it through
``interviewer.rag_client.RagClient``.
"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from interviewer.api.routes import router
from interviewer.config import InterviewerConfig
from interviewer.logging_setup import configure_logging
from interviewer.rag_client import RagClient
from interviewer.session_store import InMemorySessionStore, RedisSessionStore

# This app previously configured no logging at all — uvicorn's defaults were
# the only output. configure_logging is idempotent and text-by-default, so
# local behaviour is unchanged.
configure_logging("api")

app = FastAPI(title="mock-interviewer", version="0.1.0")

config = InterviewerConfig.from_env()

# Skills API reaches the RAG service over MCP (register_bank / interview_bank).
# Module-level for seam-testing: tests swap server._rag for a stub.
_rag = RagClient(config.rag_mcp_url, token=config.rag_mcp_token)

# Sessions live in the configured store, not in this process.
#
# They were a module-level dict behind a threading.Lock, which made the API
# single-replica by construction: POST /sessions on replica A, GET on replica
# B, and the second returns 404 non-deterministically. `RedisSessionStore`
# already existed and was correct -- it was simply never wired in, so this is
# mostly deletion.
#
# `memory` stays the DEFAULT so the Windows dev flow (which sets almost no
# environment) and the existing tests behave exactly as before. Module-level
# and named `_store` to match the `_rag` seam that tests already swap.
_store = (RedisSessionStore(config.redis_url)
          if config.session_store == "redis"
          else InMemorySessionStore())

# Origins come from config, defaulting to the four that used to be hardcoded
# here. See InterviewerConfig.cors_origins for why the default is those and why
# an in-cluster deployment usually wants an empty list instead.
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(config.cors_origins),
    allow_credentials=config.cors_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Registered BEFORE the static mount below, and that ordering is the point of
# the extraction: FastAPI matches in declaration order, so the StaticFiles
# catch-all at "/" shadows anything declared after it. Previously this was
# enforced by a warning comment at the bottom of a 340-line module; now the
# router is included here and the mount is the last statement in the file.
app.include_router(router)


# The voice UI (web/index.html) is served from this app so the page and the
# token endpoint share an origin (no CORS).
#
# INTERVIEW_WEB_DIR overrides the location. The default resolves relative to
# the package, which is correct in the repo AND in the image (both put the
# source at a root with web/ beside it) but wrong under a plain site-packages
# install — where the is_dir() guard below fails silently and the UI simply
# does not appear. Containers set the variable explicitly so the failure mode
# cannot happen.
_web_dir = (Path(config.web_dir) if config.web_dir
            else Path(__file__).resolve().parent.parent / "web")
if _web_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(_web_dir), html=True), name="web")
