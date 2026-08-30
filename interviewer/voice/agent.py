"""LiveKit agent worker skeleton (Phase 3).

The guarded import keeps the package usable without audio infrastructure:
``livekit-agents`` is an optional extra, and importing this module without
it raises a clear error instead of a silent failure.

Pipeline to be wired here (order matters for the < 1.5 s budget):
  VAD/turn detection -> STT (streaming partials) -> state machine ->
  RagClient.retrieve_context (cache-first, ~30-150 ms) -> LLM token stream
  -> sentence-level TTS chunks -> SFU -> browser speaker.
"""
try:
    from livekit import agents  # type: ignore  # optional [voice] extra
except ImportError as exc:  # pragma: no cover - import-time guard
    raise ImportError(
        "livekit-agents is required for the voice worker — "
        "pip install -e '.[voice]'"
    ) from exc

from interviewer.rag_client import RagClient
from interviewer.state_machine import Session


class InterviewerAgent(agents.Agent):
    """One agent per interview room. Owns the per-session state machine and
    calls the standalone RAG MCP service for every retrieval."""

    def __init__(self, *, rag: RagClient, session: Session):
        super().__init__()
        self._rag = rag
        self._session = session

    async def on_enter(self, ctx: agents.JobContext) -> None:
        # Phase 3: connect to the room, greet, and run the turn loop:
        #   audio_frame = await ctx.room.wait_for_audio()
        #   partial = await stt.transcribe(audio_frame)
        #   result = await self._rag.retrieve_context(partial, top_k=5)
        #   async for delta in llm.respond_stream([...]):
        #       await tts.synthesize(delta) -> ctx.room.playback()
        raise NotImplementedError("voice pipeline lands in Phase 3")
