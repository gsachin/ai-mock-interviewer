# Implementation Status — per user story

> **As of:** 2026-09-13 · **Branch:** `auto-scalling-arch` · **Plan:** `plan-state.md` · **Suite:** 221 passing
>
> Every status below was **verified by inspection or execution**, not recalled. Where a story is partial, the row names exactly which half exists.

## Headline

| Status | Count | Stories |
|---|---|---|
| ✅ **Done and verified** | 9 | US-001, US-003, US-004, US-005, US-006, US-007, US-008, US-009, US-019 |
| ✅ **Done — one path unexercised** | 1 | US-010 (S3 backend written, never run) |
| 🟡 **Partial — manifest/infra only** | 5 | US-014, US-015, US-016, US-017, US-018 |
| ⚠️ **Written, never run** | 1 | US-002 |
| ❌ **Not started** | 4 | US-011, US-012, US-013, US-020 |

**10 of 20 stories complete. Waves 0 and 1 are finished; Wave 2 has not begun.**

## The structural picture has changed

The previous version of this document found that "the deployment is running ahead of the code" — every cluster-tier story manifest-shaped while every application-tier story was untouched. **Wave 1 has largely closed that.** The API tier is now genuinely stateless (US-004), correctly probed (US-006), configurable (US-007), pooled and metric-isolated (US-008), and its uploads are shareable across replicas (US-009/010). `BRD-01`–`BRD-04` are met.

**The inversion now sits one tier down — at the worker.** Waves 2 and 3 are untouched, while their manifests already exist:

| Manifest that exists | Code it describes | Consequence today |
|---|---|---|
| `voice-worker/deployment.yaml` with `terminationGracePeriodSeconds: 900` | `control.py` does not exist | **The worker cannot drain.** Scale-down kills interviews |
| `base/observability/*` + scrape annotations | `metrics.py` does not exist | Nothing is exported; the SLO evaluates over empty series |
| `keda-scaledobjects.yaml` | `/internal/scaler/voice-queue` does not exist; KEDA not installed (0 CRDs) | The worker does not autoscale |

None of those is a defect in the manifests — they are honest placeholders with comments naming their story. But they are why "the cluster is deployed" and "the cluster scales" are currently different claims.

## Wave 0 — Containerize

| Story | Status | Evidence |
|---|---|---|
| **US-001** Container images | ✅ **Done, verified** | Multi-target `Dockerfile` (`base → api｜worker｜rag`). api **287MB**, worker **1.78GB**; on the kind nodes **67.9MB vs 466MB**. `av`/`livekit.agents` gate passes in-image (av 15.1.0, livekit-agents 1.8.1). Banks verified present and served. **Exceeded the story**: dependency layers split so the API no longer carries the voice stack; native deps moved to the layers that use them. |
| **US-002** Local Compose stack | ⚠️ **Written, never run** | `docker-compose.yml` complete with core / `engines` / `rag` / `legacy` profiles and env vars identical to the ConfigMaps. **`docker compose up` has never been executed** — re-confirmed this pass (no compose services running). No configuration of it is verified. |
| **US-003** Config-defaults tripwire | ✅ **Done, verified** | `tests/test_config_defaults.py` — every default asserted against its baseline literal, plus a guard failing when a field is added without a Windows-flow decision. It caught four would-be drift events across Waves 0–1. **Suite: 221 passed, 6 deselected.** |

## Wave 1 — Statelessness — **COMPLETE**

