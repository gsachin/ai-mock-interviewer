# Implementation Status — per user story

> **As of:** 2026-09-13 · **Branch:** `auto-scalling-arch` · **Plan:** `plan-state.md`
>
> Every status below was **verified by inspection or execution**, not recalled. Where a story is partial, the row names exactly which half exists.

## Headline

| Status | Count | Stories |
|---|---|---|
| ✅ **Done and verified** | 6 | US-001, US-003, US-004, US-005, US-006, US-019 |
| 🟡 **Partial — manifest/infra only** | 5 | US-014, US-015, US-016, US-017, US-018 |
| ⚠️ **Written, not verified** | 1 | US-002 |
| ❌ **Not started** | 8 | US-007…US-013, US-020 |

**12 of 20 stories are not complete. Wave 1 is 3 of 7 done.**

## The structural finding

**The deployment is running ahead of the code.** Every cluster-tier story (US-014…US-019) is manifest-shaped, while every application-tier story (US-004…US-013) is untouched.

That inversion is visible in the running cluster: the worker reports `0/1 Ready`, and `base/voice-worker/deployment.yaml` carries a commented-out readiness probe because **the control server it would probe does not exist yet** (`US-014`). Several manifests reference endpoints and behaviours that are still unimplemented — they are honest placeholders with comments naming their story, not oversights.

The consequence is the one flagged when we jumped to K8s manifests early: **the cluster currently deploys a service that is not yet stateless.** Multi-replica correctness (`BRD-01`) is still unmet — `server.py:54` keeps `_registry: dict[str, Session]` in process memory.

## Wave 0 — Containerize

| Story | Status | Evidence |
|---|---|---|
| **US-001** Container images | ✅ **Done, verified** | Multi-target `Dockerfile` (`base → api｜worker｜rag`). `api` **287MB**, `worker` **1.78GB**, and **67.9MB vs 466MB** on the kind nodes. `av`/`livekit.agents` gate passes in-image (av 15.1.0, livekit-agents 1.8.1). Banks verified present and served: `/skills` returns 9. **Exceeded the story**: dependency layers split so the API no longer carries the voice stack, and native deps (`libgomp1`, `ffmpeg`) moved to the layers that use them. |
| **US-002** Local Compose stack | ⚠️ **Written, never run** | `docker-compose.yml` complete with core / `engines` / `rag` / `legacy` profiles, CPU engine images, and env vars identical to the ConfigMaps. **`docker compose up` has never been executed** — no configuration of it is verified. |
| **US-003** Config-defaults tripwire | ✅ **Done, verified** | `tests/test_config_defaults.py` — 37 tests asserting every default against its baseline literal, plus a guard failing when a new field is added without a Windows-flow decision. **Suite: 158 passed, 6 deselected** (121 baseline + 37). |

## Wave 1 — Statelessness (`BRD-01`, `BRD-02`, `BRD-03`, `BRD-04`)

**All seven stories not started.** Verified individually:

| Story | Status | Evidence |
|---|---|---|
| **US-004** Sessions to Redis | ✅ **Done, verified in-cluster** | `_registry` removed; `_store` is the `SessionStore` seam. **Two API replicas, session created via the Service, `redis-sessions dbsize=1` with the exact `to_dict()` payload present, 8/8 reads across both replicas.** |
| **US-005** Session serializer | ✅ **Done, verified** | `Session.to_dict()`/`from_dict()`. Unknown keys tolerated (rolling deploys); unknown `state` raises rather than silently rewinding an interview. Confirmed by the same Redis payload above. |
| **US-006** Probes + router | ✅ **Done, verified in-cluster** | `/healthz` (checks nothing), `/readyz` (per-dependency breakdown), `/health` unchanged as a legacy alias. Routes on one `APIRouter` before the static mount. **Verified by taking Redis down: `/readyz` → 503 naming `store: TimeoutError`, `/healthz` → 200, the pod left the Ready set, Service endpoints went EMPTY, and it recovered to 1/1 when Redis returned.** |
| **US-007** Config CORS | ❌ Not started | No `cors_origins` in `config.py`; the four-entry localhost allowlist at `server.py:38-46` is unchanged. |
| **US-008** httpx pooling + metrics | ❌ Not started | **6 per-call `httpx.AsyncClient(` construction sites remain** (`llm.py` 2, `rag_client.py` 1, `stt.py` 1, `tts.py` 2). `LLMConfig.timeout=300.0` still on the voice path; MCP handshake still per retrieval. |
| **US-009** BankStore protocol | ❌ Not started | No `interviewer/bank_store.py`. |
| **US-010** Object-storage backend | ❌ Not started | Same. Banks still read from the pod's image-local folder; **`POST /skills` writes are still lost on any other replica.** |

## Wave 2 — GPU engine services (`BRD-08`, `BRD-09`)

| Story | Status | Evidence |
|---|---|---|
| **US-011** Remote engine adapters | ❌ Not started — **infra half done** | **0** references to `whisper-http`/`kokoro-http` or remote adapters in `stt.py`/`tts.py`. The worker still runs STT/TTS **in-process**. *However* the `cpu-engines` component deploys Ollama, Speaches and Kokoro — all three **Running** in kind — and the engine base URLs are already wired into the ConfigMap, so the flip is a two-value change once the adapters exist. |

**Note:** this is the pivotal story — it is what makes the worker CPU-only, gives it a sub-2s cold start, and makes `load_threshold` safe to set finite. Until it lands, `REC-05` cannot be applied.

