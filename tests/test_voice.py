"""Phase 3 voice mode: sentence-level TTS through the brain, STT-backed
candidates, barge-in, provider policy, and 10 concurrent voice sessions
(stub engines — the live gate exercises the real services)."""
import asyncio

import pytest

from interviewer.brain import LLMInterviewer
from interviewer.config import InterviewerConfig
from interviewer.state_machine import InterviewerState, Session
from interviewer.voice.budget import LatencyBudgetTracker
from interviewer.voice.interviewer import AudioCandidate
from interviewer.voice.stt import resolve_stt
from interviewer.voice.tts import resolve_tts
from interviewer.voice.stubs import StubLLM, StubSTT
from tests.test_brain import ANSWERS, EVAL_Q1, EVAL_Q2, SPEAK_SCRIPT
from tests.test_interview import StubRag


def run(coro):
    return asyncio.run(coro)


class RecordingTTS:
    """Records every synthesized sentence; instant latency."""

    def __init__(self):
        self.sentences: list[str] = []

    async def synthesize(self, text: str) -> bytes:
        self.sentences.append(text)
        return b"audio:" + text.encode()


class SlowTTS:
    """Fixed 50 ms per sentence — enough to observe barge-in."""

    def __init__(self):
        self.calls = 0

    async def synthesize(self, text: str) -> bytes:
        self.calls += 1
        await asyncio.sleep(0.05)
        return b""


def _voice_brain(rag=None, tts=None, llm=None, budget=None):
    rag = rag or StubRag()
    llm = llm or StubLLM(list(SPEAK_SCRIPT), [EVAL_Q1, EVAL_Q2])
    return LLMInterviewer(rag, llm, Session(
        session_id="v1", tenant_id="default", domain="system-design"),
        tts=tts, budget=budget or LatencyBudgetTracker())


# ── sentence-level TTS through the brain ────────────────────────────────────

def test_voice_mode_synthesizes_sentences_and_records_budget():
    tts = RecordingTTS()
    budget = LatencyBudgetTracker()
    interviewer = _voice_brain(tts=tts, budget=budget)

    summary = run(interviewer.run("bank-sd", answers=ANSWERS))

    assert summary["state"] == InterviewerState.WRAP.value
    assert tts.sentences, "interviewer turns must reach TTS"
    # the stub LLM yields canned turns; TTS got them (sentence-accumulated)
    assert any("Design a rate limiter" in s for s in tts.sentences)
    # every hop carries the voice-stage timings
    for hop in summary["stats"]["hops"]:
        assert "tts_first_audio_ms" in hop and "stt_final_ms" in hop
    # the budget tracker saw the interviewer turns (greeting, q1, q2, wrap)
    agg = summary["stats"]["voice_budget"]
    assert agg and agg["turns"] >= 4
    assert summary["stats"]["voice_budget_bar"]


# ── STT-backed candidate ────────────────────────────────────────────────────

def test_audio_candidate_measures_transcription():
    stt = StubSTT()
    stt.feed("token bucket with redis counters")
    candidate = AudioCandidate(stt, {"s1": b"fake-opus-audio"})

    answer = run(candidate.answer("s1"))
    assert answer.text == "token bucket with redis counters"
    assert answer.stt_ms > 0
    # missing audio -> silent candidate, no crash
    assert run(candidate.answer("s2")).text == ""


def test_voice_mode_with_audio_candidate():
    stt = StubSTT()
    stt.feed(ANSWERS["s1"])
    stt.feed(ANSWERS["s1:followup"])
    stt.feed(ANSWERS["s2"])
    candidate = AudioCandidate(stt, {"s1": b"a", "s1:followup": b"a", "s2": b"a"})
    interviewer = _voice_brain(tts=RecordingTTS())

    summary = run(interviewer.run("bank-sd", candidate=candidate))
    assert summary["state"] == InterviewerState.WRAP.value
    # candidate turns carry the STT transcripts
    assert [t["role"] for t in summary["turns"]].count("candidate") == 2


# ── barge-in ────────────────────────────────────────────────────────────────

def test_barge_in_interrupts_mid_sentence():
    tts = SlowTTS()

    class SlowStreamLLM(StubLLM):
        async def respond_stream(self, messages):
            self.prompts.append(messages)
            line = self._script.pop(0) if self._script else ""
            if not line:
                return
            for word in line.split():
                yield word + " "
                await asyncio.sleep(0.02)   # slow token stream

    llm = SlowStreamLLM(list(SPEAK_SCRIPT), [EVAL_Q1, EVAL_Q2])
    interviewer = _voice_brain(tts=tts, llm=llm)

    async def interrupt_during_question():
        interviewer_task = asyncio.create_task(
            interviewer.run("bank-sd", answers=ANSWERS))
        await asyncio.sleep(0.3)            # mid-greeting/question stream
        await interviewer.interrupt()
        summary = await interviewer_task
        return summary

    summary = run(interrupt_during_question())
    # the interview survived the interrupt and completed
    assert summary["state"] == InterviewerState.WRAP.value
    assert summary["stats"]["questions_asked"] == 2


# ── provider policy ─────────────────────────────────────────────────────────

def test_tts_provider_policy_rejects_robotic_engines():
    cfg = InterviewerConfig(tts_provider="espeak")
    with pytest.raises(ValueError, match="natural-human-voice"):
        resolve_tts("espeak", cfg)
    with pytest.raises(ValueError, match="natural-human-voice"):
        resolve_tts("polly", cfg)           # unknown -> refused, not defaulted


def test_resolvers_require_credentials():
    cfg = InterviewerConfig()
    with pytest.raises(ValueError, match="INTERVIEW_CARTESIA_API_KEY"):
        resolve_tts("cartesia", cfg)
    with pytest.raises(ValueError, match="INTERVIEW_ELEVENLABS_API_KEY"):
        resolve_tts("elevenlabs", cfg)
    with pytest.raises(ValueError, match="INTERVIEW_DEEPGRAM_API_KEY"):
        resolve_stt("deepgram", cfg)
    with pytest.raises(ValueError, match="unknown stt_provider"):
        resolve_stt("whisper-api", cfg)
    # stub providers resolve without credentials (CI/dev path)
    assert isinstance(resolve_tts("stub", cfg).__class__.__name__, str)
    assert isinstance(resolve_stt("stub", cfg), StubSTT)


# ── 10 concurrent voice sessions (stub engines) ─────────────────────────────

def test_10_concurrent_voice_sessions():
    async def gather_all():
        return await asyncio.gather(*[one(i) for i in range(10)])

    async def one(i):
        interviewer = _voice_brain(tts=RecordingTTS())
        return await interviewer.run("bank-sd", answers=ANSWERS)

    summaries = run(gather_all())
    assert all(s["state"] == InterviewerState.WRAP.value for s in summaries)
    assert all(s["stats"]["voice_budget"]["turns"] >= 4 for s in summaries)
