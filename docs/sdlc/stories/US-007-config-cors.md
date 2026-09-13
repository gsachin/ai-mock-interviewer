## US-007 — Configuration-driven CORS

> **Epic:** Phase 1 — Stateless API tier · **Primary module:** MOD-01 · **Depends on:** US-006, US-003 · **Blocks:** nothing (but the cluster cannot serve a browser without it)

**Story statement:** As a platform operator, I want the allowed CORS origins to come from configuration, so that the same image can serve the UI behind an ingress hostname instead of only from four hardcoded localhost addresses.

**Business value.** `server.py:38-46` hardcodes a four-entry localhost allowlist, which is correct for the Windows dev flow and wrong for every deployed environment: the browser's `Origin` will be the ingress hostname, no `Access-Control-Allow-Origin` header will be returned, and the page fails with an opaque CORS error in the console while the API logs a perfectly healthy 200. This story makes the list configurable while defaulting to **exactly** today's four values, so nothing about dev changes — the change is that an operator can now fix it without a code change, and the failure mode moves from a browser console to a startup error.

## Acceptance Criteria

### Scenario 1 — The default is exactly today's four values
**Given** `INTERVIEW_CORS_ORIGINS` is unset
**When** the app is built
**Then** the allowed origins are exactly `http://localhost:8080`, `http://127.0.0.1:8080`, `http://localhost:8501`, `http://127.0.0.1:8501`
**And** a preflight from `http://localhost:8501` returns `Access-Control-Allow-Origin: http://localhost:8501`
**And** the dev flow (legacy static UI on :8080, Streamlit on :8501) is unchanged

### Scenario 2 — A deployed origin works without a code change
**Given** `INTERVIEW_CORS_ORIGINS=https://interview.example.com`
**When** a preflight from that origin is sent
**Then** it is allowed
**And** a preflight from `http://localhost:8501` is **not** allowed — configuration replaces the list, it does not extend it
**And** no code file changes between the dev and deployed configurations

