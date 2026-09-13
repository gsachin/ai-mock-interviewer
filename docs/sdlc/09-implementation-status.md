# Implementation Status — per user story

> **As of:** 2026-09-13 · **Branch:** `auto-scalling-arch` @ `0fd00e8` · **Plan:** `plan-state.md` · **Suite:** 236 passing
>
> Every status was **verified by inspection or execution**, not recalled. Where a story is partial, the row names exactly which half exists.

## Headline

| Status | Count | Stories |
|---|---|---|
| ✅ **Done and verified** | 9 | US-001, US-003, US-004, US-005, US-006, US-007, US-008, US-009, US-019 |
| ✅ **Done — one path unexercised** | 2 | US-010 (S3 backend never run), US-011 (live audio round-trip never run) |
| 🟡 **Partial — manifest/infra only** | 5 | US-014, US-015, US-016, US-017, US-018 |
| ⚠️ **Written, never run** | 1 | US-002 |
| ❌ **Not started** | 3 | US-012, US-013, US-020 |

**11 of 20 stories complete. Waves 0, 1 done; Wave 2's pivotal story done; Wave 3 not started.**

## The structural picture

Two waves ago the finding was "the deployment is running ahead of the code across every tier". **The API tier is now genuinely converged** — stateless, probed, configurable, pooled, shareable uploads. `BRD-01`–`BRD-04` are met and verified in-cluster.

**The inversion is now confined to the worker tier**, and it has changed character: it is no longer *blocked*, it is merely *not done*. US-011 removed the reason these three could not be built:

| Manifest that exists | Code it describes | State |
|---|---|---|
| `voice-worker/deployment.yaml`, grace 900 s, `maxUnavailable: 0` | `control.py` absent | **Was blocked by US-011. Now merely unbuilt** — the finite `load_threshold` it needed exists |
| `base/observability/*`, scrape annotations | `metrics.py` absent | Unbuilt. Also no Prometheus in-cluster, so there is no scraper either |
| `keda-scaledobjects.yaml` | scaler endpoint absent; KEDA not installed (0 CRDs) | Unbuilt. No longer blocked in principle |

The difference matters for planning: these are now ordinary backlog, not dependencies on unfinished work.

## Wave 0 — Containerize

| Story | Status | Evidence |
|---|---|---|
| **US-001** Container images | ✅ **Done, verified** | Multi-target `Dockerfile`. api **287MB**, worker **1.78GB**; on-node **67.9MB vs 466MB**. In-image `av`/`livekit.agents` gate passes. Banks present and served. **Exceeded**: dependency layers split so the API no longer carries the voice stack. |
| **US-002** Local Compose stack | ⚠️ **Written, never run** | Complete with core / `engines` / `rag` / `legacy` profiles and env vars identical to the ConfigMaps. **`docker compose up` has still never been executed.** |
| **US-003** Config-defaults tripwire | ✅ **Done, verified** | Every default asserted against its baseline literal, plus a guard when a field is added without a Windows-flow decision. It has now caught five would-be drift events. |

## Wave 1 — Statelessness — **COMPLETE**

| Story | Status | Evidence |
|---|---|---|
| **US-004** Sessions to Redis | ✅ **Verified in-cluster** | `redis-sessions dbsize=1` with the exact `to_dict()` payload; 8/8 reads across two replicas via the Service. |
| **US-005** Session serializer | ✅ **Verified** | Unknown keys tolerated (rolling deploys); unknown `state` raises rather than silently rewinding an interview. |
| **US-006** Probes + router | ✅ **Verified in-cluster** | **Verified by failure, not success**: Redis down → `/readyz` 503 naming `store: TimeoutError` while reporting `banks: ok`; `/healthz` stayed 200; pod left the Ready set; Service endpoints went EMPTY; recovered on restore. |
| **US-007** Config CORS | ✅ Done | An explicitly empty value means "no CORS" and is respected — otherwise it could not be turned *off*, which a same-origin ingress deployment wants. |
| **US-008** Pooling + metrics + MCP session | ✅ Done | Persistent MCP session removes a full JSON-RPC handshake from *every* retrieval. Per-call metrics is the prerequisite US-011 depended on. |
| **US-009** BankStore protocol | ✅ Done | Resolves its root per call so the existing `monkeypatch.setattr(skills, "BANK_DIR", ...)` seam keeps working. |
| **US-010** Object-storage backend | ✅ **S3 path unexercised** | `CachedBankStore` tested against a fake backend; **`S3BankStore` has never touched real S3/MinIO**. |

