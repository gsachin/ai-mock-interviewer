# syntax=docker/dockerfile:1.7
#
# Multi-target image for the mock-interviewer.
#
#   docker build --target api    -t mock-interviewer-api    .
#   docker build --target worker -t mock-interviewer-worker .
#   docker build --target rag    -t mock-interviewer-rag    .
#
# Base is python:3.12 rather than the 3.11 used by the Windows venv: 3.12 has
# the best wheel coverage for av / ctranslate2 / onnxruntime, and it sidesteps
# the numpy pin that 3.11 forced in enterprise-rag-core. The Windows dev flow
# keeps 3.11 and is unaffected.
#
# We do NOT build the GPU inference images (vLLM / Whisper / Kokoro). Those are
# consumed as pinned upstream images — the single biggest scope reduction in
# the migration.

ARG PYTHON_VERSION=3.12

# ─────────────────────────────── base ────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# libgomp1     — OpenMP runtime required by ctranslate2 (faster-whisper) and
#                onnxruntime (kokoro).
# ffmpeg       — pydub decodes the MP3 that ElevenLabs returns. Only needed for
#                the cloud TTS providers; kept because the provider is a config
#                flip and a missing decoder would be a runtime surprise.
# curl         — container healthchecks.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libgomp1 \
      ffmpeg \
      curl \
 && rm -rf /var/lib/apt/lists/*

# Non-root. Matches the manifests' runAsUser.
RUN groupadd --gid 10001 app \
 && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin app

WORKDIR /app

# ─────────────────────────────── deps ────────────────────────────────────────
# Dependency layer, split from source so a code-only change does not reinstall.
FROM base AS deps

COPY pyproject.toml README.md ./
# The build context has no interviewer/ at this point, so install deps only via
# a throwaway metadata shim rather than the real package (which needs the source).
RUN python - <<'PY'
import tomllib, pathlib, subprocess, sys
data = tomllib.loads(pathlib.Path("pyproject.toml").read_text())
extras = data["project"].get("optional-dependencies", {})
pkgs = list(data["project"]["dependencies"])
for name in ("api", "voice"):
    pkgs += extras.get(name, [])
# Drop environment markers' packages we cannot resolve here; pip handles them.
subprocess.check_call([sys.executable, "-m", "pip", "install", *pkgs])
PY

# ─────────────────────────────── api ─────────────────────────────────────────
# Management plane + the static UI it serves. No LiveKit, no STT/TTS models.
FROM deps AS api

# Reproduce the dev directory layout at /app.
#
# This is load-bearing, not cosmetic: server.py resolves the static UI as
# Path(__file__).resolve().parent.parent / "web" and skills.py resolves the
# banks as parent.parent / "question_banks". Under a plain site-packages
# install those resolve to the wrong place and the `if is_dir()` guard fails
# SILENTLY — the UI would 404 with no error anywhere. With the source at /app
# and PYTHONPATH=/app, `__file__` is /app/interviewer/server.py and both paths
# are correct, exactly as in development.
COPY interviewer/     /app/interviewer/
COPY web/             /app/web/
COPY question_banks/  /app/question_banks/
COPY scripts/         /app/scripts/

ENV PYTHONPATH=/app

USER 10001:10001
EXPOSE 8010

# --proxy-headers so the Ingress's X-Forwarded-Proto is honoured (without it
# generated URLs come out http://). Keep-alive sits above typical LB idle
# timeouts. Replicas are scaled by the orchestrator, never by uvicorn workers,
# so HPA resource accounting stays honest.
CMD ["uvicorn", "interviewer.server:app", \
     "--host", "0.0.0.0", "--port", "8010", \
     "--proxy-headers", "--forwarded-allow-ips=*", \
     "--timeout-keep-alive", "65"]

# ─────────────────────────────── worker ──────────────────────────────────────
# LiveKit agent worker.
#
# NOTE (Phase 2 / US-011): once STT and TTS move to the GPU services this target
# drops faster-whisper, kokoro-onnx and their model weights entirely, which is
# what takes cold start under ~2s. Until then it carries the in-process engines
# so the current behaviour is preserved.
FROM deps AS worker

COPY interviewer/     /app/interviewer/
COPY web/             /app/web/
COPY question_banks/  /app/question_banks/
COPY scripts/         /app/scripts/

ENV PYTHONPATH=/app

# Phase 0 gate — the one genuine unknown in containerisation. The av==12.3.0
# pin exists only because Windows Smart App Control blocks PyAV >= 13's
# unsigned DLLs; on Linux we take a current release. If this import fails the
# image is broken and we learn it at build time, not at 3am.
RUN python -c "import av, livekit.agents; print('av', av.__version__, '| livekit-agents ok')"

USER 10001:10001

# No --reload, no dev subcommand: `start` is the working mode against
# livekit-server 1.13.6 (the legacy `dev` subcommand answers availability with
# "unavailable" on this stack). worker.py appends it automatically when argv
# is bare, but being explicit keeps the container command readable.
CMD ["python", "-m", "interviewer.voice.worker", "start"]

# ─────────────────────────────── rag ─────────────────────────────────────────
# The enterprise-rag-core MCP service. Vendored in this repo for dev/testing;
# in production it is a separate deployment. Multi-replica requires the shared
# backends (Qdrant + Elasticsearch) — see DG-03 — which are selected by env,
# not by this image.
FROM deps AS rag

COPY enterprise-rag-core/ /rag/
WORKDIR /rag

RUN python -m pip install -e ".[mcp]"

# The ONNX reranker is ~22 MiB — small and immutable, so it is baked rather
# than PVC-backed (DG-04). Set explicitly because the ERC CLI resolves it
# relative to its own repo root, the same class of bug as the web/ path above.
RUN enterprise-rag-core download-model || true
ENV RAG_CORE_RERANK_MODEL_PATH=/rag/models/reranker/minilm-int8.onnx

USER 10001:10001
EXPOSE 8031

# cli.py defaults --host to 127.0.0.1, which would make the pod unreachable.
CMD ["enterprise-rag-core", "serve", "--host", "0.0.0.0", "--port", "8031"]