### Scenario 3 — Empty means same-origin only
**Given** `INTERVIEW_CORS_ORIGINS=""` (set, but empty)
**When** the app is built
**Then** the allowed origin list is empty, not `[""]`
**And** a same-origin request (no `Origin` header, or `Origin` equal to the API's own host) is served normally
**And** a cross-origin preflight receives no `Access-Control-Allow-Origin` header
**And** this is the expected in-cluster value — the UI ships inside this image and is served from the same origin, so the usual correct setting is *empty* or the ingress origin alone

### Scenario 4 — Malformed configuration fails at startup, not at request time
**Given** `INTERVIEW_CORS_ORIGINS` containing an entry with no scheme (`example.com`), or a bare `*` combined with credentials, or only whitespace
**When** `InterviewerConfig.from_env(...)` is called
**Then** `ValueError` is raised naming the offending entry and the reason
**And** the process fails fast rather than starting with a list that can never match — a CORS misconfiguration that starts successfully is the expensive kind

### Scenario 5 — Parsing is forgiving about the harmless mistakes
**Given** `INTERVIEW_CORS_ORIGINS=" https://a.example.com , https://b.example.com/, "`
**When** it is parsed
**Then** the result is `["https://a.example.com", "https://b.example.com"]`
**And** surrounding whitespace is stripped, empty entries are dropped, and a trailing slash on an origin is removed (a browser never sends a path, so a retained slash would silently never match)

### Scenario 6 — Credentials default to false and stay consistent with `*`
**Given** no `INTERVIEW_CORS_ALLOW_CREDENTIALS`
**When** the middleware is configured
**Then** `allow_credentials` is `False` and no `Access-Control-Allow-Credentials` header is emitted
**And** the app sets no cookies — the LiveKit JWT is returned in a JSON body, not a cookie — so credentials are not needed
**Given** `INTERVIEW_CORS_ALLOW_CREDENTIALS=true` with an explicit origin list
**Then** the header is emitted and responses carry `Vary: Origin`

### Scenario 7 — A disallowed origin is a browser problem, not a server error
**Given** an `Origin` that is not in the list
**When** a simple `GET /skills` is sent
**Then** the response is 200 **without** an `Access-Control-Allow-Origin` header
**And** the test asserts the header's absence rather than a status code — a server that 403s on origin would break non-browser clients, which send no `Origin` at all

### Scenario 8 — Preflight shape is unchanged
**Given** a preflight `OPTIONS` from an allowed origin
**When** it is answered
**Then** `Access-Control-Allow-Methods` and `Access-Control-Allow-Headers` still reflect the current `*` settings
**And** `POST /voice/token` and `POST /skills` remain callable cross-origin from an allowed origin, exactly as the legacy UI and Streamlit do today

## HLD

CORS is a browser-enforcement mechanism: the server's only job is to return an honest allow-list header, and the deployment's only job is to get the list right. This story separates those two.

```mermaid
flowchart LR
  ENV["INTERVIEW_CORS_ORIGINS<br/>unset → 4 localhost defaults<br/>set → parsed list · '' → same-origin only"] --> PARSE[parse + validate at from_env]
  PARSE -->|malformed| FAIL["ValueError at startup — process exits"]
  PARSE -->|valid| MW["CORSMiddleware(allow_origins=...)<br/>allow_credentials=flag"]
  MW --> REQ{Browser sends Origin?}
  REQ -->|yes, in list| OK["+ Access-Control-Allow-Origin"]
  REQ -->|yes, not in list| NO["no ACAO — browser blocks; server still 200"]
  REQ -->|no (same-origin, curl, server-side)| PASS["served normally, CORS irrelevant"]
```

**Why the in-cluster value is usually empty.** `web/index.html` is served by the same app that answers `/voice/token` (a deliberate existing choice: same origin, no CORS). A preflight therefore normally never happens in production. The configured value matters only for the legacy static-UI option (`python -m http.server :8080`), the `legacy` Streamlit profile, and any future separate front-end host. Documenting that prevents an operator from reflexively setting `*`.

**Why `allow_credentials` defaults false.** The API carries no cookies and no session cookie exists — the candidate identifies with a LiveKit JWT passed in a request body. Defaulting it true would be a needless widening, and it would also make the `*` origin illegal, producing a confusing startup failure for someone who copied a snippet.

**Why fail fast on malformed entries.** Every CORS misconfiguration has the same signature: the server is fine, the page is broken, and the error text lives in a browser console nobody on the server side can see. Validating the list at `from_env` time converts that into a crash loop with the bad entry named. The trade — a pod that clearly refuses to start instead of one that quietly serves a broken UI — is the correct one, and it mirrors the `readOnlyRootFilesystem` decision in US-001.

## LLD

**Files changed**

| Path | Change |
|---|---|
| `interviewer/config.py` | `cors_origins: tuple[str, ...]`, `cors_allow_credentials: bool`; parsing + validation in `from_env` |
| `interviewer/server.py` | `create_app(config) -> FastAPI` factory; module-level `app = create_app(config)` preserved for `uvicorn interviewer.server:app`; CORS middleware reads the config rather than a literal |
| `tests/test_config_defaults.py` | Add both keys to `EXPECTED_DEFAULTS` (US-003) |
| `tests/test_cors.py` (new) | Scenarios 1-8 |
| `start_services.ps1` | **Unchanged** — the default path is byte-identical |

**Signatures and shape**

```python
# config.py
_DEFAULT_CORS_ORIGINS = (
    "http://localhost:8080", "http://127.0.0.1:8080",
    "http://localhost:8501", "http://127.0.0.1:8501",
)

cors_origins: tuple[str, ...] = _DEFAULT_CORS_ORIGINS
cors_allow_credentials: bool = False

def parse_cors_origins(raw: str | None) -> tuple[str, ...]:
    """None (unset) → the four localhost defaults. '' → (). Otherwise split
    on ',', strip, drop blanks, strip one trailing '/', and validate: every
    entry has an http(s) scheme; '*' is rejected when credentials are on."""
```

Factory:

```python
# server.py
def create_app(config: InterviewerConfig) -> FastAPI:
    app = FastAPI(title="mock-interviewer", version="0.1.0")
    if config.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=list(config.cors_origins),
                           allow_credentials=config.cors_allow_credentials,
                           allow_methods=["*"], allow_headers=["*"])
    app.include_router(router)          # US-006 — before the mount
    if _web_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(_web_dir), html=True), name="web")
    return app

config = InterviewerConfig.from_env()
app = create_app(config)
```

The factory is what makes these scenarios testable: building an app with a different origin list no longer requires re-importing the module with a patched environment.

**Edge cases.** An unset variable and an empty variable must be distinguishable — `env.get("INTERVIEW_CORS_ORIGINS", None)` with `None` meaning "unset", because `""` and "unset" carry opposite intent. Origins are compared as exact strings after normalization; `http://localhost:8080` and `http://127.0.0.1:8080` are different origins and both must stay in the default list. A trailing slash is stripped from the origin but never from a path in an entry (paths are invalid in an `Origin` and are rejected). `allow_methods`/`allow_headers` stay `*` deliberately: the API surface is small, wholly public, and preflight caching is not worth narrowing for. `Vary: Origin` handling is Starlette's; the test asserts the header when credentials are on so a middleware swap cannot silently drop cache-correctness.

**Test scenarios**

| # | Scenario | Check | Where |
|---|---|---|---|
| 1 | Defaults unchanged | four origins; preflight from :8501 allowed | `tests/test_cors.py` |
| 2 | Configured replaces | ingress origin allowed, localhost refused | `tests/test_cors.py` |
| 3 | Empty → same-origin | no ACAO on cross-origin; same-origin unaffected | `tests/test_cors.py` |
| 4 | Malformed rejected | no scheme; `*`+credentials; whitespace-only → `ValueError` | `tests/test_cors.py` |
| 5 | Forgiving parse | whitespace, trailing comma, trailing slash | `tests/test_cors.py` |
| 6 | Credentials default | no `Access-Control-Allow-Credentials` | `tests/test_cors.py` |
| 7 | Credentials on | header present + `Vary: Origin` | `tests/test_cors.py` |
| 8 | Disallowed ≠ error | 200 with no ACAO | `tests/test_cors.py` |
| 9 | Preflight shape | `OPTIONS` allows methods/headers | `tests/test_cors.py` |
| 10 | `uvicorn` entrypoint | `interviewer.server:app` still imports and serves | `tests/test_probes.py` |
| 11 | Defaults pinned | two keys in `EXPECTED_DEFAULTS` | `tests/test_config_defaults.py` |

## Traceability

| Upstream | Reference | How this story serves it |
|---|---|---|
| Module | MOD-01 (Management API) | Owns the middleware and the app factory |
| Module | MOD-11 (Web UI) | Same-origin with the API is why the correct in-cluster value is usually empty |
| Requirement | BRD-03 — configuration-driven CORS | Implemented exactly, with today's four values as the default |
| Requirement | BRD-14 — cloud-agnostic base | The ingress hostname is an overlay value, never baked into the image |
| Use case | UC-05 — deploy or upgrade the platform | "Missing optional config falls back to documented defaults" is this default list |
| Assumption | AS-05 — the Windows dev flow keeps working | The default is byte-identical, so the legacy UI and Streamlit keep working untouched |
| Requirement | BRD-17 — authentication model (deferred) | Credentials default false is consistent with there being no cookie-based auth to protect |

**Test files:** `tests/test_cors.py` (new), `tests/test_config_defaults.py` (extended).
