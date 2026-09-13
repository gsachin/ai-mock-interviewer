"""Interviewer configuration, env-driven.

The ``RAG_CORE_*`` namespace belongs to the enterprise-rag-core service
(separate deployment); this consumer uses its own variables.
"""
import os
from dataclasses import dataclass

_TRUTHY = {"1", "true", "yes", "on"}


def _as_bool(value: str | None, default: bool = False) -> bool:
    """Env booleans, parsed permissively.

    ``bool(os.environ.get(...))`` is the classic bug here: the string "false"
    is truthy, so ``INTERVIEW_READY_REQUIRES_RAG=false`` would read as True —
    the exact opposite of what the operator wrote, on the flag that decides
    whether a RAG blip takes the API out of service.
    """
    if value is None:
        return default
    return value.strip().lower() in _TRUTHY


def _as_origins(value: str | None) -> tuple[str, ...]:
    """Comma-separated origins. Unset keeps the historical defaults.

    An explicitly EMPTY value means "no CORS allowed" and is respected rather
    than falling back to the defaults -- conflating "" with unset would make it
    impossible to turn CORS off, which is exactly what a same-origin ingress
    deployment wants.
    """
    if value is None:
        return InterviewerConfig.__dataclass_fields__["cors_origins"].default
    return tuple(o.strip() for o in value.split(",") if o.strip())


