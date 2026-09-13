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

# Only curl here. The native dependencies the voice stack needs (libgomp1 for
# ctranslate2/onnxruntime, ffmpeg for pydub's MP3 decode) are installed in the
# layers that actually use them — the API image touches none of them, and
# ffmpeg in particular is a large C library with a long CVE history that has no
# business in the internet-facing component.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

# Non-root. Matches the manifests' runAsUser.
RUN groupadd --gid 10001 app \
 && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin app

WORKDIR /app

# ─────────────────────────────── deps ────────────────────────────────────────
# Two dependency layers, not one. The API image must NOT carry the voice stack:
# ctranslate2, onnxruntime, av and kokoro carry several hundred MB that the
# management plane never touches, and the API is the internet-facing component,
# so they are pure attack surface there. Building the voice layer on top of the
# API layer (rather than beside it) means the two still share a base layer.
#
# Verified by probing the built image, not assumed: an earlier single-layer
# version shipped livekit.agents / faster_whisper / kokoro_onnx / ctranslate2 /
# onnxruntime inside mock-interviewer-api with no use for any of them, which
# also contradicted the split's whole stated rationale.
FROM base AS deps-api

COPY pyproject.toml README.md docker/install-deps.py /tmp/deps/
RUN python /tmp/deps/install-deps.py api

# Adds [voice] on top of the API layer. Only the worker target uses this.
# libgomp1 — OpenMP runtime for ctranslate2 (faster-whisper) and onnxruntime
#            (kokoro).
# ffmpeg   — pydub decodes the MP3 that ElevenLabs returns. Only exercised by
#            the cloud TTS providers, but the provider is a config flip and a
#            missing decoder would otherwise be a runtime surprise.
FROM deps-api AS deps-worker
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 ffmpeg \
 && rm -rf /var/lib/apt/lists/*
RUN python /tmp/deps/install-deps.py voice

# ─────────────────────────────── api ─────────────────────────────────────────
# Management plane + the static UI it serves. No LiveKit, no STT/TTS models.
FROM deps-api AS api

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
FROM deps-worker AS worker

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
FROM deps-api AS rag

# libgomp1 for the ONNX reranker (onnxruntime). No ffmpeg — the RAG service
# decodes no audio.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 \
 && rm -rf /var/lib/apt/lists/*

COPY enterprise-rag-core/ /rag/
WORKDIR /rag

RUN python -m pip install -e ".[mcp,chroma,qdrant,elasticsearch,redisvl]"

# The ONNX reranker is ~22 MiB — small and immutable, so it is baked rather
# than PVC-backed (DG-04). Set explicitly because the ERC CLI resolves it
# relative to its own repo root, the same class of bug as the web/ path above.
RUN enterprise-rag-core download-model || true
ENV RAG_CORE_RERANK_MODEL_PATH=/rag/models/reranker/minilm-int8.onnx

# The reranker's TOKENIZER is a second, separate artifact: `download-model`
# fetches only the .onnx, and the tokenizer is pulled from HuggingFace lazily
# at first use. In-cluster that request is denied -- correctly -- by the
# rag-egress NetworkPolicy, so the service retried five times and died with
# "'[Errno 101] Network is unreachable' ... cross-encoder/ms-marco-MiniLM-L-6-v2".
#
# Baking it is the right fix rather than permitting egress to huggingface.co:
# a locked-down namespace should not need the public internet to start, and the
# artifact is tiny and immutable. Populate the HF cache at BUILD time, when the
# build host does have network, then run offline.
ENV HF_HOME=/opt/hf
RUN python -c "from huggingface_hub import snapshot_download; snapshot_download('cross-encoder/ms-marco-MiniLM-L-6-v2')"
# Offline from here on: a cache miss should fail fast and loudly at startup
# rather than hang on a retry loop against an unreachable host.
ENV HF_HUB_OFFLINE=1

USER 10001:10001
EXPOSE 8031

# cli.py defaults --host to 127.0.0.1, which would make the pod unreachable.
CMD ["enterprise-rag-core", "serve", "--host", "0.0.0.0", "--port", "8031"]
