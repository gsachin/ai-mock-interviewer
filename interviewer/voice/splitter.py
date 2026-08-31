"""Sentence accumulation for streaming TTS.

The LLM yields token deltas; ``SentenceAccumulator`` emits complete
sentences as soon as a boundary appears so TTS starts on sentence one
instead of waiting for the full answer — the single biggest wall-clock
saving in the voice round-trip.
"""
import re

_SENTENCE_BOUNDARY = re.compile(r"[.!?]\s+")


class SentenceAccumulator:
    """Consumes text deltas and yields complete sentences; ``flush()``
    returns the trailing partial sentence."""

    def __init__(self, max_chars: int = 200):
        self._max_chars = max_chars
        self._buf = ""

    def feed(self, delta: str) -> list[str]:
        self._buf += delta
        complete: list[str] = []
        while True:
            if len(self._buf) > self._max_chars:
                # hard cap: cut at a word boundary before the limit
                cut = self._buf.rfind(" ", 0, self._max_chars)
                if cut <= 0:
                    cut = self._max_chars
                complete.append(self._buf[:cut].strip())
                self._buf = self._buf[cut:].strip()
                continue
            m = _SENTENCE_BOUNDARY.search(self._buf)
            if not m:
                break
            complete.append(self._buf[:m.end()].strip())
            self._buf = self._buf[m.end():]
        return complete

    def flush(self) -> list[str]:
        if not self._buf.strip():
            return []
        rest, self._buf = self._buf.strip(), ""
        return [rest]
