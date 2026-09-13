## US-012 — Voice session persistence and the room→session mapping

| | |
|---|---|
| **Covers** | BRD-05 · UC-01 · UC-07 |
| **Modules** | MOD-02 (Voice Agent Worker), MOD-09 (Session Store), MOD-01 (Management API) |
| **Depends on** | US-004 / US-005 (the API reads sessions from the store, not a process-local dict) |
| **Closes** | Open item **P4** — "voice session results never reach the management plane, so `GET /sessions/{id}` is empty for voice rooms" |
| **Status** | Not started |

### Story

**Story statement:** As a candidate, I want my voice interview's transcript and scores to be readable after the call ends, so that I can review what I said and how I was scored instead of losing the whole session the moment the browser tab closes.

**Business value.** A mock interview that vanishes is a demo, not a product. Today the summary is built, complete with per-question scores and a latency ledger, and then **only logged** (`interviewer/voice/agent.py:281-286`). `session_store.py` already implements the right abstraction (`REC-01`) — it is simply dead code on the serving path — so this is wiring plus a serializer, not new infrastructure.

**The bug that becomes real the moment persistence lands.** `agent.py:97` sets `session_id=ctx.room.name` (e.g. `interview-system-design-a1b2c3d4e5f6`), while `/voice/token` (`server.py:118-122`) stores the registry entry under a 12-hex session id. They never match. Nothing breaks today because nothing is written; the first persisted write would land under an unreachable key and `GET /sessions/{id}` would keep returning 404 — a silent failure that looks exactly like "not implemented yet". **Fix the mapping first, then persist.**

## Acceptance Criteria

### AC-1 — A completed voice interview is retrievable

```gherkin
**Given** a candidate completes a voice interview through the browser
**When** the interview publishes its summary and the ended event
**Then** the terminal session is persisted to the shared session store
**And** GET /sessions/{id} for the id returned by /voice/token returns the transcript, the per-question scores and state "wrap"
**And** the response is identical whether it is served by the replica that issued the token or by any other replica
```

### AC-2 — The room-to-session mapping is authoritative

```gherkin
**Given** POST /voice/token creates a session with id S and room R
**When** the mapping is written
**Then** the key holds S with a 24 hour TTL
**And** the worker resolves the session id from room R through that mapping
**And** the key the worker writes the summary under equals S, not R
```

### AC-3 — Resolution degrades predictably

```gherkin
**Given** the mapping key is missing because its TTL expired or the room was created outside the API
**When** the worker resolves the session id for that room
**Then** it falls back to parsing interview-<domain>-<12 hex> from the room name
**And** if the name does not match that shape it uses a derived voice-<room> id
**And** it logs a warning naming the room and the fallback used, and the interview proceeds
```

### AC-4 — Persistence does not depend on the management plane

```gherkin
**Given** the API deployment is scaled to zero replicas
**When** a voice interview completes
**Then** the worker still writes the session directly through the session store
**And** the record is readable as soon as an API replica returns
**And** no HTTP call from the worker to the API is required at any point
```

### AC-5 — The stored shape renders both text and voice sessions

```gherkin
**Given** a text session and a voice session are both persisted
**When** GET /sessions/{id} renders each
**Then** both are produced by the same code path with no branching on the session's origin
**And** the payload always carries session_id, tenant_id, domain, state, turns, scores, stats and source
**And** source is "voice" or "text"
```

### AC-6 — Serialization round-trips a real Session

```gherkin
**Given** a Session holding an InterviewerState enum and a list of Turn dataclasses
**When** it is saved through RedisSessionStore
**Then** json.dumps no longer raises, because to_dict() flattens the enum to its value and each Turn to a plain object
**And** from_dict() reconstructs an equal Session, enum and turns included
```

### AC-7 — Empty, timeout and failure paths

```gherkin
**Given** the candidate disconnects before the first question
**When** the interview ends
**Then** a record is still written carrying the state actually reached, not a fabricated "wrap"

**Given** the session store is unreachable
**When** the terminal summary is written
**Then** the worker logs the failure with the session id and continues shutting down
**And** the candidate's call is not affected
```

### AC-8 — Writes are idempotent and bounded

```gherkin
**Given** the same session id is written more than once
**When** the writes complete
**Then** the last write wins under the same key with the same TTL
**And** no duplicate keys are created
**And** the key expires 86400 seconds after the last write
```

