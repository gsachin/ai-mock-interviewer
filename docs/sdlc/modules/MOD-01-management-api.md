# Management API — Module Doc

> **Module:** MOD-01 · **Form:** Service (existing) · **Defines:** TRD-01
> **Engagement:** Brownfield/Partial · **Lenses:** BA (BRD half) + TPO/Architect (TRD half)
> **Source of truth for IDs:** `01-brd.md`, `02-use-cases-workflows.md`, `03-data-state-analysis.md`, `04-coverage-gap-analysis.md`, `05-modularization.md`, `06-architecture.md`, `07-brownfield-reconciliation.md`
> **Code under design:** `interviewer/server.py`, `interviewer/session_store.py`, `interviewer/skills.py`, `web/`

## BRD Half — Business Requirements (BA Lens)

### Purpose

The management plane is everything in the product that is **not** the live interview. It creates sessions, mints LiveKit room tokens, answers `GET /sessions/{id}`, serves the Skill Update page, exposes the bank CRUD surface, and answers one control-plane question for the autoscaler: how much interview work is pending right now.

It is request/response only. **Audio never crosses it** — that is a deliberate existing design choice and this plan preserves it. A replica of this service can restart, scale, or die without touching a candidate mid-sentence, which is the property that makes the whole two-plane architecture work.

### Business Requirements Served

| Requirement | Statement (abbreviated) | How MOD-01 satisfies it |
|---|---|---|
| BRD-01 — Stateless management API tier | Any replica serves any session created on any other replica | The process-local `_registry: dict[str, Session]` + `threading.Lock` at `server.py:48-49` is deleted; all session reads and writes go through MOD-09 (`RedisSessionStore`, key `interviewer:session:{sid}`) |
| BRD-02 — Real liveness and readiness probes | Liveness checks process only; readiness reports per-dependency state, RAG gated behind a flag defaulting **off** | `/healthz` (checks nothing, no I/O) and `/readyz` (per-dependency, RAG behind `READINESS_REQUIRES_RAG=false`) replace the hardcoded `{"status":"ok"}` at `server.py:61` |
| BRD-03 — Configuration-driven CORS | Allowed origins from config, defaulting to today's four localhost values | The 4-entry hardcoded allowlist at `server.py:38-46` becomes `INTERVIEW_CORS_ORIGINS`, parsed at startup, defaulting to the current four strings |
| BRD-04 — Pluggable bank storage | Bank storage selectable across local FS / S3 / cloud with identical semantics | `/skills` and `/skills/reconcile` call the MOD-08 `BankStore` instead of touching local disk at `server.py:237` |
| BRD-05 — Durable voice sessions | `GET /sessions/{id}` returns a completed voice interview with transcript and scores | The API reads the *same* Redis record the worker writes at wrap — the API is the read side of a store it no longer owns |
| BRD-11 — Production LiveKit | Real credentials, `wss://` reachability, multi-node | `/voice/token` signs with injected keys and asserts their presence at startup; it never falls back to `devkey` in a cluster |
| BRD-12 — No secrets in images or the repository | Credentials injected per environment | LiveKit key/secret and RAG token arrive as environment variables from the secret store (MOD-13); the app only reads them |
| BRD-13 — Observability with the budget as an SLO | Metrics, structured logs, SLO alerting | `/metrics` scrape target; session-id on every log line; the active-interviews gauge derives from the same computation as the scaler endpoint |
| BRD-14 — Cloud-agnostic and on-prem from one base | One portable base, cloud specifics in overlays | The image is the same in every environment; every environment-specific value is an env var |
| BRD-17 — Multi-tenancy and authentication decided | **Deferred — PO owner** | The `/internal/scaler/voice-queue` endpoint is cluster-internal and must not be internet-exposed; today *every* other route is unauthenticated, and `tenant_id` is hardcoded `"default"` at `server.py:119`. This module does not close that gap and says so. |

### Actors & Use Cases

| Use case | Actor | Interaction with this module |
|---|---|---|
| UC-01 — Take a voice interview | Candidate | `POST /voice/token` creates the session, mints the JWT, and writes the room→session map; `GET /sessions/{id}` returns the result afterwards |
| UC-02 — Take a text interview | Candidate | Session create/read only; the FSM runs off this plane |
| UC-03 — Upload a new skill bank | Skill author / Admin | `POST /skills` — validate, store, register; the upload must be visible to every replica without a restart |
| UC-04 — Reconcile unregistered banks | Skill author / Admin | `POST /skills/reconcile` — the repair path for the stored-but-unregistered state |
| UC-05 — Deploy or upgrade the platform | Platform operator | Rolling update with `maxUnavailable: 0`; `/readyz` gates traffic |
| UC-07 — Resume after an interrupted interview | Candidate | Read the interrupted session, show the reason, offer a fresh one |
| UC-08 — Rotate LiveKit keys or secrets | Platform operator | Startup assertion turns a key/secret mismatch into a loud failure instead of an opaque "invalid token" |
| UC-09 — Investigate a latency-breach | On-call engineer | Serves `/metrics`; the scaler endpoint discloses current load |
| UC-10 — Diagnose a worker that never registered | On-call engineer | `/internal/scaler/voice-queue` distinguishes "scaled but idle" from "not scaled" |
| UC-11 — Register and dispatch an agent into a room | System / Agent | Provides the room and the room→session map that dispatch depends on |

