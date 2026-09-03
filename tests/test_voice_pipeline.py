"""Phase 3 unit tests: the LiveKit-facing plumbing — token endpoint,
event-driven candidate, playback sink (brain + LiveKit sink), audio format
normalization, and the self-hosted engine resolution (kokoro / whisper)."""
import asyncio
import io
import wave

import numpy as np
import pytest
from fastapi.testclient import TestClient

from interviewer.config import InterviewerConfig
from interviewer.state_machine import Session
from interviewer.voice.audio_format import (
    f32le_to_s16le48k,
    s16le_to_s16le48k,
    wav_to_s16le48k,
)
from interviewer.voice.interviewer import AudioCandidate
from interviewer.voice.livekit import EchoGate, LiveKitAudioSink, LiveKitCandidate
from interviewer.voice.stt import resolve_stt
from interviewer.voice.tts import resolve_tts

LIVEKIT_ENV = {"LIVEKIT_API_KEY": "devkey", "LIVEKIT_API_SECRET": "secret"}


def run(coro):
    return asyncio.run(coro)


# ── /voice/token ─────────────────────────────────────────────────────────────

def test_voice_token_endpoint_issues_jwt():
    from interviewer import server

    server.config = InterviewerConfig.from_env(LIVEKIT_ENV)
    client = TestClient(server.app)
    resp = client.post("/voice/token", json={"domain": "ios"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["livekit_url"].startswith("http")
    assert body["room"].startswith("interview-ios-")
    assert body["session_id"] and body["domain"] == "ios"
    assert body["token"].count(".") == 2  # JWT shape


def test_voice_token_endpoint_requires_keys():
    from interviewer import server

    server.config = InterviewerConfig.from_env({})  # no livekit keys
    client = TestClient(server.app)
    resp = client.post("/voice/token", json={})
    assert resp.status_code == 503
    assert "LIVEKIT_API_KEY" in resp.json()["detail"]


# ── LiveKitCandidate ─────────────────────────────────────────────────────────

def test_livekit_candidate_answers_from_pushed_transcript():
    candidate = LiveKitCandidate()

    async def scenario():
        task = asyncio.create_task(candidate.answer("s1"))
        await asyncio.sleep(0)          # answer() starts listening
        candidate.push("", 0.0)         # empty transcripts are dropped
        candidate.push("token bucket with redis counters", 300.0)
        return await task

    answer = run(scenario())
    assert answer.text == "token bucket with redis counters"
    assert answer.stt_ms == 300.0


def test_livekit_candidate_drops_stale_interjections():
    """Transcripts that arrived before the brain started listening (barge-in
    leftovers) must not answer the next question."""
    candidate = LiveKitCandidate()
    candidate.push("sorry, go on", 100.0)  # arrived while interviewer spoke

    async def scenario():
        task = asyncio.create_task(candidate.answer("s1"))
        await asyncio.sleep(0)          # answer() clears the stale item
        candidate.push("the real answer", 200.0)  # this turn's speech
        return await task

    assert run(scenario()).text == "the real answer"


# ── LiveKitAudioSink (fake rtc seam) ─────────────────────────────────────────

class FakeRTC:
    class AudioFrame:
        def __init__(self, data, sample_rate, num_channels, samples_per_channel):
            self.data, self.sample_rate = data, sample_rate
            self.num_channels = num_channels
            self.samples_per_channel = samples_per_channel


class FakeSource:
    def __init__(self):
        self.frames = []

    async def capture_frame(self, frame):
        self.frames.append(frame)
        await asyncio.sleep(0.02)  # real sources pace playout to real time


def test_livekit_sink_plays_20ms_frames():
    source = FakeSource()
    sink = LiveKitAudioSink(source, rtc=FakeRTC)

    async def scenario():
        await sink.play(b"\x01\x00" * 4800)  # 0.1 s of 48k mono s16le
        while sink._task is not None and not sink._task.done():
            await asyncio.sleep(0.01)
        await sink.play(b"\x02\x00" * 4800)  # a second sentence
        while sink._task is not None and not sink._task.done():
            await asyncio.sleep(0.01)

    run(scenario())
    assert len(source.frames) == 10  # 5 x 20 ms frames per sentence
    assert source.frames[0].samples_per_channel == 960
    assert source.frames[0].data.startswith(b"\x01\x00")
    assert source.frames[-1].data.startswith(b"\x02\x00")


def test_livekit_sink_interrupt_drops_queued_audio():
    source = FakeSource()
    sink = LiveKitAudioSink(source, rtc=FakeRTC)

    async def scenario():
        await sink.play(b"\x01\x00" * 4800)
        await sink.play(b"\x02\x00" * 4800)   # queued behind the first
        await asyncio.sleep(0.03)             # first frames go out
        await sink.interrupt()                # barge-in
        while sink._task is not None and not sink._task.done():
            await asyncio.sleep(0.01)

    run(scenario())
    assert len(source.frames) < 5, "playback must stop mid-sentence"
    assert all(f.data.startswith(b"\x01\x00") for f in source.frames)


# ── audio format normalization ───────────────────────────────────────────────

def test_f32le_24k_to_s16le_48k():
    src = np.sin(np.linspace(0, 20 * np.pi, 24000)).astype("<f4").tobytes()
    out = f32le_to_s16le48k(src, 24000)
    assert len(out) == 48000 * 2          # 1 s at 48k s16le mono
    samples = np.frombuffer(out, dtype="<i2")
    assert -32768 <= samples.min() and samples.max() <= 32767


def test_s16le_stereo_44k_to_mono_48k():
    src = (np.arange(44100, dtype="<i2") % 30000).astype("<i2")
    stereo = np.column_stack([src, src]).tobytes()
    out = s16le_to_s16le48k(stereo, 44100, num_channels=2)
    assert len(out) == 48000 * 2


def test_wav_16k_to_s16le_48k():
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000)
    out = wav_to_s16le48k(buf.getvalue())
    assert len(out) == 48000 * 2
    assert np.frombuffer(out, dtype="<i2").max() == 0  # silence stays silence


# ── self-hosted engine resolution ────────────────────────────────────────────

def test_kokoro_resolves_without_credentials():
    cfg = InterviewerConfig(tts_provider="kokoro")
    engine = resolve_tts("kokoro", cfg)
    assert type(engine).__name__ == "KokoroTTS"


def test_faster_whisper_resolves_without_credentials():
    cfg = InterviewerConfig(stt_provider="faster-whisper")
    engine = resolve_stt("faster-whisper", cfg)
    assert type(engine).__name__ == "FasterWhisperSTT"


def test_worker_refuses_stub_engines(monkeypatch):
    """The worker's fail-fast rule: stub engines never ship to a room."""
    from interviewer.voice import worker

    monkeypatch.setattr(
        worker.InterviewerConfig, "from_env",
        classmethod(lambda cls, environ=None: InterviewerConfig(
            stt_provider="stub", tts_provider="stub")))
    with pytest.raises(SystemExit, match="real engines"):
        worker.main()


# ── RCA fix regression tests (2026-09-03) ─────────────────────────────────────
# T2: bounded candidate answer. T6: EchoGate decisions + sink playback state.
# T3: worker page-event filter. T1/T7: session-shape wiring of the voice
# factory (max_questions / answer_timeout / judge model).

def test_livekit_candidate_answer_times_out():
    """T2: answer() is bounded — a candidate whose mic never delivers speech
    raises TimeoutError instead of blocking the brain forever."""
    candidate = LiveKitCandidate()
    with pytest.raises(TimeoutError):
        run(candidate.answer("s1", timeout_s=0.05))


# ── EchoGate (T6) ────────────────────────────────────────────────────────────

def test_echo_gate_barges_in_only_when_not_playing():
    gate = EchoGate()
    assert gate.on_speech_start(1.0, playing=False) is True   # genuine answer
    assert gate.on_speech_start(1.0, playing=True) is False   # no self-barge


def test_echo_gate_drops_utterance_that_ends_right_after_playback_stop():
    """The interviewer's own voice picked up by the mic: speech started
    during playback and the utterance ends within the echo tail."""
    gate = EchoGate(tail_ms=250.0)
    gate.on_speech_start(t := 10.0, playing=True)
    # playback stops at t+0.1 s; the VAD utterance ends 0.1 s later -> drop
    assert gate.on_speech_end(t + 0.2, playing=False, stop_ago_ms=100.0)


def test_echo_gate_keeps_speech_ending_long_after_playback_stopped():
    gate = EchoGate(tail_ms=250.0)
    gate.on_speech_start(t := 10.0, playing=True)
    # the interviewer stopped, then the candidate kept going — genuine answer
    assert not gate.on_speech_end(t + 5.0, playing=False, stop_ago_ms=4900.0)


def test_echo_gate_keeps_genuine_overlap_while_still_playing():
    gate = EchoGate(tail_ms=250.0)
    gate.on_speech_start(10.0, playing=True)
    # candidate talks over the interviewer; AEC keeps the mic open — keep it
    assert not gate.on_speech_end(10.8, playing=True, stop_ago_ms=0.0)


def test_echo_gate_keeps_fast_reply_started_after_playback():
    gate = EchoGate(tail_ms=250.0)
    gate.on_speech_start(10.0, playing=False)
    assert not gate.on_speech_end(10.3, playing=False, stop_ago_ms=50.0)


def test_echo_gate_resets_between_utterances():
    gate = EchoGate(tail_ms=250.0)
    gate.on_speech_start(10.0, playing=True)
    gate.on_speech_end(10.1, playing=False, stop_ago_ms=100.0)   # dropped
    # a new utterance that starts while NOT playing is a fresh answer
    gate.on_speech_start(11.0, playing=False)
    assert not gate.on_speech_end(11.2, playing=False, stop_ago_ms=0.0)


# ── sink playback state (feeds the gate) ─────────────────────────────────────

def test_livekit_sink_exposes_playing_and_last_stop():
    source = FakeSource()
    sink = LiveKitAudioSink(source, rtc=FakeRTC)

    async def scenario():
        await sink.play(b"\x01\x00" * 4800)          # 0.1 s sentence
        await asyncio.sleep(0.02)                     # drain started
        assert sink.playing is True
        while sink._task is not None and not sink._task.done():
            await asyncio.sleep(0.01)
        assert sink.playing is False
        assert sink.last_stop_ts is not None          # natural sentence end
        await sink.play(b"\x02\x00" * 4800)
        await asyncio.sleep(0.02)
        await sink.interrupt()                        # barge-in
        assert sink.last_stop_ts is not None

    run(scenario())


# ── worker page-event filter (T3) ────────────────────────────────────────────

def test_worker_page_event_filter_drops_candidate_turns():
    """candidate_heard (STT-first echo) supersedes the brain's candidate-role
    turns — both must never reach the page or the transcript duplicates."""
    from interviewer.voice import agent

    assert agent.is_page_event({"type": "turn", "role": "interviewer",
                                "text": "hi", "stage": "greeting"})
    assert agent.is_page_event({"type": "candidate_heard", "text": "answer"})
    assert agent.is_page_event({"type": "state", "phase": "listening",
                                "label": "x"})
    assert agent.is_page_event({"type": "score", "score": {}})
    assert not agent.is_page_event({"type": "turn", "role": "candidate",
                                    "text": "answer"})


# ── voice factory session-shape wiring (T1/T7) ───────────────────────────────

def test_voice_factory_threads_session_config(monkeypatch):
    """INTERVIEW_MAX_QUESTIONS / INTERVIEW_ANSWER_TIMEOUT_S /
    INTERVIEW_JUDGE_MODEL land on the brain and the judge LLM."""
    from interviewer.voice import interviewer as vi
    from interviewer.voice.stubs import StubSTT, StubTTS

    captured: dict = {}

    class FakeLLM:
        def __init__(self, cfg):
            captured["cfg"] = cfg

        metrics = type("M", (), {"first_token_ms": 0.0, "total_ms": 0.0})()

    monkeypatch.setattr(vi, "OpenAICompatibleLLM", FakeLLM)
    monkeypatch.setattr(vi, "resolve_stt", lambda *a: StubSTT())
    monkeypatch.setattr(vi, "resolve_tts", lambda *a: StubTTS())

    cfg = InterviewerConfig(
        stt_provider="stub", tts_provider="stub",
        llm_base_url="http://127.0.0.1:8000/v1", llm_model="qwen2.5:14b",
        max_questions=3, answer_timeout_s=12.5, judge_model="qwen2.5:3b")
    iv = vi.build_voice_interviewer(
        cfg, rag=object(),
        session=Session(session_id="x", tenant_id="t", domain="ios"),
        on_event=lambda e: None)

    assert iv._max_questions == 3
    assert iv._answer_timeout_s == 12.5
    assert captured["cfg"].model == "qwen2.5:3b"   # judge override wins