## HLD

```mermaid
sequenceDiagram
  participant C as Candidate (browser)
  participant API as MOD-01 Management API
  participant SFU as MOD-10 LiveKit SFU
  participant W as MOD-02 Voice Worker
  participant R as MOD-09 Redis

  C->>API: POST /voice/token {domain}
  API->>API: session_id = uuid4().hex[:12]; room = interview-<domain>-<sid>
  API->>R: SET interviewer:session:<sid> (TTL 24h)  [seed]
  API->>R: SET interviewer:room:<room> = <sid> (TTL 24h)
  API-->>C: {livekit_url, token, room, session_id}
  C->>SFU: connect wss
  SFU->>W: dispatch job (room R)
  W->>R: GET interviewer:room:<room>  -> session_id
  W->>W: run FSM, checkpoint per hop (US-013)
  W->>R: SET interviewer:session:<sid> (terminal summary, TTL 24h)
  C->>API: GET /sessions/<sid>
  API->>R: GET interviewer:session:<sid>
  API-->>C: {state: wrap, turns, scores, stats, source: voice}
```

**Why the mapping and not name parsing.** Room names are constructed by the API but they are also a parsing surface: a skill whose name contains a dash breaks `interview-<domain>-<sid>` splitting, and `agent.py:56-66`'s `domain_from_room` already has to scan the banks folder to cope. A single `SET` removes that class of bug and is independently unit-testable without a LiveKit room.

**Why the worker writes Redis directly.** Persisting through the API would make a live interview depend on a different deployment being up, and the worker is precisely the component that must survive an API outage. Redis is the shared substrate; both planes talk to it.

## LLD

**`interviewer/state_machine.py`** — add the serializer the docstring already promises (`REC-04`). Keep the module I/O-free.

```python
def to_dict(self) -> dict[str, Any]:
    """Flatten enum + dataclasses into JSON-safe primitives."""
    return {"session_id": self.session_id, "tenant_id": self.tenant_id,
            "domain": self.domain, "state": self.state.value,
            "current_question_id": self.current_question_id,
            "turns": [{"role": t.role, "text": t.text} for t in self.turns],
            "scores": self.scores}

@classmethod
def from_dict(cls, data: dict[str, Any]) -> "Session":
    """Inverse of to_dict(); InterviewerState(value) restores the enum."""
```

**`interviewer/session_store.py`** — one shape function and two key helpers; the `SessionStore` protocol keeps `save`/`load`/`delete` unchanged.

```python
SESSION_TTL_S = 86400
def session_key(session_id: str) -> str: ...          # interviewer:session:{sid}
def room_key(room: str) -> str: ...                    # interviewer:room:{room}

def session_payload(session: Session, *, source: str, doc_id: str | None = None,
                    stats: dict[str, Any] | None = None, state: str | None = None,
                    reason: str | None = None, seq: int | None = None,
                    persisted_at: float | None = None) -> dict[str, Any]:
    """The ONE persisted shape, shared by the API seed and the worker's writes."""

class RedisSessionStore:
    async def set_room_session(self, room: str, session_id: str, *,
                               ttl_seconds: int = SESSION_TTL_S) -> None: ...
    async def get_room_session(self, room: str) -> str | None: ...

async def resolve_session_id(store, room: str, *,
                             fallback_domain: str = "voice") -> tuple[str, str]:
    """-> (session_id, how) where how is 'mapping' | 'parsed' | 'derived'.
    Never raises: a store error degrades to 'derived' with a warning."""
```

Persisted payload (the normalized shape `AC-5` requires):

| Key | Type | Source |
|---|---|---|
| `session_id` | str | `Session.session_id` |
| `tenant_id` | str | `Session.tenant_id` (`"default"` until the auth model lands) |
| `domain` | str | `Session.domain` |
| `doc_id` | str \| null | `bank-<domain>` for voice |
| `state` | str | `Session.state.value`, or `"interrupted"` / `"abandoned"` |
| `turns` | list[{role, text}] | transcript |
| `scores` | list[dict] | score ledger (question_id, scores, verdict, model_answer, …) |
| `stats` | dict \| null | the summary's stats incl. `voice_budget_bar` and `hops` |
| `source` | `"voice"` \| `"text"` | writer |
| `reason` | str \| null | terminal or interrupted reason |
| `seq` | int \| null | monotonic write counter (US-013 ordering) |
| `persisted_at` | float | epoch seconds of the last write |

