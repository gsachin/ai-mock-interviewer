> **Lens:** BA + TPO + PO · **Engagement:** Brownfield/Partial · **Defines:** CV-01…CV-05

# Coverage & Gap Analysis — ai-mock-interviewer Autoscaling

**This is the gate.** Stages 6–11 do not begin until this stage either closes every gap or records an explicit accepted decision with an owner.

## Requirement → Use Case Coverage Matrix

| Requirement | Use cases | Workflows | Stories | Tests |
|---|---|---|---|---|
| BRD-01 — Stateless management API tier | UC-01, UC-07 | WF-01 | US-004, US-005 | Session round-trip across two replicas |
| BRD-02 — Real liveness and readiness probes | UC-05, UC-10 | WF-01 | US-006 | Probe tests incl. route-shadowing regression |
| BRD-03 — Configuration-driven CORS | UC-01, UC-05 | WF-01 | US-007 | Config-default tripwire + origin matrix |
| BRD-04 — Pluggable bank storage | UC-03, UC-04 | WF-02 | US-009, US-010 | Store contract tests per backend |
| BRD-05 — Durable voice sessions | UC-01, UC-07 | WF-01 | US-012, US-013 | e2e then `GET /sessions/{id}` returns `wrap` |
| BRD-06 — Scale-down drains, never kills | UC-06 | WF-01 | US-014 | Delete pod mid-interview; drain completes |
| BRD-07 — Worker replicas follow queue depth | UC-06, UC-11 | WF-01 | US-018 | KEDA target increase produces registered pods |
| BRD-08 — AI engines as independent GPU services | UC-01 | WF-01 | US-011 | Adapter contract tests + all-remote e2e |
| BRD-09 — Measurable voice latency | UC-01, UC-09 | WF-03 | US-015 | Budget histogram present and populated |
| BRD-10 — Horizontally scalable RAG | UC-01, UC-11 | WF-01 | US-017 | Two RAG replicas both serve a new bank |
| BRD-11 — Production LiveKit | UC-01, UC-08, UC-11 | WF-01 | US-016 | Browser interview over `wss://` |
| BRD-12 — No secrets in images or repo | UC-05, UC-08 | WF-01 | US-016 | Secret scan + startup assertion |
| BRD-13 — Observability and the latency SLO | UC-09, UC-10 | WF-03 | US-015 | Scrape targets up; SLO recording rule evaluates |
| BRD-14 — Cloud-agnostic and on-prem deployable | UC-05 | WF-01 | US-001, US-019 | `kustomize build` for base + two overlays |
| BRD-15 — Continuous integration | UC-05 | WF-01 | US-020 | Pipeline green on a clean checkout |
| BRD-16 — Interrupted interviews reported | UC-06, UC-07 | WF-01 | US-014 | Killed pod yields `interrupted`, not 404 |
| BRD-17 — Multi-tenancy/auth decided | UC-01, UC-07 | WF-01 | — | **Deferred — accepted gap (see Gap List)** |
| BRD-18 — Retention and privacy decided | UC-01, UC-07 | WF-01 | — | **Deferred — accepted gap (see Gap List)** |

No cell is empty. `BRD-17` and `BRD-18` carry no stories **by decision**, recorded below as accepted gaps with a PO owner — not by omission.

## Actor Coverage

| Actor | Use cases | Covered |
|---|---|---|
| Candidate | UC-01, UC-02, UC-07 | Yes |
| Skill author / Admin | UC-03, UC-04 | Yes |
| Platform operator | UC-05, UC-06, UC-08 | Yes |
| On-call engineer | UC-09, UC-10 | Yes |
| System / Agent | UC-11 | Yes |

Every actor has at least one use case, and every use case carries a full 14-row scenario table. No orphan actors.

## MVP Scope Coverage

| MVP item | Covers | Delivered by |
|---|---|---|
| Stateless management API tier | BRD-01, UC-01, UC-07 | US-004, US-005 |
| Real liveness and readiness probes | BRD-02, UC-05, UC-10 | US-006 |
| Pluggable bank storage (local / S3 / cloud) | BRD-04, UC-03, UC-04 | US-009, US-010 |
| GPU engine services for STT, TTS, LLM | BRD-08, UC-01 | US-011 |
| Voice worker autoscaling on interview queue depth | BRD-07, UC-06, UC-11 | US-018 |
| Graceful drain — scale-down never kills an interview | BRD-06, UC-06 | US-014 |
| Durable voice sessions and `GET /sessions/{id}` | BRD-05, UC-01, UC-07 | US-012, US-013 |
| Multi-replica RAG service | BRD-10, UC-11 | US-017 |
| Cloud-agnostic base plus thin per-cloud overlays | BRD-14, UC-05 | US-001, US-019 |
| Observability with the latency budget as an SLO | BRD-13, BRD-09, UC-09 | US-015 |
| CI: build, test, validate manifests | BRD-15, UC-05 | US-020 |