### Business Acceptance Criteria

1. Two API replicas behind a Service return the **same** `GET /sessions/{sid}` body for a session created on either one. Verified by the BRD-01 cross-replica round-trip test (US-004, US-005).
2. `GET /healthz` returns 200 within 5 ms even when Redis, RAG, and the bank store are all unreachable — it checks nothing, and that is correct behaviour for liveness.
3. `GET /readyz` returns 503 with a per-dependency body naming the failed dependency when Redis is down, and returns **200** when only RAG is down (flag defaults off). Verified by US-006.
4. A browser served from a real hostname can call `POST /voice/token` successfully, and the default allowlist in an unconfigured deployment still equals today's four localhost origins. Verified by the config-default tripwire test (US-007).
5. A skill uploaded through replica A is discoverable by replica B within the documented staleness window, with no restart of anything. Verified by the two-replica bank test (US-017 adjacency).
6. `POST /voice/token` either returns a complete `{livekit_url, token, room, session_id}` or fails with a typed error; there is no state in which a room exists but no session record does.
7. Every route is reachable. The static mount at `/` does not shadow any API route — locked by a regression test, because this codebase has already shipped a comparable shadowing bug.

## TRD-01 — Technical Requirements (TPO / Architect Lens)

### Non-Functional Requirements

| # | Requirement | Target | Measured today | How it is measured |
|---|---|---|---|---|
| NFR-1 | `GET /healthz` latency | ≤ 5 ms p99, zero dependencies | n/a — `/health` returns a constant | Histogram per route; alert on any non-2xx |
| NFR-2 | `GET /readyz` latency | ≤ 1000 ms hard ceiling, 250 ms timeout per dependency | n/a — no probe exists | Histogram; run in parallel, not serially, so the ceiling is one timeout not four |
| NFR-3 | `GET /sessions/{sid}` latency | ≤ 50 ms p95 at 100 RPS/replica | < 1 ms (process-local dict, single replica only) | Route histogram; the delta is Redis RTT (1–3 ms in-cluster) plus deserialization |
| NFR-4 | `POST /voice/token` latency | ≤ 120 ms p95 | not instrumented | Route histogram; JWT signing is ~1 ms, so the cost is the Redis write |
| NFR-5 | Replica count | ≥ 2 for availability | 1 (cannot be 2 today — BRD-01) | `kube_deployment_status_replicas_available` |
| NFR-6 | Cold start (pod ready) | ≤ 5 s | n/a (no container) | Kubelet events; the image carries no models and no `ctranslate2` |
| NFR-7 | Redis pressure per replica | ≤ 50 pooled connections | n/a | `redis_client_pool_connections`; the module is the only Redis consumer besides the worker |
| NFR-8 | Static UI served same-origin | 0 CORS preflights for the UI's own calls | same-origin today | Browser network panel in the e2e run |
| NFR-9 | Error taxonomy | Every failure is a typed 4xx/5xx with a machine-readable code | free-text `HTTPException` details | Contract test over the OpenAPI schema |

**Resource envelope:** requests `250m` CPU / `512Mi` memory, **no CPU limit** (see MOD-13 — CPU limits cause CFS throttling, which on a request/response tier shows up as p99 jitter). HPA on CPU is a fair signal here precisely because the requests are short and uniform.

### Interfaces Exposed

All routes are declared on an `APIRouter` that is mounted **before** the `StaticFiles` mount at `/` (`server.py:317-319`). This is structural, not stylistic: `StaticFiles` is a catch-all and silently shadows anything declared after it. Extracting the routes into a router removes the ordering footgun permanently (REC-07).

