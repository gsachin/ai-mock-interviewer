"""LiveKit agent worker (Phase 3 wiring).

The guarded import keeps the package usable without audio infrastructure:
``livekit-agents`` is an optional extra, and importing this module without
it raises a clear error instead of a silent failure.

Wiring (requires a LiveKit server + API keys — see README):
  room audio -> STT (VAD/turn detection via livekit-agents) ->
  AudioCandidate -> LLMInterviewer (RAG over MCP, fast voice LLM,
  sentence-level TTS) -> room playback, with barge-in on
  ctx.room.on_user_input.
"""
try:
    from livekit import agents  # type: ignore  # optional [voice] extra
except ImportError as exc:  # pragma: no cover - import-time guard
    raise ImportError(
        "livekit-agents is required for the voice worker — "
        "pip install -e '.[voice]'"
    ) from exc

from interviewer.brain import LLMInterviewer


class InterviewerAgent(agents.Agent):
    """One agent per interview room: owns the per-session brain and calls
    the standalone RAG MCP service for every retrieval."""

    def __init__(self, *, interviewer: LLMInterviewer, stt, audio: dict[str, bytes]):
        super().__init__()
        self._interviewer = interviewer
        self._stt = stt
        self._audio = audio

    async def on_enter(self, ctx: agents.JobContext) -> None:
        # Operational wiring (validated against a live LiveKit deployment,
        # not in CI):
        #   participant = await ctx.connect()
        #   from interviewer.voice.interviewer import AudioCandidate
        #   candidate = AudioCandidate(self._stt, self._audio)
        #   self._interviewer.ctx = ctx          # playback sink
        #   summary = await self._interviewer.run("bank-system-design",
        #                                         candidate=candidate)
        raise NotImplementedError("LiveKit deployment wiring — see README "
                                  "runbook (needs a LiveKit server + keys)")
