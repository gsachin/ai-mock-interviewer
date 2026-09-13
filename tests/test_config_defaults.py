"""A tripwire, not a behaviour test.

Every default in ``InterviewerConfig`` is asserted against its literal value.
That is deliberately boring: the migration refactors `server.py`, `config.py`
and the worker, and the risk is not that a default breaks loudly — it is that
one drifts quietly and the Windows dev flow (`start_services.ps1`, which
supplies almost no environment) starts behaving differently with nothing
pointing at why.

`AS-05` says the Windows flow keeps working. This file is how that is
guaranteed by a test rather than by care. If a change here is intentional,
update the literal in the same commit — that edit *is* the review.
"""
import pytest

from interviewer.config import InterviewerConfig


# The defaults as of the pre-migration baseline. Each entry is the literal
# value a developer running `start_services.ps1` with no extra environment
# gets today.
BASELINE_DEFAULTS = {
    # RAG
    "rag_mcp_url": "http://127.0.0.1:8000/mcp",
    "rag_mcp_token": None,
    "default_domain": "system-design",
    "top_k": 5,
    # LLM
    "llm_base_url": "http://127.0.0.1:8000/v1",
    "llm_model": None,
    "llm_token": None,
    "judge_model": None,
    # Sessions
    "session_store": "memory",
    "redis_url": "redis://localhost:6379",
    # Voice engines
    "tts_provider": "cartesia",
    "tts_voice_id": None,
    "stt_provider": "stub",
    "deepgram_api_key": None,
    "whisper_model": "base",
    "whisper_device": "auto",
    "cartesia_api_key": None,
    "elevenlabs_api_key": None,
    "piper_binary": "piper",
    "piper_model": None,
    "kokoro_voice": "af_heart",
    "kokoro_model_dir": None,
    "voice_llm_base_url": None,
    "voice_llm_model": None,
    # LiveKit
    "livekit_url": "http://127.0.0.1:7880",
    "livekit_api_key": None,
    "livekit_api_secret": None,
    # Session shape (RCA 2026-09-03)
    "max_questions": 3,
    "answer_timeout_s": 60.0,
    # Paths (Wave 0) — None means "resolve relative to the package", i.e. the
    # pre-migration behaviour. Containers set these explicitly.
    "web_dir": None,
    "bank_dir": None,
    # Readiness (US-006) — off by default so a RAG outage cannot pull the API
    # out of service.
    "ready_requires_rag": False,
    # CORS (US-007) — these four are the values that were hardcoded in
    # server.py. Changing them changes the Windows dev flow.
    "cors_origins": (
        "http://localhost:8080", "http://127.0.0.1:8080",
        "http://localhost:8501", "http://127.0.0.1:8501",
    ),
    "cors_allow_credentials": False,
    # Voice-path LLM timeout (US-008). Deliberately 8 s, not the 300 s judge
    # default: a stalled engine must fail fast rather than leave a candidate
    # in silence for five minutes.
    "voice_llm_timeout_s": 8.0,
    # Bank storage (US-009/010). "local" keeps the question_banks folder
    # authoritative, which is the pre-existing behaviour.
    "bank_store": "local",
    "bank_s3_bucket": None,
    "bank_s3_prefix": "question_banks/",
    "bank_s3_endpoint_url": None,
    "bank_s3_region": None,
    "bank_s3_access_key": None,
    "bank_s3_secret_key": None,
}


@pytest.mark.parametrize("field,expected", sorted(BASELINE_DEFAULTS.items()))
def test_default_is_unchanged(field: str, expected: object) -> None:
    config = InterviewerConfig.from_env({})
    assert getattr(config, field) == expected, (
        f"InterviewerConfig.{field} default drifted from {expected!r} to "
        f"{getattr(config, field)!r}. If intentional, update BASELINE_DEFAULTS "
        f"in the same commit — and check the Windows dev flow, which supplies "
        f"no environment."
    )


def test_no_default_field_is_uncovered() -> None:
    """Every field must appear above, so a newly added default cannot slip in
    unasserted. Fails loudly the moment a field is added without a decision
    about what the Windows flow should do with it."""
    covered = set(BASELINE_DEFAULTS)
    actual = set(InterviewerConfig.__dataclass_fields__)
    assert actual == covered, (
        f"uncovered defaults: {sorted(actual - covered)}; "
        f"stale entries: {sorted(covered - actual)}"
    )


def test_from_env_uses_os_environ_when_not_given(monkeypatch) -> None:
    """The explicit-empty-dict form above is only meaningful if the default
    path really does read the process environment."""
    monkeypatch.setenv("INTERVIEW_TOP_K", "9")
    assert InterviewerConfig.from_env().top_k == 9


@pytest.mark.parametrize(
    "env_key,field,value",
    [
        ("INTERVIEW_WEB_DIR", "web_dir", "/app/web"),
        ("INTERVIEW_BANK_DIR", "bank_dir", "/app/question_banks"),
    ],
)
def test_path_overrides_are_read(env_key: str, field: str, value: str) -> None:
    """The Wave 0 path keys must actually take effect — the container relies on
    them, and a silently-ignored override would reproduce exactly the failure
    they exist to prevent (the static mount finding nothing)."""
    config = InterviewerConfig.from_env({env_key: value})
    assert getattr(config, field) == value