| Method | Path | Status | Purpose | Notes |
|---|---|---|---|---|
| GET | `/healthz` | **new** | Liveness | No I/O. Returns `{"status":"ok"}` unconditionally — a liveness probe that checks dependencies is a restart loop waiting to happen |
| GET | `/readyz` | **new** | Readiness | Per-dependency body; 200/503. RAG gated by `READINESS_REQUIRES_RAG` (default `false`) |
| GET | `/health` | existing | Migration alias | Kept so the existing launcher probe keeps working for one release; returns the `/healthz` body |
| POST | `/voice/token` | existing | Mint a LiveKit JWT, create the session | Body `{domain}`. Returns `{livekit_url, token, room, session_id}`, with a 12-hex session id |
| GET | `/sessions/{sid}` | existing | Read a session incl. summary and scores | 404 = genuinely absent; **503 = store unavailable**. Conflating these is what makes an outage look like data loss |
| GET | `/sessions/{sid}/scores` | existing | Score ledger only | Cheap read for the results view |
| GET | `/skills` | existing | List banks with registration state | Reads through MOD-08's cache, not the raw folder |
| POST | `/skills` | existing | Upload, validate, store, register | Write-then-register ordering preserved (WF-02) |
| POST | `/skills/reconcile` | existing | Register present-but-unregistered banks | Probes all banks before writing anything |
| GET | `/internal/scaler/voice-queue` | **new** | KEDA `metrics-api` scaler source | Returns `{"pending": n, "active": n, "total": n}`. Cluster-internal only; not routed by the public ingress |
| GET | `/metrics` | **new** | Prometheus scrape | MOD-12 definitions behind intent-named functions |

```mermaid
flowchart LR
  EXT[Public ingress] --> RT
  INT[Cluster-internal] --> RT
  RT[APIRouter<br/>declared BEFORE the static mount] --> H{route}
  H -->|/healthz| L[liveness<br/>no I/O, 5 ms]
  H -->|/readyz| RD[readiness<br/>Redis · banks · RAG flag]
  H -->|/sessions/{sid}| S[MOD-09 SessionStore]
  H -->|/voice/token| T[mint JWT<br/>write room to session map]
  H -->|/skills| B[MOD-08 BankStore]
  H -->|/internal/scaler/voice-queue| Q[MOD-09 scan<br/>pending + active]
  RT --> ST[StaticFiles mount /<br/>MUST be last]
```

**Ready semantics.** `/readyz` returns 200 only when every *required* dependency is satisfied. Redis is required. The bank store is required **unless the materialized cache is within TTL** — a store outage with a warm cache must not remove a serving replica from the pool. RAG is not required by default, because the application is explicitly built to survive a RAG outage (UC-01 degraded-dependency row) and gating readiness on it would let a RAG blip take `/voice/token` and the static UI down with it. The response body always enumerates **all** checks, passing and failing, so a partial failure is diagnosable from one curl.

### Interfaces Consumed

| Dependency | Module | Protocol | Contract | Timeout / budget |
|---|---|---|---|---|
| Session store | MOD-09 | Redis (`redis.asyncio`) | `SessionStore` protocol: `get` / `save` / `delete` | 250 ms per op; key `interviewer:session:{sid}`, TTL 86400 |
| Bank store | MOD-08 | `BankStore` protocol | `list` / `get` / `put` / `delete` | 300 ms read; cache hit ≤ 5 ms |
| RAG service | MOD-04 | MCP over HTTP `:8031` | `register_bank`, and a health probe for `/readyz` | 120 s for `register_bank` (not the 30 s default); 250 ms for the readiness probe |
| LiveKit | MOD-10 | **None** — JWT signed locally | HS256 over the room name, grants `room_join` + publish + subscribe | No network call, so token minting cannot fail on SFU reachability |
| Metrics | MOD-12 | In-process library | Intent-named metric functions | Non-blocking |

**A connection-pooling requirement fall on this module too.** The codebase constructs a fresh `httpx.AsyncClient` per call at `llm.py:88,116`, `tts.py:39,68`, `stt.py:36`, and `rag_client.py:88` — and `rag_client` additionally re-runs the full MCP `initialize()` handshake on every call. MOD-01 owns the RAG probe and the `register_bank` call, so it must hold **one** long-lived client created in the FastAPI lifespan, not one per request.

### Data Model

| Asset | Where it lives now | Where it lives after | Owned by |
|---|---|---|---|
| DAT-01 — Session (FSM state, turns, score ledger) | `_registry` dict in this process (`server.py:48`) | Redis via `RedisSessionStore` | Written by MOD-02, read by MOD-01 |
| DAT-02 — Interview summary + latency ledger | Logged only, never stored (`agent.py:281-286`) | Redis, normalized shape | Written by MOD-02, read by MOD-01 |
| DAT-03 — Question banks (9 files, 26,710 B) | Local folder globbed per request (`skills.py:39`) | `BankStore` + ~10 s materialized cache | MOD-08; MOD-01 is the write path |
| Room → session mapping | **Does not exist** | Redis, same TTL as the session | MOD-01 writes it at token time |

