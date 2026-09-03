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
import time
from typing import Any

from interviewer.brain import CandidateAnswer


class LiveKitCandidate:
    """Candidate whose answers arrive from the worker's manual answer
    capture (Start answer -> mic buffer -> Finish answer -> STT).

    ``push`` is called by the worker after each manual answer is
    transcribed; ``answer`` awaits the next transcript. ``answer`` is
    bounded by ``timeout_s`` — the no-hang gate (RCA R1): it raises
    ``TimeoutError`` so the brain can re-prompt and move on instead of
    waiting forever for a mic that never delivers speech.
    ``drop_before_ts`` (monotonic) keeps transcripts that arrived at or
    after that instant — the brain uses it so an answer spoken while the
    re-prompt plays is not drained as barge-in junk.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[tuple[str, float, float]] = asyncio.Queue()

    def push(self, transcript: str, stt_ms: float) -> None:
        if transcript.strip():
            self._queue.put_nowait(
                (transcript.strip(), stt_ms, time.monotonic()))

    async def answer(self, question_id: str,
                     timeout_s: float | None = None,
                     drop_before_ts: float | None = None) -> CandidateAnswer:
        # Drop stale interjections that arrived while the interviewer was
        # still speaking (barge-in leftovers) — anything queued before the
        # brain started listening predates the question. When a re-prompt
        # is spoken the brain passes ``drop_before_ts`` instead, keeping
        # transcripts that arrived while the re-prompt played.
        keep: list[tuple[str, float, float]] = []
        while not self._queue.empty():
            try:
                keep.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if drop_before_ts is not None:
            # a re-prompt is in play: keep transcripts that arrived while it
            # was spoken (>= drop_before_ts), discard everything older
            keep = [it for it in keep if it[2] >= drop_before_ts]
        else:
            keep = []                     # default: drain all stale entries
        for it in keep:
            self._queue.put_nowait(it)
        if timeout_s is None:
            text, stt_ms, _ = await self._queue.get()
        else:
            text, stt_ms, _ = await asyncio.wait_for(
                self._queue.get(), timeout=timeout_s)
        return CandidateAnswer(text=text, stt_ms=stt_ms)


class LiveKitReviewer:
    """Page decisions for the review gate after a scored question (Retake /
    Next). The worker pushes each control message; ``decide`` waits for one
    decision with a bounded wait — a timeout (no choice) means the interview
    advances. Mirrors LiveKitCandidate's stale-drop at the gate boundary so
    a leftover decision never answers the next question's gate."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue()

    def push(self, decision: str) -> None:
        if decision in ("next", "retake"):
            self._queue.put_nowait(decision)

    async def decide(self, question_id: str,
                     timeout_s: float | None = None) -> str:
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        if timeout_s is None:
            decision = await self._queue.get()
        else:
            decision = await asyncio.wait_for(self._queue.get(),
                                              timeout=timeout_s)
        return decision if decision in ("next", "retake") else "next"


class EchoGate:
    """Acoustic echo guard for a speakers + mic setup (RCA R1/T6).

    Pure decision logic (no livekit import — unit-testable): the worker's
    VAD consumer asks it what a VAD event means while the interviewer's own
    TTS may be feeding back into the mic. The consumer feeds it the sink's
    playback state, so the gate keeps only per-utterance state.

    Rules (echo discipline, RCA 3.2):
    - Speech that STARTS while the interviewer is playing does not barge in —
      the interviewer's own voice must never interrupt itself.
    - An utterance that started during playback and ENDS just after playback
      stopped (within ``tail_ms``) is the echo tail — dropped, never
      transcribed as a candidate answer. Speech that ends while playback is
      still going is genuine overlap and is kept.
    """

    def __init__(self, tail_ms: float = 250.0):
        self.tail_ms = tail_ms
        self._start_seen = False
        self._started_while_playing = False

    def on_speech_start(self, now: float, playing: bool) -> bool:
        """START_OF_SPEECH decision: True = barge in (interrupt playback)."""
        if not self._start_seen:
            self._start_seen = True
            self._started_while_playing = playing
        return not playing                     # never self-interrupt

    def on_speech_end(self, now: float, playing: bool,
                      stop_ago_ms: float) -> bool:
        """END_OF_SPEECH decision: True = drop the utterance as echo tail."""
        started_while_playing = self._started_while_playing
        self._start_seen = False               # reset for the next utterance
        self._started_while_playing = False
        if not started_while_playing:
            return False
        return (not playing) and stop_ago_ms <= self.tail_ms


class LiveKitAudioSink:
    """Plays synthesized sentences into a LiveKit ``AudioSource``.

    ``play`` enqueues and returns immediately (the brain keeps streaming the
    LLM and synthesizing the next sentence while this one plays); a background
    task drains the queue into the source at 48 kHz in 20 ms frames.
    ``interrupt`` (barge-in) stops the current frame and drops everything
    queued — the brain then checks its barge-in flag and stops generating.
    The next ``play`` clears the stop flag, so the following turn plays
    normally.

    ``playing`` and ``last_stop_ts`` expose the playout state to the worker's
    echo gate (T6): True while frames are being delivered, and the timestamp
    playback last ended.
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
        self.last_stop_ts: float | None = None

    @property
    def playing(self) -> bool:
        return self._task is not None and not self._task.done()

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
        self.last_stop_ts = time.time()        # echo gate: playback stopped
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
                    self.last_stop_ts = time.time()  # natural sentence end
                    self._task = None
                    return
        finally:
            self._task = None