**`interviewer/server.py`**
- `POST /voice/token`: after creating the `Session`, seed both keys through the store (`session_payload(session, source="voice")` and `set_room_session(room, session.session_id)`). A store failure must not fail token issuance — log and continue, because the interview itself does not need the store.
- `GET /sessions/{id}`: read through the store first, fall back to the in-process registry (which US-004/US-005 make store-backed). Return the persisted dict **as stored** so voice and text sessions render identically.

**`interviewer/voice/agent.py`**
- Replace line 97's `session_id=ctx.room.name` with `session_id, how = await resolve_session_id(store, ctx.room.name)`, logging `how`; keep `domain_from_room` for the domain and keep `tenant_id="default"`.
- In `run_brain()`'s `finally` (line 296), persist the terminal summary — this is the change that closes P4. The existing `log.info` stays; the store write is added beside it, wrapped so a store error can never mask the `ended` event.

**Edge cases.** Redis reachable at token time but not at the end (persist fails, interview unaffected); two interviews in the same room name across time (the mapping is refreshed on each token issuance); a room created by an operator with the LiveKit CLI (no mapping → parsed fallback); a store payload written by an older build (missing keys → `load` returns it as-is, the API tolerates absent optional keys); the `source` field on records written before this story (absent → treated as text).

| # | Test scenario | File | Assertion |
|---|---|---|---|
| 1 | Session round-trips through `to_dict`/`from_dict` | `tests/test_state_machine.py` | reconstructed session equals the original, enum included |
| 2 | Redis store no longer crashes on a real Session | `tests/test_session_store.py` | `save` succeeds; `load` returns the same shape |
| 3 | Room→session mapping round-trip and TTL | `tests/test_session_store.py` | `get_room_session` returns the id; TTL is 86400 |
| 4 | Token id equals the persisted id (the P4 regression) | `tests/test_voice_pipeline.py` | `/voice/token` id == `resolve_session_id(room)` |
| 5 | Resolution fallbacks | `tests/test_voice_pipeline.py` | mapping → parsed → derived, never raises |
| 6 | Terminal summary is persisted | `tests/test_voice_pipeline.py` | fake store receives the summary from `run_brain()`'s finally |
| 7 | Both sources render through one shape | `tests/test_skills_api.py` / new API test | text and voice payloads return the same key set |
| 8 | Store down does not break the interview | `tests/test_voice_pipeline.py` | a raising store still yields the `ended` event |
| 9 | End-to-end then read back | `scripts/e2e_voice_client.py` | after `state=wrap`, `GET /sessions/{sid}` has turns and scores |

## Traceability

| ID | Requirement / decision | How this story satisfies it |
|---|---|---|
| BRD-05 | Durable voice sessions; `GET /sessions/{id}` returns a completed voice interview | The terminal summary is persisted under the API's session id and read back by the API |
| BRD-01 | Stateless management API tier | Session reads come from the shared store, so any replica answers |
| UC-01 | Take a voice interview — postcondition "summary and per-question scores persisted" | Delivered here; the checkpoint half of the postcondition is US-013 |
| UC-07 | Resume after an interrupted interview — the record must be visible with a reason | This story makes the record exist; US-013 fills in the `interrupted` state |
| MOD-02 / MOD-09 / MOD-01 | Worker, session store, management API | Worker writes directly; API seeds and reads |
| DAT-01 / DAT-02 | Session and interview summary become Redis-authoritative | Both now have one writer each and one shared shape |
| Decision DG-02 | Reuse the existing store; add the serializer | `Session.to_dict()`/`from_dict()` plus `RedisSessionStore` wiring |
| REC-01 | `RedisSessionStore` is the right abstraction, currently dead | Reuse — it becomes the serving path |
| REC-04 | A serialization promise the code makes but does not keep | Extend — the method now exists |
| Open item P4 | Voice results never reach the management plane | Closed by the terminal write |

**Test files covering this story:** `tests/test_state_machine.py`, `tests/test_session_store.py`, `tests/test_voice_pipeline.py`, `tests/test_skills_api.py` (shape parity), `scripts/e2e_voice_client.py` (acceptance).