Every MVP In item from `00-product-intent.md` §5 appears here with the requirements and use cases that deliver it, and every row cites IDs. The two MVP-Out items that are deferred (`BRD-17`, `BRD-18`) are recorded as accepted gaps rather than silently dropped.

## Contradiction Detection

| ID | Contradiction | Resolution |
|---|---|---|
| CV-01 | Assumption AS-02 treats KEDA as acceptable, but it may be refused as a cluster dependency | **Escalated, unresolved by design.** If refused, the worker scaler degrades to HPA-on-CPU, which post-extraction is the *wrong* metric — the worker becomes I/O-bound, so CPU would scale on VAD bursts rather than interviews. Recorded as an open dependency for the platform owner, not papered over. |
| CV-02 | BRD-08 (extract engines) requires per-process engine construction to be worthwhile, which requires the `LLMMetrics` concurrency fix first — yet the fix appears in no requirement | **Ordering constraint recorded as a requirement-level dependency.** The per-call metrics parameter ships in the same wave *before* per-process engines. Getting this backwards silently misattributes `llm_first_token_ms` to the wrong hop, corrupting the exact data the SLO is built on. |
| CV-03 | BRD-09 asserts a 1500 ms budget while the measured value is 6.5–8.4 s — an SLO violated 4–6× | **Escalated to the PO as an open product decision.** Engineering can measure and improve; it cannot decide whether 1500 ms is a hard requirement or an aspiration. Until decided, the SLO ships as ticket-severity. |
| CV-04 | BRD-01 (statelessness) conflicts with DAT-07 (audio buffer and suspended coroutines living only in the worker process) | **Accepted gap.** Not solvable by externalizing state. Mitigated by drain + long grace + `maxUnavailable: 0`, and made honest by BRD-16. |
| CV-05 | UC-06 (drain) conflicts with `load_threshold=float("inf")`, which makes every worker accept every job | **Resolved by coupling.** Engine extraction makes the worker I/O-bound, so a finite load threshold becomes accurate — the two requirements are dependent, and BRD-08 is what unblocks BRD-07 and BRD-06. |

## Gap List

| # | Gap | Type | Owner | Decision |
|---|---|---|---|---|
| 1 | Multi-tenancy and authentication model (BRD-17) | Scope gap | PO | **Accepted, deferred.** No user model exists; `tenant_id` is hardcoded. Explicitly out of MVP scope, carried as an open decision rather than inherited silently from item P5. |
| 2 | Interview data retention and privacy (BRD-18) | Scope gap | PO | **Accepted, deferred.** Interviews are personal data by nature and no policy, deletion path, or consent capture exists. Flagged as the highest-risk deferred item because it is a legal exposure, not only a design one. |
| 3 | Peak concurrency and sessions/day (AS-03) | Data gap | PO | **Accepted with a validation plan:** instrument arrivals, re-size after one week of real traffic. Until then the GPU floor is parametrized. |
| 4 | KEDA acceptance (AS-02) | Feasibility gap | TPO | **Open.** Requires platform-owner confirmation. Fallback documented but materially worse. |
| 5 | Whether 1500 ms is achievable on self-hosted GPU (CV-03) | Feasibility gap | Architect | **Open, with a measurement plan:** benchmark first-token latency for the actual voice prompt shape before committing GPU spend. The `llm-bench` skill exists for this. |
| 6 | LiveKit's pending-agent-dispatch metric availability | Feasibility gap | Architect | **Mitigated by design.** The scaler endpoint computes pending interviews itself, so it works whether or not the SFU exposes the metric. |
| 7 | LiveKit worker re-registration reliability (P2) | Feasibility gap | TPO | **Open.** Dev-mode drops an idle worker and it does not reliably re-register. Made observable via `SM-02` readiness and a `livekit_worker_registered` SLI; the supervisor is treated as required, not optional. |

Gaps 1–5 require a human decision or measurement. Gaps 6–7 have engineering mitigations recorded. **None is silently closed.**

## Iteration Log

| Iteration | Triggered by | Changes made | Result |
|---|---|---|---|
| 1 | Initial Stage 5 pass — 18 Discovery Challenge questions run against Stages 1–4 | Reshaped DG-01 from an RWX PVC to a pluggable object-store design (BRD-14 would have failed); added CV-02 and promoted the `LLMMetrics` fix into Phase 1 as a hard ordering constraint; added CV-05 coupling engine extraction to the drain and scaler requirements; added BRD-16 after accepting CV-04; added the readiness-reflects-registration contract to SM-02 in response to gap 7; added `BRD-17`/`BRD-18` after the Discovery Matrix exposed the compliance dimension as genuinely empty. | 7 gaps found: 3 accepted with owners, 2 open pending decision/measurement, 2 mitigated by design. No uncovered requirement; no empty use-case cell. |
| 2 | Verification pass — re-ran the coverage matrix against the final requirement set | No new gaps. Confirmed every BRD row resolves to at least one UC and one workflow, and that the two deferred requirements are recorded rather than orphaned. | Closed. Gate satisfied. |

