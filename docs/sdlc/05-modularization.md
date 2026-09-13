> **Lens:** TPO + Architect · **Engagement:** Brownfield/Partial · **Defines:** MOD-01…MOD-13

# Modularization — ai-mock-interviewer Autoscaling

## Decomposition Principle

**A module becomes a service only if it has a different scale profile, a different failure domain, or a different resource class. Otherwise it stays an in-process library.**

This is the whole argument, and it is the reason this plan does *not* propose a conventional microservice split. Applied honestly, most of this system should stay as it is: the existing management-plane / voice-hot-path separation already follows the rule correctly, and the parts that must split are split by *resource class* (CPU vs GPU), not by domain noun.

**Decomposition approach: DDD bounded contexts, applied selectively.** The functional boundaries (interview conduct, retrieval, skill administration, inference) are already real bounded contexts with their own vocabulary and data. But bounded context ≠ deployable service. Three of these contexts are libraries because their scale profile is identical to their host.

**Monolith-first vs services.** Not applicable as a fresh decision — this is brownfield, and the system is already partially distributed (RAG is a separate service over MCP). The plan therefore *extends* the existing seams rather than re-cutting them, which is both lower risk and consistent with the `REC-xx` reconciliation rule that the codebase wins over the blueprint.

## Module Map

### MOD-01 — Management API

**Form: Service (existing).** Owns session creation and lookup, LiveKit token issuance, the skills admin surface, and the static UI.
**Scale profile:** Horizontal, stateless, request/response only — short uniform requests make CPU and RPS fair metrics.
**Why a service:** Different failure domain from the realtime path. It must be able to restart, scale, and fail without touching a live interview. Audio never crosses it (a deliberate existing design choice worth preserving).
**Inputs:** HTTP requests. **Outputs:** JSON, JWTs, static assets. **Depends on:** MOD-08, MOD-09, MOD-04.

### MOD-02 — Voice Agent Worker

**Form: Service (existing).** Runs one interview per pod against a LiveKit room.
**Scale profile:** **1 interview = 1 pod**, scaled on pending-plus-active interviews. The work unit is a whole interview pinned to a process for its lifetime.
**Why a service:** Long-lived stateful WebRTC session; scales on a completely different axis from the API; the hardest module in the system.
**Inputs:** LiveKit job dispatch, room audio, control data packets. **Outputs:** synthesized audio, state/turn/score data packets, persisted sessions. **Depends on:** MOD-03, MOD-04, MOD-05, MOD-06, MOD-07, MOD-09, MOD-10.

### MOD-03 — Dialogue Brain

**Form: Library (deliberate).** The interview FSM (`state_machine.py`) and `LLMInterviewer` (`brain.py`).
**Why a library and not a service:** this is the single most important boundary decision in the plan. The brain executes inside the interview turn — 3–4 LLM calls, 2 retrievals, and a TTS call per question against a 1500 ms budget. Extracting it would insert a network hop into every one of those operations, directly hostile to the North Star metric. It is pure logic with no independent scale profile, and `docs/CLAUDE.md` states the FSM *is* the product. **Do not extract it, and do not refactor it for Kubernetes** — add a checkpoint callback and leave the structure alone.
**Depends on:** MOD-04, MOD-05, MOD-06, MOD-07 through injected engine protocols.

### MOD-04 — RAG Service

**Form: Service (separate repo, existing).** Retrieval over MCP: hybrid search, reranking, semantic cache.
**Scale profile:** Horizontal, stateless once vectors and the keyword index are externalized. Short uniform requests (200–360 ms) make CPU a fair proxy.
**Why a service:** Already is one, consumed over MCP; owns its own storage and its own scaling needs.
**Depends on:** Qdrant, Elasticsearch, Redis.

### MOD-05 — LLM Inference

**Form: Service, GPU (new).** vLLM serving an OpenAI-compatible chat endpoint, deployed **twice**: `vllm-voice` (low concurrency, prefix caching, min 1) and `vllm-judge` (aggressive batching, scale to zero).
**Scale profile:** GPU-bound; scaled on `vllm:num_requests_waiting` and KV-cache utilization.
**Why a service:** Different resource class (GPU) and the single largest cost line. Splitting voice from judge is justified because continuous batching *adds* latency under concurrency, which fights the voice budget — while the judge is off the hot path and may scale to zero. `config.py` already separates their models and base URLs, so this is a values change, not a code change.
**Consumed by:** MOD-02 (voice), MOD-03 (judge), MOD-04 (embeddings if self-hosted).

### MOD-06 — STT Inference

