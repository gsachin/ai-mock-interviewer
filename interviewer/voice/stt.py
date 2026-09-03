"""Speech-to-text engines behind the STTEngine protocol.

Deepgram (cloud, streaming-grade finals) and faster-whisper (self-hosted,
lazy import so the extra is optional). ``resolve_stt`` picks by provider —
unknown providers raise, mirroring the core's backend guards.
"""
import asyncio

import httpx
import numpy as np

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
    the package runs without the extra until this engine is selected.

    ``device="auto"`` tries CUDA first and falls back to CPU+int8 once if the
    CUDA runtime (cublas/cudnn) is missing — common on Windows GPU machines.
    """

    def __init__(self, model_size: str = "base", device: str = "auto"):
        self._model_size = model_size
        self._device = device
        self._model = None

    def _build(self, device: str):
        from faster_whisper import WhisperModel

        kwargs = {"device": device}
        if device == "cpu":
            kwargs["compute_type"] = "int8"  # ~2x faster finals on CPU
        return WhisperModel(self._model_size, **kwargs)

    def _ensure_model(self):
        if self._model is None:
            self._model = self._build(self._device)
        return self._model

    async def _transcribe_once(self, model, audio: np.ndarray) -> str:
        segments, _info = await asyncio.to_thread(
            model.transcribe, audio, language="en", beam_size=1,
            condition_on_previous_text=False)
        return " ".join(seg.text.strip() for seg in segments)

    async def transcribe(self, audio_frame: bytes) -> str:
        """``audio_frame`` is 16 kHz mono s16le PCM (the VAD speech buffer).
        Converted to float32 for faster-whisper; beam_size=1 keeps finals
        fast on CPU."""
        model = await asyncio.to_thread(self._ensure_model)
        audio = (np.frombuffer(audio_frame, dtype=np.int16)
                 .astype(np.float32) / 32768.0)
        try:
            return await self._transcribe_once(model, audio)
        except RuntimeError as exc:
            if self._device != "auto":
                raise
            # CUDA runtime not loadable (e.g. cublas64_12.dll missing) —
            # rebuild once on CPU and remember the choice.
            print(f"[stt] CUDA unavailable ({exc}) — falling back to CPU int8")
            self._device = "cpu"
            self._model = None
            model = await asyncio.to_thread(self._ensure_model)
            return await self._transcribe_once(model, audio)


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
