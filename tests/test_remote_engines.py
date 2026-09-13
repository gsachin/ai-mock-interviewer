"""US-011 remote engine adapters: the wav wrap that feeds the transcription
endpoint, the two HTTP engines, and the client-lifetime invariant that makes
one engine safe to share across rooms.

No network, no GPU, no cluster — ``httpx.MockTransport`` stands in for the
engine services, so every assertion here is about the bytes on the wire and
the bytes handed back to the caller.
"""
import asyncio
import io
import json
import wave

import httpx
import numpy as np
import pytest

from interviewer.config import InterviewerConfig
from interviewer.voice.audio_format import s16le16k_to_wav
from interviewer.voice.stt import MAX_AUDIO_BYTES, RemoteWhisperSTT, resolve_stt
from interviewer.voice.tts import (
    NATURAL_VOICE_PROVIDERS,
    RemoteKokoroTTS,
    resolve_tts,
)

# 0.32 s of deterministic 16 kHz mono s16le — what agent.py's capture buffer
# holds (raw frames, no container).
PCM16K = bytes(range(256)) * 40

SAMPLES_24K = (np.sin(np.linspace(0.0, 20 * np.pi, 2400)) * 20000).astype("<i2")


def run(coro):
    return asyncio.run(coro)


def _wav_24k(samples: np.ndarray) -> bytes:
    """Kokoro's native output shape: 24 kHz mono 16-bit."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(samples.astype("<i2").tobytes())
    return buf.getvalue()


def _file_part(body: bytes, content_type: str) -> bytes:
    """Pull the ``file`` part back out of a multipart body the way a server
    would. Asserting against the raw body instead would pass even if the
    boundary or the part headers were malformed."""
    boundary = content_type.split("boundary=")[1].encode()
    for chunk in body.split(b"--" + boundary):
        if b'name="file"' in chunk:
            return chunk.partition(b"\r\n\r\n")[2].rsplit(b"\r\n", 1)[0]
    raise AssertionError("no `file` part in the multipart body")


# ── the wav wrap ────────────────────────────────────────────────────────────

def test_pcm_wraps_to_a_readable_16k_mono_wav():
    """AC-2: the endpoint wants a file; the capture buffer is containerless."""
    wav_bytes = s16le16k_to_wav(PCM16K)

    assert wav_bytes[:4] == b"RIFF" and wav_bytes[8:12] == b"WAVE"
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
        assert wav.getframerate() == 16000
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getnframes() * 2 == len(PCM16K)
        # not merely "a header was added" — the frames survive byte-for-byte
        assert wav.readframes(wav.getnframes()) == PCM16K


# ── RemoteWhisperSTT ────────────────────────────────────────────────────────

def test_remote_stt_uploads_a_wav_and_returns_the_transcript():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["content_type"] = request.headers.get("content-type", "")
        seen["body"] = request.content
        return httpx.Response(200, json={"text": "  token bucket with redis  "},
                              request=request)

    stt = RemoteWhisperSTT("http://stt.test/v1", "Systran/faster-whisper-base",
                           transport=httpx.MockTransport(handler))

    assert run(stt.transcribe(PCM16K)) == "token bucket with redis"
    assert seen["url"] == "http://stt.test/v1/audio/transcriptions"
    assert seen["content_type"].startswith("multipart/form-data; boundary=")
    # the part is a wav the far end can actually decode, at the buffer's rate
    with wave.open(io.BytesIO(_file_part(seen["body"], seen["content_type"])),
                   "rb") as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16000, 1, 2)
        assert wav.readframes(wav.getnframes()) == PCM16K
    assert b'name="model"' in seen["body"]
    assert b"Systran/faster-whisper-base" in seen["body"]


def test_remote_stt_clamps_a_runaway_buffer():
    """The worker buffers up to 120 s (MAX_ANSWER_SECONDS); the endpoint
    decodes everything it is handed, so the ceiling has to be engine-side."""
    oversized = bytes(MAX_AUDIO_BYTES + 64000)      # 62 s of silence
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["content_type"] = request.headers["content-type"]
        seen["body"] = request.content
        return httpx.Response(200, json={"text": ""}, request=request)

    stt = RemoteWhisperSTT("http://stt.test/v1", transport=httpx.MockTransport(handler))
    assert run(stt.transcribe(oversized)) == ""

    with wave.open(io.BytesIO(_file_part(seen["body"], seen["content_type"])),
                   "rb") as wav:
        assert wav.getnframes() * 2 == MAX_AUDIO_BYTES


def test_remote_stt_requires_a_base_url():
    """AC-5: the worker resolves engines before it registers with the SFU, so
    this raise is what turns a missing ConfigMap key into a failed start
    instead of a silent room."""
    with pytest.raises(ValueError, match="INTERVIEW_STT_BASE_URL"):
        resolve_stt("whisper-http", InterviewerConfig())
    with pytest.raises(ValueError, match="INTERVIEW_STT_BASE_URL"):
        RemoteWhisperSTT(None)


def test_remote_stt_normalizes_a_trailing_slash():
    """An operator writing http://stt:8000/v1/ must not produce a double
    slash — the ingress in front of Speaches routes on the exact path."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"text": "ok"}, request=request)

    stt = RemoteWhisperSTT("http://stt.test/v1/", transport=httpx.MockTransport(handler))
    assert run(stt.transcribe(PCM16K)) == "ok"
    assert seen["url"] == "http://stt.test/v1/audio/transcriptions"


