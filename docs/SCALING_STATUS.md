# Scaling Migration — Resumable Status

> **Session ID:** `f88c9825-bc07-4eb2-a5e9-4d63c707c137`
> **Last updated:** 2026-09-13
> **Branch:** `auto-scalling-arch` (pushed to origin)
> **Repo:** `D:\project\ai-mock-interviewer`
> **Plan:** `docs/sdlc/plan-state.md` · **Per-story detail:** `docs/sdlc/09-implementation-status.md`

**Purpose of this file:** everything needed to resume this work in a fresh session without re-deriving it. Read this first. It is the single source of truth for where the migration stands; the `docs/sdlc/` tree is the plan, this is the state.

---

## 1. Resume here — exact next action

**The blocker: the end-to-end voice interview does not reach `state=wrap`.**

Current behaviour: the client joins the room, subscribes to the interviewer's audio, and **receives the spoken greeting** — then the worker raises a generic exception and the interview ends with `interviewer error — please end the call and try again`.

**Run this experiment first (≈10 minutes).** It tests the leading hypothesis:

```bash
# 1. Temporarily revert the persistent MCP session in rag_client.py:
#    make _call() use a fresh session per call (as it did before US-008)
#    i.e. replace `session = await self._ensure_session(timeout_s)` with the
#    original `async with self.session(timeout_s=timeout_s) as session:`
# 2. Rebuild + load + restart the worker (commands in §6)
# 3. Re-run the e2e (§7)
```

**Hypothesis:** `AsyncExitStack` in `RagClient._ensure_session` (added in US-008 to reuse one MCP session across calls) is incompatible with MCP's anyio task groups, which are **strictly task-bound**. Holding one open across calls from different tasks would explain both the earlier `Cancelled via cancel scope` error and an exception that never surfaces a traceback.

- If the interview advances past the greeting → the hypothesis is right. The correct fix is a **dedicated owner task** for the MCP session (calls marshalled through a queue), not permanently reverting the optimization.
- If it still fails → the session is exonerated; look next at the greeting→question transition and the rubric fetch.

**Known oddity worth chasing alongside:** `agent.py:319-321` catches the exception and calls `log.exception(...)`, **yet the worker log contains no traceback at all** (searched every JSON record for `exc`/`exc_info`). `log.exception` should always emit one. Either the formatter is dropping it or the exception is being swallowed in a task before reaching that handler.

---

## 2. The machine (verified this session — do not re-derive)

| | |
|---|---|
| OS | Windows 11 (10.0.26200), Git Bash / MINGW64 |
| CPU / RAM | 6 cores / ~16 GB total, all shared with the kind cluster |
| **GPU** | **NVIDIA RTX 5060 Ti, 16 GB** — driver 610.88 |
| Container GPU | **Works** (`docker run --gpus all` sees the card) |
| **kind GPU** | **Impossible** — kind nodes are containers created without `--gpus` |
| Docker | 29.7.2, Docker Desktop, overlayfs |
| kubectl | v1.36.1, Kustomize v5.8.1 |
| kind | v0.33.0 at `.tools/kind.exe` (git-ignored) |
| Cluster | `mock-interviewer`, k8s **v1.37.0**, 3 nodes (1 CP + 2 workers), all Ready |

### Measured performance — the single most important number

| Where the LLM runs | per voice turn |
|---|---|
| CPU (ollama in-cluster, 6 shared cores) | **21.8 s** |
| **GPU (host ollama, RTX 5060 Ti)** | **0.14 s** after a 17.9 s one-off VRAM load |

**~150×.** This independently validates the plan's central claim: the 1500 ms voice budget is unreachable on CPU and comfortable on GPU.

**If an interview is mysteriously slow, check WHICH ollama it is talking to before anything else.**

---

## 3. The GPU tier design (decided and implemented)

The GPU tier sits **beside** the cluster, not inside it — in every environment. That is the same shape as production, where the GPU node pool is a separate tier; only the side of the boundary the hardware sits on differs.

