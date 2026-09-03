"""Cheap energy-based silence trimming for manual answer buffers
(16 kHz mono s16le).

The manual arm records everything between Start answer and Finish —
including the candidate's thinking time. Whisper then decodes the FULL
buffer: measured ~9 s of CPU for 60 s of quiet and ~35 s for 150 s
(RCA 2026-09-03), which is exactly the multi-minute "Transcribing your
answer…" the page showed after a short spoken answer. Trimming to the
speech region first keeps finals at ~1-3 s for a real answer.
"""
import numpy as np

RATE = 16000
_FRAME_MS = 50
_FRAME_N = RATE * _FRAME_MS // 1000
# RMS below this is treated as quiet/room-noise floor (measured room noise
# ~0.004; real speech frames run well above).
_THRESHOLD = 0.006
# Keep a little context around the detected speech so soft word onsets and
# trailing breath are not clipped.
_PAD_MS = 250
_TAIL_MS = 600


def speech_span(pcm: bytes) -> tuple[int, int] | None:
    """(start_sample, end_sample) of the loud region, or None when the
    buffer holds no detectable speech."""
    if not pcm:
        return None
    samples = (np.frombuffer(pcm, dtype=np.int16)
               .astype(np.float32) / 32768.0)
    usable = len(samples) // _FRAME_N * _FRAME_N
    frames = samples[:usable].reshape(-1, _FRAME_N)
    rms = np.sqrt((frames ** 2).mean(axis=1))
    loud = np.flatnonzero(rms > _THRESHOLD)
    if loud.size == 0:
        return None
    pad = _PAD_MS * RATE // 1000
    tail = _TAIL_MS * RATE // 1000
    start = max(0, loud[0] * _FRAME_N - pad)
    end = min(len(samples), (int(loud[-1]) + 1) * _FRAME_N + tail)
    return start, end


def trim_speech_buffer(pcm: bytes) -> bytes:
    """Return ``pcm`` trimmed to its loud region (b"" when nothing loud)."""
    span = speech_span(pcm)
    if span is None:
        return b""
    start, end = span
    samples = np.frombuffer(pcm, dtype=np.int16)[start:end]
    return samples.tobytes()
