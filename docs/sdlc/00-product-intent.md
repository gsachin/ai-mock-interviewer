> **Stage 0 — Engagement [Lens: all]** · **Decided by:** PO · **Engagement:** Brownfield/Partial — existing 5-phase codebase (~7,560 LOC Python, 121 passing unit tests, 4 phase TRDs, 1 RCA, 1 HLD) · **Scope:** full delivery plan

# Stage 0 — Engagement

**Manifest check:** `docs/sdlc/plan-state.md` absent on entry → **fresh run**, no resume.
**Classification: Brownfield / Partial.** A working, tested system exists. Per the brownfield rule, **the codebase wins over the blueprint**: every conflict is raised as a `REC-xx` reconciliation note, never silently designed around.

**Evidence inspected:**
| Source | Class | What it gave |
|---|---|---|
| `docs/architectexture/HLD_ARCHITECTURE.md` | Document-observed | Canonical HLD; §8 is a ready-made container inventory |
| `docs/CLAUDE.md` | Document-observed | Commands, architecture contract, conventions |
| `README.md`, `pyproject.toml` | Document-observed | Stack, optional extras, the `av==12.3.0` Windows pin |
| `docs/STATUS.md` | Document-observed | Measured latency: 6.5–8.4 s vs a 1500 ms budget |
| `docs/DONE_AND_PENDING.md` | Document-observed | Open items P1 (latency), P2 (worker drop), P4 (voice sessions not persisted), P5 (production hardening) |
| `docs/RCA_VOICE_BROWSER_INTERVIEW.md` | Document-observed | Why manual capture, no-hang, and echo-gate exist |
| `start_services.ps1` (966 lines) | Code-observed | The de-facto deployment mechanism being replaced |
| `interviewer/{server,state_machine,session_store,config,brain,llm,skills}.py`, `interviewer/voice/{worker,agent,protocols,interviewer,budget}.py` | Code-observed | Read directly — defects and seams enumerated below |

**Scope decision:** full delivery plan (all stages). You named plan artifacts — modularization, architecture, scale — which per the skill defaults to the full pipeline.

**Constraints fixed by you (Stage 1 inputs, `User-provided`):** cloud-agnostic Kubernetes with cloud specifics isolated in thin overlays · multi-replica RAG in scope · Streamlit dropped from the cluster · GPU nodes available (self-host vLLM + Whisper + Kokoro) · bank storage configurable for cloud **and** on-prem · deliverable = plan + working scaffold.

---

# 1. Problem

The interviewer works, and cannot leave one Windows machine. A real-time AI mock interviewer — the product works end to end, four phases of it, verified by 121 passing unit tests and a mic-free e2e client that drives a real interview to completion.

**What does not work is everything around it.** Six bare processes are launched by a 966-line PowerShell script, shared publicly through ephemeral Cloudflare quick tunnels whose hostnames re-randomize on every start. Session state lives in a process-local Python dict. There is no container, no orchestration, no CI, no secret management, and no real health check — `/health` returns `{"status":"ok"}` while hardcoding the string, checking nothing.

The consequences are concrete:
- **It cannot scale.** A second candidate on a second replica cannot see the first candidate's session. Two replicas is not a tuning exercise; it is a correctness failure.
- **It cannot be deployed.** Every release is a human running a script on a specific laptop.
- **It cannot hold its own quality bar.** The voice round-trip is measured at 6.5–8.4 s against a hard 1500 ms budget — violated 4–6×, because TTS (~3.6 s) and STT (~1.0 s) run on CPU.
- **It cannot survive a restart.** Scale-down, node drain, or an OOM kills a live interview silently: the candidate sits in a LiveKit room showing "Connected" over dead air.

A mock interviewer is only useful if a candidate can reach it. Today it serves one candidate at a time, on one machine, owned by one person.

# 2. Product Intent

Turn a working single-machine AI mock interviewer into an elastic, cloud-agnostic Kubernetes service that any candidate can reach concurrently — by making every tier stateless or explicitly stateful, moving the CPU-bound AI engines (STT, TTS, LLM) behind the engine protocols that already exist onto an independently scaled GPU node pool, and giving the voice worker a scaling model built on interview queue depth with a graceful drain, so that capacity follows demand and scale-down never cuts a live interview. The dialogue brain stays in-process, because the interview turn is the one path where a network hop is not affordable.

