> **Last updated:** 2026-09-12 · **Engagement:** Brownfield/Partial — ai-mock-interviewer (real-time AI voice mock interviewer) → cloud-agnostic Kubernetes with GPU-backed AI engines

# Plan State — ai-mock-interviewer Autoscaling

> **Checkpoint manifest.** A fresh session reads this file first and continues from **Next action**. Stages marked `done` are never regenerated. If this file ever contradicts the tree, the tree wins — regenerate this file, never edit the tree to match it.

## Engagement summary

| Field | Value |
|---|---|
| Product | ai-mock-interviewer — real-time, web-based AI technical mock interviewer (voice + text) |
| Classification | **Brownfield / Partial** — working 5-phase system, ~7,560 LOC Python, 121 passing unit tests |
| Scope | Full delivery plan (all stages) |
| Constraints | Cloud-agnostic K8s (portable primitives, cloud specifics in thin overlays) · on-prem capable from the same base · multi-replica RAG in scope · Streamlit dropped from the cluster · GPU nodes available · bank storage configurable |
| Deliverable | Plan artifacts + working scaffold (Dockerfiles, Kustomize manifests, KEDA/HPA, Compose, CI) |

## Stage status

| Stage | Status | Evidence file | Notes |
|---|---|---|---|
| 0 | done | `00-product-intent.md` | Fresh run; no prior manifest. Classification + evidence inventory recorded |
| 1 | done | `00-product-intent.md` | Intent, 4 personas, North Star + 5 guardrails, MVP in/out, AS-01…AS-05 |
| 2 | done | `01-brd.md` | Discovery Matrix 13/13, BRD-01…BRD-18, Evidence Register |
| 3 | done | `02-use-cases-workflows.md` | 11 UCs × 14 scenario rows, 3 WFs with failure drills + partial-completion matrices |
| 4 | done | `03-data-state-analysis.md` | DAT-01…DAT-07, DG-01…DG-04, SM-01…SM-03, Load & Capacity Model (parametrized) |
| 5 | done | `04-coverage-gap-analysis.md` | GATE. 2 iterations. 7 gaps: 3 accepted, 2 open, 2 mitigated. STATUS: COMPLETE |
| 6 | done | `05-modularization.md` | MOD-01…MOD-13 with form justification; brain kept as a library |
| 7 | done | `06-architecture.md` | Mermaid system + per-module flows, orchestration pattern chosen, 13-row Scale & Bottleneck |
| 7b | done | `07-brownfield-reconciliation.md` | REC-01…REC-13; 9 of 13 are reuse/extend/preserve |
| 8 | done | `modules/` | 8 module BRD+TRD pairs with TRD-01…TRD-08 |
| 9 | done | `stories/` | 20 self-contained stories US-001…US-020 |
| 10 | done | `08-coverage-verification.md` | 19 capability rows, story→test mapping |
| 11 | done | `08-coverage-verification.md` | Scorecard PASS; 7 blocking gaps carried to the PO |

## ID registry

AS-01…05 · BRD-01…18 · UC-01…11 · WF-01…03 · DAT-01…07 · DG-01…04 · SM-01…03 · CV-01…05 · MOD-01…13 · REC-01…14 · TRD-01…08 · US-001…020

Defined-once rule is enforced by the validator in both directions: every token above must resolve to exactly one definition in the tree, and every defined ID must appear above.

## Freeze decision

**STATUS: COMPLETE.** Stage 5 froze after 2 iterations. Seven gaps were found and none was silently closed — three accepted with a named owner (auth model, retention/privacy, peak concurrency), two open pending an external decision or measurement (KEDA acceptance, GPU latency feasibility), and two mitigated by design (scaler metric availability, worker re-registration).

## Blocking gaps carried forward (owner: PO)

| # | Gap | Why it is not an engineering fix |
|---|---|---|
| 1 | Multi-tenancy and authentication model (`BRD-17`) | No user model exists; `tenant_id` is hardcoded `"default"` |
| 2 | Interview retention and privacy (`BRD-18`) | Legal exposure — interviews are personal data; no policy, deletion path, or consent capture exists |
| 3 | Peak concurrency `C` (`AS-03`) | Sizes the GPU floor, the dominant cost line |
| 4 | Whether 1500 ms is a hard requirement (`CV-03`) | Determines whether the GPU spend is justified by the product claim it serves |
| 5 | KEDA acceptance (`AS-02`) | Fallback (HPA-on-CPU) is the wrong metric for an I/O-bound worker |
| 6 | GPU latency feasibility | Measure with `llm-bench` before committing spend |
| 7 | LiveKit worker re-registration (open item P2) | Resurfaces in-cluster as "scaled a pod the SFU never saw" |

## Next action

**Build Wave 0 — containerize without behavior change.** Start with `US-001` (multi-target `Dockerfile`, `base → api`, `base → worker`), whose first gate is verifying `import av, livekit.agents` **inside the built image** — the one genuine unknown in containerization, because the `av==12.3.0` pin exists only for Windows Smart App Control.

Then, in order: `US-002` (Compose as the daily driver, CPU engine images so it runs without a GPU), `US-003` (config-defaults tripwire — the mechanism that keeps `start_services.ps1` working).

**Standing regression gate at every step:** `pytest tests/ -m "not live"` must stay at **121 passed**, and `scripts/e2e_voice_client.py` must still reach `state=wrap`.
