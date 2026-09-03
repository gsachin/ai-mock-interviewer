"""Engine protocols for the voice pipeline — the pluggability contract,
mirroring enterprise-rag-core's adapter approach (one contract, swappable
cloud/self-hosted implementations)."""
from typing import AsyncIterator, Protocol


class STTEngine(Protocol):
    """Streaming speech-to-text. Implementations: Deepgram, faster-whisper."""

    async def transcribe(self, audio_frame: bytes) -> str:
        """Incremental transcript for one audio frame (partials welcome)."""
        ...


class TTSEngine(Protocol):
    """Streaming text-to-speech.

    Hard requirement: the interviewer must sound like a NATURAL HUMAN —
    robotic/espeak-class engines are excluded from production. Accepted
    implementations, in preference order:
      elevenlabs — best naturalness (Flash v2.5 for latency)
      cartesia   — Sonic: very low first-audio latency, natural voice presets
      kokoro     — self-hosted open model, pick a high-quality voice preset
      piper      — self-hosted CPU fallback only (acceptable, not preferred)
    The voice preset is selected via InterviewerConfig.tts_voice_id.
    """

    async def synthesize(self, text: str) -> bytes:
        """Audio bytes for one spoken sentence (sentence-level chunking).

        Format contract: **48 kHz mono s16le PCM** (the LiveKit room format,
        see ``interviewer.voice.audio_format``) — playback sinks need no
        conversion.
        """
        ...


class LLMEngine(Protocol):
    """Streaming LLM for interviewer turns. Implementations: vLLM, OpenAI-
    compatible endpoints, Ollama."""

    async def respond_stream(self, messages: list[dict]) -> AsyncIterator[str]:
        """Yield text deltas — TTS starts on sentence one, not on completion."""
        ...


class AudioSink(Protocol):
    """Playback sink for synthesized interviewer audio (48 kHz mono s16le).

    ``play`` hands one sentence to the sink — implementations may queue and
    return immediately (a LiveKit audio source) or block until playback ends.
    ``interrupt`` stops playback at once (barge-in).
    """

    async def play(self, audio: bytes) -> None: ...

    async def interrupt(self) -> None: ...
