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
        )
