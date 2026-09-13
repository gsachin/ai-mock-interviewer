"""Speech-to-text engines behind the STTEngine protocol.

Deepgram (cloud, streaming-grade finals) and faster-whisper (self-hosted,
lazy import so the extra is optional). ``resolve_stt`` picks by provider —
unknown providers raise, mirroring the core's backend guards.
"""
import asyncio
import logging
import time

import httpx
import numpy as np

from interviewer.voice.audio_format import s16le16k_to_wav
from interviewer.voice.protocols import STTEngine

log = logging.getLogger(__name__)

# Faster-whisper decodes the whole input; CPU int8 runs ~0.15-0.25x real
# time on quiet buffers (measured 35 s for 150 s — RCA 2026-09-03). The
# worker trims answers first; this is the engine-side backstop.
MAX_AUDIO_BYTES = 60 * 16000 * 2      # 16 kHz mono s16le, 60 s

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

    def _ensure_model(self, device: str | None = None) -> None:
        if self._model is None:
            self._model = self._build(device or self._device)

    def _fallback_to_cpu(self, exc: Exception) -> None:
        """CUDA runtime not loadable (e.g. cublas64_12.dll missing) —
        rebuild once on CPU and remember the choice. Covers model BUILD
        failures too, not only inference failures."""
        if self._device == "cpu":
            raise exc
        log.warning("CUDA unavailable (%s) — falling back to CPU int8", exc)
        self._device = "cpu"
        self._model = None

    async def _transcribe_once(self, model, audio: np.ndarray) -> str:
        segments, _info = await asyncio.to_thread(
            model.transcribe, audio, language="en", beam_size=1,
            condition_on_previous_text=False)
        return " ".join(seg.text.strip() for seg in segments)

    async def transcribe(self, audio_frame: bytes) -> str:
        """``audio_frame`` is 16 kHz mono s16le PCM (the speech buffer).
        Converted to float32 for faster-whisper; beam_size=1 keeps finals
        fast on CPU. Input is clamped to the most recent 60 s — whisper
        decodes the entire buffer, so a long quiet tail would otherwise
        cost minutes of CPU (RCA 2026-09-03)."""
        # keep the tail: the candidate speaks right before clicking Finish
        if len(audio_frame) > MAX_AUDIO_BYTES:
            log.info("faster-whisper input clamped: %.1f s -> 60 s",
                     len(audio_frame) / 32000.0)
            audio_frame = audio_frame[-MAX_AUDIO_BYTES:]
        t0 = time.perf_counter()
        audio = (np.frombuffer(audio_frame, dtype=np.int16)
                 .astype(np.float32) / 32768.0)
        try:
            await asyncio.to_thread(self._ensure_model)
            text = await self._transcribe_once(self._model, audio)
        except (RuntimeError, OSError) as exc:
            self._fallback_to_cpu(exc)
            await asyncio.to_thread(self._ensure_model)
            text = await self._transcribe_once(self._model, audio)
        elapsed = time.perf_counter() - t0
        if elapsed > 3.0:   # slow decodes are worth knowing about
            log.info("faster-whisper decode took %.1f s for %.1f s of audio",
                     elapsed, len(audio_frame) / 32000.0)
        return text


class RemoteWhisperSTT:
    """Whisper behind an OpenAI-compatible transcription endpoint (Speaches —
    the maintained successor to faster-whisper-server).

    The model lives in its own service, so the worker stays a small CPU-only
    process with no ctranslate2 and no weights: an answer costs one HTTP call
    instead of a 1.0 s CPU decode, and the worker becomes I/O-bound, which is
    what makes CPU-load dispatch across replicas meaningful at all.
    """

    def __init__(self, base_url: str, model: str = "base",
                 *, timeout: float = 30.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        if not base_url:
            raise ValueError(
                "INTERVIEW_STT_BASE_URL is required for stt_provider=whisper-http")
        # a trailing slash would produce ".../v1//audio/transcriptions"
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout
        self._transport = transport
        # Bounded and small on purpose: two rooms per worker at most, so a
        # 100-connection default pool would hide a runaway from the operator.
        self._limits = httpx.Limits(max_connections=8, max_keepalive_connections=4)
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        """One client per engine, built lazily (the module must import with no
        network). Building it per call would pay a fresh TCP handshake for
        every answer, which is exactly the cost the pooling exists to remove.
        """
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                limits=self._limits,
                transport=self._transport,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def transcribe(self, audio_frame: bytes) -> str:
        """``audio_frame`` is raw 16 kHz mono s16le PCM (the speech buffer),
        wrapped into a wav container because the endpoint wants a file.

        Clamped to the most recent 60 s: the worker buffers up to 120 s and the
        endpoint decodes everything it is handed, so the ceiling has to be
        engine-side (the local faster-whisper engine clamps for the same
        reason).
        """
        if len(audio_frame) > MAX_AUDIO_BYTES:
            log.info("whisper-http input clamped: %.1f s -> 60 s",
                     len(audio_frame) / 32000.0)
            audio_frame = audio_frame[-MAX_AUDIO_BYTES:]
        resp = await self._get_client().post(
            f"{self._base_url}/audio/transcriptions",
            files={"file": ("audio.wav", s16le16k_to_wav(audio_frame), "audio/wav")},
            data={"model": self._model},
        )
        resp.raise_for_status()
        try:
            return (resp.json().get("text") or "").strip()
        except ValueError:
            # 200 with a non-JSON body (an ingress error page, say) is not a
            # crash: an empty transcript makes the worker re-prompt instead.
            log.warning("whisper-http returned a non-JSON body (status %s)",
                        resp.status_code)
            return ""


def resolve_stt(provider: str, config) -> STTEngine:
    provider = (provider or "").lower()
    if provider == "deepgram":
        if not config.deepgram_api_key:
            raise ValueError("INTERVIEW_DEEPGRAM_API_KEY is required for stt_provider=deepgram")
        return DeepgramSTT(config.deepgram_api_key)
    if provider in ("faster-whisper", "whisper"):
        return FasterWhisperSTT(config.whisper_model, config.whisper_device)
    if provider == "whisper-http":
        return RemoteWhisperSTT(config.stt_base_url, config.whisper_model)
    if provider == "stub":
        from interviewer.voice.stubs import StubSTT

        return StubSTT()
    raise ValueError(f"unknown stt_provider: {provider!r} "
                     f"(deepgram | faster-whisper | whisper-http | stub)")
