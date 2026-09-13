> **Lens:** TPO + Architect · **Engagement:** Brownfield/Partial · **Defines:** REC-01…REC-13

# Brownfield Reconciliation — ai-mock-interviewer Autoscaling

**Governing rule: the codebase wins over the blueprint.** Every conflict between this plan and the existing implementation is recorded here as a reconciliation note. Nothing was silently designed around, and nothing is proposed for replacement merely for being older than an alternative.

Reconciliation dispositions: **Reuse** (already correct — wire it up) · **Extend** (additive change) · **Refactor** (change the existing code) · **Harden** (keep behaviour, fix fragility) · **Delete** (dead code) · **Preserve** (leave alone deliberately).

### REC-01 — Reuse: `session_store.py` already has the right abstraction

`RedisSessionStore` and the `SessionStore` protocol exist and are correct, but they are dead code on the serving path — only `demo.py` and `web/streamlit_app.py` use them, while `server.py` keeps its own in-process dict.
**Disposition: Reuse.** The protocol's `save`/`load`/`delete` is exactly what `GET /sessions/{id}` needs, so building a new store would be waste. This is why `BRD-01` is a small change rather than an architectural one — the abstraction was already right, it simply was not wired in.

### REC-02 — Reuse: `voice/protocols.py` signatures are already network-shaped

`STTEngine.transcribe(bytes) -> str` and `TTSEngine.synthesize(text) -> bytes` describe a request/response service, not an in-process object.
**Disposition: Reuse.** The GPU extraction in `BRD-08` is therefore *additive* — new implementations behind an existing contract — not a restructuring. The seam was designed well and the plan's job is to use it, not to redesign it.

### REC-03 — Delete: a dead STT engine resolution

`voice/interviewer.py:49` calls `resolve_stt(...)` and discards the result. Verified against `LLMInterviewer.__init__` (`brain.py:124-135`), which has **no `stt` parameter** — the constructor accepts `tts`, `voice_llm`, `budget`, `sink`, `on_event`, and `decider`, but not `stt`. The live STT is resolved separately at `agent.py:132`.
**Disposition: Delete.** Zero-risk removal. It also matters for the plan's correctness: the "double engine construction per room" concern is really *one live and one dead*, so `BRD-08`'s engine-injection work is smaller than it appears.

### REC-04 — Extend: a serialization promise the code makes but does not keep

`state_machine.py`'s docstring states session state "is serialized to Redis by the caller", but no serializer exists — `Session` holds an `InterviewerState` enum and a list of `Turn` dataclasses, and `RedisSessionStore.save` calls `json.dumps` on it, which raises `TypeError`.
**Disposition: Extend.** Add `Session.to_dict()` / `from_dict()`. This is the reason `DG-02` notes that wiring the store alone is insufficient: the intent already exists in the docstring, only the method is missing.

### REC-05 — Refactor: the always-accept load threshold

`worker.py:62` sets `load_threshold=float("inf")` with a comment explaining it as a workaround: the default 0.7 CPU gate marked the worker unavailable during TTS/STT bursts, so the server refused to dispatch at all.
**Disposition: Refactor to finite (~0.95).** The workaround existed *because* the engines ran in-process. Once `BRD-08` moves them out, the worker is I/O-bound and the gate becomes accurate — so this refactor is **dependent on** the engine extraction, and the comment must be rewritten to say so rather than left implying the old reasoning.

### REC-06 — Harden: a monkey-patch on a private API

`worker.py:65-78` overrides `server._is_available` to log refusal reasons. It is a private attribute of a third-party class.
**Disposition: Harden.** Wrap the assignment in `try/except AttributeError` with a warning, and pin `livekit-agents` to a minor version in the image. A floating `>=1` constraint that renames this attribute turns a diagnostic into an import-time crash — a fragile diagnostic is worse than none.

**Verified against the container image (livekit-agents 1.8.1 on Linux, while the Windows venv runs 1.7.1).** Every attribute the diagnostic touches still exists — `_is_available` is a method, `draining` is a property, and `_devmode`, `_load_threshold` and `_worker_load` are all set as **instance** attributes in `__init__`. **So there is no defect today, and the precaution is a precaution, not a repair.** Worth recording explicitly, because a class-level `hasattr` check reports these three as missing and would produce a false bug report; they are only visible on an instance. The minor-version drift (1.7.1 → 1.8.1) is real, which is exactly what the pin exists to control.

### REC-07 — Refactor: route ordering by convention

`server.py:317-319` mounts `StaticFiles` at `/` as a catch-all, and FastAPI matches in declaration order, so every API route must be declared above it. This is enforced only by a comment, and the codebase has already suffered a comparable shadowing bug (a filter that silently swallowed every message).
**Disposition: Refactor.** Extract routes into an `APIRouter` and include it before the mount, then add a regression test asserting the probes are not shadowed. Convention becomes structure.

### REC-08 — Preserve: `brain.py` structure

`brain.py` is ~500 lines owning the FSM, prompting, RAG calls, scoring, TTS orchestration, budget tracking, and event emission.
**Disposition: Preserve.** Add an `on_checkpoint` callback invoked from the existing `_hop()` choke point and nothing else. Refactoring this file for Kubernetes is the single most likely way to derail the schedule, and it buys the migration nothing.

### REC-09 — Refactor: a Windows-only pin with global effect