| Story | Status | Evidence |
|---|---|---|
| **US-004** Sessions to Redis | ✅ **Done, verified in-cluster** | `_registry` removed; `_store` is the `SessionStore` seam. **Two replicas, session created via the Service, `redis-sessions dbsize=1` with the exact `to_dict()` payload, 8/8 reads across both replicas.** |
| **US-005** Session serializer | ✅ **Done, verified** | `Session.to_dict()`/`from_dict()`. Unknown keys tolerated (rolling deploys); unknown `state` raises rather than silently rewinding an interview. |
| **US-006** Probes + router | ✅ **Done, verified in-cluster** | `/healthz` (checks nothing), `/readyz` (per-dependency, 503 on failure), `/health` unchanged as a legacy alias. Routes on one `APIRouter` declared before the static mount. **Verified by taking Redis down: `/readyz` → 503 naming `store: TimeoutError` while reporting `banks: ok`; `/healthz` → 200; the pod left the Ready set; Service endpoints went EMPTY; recovered to 1/1 on restore.** |
| **US-007** Config CORS | ✅ **Done** | Origins from config, defaulting to the previously hardcoded four. An explicitly empty value means "no CORS" and is respected rather than falling back — otherwise CORS could not be turned *off*, which is what a same-origin ingress deployment wants. |
| **US-008** httpx pooling + metrics + MCP session | ✅ **Done** | Shared pooled client per engine with explicit `Limits`; **persistent MCP session** (removes a full JSON-RPC `initialize()` handshake from *every* retrieval — the highest-ROI latency fix in the migration, and it needs no GPU); **per-call metrics**, the hard prerequisite for US-011. Two tests prove two overlapping calls on one engine report distinct objects. |
| **US-009** BankStore protocol | ✅ **Done** | `BankStore` protocol + `LocalBankStore` (default, pre-existing behaviour) + `CachedBankStore`. Resolves its root per call so the existing `monkeypatch.setattr(skills, "BANK_DIR", ...)` seam keeps working. |
| **US-010** Object-storage backend | ✅ **Done — S3 path unexercised** | `S3BankStore` written with a lazy `aioboto3` import and a new `[s3]` extra. **`CachedBankStore` is tested against a fake backend, which proves the cache contract — but `S3BankStore` itself has never run against a real S3/MinIO**, because none is available and `aioboto3` is not installed. |

**Known gap inside Wave 1:** the bank cache refresh is **startup-only**. It converges a pod that restarts; a long-lived replica only picks up another replica's upload when something calls the store. A periodic refresh task is outstanding, so the cross-replica promise currently holds for restarts but **not for a running process**.

## Wave 2 — GPU engine services — **NOT STARTED**

| Story | Status | Evidence |
|---|---|---|
| **US-011** Remote engine adapters | ❌ **Not started — infra half done** | **0** references to `whisper-http`/`kokoro-http` or remote adapters. The worker still runs STT/TTS **in-process**. *Infra*: the `cpu-engines` component deploys Ollama, Speaches and Kokoro — all **Running** in kind — and the engine base URLs are already in the ConfigMap, so the flip is a two-value change once the adapters exist. |

**Why this is the pivotal story.** It is what makes the worker CPU-only, gives it a sub-2s cold start, and makes `load_threshold` safe to set finite — which is what unblocks US-014 (drain) and US-018 (autoscaling). **`REC-05` cannot be applied until it lands.** Its stated prerequisite (per-call metrics) is now discharged and directly tested.

## Wave 3 — Durability — **NOT STARTED**

| Story | Status | Evidence |
|---|---|---|
| **US-012** Voice session persistence | ❌ Not started | `agent.py` still keys the session by `ctx.room.name` (4 occurrences), not the API's 12-hex id — the mismatch that makes persistence write to unreachable keys. Summary still only logged. **`GET /sessions/{id}` remains permanently empty for voice rooms (open item P4).** |
| **US-013** Checkpointing + interrupted snapshot | ❌ Not started | No `on_checkpoint`; `_on_shutdown` writes nothing. |
| **US-014** Graceful drain + reconcile | 🟡 **Partial — manifest only** | `terminationGracePeriodSeconds: 900` and `maxUnavailable: 0` are in the deployment. **`interviewer/voice/control.py` does not exist** (re-confirmed this pass), so `/readyz`, `POST /drain` and the `preStop` hook are placeholders. The PDB/KEDA reasoning is documented but unexercised. |

## Wave 4 — Cluster

