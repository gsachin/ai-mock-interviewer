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
    """Echo engine: yields canned responses from a script, then empty."""

    def __init__(self, script: list[str] | None = None) -> None:
        self._script = list(script or [])
        self.prompts: list[list[dict]] = []

    async def respond_stream(self, messages: list[dict]) -> AsyncIterator[str]:
        self.prompts.append(messages)
        for line in self._script:
            yield line
        self._script.clear()
