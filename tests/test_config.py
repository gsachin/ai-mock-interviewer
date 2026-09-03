"""InterviewerConfig: env parsing and defaults."""
from interviewer.config import InterviewerConfig


def test_defaults():
    cfg = InterviewerConfig.from_env(environ={})
    assert cfg.rag_mcp_url == "http://127.0.0.1:8000/mcp"
    assert cfg.rag_mcp_token is None
    assert cfg.default_domain == "system-design"
    assert cfg.top_k == 5


def test_env_overrides():
    cfg = InterviewerConfig.from_env({
        "RAG_MCP_URL": "https://rag.example.com/mcp",
        "RAG_MCP_TOKEN": "secret-bearer",
        "INTERVIEW_DOMAIN": "ios",
        "INTERVIEW_TOP_K": "8",
    })
    assert cfg.rag_mcp_url == "https://rag.example.com/mcp"
    assert cfg.rag_mcp_token == "secret-bearer"
    assert cfg.default_domain == "ios"
    assert cfg.top_k == 8


def test_voice_defaults_and_overrides():
    cfg = InterviewerConfig.from_env(environ={})
    assert cfg.tts_provider == "cartesia"      # natural-voice-first default
    assert cfg.tts_voice_id is None

    cfg2 = InterviewerConfig.from_env({
        "INTERVIEW_TTS_PROVIDER": "elevenlabs",
        "INTERVIEW_TTS_VOICE_ID": "voice_9BWqMINAzXWkHeueBiaj",
    })
    assert cfg2.tts_provider == "elevenlabs"
    assert cfg2.tts_voice_id == "voice_9BWqMINAzXWkHeueBiaj"


def test_voice_session_shape_defaults():
    """T1: three questions per session, a 60 s answer budget, and no judge
    override unless configured."""
    cfg = InterviewerConfig.from_env(environ={})
    assert cfg.max_questions == 3
    assert cfg.answer_timeout_s == 60.0
    assert cfg.judge_model is None


def test_voice_session_shape_env_overrides():
    cfg = InterviewerConfig.from_env({
        "INTERVIEW_MAX_QUESTIONS": "5",
        "INTERVIEW_ANSWER_TIMEOUT_S": "45.5",
        "INTERVIEW_JUDGE_MODEL": "qwen2.5:3b",
    })
    assert cfg.max_questions == 5
    assert cfg.answer_timeout_s == 45.5
    assert cfg.judge_model == "qwen2.5:3b"