| Story | Status | Evidence |
|---|---|---|
| **US-015** Metrics + SLO | 🟡 **Partial — manifest only** | ServiceMonitor + PrometheusRule exist; pods carry scrape annotations. **`interviewer/metrics.py` does not exist** (re-confirmed), and there is no `/metrics` endpoint. Nothing is exported and the SLO would evaluate over empty series. **Also**: no Prometheus is installed in the cluster, so even once `/metrics` exists there is no scraper. |
| **US-016** LiveKit production | 🟡 **Partial** | `base/livekit/` runs `1/1`, the key/`LIVEKIT_KEYS` mapping is documented, the `LIVEKIT_PORT` collision is fixed, and an ingress NetworkPolicy was added. **TLS/`wss://`, ingress, cert-manager and multi-node are not done**, and it still runs `--dev`. |
| **US-017** RAG multi-replica | 🟡 **Partial — deployed, single replica** | Image builds with all backend extras and the reranker tokenizer baked; Deployment/Service/HPA exist. **Qdrant and Elasticsearch are not deployed** — the local overlay uses chroma + in-memory BM25 — so the service is single-replica and `BRD-10` is unmet. |
| **US-018** Worker autoscaling | 🟡 **Partial — manifest only** | `keda-scaledobjects.yaml` exists. **KEDA is not installed (0 CRDs)** and **`GET /internal/scaler/voice-queue` does not exist**, so the `metrics-api` trigger has no source. The worker does not autoscale. |
| **US-019** Kustomize base + overlays | ✅ **Done, verified** | 47 files. All five targets build clean (base 44, cpu-engines 13, gpu-engines 26, local 53, production 70). Deployed to kind and iterated against reality. |
| **US-020** CI pipeline | ❌ Not started | No `.github/workflows/`. |

## Unplanned work

Substantial effort went into things no story enumerated.

| Work | Why it existed |
|---|---|
| Local kind cluster + `-WithKind` launcher integration | Chosen direction; 3 nodes, k8s v1.37.0 |
| metrics-server installed | kind ships none, so both HPAs were **inert objects** reporting `unable to fetch metrics`. Autoscaling could not have worked at all |
| **12 defects found by deploying** | See below |

### The defect record — every one found by running it, none visible in a rendered manifest

| # | Defect | Family |
|---|---|---|
| 1 | `.dockerignore`'s `**/*.md` stripped all 9 question banks from the image | dependency/packaging |
| 2 | API image carried the entire voice stack (~630MB dead weight) | dependency/packaging |
| 3 | RAG image installed only `.[mcp]`; the overlay selects chroma → `No module named 'chromadb'` | dependency/packaging |
| 4 | RAG image shipped no reranker tokenizer; egress correctly blocked → retry loop, then death | dependency/packaging |
| 5 | **`redis` lived in `[dev]`, not `[api]`** — image up, probes green, 500 on the first request | dependency/packaging |
| 6 | redis crash-looped: `chown: Operation not permitted` (`drop: [ALL]` removes `CAP_CHOWN`) | read-only-rootfs gap |
| 7 | ollama crash-looped on a read-only `/root/.ollama` | read-only-rootfs gap |
| 8 | livekit crash-looped: injected `LIVEKIT_PORT` collided with its own variable | injected config |
| 9 | **No NetworkPolicy granted ingress to livekit** — Running, listening, unreachable | missing policy |
| 10 | **No NetworkPolicy granted ingress to either Redis** — same shape, found later | missing policy |
| 11 | `kind load` under an unchanged tag silently ignored by `IfNotPresent` | test-signal integrity |
| 12 | `mcp`'s `read_timeout_seconds` takes a **float, not a timedelta** — caught by checking the real SDK after a fake rejected the kwarg | API contract |

**Two coherent families dominate.** Five are *a dependency declared somewhere other than where it is used*; two are *a read-only rootfs plus a data directory nobody mounted*; two are *every workload needs an ingress policy, and two did not get one*.

**Every one produced something that looked healthy.** Five of the twelve were found only because a signal was distrusted rather than believed — an HTTP 200 with an empty Redis, a green build with a broken artifact, a test failure that turned out to be my bug rather than the test's.

## Recommendation

**The case for pulling US-020 forward is now much stronger.** Twelve defects — every one caught by exercising a deployed stack, none by a green build — is a direct measurement of what the CI job in US-020 is worth, and the `e2e` job specifically would have failed on all of them. Landing the validation and e2e jobs *before* Waves 2–3 means the remaining stories get checked automatically instead of the hard way.

If that is not taken, the recommended next story is **US-011**: it is unblocked, it is pivotal, and both US-014 and US-018 depend on it in ways their own manifests cannot express.
