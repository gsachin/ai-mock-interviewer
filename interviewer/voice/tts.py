"""Text-to-speech engines behind the TTSEngine protocol.

Natural-human-voice policy (hard requirement): only allowlisted providers
may be resolved — robotic espeak-class engines never ship. Preference
order: elevenlabs, cartesia (cloud), kokoro, piper (self-hosted).
"""
import asyncio
import subprocess

import httpx

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
            return resp.content


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
            return resp.content


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

        return await asyncio.to_thread(_run)


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
        raise ValueError("tts_provider=kokoro is allowlisted but not yet wired — "
                         "use piper for self-hosted audio today")
    from interviewer.voice.stubs import StubTTS

    return StubTTS()