| Environment | Where inference runs | How the app reaches it |
|---|---|---|
| Windows dev (`start_services.ps1`) | host ollama | `http://127.0.0.1:11434/v1` (CUDA auto-detected) |
| Local kind cluster | host ollama (GPU) | `http://host.docker.internal:11434/v1` → `192.168.65.254` |
| Production | GPU node pool | `components/gpu-engines/` (vLLM / Whisper / Kokoro) |

**`192.168.65.254` is Docker Desktop's host gateway.** On Linux or another runtime, replace it with that platform's gateway address. It appears in:
- `deploy/base/voice-worker/networkpolicy.yaml` (egress, ports 11434/8000/8880)
- `deploy/base/rag/networkpolicy.yaml` (egress, port 11434)

**Why RAG also points at the host:** the in-cluster ollama has `llama3.2` and `qwen2.5` but **not `nomic-embed-text`**, which the RAG service needs. The host ollama has all three.

---

## 4. Story-by-story status

**11 of 20 complete.** Full detail in `docs/sdlc/09-implementation-status.md`; summary:

| Status | Count | Stories |
|---|---|---|
| ✅ Done + verified | 9 | US-001, US-003, US-004, US-005, US-006, US-007, US-008, US-009, US-019 |
| ✅ Done, one path unexercised | 2 | US-010 (S3 backend never run), US-011 (live audio round-trip never run) |
| 🟡 Partial — manifest only | 5 | US-014, US-015, US-016, US-017, US-018 |
| ⚠️ Written, never run | 1 | US-002 |
| ❌ Not started | 3 | US-012, US-013, US-020 |

**Waves 0 and 1 are complete. Wave 2's pivotal story (US-011) is done and unblocks US-014 and US-018.**

**Test suite: 236 passing, 6 deselected** (`./.venv/Scripts/python.exe -m pytest tests/ -m "not live"`).

### Verification that IS complete
- **US-004**: `redis-sessions dbsize=1` with the exact `to_dict()` payload; 8/8 reads across two replicas via the Service.
- **US-006**: verified by *failure* — Redis down → `/readyz` 503 naming `store: TimeoutError` while reporting `banks: ok`; `/healthz` stayed 200; pod left the Ready set; Service endpoints went EMPTY; recovered on restore.
- **US-011**: adapters resolve, `kokoro-http` is in the natural-voice allowlist, local engines unchanged, missing URL raises at startup.

### Verification that is NOT complete
- **US-011's live audio round-trip** through Speaches and Kokoro — the e2e is the only thing that proves it, and it is blocked (§1).
- **US-010's `S3BankStore`** — never touched a real S3/MinIO.
- **US-002's compose stack** — never brought up.
- **Bank cache refresh is startup-only** — converges a pod that restarts, not a long-running process.

---

## 5. Defect record — 15 found, none visible in a green build

Every one was found by *running* something, not by reading it. They collapse into four mechanically-checkable families:

| Family | # | Defects |
|---|---|---|
| **Packaging** — a dependency declared somewhere other than where it's used | 5 | `.dockerignore` stripped the 9 question banks; API image carried the whole voice stack; RAG image installed only `.[mcp]`; RAG shipped no reranker tokenizer; **`redis` and `livekit-api` both lived in the wrong extra** |
| **Unmounted data directory** — read-only rootfs or emptyDir missing a path the image needs | 4 | redis (`CAP_CHOWN` vs entrypoint chown); ollama (`/root/.ollama`); Speaches (HF `hub/`); RAG (HF cache unreadable by uid 10001) |
| **NetworkPolicy** — bidirectional, and every workload needs both halves | 3 | No ingress to livekit; no ingress to either Redis; **egress without matching ingress (twice: worker→api, and WebRTC media)** |
| **One-off** | 3 | livekit's `LIVEKIT_PORT` collided with an injected service-link var; `kind load` under an unchanged tag silently ignored by `IfNotPresent`; `mcp`'s `read_timeout_seconds` takes a float, not a `timedelta` |

