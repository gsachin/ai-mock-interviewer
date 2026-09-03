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
from interviewer.voice.audio_format import (
    f32le_to_s16le48k,
    s16le_to_s16le48k,
    wav_to_s16le48k,
)
from interviewer.voice.interviewer import AudioCandidate
from interviewer.voice.livekit import LiveKitAudioSink, LiveKitCandidate
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
