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
        )