# 3. Target Users & Personas

| Persona | Jobs to be done | Success looks like |
|---|---|---|
| **Candidate** | "When I practise, I want a realistic spoken interview that responds to my actual answers, so I can find my gaps before the real thing." | Joins from a link, speaks, gets scored feedback. Never sees infrastructure. |
| **Skill author / Admin** | "When I add a domain, I want to drop a bank and have it live, without a restart." | Uploads a `.md` on the Skill Update page; it registers and is immediately usable. |
| **Platform operator** | "When traffic changes, I want capacity to follow it without me waking up." | Capacity tracks interviews; a deploy or a node drain never kills an interview. |
| **On-call engineer** | "When a turn is slow, I want to know which hop broke the budget." | Traces a breach to STT, TTS, LLM, or RAG from a dashboard, without reading logs. |

# 4. Success Metrics

| Metric | Class | Target | Today |
|---|---|---|---|
| Interviews completed per day within the latency budget, per GPU-hour | **North Star** | Maximise at a bounded GPU cost | Not measurable — no metrics exist |
| Voice round-trip, utterance-end → first audio | Guardrail | ≤ 1500 ms p95 (`budget.py`) | **6.5–8.4 s** — violated 4–6× |
| Interview completion rate reaching `wrap` | Guardrail | ≥ 95% | Unmeasured |
| Cost per completed interview | Guardrail | TBD — blocked on peak concurrency | Unknown |
| Worker cold-start → dispatchable by the SFU | Guardrail | ≤ 30 s | N/A — no autoscaling today |
| Sessions served concurrently | Guardrail | Elastic to a configured ceiling | 1 (single process) |

The North Star deliberately couples throughput and cost in one number: it cannot be gamed by trading one for the other, which is the actual decision this project faces (see §6 and the accepted gap on GPU floor cost).

# 5. MVP Scope

| In scope | Out of scope |
|---|---|
| Stateless management API tier | Mid-interview resumption |
| Real liveness and readiness probes | End-to-end OIDC / user auth |
| Pluggable bank storage (local / S3 / cloud) | Multi-region deployment |
| GPU engine services for STT, TTS, LLM | Interview-quality evaluation harness |
| Voice worker autoscaling on interview queue depth | MIG / GPU time-slicing |
| Graceful drain — scale-down never kills an interview | Streamlit UI in the cluster |
| Durable voice sessions and `GET /sessions/{id}` | Migrating users or existing session data |
| Multi-replica RAG service | Reworking the LiveKit dev-mode dev flow |
| Cloud-agnostic base plus thin per-cloud overlays | |
| Observability with the latency budget as an SLO | |
| CI: build, test, validate manifests | |

# 6. Non-goals

- **Mid-interview resumption.** A killed worker loses the interview; the ~3.8 MB audio buffer and suspended coroutines are not recoverable. Reported honestly, not resumed (see `CV-04`).
- **End-to-end auth.** No user model exists today (`tenant_id` is hardcoded `"default"`). Deferred with an owner, not silently inherited (`BRD-17`).
- **Interview data retention and privacy posture.** Interviews are personal data by nature and no policy exists. Deferred with an owner (`BRD-18`).
- **Replacing the Windows dev flow.** `start_services.ps1` keeps working; Compose supersedes it without deleting it.
- **Refactoring `brain.py`.** ~500 lines owning FSM, prompting, RAG, scoring, TTS orchestration, budget and events. Add a checkpoint callback; leave the structure alone.

# 7. Assumptions

| ID | Assumption | Class | Impact if wrong |
|---|---|---|---|
| AS-01 | GPU nodes will be available in the target cluster | User-provided | Whole GPU tier redesign; fall back to cloud STT/TTS behind the same protocol |
| AS-02 | KEDA is an acceptable cluster dependency | Assumed | Worker scaling degrades to HPA-on-CPU — the *wrong* metric post-extraction |
| AS-03 | Peak concurrency and sessions/day are unknown; design parametrically | User-provided | GPU floor is sized by guess; re-size after one week of real traffic |
| AS-04 | The existing 121-test suite is the regression floor | Code-observed | Refactors proceed without a safety net |
| AS-05 | The Windows dev flow keeps working throughout | User-provided | Contributor workflow breaks; mitigated by a config-defaults tripwire test |
