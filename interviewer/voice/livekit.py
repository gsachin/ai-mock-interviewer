"""LiveKit-side voice plumbing: the event-driven candidate and the room
playback sink.

``LiveKitCandidate`` decouples the event-driven audio loop (VAD -> STT final)
from the pull-based FSM: the worker pushes transcripts as they arrive and the
brain's ``candidate.answer()`` blocks on the queue — the same shape as the
text-mode ``QueueCandidate`` in ``web/streamlit_app.py``.

``LiveKitAudioSink`` is the ``AudioSink`` implementation for a room: every
synthesized sentence (48 kHz mono s16le) is queued and drained into the
LiveKit audio source by a background playout task; barge-in clears the queue
and stops the current frame mid-sentence.
"""
import asyncio
from typing import Any

from interviewer.brain import CandidateAnswer


class LiveKitCandidate:
    """Candidate whose answers arrive from STT utterance-end events.

    ``push`` is called by the worker's VAD/STT loop; ``answer`` awaits the
    next transcript (in a live 1:1 interview the only speech between the
    question and the next turn is the candidate's answer, so ordering by
    arrival is sufficient).
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[tuple[str, float]] = asyncio.Queue()

    def push(self, transcript: str, stt_ms: float) -> None:
        if transcript.strip():
            self._queue.put_nowait((transcript.strip(), stt_ms))

    async def answer(self, question_id: str) -> CandidateAnswer:
        # Drop stale interjections that arrived while the interviewer was
        # still speaking (barge-in leftovers). The brain calls answer() the
        # moment it starts listening — anything already queued predates the
        # question.
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        text, stt_ms = await self._queue.get()
        return CandidateAnswer(text=text, stt_ms=stt_ms)


class LiveKitAudioSink:
    """Plays synthesized sentences into a LiveKit ``AudioSource``.

    ``play`` enqueues and returns immediately (the brain keeps streaming the
    LLM and synthesizing the next sentence while this one plays); a background
    task drains the queue into the source at 48 kHz in 20 ms frames.
    ``interrupt`` (barge-in) stops the current frame and drops everything
    queued — the brain then checks its barge-in flag and stops generating.
    The next ``play`` clears the stop flag, so the following turn plays
    normally.
    """

    FRAME_MS = 20  # 20 ms frames: smooth playout without flooding the queue

    def __init__(self, source: Any, *, rtc: Any = None):
        """``source`` is a ``livekit.rtc.AudioSource`` (48 kHz mono).
        ``rtc`` is the module for AudioFrame creation — passed in so this
        module imports livekit only where it is used."""
        self._source = source
        self._rtc = rtc
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._stop = False
        self._task: asyncio.Task | None = None

    async def play(self, audio: bytes) -> None:
        """Queue one sentence (48 kHz mono s16le) for playback."""
        if self._rtc is None:  # no room (tests) — accept and drop
            return
        self._stop = False  # a new turn of speech begins
        self._queue.put_nowait(audio)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._drain())

    async def interrupt(self) -> None:
        """Barge-in: drop everything queued; the current frame ends at once.
        The parked drain task (if any) wakes on the next play — its stop flag
        is consumed then, never carried into the next turn."""
        self._stop = True
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def _drain(self) -> None:
        """Background playout: feed 20 ms frames to the LiveKit source until
        the queue is empty, then park until the next play."""
        sample_rate = 48000
        frame_samples = sample_rate * self.FRAME_MS // 1000
        frame_bytes = frame_samples * 2  # s16le mono
        try:
            while True:
                chunk = await self._queue.get()
                if self._stop:  # interrupted while parked — drop the chunk
                    continue
                for i in range(0, len(chunk), frame_bytes):
                    if self._stop:
                        break
                    data = chunk[i:i + frame_bytes]
                    if len(data) < frame_bytes:  # pad the tail frame
                        data += b"\x00" * (frame_bytes - len(data))
                    frame = self._rtc.AudioFrame(
                        data=data, sample_rate=sample_rate, num_channels=1,
                        samples_per_channel=frame_samples)
                    await self._source.capture_frame(frame)
                if self._queue.empty():
                    self._task = None
                    return
        finally:
            self._task = None
