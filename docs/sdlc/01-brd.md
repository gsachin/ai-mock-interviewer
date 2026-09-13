> **Lens:** BA · **Decided by:** BA, confirmed by PO · **Engagement:** Brownfield/Partial · **Defines:** BRD-01…BRD-18

# Business Requirements Document — ai-mock-interviewer Autoscaling

## Discovery Matrix

| # | Dimension | Finding | Evidence | Gaps |
|---|---|---|---|---|
| 1 | Business goals | Elastic capacity for concurrent interviews; cut voice latency 4–6×; remove the single-Windows-host dependency | User-provided | Cost ceiling per interview undefined |
| 2 | Stakeholders & actors | Candidate, Skill author/Admin, Platform operator, On-call engineer, System/Agent | Derived from `web/index.html`, `web/skills.html`, `docs/DONE_AND_PENDING.md` | No formal ownership of the auth/retention decisions |
| 3 | Functional scope | Voice interview, text interview, skills CRUD, RAG retrieval, LLM-judge scoring | Code-observed `interviewer/` | Streamlit text UI dropped from cluster scope |
| 4 | Data | Session (state, turns, scores), interview summary + latency ledger, 9 question banks (26,710 B), RAG chunks + vectors, BM25 index, GPU model weights, candidate audio buffer. **No database, no users table, no auth.** | Code-observed `server.py:48`, `session_store.py`, `skills.py`, `agent.py:137` | Retention/privacy posture undefined (`BRD-18`) |
| 5 | Integrations | LiveKit SFU (WebRTC + data packets); RAG service over MCP; OpenAI-compatible LLM; Deepgram/Cartesia/ElevenLabs cloud options | Code-observed `rag_client.py`, `llm.py`, `voice/{stt,tts}.py` | LiveKit version's pending-job metric unverified |
| 6 | Non-functional | 1500 ms voice round-trip budget; 48 kHz mono s16le PCM contract; natural-voice policy (espeak-class excluded); 900 s worst-case interview | Document-observed `budget.py:10`, `protocols.py`, `tts.py:20` | 1500 ms may be unachievable even on GPU |
| 7 | Constraints | Cloud-agnostic K8s; on-prem capable from the same base; `av==12.3.0` pinned for Windows Smart App Control; no container, no CI, no secret management today | User-provided + Code-observed `pyproject.toml` | None |
| 8 | Assumptions | AS-01…AS-05 | See §7 of `00-product-intent.md` | AS-02 (KEDA) is load-bearing |
| 9 | Risks | GPU floor cost dominates; LiveKit dev-mode worker drop (P2); in-flight interview loss on worker death; WebRTC over ingress is environment-sensitive | Document-observed `DONE_AND_PENDING.md` | None |
| 10 | Compliance & privacy | **None identified.** No PII handling, no retention policy, no DPA, no consent capture — yet interviews are personal data by nature | Code-observed (absence) | `BRD-18` — explicit PO decision required |
| 11 | Operational | FastAPI has no logging configuration at all; no metrics; `/health` hardcodes `"ok"` and checks nothing; worker logs plain text to a temp file | Code-observed `server.py:61`, `worker.py:22` | `BRD-02`, `BRD-13` |
| 12 | Analytics | Per-hop `timings_ms` ledger and `voice_budget_bar` already computed per session; `LatencyBudgetTracker` implements the budget gate | Code-observed `voice/budget.py`, `brain.py:188` | Exported nowhere — wiring, not measurement |
| 13 | Lifecycle & support | Sessions TTL 24 h; no bank versioning; no backup/restore for Chroma or the bank folder; no rollback path | Code-observed `session_store.py:32`, `enterprise_rag/config.py:171` | `DG-01`, `DG-03` decisions |

## Requirements

### BRD-01 — Stateless management API tier

The API tier holds no session state in process memory: any replica serves any session created on any other replica. *Rationale: `server.py:48` keeps `_registry: dict[str, Session]` behind a `threading.Lock`, so scaling past one replica makes `GET /sessions/{id}` 404 non-deterministically.* **Must.**

### BRD-02 — Real liveness and readiness probes

Liveness checks only process health; readiness reports per-dependency state and reflects what the pod can actually serve. RAG availability gates readiness behind a config flag defaulting to **off**. *Rationale: `/health` returns a hardcoded `"ok"` (`server.py:61`); the app is explicitly built to survive a RAG outage, so making RAG mandatory for readiness would let a RAG blip take down `/voice/token` and the static UI.* **Must.**

### BRD-03 — Configuration-driven CORS

Allowed origins come from configuration, defaulting to today's four localhost values. *Rationale: the hardcoded four-entry localhost allowlist (`server.py:38-46`) blocks every request once served from a real hostname behind an ingress.* **Must.**