**Gap inside Wave 1:** the bank cache refresh is **startup-only**. It converges a pod that restarts; a long-lived replica picks up another replica's upload only when something calls the store. The cross-replica promise holds for restarts, **not for a running process**.

## Wave 2 — GPU engine services

| Story | Status | Evidence |
|---|---|---|
| **US-011** Remote engine adapters | ✅ **Done — live audio round-trip unverified** | `RemoteWhisperSTT` (`whisper-http`) and `RemoteKokoroTTS` (`kokoro-http`) as additive branches; PCM WAV-wrapped via a new stdlib helper; Kokoro's 24 kHz output converted with the existing tested `wav_to_s16le48k`; `kokoro-http` added to `NATURAL_VOICE_PROVIDERS` (same model, so `af_heart` carries over — a hosting change, not a vendor change). Process-level engines with a per-room fallback. Dead `resolve_stt` deleted. **`load_threshold` inf → 0.95.** Monkey-patch guarded; `livekit-agents` pinned to a minor. |

**Why this was pivotal, and what it unblocked.** The worker is now CPU-only and I/O-bound, which is what makes a finite dispatch gate accurate — and therefore what unblocks US-014 and US-018. Reading livekit's source explained the original `float("inf")` workaround precisely: `_default_load_threshold = ServerEnvOption(dev_default=math.inf, prod_default=0.7)`, so the 0.7 gate the author disabled is the **production** default, and the bursts were the in-process engines. Leaving it infinite is now actively wrong with more than one replica.

**What is NOT verified:** the live audio round-trip through Speaches and Kokoro. The engines run and are wired (`INTERVIEW_STT_PROVIDER=whisper-http` confirmed on the running pod), but no interview has been driven end to end through them. See §"The e2e attempt".

## Wave 3 — Durability — **NOT STARTED**

| Story | Status | Evidence |
|---|---|---|
| **US-012** Voice session persistence | ❌ Not started | `agent.py` still keys the session by `ctx.room.name`, not the API's 12-hex id — the mismatch that makes persistence write to unreachable keys. **`GET /sessions/{id}` remains permanently empty for voice rooms (open item P4).** |
| **US-013** Checkpointing + interrupted snapshot | ❌ Not started | No `on_checkpoint`; `_on_shutdown` writes nothing. |
| **US-014** Graceful drain + reconcile | 🟡 **Partial — manifest only** | Grace period and `maxUnavailable: 0` are in place; **`control.py` does not exist**, so `/readyz`, `POST /drain` and `preStop` are placeholders. **Now unblocked by US-011.** |

## Wave 4 — Cluster

| Story | Status | Evidence |
|---|---|---|
| **US-015** Metrics + SLO | 🟡 **Partial — manifest only** | ServiceMonitor + PrometheusRule exist. **`metrics.py` does not exist** and no Prometheus is installed, so nothing is exported and nothing would scrape it. |
| **US-016** LiveKit production | 🟡 **Partial** | Runs `1/1`; key mapping documented; `LIVEKIT_PORT` collision fixed; ingress policy added. **TLS/`wss://`, ingress, cert-manager, multi-node not done**; still `--dev`. |
| **US-017** RAG multi-replica | 🟡 **Partial — deployed, single replica** | Image builds with all backend extras and the reranker tokenizer baked. **Qdrant and Elasticsearch not deployed** — chroma + in-RAM BM25 — so `BRD-10` is unmet. |
| **US-018** Worker autoscaling | 🟡 **Partial — manifest only** | ScaledObject exists; **KEDA not installed, scaler endpoint absent**. **Now unblocked by US-011.** |
| **US-019** Kustomize base + overlays | ✅ **Done, verified** | All five targets build clean; deployed to kind and iterated against reality. |
| **US-020** CI pipeline | ❌ Not started | No `.github/workflows/`. |