@dataclass(frozen=True)
class InterviewerConfig:
    rag_mcp_url: str = "http://127.0.0.1:8000/mcp"
    rag_mcp_token: str | None = None   # OIDC bearer (scope rag:retrieve); None = none-auth mode
    default_domain: str = "system-design"
    top_k: int = 5
    # Voice quality is a hard requirement: the interviewer must sound like a
    # natural human. Robotic (espeak-class) voices are excluded from production.
    tts_provider: str = "cartesia"     # cartesia | elevenlabs | kokoro | piper
    tts_voice_id: str | None = None    # provider voice preset; None = provider default
    # Phase 2 LLM: any OpenAI-compatible chat endpoint (vLLM / Ollama / MLX).
    llm_base_url: str = "http://127.0.0.1:8000/v1"
    llm_model: str | None = None       # required for LLM turns
    llm_token: str | None = None
    session_store: str = "memory"      # memory | redis
    redis_url: str = "redis://localhost:6379"
    # Phase 3 voice: STT/TTS providers + the hot-path LLM (a fast small model —
    # the judge LLM above can stay slow since judging is off the hot path).
    stt_provider: str = "stub"         # deepgram | faster-whisper | stub
    deepgram_api_key: str | None = None
    whisper_model: str = "base"
    whisper_device: str = "auto"
    cartesia_api_key: str | None = None
    elevenlabs_api_key: str | None = None
    piper_binary: str = "piper"
    piper_model: str | None = None
    kokoro_voice: str = "af_heart"     # high-quality natural preset
    kokoro_model_dir: str | None = None  # None = ~/.cache/mock-interviewer/kokoro
    voice_llm_base_url: str | None = None
    voice_llm_model: str | None = None
    # Voice-path LLM timeout, separate from the 300 s judge/LLM default.
    #
    # 300 s is fine for judging (off the hot path, bounded by
    # answer_timeout_s) and catastrophic on the voice path: a stalled vLLM
    # would hang a spoken turn for FIVE MINUTES while the candidate sits in
    # silence. The measured first token on CPU llama3.2:3b is 280-800 ms, so
    # 8 s is roughly 10x headroom — long enough not to trip on a slow start,
    # short enough to fail fast and re-prompt instead of going quiet.
    voice_llm_timeout_s: float = 8.0
    # Phase 3 voice: LiveKit deployment. The worker and the /voice/token
    # endpoint share these; unset keys = LiveKit dev-mode defaults.
    livekit_url: str = "http://127.0.0.1:7880"
    livekit_api_key: str | None = None      # dev mode: "devkey"
    livekit_api_secret: str | None = None   # dev mode: "secret"
    # RCA fixes (2026-09-03): session shape + no-hang guarantees.
    max_questions: int = 3      # spoken questions per voice session
    answer_timeout_s: float = 60.0  # per-answer wait; then one re-prompt, then
                                # the question is scored as unanswered
    judge_model: str | None = None  # override the judge LLM model on the
                                # same base URL (a faster model is a latency
                                # lever; None = the configured llm_model)
    # Static asset + content paths. Both default to None, meaning "resolve
    # relative to the package as before" — so behaviour is unchanged on the
    # Windows dev flow. Containers set them explicitly because server.py
    # resolves web/ as Path(__file__).parent.parent / "web", which under a
    # plain site-packages install points somewhere that does not exist and
    # fails SILENTLY (the is_dir() guard), serving nothing with no error.
    web_dir: str | None = None      # INTERVIEW_WEB_DIR — static UI folder
    bank_dir: str | None = None     # INTERVIEW_BANK_DIR — question_banks folder
    # Readiness gate for the RAG service. Defaults to False, and that default
    # is a design decision rather than a convenience: the app is built to
    # survive a RAG outage (GET /skills reports rag_ok=false instead of
    # failing; an interview degrades to last-known rubric). Requiring RAG for
    # readiness would invert that — a RAG blip would pull the API pods out of
    # service and take /voice/token and the static UI down with them, which
    # are the two things that must keep working.
    ready_requires_rag: bool = False
    # CORS (US-007). The defaults are byte-identical to the four values that
    # were hardcoded in server.py, so the dev flow is unchanged -- but they are
    # now overridable, which is what unblocks serving from a real hostname.
    # The hardcoded list blocked every request the moment the app sat behind an
    # ingress, and the failure mode is a browser-side CORS error with a
    # perfectly healthy server: nothing in the logs, nothing in the probes.
    #
    # In-cluster this is usually just the ingress origin, or empty: the UI is
    # served from this same app, so it is same-origin and needs no CORS at all.
    # The four localhost entries exist for the legacy `python -m http.server`
    # (:8080) and Streamlit (:8501) paths, which call the token endpoint
    # cross-origin.
    cors_origins: tuple[str, ...] = (
        "http://localhost:8080", "http://127.0.0.1:8080",
        "http://localhost:8501", "http://127.0.0.1:8501",
    )
    # Credentials cannot be combined with a wildcard origin, so this stays a
    # separate opt-in rather than something inferred from `*` being present.
    cors_allow_credentials: bool = False

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "InterviewerConfig":
        env = os.environ if environ is None else environ
        return cls(
            rag_mcp_url=env.get("RAG_MCP_URL", "http://127.0.0.1:8000/mcp"),
            rag_mcp_token=env.get("RAG_MCP_TOKEN"),
            default_domain=env.get("INTERVIEW_DOMAIN", "system-design"),
            top_k=int(env.get("INTERVIEW_TOP_K", "5")),
            tts_provider=env.get("INTERVIEW_TTS_PROVIDER", "cartesia"),
            tts_voice_id=env.get("INTERVIEW_TTS_VOICE_ID"),
            llm_base_url=env.get("INTERVIEW_LLM_BASE_URL", "http://127.0.0.1:8000/v1"),
            llm_model=env.get("INTERVIEW_LLM_MODEL"),
            llm_token=env.get("INTERVIEW_LLM_TOKEN"),
            session_store=env.get("INTERVIEW_SESSION_STORE", "memory"),
            redis_url=env.get("INTERVIEW_REDIS_URL", "redis://localhost:6379"),
            stt_provider=env.get("INTERVIEW_STT_PROVIDER", "stub"),
            deepgram_api_key=env.get("INTERVIEW_DEEPGRAM_API_KEY"),
            whisper_model=env.get("INTERVIEW_WHISPER_MODEL", "base"),
            whisper_device=env.get("INTERVIEW_WHISPER_DEVICE", "auto"),
            cartesia_api_key=env.get("INTERVIEW_CARTESIA_API_KEY"),
            elevenlabs_api_key=env.get("INTERVIEW_ELEVENLABS_API_KEY"),
            piper_binary=env.get("INTERVIEW_PIPER_BINARY", "piper"),
            piper_model=env.get("INTERVIEW_PIPER_MODEL"),
            kokoro_voice=env.get("INTERVIEW_KOKORO_VOICE", "af_heart"),
            kokoro_model_dir=env.get("INTERVIEW_KOKORO_MODEL_DIR"),
            voice_llm_base_url=env.get("INTERVIEW_VOICE_LLM_BASE_URL"),
            voice_llm_model=env.get("INTERVIEW_VOICE_LLM_MODEL"),
            voice_llm_timeout_s=float(
                env.get("INTERVIEW_VOICE_LLM_TIMEOUT_S", "8")),
            livekit_url=env.get("LIVEKIT_URL", "http://127.0.0.1:7880"),
            livekit_api_key=env.get("LIVEKIT_API_KEY"),
            livekit_api_secret=env.get("LIVEKIT_API_SECRET"),
            max_questions=int(env.get("INTERVIEW_MAX_QUESTIONS", "3")),
            answer_timeout_s=float(env.get("INTERVIEW_ANSWER_TIMEOUT_S", "60")),
            judge_model=env.get("INTERVIEW_JUDGE_MODEL"),
            web_dir=env.get("INTERVIEW_WEB_DIR"),
            bank_dir=env.get("INTERVIEW_BANK_DIR"),
            ready_requires_rag=_as_bool(env.get("INTERVIEW_READY_REQUIRES_RAG")),
            cors_origins=_as_origins(env.get("INTERVIEW_CORS_ORIGINS")),
            cors_allow_credentials=_as_bool(
                env.get("INTERVIEW_CORS_ALLOW_CREDENTIALS")),
        )