## Wave 3 — Durability (`BRD-05`, `BRD-06`, `BRD-16`)

| Story | Status | Evidence |
|---|---|---|
| **US-012** Voice session persistence | ❌ Not started | `agent.py:97` still keys the session by `ctx.room.name`, not the API's 12-hex id — the mismatch that makes persistence write to unreachable keys. Summary still only logged. **`GET /sessions/{id}` remains permanently empty for voice rooms (open item P4).** |
| **US-013** Checkpointing + interrupted snapshot | ❌ Not started | No `on_checkpoint`; `_on_shutdown` writes nothing. |
| **US-014** Graceful drain + reconcile | 🟡 **Partial — manifest only** | `terminationGracePeriodSeconds: 900` and `maxUnavailable: 0` are in `base/voice-worker/deployment.yaml`. **`interviewer/voice/control.py` does not exist**, so `/readyz`, `POST /drain` and the `preStop` hook are placeholders. **The worker cannot currently drain** — the PDB/KEDA reasoning is documented but unexercised. |

## Wave 4 — Cluster

| Story | Status | Evidence |
|---|---|---|
| **US-015** Metrics + SLO | 🟡 **Partial — manifest only** | `base/observability/{servicemonitor,prometheus-rules}.yaml` exist; pods carry `prometheus.io/scrape` annotations. **`interviewer/metrics.py` does not exist and there is no `/metrics` endpoint**, so nothing is exported and the SLO would evaluate over empty series. |
| **US-016** LiveKit production | 🟡 **Partial** | `base/livekit/` deploys and runs (`1/1`), with the key/`LIVEKIT_KEYS` mapping documented and the `LIVEKIT_PORT` collision fixed. **TLS/`wss://`, ingress, cert-manager and multi-node are not done**, and it still runs `--dev`. |
| **US-017** RAG multi-replica | 🟡 **Partial — running, single replica** | Image builds (872MB) with all backend extras; Deployment/Service/HPA exist; `redeploy` in progress after the tokenizer fix. **Qdrant and Elasticsearch are not deployed** — the local overlay uses chroma + in-memory BM25, so the service is single-replica and `BRD-10` is unmet. |
| **US-018** Worker autoscaling | 🟡 **Partial — manifest only** | `components/gpu-engines/keda-scaledobjects.yaml` exists. **KEDA is not installed** (`kubectl get crd \| grep -c keda` → **0**), and **`GET /internal/scaler/voice-queue` does not exist**, so the `metrics-api` trigger has no source. The worker does not autoscale. |
| **US-019** Kustomize base + overlays | ✅ **Done, verified** | 47 files. **All five targets build clean**: base 44, cpu-engines 13, gpu-engines 26, **local 53**, production 70. Deployed to kind and iterated against reality. |
| **US-020** CI pipeline | ❌ Not started | No `.github/workflows/`. |

## Unplanned work (not in any story)

Substantial effort went into things the plan did not enumerate:

| Work | Why it existed |
|---|---|
| **Local kind cluster** + `-WithKind` launcher integration | Chosen direction; 3 nodes, k8s v1.37.0 |
| **metrics-server** installed | kind ships none, so both HPAs were **inert objects** reporting `unable to fetch metrics`. Autoscaling could not have worked at all. |
| **7 deployment defects found and fixed** | See below |

### The seven defects — all found by deploying, none visible from a valid manifest

1. `.dockerignore` stripped all 9 question banks from the image (healthy UI, zero interviews possible)
2. API image carried the entire voice stack (~630MB of dead weight on the internet-facing tier)
3. RAG image installed only `.[mcp]` while the overlay selects chroma → `No module named 'chromadb'`
4. redis crash-looped: `chown: Operation not permitted` (`capabilities.drop: [ALL]` removes `CAP_CHOWN`)
5. livekit crash-looped: Kubernetes' injected `LIVEKIT_PORT` collided with livekit-server's own variable
6. **No NetworkPolicy granted ingress to the SFU** — a Running, listening, unreachable server
7. ollama crash-looped on read-only `/root/.ollama`; RAG image shipped no reranker tokenizer

**Both kustomize targets and the container builds were green throughout.** Every one would have been caught by a single scripted interview attempt against a deployed stack.

## What this says about sequencing

The plan ordered Wave 4 last and said explicitly that jumping to manifests early "would be correct YAML and a broken system". That is precisely the state now: **US-019 is done and verified while `BRD-01` is unmet.**

Three concrete consequences while Wave 1 is outstanding:

- **The API cannot be scaled past one replica.** `GET /sessions/{id}` 404s non-deterministically across replicas; the HPA exists but scaling it up would cause failures, not capacity.
- **`POST /skills` writes are replica-local.** Any upload is invisible to every other replica and to every worker.
- **Nothing measures.** With `metrics.py` absent, the HPA targets and the latency SLO both read empty series — they look configured and cannot fire.

**Recommended next step is unchanged: Wave 1, starting with US-004 and US-005 together.** US-005 is a hard prerequisite — `RedisSessionStore.save` raises `TypeError` on a real `Session` until the serializer exists, so wiring the store alone would trade a 404 for a 500.

**One scope question worth deciding deliberately.** US-020 (CI) was planned last. This session produced seven defects that only surface when a deployed stack is exercised, and an `e2e` job is the cheapest known way to catch that class. There is a real argument for pulling the e2e portion of US-020 forward to sit alongside Wave 1 rather than after Wave 4 — every one of the seven would have failed it.
