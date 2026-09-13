"""Voice mode wiring: an STT-backed Candidate and the factory that turns an
InterviewerConfig into a voice-enabled LLMInterviewer."""
import time
from typing import Any

from interviewer.brain import CandidateAnswer, LLMInterviewer
from interviewer.config import InterviewerConfig
from interviewer.llm import LLMConfig, OpenAICompatibleLLM
from interviewer.voice.budget import LatencyBudgetTracker
from interviewer.voice.tts import resolve_tts


class AudioCandidate:
    """Candidate answers arrive as audio bytes, transcribed per turn. The
    transcription latency is measured and reported through CandidateAnswer —
    the voice budget needs it."""

    def __init__(self, stt, audio: dict[str, bytes] | None = None):
        self._stt = stt
        self._audio = dict(audio or {})

    async def answer(self, question_id: str,
                     timeout_s: float | None = None,
                     drop_before_ts: float | None = None) -> CandidateAnswer:
        audio = self._audio.pop(question_id, b"")
        if not audio:
            return CandidateAnswer(text="")
        t0 = time.perf_counter()
        text = await self._stt.transcribe(audio)
        return CandidateAnswer(text=text, stt_ms=(time.perf_counter() - t0) * 1000)


def build_voice_interviewer(config: InterviewerConfig, rag: Any,
                            session: Any, sink: Any = None,
                            on_event: Any = None,
                            decider: Any = None, *,
                            tts: Any = None) -> LLMInterviewer:
    """Voice-enabled brain: STT/TTS engines from config, a fast voice LLM
    for the hot path (falls back to the judge LLM), a fresh latency budget
    tracker, and an optional playback sink (the LiveKit room). The judge LLM
    stays as configured (slow is fine — its wait is measured and reported as
    judge_wait_ms).

    RCA session shape: ``config.max_questions`` (INTERVIEW_MAX_QUESTIONS,
    default 3) sizes the session; ``config.answer_timeout_s`` bounds every
    answer wait; ``config.judge_model`` overrides the judge model on the same
    base URL (INTERVIEW_JUDGE_MODEL).
    """
    # NO stt here, deliberately. This function used to call resolve_stt and
    # discard the result: LLMInterviewer.__init__ takes `tts`, `voice_llm`,
    # `budget`, `sink`, `on_event` and `decider` -- and no `stt`. So every
    # interview built an STT engine and threw it away, which on the remote
    # engines would mean a wasted connection pool per room. The live STT
    # belongs to voice/agent.py, which is the only thing that transcribes.
    #
    # `tts` may be injected so the worker can build engines once per process;
    # falling back to resolving here keeps the dev/CLI paths working unchanged.
    if tts is None:
        tts = resolve_tts(config.tts_provider, config)
    voice_llm = None
    if config.voice_llm_base_url or config.voice_llm_model:
        voice_llm = OpenAICompatibleLLM(LLMConfig(
            base_url=config.voice_llm_base_url or config.llm_base_url,
            model=config.voice_llm_model,
            token=config.llm_token,
            # The hot path gets its own short timeout; the judge below keeps
            # the 300 s default because its wait is off the critical path.
            timeout=config.voice_llm_timeout_s,
        ))
    return LLMInterviewer(
        rag, OpenAICompatibleLLM(LLMConfig(
            base_url=config.llm_base_url,
            model=config.judge_model or config.llm_model,
            token=config.llm_token)),
        session,
        max_questions=config.max_questions,
        answer_timeout_s=config.answer_timeout_s,
        tts=tts,
        voice_llm=voice_llm,
        budget=LatencyBudgetTracker(),
        sink=sink,
        on_event=on_event,
        decider=decider,
    )
