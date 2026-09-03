"""Interviewer configuration, env-driven.

The ``RAG_CORE_*`` namespace belongs to the enterprise-rag-core service
(separate deployment); this consumer uses its own variables.
"""
import os
from dataclasses import dataclass


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
            livekit_url=env.get("LIVEKIT_URL", "http://127.0.0.1:7880"),
            livekit_api_key=env.get("LIVEKIT_API_KEY"),
            livekit_api_secret=env.get("LIVEKIT_API_SECRET"),
            max_questions=int(env.get("INTERVIEW_MAX_QUESTIONS", "3")),
            answer_timeout_s=float(env.get("INTERVIEW_ANSWER_TIMEOUT_S", "60")),
            judge_model=env.get("INTERVIEW_JUDGE_MODEL"),
        )