Two iterations were needed, not zero: iteration 1 found and fixed a data-store decision that would have violated the cloud-agnostic constraint, and surfaced two product-level gaps (auth, privacy) that the original scope had inherited unexamined.

## Discovery Challenge

| # | Question | Answer |
|---|---|---|
| 1 | What if peak concurrency is 2? | Then the GPU floor *is* the cost, and scale-to-zero on the judge is the only real lever. AS-03 must be answered before the floor is treated as settled. |
| 2 | What if a worker dies mid-interview? | The interview is lost. DAT-07 cannot be externalized. Reported honestly via BRD-16; resumption is an explicit non-goal. |
| 3 | What if KEDA is refused? | Worker scaling degrades to HPA-on-CPU — the wrong metric. Recorded as CV-01, unresolved. |
| 4 | What if a GPU node takes 10 minutes to appear? | Node provisioning plus weight load exceeds the product's entire latency claim. `minReplicas ≥ 1` on GPU is therefore a requirement, not a tuning preference. |
| 5 | What if RAG is down at interview start? | The interview degrades rather than fails, so readiness must not require RAG (BRD-02 defaults the flag off). |
| 6 | What if two admins upload the same bank concurrently? | Object-store last-write-wins, with registration made idempotent by the existing reconcile endpoint. Documented, not accidental. |
| 7 | What if resumption is really needed? | It is not offered. Rewinding a candidate into a half-spoken question is worse than an honest failure. |
| 8 | What if the ingress buffers WebSocket frames? | The SFU session dies. `proxy-buffering: off` and long timeouts are mandatory and are the most commonly missed LiveKit ingress settings. |
| 9 | What if `livekit-agents` renames a private attribute? | The worker's diagnostic monkey-patch becomes an import-time crash. Pin to a minor and guard with `try/except`. |
| 10 | What if the bank cache is stale on replica B? | The bounded TTL is what keeps the documented no-restart promise true; all three glob consumers must read through the same cache. |
| 11 | What if 1500 ms is unreachable on GPU? | A PO decision, not an architect edit. The stage budgets only sum to 1250 ms assuming perfect overlap, and RAG context prefill dominates first-token latency more than model size — so prefix caching and a short rubric matter more than a smaller model. |
| 12 | What if we page on an SLO we are 5× away from? | It gets muted and the signal is lost. Ship ticket-severity, promote once GPU lands. |
| 13 | Does a PDB protect drained interviews? | **No.** KEDA scales via the scale subresource, bypassing the eviction API. PDBs cover node drains and upgrades only; `preStop` plus a long grace period is the actual protection. |
| 14 | What if the worker image still carries models? | Cold start balloons and scale-up is slow. Post-extraction the worker ships no models — no `ctranslate2`, no `kokoro_onnx`. |
| 15 | What if engines are shared per process but metrics still mutate instance state? | First-token latency silently attributes to the wrong hop, corrupting the SLO's own data. CV-02 exists for this. |
| 16 | What if `brain.py` is refactored for Kubernetes? | ~500 lines owning FSM, prompting, RAG, scoring, TTS orchestration, budget and events. Add the checkpoint callback; leave the structure alone. |
| 17 | What if the `av` pin is load-bearing on Linux? | It exists solely for Windows Smart App Control. Make it platform-conditional and verify `import av` in the image as the first Phase 0 gate. |
| 18 | What about the static UI's relative path? | It resolves two levels up from `server.py` and silently vanishes under a package-only install. Reproduce the dev layout at `/app` with `PYTHONPATH`. |

## Freeze Decision

**STATUS: COMPLETE**

The gate is satisfied for Stages 6–11: every requirement maps to at least one use case and workflow; every actor has use cases; every use case carries a full 14-row scenario table; every workflow has a Mermaid diagram, a failure drill, and a partial-completion matrix; every data asset has a decision or an explicit accepted gap; and the architecture stage is unblocked.

**Seven gaps were found and none was silently closed** — three accepted with a named owner (auth model, retention/privacy, peak concurrency), two open pending an external decision or measurement (KEDA acceptance, GPU latency feasibility), and two mitigated by design (scaler metric availability, worker re-registration). The two deferred requirements are recorded as **accepted scope gaps with a PO owner**, which is why they carry no stories without that being an omission.

**Carried forward as blocking for the PO, not for engineering:** `BRD-17` and `BRD-18` are legal and product exposure, not technical debt, and `CV-03` (whether 1500 ms is firm) determines whether the GPU spend is justified by the product claim it is meant to satisfy.