### BRD-04 — Pluggable bank storage with identical semantics

Question-bank storage is selectable across local filesystem, S3-compatible object storage, and cloud object stores, with the same read/write/list semantics and a bounded staleness window. *Rationale: `POST /skills` writes to the pod's local disk (`server.py:237`) while every request re-globs that folder, so replica B never sees replica A's upload; and per the user constraint the same base must serve cloud and on-prem.* **Must.**

### BRD-05 — Durable voice sessions

Voice interview results are persisted, and `GET /sessions/{id}` returns a completed voice interview with its transcript and scores. *Rationale: closes open item P4; today `agent.py:281-286` only logs the summary, so `GET /sessions/{id}` is permanently empty for voice rooms.* **Must.**

### BRD-06 — Scale-down drains, never kills

Removing a worker replica stops new dispatch to it and waits for in-flight interviews to finish before termination. *Rationale: LiveKit pins an interview to one worker process for its lifetime; a naive scale-down cuts a live candidate mid-sentence.* **Must.**

### BRD-07 — Worker replicas follow interview queue depth

Worker replica count tracks pending-plus-active interviews, not CPU. *Rationale: post-extraction the worker is I/O-bound, so CPU would scale on VAD bursts rather than on interviews — the wrong work unit.* **Must.**

### BRD-08 — AI engines served as independent GPU services

STT, TTS, and LLM are served by separate GPU-backed services behind the existing engine protocols, and the worker becomes CPU-only. *Rationale: eliminates the 3.6 s CPU TTS and 1.0 s CPU STT from the critical path, and — critically — makes the worker I/O-bound so its CPU-load dispatch gate becomes accurate again, which is what unblocks correct multi-replica dispatch.* **Must.**

### BRD-09 — Measurable voice latency against the budget

Every voice turn records per-hop latency and reports it against the 1500 ms budget, with the existing instrumentation exported as metrics. *Rationale: the data is already computed by `LatencyBudgetTracker` and `_hop()`; it is simply not exported, so the product's core quality claim is currently unfalsifiable in production.* **Must.**

### BRD-10 — Horizontally scalable RAG service

The RAG service runs at more than one replica, requiring BM25 off process memory and vectors off local disk. *Rationale: `bm25_memory.py` holds the index in RAM warmed only at boot and Chroma persists to local disk (`config.py:171-175`), so a bank registered on replica A is invisible to replica B.* **Must.**

### BRD-11 — Production LiveKit

LiveKit runs with real credentials, TLS-terminated `wss://` reachability from the browser, and multi-node support. *Rationale: `livekit-server --dev` with `devkey`/`secret` and a `127.0.0.1` URL cannot serve a remote candidate; the launcher currently works around this by restarting the management plane after tunneling.* **Must.**

### BRD-12 — No secrets in images or the repository

Credentials are injected per environment from a secret store; nothing sensitive is baked into an image or committed. *Rationale: today every key lives in a developer's shell environment, and no secret management exists at all.* **Must.**

### BRD-13 — Observability with the latency budget as an SLO

Metrics, structured logs to stdout, and an SLO for the voice latency budget with burn-rate alerting. *Rationale: no metrics and no tracing exist; the FastAPI app has no logging configuration; the 1500 ms budget is a product promise that is currently unmonitored.* **Must.**

### BRD-14 — Cloud-agnostic and on-prem deployable from one base

A single portable manifest base deploys to any conformant cluster, with cloud-specific concerns isolated in thin overlays. *Rationale: user constraint; this is what forced the object-storage decision over an RWX PVC in `DG-01`.* **Must.**

### BRD-15 — Continuous integration

Every change is built, tested, and has its manifests validated before merge. *Rationale: no CI exists for this repo at all; there is no image, no registry, and no automated gate.* **Should.**

### BRD-16 — Interrupted interviews are reported, not lost

An interview whose worker dies is recorded with an `interrupted` state and a reason, and stale in-progress sessions are reconciled. *Rationale: a worker death currently produces silence — the candidate sees "Connected" over dead air and the session is never written anywhere.* **Should.**

### BRD-17 — Multi-tenancy and authentication model decided

An explicit decision on whether the platform is single-tenant or multi-tenant, and what authenticates a candidate and an administrator. *Rationale: no user model, no login, no auth on `/sessions/{id}` or `/voice/token`, and `tenant_id` is hardcoded `"default"` (`server.py:119`); 12 hex characters is not a secret. Currently parked as item P5.* **Deferred — PO owner.**

### BRD-18 — Interview data retention and privacy posture decided

An explicit decision on what interview data is stored, for how long, who can read it, and on what legal basis. *Rationale: interviews are personal data by nature — a candidate's spoken words and scored performance — and today there is no retention policy, no deletion path, and no consent capture. Not a formality.* **Deferred — PO owner.**