## The e2e attempt — blocked, and why that matters

Attempted this session; **it did not execute**, and is recorded as blocked rather than attempted-and-assumed.

The client must run **inside** the cluster because `/voice/token` returns the in-cluster `LIVEKIT_URL`, which does not resolve on a laptop. `api-ingress` admitted only `ingress-nginx`, so a `voice-worker → api:8010` rule was added. **The rule is applied and the pod labels match, but the connection times out**; deleting and recreating the policy did not make kindnet reprogram it. Other podSelector rules on this cluster work fine (worker→tts, worker→livekit opened immediately), so this is not a general kindnet failure.

**Consequence:** US-011's adapters are verified by unit test and resolution check, and the engines are running and configured — but **no interview has been driven through them**. That is the single most valuable outstanding verification in this migration.

**Workaround for next time:** port-forward `livekit` and `api`, patch **only** the api deployment's `LIVEKIT_URL` to `http://127.0.0.1:7880` (the worker keeps the in-cluster value), and run the client from the host.

## Unplanned work

| Work | Why it existed |
|---|---|
| Local kind cluster + `-WithKind` integration | Chosen direction; 3 nodes, k8s v1.37.0 |
| metrics-server installed | kind ships none, so both HPAs were **inert objects** |
| **13 defects found by deploying** | Below |

### Defect record

| # | Defect | Family |
|---|---|---|
| 1 | `.dockerignore` stripped all 9 question banks from the image | packaging |
| 2 | API image carried the entire voice stack | packaging |
| 3 | RAG image installed only `.[mcp]`; overlay selects chroma | packaging |
| 4 | RAG image shipped no reranker tokenizer | packaging |
| 5 | `redis` lived in `[dev]`, not `[api]` | packaging |
| 6 | redis: `chown: Operation not permitted` (`drop: [ALL]` removes `CAP_CHOWN`) | **unmounted data dir** |
| 7 | ollama: read-only `/root/.ollama` | **unmounted data dir** |
| 8 | **Speaches: `scan_cache_dir()` raises on a missing `hub/`** | **unmounted data dir** |
| 9 | livekit: injected `LIVEKIT_PORT` collided with its own variable | injected config |
| 10 | No NetworkPolicy granted ingress to livekit | missing policy |
| 11 | No NetworkPolicy granted ingress to either Redis | missing policy |
| 12 | `kind load` under an unchanged tag silently ignored by `IfNotPresent` | test-signal integrity |
| 13 | `mcp`'s `read_timeout_seconds` takes a float, not a timedelta | API contract |

**The families have consolidated into four mechanical checks:**

- **5 × packaging** — *a dependency declared somewhere other than where it is used.* One rule would catch all five.
- **3 × unmounted data directory** — *a read-only rootfs or emptyDir missing a path the image requires.* Three occurrences is a checklist item, not coincidence.
- **2 × missing NetworkPolicy** — *every workload needs a matching ingress policy.* A machine check over the rendered manifests.
- **3 × one-offs** — injected config, mutable image tags, an SDK signature.

**Every one produced something that looked healthy, and six were caught only because a signal was distrusted** — an HTTP 200 with an empty Redis, a green build with a broken artifact, and a test failure that turned out to be the implementation's bug rather than the test's.

## Recommendation

**Pull US-020 forward.** Thirteen defects, every one caught by exercising a deployed stack and none by a green build, is a measurement of what the CI job is worth — and the four families above are directly mechanisable:

- a manifest check asserting **every workload has an ingress policy**
- an image-content assertion that **every import the serving path needs is in a runtime extra**
- a lint for **read-only rootfs without a writable mount**, and for **emptyDir paths the image reads at boot**

Any of those would have caught multiple defects before a cluster was involved. The `e2e` job would have caught all thirteen.

If not, the highest-value remaining work is **the blocked e2e run** — not a new story. US-011 is the pivotal change and its live path is the one thing still unproven; everything in Wave 3 is built on the assumption that it works.