**Form: Service, GPU (new).** Whisper-compatible transcription endpoint.
**Scale profile:** GPU-bound, bursty — one final transcription per answer, with an in-flight request gauge as the scaling signal.
**Why a service:** GPU resource class; it is the ~1.0 s CPU cost being removed from the critical path. Degradation mode is falling back to a slower CPU transcription rather than failing.
**Consumed by:** MOD-02 via the existing `STTEngine` protocol.

### MOD-07 — TTS Inference

**Form: Service, GPU (new).** Kokoro-compatible speech synthesis endpoint.
**Scale profile:** GPU-bound with the **highest call rate in the system**, because TTS streams sentence by sentence rather than per answer.
**Why a service:** GPU resource class, and the 3.6 s CPU cost that dominates the current budget breach. Critically, this is the *same Kokoro model the app already uses* — the natural-voice policy in `tts.py` carries over verbatim, so this is a hosting change, not a vendor change.
**Consumed by:** MOD-02 via the existing `TTSEngine` protocol.

### MOD-08 — Bank / Skill Store

**Form: Library with a service-backed implementation (new).** A `BankStore` protocol with local, S3-compatible, and cloud implementations plus a short-TTL materialized cache.
**Why not a service:** no independent scale profile — it is a client of already-existing storage. Making it a service would add a hop to a read that is currently a local file read.
**Constraint:** all three existing glob consumers must read through the same cache, or the documented "a skill uploaded after boot works without a restart" promise becomes a regression.

### MOD-09 — Session Store

**Form: Infrastructure (existing, Redis).** Sessions and interview summaries.
**Why not built:** `RedisSessionStore` already exists and its protocol is exactly what the API needs. The only missing piece is a serializer, which is a few lines — not a reason to build a new store.
**Scale profile:** Stateful, 1 replica for the scaffold; a scale event would be an outage.

### MOD-10 — Realtime Transport

**Form: Third-party (buy).** LiveKit SFU via its official Helm chart.
**Why buy:** the SFU's configuration surface — RTC port ranges, TURN, node-IP advertisement, multi-node Redis, key format — changes between versions and is a maintenance liability to hand-roll. It is cloud-agnostic and maintained upstream. Hand-rolling it has no upside.

### MOD-11 — Web UI

**Form: Static asset (existing).** `index.html` and `skills.html`, vanilla JS, no build step.
**Why not a service:** served same-origin by MOD-01 so the page and `/voice/token` share an origin with no CORS. It ships inside MOD-01's image. Note the mount path resolves relative to `server.py`, so the image must reproduce the dev directory layout.

### MOD-12 — Observability

**Form: Cross-cutting library (new).** Metrics definitions and structured logging.
**Why a library:** it must be callable from every module in-process; a collector tier is not justified at this scale.
**Key design constraint:** all metric definitions sit behind intent-named functions so the backend (Prometheus today, OTel later) is swappable without touching call sites.

### MOD-13 — Platform & Deployment

**Form: Configuration (new).** Kustomize base plus thin overlays, node pools, autoscalers, secrets, policies, CI.
**Why config, not code:** it deploys other modules; it has no runtime of its own. The base must be portable enough to satisfy the cloud-agnostic and on-prem requirement, which is what keeps cloud-specific concerns (storage classes, load-balancer annotations, secret-store bindings) in the overlays.

## Decomposition Justification

**Against team size.** A small team can operate this because the split is 4 services we build (MOD-01, 02, 04, 08-backed) plus 4 bought GPU services, not 13 microservices. The library boundary keeps MOD-03 versioned with MOD-02 rather than coordinated across a network.

**Against scale.** The two tiers that genuinely need independent scaling are the API (request-rate) and the worker (interview count); the GPU tier needs a third, different axis (queue depth). Everything else scales with one of those or is infrastructure.

**Against the constraints.** The cloud-agnostic + on-prem requirement is what makes MOD-13 a first-class module rather than an afterthought, and it is the reason MOD-08 is storage-pluggable rather than PVC-backed.

**Rejected alternatives, with reasons:**
- *Splitting MOD-01 into session / token / skills services* — no independent scale profile, pure operational overhead.
- *Extracting MOD-03 (the brain) as a service* — puts a network hop inside every interview turn against a latency budget already missed by 4–6×.
- *A saga or event bus across modules* — no cross-module transaction exists; the one multi-step flow (skill upload) is already idempotent and repairable by an existing endpoint.
- *Making MOD-08 a service* — adds a hop to what is currently a local file read.
- *Building our own GPU images* — upstream vLLM, Whisper, and Kokoro servers already exist, are maintained, and publish CPU variants that make local development a faithful mirror of the cluster.

**Every `MOD-xx` above has a component flow and a scale profile in `06-architecture.md`.** The eight modules with build work (MOD-01, 02, 04, 05, 06, 07, 08, 13) each receive a BRD+TRD pair under `modules/`.