**Six of the fifteen were caught only because a signal was distrusted** — an HTTP 200 with an empty Redis, a green build with a broken artifact, and test failures that turned out to be the implementation's bug rather than the test's.

**This is the standing argument for US-020.** Three of the four families are directly mechanisable as CI checks.

---

## 6. Cluster operations — copy-paste

```bash
cd /d/project/ai-mock-interviewer

# Rebuild + deploy an image (kind cannot pull local images; it must be loaded)
docker build --target <api|worker|rag> -t mock-interviewer-<target>:dev .
./.tools/kind.exe load docker-image mock-interviewer-<target>:dev --name mock-interviewer

# IMPORTANT: `kind load` under an UNCHANGED tag is silently ignored by
# imagePullPolicy: IfNotPresent. Force a real pod recreation or you will test
# the old code and believe it passed.
kubectl -n mock-interviewer delete pod -l app.kubernetes.io/name=<api|voice-worker|rag>

# Apply manifest changes
kubectl kustomize deploy/overlays/local > /tmp/r.yaml
kubectl apply -f /tmp/r.yaml       # use a file; process substitution breaks here

# Quick health
kubectl -n mock-interviewer get pods
kubectl -n mock-interviewer exec deploy/api -- curl -s http://localhost:8010/readyz
```

**Bring up the cluster from scratch:**
```powershell
.\start_services.ps1 -WithKind          # ensure cluster exists + ready
.\start_services.ps1 -WithKind -RecreateKind   # rebuild it (destructive)
```
`start_services.ps1` also carries a `.NOTES` block documenting the GPU tier — read it before debugging anything slow.

**Unverified infrastructure commands** (never exercised — test before relying on them): the kind *fresh-create* path and `-RecreateKind`; they need a teardown.

---

## 7. Running the e2e

The client **must run inside the cluster**: `/voice/token` returns the in-cluster `LIVEKIT_URL`, which does not resolve on a laptop.

```bash
P=$(kubectl -n mock-interviewer get pods -l app.kubernetes.io/name=voice-worker \
      --no-headers | awk '$3=="Running"{print $1}' | head -1)
kubectl -n mock-interviewer exec "$P" -- \
  python scripts/e2e_voice_client.py --backend http://api:8010 --answers 3
```

**Note:** the worker never shows `1/1` Ready — its readiness probe is commented out pending US-014's control server. Select on `$3=="Running"`, not on `1/1`.

**Passing means:** `state=wrap`, interviewer audio frames > 0, ≥1 question asked, scores produced, and an `ended` event.

**Workaround if the in-cluster path is stubborn:** port-forward `livekit` and `api`, patch **only** the api deployment's `LIVEKIT_URL` to `http://127.0.0.1:7880` (the worker keeps the in-cluster value), and run the client from the host. Needs the host venv to have kokoro weights cached.

---

## 8. Configuration landmines (each cost real time)

- **`INTERVIEW_VOICE_LLM_TIMEOUT_S` defaults to 8 s.** Correct on GPU; **fatal on CPU** (a turn takes ~22 s). The local overlay overrides it to 120 s. Do not "fix" this back to 8 s without checking which ollama is in play.
- **`HF_HUB_OFFLINE=1` belongs in every image we ship.** In a locked-down namespace an outbound call is dropped *without an RST*, so it hangs forever instead of failing. This turned a 2-hour mystery into a 2-minute error.
- **NetworkPolicy is bidirectional.** An egress rule without its matching ingress rule looks correct in isolation and drops packets at runtime. Three defects came from this.
- **kindnet DOES enforce NetworkPolicy** on kind v0.33.0 / k8s v1.37.0 — a stale comment in `voice-worker/networkpolicy.yaml` claimed the opposite and has been corrected. The signature of a policy drop is a **TCP timeout** to a port `netstat` shows as LISTENing; a refusal would mean nothing is listening.
- **HPA needs metrics-server** — kind ships none, so both HPAs were inert objects until it was installed.
- **The worker runs `--dev` LiveKit.** `LIVEKIT_PORT` collides with Kubernetes' injected service-link variable; `enableServiceLinks: false` on the livekit pod is the fix, not renaming the Service.