`pyproject.toml` pins `av==12.3.0` because PyAV ≥13's unsigned DLLs are blocked by Windows Smart App Control — a Windows machine-policy issue that does not exist on Linux.
**Disposition: Refactor to platform markers** (`av==12.3.0; sys_platform == 'win32'`, `av>=13,<16; sys_platform != 'win32'`). **Verify first**: `import av` inside the built image is the earliest gate in Phase 0, because it is the one genuine unknown in containerization.

### REC-10 — Reuse: the prepopulate script is already portable

`scripts/prepopulate_banks.sh` globs the banks, validates slugs, and calls the RAG CLI per bank. It is idempotent and already runs on Linux unmodified.
**Disposition: Reuse verbatim** as the payload of a Kubernetes Job, with an initContainer syncing banks from the object store. Rewriting it would be gratuitous.

### REC-11 — Reuse: the e2e client is a ready-made smoke test

`scripts/e2e_voice_client.py` posts to `/voice/token`, joins a room, drives the manual answer protocol, and asserts the interview reaches `state=wrap` — with no microphone and no human.
**Disposition: Reuse** as the post-deploy smoke test and the Phase 2 acceptance gate. It is the only artifact that exercises the real multi-process audio pipeline, so it is the highest-value test in the plan.

### REC-12 — Extend: instrumentation that exists but is not exported

`voice/budget.py` implements the latency gate with per-hop records; `llm.py` records first-token and total milliseconds; every summary carries a per-hop ledger and a `voice_budget_bar`. `brain.py:188`'s `_hop()` runs at every stage boundary and already accepts `**extra`.
**Disposition: Extend.** `BRD-13` is wiring, not measurement — a single `observe` call at the existing choke point produces most of the metric series. This is the cheapest high-value change in the plan and it needs no GPU.

### REC-13 — Preserve: the Windows launcher

`start_services.ps1` is a 966-line, heavily Windows-coupled orchestrator using `taskkill`, `Win32_Process`, `.venv\Scripts\`, and `$env:TEMP`.
**Disposition: Preserve; supersede without deleting.** It stays the supported Windows dev path (`AS-05`), while Compose becomes the recommended cross-platform loop. Deleting it would break a working contributor workflow for no migration benefit.

### REC-14 — Reuse: `livekit-agents` already ships four mechanisms this plan was designing by hand

Verified by reading `livekit-agents` 1.8.1 source inside the built image, not assumed from documentation. `WorkerOptions` exposes supported hooks that replace or strengthen four custom designs in Stages 8–9. **Reuse over invention applies to upstream too** — building these ourselves would be duplicated, less-tested effort.

| Finding | Supplied by | Effect on the plan |
|---|---|---|
| `drain_timeout` | `WorkerOptions` | A **first-class drain**. US-014 currently proposes a custom `control.py` plus `preStop` polling; the supported path should be the primary mechanism, with the custom control server kept only for `/readyz`, `/metrics` and the scaler snapshot it already owns. |
| `load_fnc` | `WorkerOptions` | A supported way for the worker to report its own load for dispatch decisions. Complements US-018's external `/internal/scaler/voice-queue` — the endpoint still drives replica count, but `load_fnc` can drive per-replica *dispatch* accuracy without a private-attribute monkey-patch (REC-06). |
| `prometheus_port`, `prometheus_multiproc_dir` | `WorkerOptions` | Directly resolves the gotcha recorded in US-015 — that per-room forked job processes do not share a Prometheus registry. This is the supported mechanism, so the design need not invent one or restrict the executor. |
| `_default_load_threshold = ServerEnvOption(dev_default=math.inf, prod_default=0.7)` | `AgentServer` | **Explains the original workaround exactly.** In dev mode the default threshold is already infinite; the 0.7 gate the author disabled is the *production* default. This confirms the diagnosis in REC-05 and US-011 — in-process TTS/STT made the production CPU gate refuse dispatch, and moving the engines off-process is what makes it accurate again. |

**One dependency surprise, in our favour:** `prometheus-client` (0.26.0) is already a **transitive dependency of `livekit-agents`**. US-015 therefore adds no new package to the dependency graph — it uses one already present.

**Disposition: Reuse.** Where this plan proposed building a mechanism, prefer the upstream hook and keep the custom work for what upstream does not cover. Recorded here rather than silently folded in, because it changes the LLD of two stories and the rationale for a third.

## Reconciliation Summary

| Disposition | Count | Notes |
|---|---|---|
| Reuse | 5 | REC-01, REC-02, REC-10, REC-11, REC-14 |
| Extend | 2 | REC-04, REC-12 |
| Refactor | 3 | REC-05, REC-07, REC-09 |
| Harden | 1 | REC-06 |
| Delete | 1 | REC-03 |
| Preserve | 2 | REC-08, REC-13 |

**Ten of fourteen dispositions are Reuse, Extend, or Preserve.** That ratio is the honest headline of this plan: the existing architecture already has the right seams, and the migration is mostly a matter of wiring, hardening, and operationalizing what is there. The three refactors are each tied to a concrete, cited defect — none is motivated by a preference for something more modern. REC-14 extends the same principle upstream: where `livekit-agents` already ships a supported mechanism, the plan uses it rather than building its own.

**No existing pattern is replaced without a cited defect.** Where the plan changes behaviour (REC-05's threshold, REC-07's routing, REC-09's pin), the artifact names the specific failure the change prevents.