## Evidence Register

| Fact | Class | Source | Used by |
|---|---|---|---|
| `AS-01` GPU nodes available in target cluster | User-provided | Conversation | GPU tier design |
| `AS-02` KEDA acceptable as a cluster dependency | Assumed | Not yet confirmed with platform owner | `BRD-07`, worker autoscaling |
| `AS-03` Peak concurrency and sessions/day unknown | User-provided | Conversation | Load & Capacity Model (`03-data-state-analysis.md`) |
| `AS-04` 121 passing unit tests are the regression floor | Code-observed | `docs/STATUS_PHASE4.md`, `DONE_AND_PENDING.md` | All refactor waves |
| `AS-05` Windows dev flow keeps working | User-provided | Conversation | `REC-13`, Phase 0 |
| Session registry is a process-local dict | Code-observed | `interviewer/server.py:48-49` | `BRD-01` |
| `/health` hardcodes `status: ok` | Code-observed | `interviewer/server.py:61-67` | `BRD-02` |
| CORS is a four-entry localhost allowlist | Code-observed | `interviewer/server.py:38-46` | `BRD-03` |
| Skill upload writes local disk; requests re-glob the folder | Code-observed | `interviewer/server.py:237`, `skills.py:39` | `BRD-04` |
| Voice summary is logged, never persisted | Code-observed | `interviewer/voice/agent.py:281-286` | `BRD-05` |
| `RedisSessionStore` is dead code on the serving path | Code-observed | `interviewer/session_store.py` vs `server.py` | `BRD-01`, `DG-02` |
| `RedisSessionStore` crashes on a real `Session` (enum + dataclasses) | Code-observed | `session_store.py:47` `json.dumps` | `DG-02`, `REC-04` |
| `worker.py` sets `load_threshold=float("inf")` | Code-observed | `interviewer/voice/worker.py:62` | `BRD-07`, `REC-05` |
| `worker.py` monkey-patches the private `server._is_available` | Code-observed | `interviewer/voice/worker.py:65-78` | `REC-06` |
| `voice/interviewer.py:49` resolves a discarded STT engine | Code-observed | vs `brain.py:124-135` (no `stt` param) | `REC-03` |
| Voice session keyed by room name, not the API's session id | Code-observed | `agent.py:97` vs `server.py:118-122` | `BRD-05`, Phase 3 |
| `LLMMetrics` mutates instance state — not concurrency-safe | Code-observed | `interviewer/llm.py:31-33`, `87`, `115` | `CV-02`, Phase 1 |
| A new `httpx.AsyncClient` per LLM/TTS/STT/MCP call | Code-observed | `llm.py:88,116`, `tts.py:39,68`, `stt.py:36`, `rag_client.py:88` | `BRD-09` |
| `LLMConfig.timeout = 300.0` on the voice path | Code-observed | `interviewer/llm.py:27` | Phase 1 |
| Static mount at `/` shadows routes declared after it | Code-observed | `interviewer/server.py:317-319` | `BRD-02`, `REC-07` |
| BM25 index in RAM, warmed only at boot | Code-observed | `enterprise_rag/adapters/bm25_memory.py:27-31` | `BRD-10` |
| Chroma persists to local disk | Code-observed | `enterprise_rag/config.py:171-175` | `BRD-10` |
| Qdrant and Elasticsearch adapters already exist and are tested | Code-observed | `enterprise_rag/adapters/` | `DG-03` |
| Voice round-trip measured 6.5–8.4 s vs a 1500 ms budget | Document-observed | `docs/STATUS.md` §4 | `BRD-09`, `CV-03` |
| Dev-mode LiveKit drops an idle worker ~20 s and it does not reliably re-register | Document-observed | `DONE_AND_PENDING.md` P2 | `BRD-06`, `BRD-11` |
| `scripts/e2e_voice_client.py` drives a full interview with no microphone | Code-observed | `scripts/e2e_voice_client.py` | Verification strategy |
| `scripts/prepopulate_banks.sh` is already Linux-portable | Code-observed | `scripts/prepopulate_banks.sh` | Phase 4 jobs |
| Extracting STT to a GPU service moves candidate audio across the cluster network — today the mic buffer never leaves the worker process (RAM → in-process Whisper → text) | Derived | Architecture of `BRD-08` vs `agent.py:137` | **`BRD-18`** — raises the privacy stakes: the design now asserts where a candidate's voice travels, and there is no owner-visible answer |
| Peak concurrent interviews at peak | Unknown | Not provided | Load & Capacity Model — parametrized |
| Sessions per day | Unknown | Not provided | GPU floor sizing |
| Whether 1500 ms is achievable on self-hosted GPU | Unknown | Not measured | `CV-03` — validation plan |
