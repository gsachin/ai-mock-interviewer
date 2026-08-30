"""Text-mode stubs: develop and test the interviewer brain without any audio
infrastructure. Each stub is a trivial implementation of a voice protocol."""

from typing import AsyncIterator


class StubSTT:
    """Transcripts come from a caller-fed queue (simulates VAD + STT)."""

    def __init__(self) -> None:
        self._queue: list[str] = []

    def feed(self, transcript: str) -> None:
        self._queue.append(transcript)

    async def transcribe(self, audio_frame: bytes) -> str:
        return self._queue.pop(0) if self._queue else ""


class StubTTS:
    """Returns empty audio — the text is what a text-mode client displays."""

    async def synthesize(self, text: str) -> bytes:
        return b""


class StubLLM:
    """Canned engine: scripted streaming turns plus canned judge responses.
    Exposes the same ``metrics`` interface as OpenAICompatibleLLM."""

    DEFAULT_EVALUATION = (
        "Correctness: 4 - covers the core mechanism.\n"
        "Depth: 3 - misses trade-offs.\n"
        "Communication: 4 - clear and structured.\n"
        "FOLLOW_UP: none"
    )

    def __init__(self, script: list[str] | None = None,
                 evaluations: list[str] | None = None) -> None:
        self._script = list(script or [])
        self._evaluations = list(evaluations or [self.DEFAULT_EVALUATION])
        self.prompts: list[list[dict]] = []
        self.metrics = type("Metrics", (), {"first_token_ms": 1.0, "total_ms": 2.0})()

    async def respond_stream(self, messages: list[dict]) -> AsyncIterator[str]:
        self.prompts.append(messages)
        line = self._script.pop(0) if self._script else ""
        if line:
            yield line

    async def respond(self, messages: list[dict]) -> str:
        self.prompts.append(messages)
        if self._evaluations:
            return self._evaluations.pop(0)
        return self.DEFAULT_EVALUATION
