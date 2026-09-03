"""Text-to-speech engines behind the TTSEngine protocol.

Natural-human-voice policy (hard requirement): only allowlisted providers
may be resolved — robotic espeak-class engines never ship. Preference
order: elevenlabs, cartesia (cloud), kokoro, piper (self-hosted).
"""
import asyncio
import os
import subprocess

import httpx

from interviewer.voice.audio_format import (
    f32le_to_s16le48k,
    mp3_to_s16le48k,
    wav_to_s16le48k,
)
from interviewer.voice.protocols import TTSEngine

NATURAL_VOICE_PROVIDERS = {"elevenlabs", "cartesia", "kokoro", "piper", "stub"}

_CARTESIA_TTS_URL = "https://api.cartesia.ai/tts/bytes"
_ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"


class CartesiaTTS:
    """Cartesia Sonic — natural voice presets with the lowest first-audio
    latency of the cloud pair."""

    def __init__(self, api_key: str, voice_id: str,
                 model_id: str = "sonic-2",
                 *, transport: httpx.AsyncBaseTransport | None = None):
        self._api_key = api_key
        self._voice_id = voice_id
        self._model_id = model_id
        self._transport = transport

    async def synthesize(self, text: str) -> bytes:
        async with httpx.AsyncClient(timeout=30.0, transport=self._transport) as client:
            resp = await client.post(
                _CARTESIA_TTS_URL,
                headers={"X-API-Key": self._api_key,
                         "Content-Type": "application/json"},
                json={
                    "model_id": self._model_id,
                    "voice": {"mode": "id", "id": self._voice_id},
                    "transcript": text,
                    "output_format": {"container": "raw", "encoding": "pcm_f32le",
                                      "sample_rate": 24000},
                },
            )
            resp.raise_for_status()
            return f32le_to_s16le48k(resp.content, 24000)


class ElevenLabsTTS:
    """ElevenLabs — best naturalness (Flash v2.5 for latency)."""

    def __init__(self, api_key: str, voice_id: str,
                 model_id: str = "eleven_flash_v2_5",
                 *, transport: httpx.AsyncBaseTransport | None = None):
        self._api_key = api_key
        self._voice_id = voice_id
        self._model_id = model_id
        self._transport = transport

    async def synthesize(self, text: str) -> bytes:
        async with httpx.AsyncClient(timeout=30.0, transport=self._transport) as client:
            resp = await client.post(
                _ELEVENLABS_TTS_URL.format(voice_id=self._voice_id),
                params={"output_format": "mp3_44100_128"},
                headers={"xi-api-key": self._api_key,
                         "Content-Type": "application/json"},
                json={"text": text, "model_id": self._model_id},
            )
            resp.raise_for_status()
            return mp3_to_s16le48k(resp.content)


class PiperTTS:
    """Local Piper (CPU) — self-hosted fallback; quality is acceptable but
    not the production choice (that is Cartesia/ElevenLabs)."""

    def __init__(self, binary: str = "piper", model_path: str | None = None):
        self._binary = binary
        self._model_path = model_path

    async def synthesize(self, text: str) -> bytes:
        if not self._model_path:
            raise ValueError("INTERVIEW_PIPER_MODEL is required for tts_provider=piper")
        cmd = [self._binary, "--model", self._model_path, "--output_file", "-"]

        def _run() -> bytes:
            proc = subprocess.run(cmd, input=text.encode(), capture_output=True,
                                  check=True)
            return proc.stdout

        wav = await asyncio.to_thread(_run)
        return wav_to_s16le48k(wav)


class KokoroTTS:
    """Self-hosted Kokoro via ``kokoro-onnx`` — no torch/espeak-ng needed.

    Model + voice files (~110 MB total) download once into the cache dir on
    first synthesis. Emits 48 kHz mono s16le per the format contract."""

    MODEL_URL = ("https://github.com/thewh1teagle/kokoro-onnx/releases/"
                 "download/model-files-v1.1/kokoro-v1.0.onnx")
    VOICES_URL = ("https://github.com/thewh1teagle/kokoro-onnx/releases/"
                  "download/model-files-v1.1/voices-v1.0.bin")

    def __init__(self, voice: str, model_dir: str | None = None):
        self._voice = voice
        self._model_dir = model_dir or os.path.join(
            os.path.expanduser("~"), ".cache", "mock-interviewer", "kokoro")
        self._kokoro = None

    def _ensure_engine(self):
        if self._kokoro is None:
            from kokoro_onnx import Kokoro  # lazy: the [voice] extra

            model_path = self._download(self.MODEL_URL, "kokoro-v1.0.onnx")
            voices_path = self._download(self.VOICES_URL, "voices-v1.0.bin")
            self._kokoro = Kokoro(model_path, voices_path)
        return self._kokoro

    def _download(self, url: str, filename: str) -> str:
        os.makedirs(self._model_dir, exist_ok=True)
        path = os.path.join(self._model_dir, filename)
        if os.path.exists(path):
            return path
        print(f"[kokoro] downloading {filename} -> {path}")
        with httpx.Client(follow_redirects=True, timeout=600.0) as client:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                with open(path, "wb") as f:
                    for chunk in resp.iter_bytes():
                        f.write(chunk)
        return path

    async def synthesize(self, text: str) -> bytes:
        kokoro = await asyncio.to_thread(self._ensure_engine)

        def _synth():
            samples, sample_rate = kokoro.create(
                text, voice=self._voice, speed=1.0, lang="en-us")
            return samples, sample_rate

        samples, sample_rate = await asyncio.to_thread(_synth)
        return f32le_to_s16le48k(samples.astype("<f4").tobytes(), sample_rate)


def resolve_tts(provider: str, config) -> TTSEngine:
    provider = (provider or "").lower()
    if provider not in NATURAL_VOICE_PROVIDERS:
        raise ValueError(
            f"tts_provider {provider!r} is not allowed — natural-human-voice "
            f"policy permits only: {sorted(NATURAL_VOICE_PROVIDERS)}"
        )
    if provider == "cartesia":
        if not config.cartesia_api_key:
            raise ValueError("INTERVIEW_CARTESIA_API_KEY is required for tts_provider=cartesia")
        return CartesiaTTS(config.cartesia_api_key,
                           config.tts_voice_id or "a0e99841-438c-4a64-b679-ae501e7d6091")
    if provider == "elevenlabs":
        if not config.elevenlabs_api_key:
            raise ValueError("INTERVIEW_ELEVENLABS_API_KEY is required for tts_provider=elevenlabs")
        return ElevenLabsTTS(config.elevenlabs_api_key,
                             config.tts_voice_id or "21m00Tcm4TlvDq8ikWAM")
    if provider == "piper":
        return PiperTTS(config.piper_binary, config.piper_model)
    if provider == "kokoro":
        return KokoroTTS(config.kokoro_voice, config.kokoro_model_dir)
    from interviewer.voice.stubs import StubTTS

    return StubTTS()