**The serializer is the blocker, not the store.** `RedisSessionStore.save` calls `json.dumps(summary)`, which raises on a real `Session` because it holds an `InterviewerState` enum and `Turn` dataclasses. `Session.to_dict()` / `from_dict()` is therefore a **required companion**, not a nicety (DG-02, REC-04) — and `state_machine.py`'s docstring already promises it. Round-tripping must be lossless: FSM state, turn list, per-question scores, timings and errors.

**The room→session map closes a latent bug.** `agent.py:97` sets `session_id = ctx.room.name`, which does not match the 12-hex id `POST /voice/token` stores. Rather than change the worker's identity model, MOD-01 writes `interviewer:room:{room_name} -> {sid}` when it mints the token, and the worker resolves the true session id from it. One write on the create path repairs the join without touching `run_agent`'s structure (REC-08).

### Stack Choices & Rationale

| Choice | Alternative rejected | Why |
|---|---|---|
| Keep FastAPI + uvicorn, one worker process per pod | Gunicorn multi-worker in one pod | Replicas are the scaling unit; multiple worker processes per pod make the HPA signal and the connection pools harder to reason about, and buy nothing at this request rate |
| Route extraction into `APIRouter`, declared before the static mount | Keeping module-level `@app.get` decorators in `server.py` | The ordering constraint is currently enforced by a comment and has already been violated once in this codebase. A router mount makes the correct order the only possible order |
| Reuse `RedisSessionStore` | A new store abstraction or an external database | The protocol is already exactly what `GET /sessions/{id}` needs. A new abstraction would be pure waste; the only missing piece is a serializer (DG-02) |
| Configuration-driven CORS with today's values as the default | Wildcard `*` | A wildcard would make the default deployment *less* safe than it is today while looking more convenient; a config default preserves the current posture and is testable as a tripwire (BRD-03) |
| Bind `0.0.0.0` with `--proxy-headers` | Binding `127.0.0.1` as today | A pod bound to loopback is unreachable from the Service VIP. `--proxy-headers` is required so the UI and logs see the real client host behind the ingress rather than the proxy's |
| `READINESS_REQUIRES_RAG` defaulting to **false** | Gating readiness on RAG | Inverse of the usual instinct, and correct here: RAG is a *degradable* dependency by design, so making it mandatory would convert a RAG blip into a full outage of token issuance |

### Failure & Degradation Behaviour

| Failure | Detected by | Behaviour | Candidate sees |
|---|---|---|---|
| Redis unreachable | `/readyz` fails; store ops raise | Replica leaves the ready set; in-flight requests return **503** (not 404) on session routes; `/healthz` and the static UI keep serving | A retryable error, not "your session does not exist" |
| RAG unreachable | `/readyz` reports the failure but stays **200** (flag off) | Bank reads serve from cache; `POST /skills` returns 502 **after** the file is stored, leaving the reconcile-repairable state | Unchanged for the candidate |
| Bank store unreachable | Cache refresh fails | Serve the **stale** cache and log the refresh failure; fail writes with 503 | Unchanged, until they upload |
| LiveKit keys missing | **Startup assertion** | Process exits at boot with a named error | Nothing — the pod never joins the ready set. Failing loudly beats an opaque "invalid token" mid-interview (UC-08) |
| `register_bank` exceeds its budget | 120 s deadline | 502 with the bank retained; `/skills/reconcile` repairs it | Admin sees the failure and a repair action |
| A route is declared after the static mount | Regression test in CI | Build fails | Nothing — the class of bug is eliminated, not detected |
| Static UI assets missing (package-only install) | `/` returns 404 | The mount path resolves two levels up from `server.py`; the image must reproduce the dev layout at `/app` with `PYTHONPATH` set (Discovery Challenge #18) | Would be a blank page — caught by the e2e smoke test before release |

**Scaling and its limits.** HPA on CPU is a fair metric for this tier because requests are short and uniform. The saturation limit at 10× is Redis connection count, not CPU: each replica holds a pool, and the degradation of choice is to shed skills **writes** with 503 first (they are admin-rate, repairable via reconcile) while keeping session **reads** serving, because a candidate's results are not recoverable by a retry of a different request.

### Open Items & Risks

| # | Item | Type | Owner |
|---|---|---|---|
| 1 | `/internal/scaler/voice-queue` is unauthenticated | Security | Cluster-internal NetworkPolicy is the mitigation; a token is a MOD-13 concern. Recorded, not assumed |
| 2 | No authentication on any public route | Scope gap — BRD-17 | PO |
| 3 | `BRD-13`/`BRD-09` SLO is 4–6× from measured reality (CV-03) | Product decision | PO |
| 4 | `/health` alias removal | Cleanup | Remove one release after cutover; ticket only |
