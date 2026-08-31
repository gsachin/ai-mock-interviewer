"""Speech-to-text engines behind the STTEngine protocol.

Deepgram (cloud, streaming-grade finals) and faster-whisper (self-hosted,
lazy import so the extra is optional). ``resolve_stt`` picks by provider —
unknown providers raise, mirroring the core's backend guards.
"""
import asyncio

import httpx

from interviewer.voice.protocols import STTEngine

_DEEPGRAM_LISTEN_URL = "https://api.deepgram.com/v1/listen"


class DeepgramSTT:
    """Deepgram Nova streaming STT — finals ~300 ms after utterance end."""

    def __init__(self, api_key: str, model: str = "nova-2-general",
                 *, transport: httpx.AsyncBaseTransport | None = None):
        self._api_key = api_key
        self._model = model
        self._transport = transport

    async def transcribe(self, audio_frame: bytes) -> str:
        async with httpx.AsyncClient(timeout=30.0, transport=self._transport) as client:
            resp = await client.post(
                _DEEPGRAM_LISTEN_URL,
                params={"model": self._model, "punctuate": "true"},
                headers={"Authorization": f"Token {self._api_key}",
                         "Content-Type": "audio/webm"},
                content=audio_frame,
            )
            resp.raise_for_status()
            data = resp.json()
        results = (data.get("results") or {}).get("channels") or []
        alternatives = results[0].get("alternatives") if results else []
        return (alternatives[0].get("transcript") or "" if alternatives else "")


class FasterWhisperSTT:
    """Local faster-whisper (CTranslate2). Model loads lazily on first use —
    the package runs without the extra until this engine is selected."""

    def __init__(self, model_size: str = "base", device: str = "auto"):
        self._model_size = model_size
        self._device = device
        self._model = None

    def _ensure_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(self._model_size, device=self._device)
        return self._model

    async def transcribe(self, audio_frame: bytes) -> str:
        model = await asyncio.to_thread(self._ensure_model)
        segments, _info = await asyncio.to_thread(
            model.transcribe, audio_frame, language="en")
        return " ".join(seg.text.strip() for seg in segments)


def resolve_stt(provider: str, config) -> STTEngine:
    provider = (provider or "").lower()
    if provider == "deepgram":
        if not config.deepgram_api_key:
            raise ValueError("INTERVIEW_DEEPGRAM_API_KEY is required for stt_provider=deepgram")
        return DeepgramSTT(config.deepgram_api_key)
    if provider in ("faster-whisper", "whisper"):
        return FasterWhisperSTT(config.whisper_model, config.whisper_device)
    if provider == "stub":
        from interviewer.voice.stubs import StubSTT

        return StubSTT()
    raise ValueError(f"unknown stt_provider: {provider!r} "
                     f"(deepgram | faster-whisper | stub)")