def test_remote_stt_treats_a_non_json_200_as_an_empty_transcript():
    """A gateway answering 200 with an HTML body is not a crash: an empty
    transcript is a state the worker already handles (re-prompt the
    candidate)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>gateway</html>", request=request)

    stt = RemoteWhisperSTT("http://stt.test/v1", transport=httpx.MockTransport(handler))
    assert run(stt.transcribe(PCM16K)) == ""


# ── RemoteKokoroTTS ─────────────────────────────────────────────────────────

def test_remote_kokoro_returns_48k_mono_s16le():
    """AC-3 — the test that matters: Kokoro is native 24 kHz and the TTSEngine
    contract is 48 kHz, so the sentence must come back at twice the rate."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=_wav_24k(SAMPLES_24K), request=request)

    tts = RemoteKokoroTTS("http://tts.test/v1", "af_heart",
                          transport=httpx.MockTransport(handler))
    out = run(tts.synthesize("Tell me about rate limiting."))

    assert seen["url"] == "http://tts.test/v1/audio/speech"
    assert seen["body"] == {
        "model": "kokoro",
        "input": "Tell me about rate limiting.",
        "voice": "af_heart",
        "response_format": "wav",
    }
    # Raw PCM carries no header, so the rate can only be checked as duration:
    # the same sentence at 48 kHz is exactly twice the samples of 24 kHz.
    out_samples = np.frombuffer(out, dtype="<i2")
    assert len(out_samples) == 2 * len(SAMPLES_24K)
    assert len(out) == 2 * len(out_samples)     # s16le = 2 bytes per frame
    assert np.abs(out_samples).max() > 1000     # a real signal, not silence


def test_remote_tts_requires_a_base_url():
    with pytest.raises(ValueError, match="INTERVIEW_TTS_BASE_URL"):
        resolve_tts("kokoro-http", InterviewerConfig())
    with pytest.raises(ValueError, match="INTERVIEW_TTS_BASE_URL"):
        RemoteKokoroTTS(None)


# ── resolution and the natural-voice policy ─────────────────────────────────

def test_resolvers_build_the_remote_engines():
    cfg = InterviewerConfig(stt_base_url="http://stt:8000/v1",
                            tts_base_url="http://tts:8880/v1")

    assert isinstance(resolve_stt("whisper-http", cfg), RemoteWhisperSTT)
    tts = resolve_tts("kokoro-http", cfg)
    assert isinstance(tts, RemoteKokoroTTS)
    # AC-4: the same Kokoro model the app ships locally, so the voice identity
    # carries over — and tts_voice_id still wins when an operator pins one.
    assert tts._voice == "af_heart"
    assert resolve_tts("kokoro-http", InterviewerConfig(
        tts_base_url="http://tts:8880/v1", tts_voice_id="am_michael"))._voice == "am_michael"


def test_kokoro_http_is_allowlisted_but_robotic_engines_are_still_refused():
    assert "kokoro-http" in NATURAL_VOICE_PROVIDERS

    cfg = InterviewerConfig(tts_base_url="http://tts:8880/v1")
    with pytest.raises(ValueError, match="natural-human-voice"):
        resolve_tts("espeak", cfg)
    with pytest.raises(ValueError, match="natural-human-voice"):
        resolve_tts("kokoro-lite", cfg)     # unknown -> refused, not defaulted


def test_remote_base_urls_are_read_from_env():
    """The ConfigMap and docker-compose set these names; the resolve step
    depends on them arriving intact."""
    cfg = InterviewerConfig.from_env({
        "INTERVIEW_STT_BASE_URL": "http://whisper:8000/v1",
        "INTERVIEW_TTS_BASE_URL": "http://kokoro:8880/v1",
    })
    assert cfg.stt_base_url == "http://whisper:8000/v1"
    assert cfg.tts_base_url == "http://kokoro:8880/v1"


# ── engine lifetime (AC-7: one engine, many rooms) ──────────────────────────

def test_remote_stt_client_is_built_lazily_and_reused():
    """A per-call client pays a fresh TCP handshake for every answer — the
    exact cost the pooling exists to remove."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"text": "hi"}, request=request)

    stt = RemoteWhisperSTT("http://stt.test/v1", transport=httpx.MockTransport(handler))
    assert stt._client is None, "the client must not be built at import time"

    async def two_answers():
        await stt.transcribe(PCM16K)
        first = stt._client
        await stt.transcribe(PCM16K)
        second = stt._client
        await stt.aclose()
        return first, second

    first, second = run(two_answers())
    assert len(calls) == 2
    assert first is not None and second is first, "a second client was created"
    assert stt._client is None, "aclose must release the pool"


def test_remote_tts_client_is_built_lazily_and_reused():
    calls = []
    fixture = _wav_24k(SAMPLES_24K)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=fixture, request=request)

    tts = RemoteKokoroTTS("http://tts.test/v1", transport=httpx.MockTransport(handler))
    assert tts._client is None

    async def two_sentences():
        await tts.synthesize("one")
        first = tts._client
        await tts.synthesize("two")
        second = tts._client
        await tts.aclose()
        return first, second

    first, second = run(two_sentences())
    assert len(calls) == 2
    assert first is not None and second is first, "a second client was created"
    assert tts._client is None