---

## 9. Key files

| Path | What it is |
|---|---|
| `docs/SCALING_STATUS.md` | **This file** — resumable state |
| `docs/sdlc/09-implementation-status.md` | Per-story detail, re-verified |
| `docs/sdlc/plan-state.md` | Plan manifest: stage status, ID registry, next action |
| `docs/sdlc/` | The full plan (38 artifacts: BRD, use cases, modularization, architecture, 20 stories) |
| `Dockerfile` | Multi-target `base → api｜worker｜rag`. **api 287 MB, worker 1.78 GB** |
| `docker/install-deps.py` | Reads `pyproject.toml` so extras stay the single source of truth |
| `deploy/` | Kustomize base + `components/{cpu,gpu}-engines` + `overlays/{local,production}` |
| `start_services.ps1` | Windows launcher; `.NOTES` documents the GPU tier |
| `interviewer/api/routes.py` | All HTTP routes on one router, before the static mount |
| `interviewer/bank_store.py` | Pluggable bank storage |
| `interviewer/voice/{stt,tts}.py` | Local **and** remote (`whisper-http`/`kokoro-http`) engines |

---

## 10. Commit trail (this session)

| Commit | What |
|---|---|
| `cd64fad` | SDLC plan for the autoscaling migration (`docs/sdlc/`) |
| `eef8a9a` | Wave 0: containerize + config surface |
| `9183d4b` | Fix: question banks excluded from the image by `.dockerignore` |
| `e21187a` | Local kind cluster + launcher integration |
| `2e209a9` | Fix: split dependency layers (API no longer carries the voice stack) |
| `233bf59` | US-005 + US-004: session serializer, sessions behind a shared store |
| `bda633c` | Fix: ingress policies for both Redis instances |
| `6cced00` | US-006: real probes + route extraction |
| `db32169` / `7e0389e` | Implementation status doc (added, then corrected) |
| `104781f` | US-007 (config CORS) + US-008 (pooling, per-call metrics, voice timeout) |
| `bd075fa` | Complete US-008: persistent MCP session, brain metrics wiring |
| `39665d3` | US-009 + US-010: pluggable bank storage with a materialised cache |
| `3e018d1` | Status doc: Wave 1 complete |
| `ef7f938` | **US-011**: remote STT/TTS adapters, process-level engines, finite load threshold |
| `0fd00e8` | (user) Speaches HF cache fix, worker→api policy, overlay engine flip |
| `555d4d4` | Status doc: US-011 done |
| `dafb5a1` | Unblock e2e: bidirectional NetworkPolicy, `livekit-api`, CPU timeout |
| `4685d39` | Use the host GPU; correct the kindnet assumption |
| `876464c` | Route RAG embeddings to the host GPU; document the GPU tier |
| `01cd99e` | RAG: disable outbound telemetry, make the baked HF cache readable |

---

## 11. Standing recommendation

**Pull US-020 (CI) forward before more feature work.** Fifteen defects — every one caught by exercising a deployed stack, none by a green build — is a direct measurement of what that job is worth. Three of the four defect families are mechanically checkable:

1. a manifest check that **every workload has both an ingress AND an egress policy**
2. an image-content assertion that **every import the serving path needs is in a runtime extra**
3. a lint for **read-only rootfs without a writable mount**, and for **emptyDir paths the image reads at boot**

The `e2e` job would have caught all fifteen.

**If you would rather keep building:** US-012 next (voice session persistence — it closes open item P4), then US-014 and US-018, both of which US-011 has now unblocked.
